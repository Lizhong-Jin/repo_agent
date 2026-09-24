"""Failure classification, bounded retries, and safe installer logs."""

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from cli import install_network as network


@pytest.mark.parametrize(
    "message,category,retry",
    [
        ("CERTIFICATE_VERIFY_FAILED no matching distribution", "证书", False),
        ("x509: certificate signed by unknown authority", "证书", False),
        ("407 Proxy Authentication Required", "认证", False),
        ("ProxyError: cannot connect to proxy", "代理连接", True),
        ("dial tcp: lookup proxy.golang.org: no such host", "DNS", True),
        ("getaddrinfo EAI_AGAIN", "DNS", True),
        ("dial tcp [2607:f8b0::1]:443: i/o timeout", "超时", True),
        ("connection reset by peer", "临时网络", True),
        ("HTTP 503 Service Unavailable", "临时网络", True),
        ("No matching distribution found", "包版本", False),
        ("npm ERR! code ETARGET", "包版本", False),
        ("compile failed: undefined symbol", "安装命令", False),
    ],
)
def test_classifies_errors(message, category, retry):
    actual, retriable, hint = network.classify_failure(message)
    assert category in actual and retriable is retry and hint


def test_retries_transient_error_then_succeeds(monkeypatch, tmp_path):
    calls, delays = [], []

    def run(command, **kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise subprocess.CalledProcessError(1, command, stderr="connection reset")
        return SimpleNamespace(stdout="downloaded", stderr="")

    monkeypatch.setattr(network, "execute", run)
    monkeypatch.setattr(network.time, "sleep", delays.append)
    network.run_download(["go", "install"], label="gopls")
    assert len(calls) == 3 and delays == [2, 4]
    assert calls[0]["env"]["PIP_RETRIES"] == "0"
    logs = list((tmp_path / "user-state/repo-agent/install-logs").glob("*.log"))
    assert len(logs) == 1 and "尝试 3：成功" in logs[0].read_text()
    assert logs[0].stat().st_mode & 0o777 == 0o600


def test_exhaustion_preserves_safe_actionable_log(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("AGENT_INSTALL_PROXY", "https://alice:password@proxy.example:443")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "private-key-value")
    monkeypatch.setenv("AGENT_INSTALL_RETRIES", "1")
    calls = []

    def run(command, **kw):
        calls.append(kw)
        raise subprocess.CalledProcessError(
            1,
            command,
            stderr="timeout https://alice:password@proxy.example:443 private-key-value https://other.example/?token=secret-token",
        )

    monkeypatch.setattr(network, "execute", run)
    monkeypatch.setattr(network.time, "sleep", lambda _: None)
    with pytest.raises(network.DownloadError, match="超时") as error:
        network.run_download(["npm", "install"], label="JS/TS")
    text = str(error.value) + capsys.readouterr().out
    for log in (tmp_path / "user-state/repo-agent/install-logs").glob("*.log"):
        text += log.read_text()
    assert len(calls) == 2
    for secret in ("alice", "password@", "private-key-value", "secret-token"):
        assert secret not in text
    assert calls[0]["env"]["HTTPS_PROXY"] == os.environ["AGENT_INSTALL_PROXY"]


def test_permanent_error_is_not_retried(monkeypatch):
    def run(command, **kw):
        raise subprocess.CalledProcessError(1, command, stderr="certificate verify failed")

    monkeypatch.setattr(network, "execute", run)
    monkeypatch.setattr(
        network.time, "sleep", lambda _: pytest.fail("must not retry certificate errors")
    )
    with pytest.raises(network.DownloadError, match="PIP_CERT"):
        network.run_download(["pip", "install"], label="pip")


def test_timeout_bytes_are_handled_and_retry_is_bounded(monkeypatch):
    monkeypatch.setenv("AGENT_INSTALL_RETRIES", "0")

    def run(command, **kw):
        raise subprocess.TimeoutExpired(command, 1, output=b"partial output")

    monkeypatch.setattr(network, "execute", run)
    with pytest.raises(network.DownloadError, match="超时"):
        network.run_download(["go", "install"], label="gopls")


@pytest.mark.parametrize("value", ["invalid", "-1", "6"])
def test_invalid_retry_setting_rejected(value, monkeypatch):
    monkeypatch.setenv("AGENT_INSTALL_RETRIES", value)
    with pytest.raises(ValueError, match="AGENT_INSTALL_RETRIES"):
        network.network_options()


def test_download_settings_are_per_process(monkeypatch):
    env = {
        "PIP_INDEX_URL": "old",
        "AGENT_INSTALL_PYPI_INDEX": "new",
        "AGENT_INSTALL_GOPROXY": "https://go.example",
        "AGENT_INSTALL_NPM_REGISTRY": "https://npm.example",
    }
    output = network.download_environment(env)
    assert output["PIP_INDEX_URL"] == "new"
    assert output["GOPROXY"] == "https://go.example"
    assert output["npm_config_registry"] == "https://npm.example"
    assert env["PIP_INDEX_URL"] == "old"


def test_real_process_failure_is_classified_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_INSTALL_RETRIES", "0")
    with pytest.raises(network.DownloadError, match="包版本"):
        network.run_download(
            [
                sys.executable,
                "-c",
                "import sys; print('No matching distribution found'); sys.exit(1)",
            ],
            label="offline test",
        )
    assert (
        "No matching distribution"
        in next((tmp_path / "user-state/repo-agent/install-logs").glob("*.log")).read_text()
    )


def test_real_timeout_stops_child_before_retry_or_rollback(tmp_path, monkeypatch):
    import time

    child_output = tmp_path / "late-write"
    child = f"import time; from pathlib import Path; time.sleep(1); Path({str(child_output)!r}).write_text('bad')"
    parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(10)"
    monkeypatch.setattr(network, "network_options", lambda: (0, 0.15))
    with pytest.raises(network.DownloadError, match="超时"):
        network.run_download([sys.executable, "-c", parent], label="timeout test")
    time.sleep(1.1)
    assert not child_output.exists()
