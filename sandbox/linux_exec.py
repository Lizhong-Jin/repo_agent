"""Trusted Linux launcher: install seccomp before executing any project code.

Invoked by absolute path with Python -I inside bubblewrap. This file deliberately
uses only the standard library; libseccomp is a required OS dependency.
"""

import ctypes
import errno
import os
import sys


# Deny network sockets, including pathname Unix sockets reachable via the workspace.
# Anonymous socketpair is allowed for Node/libuv child-process pipes: it creates
# an already-connected private pair, without access to an external endpoint.
# io_uring must also be blocked: it can perform socket operations independently
# of the socket/connect syscalls. Namespace changes and hard links are not needed
# by project tools and would weaken the filesystem boundary.
DENIED_SYSCALLS = (
    "socket", "socketcall", "connect", "bind", "listen",
    "accept", "accept4", "io_uring_setup", "io_uring_enter", "io_uring_register",
    "link", "linkat", "mount", "umount", "umount2", "pivot_root", "chroot",
    "setns", "unshare", "ptrace", "process_vm_readv", "process_vm_writev",
    "open_by_handle_at", "name_to_handle_at", "bpf", "userfaultfd",
    "mknod", "mknodat", "keyctl", "add_key", "request_key",
)


def install_seccomp():
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    # seccomp_load sets no_new_privs by default; the filter survives exec/fork.
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise OSError("Cannot create seccomp policy")
    try:
        for name in DENIED_SYSCALLS:
            number = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number == -1:  # Syscall does not exist on this architecture/library.
                continue
            code = lib.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0)
            if code < 0:
                raise OSError(-code, "Cannot restrict syscall " + name)
        code = lib.seccomp_load(context)
        if code < 0:
            raise OSError(-code, "Cannot load seccomp policy")
    finally:
        lib.seccomp_release(context)


def main():
    if len(sys.argv) < 2:
        raise ValueError("Missing sandbox command")
    install_seccomp()
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)


if __name__ == "__main__":
    main()
