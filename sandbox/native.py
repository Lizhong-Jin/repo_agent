"""Native backend selection and compatibility imports.

New platform implementations inherit NativeBackendBase, never the macOS backend.
"""

import sys

from host_support.processes import ProcessRunner as ProcessRunner

from .macos_native import MacOSNativeBackend
from .macos_native import seatbelt_profile as seatbelt_profile
from .native_common import NativeTool as NativeTool


def create_native_backend(workspace, **options):
    if sys.platform == "linux":
        from .linux_native import LinuxNativeBackend

        return LinuxNativeBackend(workspace, **options)
    if sys.platform == "darwin":
        return MacOSNativeBackend(workspace, **options)
    raise ValueError("native 沙箱仅支持 macOS/Linux；不会退回未隔离执行")


class _NativeConstructor(type):
    def __call__(cls, *args, **kwargs):
        if cls is NativeBackend and sys.platform == "linux":
            return create_native_backend(*args, **kwargs)
        return super().__call__(*args, **kwargs)


class NativeBackend(MacOSNativeBackend, metaclass=_NativeConstructor):
    """Legacy constructor and macOS subclass API; CLI uses the explicit factory."""

    def __new__(cls, *args, **kwargs):
        if cls is NativeBackend:
            if sys.platform == "linux":
                from .linux_native import LinuxNativeBackend

                # Preserve direct __new__ calls used to allocate an uninitialized
                # backend. Normal construction is dispatched by the metaclass.
                return object.__new__(LinuxNativeBackend)
            if sys.platform != "darwin":
                raise ValueError("native 沙箱仅支持 macOS/Linux；不会退回未隔离执行")
        return object.__new__(cls)
