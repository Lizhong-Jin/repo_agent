"""GPU-only correctness smoke: torch reference, Triton JIT, CUDA extension.

Run through run_command with timeout_seconds=600 in the cuda sandbox profile.
No CUDA GPU is a failure. These checks validate the environment, not arbitrary
user kernels. Imports are lazy so CPU-only hosts can still inspect this module.
"""

import argparse
import json

from .compute_probe import collect

CUDA_SOURCE = r"""
#include <torch/extension.h>
#include <ATen/cuda/CUDAContext.h>
#include <c10/cuda/CUDAGuard.h>
#include <c10/cuda/CUDAException.h>
__global__ void add_kernel(const float* a, const float* b, float* out, int64_t n) {
    int64_t i = int64_t(blockIdx.x) * blockDim.x + threadIdx.x;
    if (i < n) out[i] = a[i] + b[i];
}
torch::Tensor cuda_add(torch::Tensor a, torch::Tensor b) {
    TORCH_CHECK(a.is_cuda() && b.is_cuda(), "CUDA tensors required");
    TORCH_CHECK(a.device() == b.device(), "devices must match");
    TORCH_CHECK(a.scalar_type() == torch::kFloat32 && b.scalar_type() == torch::kFloat32,
                "float32 required");
    TORCH_CHECK(a.dim() == 1 && b.dim() == 1 && a.numel() == b.numel(), "1D equal sizes required");
    TORCH_CHECK(a.is_contiguous() && b.is_contiguous(), "contiguous tensors required");
    c10::cuda::CUDAGuard guard(a.device());
    auto out = torch::empty_like(a);
    if (a.numel() != 0) {
        add_kernel<<<(a.numel() + 255) / 256, 256, 0, at::cuda::getCurrentCUDAStream()>>>(
            a.data_ptr<float>(), b.data_ptr<float>(), out.data_ptr<float>(), a.numel());
        C10_CUDA_KERNEL_LAUNCH_CHECK();
    }
    return out;
}
"""


def run_checks(*, benchmark: bool = False) -> dict:
    import torch
    import triton
    from torch.utils.cpp_extension import load_inline

    from .triton_smoke_kernel import add_kernel

    def triton_add(a, b):
        out = torch.empty_like(a)
        if a.numel():
            add_kernel[(triton.cdiv(a.numel(), 256),)](a, b, out, a.numel(), BLOCK=256)
        return out

    extension = load_inline(
        name="repo_agent_cuda_smoke",
        cpp_sources="torch::Tensor cuda_add(torch::Tensor a, torch::Tensor b);",
        cuda_sources=CUDA_SOURCE,
        functions=["cuda_add"],
        with_cuda=True,
        extra_cuda_cflags=["-O2"],
        verbose=False,
    )
    torch.manual_seed(17)
    cases = 0
    dtypes = [torch.float32, torch.float16]
    if torch.cuda.is_bf16_supported():
        dtypes.append(torch.bfloat16)
    stream = torch.cuda.Stream()
    with torch.cuda.stream(stream):
        for dtype in dtypes:
            for n in (0, 1, 33, 1025, 65537):
                a = torch.randn(n, device="cuda", dtype=dtype)
                b = torch.randn_like(a)
                reference = a + b
                torch.testing.assert_close(triton_add(a, b), reference)
                if dtype == torch.float32:
                    torch.testing.assert_close(extension.cuda_add(a, b), reference)
                cases += 1
    stream.synchronize()
    result = {
        "status": "passed",
        "cases": cases,
        "cuda_extension": "passed",
        "triton": "passed",
        "nondefault_stream": "passed",
    }
    if benchmark:
        from triton.testing import do_bench

        a = torch.randn(1 << 20, device="cuda")
        b = torch.randn_like(a)
        # Same end-to-end call boundary: every implementation allocates its output.
        result["benchmark"] = {
            "n": a.numel(),
            "dtype": "float32",
            "unit": "ms",
            "scope": "GPU event timing, output allocated per call, after warmup",
            "torch": do_bench(lambda: a + b, warmup=100, rep=300),
            "triton": do_bench(lambda: triton_add(a, b), warmup=100, rep=300),
            "cuda": do_bench(lambda: extension.cuda_add(a, b), warmup=100, rep=300),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", action="store_true")
    args = parser.parse_args()
    environment = collect()
    if not environment["operator_environment_ready"]:
        print(json.dumps({"status": "unavailable", "environment": environment}, indent=2))
        raise SystemExit(1)
    result = run_checks(benchmark=args.benchmark)
    print(json.dumps({"environment": environment, "verification": result}, indent=2))


if __name__ == "__main__":
    main()
