"""Standalone CUDA driver/kernel probe, executed only INSIDE the native sandbox.

No PyTorch, nvcc or third-party Python module is needed to verify device access.
"""

import ctypes as C
import json
import uuid


PTX = b"""
.version 6.0
.target sm_50
.address_size 64
.visible .entry native_probe(.param .u64 output) {
    .reg .b64 %ptr;
    .reg .b32 %value;
    ld.param.u64 %ptr, [output];
    mov.u32 %value, 42;
    st.global.u32 [%ptr], %value;
    ret;
}
"""


def probe():
    driver = C.CDLL("libcuda.so.1")

    def function(name, *argtypes):
        fn = getattr(driver, name)
        fn.argtypes = list(argtypes)
        fn.restype = C.c_int

        def checked(*args):
            code = fn(*args)
            if code != 0:
                raise RuntimeError(f"{name} failed (CUDA error {code})")
        return checked

    ptr, integer, uint = C.c_void_p, C.c_int, C.c_uint
    deviceptr = C.c_uint64
    init = function("cuInit", uint)
    count_devices = function("cuDeviceGetCount", C.POINTER(integer))
    get_device = function("cuDeviceGet", C.POINTER(integer), integer)
    get_uuid = function("cuDeviceGetUuid", ptr, integer)
    create = function("cuCtxCreate_v2", C.POINTER(ptr), uint, integer)
    destroy = function("cuCtxDestroy_v2", ptr)
    allocate = function("cuMemAlloc_v2", C.POINTER(deviceptr), C.c_size_t)
    free = function("cuMemFree_v2", deviceptr)
    load = function("cuModuleLoadData", C.POINTER(ptr), ptr)
    unload = function("cuModuleUnload", ptr)
    get_kernel = function("cuModuleGetFunction", C.POINTER(ptr), ptr, C.c_char_p)
    launch = function("cuLaunchKernel", ptr, uint, uint, uint, uint, uint, uint,
                      uint, ptr, C.POINTER(ptr), C.POINTER(ptr))
    synchronize = function("cuCtxSynchronize")
    copy = function("cuMemcpyDtoH_v2", ptr, deviceptr, C.c_size_t)
    init(0)
    count = integer()
    count_devices(C.byref(count))
    if count.value < 1:
        raise RuntimeError("CUDA reported no usable devices")
    devices = []
    for ordinal in range(count.value):
        device, context, module, memory = integer(), ptr(), ptr(), deviceptr()
        get_device(C.byref(device), ordinal)
        identity = C.create_string_buffer(16)
        get_uuid(identity, device)
        create(C.byref(context), 0, device)
        try:
            allocate(C.byref(memory), 4)
            load(C.byref(module), C.create_string_buffer(PTX))
            kernel = ptr()
            get_kernel(C.byref(kernel), module, b"native_probe")
            args = (ptr * 1)(C.cast(C.byref(memory), ptr))
            launch(kernel, 1, 1, 1, 1, 1, 1, 0, None, args, None)
            synchronize()
            value = uint()
            copy(C.byref(value), memory, 4)
            if value.value != 42:
                raise RuntimeError("CUDA kernel returned an incorrect result")
            devices.append("GPU-" + str(uuid.UUID(bytes=identity.raw)))
        finally:
            # Destroy the context even if cleanup of a particular allocation fails.
            try:
                if module.value:
                    unload(module)
                if memory.value:
                    free(memory)
            finally:
                destroy(context)
    return {"cuda_kernel_verified": True, "devices": devices}


if __name__ == "__main__":
    print(json.dumps(probe()))
