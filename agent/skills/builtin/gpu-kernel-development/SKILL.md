---
name: gpu-kernel-development
description: 编写、调试或优化 CUDA C++、Triton 和 PyTorch 自定义算子，按当前 native / Docker 执行环境完成参考实现、正确性测试、编译与 GPU 性能测量。用于算子和 kernel 开发，不用于一般模型训练或普通 Python 修改。
---

确定算子的数学定义、输入形状、dtype、stride、设备、输出及是否需要 autograd。
沿用项目已有接口与构建方式；仅缺失信息妨碍实现时询问。技能不扩大执行或回写权限。
先通过 load_skill 加载 coding 技能；当前上下文已有正文时直接复用。

## 先确认实际执行环境

- 优先调用 get_execution_environment，传入
  `{"sections":["execution","system","runtimes","gpu"]}`。
  依据 execution 中的模式、工作区路径、执行权限、tool_limits 和 GPU 授权信息决定后续操作，
  依据 runtimes.python.executable 选择解释器；不要根据 CLI 所在机器或历史对话推断环境。
- 区分 GPU 授权、驱动可计算、框架可用和当前算子验证通过。
  Linux native 的 execution.gpu_access.startup_probe 验证驱动/PTX kernel，不验证 PyTorch、Triton 或 nvcc。
  gpu.status 依赖 PyTorch，缺少 PyTorch 时的 unknown 不代表没有物理 GPU。
  operator_environment_ready 要求 PyTorch CUDA、Triton、nvcc 同时可用，是完整环境自检条件；
  只做 PyTorch 或 Triton 任务时，按实际使用的依赖判断，不因无关组件缺失阻止可做的工作。
- local 不提供命令/Python 执行；macOS native 不提供本项目的 NVIDIA CUDA GPU 支持。
  缺少所需 GPU、依赖或执行权限时，继续允许范围内的实现与代码检查，明确未执行的验证。
  不将 CPU、MPS、Triton interpreter、环境查询成功或符号查询成功视作 CUDA 验证通过。

## 按模式准备编译与运行

- Linux / WSL2 native：默认 auto 检测 NVIDIA CUDA GPU；standard 明确关闭 GPU。
  使用原项目的真实路径，修改立即生效，没有 `/apply` 或 Docker 回写备份。
  使用 Agent 解释器中已有的框架及允许访问的系统工具链；不要假定激活的项目 venv、
  宿主机任意 CUDA_HOME/LD_LIBRARY_PATH 或 CUDA_VISIBLE_DEVICES 会自动继承。
  缺依赖时说明所需环境准备，不在只读解释器目录或断网命令中反复尝试安装。
- Docker：使用容器 `/workspace` 路径和镜像内依赖，修改先留在副本中，沿用应用回写流程。
  镜像构建、依赖预装和 GPU 选择由宿主机配置；不要把 Docker 配额套用到 native。
- 两种隔离模式的命令均断网；已启用的主进程 web_search/web_fetch 只用于查询公开资料，
  不给项目代码提供网络或安装能力。GPU/profile 不能通过模型工具参数切换。
- 缓存优先使用执行器已设置的 TMPDIR、TRITON_CACHE_DIR、TORCH_EXTENSIONS_DIR 等路径，
  需要临时目录时使用 tempfile.gettempdir()，不要在 native 硬编码可写 `/tmp`。
  默认临时缓存不跨工具调用保留；将一次候选的编译、测试、预热与测量放在同一进程中，
  需保留的源码、测试和测量结果写到工作区普通文件。
- 从工具 schema 或 execution.tool_limits 读取实际超时上限；GPU 模式通常允许最高 900 秒，
  默认命令/Python 仍为 60/10 秒。首次编译可在上限允许时显式设置 timeout_seconds=600，
  不给 standard 工具发送超出其上限的参数。超时或输出截断不是测试通过。
- 仅工具 schema 包含 check_id 时提供稳定检查标识；当前 Docker 代理支持，native 不接受。
  native 直接按退出码、超时、清理状态和实际测试输出判断结果。

## 实现与验证

- 提供或复用简洁的参考实现，再实现用户指定的 CUDA 或 Triton 算子。
  明确布局、broadcast、dtype promotion 及不支持输入的处理。
  `.cu`/`.cuh` 走 clangd，Triton/PyTorch 的 `.py` 走 Python 语言服务；
  compile_commands.json 或 .clangd 中的 Toolkit/include 和路径须符合实际执行环境。
- CUDA 扩展沿用项目构建系统或 torch.utils.cpp_extension；使用输入设备 guard 和
  PyTorch 当前 CUDA stream，检查 kernel 启动错误。Triton 处理尾部 mask、边界和空张量。
  需要 torch.compile、自定义算子注册或 autograd 时，补齐并分别验证相应接入。
- 用 torch.testing.assert_close 或项目已有检查对照参考结果，按 dtype 和误差来源明确
  atol/rtol；覆盖尾部、小尺寸、典型大尺寸及接口允许的布局和 dtype。
  有梯度或 stream 要求时做对应验证，不为通过而放宽误差或删断言。
- 完整环境自检可在相关模块可导入且依赖齐备时运行
  `python -I -m sandbox.operator_smoke`，解释器用实际环境报告的路径，超时按工具上限设置。
  native 中 `-I -m` 找不到 Agent 模块时不要判为 GPU 故障；优先使用环境工具和项目测试。
  自检包含 CUDA 扩展、Triton 与 PyTorch，不能代替当前算子的测试。
  `python -I -m sandbox.compute_probe --require-gpu` 同样要求整套依赖，不是单独的驱动探测。

## 性能与迭代

- 正确性通过后再比较性能。完成编译和 warm-up，用 CUDA events 或项目已有 GPU benchmark
  方法测量并确保 GPU 完成工作；区分 kernel-only 与端到端（含分配/拷贝），基线保持相同口径。
  不将工具/容器总耗时或未同步的 CPU 计时当作 GPU kernel 延迟。
- 报告 GPU 型号、版本、shape/dtype、重复方式、延迟及相对基线结果。native 无 CPU/显存配额
  不代表 GPU 独占；负载变化或接近噪声的差异需要复测，不直接宣称加速。
- 用户要求逐步优化时，依据测量结果提出下一项修改，保留正确基线、最佳实现与实验记录；
  退化候选不替换已验证最佳结果。遵守用户预算，记录尚未完成的实验，不能把轮数无上限
  当作后台任务、自动调参器或保证持续优化的机制。

交付实现、可复现的测试/benchmark 命令和实际结果，分别说明代码检查、编译、GPU 正确性
与性能状态；native 说明已修改原项目，Docker 说明副本及实际回写状态，不宣称未测量的加速。
