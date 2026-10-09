"""Windows LPAC/Job primitives. No workspace policy and no unisolated fallback.

Imported safely on other hosts; DLLs are loaded only when constructing the API.
All Win32 integers have fixed widths, including when inspecting layouts on POSIX.
"""

import ctypes as C
import os
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

DWORD = C.c_uint32
BOOL = C.c_int32
HANDLE = C.c_void_p
SIZE_T = C.c_size_t
LPWSTR = C.c_wchar_p

CREATE_SUSPENDED = 0x4
CREATE_UNICODE_ENVIRONMENT = 0x400
EXTENDED_STARTUPINFO_PRESENT = 0x80000
CREATE_NO_WINDOW = 0x8000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x400
HANDLE_LIST = 0x20002
MITIGATION_POLICY = 0x20007
SECURITY_CAPABILITIES = 0x20009
JOB_LIST = 0x2000D
ALL_APPLICATION_PACKAGES_POLICY = 0x2000F


class SecurityAttributes(C.Structure):
    _fields_ = [("length", DWORD), ("descriptor", HANDLE), ("inherit", BOOL)]


class StartupInfo(C.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("reserved", LPWSTR),
        ("desktop", LPWSTR),
        ("title", LPWSTR),
        ("x", DWORD),
        ("y", DWORD),
        ("xsize", DWORD),
        ("ysize", DWORD),
        ("xchars", DWORD),
        ("ychars", DWORD),
        ("fill", DWORD),
        ("flags", DWORD),
        ("show", C.c_uint16),
        ("reserved_size", C.c_uint16),
        ("reserved_bytes", HANDLE),
        ("stdin", HANDLE),
        ("stdout", HANDLE),
        ("stderr", HANDLE),
    ]


class StartupInfoEx(C.Structure):
    _fields_ = [("startup", StartupInfo), ("attributes", HANDLE)]


class ProcessInformation(C.Structure):
    _fields_ = [("process", HANDLE), ("thread", HANDLE), ("pid", DWORD), ("tid", DWORD)]


class SecurityCapabilities(C.Structure):
    _fields_ = [("sid", HANDLE), ("capabilities", HANDLE), ("count", DWORD), ("reserved", DWORD)]


class JobBasicLimits(C.Structure):
    _fields_ = [
        ("process_time", C.c_int64),
        ("job_time", C.c_int64),
        ("flags", DWORD),
        ("min_working_set", SIZE_T),
        ("max_working_set", SIZE_T),
        ("active_limit", DWORD),
        ("affinity", SIZE_T),
        ("priority", DWORD),
        ("scheduling", DWORD),
    ]


class IOCounters(C.Structure):
    _fields_ = [
        (name, C.c_uint64)
        for name in (
            "read_ops",
            "write_ops",
            "other_ops",
            "read_bytes",
            "write_bytes",
            "other_bytes",
        )
    ]


class JobExtendedLimits(C.Structure):
    _fields_ = [
        ("basic", JobBasicLimits),
        ("io", IOCounters),
        ("process_memory", SIZE_T),
        ("job_memory", SIZE_T),
        ("peak_process_memory", SIZE_T),
        ("peak_job_memory", SIZE_T),
    ]


class JobAccounting(C.Structure):
    _fields_ = [
        ("user_time", C.c_int64),
        ("kernel_time", C.c_int64),
        ("period_user", C.c_int64),
        ("period_kernel", C.c_int64),
        ("page_faults", DWORD),
        ("total", DWORD),
        ("active", DWORD),
        ("terminated", DWORD),
    ]


@dataclass
class AppContainerProfile:
    name: str
    sid: int
    directory: Path | None = None


class WindowsIsolationAPI:
    def __init__(self):
        if os.name != "nt" or C.sizeof(HANDLE) != 8:
            raise OSError("Windows isolation requires 64-bit Windows")
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.security = C.WinDLL("advapi32", use_last_error=True)
        self.userenv = C.WinDLL("userenv", use_last_error=True)
        self.ole = C.WinDLL("ole32", use_last_error=True)
        ptr = C.POINTER
        signatures = [
            (self.kernel, "CloseHandle", BOOL, [HANDLE]),
            (self.kernel, "LocalFree", HANDLE, [HANDLE]),
            (self.kernel, "CreateJobObjectW", HANDLE, [HANDLE, LPWSTR]),
            (self.kernel, "OpenJobObjectW", HANDLE, [DWORD, BOOL, LPWSTR]),
            (self.kernel, "SetInformationJobObject", BOOL, [HANDLE, DWORD, HANDLE, DWORD]),
            (
                self.kernel,
                "QueryInformationJobObject",
                BOOL,
                [HANDLE, DWORD, HANDLE, DWORD, ptr(DWORD)],
            ),
            (self.kernel, "TerminateJobObject", BOOL, [HANDLE, DWORD]),
            (self.kernel, "IsProcessInJob", BOOL, [HANDLE, HANDLE, ptr(BOOL)]),
            (
                self.kernel,
                "InitializeProcThreadAttributeList",
                BOOL,
                [HANDLE, DWORD, DWORD, ptr(SIZE_T)],
            ),
            (
                self.kernel,
                "UpdateProcThreadAttribute",
                BOOL,
                [HANDLE, DWORD, SIZE_T, HANDLE, SIZE_T, HANDLE, HANDLE],
            ),
            (self.kernel, "DeleteProcThreadAttributeList", None, [HANDLE]),
            (
                self.kernel,
                "CreateProcessW",
                BOOL,
                [
                    LPWSTR,
                    LPWSTR,
                    HANDLE,
                    HANDLE,
                    BOOL,
                    DWORD,
                    HANDLE,
                    LPWSTR,
                    ptr(StartupInfoEx),
                    ptr(ProcessInformation),
                ],
            ),
            (self.kernel, "ResumeThread", DWORD, [HANDLE]),
            (self.kernel, "WaitForSingleObject", DWORD, [HANDLE, DWORD]),
            (self.kernel, "GetExitCodeProcess", BOOL, [HANDLE, ptr(DWORD)]),
            (
                self.kernel,
                "CreatePipe",
                BOOL,
                [ptr(HANDLE), ptr(HANDLE), ptr(SecurityAttributes), DWORD],
            ),
            (self.kernel, "SetHandleInformation", BOOL, [HANDLE, DWORD, DWORD]),
            (
                self.kernel,
                "PeekNamedPipe",
                BOOL,
                [HANDLE, HANDLE, DWORD, HANDLE, ptr(DWORD), HANDLE],
            ),
            (self.kernel, "ReadFile", BOOL, [HANDLE, HANDLE, DWORD, ptr(DWORD), HANDLE]),
            (self.kernel, "GetWindowsDirectoryW", DWORD, [LPWSTR, DWORD]),
            (self.security, "OpenProcessToken", BOOL, [HANDLE, DWORD, ptr(HANDLE)]),
            (
                self.security,
                "GetTokenInformation",
                BOOL,
                [HANDLE, DWORD, HANDLE, DWORD, ptr(DWORD)],
            ),
            (self.security, "EqualSid", BOOL, [HANDLE, HANDLE]),
            (self.security, "FreeSid", HANDLE, [HANDLE]),
            (self.security, "ConvertSidToStringSidW", BOOL, [HANDLE, ptr(HANDLE)]),
            (
                self.userenv,
                "CreateAppContainerProfile",
                C.c_int32,
                [LPWSTR, LPWSTR, LPWSTR, HANDLE, DWORD, ptr(HANDLE)],
            ),
            (self.userenv, "GetAppContainerFolderPath", C.c_int32, [LPWSTR, ptr(HANDLE)]),
            (self.userenv, "DeleteAppContainerProfile", C.c_int32, [LPWSTR]),
            (
                self.userenv,
                "DeriveAppContainerSidFromAppContainerName",
                C.c_int32,
                [LPWSTR, ptr(HANDLE)],
            ),
            (self.ole, "CoTaskMemFree", None, [HANDLE]),
        ]
        for library, name, result, arguments in signatures:
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments

    @staticmethod
    def check(result):
        if not result:
            raise C.WinError(C.get_last_error())
        return result

    @staticmethod
    def hresult(result):
        if result < 0:
            raise OSError(f"AppContainer API failed (HRESULT 0x{result & 0xFFFFFFFF:08x})")

    def create_profile(self, name=None):
        name = name or "repo-agent-call-" + uuid.uuid4().hex
        sid = HANDLE()
        # Never reuse an existing profile, and never add network or broad capabilities.
        self.hresult(
            self.userenv.CreateAppContainerProfile(name, name, name, None, 0, C.byref(sid))
        )
        return AppContainerProfile(name, sid.value)

    def recover_profile(self, name):
        sid = HANDLE()
        self.hresult(self.userenv.DeriveAppContainerSidFromAppContainerName(name, C.byref(sid)))
        profile = AppContainerProfile(name, sid.value)
        try:
            profile.directory = self.profile_directory(profile)
            self.delete_profile(profile)
        finally:
            self.free_profile_sid(profile)

    def job_is_empty(self, name):
        handle = self.kernel.OpenJobObjectW(4, False, name)  # JOB_OBJECT_QUERY
        if not handle:
            if C.get_last_error() == 2:
                return True  # A Job is destroyed only after its members have terminated.
            raise C.WinError(C.get_last_error())
        try:
            return self.active_processes(handle) == 0
        finally:
            self.close_handle(handle)

    def profile_directory(self, profile):
        sid_string, directory = HANDLE(), HANDLE()
        try:
            self.check(self.security.ConvertSidToStringSidW(profile.sid, C.byref(sid_string)))
            self.hresult(
                self.userenv.GetAppContainerFolderPath(C.wstring_at(sid_string), C.byref(directory))
            )
            return Path(C.wstring_at(directory))
        finally:
            if directory.value:
                self.ole.CoTaskMemFree(directory)
            if sid_string.value:
                self.kernel.LocalFree(sid_string)

    def delete_profile(self, profile):
        if profile.directory is not None and profile.directory.exists():
            from .windows_security import clear_private_directory

            clear_private_directory(profile.directory)
        self.hresult(self.userenv.DeleteAppContainerProfile(profile.name))
        if profile.directory is not None and profile.directory.exists():
            raise OSError(f"AppContainer storage remains after deletion: {profile.name}")

    def free_profile_sid(self, profile):
        if profile.sid:
            self.security.FreeSid(profile.sid)
            profile.sid = 0

    def windows_directory(self):
        buffer = C.create_unicode_buffer(32768)
        length = self.check(self.kernel.GetWindowsDirectoryW(buffer, len(buffer)))
        if length >= len(buffer):
            raise OSError("Windows directory exceeds buffer")
        return buffer.value

    def create_job(self, own, name=None):
        job = own(self.check(self.kernel.CreateJobObjectW(None, name)))
        if name and C.get_last_error() == 183:
            raise OSError("Refusing to reuse an existing Windows isolation Job")
        limits = JobExtendedLimits()
        limits.basic.flags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )
        # Neither BREAKAWAY_OK nor SILENT_BREAKAWAY_OK is allowed.
        self.check(self.kernel.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)))
        # Job UI limits are incompatible with nested Jobs (common in CI/hosts).
        # Win32k lockdown is instead mandatory on every process creation below.
        return job

    def create_pipe(self, own, *, child_reads=False):
        read, write = HANDLE(), HANDLE()
        attributes = SecurityAttributes(C.sizeof(SecurityAttributes), None, True)
        self.check(self.kernel.CreatePipe(C.byref(read), C.byref(write), C.byref(attributes), 0))
        own(read.value)
        own(write.value)
        parent = write.value if child_reads else read.value
        self.check(self.kernel.SetHandleInformation(parent, 1, 0))
        return read.value, write.value

    def create_suspended(self, profile, job, stdio, command, cwd, environment, own):
        capabilities = SecurityCapabilities(profile.sid, None, 0, 0)
        handles = (HANDLE * 3)(*stdio)
        jobs = (HANDLE * 1)(job)
        lpac = DWORD(1)
        mitigations = C.c_uint64((1 << 28) | (1 << 32))  # win32k and extension points disabled
        attributes = [
            (SECURITY_CAPABILITIES, capabilities),
            (HANDLE_LIST, handles),
            (JOB_LIST, jobs),
            (ALL_APPLICATION_PACKAGES_POLICY, lpac),
            (MITIGATION_POLICY, mitigations),
        ]
        size = SIZE_T()
        self.kernel.InitializeProcThreadAttributeList(None, len(attributes), 0, C.byref(size))
        if C.get_last_error() != 122 or not size.value:
            raise C.WinError(C.get_last_error())
        buffer = C.create_string_buffer(size.value)
        self.check(
            self.kernel.InitializeProcThreadAttributeList(buffer, len(attributes), 0, C.byref(size))
        )
        try:
            for key, value in attributes:
                self.check(
                    self.kernel.UpdateProcThreadAttribute(
                        buffer, 0, key, C.byref(value), C.sizeof(value), None, None
                    )
                )
            startup = StartupInfoEx()
            startup.startup.cb = C.sizeof(startup)
            startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
            startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = stdio
            startup.attributes = C.cast(buffer, HANDLE)
            information = ProcessInformation()
            line = C.create_unicode_buffer(subprocess.list2cmdline(command))
            env = C.create_unicode_buffer(environment)
            flags = (
                CREATE_SUSPENDED
                | CREATE_UNICODE_ENVIRONMENT
                | EXTENDED_STARTUPINFO_PRESENT
                | CREATE_NO_WINDOW
            )
            # JOB_LIST binds the job atomically: even host death immediately after
            # CreateProcess cannot leave an unassigned suspended child behind.
            self.check(
                self.kernel.CreateProcessW(
                    command[0],
                    line,
                    None,
                    None,
                    True,
                    flags,
                    env,
                    str(cwd),
                    C.byref(startup),
                    C.byref(information),
                )
            )
            own(information.process)
            own(information.thread)
            return information
        finally:
            self.kernel.DeleteProcThreadAttributeList(buffer)

    def verify(self, process, job, profile):
        contained = BOOL()
        self.check(self.kernel.IsProcessInJob(process, job, C.byref(contained)))
        if not contained.value:
            raise OSError("Process was not assigned to its isolation job")
        token = HANDLE()
        self.check(self.security.OpenProcessToken(process, 0x8, C.byref(token)))
        try:
            is_container, length = DWORD(), DWORD()
            self.check(
                self.security.GetTokenInformation(
                    token, 29, C.byref(is_container), C.sizeof(is_container), C.byref(length)
                )
            )
            if not is_container.value:
                raise OSError("Process token is not an AppContainer")
            # TokenAppContainerSid contains a pointer followed by SID storage.
            data = C.create_string_buffer(256)
            self.check(
                self.security.GetTokenInformation(token, 31, data, len(data), C.byref(length))
            )
            sid = C.cast(data, C.POINTER(HANDLE))[0]
            if not sid or not self.security.EqualSid(sid, profile.sid):
                raise OSError("AppContainer identity mismatch")
            self.security.GetTokenInformation(token, 30, None, 0, C.byref(length))
            if C.get_last_error() != 122 or not length.value:
                raise C.WinError(C.get_last_error())
            groups = C.create_string_buffer(length.value)
            self.check(
                self.security.GetTokenInformation(token, 30, groups, len(groups), C.byref(length))
            )
            if C.cast(groups, C.POINTER(DWORD))[0] != 0:
                raise OSError("Unexpected AppContainer capabilities")
        finally:
            self.close_handle(token.value)

    def resume(self, thread):
        if self.kernel.ResumeThread(thread) != 1:
            raise OSError("Unable to resume the single suspended main thread")

    def poll(self, process):
        status = self.kernel.WaitForSingleObject(process, 0)
        if status == 258:
            return None
        if status != 0:
            raise C.WinError(C.get_last_error())
        code = DWORD()
        self.check(self.kernel.GetExitCodeProcess(process, C.byref(code)))
        return code.value

    def read(self, pipe):
        available = DWORD()
        if not self.kernel.PeekNamedPipe(pipe, None, 0, None, C.byref(available), None):
            if C.get_last_error() in (109, 233):
                return b""
            raise C.WinError(C.get_last_error())
        if not available.value:
            return None
        buffer = C.create_string_buffer(min(available.value, 65536))
        count = DWORD()
        self.check(self.kernel.ReadFile(pipe, buffer, len(buffer), C.byref(count), None))
        return buffer.raw[: count.value]

    def terminate_job(self, job):
        self.check(self.kernel.TerminateJobObject(job, 1))

    def active_processes(self, job):
        accounting = JobAccounting()
        self.check(
            self.kernel.QueryInformationJobObject(
                job, 1, C.byref(accounting), C.sizeof(accounting), None
            )
        )
        return accounting.active

    def close_handle(self, handle):
        self.check(self.kernel.CloseHandle(handle))
