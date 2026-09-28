"""Query adapter driver-store paths INSIDE the sandbox, without traversing them.

Uses the libdxcore D3DKMT ABI also used by NVIDIA's libnvidia-container dxcore.c:
https://github.com/NVIDIA/libnvidia-container/blob/main/src/dxcore.c
No third-party Python packages or host-side library loading are required.
"""

import ctypes as C
import json


class Luid(C.Structure):
    _fields_ = [("low", C.c_uint32), ("high", C.c_int32)]


class Adapter(C.Structure):
    _fields_ = [
        ("handle", C.c_uint32),
        ("luid", Luid),
        ("sources", C.c_uint32),
        ("regions", C.c_uint32),
    ]


class Enumeration(C.Structure):
    _fields_ = [("count", C.c_uint32), ("adapters", C.POINTER(Adapter))]


class Enumeration3(C.Structure):
    _fields_ = [("filter", C.c_uint64), ("count", C.c_uint32), ("adapters", C.POINTER(Adapter))]


class RegistryValue(C.Union):
    _fields_ = [("qword", C.c_uint64), ("text", C.c_wchar)]


class Registry(C.Structure):
    _fields_ = [
        ("query_type", C.c_uint32),
        ("flags", C.c_uint32),
        ("value_name", C.c_wchar * 260),
        ("value_type", C.c_uint32),
        ("adapter_index", C.c_uint32),
        ("output_size", C.c_uint32),
        ("status", C.c_uint32),
        ("output", RegistryValue),
    ]


class Query(C.Structure):
    _fields_ = [
        ("handle", C.c_uint32),
        ("kind", C.c_uint32),
        ("data", C.c_void_p),
        ("size", C.c_uint32),
    ]


def adapters(library):
    for name, structure in (
        ("D3DKMTEnumAdapters3", Enumeration3),
        ("D3DKMTEnumAdapters2", Enumeration),
    ):
        function = getattr(library, name, None)
        if function is None:
            continue
        function.argtypes, function.restype = [C.POINTER(structure)], C.c_int32
        request = structure()
        if structure is Enumeration3:
            request.filter = 1  # Include compute-only adapters.
        if function(C.byref(request)) != 0:
            continue
        count = request.count
        if not 0 < count <= 256:
            raise RuntimeError("Invalid WSL adapter count")
        storage = (Adapter * count)()
        request.adapters = storage
        if function(C.byref(request)) != 0 or request.count > count:
            raise RuntimeError("WSL adapters changed during enumeration")
        return [storage[index].handle for index in range(request.count)]
    raise RuntimeError("WSL adapter enumeration unavailable")


def driver_store(library, handle):
    function = library.D3DKMTQueryAdapterInfo
    function.argtypes, function.restype = [C.POINTER(Query)], C.c_int32
    request = Registry()
    request.query_type = 2  # DRIVERSTOREPATH; KMTQAITYPE_QUERYREGISTRY = 48.
    query = Query(handle, 48, C.addressof(request), C.sizeof(request))
    if function(C.byref(query)) != 0:
        raise RuntimeError("Cannot query WSL driver-store size")
    size = request.output_size
    width = C.sizeof(C.c_wchar)
    if not width <= size <= 260 * width or size % width:
        raise RuntimeError("Invalid WSL driver-store path size")
    storage = C.create_string_buffer(C.sizeof(Registry) + size + C.sizeof(C.c_wchar))
    value = Registry.from_buffer(storage)
    value.query_type, value.output_size = 2, size
    query = Query(handle, 48, C.addressof(storage), C.sizeof(Registry) + size)
    if function(C.byref(query)) != 0 or value.status != 0 or value.output_size > size:
        raise RuntimeError("Cannot query WSL driver-store path")
    text = C.wstring_at(C.addressof(storage) + Registry.output.offset, size // C.sizeof(C.c_wchar))
    return text.split("\0", 1)[0]


def probe():
    library = C.CDLL("/usr/lib/wsl/lib/libdxcore.so")
    paths, errors = set(), []
    for handle in adapters(library):
        try:
            paths.add(driver_store(library, handle))
        except RuntimeError as error:
            # Enumeration also includes non-CUDA/older display adapters, which
            # may not implement this query. CUDA startup still has to succeed
            # with only the positively identified packages mounted.
            errors.append(str(error))
    if not paths:
        raise RuntimeError("No WSL driver-store paths: " + "; ".join(errors))
    return {"driver_store_paths": sorted(paths)}


if __name__ == "__main__":
    print(json.dumps(probe()))
