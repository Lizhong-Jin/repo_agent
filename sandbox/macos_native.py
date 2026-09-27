"""macOS Seatbelt policy and enforcement; common lifecycle lives separately."""

import json
import sys
from pathlib import Path

from host_support.languages import sandbox_environment
from tools._internal.file_policy import PROTECTED_NAMES, PROTECTED_SUFFIXES

from .native_common import NativeBackendBase


def _quoted(value):
    text = str(value)
    if any(ord(char) < 32 for char in text):
        raise ValueError("Native 沙箱路径不能包含控制字符")
    return json.dumps(text, ensure_ascii=False)


def _pattern(text):
    # Seatbelt uses POSIX regexes, not Python's (?i) or non-capturing groups.
    return "".join(
        f"[{char.lower()}{char.upper()}]"
        if char.isalpha()
        else f"[{char}]"
        if char in ".-"
        else char
        for char in text
    )


def seatbelt_profile(workspace, scratch, read_paths, protected_paths, *, git_read=False):
    """Host-generated policy. Later deny rules also cover newly created names."""
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec process-fork)",
        "(allow signal (target same-sandbox))",
        # Never expose kern.procargs*: the host process environment contains keys.
        '(allow sysctl-read (sysctl-name-prefix "hw.") (sysctl-name-prefix "machdep.cpu.") '
        '(sysctl-name "kern.ostype" "kern.osrelease" "kern.osversion" "kern.osproductversion" '
        '"kern.argmax" "kern.maxfiles" "kern.maxfilesperproc" "kern.maxproc" '
        '"kern.hostname" "kern.boottime" "kern.usrstack64" "kern.version"))',
        "(allow file-read-metadata)",
        # dyld/libignition opens / as an openat root during process startup.
        '(allow file-read* (literal "/"))',
        '(allow file-read* file-write-data (literal "/dev/null"))',
        '(allow file-read* (literal "/dev/urandom") (literal "/dev/random"))',
    ]
    for path in sorted(set(map(str, [workspace, scratch, *read_paths]))):
        lines.append(f"(allow file-read* file-map-executable (subpath {_quoted(path)}))")
    for path in (workspace, scratch):
        lines.append(f"(allow file-write* (subpath {_quoted(path)}))")
        # Do not rename/remove the allowed root itself.
        lines.append(f"(deny file-write-unlink (literal {_quoted(path)}))")
    # Protect the interpreter/dependencies even when installed inside the workspace.
    for path in read_paths:
        lines.append(f"(deny file-write* (subpath {_quoted(path)}))")
    names = "|".join(_pattern(name) for name in sorted(PROTECTED_NAMES - {".git"}))
    suffixes = "|".join(_pattern(suffix) for suffix in PROTECTED_SUFFIXES)
    pattern = f"(^|/)({names}|{_pattern('.env')}([.][^/]*)?|[^/]*({suffixes}))(/|$)"
    lines.append(f'(deny file-read* file-write* (regex #"{pattern}"))')
    git_ops = "file-write*" if git_read else "file-read* file-write*"
    lines.append(f'(deny {git_ops} (regex #"(^|/){_pattern(".git")}(/|$)"))')
    for path in protected_paths:
        lines.append(f"(deny file-read* file-write* (subpath {_quoted(path)}))")
    for path in (*protected_paths, *read_paths):
        # Ancestor renames must not relocate a protected subtree into an allowed path.
        for parent in Path(path).parents:
            if parent == workspace or not parent.is_relative_to(workspace):
                break
            lines.append(f"(deny file-write-unlink (literal {_quoted(parent)}))")
    # Deny hard-link creation even from otherwise readable toolchain paths.
    lines.extend(["(deny file-link)", "(deny network*)"])
    return "\n".join(lines) + "\n"


class MacOSNativeBackend(NativeBackendBase):
    platform_name = "macos"
    isolation = "seatbelt"
    temporary_root = "/private/tmp"

    def _platform_setup(self):
        if sys.platform != "darwin":
            raise ValueError("native 沙箱仅支持 macOS/Linux；不会退回未隔离执行")
        self.executable = Path("/usr/bin/sandbox-exec")
        if not self.executable.is_file():
            raise ValueError("未找到 macOS sandbox-exec；不会退回未隔离执行")

    def _read_paths(self):
        paths = {
            self.runtime,
            Path(sys.prefix).resolve(),
            Path(sys.base_prefix).resolve(),
            # Never allow /System wholesale: /System/Volumes/Data aliases user data.
            Path("/System/Library"),
            Path("/System/Cryptexes"),
            Path("/System/Volumes/Preboot/Cryptexes"),
            Path("/usr"),
            Path("/bin"),
            Path("/sbin"),
            Path("/Library/Apple"),
            Path("/Library/Developer"),
            Path("/Library/Frameworks"),
            Path("/opt/homebrew"),
            Path("/private/etc"),
            Path("/private/var/db/dyld"),
            Path("/private/var/db/timezone"),
        }
        # Include active dependency directories, but never sys.path's project entry.
        paths.update(
            Path(path).resolve()
            for path in sys.path
            if path and Path(path).name in {"site-packages", "dist-packages"}
        )
        return tuple(sorted(paths, key=str))

    def _environment(self, scratch):
        return sandbox_environment(self.python, scratch, platform="darwin")

    def _sandbox_command(self, command, control, scratch, read_paths, *, git_read=False):
        profile = control / "policy.sb"
        profile.write_text(
            seatbelt_profile(
                self.workspace,
                scratch,
                read_paths,
                self.protected_paths,
                git_read=git_read,
            )
        )
        return [str(self.executable), "-f", str(profile), *command]

    def _preflight(self):
        # Verify actual kernel enforcement, rather than trusting executable presence.
        denied = self.directory / "denied.txt"
        denied.write_text("native sandbox probe")
        code = (
            "import errno, os, pathlib, socket, subprocess, sys, tempfile\n"
            "def blocked(action):\n"
            " try: action()\n"
            " except OSError as e:\n"
            "  assert e.errno in (errno.EPERM, errno.EACCES), repr(e)\n"
            " else: raise AssertionError('sandbox restriction missing')\n"
            f"blocked(lambda: pathlib.Path({str(denied)!r}).read_text())\n"
            f"blocked(lambda: pathlib.Path({str(denied)!r}).write_text('changed'))\n"
            "blocked(lambda: socket.socket().connect(('127.0.0.1', 9)))\n"
            "p = pathlib.Path(tempfile.gettempdir()) / 'probe'\n"
            "p.write_text('ok'); assert p.read_text() == 'ok'; p.unlink()\n"
            "blocked(lambda: (p.parent / '.ENV').write_text('blocked'))\n"
            f"with tempfile.NamedTemporaryFile(dir={str(self.workspace)!r}, "
            "prefix='.native-probe-') as f:\n"
            " f.write(b'probe'); f.flush()\n"
            " blocked(lambda: os.link(f.name, str(p)))\n"
            "subprocess.run([sys.executable, '-I', '-c', "
            f'"import pathlib; pathlib.Path({str(denied)!r}).read_text()"], '
            "check=False, capture_output=True).returncode != 0 or sys.exit(1)\n"
            "print('native-ok')\n"
        )
        result = self._run([str(self.python), "-I", "-c", code], timeout=15)
        if result.exit_code != 0 or result.stdout.strip() != "native-ok" or not self.healthy:
            raise ValueError(
                "macOS 原生沙箱自检失败；不会退回未隔离执行。"
                "当前系统或外层沙箱可能不允许 sandbox-exec。"
                + (f"\n{result.stderr[:1500]}" if result.stderr else "")
            )
