"""Approval, endpoint, credential and archive boundaries without external side effects."""

import asyncio
import base64
import hashlib
import io
import json
import os
import stat
import tarfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_web_fetch import Network, reply

from agent import AgentRuntime
from host_support.cancellation import CancellationContext, RunCancelled, cancellation_scope
from llm import ToolCall
from tools._internal.dependency_service import sha256, unpack_npm, unpack_wheels
from tools._internal.file_access import FileAccess
from tools._internal.service_http import ServiceHTTP
from tools._internal.web_errors import WebError
from tools.access_policy import AccessPolicy
from tools.permissions import PermissionManager, PermissionTool
from tools.scoped_services import ScopedServices, ScopedServiceTool


class HTTP:
    def __init__(self):
        self.calls = []
        self.respond = lambda *args, **kwargs: b"{}"

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"status": 200, "body": self.respond(*args, **kwargs)}


@pytest.fixture
def service(tmp_path):
    manager = PermissionManager(tmp_path, backend="native")
    from tools._internal.operation_journal import OperationJournal

    return ScopedServices(
        manager,
        http=HTTP(),
        github_token="test-secret-token",
        journal=OperationJournal(tmp_path / "state", "f" * 32),
    )


@pytest.fixture
def http_factory():
    clients = []

    def create(*args, **kwargs):
        client = ServiceHTTP(*args, **kwargs)
        clients.append(client)
        return client

    yield create
    for client in clients:
        client.close()


def tool(service, name="network_request"):
    return ScopedServiceTool(service, name)


def approve(service, result):
    assert result.success and result.data["permission_required"], result
    assert not service.http.calls
    service.manager.decide(result.data["request"]["id"], approve=True)


def wheel(entries=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "demo-1.dist-info/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        for path, data in (entries or {"demo.py": "VALUE = 73"}).items():
            if isinstance(path, str):
                # Passing a string to writestr constructs ZipInfo, which rewrites
                # backslashes on Windows and truncates NUL on every platform.
                # Keep malicious fixtures malicious in both ZIP headers.
                entry = zipfile.ZipInfo("placeholder")
                entry.filename = entry.orig_filename = path
            else:
                entry = path
            archive.writestr(entry, data)
    return stream.getvalue()


@pytest.fixture(params=["posix", "windows"])
def zip_separators(request, monkeypatch):
    # Exercise both ZIP name normalization behaviors on every CI runner, without
    # modifying the real filesystem's os module or skipping native Windows tests.
    values = {**vars(os), "sep": "/", "altsep": None}
    if request.param == "windows":
        values.update(sep="\\", altsep="/")
    monkeypatch.setattr(zipfile, "os", SimpleNamespace(**values))
    return request.param


def dependency(service, blob=None):
    blob = wheel() if blob is None else blob
    lock = {
        "version": 1,
        "wheels": [
            {
                "url": "https://files.pythonhosted.org/packages/demo-1-py3-none-any.whl",
                "sha256": sha256(blob),
            }
        ],
    }
    (service.workspace / "deps.lock.json").write_text(json.dumps(lock))
    service.http.respond = lambda *args, **kwargs: blob
    return {"lock_path": "deps.lock.json"}


def test_exact_single_use_approval_and_reset(service):
    args = {"url": "https://example.org/a?query=one"}
    action = tool(service)
    requested = action.execute(args)
    approve(service, requested)
    assert service.manager.snapshot()["policy"] == "scoped"
    assert action.execute(args).success
    assert len(service.http.calls) == 1 and not service.manager.profile.operations
    service.http.calls.clear()
    approve(service, action.execute(args))
    service.manager.reset()
    assert service.manager.snapshot()["policy"] == "strict"
    assert action.execute(args).data["permission_required"] and not service.http.calls


def test_scope_is_not_mutable_through_arguments_requests_or_snapshots(service):
    scope = {"url": "https://example.org/approved"}
    requested = service.manager.request("network", reason="test", scope=scope)
    scope["url"] = "https://evil.example.org/"
    requested["scope"]["url"] = scope["url"]
    snapshot = service.manager.snapshot()
    snapshot["pending"][0]["scope"]["url"] = scope["url"]
    service.manager.decide(requested["id"], approve=True)
    assert not service.manager.consume("network", scope)
    assert service.manager.consume("network", {"url": "https://example.org/approved"})


@pytest.mark.parametrize("damage", ["url", "query", "revoke", "failure"])
def test_changed_target_revocation_and_failures_cannot_reuse_grant(service, damage):
    args = {"url": "https://example.org/a?query=one"}
    action = tool(service)
    approve(service, action.execute(args))
    if damage == "revoke":
        service.manager.revoke(service.manager.profile.operations[0][0])
    elif damage == "url":
        args["url"] = "https://evil.example.org/a?query=one"
    elif damage == "query":
        args["url"] = "https://example.org/a?query=secret"
    else:
        service.http.respond = lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("test-secret-token")
        )
    result = action.execute(args)
    if damage == "failure":
        assert not result.success and "test-secret-token" not in repr(result)
        assert not service.manager.profile.operations
    else:
        assert result.data["permission_required"] and not service.http.calls


@pytest.mark.parametrize("mode", ["review", "unhealthy", "other_manager"])
def test_services_fail_closed_for_review_unhealthy_and_foreign_authority(service, mode):
    action = tool(service)
    if mode == "review":
        service.manager.profile = replace(service.manager.profile, access_mode="review")
        assert not AccessPolicy("review", permissions=service.manager).allows(action)
    elif mode == "unhealthy":
        service.backend = SimpleNamespace(healthy=False)
    else:
        assert not AccessPolicy(
            permissions=PermissionManager(service.workspace, backend="native")
        ).allows(action)
        return
    assert not action.execute({"url": "https://example.org/"}).success
    assert not service.manager.pending and not service.http.calls


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/",
        "http://example.org/",
        "https://example.org:80/",
        "https://example.org:444/",
        "https://user:pass@example.org/",
        "file:///etc/passwd",
        "https://example.org/#fragment",
    ],
)
def test_invalid_network_scope_rejected_before_approval(service, url):
    assert not tool(service).execute({"url": url}).success
    assert not service.manager.pending and not service.http.calls


def test_http_pins_dns_no_ambient_auth_proxy_or_redirect(monkeypatch, http_factory):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("REPO_AGENT_GITHUB_TOKEN", "ambient-secret")
    network = Network(lambda _: reply("ok"))
    http = http_factory(network.backend)
    assert http.request("GET", "https://example.org/")["body"] == b"ok"
    stream = network.streams[0]
    assert stream.host == "8.8.8.8" and stream.sni == "example.org" and stream.closed
    assert b"authorization" not in bytes(stream.written).lower()
    network.respond = lambda _: reply(
        status=302, headers={"Location": "https://other.example.org/"}
    )
    with pytest.raises(ValueError):
        http.request("GET", "https://api.github.com/repos/o/r", token="specific-token")
    assert len(network.streams) == 2 and network.streams[1].closed
    assert b"specific-token" in bytes(network.streams[1].written)
    with pytest.raises(ValueError):
        http.request("GET", "https://evil.example.org/", token="specific-token")
    assert len(network.streams) == 2


@pytest.mark.parametrize("attack", ["mixed_dns", "peer", "oversized", "partial"])
def test_http_transport_enforces_existing_ssrf_and_body_limits(attack, http_factory):
    network = Network(
        lambda _: reply(
            b"x" * 100 if attack == "oversized" else b"ok",
            status=206 if attack == "partial" else 200,
        ),
        addresses=["8.8.8.8", "127.0.0.1"] if attack == "mixed_dns" else None,
        peer="1.1.1.1" if attack == "peer" else None,
    )
    with pytest.raises((ValueError, WebError)):
        http_factory(network.backend).request("GET", "https://example.org/", limit=10)
    assert all(stream.closed for stream in network.streams)
    if attack == "mixed_dns":
        assert not network.streams
    if attack == "peer":
        assert not network.streams[0].written


def test_http_cancellation_closes_stream_and_consumes_grant(service, http_factory):
    network = Network(lambda _: reply(), delay=30)
    service.http = http_factory(network.backend)
    args = {"url": "https://example.org/"}
    requested = tool(service).execute(args)
    service.manager.decide(requested.data["request"]["id"], approve=True)
    context = CancellationContext()
    timer = threading.Timer(0.15, context.cancel)
    timer.start()
    try:
        with cancellation_scope(context), pytest.raises(RunCancelled):
            tool(service).execute(args)
    finally:
        timer.cancel()
        timer.join()
    assert not service.manager.profile.operations
    assert network.streams and all(stream.closed for stream in network.streams)


@pytest.mark.parametrize("failure", ["cancel", "timeout"])
def test_slow_os_dns_cannot_delay_cancellation_or_start_late_http(
    http_factory, monkeypatch, failure
):
    network = Network(lambda _: reply())
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    context = CancellationContext()

    def blocking_dns():
        entered.set()
        try:
            assert release.wait(3), "Test must release its resolver"
            return ["8.8.8.8"]
        finally:
            finished.set()

    async def resolve(*args):
        if failure == "cancel":
            context.cancel()
        return await asyncio.get_running_loop().run_in_executor(None, blocking_dns)

    network.resolve = resolve
    monkeypatch.setattr("tools._internal.service_http.REQUEST_TIMEOUT_SECONDS", 0.15)
    http = http_factory(network.backend)
    started = time.monotonic()
    try:
        with (
            cancellation_scope(context),
            pytest.raises(RunCancelled if failure == "cancel" else TimeoutError),
        ):
            http.request("GET", "https://example.org/")
        assert entered.is_set()
        http.close()
        assert time.monotonic() - started < 1
        assert not finished.is_set() and http._loop.is_closed()
    finally:
        release.set()
        assert finished.wait(3)
    assert not network.streams
    with pytest.raises(ValueError, match="closed"):
        http.request("GET", "https://example.org/")


def test_total_http_deadline_closes_slow_stream(http_factory, monkeypatch):
    network = Network(lambda _: reply(), delay=30)
    monkeypatch.setattr("tools._internal.service_http.REQUEST_TIMEOUT_SECONDS", 0.02)
    with pytest.raises(TimeoutError):
        http_factory(network.backend).request("GET", "https://example.org/")
    assert network.streams and all(stream.closed for stream in network.streams)


def test_dependencies_verified_without_host_execution_or_overwrite(service):
    args = dependency(service)
    action = tool(service, "install_python_dependencies")
    approve(service, action.execute(args))
    result = action.execute(args)
    assert result.success, result
    directory = service.workspace / result.data["directory"]
    assert (directory / "demo.py").read_text() == "VALUE = 73"
    assert (directory / "repo-agent-manifest.json").is_file()
    assert not result.data["console_scripts_installed"]
    assert len(service.http.calls) == 1
    service.http.calls.clear()
    approve(service, action.execute(args))
    assert not action.execute(args).success
    assert not service.http.calls
    assert (directory / "demo.py").read_text() == "VALUE = 73"


@pytest.mark.parametrize("damage", ["hash", "lock_changed", "source", "target_link"])
def test_dependencies_refuse_changed_content_and_unsafe_destinations(service, tmp_path, damage):
    args = dependency(service)
    action = tool(service, "install_python_dependencies")
    approve(service, action.execute(args))
    if damage == "hash":
        service.http.respond = lambda *args, **kwargs: b"tampered"
    elif damage == "lock_changed":
        (service.workspace / "deps.lock.json").write_text('{"version":1,"wheels":[]}')
    elif damage == "source":
        source = service.workspace / "deps.lock.json"
        os.link(source, service.workspace / "second-link")
    else:
        outside = tmp_path.parent / (tmp_path.name + "-outside")
        outside.mkdir()
        try:
            (service.workspace / ".agent-dependencies").symlink_to(
                outside, target_is_directory=True
            )
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                pytest.skip("Windows symlink privilege unavailable")
            raise
    assert not action.execute(args).success
    assert not list(service.workspace.glob(".agent-dependencies/*/repo-agent-manifest.json"))
    if damage in {"source", "lock_changed", "target_link"}:
        assert not service.http.calls


@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        "/escape.py",
        "C:/escape.py",
        "a\\b.py",
        "a.py\x00hidden",
        "\x00a.py",
        "a/./b.py",
        "a/../b.py",
        "a//b.py",
        "./a.py",
        "pkg//",
        "x:stream",
        "CON",
        "a/.env",
        "a.pth",
        "sitecustomize.py",
        "pkg.data/scripts/run",
        "repo-agent-manifest.json",
    ],
)
def test_wheel_cannot_escape_or_install_startup_hooks(path, zip_separators):
    with pytest.raises(ValueError):
        unpack_wheels([wheel({path: "malicious"})])


@pytest.mark.parametrize("path", ["a\\b.py", "a.py\x00hidden", "pkg//", "a/./b.py"])
def test_wheel_fixture_preserves_raw_names_in_both_headers(path, zip_separators):
    blob = wheel({path: "malicious"})
    # Names occur in the local header and the central directory, never the data.
    assert blob.count(path.encode()) == 2
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        entry = archive.infolist()[1]
        assert entry.orig_filename == path
        if "\x00" in path or (zip_separators == "windows" and "\\" in path):
            assert entry.filename != path
        else:
            assert entry.filename == path


@pytest.mark.parametrize("path", ["a\\b.py", "a.py\x00hidden", "pkg//"])
def test_invalid_wheel_names_rejected_before_reading_any_content(path, zip_separators, monkeypatch):
    blob = wheel({path: "malicious"})
    with monkeypatch.context() as patch:
        patch.setattr(
            zipfile.ZipFile, "open", lambda *a, **kw: pytest.fail("Read before name validation")
        )
        with pytest.raises(ValueError):
            unpack_wheels([blob])


def test_valid_wheel_directories_and_unicode_names_remain_supported(zip_separators):
    files = unpack_wheels([wheel({"pkg/": "", "pkg/示例.py": "VALUE = 73"})])
    assert files["pkg/示例.py"] == b"VALUE = 73"
    assert "pkg" not in files


def test_wheel_metadata_name_is_checked_before_reading(zip_separators, monkeypatch):
    # Replace the separator in both headers, retaining lengths and payload CRC.
    blob = wheel().replace(b"demo-1.dist-info/WHEEL", b"demo-1.dist-info\\WHEEL")
    assert blob.count(b"demo-1.dist-info\\WHEEL") == 2
    monkeypatch.setattr(
        zipfile.ZipFile, "open", lambda *a, **kw: pytest.fail("Read invalid WHEEL name")
    )
    with pytest.raises(ValueError):
        unpack_wheels([blob])


@pytest.mark.parametrize("path", ["a\\b.py", "a.py\x00hidden", "a/./b.py", "pkg//"])
def test_install_rejects_raw_invalid_names_without_writing(service, zip_separators, path):
    args = dependency(service, wheel({path: "malicious"}))
    action = tool(service, "install_python_dependencies")
    approve(service, action.execute(args))
    result = action.execute(args)
    assert not result.success and result.error_code == "SCOPED_SERVICE_FAILED"
    assert len(service.http.calls) == 1
    assert not (service.workspace / ".agent-dependencies").exists()
    assert not service.manager.profile.operations


def test_wheel_symlinks_collisions_and_expansion_budget():
    link = zipfile.ZipInfo("linked.py")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError):
        unpack_wheels([wheel({link: "outside"})])
    with pytest.raises(ValueError):
        unpack_wheels([wheel({"A.py": "a", "a.py": "b"})])
    with pytest.raises(ValueError):
        unpack_wheels([wheel({"a": "a", "a/b": "b"})])


@pytest.mark.parametrize("manager", ["python", "npm"])
@pytest.mark.parametrize("paths", [("é/a", "e\u0301/b"), ("A/a", "a/b"), ("a", "a/b")])
def test_archives_reject_portable_directory_aliases_and_ancestor_files(manager, paths):
    with pytest.raises(ValueError):
        if manager == "python":
            unpack_wheels([wheel(dict.fromkeys(paths, "value"))])
        else:
            unpack_npm(
                [npm_archive({"package/" + p: b"value" for p in paths})],
                [{"path": "node_modules/demo"}],
            )


def publish_arguments(service):
    (service.workspace / "code.py").write_bytes(b"print('approved')\n")
    return {
        "operation": "publish_files",
        "repo": "owner/project",
        "branch": "feature/change",
        "expected_sha": "a" * 40,
        "message": "Reviewed update",
        "files": ["code.py"],
    }


def github_responses(service, *, advanced=False):
    def respond(method, url, **kwargs):
        if "/git/ref/" in url:
            value = {"object": {"sha": ("b" if advanced else "a") * 40}}
        elif method == "GET":
            value = {"tree": {"sha": "c" * 40}}
        elif method == "PATCH":
            value = {
                "ref": "refs/heads/feature/change",
                "object": {"sha": "d" * 40, "type": "commit"},
            }
        else:
            value = {"sha": "d" * 40}
        return json.dumps(value).encode()

    service.http.respond = respond


def test_github_uploads_only_approved_bytes_and_never_force_pushes(service):
    args = publish_arguments(service)
    github_responses(service)
    action = tool(service, "github")
    approve(service, action.execute(args))
    result = action.execute(args)
    assert result.success, result
    assert service.http.calls[-1][0][0] == "PATCH"
    assert service.http.calls[-1][1]["payload"] == {"sha": "d" * 40, "force": False}
    assert all(
        call[0][1].startswith("https://api.github.com/repos/owner/project/")
        for call in service.http.calls
    )
    assert all(call[1]["token"] == "test-secret-token" for call in service.http.calls)
    assert not service.manager.profile.operations


@pytest.mark.parametrize("damage", ["file_changed", "branch", "repo", "advanced", "credentials"])
def test_github_changes_require_reapproval_or_refuse_without_mutation(service, damage):
    args = publish_arguments(service)
    github_responses(service, advanced=damage == "advanced")
    action = tool(service, "github")
    approve(service, action.execute(args))
    if damage == "file_changed":
        (service.workspace / "code.py").write_text("changed")
    elif damage in {"branch", "repo"}:
        args[damage] = "other/target"
    elif damage == "credentials":
        service.github._token = None
    result = action.execute(args)
    assert not result.success or result.data["permission_required"]
    assert all(call[0][0] == "GET" for call in service.http.calls)


@pytest.mark.parametrize("path", [".env", ".git/config", "../outside.txt", ".ssh/id_rsa"])
def test_github_never_reads_protected_files_for_upload(service, path):
    args = publish_arguments(service)
    args["files"] = [path]
    assert not tool(service, "github").execute(args).success
    assert not service.manager.pending and not service.http.calls


def test_github_pr_is_draft_and_full_payload_is_approved(service):
    args = {
        "operation": "create_pull_request",
        "repo": "owner/project",
        "head": "feature",
        "base": "main",
        "title": "Review",
        "body": "Exact description",
    }
    action = tool(service, "github")
    requested = action.execute(args)
    assert requested.data["request"]["scope"]["body"] == "Exact description"
    approve(service, requested)
    service.http.respond = lambda *args, **kwargs: (
        b'{"number":1,"html_url":"https://github.com/owner/project/pull/1"}'
    )
    assert action.execute(args).data["draft"] is True
    assert service.http.calls[0][1]["payload"]["draft"] is True


def test_model_cannot_grant_or_restore_single_use_authority(service):
    result = PermissionTool(service.manager, "request_permission").execute(
        {
            "capability": "network",
            "scope": {"url": "https://example.org/"},
            "reason": "Please fetch",
        }
    )
    assert result.success and not service.manager.profile.operations
    service.manager.decide(result.data["request"]["id"], approve=True)
    snapshot = service.manager.snapshot()
    other = PermissionManager(service.workspace, backend="native")
    assert snapshot["operations"] and not other.profile.operations


def test_scoped_approval_save_failure_rolls_back(open_conversation, monkeypatch):
    from test_permissions import bind

    conversation = open_conversation()
    manager, controller = bind(conversation)
    request = manager.request("network", reason="fetch", scope={"url": "https://example.org/"})
    monkeypatch.setattr(
        conversation, "checkpoint", lambda **kw: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(OSError):
        controller.command("/permissions approve " + request["id"])
    assert not manager.profile.operations and request["id"] in manager.pending


def npm_archive(entries=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in (
            entries
            or {
                "package/package.json": (
                    b'{"name":"demo","main":"index.js","scripts":{"postinstall":"never-run"}}'
                ),
                "package/index.js": b"module.exports = 73;",
            }
        ).items():
            entry = name if isinstance(name, tarfile.TarInfo) else tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return stream.getvalue()


def test_npm_preserves_nested_pinned_dependencies_without_running_scripts(service, monkeypatch):
    blob = npm_archive()
    record = {
        "resolved": "https://registry.npmjs.org/demo/-/demo-1.0.0.tgz",
        "integrity": "sha512-" + base64.b64encode(hashlib.sha512(blob).digest()).decode(),
    }
    lock = {
        "lockfileVersion": 3,
        "packages": {
            "": {"name": "root", "scripts": {"install": "never-run"}},
            "node_modules/demo": record,
            "node_modules/demo/node_modules/nested": record,
        },
    }
    (service.workspace / "package-lock.json").write_text(json.dumps(lock))
    service.http.respond = lambda *args, **kw: blob
    monkeypatch.setattr(
        "subprocess.Popen", lambda *args, **kw: pytest.fail("No host package commands")
    )
    action, args = tool(service, "install_npm_dependencies"), {"lock_path": "package-lock.json"}
    approve(service, action.execute(args))
    result = action.execute(args)
    assert result.success, result
    root = service.workspace / result.data["directory"]
    assert (root / "node_modules/demo/index.js").read_bytes() == b"module.exports = 73;"
    assert (root / "node_modules/demo/node_modules/nested/package.json").is_file()
    assert not (service.workspace / "node_modules").exists()
    assert not result.data["console_scripts_installed"]


@pytest.mark.parametrize("damage", ["link", "hash", "registry", "path", "version"])
def test_npm_rejects_unsafe_lock_before_approval(service, damage):
    record = {
        "resolved": "https://registry.npmjs.org/demo/-/demo-1.0.0.tgz",
        "integrity": "sha512-" + base64.b64encode(b"a" * 64).decode(),
    }
    if damage == "link":
        record["link"] = True
    elif damage == "hash":
        record["integrity"] = "sha1-unsupported"
    elif damage == "registry":
        record["resolved"] = "https://evil.example.org/demo.tgz"
    key = "../outside" if damage == "path" else "node_modules/demo"
    (service.workspace / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 2 if damage == "version" else 3, "packages": {key: record}})
    )
    assert (
        not tool(service, "install_npm_dependencies")
        .execute({"lock_path": "package-lock.json"})
        .success
    )
    assert not service.manager.pending and not service.http.calls


@pytest.mark.parametrize(
    "path",
    [
        "escape",
        "package/../escape",
        "package/.npmrc",
        "package/node_modules/evil/index.js",
        "package/C:/escape",
        "package/a:stream",
    ],
)
def test_npm_archive_cannot_traverse_or_smuggle_dependencies(path):
    with pytest.raises(ValueError):
        unpack_npm([npm_archive({path: b"unsafe"})], [{"path": "node_modules/demo"}])


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE]
)
def test_npm_archive_rejects_all_link_and_device_types(kind):
    entry = tarfile.TarInfo("package/link")
    entry.type, entry.linkname = kind, "outside"
    with pytest.raises(ValueError):
        unpack_npm([npm_archive({entry: b""})], [{"path": "node_modules/demo"}])


def test_windows_native_npm_does_not_relax_existing_snapshot_exclusions(service):
    service.backend = SimpleNamespace(platform_name="windows", healthy=True)
    result = tool(service, "install_npm_dependencies").execute({"lock_path": "package-lock.json"})
    assert not result.success and result.error_code == "SCOPED_SERVICE_UNAVAILABLE"
    assert not service.manager.pending and not service.http.calls


def test_request_stops_runtime_before_later_service_calls(service):
    from test_permissions import Model

    action = tool(service)
    model = Model(
        [
            ToolCall("one", "network_request", {"url": "https://example.org/"}),
            ToolCall("two", "network_request", {"url": "https://other.example.org/"}),
        ]
    )
    runtime = AgentRuntime(model, [action], access_policy=AccessPolicy(permissions=service.manager))
    result = runtime.run("Fetch")
    assert result.pause_reason == "permission_required" and result.resumable
    assert json.loads(result.history[-1].content)["error"]["code"] == "PERMISSION_PENDING"
    assert not service.http.calls and len(service.manager.pending) == 1


def test_concurrent_consumers_cannot_double_spend(service):
    action, args = tool(service), {"url": "https://example.org/"}
    approve(service, action.execute(args))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(action.execute, [args, args]))
    assert len(service.http.calls) == 1
    assert sum(bool(r.data.get("permission_required")) for r in results) == 1


@pytest.mark.parametrize("failure", ["cancel", "disk"])
def test_interrupted_install_leaves_no_completion_receipt_or_staged_files(
    service, monkeypatch, failure
):
    args = dependency(service)
    action = tool(service, "install_python_dependencies")
    approve(service, action.execute(args))
    real_stage = FileAccess.stage
    context = CancellationContext()
    count = 0

    def stage(access, *args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            if failure == "disk":
                raise OSError("disk full")
            context.cancel()
        return real_stage(access, *args, **kwargs)

    monkeypatch.setattr(FileAccess, "stage", stage)
    with cancellation_scope(context):
        if failure == "cancel":
            with pytest.raises(RunCancelled):
                action.execute(args)
        else:
            assert not action.execute(args).success
    directory = service.workspace / ".agent-dependencies"
    assert not list(directory.rglob("repo-agent-manifest.json"))
    assert not list(directory.rglob("*.tmp"))
    assert not service.manager.profile.operations


def test_npm_hash_mismatch_writes_nothing(service):
    (service.workspace / "package-lock.json").write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "node_modules/demo": {
                        "resolved": "https://registry.npmjs.org/demo/-/demo-1.tgz",
                        "integrity": "sha512-" + base64.b64encode(b"x" * 64).decode(),
                    }
                },
            }
        )
    )
    args, action = {"lock_path": "package-lock.json"}, tool(service, "install_npm_dependencies")
    approve(service, action.execute(args))
    service.http.respond = lambda *a, **kw: npm_archive()
    assert not action.execute(args).success
    assert not (service.workspace / ".agent-dependencies").exists()
    assert not service.manager.profile.operations


def test_github_rechecks_bytes_after_approval_consumption(service, monkeypatch):
    args = publish_arguments(service)
    action = tool(service, "github")
    approve(service, action.execute(args))
    consume = service.manager.consume

    def replace_after_consume(*args):
        result = consume(*args)
        (service.workspace / "code.py").write_text("race")
        return result

    monkeypatch.setattr(service.manager, "consume", replace_after_consume)
    assert not action.execute(args).success
    assert not service.http.calls and not service.manager.profile.operations


def test_github_branch_race_does_not_update_reference(service):
    args = publish_arguments(service)
    github_responses(service)
    normal = service.http.respond
    refs = 0

    def race(method, url, **kwargs):
        nonlocal refs
        if "/git/ref/" in url:
            refs += 1
            if refs == 2:
                return json.dumps({"object": {"sha": "b" * 40}}).encode()
        return normal(method, url, **kwargs)

    service.http.respond = race
    action = tool(service, "github")
    approve(service, action.execute(args))
    assert not action.execute(args).success
    assert refs == 2 and all(call[0][0] != "PATCH" for call in service.http.calls)


def test_docker_composition_installs_only_into_private_workspace(tmp_path, monkeypatch):
    from cli import execution_environment as environment
    from cli.arguments import parse_arguments
    from tools import create_default_tools

    original, private = tmp_path / "project", tmp_path / "private"
    original.mkdir()
    private.mkdir()
    backend = SimpleNamespace(healthy=True)
    session = SimpleNamespace(
        workspace=private, directory=private, backend=backend, tools=lambda **kw: []
    )
    monkeypatch.setattr(environment, "SandboxSession", lambda *a, **kw: session)
    monkeypatch.setattr(
        environment,
        "detect_environment",
        lambda **kw: SimpleNamespace(profile="standard", reason="test"),
    )
    monkeypatch.setattr(environment, "check_image_profile", lambda *a, **kw: None)
    monkeypatch.setenv("REPO_AGENT_GITHUB_TOKEN", "host-only-test-token")
    _, args = parse_arguments(["--sandbox", "docker"])
    with ExitStack() as cleanup:
        opened = environment.open_execution_environment(
            args, original, SimpleNamespace(project=original, data={}), None, cleanup
        )
        services = [t for t in opened.tools if type(t) is ScopedServiceTool]
        assert len(services) == 4
        service = services[0].service
        service.http = HTTP()
        args = dependency(service)
        action = tool(service, "install_python_dependencies")
        approve(service, action.execute(args))
        result = action.execute(args)
        assert result.success, result
        assert (private / result.data["directory"] / "demo.py").is_file()
        assert not list(original.iterdir())
        assert "host-only-test-token" not in json.dumps(opened.permissions.snapshot())
        backend.healthy = False
        assert not tool(service).execute({"url": "https://example.org/"}).success
    assert not any(
        type(t) is ScopedServiceTool for t in create_default_tools(private, isolated_execution=True)
    )
