"""Windows handle-relative file operations (stdlib only, loaded lazily).

Every path component is opened separately with FILE_OPEN_REPARSE_POINT. Children
are addressed relative to a directory handle, never by reconstructing its path.
This includes enumeration, deletion and rename, so junction swaps cannot redirect
an operation after validation. Normal file opens also reject hard links.
"""

import ctypes as C
import errno
import ntpath
import os
import stat
import struct
from contextlib import contextmanager
from functools import lru_cache


def validate_component(name):
    """Reject Win32 aliases, devices, streams and multi-component relative names."""
    if not isinstance(name, str) or not name or name in {".", ".."}:
        raise ValueError("Invalid Windows file name")
    stem = name.split(".")[0].rstrip(" ").upper()
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    reserved.update(f"{prefix}{digit}" for prefix in ("COM", "LPT") for digit in "123456789¹²³")
    if (
        any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name)
        or name.endswith((" ", "."))
        or stem in reserved
    ):
        raise ValueError(f"Unsafe Windows file name: {name!r}")
    return name


def validate_snapshot_names(names):
    """Validate POSIX snapshot keys before any Windows host mutation."""
    seen = {}
    for name in names:
        if not isinstance(name, str) or not name:
            raise ValueError("Invalid Windows snapshot path")
        parts = name.split("/")
        for index, part in enumerate(parts):
            validate_component(part)
            prefix = "/".join(parts[: index + 1])
            previous = seen.setdefault(prefix.casefold(), prefix)
            if previous != prefix:
                raise ValueError(f"Windows path collision: {previous!r}, {prefix!r}")


@lru_cache(maxsize=1)
def _api():
    if os.name != "nt":
        raise OSError(errno.ENOTSUP, "Windows file service requires Windows")
    return _WindowsAPI()


class _UnicodeString(C.Structure):
    _fields_ = [("Length", C.c_ushort), ("MaximumLength", C.c_ushort), ("Buffer", C.c_void_p)]


class _ObjectAttributes(C.Structure):
    _fields_ = [
        ("Length", C.c_uint32),
        ("RootDirectory", C.c_void_p),
        ("ObjectName", C.POINTER(_UnicodeString)),
        ("Attributes", C.c_uint32),
        ("SecurityDescriptor", C.c_void_p),
        ("SecurityQualityOfService", C.c_void_p),
    ]


class _IOStatus(C.Structure):
    _fields_ = [("Status", C.c_void_p), ("Information", C.c_size_t)]


class _BasicInfo(C.Structure):
    _fields_ = [
        ("CreationTime", C.c_longlong),
        ("LastAccessTime", C.c_longlong),
        ("LastWriteTime", C.c_longlong),
        ("ChangeTime", C.c_longlong),
        ("FileAttributes", C.c_uint32),
    ]


class _RenameInfo(C.Structure):
    _fields_ = [
        ("Flags", C.c_uint32),
        ("RootDirectory", C.c_void_p),
        ("FileNameLength", C.c_uint32),
        ("FileName", C.c_ushort * 1),
    ]


class _Overlapped(C.Structure):
    _fields_ = [
        ("Internal", C.c_size_t),
        ("InternalHigh", C.c_size_t),
        ("Offset", C.c_uint32),
        ("OffsetHigh", C.c_uint32),
        ("hEvent", C.c_void_p),
    ]


class _WindowsAPI:
    def __init__(self):
        import msvcrt

        self.crt = msvcrt
        self.nt = C.WinDLL("ntdll", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self._bind(
            self.nt,
            "NtCreateFile",
            C.c_int32,
            [
                C.POINTER(C.c_void_p),
                C.c_uint32,
                C.POINTER(_ObjectAttributes),
                C.POINTER(_IOStatus),
                C.c_void_p,
                C.c_uint32,
                C.c_uint32,
                C.c_uint32,
                C.c_uint32,
                C.c_void_p,
                C.c_uint32,
            ],
        )
        self._bind(
            self.nt,
            "NtQueryDirectoryFile",
            C.c_int32,
            [
                C.c_void_p,
                C.c_void_p,
                C.c_void_p,
                C.c_void_p,
                C.POINTER(_IOStatus),
                C.c_void_p,
                C.c_uint32,
                C.c_int,
                C.c_ubyte,
                C.c_void_p,
                C.c_ubyte,
            ],
        )
        self._bind(
            self.nt,
            "NtSetInformationFile",
            C.c_int32,
            [
                C.c_void_p,
                C.POINTER(_IOStatus),
                C.c_void_p,
                C.c_uint32,
                C.c_int,
            ],
        )
        self._bind(self.nt, "RtlNtStatusToDosError", C.c_uint32, [C.c_int32])
        self._bind(
            self.kernel,
            "GetFileInformationByHandleEx",
            C.c_int,
            [C.c_void_p, C.c_int, C.c_void_p, C.c_uint32],
        )
        self._bind(
            self.kernel,
            "SetFileInformationByHandle",
            C.c_int,
            [C.c_void_p, C.c_int, C.c_void_p, C.c_uint32],
        )
        self._bind(
            self.kernel,
            "GetFinalPathNameByHandleW",
            C.c_uint32,
            [C.c_void_p, C.c_void_p, C.c_uint32, C.c_uint32],
        )
        self._bind(self.kernel, "CloseHandle", C.c_int, [C.c_void_p])
        self._bind(
            self.kernel,
            "LockFileEx",
            C.c_int,
            [
                C.c_void_p,
                C.c_uint32,
                C.c_uint32,
                C.c_uint32,
                C.c_uint32,
                C.POINTER(_Overlapped),
            ],
        )

    @staticmethod
    def _bind(library, name, result, arguments):
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments

    def check(self, status):
        if status < 0:
            raise C.WinError(self.nt.RtlNtStatusToDosError(status))

    def handle(self, fd):
        return self.crt.get_osfhandle(fd)

    def attributes(self, fd):
        info = _BasicInfo()
        if not self.kernel.GetFileInformationByHandleEx(
            self.handle(fd), 0, C.byref(info), C.sizeof(info)
        ):
            raise C.WinError(C.get_last_error())
        return info

    def check_name(self, fd, expected):
        buffer = C.create_unicode_buffer(32768)
        size = self.kernel.GetFinalPathNameByHandleW(
            self.handle(fd),
            buffer,
            len(buffer),
            4,  # NORMALIZED | VOLUME_NAME_NONE
        )
        if not size:
            raise C.WinError(C.get_last_error())
        if size >= len(buffer):
            raise ValueError("Windows path is too long")
        actual = ntpath.basename(buffer.value)
        if actual.casefold() != expected.casefold():
            raise PermissionError("Windows short-name aliases are not allowed")

    def open(
        self, name, parent, access, disposition=1, *, directory=False, metadata=False, crt_flags=0
    ):
        encoded = name.encode("utf-16-le")
        if len(encoded) > 65532:
            raise ValueError("Windows path is too long")
        buffer = C.create_string_buffer(encoded + b"\0\0")
        string = _UnicodeString(len(encoded), len(encoded) + 2, C.addressof(buffer))
        attributes = _ObjectAttributes(
            C.sizeof(_ObjectAttributes),
            self.handle(parent) if parent is not None else None,
            C.pointer(string),
            0x40,
            None,
            None,  # OBJ_CASE_INSENSITIVE
        )
        handle, ios = C.c_void_p(), _IOStatus()
        # Synchronous, non-inheritable handles. OPEN_REPARSE_POINT applies even
        # to metadata queries; each relative name is exactly one component.
        options = 0x00200000 | 0x20
        if not metadata:
            options |= 0x1 if directory else 0x40
        if directory:
            access |= 0x20  # FILE_TRAVERSE
        self.check(
            self.nt.NtCreateFile(
                C.byref(handle),
                access | 0x100000 | 0x80,
                C.byref(attributes),
                C.byref(ios),
                None,
                0x80,
                0x7,
                disposition,
                options,
                None,
                0,
            )
        )
        try:
            fd = self.crt.open_osfhandle(handle.value, os.O_BINARY | os.O_NOINHERIT | crt_flags)
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise
        try:
            reparse = self.attributes(fd).FileAttributes & 0x400
            if not metadata and reparse:
                raise PermissionError("Reparse points are not allowed")
            if parent is not None and not reparse:
                self.check_name(fd, name)
            if not directory and not metadata:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise PermissionError("Expected an independent regular file")
            return fd
        except BaseException:
            os.close(fd)
            raise


@contextmanager
def _parent(path, dir_fd):
    if dir_fd is not None:
        yield dir_fd, validate_component(os.fspath(path))
        return
    path = os.fsdecode(path).replace("/", "\\")
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    if path.startswith(("\\\\.\\", "\\??\\")):
        raise ValueError("Windows device paths are not allowed")
    # Do not normalize away '..': it could conceal an intervening reparse point.
    drive, tail = ntpath.splitdrive(path)
    if drive and not tail.startswith("\\"):
        raise ValueError("Drive-relative paths are not allowed")
    if not drive:
        path = ntpath.join(os.getcwd(), path)
        drive, tail = ntpath.splitdrive(path)
    if not tail.startswith("\\") or not (
        (len(drive) == 2 and drive[0].isalpha() and drive[1] == ":")
        or (drive.startswith("\\\\") and len(drive[2:].split("\\")) == 2)
    ):
        raise ValueError("Expected an absolute drive or UNC path")
    parts = [validate_component(p) for p in tail.split("\\") if p not in {"", "."}]
    root = "\\??\\" + ("UNC\\" + drive[2:] if drive.startswith("\\\\") else drive) + "\\"
    api = _api()
    fd = api.open(root, None, 1, directory=True)
    try:
        for part in parts[:-1]:
            child = api.open(part, fd, 1, directory=True)
            os.close(fd)
            fd = child
        yield fd, parts[-1] if parts else None
    finally:
        os.close(fd)


def open_directory(path, *, dir_fd=None):
    with _parent(path, dir_fd) as (parent, name):
        return os.dup(parent) if name is None else _api().open(name, parent, 1, directory=True)


def open_file(path, flags=os.O_RDONLY, mode=0o600, *, dir_fd=None, nonblocking=True):
    access = 1 if not flags & os.O_WRONLY else 0
    if flags & (os.O_WRONLY | os.O_RDWR):
        access |= 2 | 0x100  # WRITE_DATA and WRITE_ATTRIBUTES
    disposition = (2 if flags & os.O_EXCL else 3) if flags & os.O_CREAT else 1
    with _parent(path, dir_fd) as (parent, name):
        if name is None:
            raise IsADirectoryError(str(path))
        fd = _api().open(
            name,
            parent,
            access,
            disposition,
            crt_flags=flags & (os.O_APPEND | os.O_WRONLY | os.O_RDWR),
        )
    try:
        # Validate the opened object before any truncation (including hard links).
        if flags & os.O_TRUNC:
            os.ftruncate(fd, 0)
        return fd
    except BaseException:
        os.close(fd)
        raise


def stat_at(name, *, dir_fd):
    fd = _api().open(validate_component(os.fspath(name)), dir_fd, 0, metadata=True)
    try:
        info = os.fstat(fd)
        if _api().attributes(fd).FileAttributes & 0x400:
            # Classify every reparse tag as a link, including junctions/cloud tags.
            values = list(info)
            values[0] = stat.S_IFLNK | stat.S_IMODE(info.st_mode)
            return os.stat_result(values)
        return info
    finally:
        os.close(fd)


def list_directory(fd):
    api, buffer, ios = _api(), C.create_string_buffer(65536), _IOStatus()
    result, restart = [], True
    while True:
        status = api.nt.NtQueryDirectoryFile(
            api.handle(fd),
            None,
            None,
            None,
            C.byref(ios),
            buffer,
            len(buffer),
            12,
            False,
            None,
            restart,  # FileNamesInformation
        )
        if status & 0xFFFFFFFF == 0x80000006:  # STATUS_NO_MORE_FILES
            return result
        api.check(status)
        restart = False
        offset = 0
        while True:
            following, _, size = struct.unpack_from("<III", buffer, offset)
            name = buffer.raw[offset + 12 : offset + 12 + size].decode("utf-16-le")
            if name not in {".", ".."}:
                result.append(name)
            if not following:
                break
            offset += following


def mkdir_at(name, mode=0o777, *, dir_fd):
    fd = _api().open(validate_component(os.fspath(name)), dir_fd, 1, 2, directory=True)
    os.close(fd)


def unlink_at(name, *, dir_fd):
    api = _api()
    fd = api.open(validate_component(os.fspath(name)), dir_fd, 0x10000)
    try:
        # Windows 10+ supports deletion of read-only staging files without
        # changing their attributes or weakening their ACLs.
        disposition, ios = C.c_uint32(1 | 2 | 16), _IOStatus()
        api.check(
            api.nt.NtSetInformationFile(
                api.handle(fd),
                C.byref(ios),
                C.byref(disposition),
                C.sizeof(disposition),
                64,
            )
        )
    finally:
        os.close(fd)


def rename_at(source, destination, *, src_dir_fd, dst_dir_fd, replace=False):
    api = _api()
    source = validate_component(os.fspath(source))
    encoded = validate_component(os.fspath(destination)).encode("utf-16-le")
    # NtSetInformationFile supports a RootDirectory handle, unlike the Win32
    # wrapper's documented full-path-only rename contract.
    size = C.sizeof(_RenameInfo) + len(encoded)
    buffer = C.create_string_buffer(size)
    info = _RenameInfo.from_buffer(buffer)
    info.Flags, info.RootDirectory = (1 | 2 | 64) if replace else 0, api.handle(dst_dir_fd)
    info.FileNameLength = len(encoded)
    C.memmove(C.addressof(buffer) + _RenameInfo.FileName.offset, encoded, len(encoded))
    fd = api.open(source, src_dir_fd, 0x10000)
    try:
        try:
            target = stat_at(destination, dir_fd=dst_dir_fd)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(target.st_mode) or target.st_nlink != 1:
                raise PermissionError("Expected an independent regular destination")
        ios = _IOStatus()
        api.check(api.nt.NtSetInformationFile(api.handle(fd), C.byref(ios), buffer, size, 65))
    finally:
        os.close(fd)


def set_mode(fd, mode):
    """Windows preserves only the read-only bit; ACLs are inherited, not chmod'ed."""
    if stat.S_ISDIR(os.fstat(fd).st_mode):
        return  # Windows directory writability is controlled by the inherited ACL.
    api = _api()
    attributes = api.attributes(fd).FileAttributes & ~0x80  # NORMAL is valid only on its own.
    attributes = attributes & ~1 if mode & 0o222 else attributes | 1
    info = _BasicInfo(FileAttributes=attributes or 0x80)
    if not api.kernel.SetFileInformationByHandle(api.handle(fd), 0, C.byref(info), C.sizeof(info)):
        raise C.WinError(C.get_last_error())


def lock_descriptor(fd, *, blocking=True):
    api, overlapped = _api(), _Overlapped()
    if not api.kernel.LockFileEx(
        api.handle(fd), 2 | (0 if blocking else 1), 0, 1, 0, C.byref(overlapped)
    ):
        error = C.get_last_error()
        if error == 33:
            raise BlockingIOError(errno.EAGAIN, "File is already locked")
        raise C.WinError(error)
