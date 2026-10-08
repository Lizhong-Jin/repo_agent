# macOS native Metal 支持与验证

Apple Silicon macOS native 已接入 Metal。默认 `auto` 将 Apple Silicon 视为 GPU 候选，
在真实沙箱内执行运算自检；`metal` 显式要求启用，`standard` 关闭 GPU。
Intel/AMD Mac 或 x86_64 Python 的 auto 使用 standard，显式 metal 则报错。
选择 Metal 后任何驱动、权限或运算自检失败均拒绝启动，不静默回退 CPU。

```bash
repo-agent --sandbox native                         # Apple Silicon 自动启用 Metal
repo-agent --sandbox native --sandbox-profile metal # 显式要求 Metal
repo-agent --sandbox native --sandbox-profile standard # 关闭 GPU
```

当前使用系统默认 Metal 设备，不接受 `--sandbox-gpus`；Docker / Linux 不接受 metal profile。
基础自检使用 Agent Python，用户代码和 MPS 检测使用选定的项目 Python（支持 `--project-python`）。
`get_execution_environment` 的 `execution.gpu_access` 包含授权状态、设备及启动 kernel 自检；
`gpu.metal` 报告基础 Metal 状态，`gpu.mps` 单独报告 PyTorch MPS 实际矩阵运算结果。
未安装 PyTorch 不影响基础 Metal；不会自动安装任何计算依赖。MPS 检测及 native 环境默认
设置 `PYTORCH_ENABLE_MPS_FALLBACK=0`，避免把 CPU fallback 当作 GPU 成功。

Metal 模式的命令/Python 最大超时为 900 秒，默认仍为 60/10 秒。源码及发行包都使用同一套
Python 探针和系统 Metal framework，无需编译额外 helper，也不依赖可选 Rust 扩展。

在 Apple Silicon Mac 的终端执行：

```bash
.venv/bin/python -m sandbox.metal_check
```

使用项目 Python 标准库和系统 Metal / Objective-C runtime，不需要 PyTorch、
Rust、Xcode 或第三方 Python 桥接库。验证创建并清理临时工作区，通过现有 native
进程监督器执行，运算进程限时 45 秒；失败返回非零状态及 JSON 错误，不重试为
无限制执行。外层工具沙箱可能屏蔽 GPU 或禁止嵌套 Seatbelt，需从正常终端运行。

## 权限

在现有默认拒绝的 Seatbelt 策略上仅新增：

```scheme
(allow iokit-open (iokit-user-client-class "AGXDeviceUserClient"))
(allow mach-lookup (xpc-service-name "com.apple.MTLCompilerService"))
```

前者允许连接 Apple Silicon GPU，后者允许访问 Metal 着色器编译服务。权限使用
精确名称，不开放全部 IOKit / Mach 服务，不新增 IOSurface、WindowServer、网络、
宿主缓存目录或 `file-issue-extension` 权限。现有文件保护规则保持原样；验证进程的
HOME 和临时目录继续由 native 后端放在每次调用的私有目录内。

这些是**当前探针在已测试机器上的最小增量权限**，不是所有 Metal / MPS / MLX
程序或 macOS 版本的完整权限清单。使用系统 GPU 驱动和编译服务也不提供 GPU
独占或资源配额。

## 真实运算与隔离

`sandbox/metal_probe.py` 在沙箱内创建设备、从源码编译计算 kernel、创建共享缓冲区、
提交 256 项无符号整数向量加法、等待命令完成，并逐项核对结果。输入为 `a[i]=i`、
`b[i]=3*i+7`；预期输出为 `4*i+7`，校验和为 `132352`。输出缓冲区预填充不同的
哨兵值，没有 CPU 运算替代路径。每次使用唯一 kernel 名，避免把缓存命中当成编译
权限已经通过的证据。只有命令成功完成且全部结果正确，才报告
`metal_kernel_verified=true`。

`sandbox/metal_check.py` 先执行现有 native 启动隔离自检，再在**完成 Metal 运算的
同一个进程中**验证：

- 工作区外 canary、工作区 `.env` 和 `.git/config` 的内容读写均被拒绝。
- 本地网络连接、监听以及新建硬链接被拒绝。
- 指向工作区外 canary 的符号链接无法绕过内容访问限制。
- 子进程继承工作区外文件读取限制。
- 普通工作区文件和私有临时文件仍能正常读写。

宿主进程最后核对 canary 未被改动，成功时返回 `isolation_verified=true`。

## 已验证范围与回归

2026-10-08 在 Apple M4 / macOS 15.8.1 / arm64 上验证：

| 增量权限 | 结果 |
| --- | --- |
| 无 | 无法创建 Metal 设备 |
| 仅 AGXDeviceUserClient | 设备可创建，源码编译失败 |
| 仅 MTLCompilerService | 无法创建 Metal 设备 |
| 两者均启用 | 编译、GPU 运算和隔离检查通过 |

可复现的真实硬件测试（显式启用后，设备或权限不足会失败，不会静默跳过）：

```bash
RUN_NATIVE_METAL_TESTS=1 .venv/bin/python -m pytest -q tests/test_macos_metal.py
```

测试同时覆盖 auto / metal / standard、工具和子进程执行，以及环境报告。未设置该变量时，
只执行可移植测试，跳过硬件测试。可额外指定 `NATIVE_TEST_MPS_PYTHON=/绝对路径/python`
验证选定项目环境中的真实 PyTorch MPS 运算。已在同一台 M4 上验证 PyTorch 2.14.1 / Python 3.13.15。

`python -m sandbox.compute_probe --require-mps` 可单独要求 MPS 矩阵运算通过；原有
`--require-gpu` 和 `operator_environment_ready` 保留 CUDA / Triton / nvcc 组合条件。
基础 Metal 自检不保证所有 MPS 算子均支持，也不声明 Intel/AMD Mac、其他 macOS 版本、
MLX 或图形渲染的兼容性。

API 参考：[Apple Metal compute 编码流程](https://developer.apple.com/library/archive/documentation/Miscellaneous/Conceptual/MetalProgrammingGuide/Compute-Ctx/Compute-Ctx.html)、
[运行时编译 Metal 库](https://developer.apple.com/documentation/metal/mtldevice/makelibrary(source:options:))。
