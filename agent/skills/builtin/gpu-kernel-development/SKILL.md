---
name: gpu-kernel-development
description: 编写、调试或优化 CUDA C++、Triton 和 PyTorch 自定义算子，包括参考实现、正确性测试、扩展编译与 GPU 性能测量。用于算子和 kernel 开发，不用于一般模型训练或普通 Python 修改。
---

先确定算子的数学定义、输入形状、dtype、stride、设备、输出及是否需要 autograd。
沿用项目已有算子接口与构建方式；仅缺失信息妨碍实现时询问。技能不扩大执行或回写权限。

## 环境与实现

- 先通过 run_command 运行 `python -I -m sandbox.compute_probe`，核对 torch、Triton、
  CUDA 版本、nvcc 和实际 GPU。探测成功仅代表收集到环境信息，不代表 GPU 可用。
  需要执行 GPU 代码时加 `--require-gpu`。没有 GPU 或隔离命令工具时继续编写和静态检查，
  清楚记录 GPU 编译、正确性和性能尚未验证，不把 MPS、CPU 或 Triton interpreter 当成 CUDA。
- CUDA .cu/.cuh 可用 get_symbols；Triton 与 torch 都是 .py，走 Python 符号工具。
  语义工具返回符号不意味着 kernel 可以编译。为 CUDA 工程提供容器路径有效的编译配置。
- 先写简洁的 PyTorch 参考实现，再实现用户指定的 CUDA 或 Triton 算子。
  明确连续布局限制、broadcast 和 dtype promotion；不支持的输入应明确拒绝。
- CUDA 扩展可用 torch.utils.cpp_extension 或项目构建系统。使用输入设备的 guard 和
  PyTorch 当前 CUDA stream，检查 kernel 启动错误。Triton 正确处理尾部 mask、边界和空张量。
  若需 torch.compile、自定义算子注册或 autograd，分别补齐 fake/meta 与梯度支持，不能默认兼容。
- 镜像运行期禁网；编译缓存写 /tmp。单次容器结束即清空临时缓存，因此编译、测试和 benchmark
  尽量放同一条命令。首次编译可显式设置 timeout_seconds=600（cuda profile 上限 900）。
  不能通过模型参数切换 profile、GPU 或服务器命令。

## 验证与性能

- 用 torch.testing.assert_close 对照参考结果，按 dtype 与误差来源明确 atol/rtol；覆盖尾部、
  小尺寸、典型大尺寸、允许的布局、设备与 dtype。零尺寸或非连续输入按接口约定测试。
  需要训练时验证梯度；需要 stream 并发时覆盖非默认 stream。不要为通过而放宽误差或删断言。
- 基准测试前完成编译和 warm-up，用 CUDA events 或 triton.testing.do_bench 测量，
  保证 GPU 完成工作。区分 kernel-only 与端到端（含分配/拷贝），对照项使用相同口径。
  报告 GPU 型号、版本、形状、dtype、重复方式与耗时；不从 CPU wall time 推算 GPU 加速。
- 可用 `python -I -m sandbox.operator_smoke` 验证环境能运行 torch、Triton 和 CUDA 扩展；
  这是环境自检，不能替代当前算子的测试。缺 GPU 时命令失败，不算测试通过。
- 为独立验证使用稳定 check_id；更正后用同一个 check_id 重跑原断言。

交付实现、可复现的测试/基准命令和实际结果，分别说明静态检查、编译、GPU 正确性与性能状态。
性能不足时保留正确性基线，结合证据迭代，不宣称未测量的加速。
