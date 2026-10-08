"""Small real Metal compute probe, intended to run in a supervised child process.

Uses the system Objective-C runtime through ctypes; no compiler or Python package
is needed. Initially supported on Apple Silicon only (including the struct ABI).
The caller must impose a timeout, including on shader compilation and GPU waits.
"""

import ctypes as C
import json
import platform
import sys
import uuid

SOURCE = b"""
#include <metal_stdlib>
using namespace metal;
kernel void vector_add(device const uint *a [[buffer(0)]],
                       device const uint *b [[buffer(1)]],
                       device uint *out [[buffer(2)]],
                       uint i [[thread_position_in_grid]]) {
    out[i] = a[i] + b[i];
}
"""
ELEMENTS = 256


class Size(C.Structure):
    _fields_ = [("width", C.c_ulong), ("height", C.c_ulong), ("depth", C.c_ulong)]


def valid_report(report):
    return (
        isinstance(report, dict)
        and report.get("metal_kernel_verified") is True
        and report.get("runtime_shader_compilation") is True
        and report.get("elements") == ELEMENTS
        and report.get("result_checksum") == sum(4 * i + 7 for i in range(ELEMENTS))
        and isinstance(report.get("device"), str)
        and bool(report["device"])
    )


class ObjectiveC:
    def __init__(self):
        self.runtime = C.CDLL("/usr/lib/libobjc.A.dylib")
        self.runtime.objc_getClass.argtypes = [C.c_char_p]
        self.runtime.objc_getClass.restype = C.c_void_p
        self.runtime.sel_registerName.argtypes = [C.c_char_p]
        self.runtime.sel_registerName.restype = C.c_void_p

    def send(self, target, selector, result=C.c_void_p, types=(), args=()):
        if not target:
            raise RuntimeError(f"nil receiver for {selector}")
        # Separate typed function pointers: do not mutate shared objc_msgSend ABI.
        fn = C.CFUNCTYPE(result, C.c_void_p, C.c_void_p, *types)(("objc_msgSend", self.runtime))
        return fn(target, self.runtime.sel_registerName(selector.encode()), *args)

    def string(self, value):
        return self.send(
            self.runtime.objc_getClass(b"NSString"),
            "stringWithUTF8String:",
            types=(C.c_char_p,),
            args=(value,),
        )

    def text(self, value):
        if not value:
            return "unknown error"
        raw = self.send(value, "UTF8String", C.c_char_p)
        return raw.decode("utf-8", errors="replace") if raw else "unknown error"


def probe():
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise RuntimeError("Metal probe currently requires Apple Silicon macOS")
    metal = C.CDLL("/System/Library/Frameworks/Metal.framework/Metal")
    objc = ObjectiveC()
    pool = objc.send(objc.runtime.objc_getClass(b"NSAutoreleasePool"), "new")
    owned = []

    def own(value, operation, error=None):
        if not value:
            detail = ""
            if error is not None and error.value:
                detail = ": " + objc.text(objc.send(error.value, "localizedDescription"))
            raise RuntimeError(operation + " failed" + detail)
        owned.append(value)
        return value

    try:
        metal.MTLCreateSystemDefaultDevice.argtypes = []
        metal.MTLCreateSystemDefaultDevice.restype = C.c_void_p
        device = own(metal.MTLCreateSystemDefaultDevice(), "create Metal device")
        name = objc.text(objc.send(device, "name"))
        # Avoid accepting a cached shader as proof that compilation is permitted.
        kernel_name = ("vector_add_" + uuid.uuid4().hex).encode("ascii")
        source = SOURCE.replace(b"vector_add", kernel_name)
        error = C.c_void_p()
        library = own(
            objc.send(
                device,
                "newLibraryWithSource:options:error:",
                types=(C.c_void_p, C.c_void_p, C.POINTER(C.c_void_p)),
                args=(objc.string(source), None, C.byref(error)),
            ),
            "compile Metal source",
            error,
        )
        function = own(
            objc.send(
                library,
                "newFunctionWithName:",
                types=(C.c_void_p,),
                args=(objc.string(kernel_name),),
            ),
            "create kernel",
        )
        error = C.c_void_p()
        pipeline = own(
            objc.send(
                device,
                "newComputePipelineStateWithFunction:error:",
                types=(C.c_void_p, C.POINTER(C.c_void_p)),
                args=(function, C.byref(error)),
            ),
            "create compute pipeline",
            error,
        )
        queue = own(objc.send(device, "newCommandQueue"), "create command queue")
        arrays = [
            (C.c_uint32 * ELEMENTS)(*range(ELEMENTS)),
            (C.c_uint32 * ELEMENTS)(*(3 * i + 7 for i in range(ELEMENTS))),
            (C.c_uint32 * ELEMENTS)(*([0xFFFFFFFF] * ELEMENTS)),
        ]
        buffers = [
            own(
                objc.send(
                    device,
                    "newBufferWithBytes:length:options:",
                    # MTLResourceStorageModeShared = 0; CPU and GPU see the same storage.
                    types=(C.c_void_p, C.c_ulong, C.c_ulong),
                    args=(C.cast(array, C.c_void_p), C.sizeof(array), 0),
                ),
                "allocate shared buffer",
            )
            for array in arrays
        ]
        command = objc.send(queue, "commandBuffer")
        encoder = objc.send(command, "computeCommandEncoder")
        objc.send(encoder, "setComputePipelineState:", None, (C.c_void_p,), (pipeline,))
        for index, buffer in enumerate(buffers):
            objc.send(
                encoder,
                "setBuffer:offset:atIndex:",
                None,
                (C.c_void_p, C.c_ulong, C.c_ulong),
                (buffer, 0, index),
            )
        objc.send(
            encoder,
            "dispatchThreadgroups:threadsPerThreadgroup:",
            None,
            (Size, Size),
            (Size(ELEMENTS, 1, 1), Size(1, 1, 1)),
        )
        objc.send(encoder, "endEncoding", None)
        objc.send(command, "commit", None)
        objc.send(command, "waitUntilCompleted", None)
        # MTLCommandBufferStatusCompleted = 4; device creation alone is not success.
        if objc.send(command, "status", C.c_ulong) != 4:
            error = objc.send(command, "error")
            detail = objc.text(objc.send(error, "localizedDescription")) if error else "unknown"
            raise RuntimeError("Metal command failed: " + detail)
        pointer = objc.send(buffers[2], "contents")
        if not pointer:
            raise RuntimeError("Metal result buffer is inaccessible")
        values = list((C.c_uint32 * ELEMENTS).from_address(pointer))
        if values != [4 * i + 7 for i in range(ELEMENTS)]:
            raise RuntimeError("Metal vector addition returned incorrect results")
        return {
            "metal_kernel_verified": True,
            "device": name,
            "elements": ELEMENTS,
            "result_checksum": sum(values),
            "runtime_shader_compilation": True,
        }
    finally:
        for value in reversed(owned):
            objc.send(value, "release", None)
        objc.send(pool, "drain", None)


def main():
    try:
        result = probe()
    except Exception as error:
        print(json.dumps({"metal_kernel_verified": False, "error": str(error)}), flush=True)
        return 1
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
