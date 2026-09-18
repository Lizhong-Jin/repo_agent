"""Imported only by the GPU smoke after validating the runtime dependencies."""

import triton
import triton.language as tl


@triton.jit
def add_kernel(a, b, out, n, BLOCK: tl.constexpr):
    offsets = tl.program_id(0) * BLOCK + tl.arange(0, BLOCK)
    valid = offsets < n
    values = tl.load(a + offsets, valid, other=0) + tl.load(b + offsets, valid, other=0)
    tl.store(out + offsets, values, valid)
