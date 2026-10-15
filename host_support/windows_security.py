"""ACLs for newly owned private trees, never temporary grants on user projects."""

import ctypes as C
import os
import stat
from contextlib import contextmanager

from . import windows_files
from .cancellation import checkpoint
from .filesystem import walk_descriptors


class PrivateWindowsSecurity:
    def __init__(self, api):
        self.api = api
        self.advapi = api.security
        signatures = [
            (api.kernel, "GetCurrentProcess", C.c_void_p, []),
            (
                self.advapi,
                "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                C.c_int32,
                [C.c_wchar_p, C.c_uint32, C.POINTER(C.c_void_p), C.c_void_p],
            ),
            (
                self.advapi,
                "GetSecurityDescriptorDacl",
                C.c_int32,
                [C.c_void_p, C.POINTER(C.c_int32), C.POINTER(C.c_void_p), C.POINTER(C.c_int32)],
            ),
            (
                self.advapi,
                "SetSecurityInfo",
                C.c_uint32,
                [
                    C.c_void_p,
                    C.c_uint32,
                    C.c_uint32,
                    C.c_void_p,
                    C.c_void_p,
                    C.c_void_p,
                    C.c_void_p,
                ],
            ),
        ]
        for library, name, result, arguments in signatures:
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments
        token = C.c_void_p()
        api.check(self.advapi.OpenProcessToken(api.kernel.GetCurrentProcess(), 8, C.byref(token)))
        try:
            size = C.c_uint32()
            self.advapi.GetTokenInformation(token, 1, None, 0, C.byref(size))
            if not size.value:
                raise OSError("Cannot determine the current user's SID")
            data = C.create_string_buffer(size.value)
            api.check(self.advapi.GetTokenInformation(token, 1, data, len(data), C.byref(size)))
            self.user_sid = self.sid_string(C.cast(data, C.POINTER(C.c_void_p))[0])
        finally:
            api.close_handle(token.value)

    def sid_string(self, sid):
        result = C.c_void_p()
        self.api.check(self.advapi.ConvertSidToStringSidW(sid, C.byref(result)))
        try:
            return C.wstring_at(result)
        finally:
            self.api.kernel.LocalFree(result)

    def sddl(self, sid=None, *, writable=False):
        # Owner rights suppress implicit WRITE_DAC in a restricted owner's token.
        # The ordinary host user retains explicit full control for cleanup.
        grants = f"(A;OICI;FA;;;SY)(A;OICI;FA;;;{self.user_sid})(A;OICI;RC;;;OW)"
        if sid is None:
            return "D:P" + grants
        deny = 0xC0000 if writable else 0xD0156
        allow = 0x1301BF if writable else 0x1200A9
        return f"D:P(D;OICI;0x{deny:x};;;{sid})" + grants + f"(A;OICI;0x{allow:x};;;{sid})"

    @contextmanager
    def _dacl(self, sid, *, writable):
        """Keep one parsed descriptor alive for an entire private-tree traversal."""
        descriptor, dacl = C.c_void_p(), C.c_void_p()
        present, defaulted = C.c_int32(), C.c_int32()
        self.api.check(
            self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                self.sddl(sid, writable=writable), 1, C.byref(descriptor), None
            )
        )
        try:
            self.api.check(
                self.advapi.GetSecurityDescriptorDacl(
                    descriptor, C.byref(present), C.byref(dacl), C.byref(defaulted)
                )
            )
            if not present.value or not dacl.value:
                raise OSError("Private ACL must not be absent or NULL")
            yield dacl
        finally:
            self.api.kernel.LocalFree(descriptor)

    def _set_acl(self, path, dacl, *, directory=True, dir_fd=None):
        with windows_files._parent(path, dir_fd) as (parent, name):
            if name is None:
                raise ValueError("Never change a volume-root ACL")
            files = windows_files._api()
            fd = files.open(name, parent, 0x40000 | 0x20000, directory=directory)
            try:
                result = self.advapi.SetSecurityInfo(
                    files.handle(fd), 1, 0x80000004, None, None, dacl, None
                )
                if result:
                    raise C.WinError(result)
            finally:
                os.close(fd)

    def set_acl(self, path, sid=None, *, writable=False, directory=True):
        """Replace the ACL only on caller-owned staging/state objects, by handle."""
        with self._dacl(sid, writable=writable) as dacl:
            self._set_acl(path, dacl, directory=directory)

    def seal_tree(self, path, sid, *, writable=False):
        """Seal before any untrusted process is launched; reject all link types."""
        with self._dacl(sid, writable=writable) as dacl:
            checkpoint()
            self._set_acl(path, dacl)
            for _, dirs, names, parent in walk_descriptors(path):
                for name in (*dirs, *names):
                    checkpoint()
                    info = windows_files.stat_at(name, dir_fd=parent)
                    is_directory = stat.S_ISDIR(info.st_mode)
                    if not is_directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                        raise ValueError("Private runtime contains a link or special file")
                    # The walker already pinned this parent without following links.
                    # Avoid reopening every ancestor for each child in large runtimes.
                    self._set_acl(name, dacl, directory=is_directory, dir_fd=parent)


def clear_private_directory(path):
    """Unlink private contents by pinned handles, including junctions themselves.

    Caller must first confirm the Job is empty. This never follows a reparse
    point and intentionally supports multiply linked files for safe unlinking.
    """
    from .filesystem import open_directory

    api = windows_files._api()

    def unlink(parent, name):
        fd = api.open(name, parent, 0x10000, metadata=True)
        try:
            disposition, ios = C.c_uint32(1 | 2 | 16), windows_files._IOStatus()
            api.check(
                api.nt.NtSetInformationFile(
                    api.handle(fd), C.byref(ios), C.byref(disposition), C.sizeof(disposition), 64
                )
            )
        finally:
            os.close(fd)

    def clear(fd):
        for name in windows_files.list_directory(fd):
            info = windows_files.stat_at(name, dir_fd=fd)
            if stat.S_ISDIR(info.st_mode):
                child = windows_files.open_directory(name, dir_fd=fd)
                try:
                    clear(child)
                finally:
                    os.close(child)
            unlink(fd, name)

    fd = open_directory(path)
    try:
        clear(fd)
    finally:
        os.close(fd)
