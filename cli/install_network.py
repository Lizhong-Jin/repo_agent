"""Bounded download retries and redacted diagnostics, available before dependencies."""

import os
import re
import subprocess
import tempfile
import time

if not __package__:
    from _bootstrap import enable_host_support

    enable_host_support()

from host_support.paths import app_directory
from host_support.processes import kill_process_group, start_process


class DownloadError(ValueError):
    """Safe summary; subprocess output and environment are never part of the exception."""


def network_options():
    values = []
    for key, default, maximum in (
        ("AGENT_INSTALL_RETRIES", 2, 5),
        ("AGENT_INSTALL_TIMEOUT", 600, 3600),
    ):
        try:
            value = int(os.environ.get(key, str(default)))
        except ValueError:
            raise ValueError(f"{key} 必须是整数") from None
        if not (0 if key.endswith("RETRIES") else 1) <= value <= maximum:
            raise ValueError(f"{key} 超出允许范围（最大 {maximum}）")
        values.append(value)
    return tuple(values)


def download_environment(env=None):
    result = dict(os.environ if env is None else env)
    for source, targets in {
        "AGENT_INSTALL_PROXY": ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"),
        "AGENT_INSTALL_PYPI_INDEX": ("PIP_INDEX_URL",),
        "AGENT_INSTALL_NPM_REGISTRY": ("npm_config_registry",),
        "AGENT_INSTALL_GOPROXY": ("GOPROXY",),
    }.items():
        if result.get(source):
            for target in targets:
                result[target] = result[source]
    result.setdefault("PIP_RETRIES", "0")
    result.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    result.setdefault("npm_config_fetch_retries", "0")
    return result


def redact(output, env):
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    text = output or ""
    # Also remove literal secrets when a subprocess includes them outside a URL.
    for key, value in env.items():
        if (
            value
            and len(value) >= 4
            and any(
                word in key.upper()
                for word in ("TOKEN", "KEY", "PASSWORD", "SECRET", "PROXY", "INDEX", "REGISTRY")
            )
        ):
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(https?://)[^\s/@]+(?::[^\s/@]*)?@", r"\1[REDACTED]@", text)
    text = re.sub(
        r"(?i)([?&](?:token|key|api_key|password|signature|credential)=)[^\s&]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer|basic)\s+\S+", r"\1[REDACTED]", text)
    return text


def classify_failure(output, *, timed_out=False):
    text = output.lower()
    if any(
        term in text
        for term in (
            "certificate_verify_failed",
            "certificate verify failed",
            "unable to get local issuer",
            "certificate signed by unknown authority",
            "self signed certificate",
            "sslcertverificationerror",
            "ssl certificate problem",
            "cert_has_expired",
            "certificate has expired",
            "unable_to_verify_leaf_signature",
            "self_signed_cert_in_chain",
        )
    ):
        return (
            "证书校验失败",
            False,
            "修复 Python/系统 CA 信任库；pip 可使用 PIP_CERT 指定可信 CA 文件。",
        )
    if any(
        term in text
        for term in (
            "407",
            "proxy authentication",
            "401 unauthorized",
            "403 forbidden",
            "e401",
            "e403",
        )
    ):
        return "代理或下载源认证失败", False, "检查代理或包源的访问凭据与权限。"
    if any(
        term in text
        for term in (
            "proxyerror",
            "could not resolve proxy",
            "cannot connect to proxy",
            "unable to connect to proxy",
        )
    ):
        return "代理连接失败", True, "检查 AGENT_INSTALL_PROXY 或现有 HTTP(S)_PROXY 设置。"
    if any(
        term in text
        for term in (
            "name or service not known",
            "temporary failure in name resolution",
            "could not resolve host",
            "no such host",
            "getaddrinfo",
            "eai_again",
            "enotfound",
        )
    ):
        return "DNS 解析失败", True, "检查网络与 DNS，或设置可访问的代理/下载源。"
    if timed_out or any(term in text for term in ("timed out", "timeout", "time out", "etimedout")):
        return (
            "下载或安装步骤超时",
            True,
            "检查网络、代理；构建较慢时可增大 AGENT_INSTALL_TIMEOUT（秒）。",
        )
    if any(
        term in text
        for term in (
            "connection reset",
            "connection refused",
            "connection aborted",
            "network is unreachable",
            "econnreset",
            "econnrefused",
            "unexpected eof",
            "tls handshake",
            "429",
            "502",
            "503",
            "504",
        )
    ):
        return "临时网络错误", True, "检查网络或下载源，稍后可重试。"
    if any(
        term in text
        for term in (
            "no matching distribution",
            "could not find a version",
            "no matching version",
            "etarget",
            "unknown revision",
            "404 not found",
        )
    ):
        return "包版本或下载源不可用", False, "确认包版本存在、Python/平台兼容以及下载源同步完整。"
    return "安装命令失败", False, "请查看日志中的构建或依赖错误；修复后重试相同步骤。"


def execute(command, *, check, capture_output, text, timeout, env, cwd):
    """Stop the entire installer process group before retry or environment rollback."""
    with start_process(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        env=env,
        cwd=cwd,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except BaseException:
            try:
                kill_process_group(process)
            except ProcessLookupError:
                pass
            process.communicate()
            raise
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result


def run_download(command, *, label, env=None, cwd=None):
    retries, timeout = network_options()
    environment = download_environment(env)
    directory = app_directory("state") / "install-logs"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix="download-", suffix=".log", dir=directory)
    with os.fdopen(fd, "w", encoding="utf-8") as log:
        print(f"{label}；日志：{name}", flush=True)
        for attempt in range(retries + 1):
            timed_out = False
            try:
                result = execute(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    env=environment,
                    cwd=cwd,
                )
                output = (getattr(result, "stdout", "") or "") + (
                    getattr(result, "stderr", "") or ""
                )
                log.write(f"尝试 {attempt + 1}：成功\n" + redact(output, environment) + "\n")
                log.flush()
                print(f"{label}完成。", flush=True)
                return result
            except subprocess.TimeoutExpired as error:
                timed_out = True
                output = redact(error.stdout, environment) + redact(error.stderr, environment)
            except subprocess.CalledProcessError as error:
                output = redact(error.stdout, environment) + redact(error.stderr, environment)
            except KeyboardInterrupt:
                log.write("用户取消。\n")
                raise
            except OSError:
                output = "安装程序无法启动；请检查可执行文件与目录权限。"
            category, retryable, hint = classify_failure(output, timed_out=timed_out)
            log.write(f"尝试 {attempt + 1}：{category}\n{output}\n")
            log.flush()
            if retryable and attempt < retries:
                delay = 2 ** (attempt + 1)
                print(
                    f"{label}：{category}，{delay} 秒后重试（{attempt + 1}/{retries}）。",
                    flush=True,
                )
                time.sleep(delay)
            else:
                raise DownloadError(f"{label}：{category}。{hint} 日志：{name}") from None
