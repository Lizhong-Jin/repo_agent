# CUDA、Triton 与 PyTorch 算子开发

[文档首页](index.md) · [项目首页](../README.md) · [Sandbox](../sandbox/README.md)

此功能包括 CUDA 文件符号查询、GPU 沙箱运行配置、GPU 开发镜像、环境探测、
算子环境自检，以及内置 `$gpu-kernel-development` 技能。Triton/PyTorch 是 Python 库，
其 `.py` 文件继续使用 pylsp；`.cu`、`.cuh` 使用 clangd 的 `cuda` 语言标识。

## 先选择执行模式

| 模式 | GPU 与依赖来源 | 路径与文件生效 | 资源及缓存 |
| --- | --- | --- | --- |
| Linux / WSL2 native | 本机 NVIDIA 驱动、项目 Python 环境中的框架和可访问的系统 Toolkit；不需要 Docker 或 NVIDIA Container Toolkit | 原项目真实路径，直接修改，无 `/apply` 或回写备份 | 无 CPU/内存/显存配额，也不保证独占 GPU；默认缓存按工具调用清理 |
| macOS native | 本项目不提供 NVIDIA CUDA GPU 接入 | 原项目真实路径，直接修改 | 可做允许范围内的代码编辑和检查，不能据此声明 CUDA 实测通过 |
| Docker cuda | Docker 主机驱动、NVIDIA Container Toolkit、CUDA 镜像内框架及 Toolkit | 容器 `/workspace`，副本变更按回写策略生效 | 有容器 CPU/内存配额，无显存配额；默认 `/tmp` 缓存不跨调用保留 |
| local | 不提供命令、Python 或 GPU 探测子进程 | 直接修改原项目 | GPU 查询为 unknown；不执行编译或 benchmark |

### Linux / WSL2 native

```bash
repo-agent --sandbox native
repo-agent --sandbox native --sandbox-profile standard   # 强制关闭 GPU
repo-agent --sandbox native --sandbox-profile cuda       # 强制要求 GPU
repo-agent --sandbox native --sandbox-gpus 0              # 普通 Linux 选择单卡
repo-agent --sandbox native --sandbox-gpus all            # WSL2 仅支持 all
```

默认 auto 发现 NVIDIA CUDA 设备后启用全部 GPU，没有发现则使用 standard；检测到设备但驱动、
设备权限或 CUDA kernel 启动自检异常时明确报错，不静默降级。macOS 不进行此 GPU 检测。
驱动自检执行小型 PTX kernel，**不依赖也不验证 PyTorch、Triton 或 nvcc**。

按任务提前准备 项目实际使用的 Python 环境和系统工具链。native 安装不自动安装 CUDA、
PyTorch 或 Triton；不能把宿主机另一套 venv 中可导入的包当作 Agent 环境已具备的依赖。
当前原生后端只读挂载解释器/依赖目录，命令断网；依赖由用户在沙箱外准备。
不要将 Docker 镜像构建作为 native 的必经步骤。完整授权、工具链查找与限制见
[原生 GPU 说明](native-sandbox.md#linux--wsl2-原生-gpu)。

### Docker CUDA 环境

当前 GPU 镜像基线面向 Linux x86_64，使用官方
`pytorch/pytorch:2.7.1-cuda12.8-cudnn9-devel`：PyTorch 2.7.1、CUDA Toolkit 12.8、
cuDNN 9，配套固定 Triton 3.3.1。包含 nvcc、C/C++ 编译器、Ninja、pytest 和语言服务器。
升级时应一起检查 PyTorch、Triton、Toolkit、驱动及 GPU 架构兼容性。

宿主机需要 NVIDIA GPU、兼容驱动、Docker 和 NVIDIA Container Toolkit。
镜像包含 CUDA 用户态工具链，不包含宿主机内核驱动；GPU 通过 Docker 的 `--gpus` 挂载。
Mac 的 MPS 或 Linux CPU 模式不能替代 CUDA/Triton GPU 验证。

## Docker 构建与启动

显式选择 Docker 模式时，普通开发和 GPU 算子开发使用以下入口；GPU 能否使用取决于 Docker 主机：

```bash
# 首次使用或更新沙箱代码后构建，不需要模型配置
repo-agent-build-sandbox

# 完成项目模型配置后启动任意任务
repo-agent --sandbox docker "任务内容"
```

例如任务内容可以是“用 Triton 实现逐元素加法，提供 PyTorch 参考实现、边界测试和基准测试”。
可以在任务中引用内置 `$gpu-kernel-development` 技能，无需另选 GPU 启动命令。

仓库只保留 `sandbox/Dockerfile`，Node/TypeScript/Go 构建阶段由普通环境与 CUDA 环境共用。
`sandbox/build.py` 在构建前调用 `sandbox/environment.py` 检测，并传入基础镜像和环境参数。
Dockerfile 内部不能可靠检测宿主机 GPU，因此请使用上述构建入口；直接运行普通 `docker build`
默认只构建 standard 环境。GPU 镜像构建检查依赖版本、nvcc 和 CUDA 符号查询，
实际 kernel 编译与执行在运行期验证。

构建与任务启动共用以下规则：

- 检测当前 Docker context 对应的 Docker 主机，不根据客户端是 Mac 还是 Linux 猜测硬件。
- Docker 未配置 NVIDIA runtime 时选择 standard，并打印原因。这不等于物理机器没有显卡；
  Linux GPU 主机应先安装驱动并配置 NVIDIA Container Toolkit。
- 已配置 NVIDIA runtime 时，在短暂、无工作目录挂载的容器内执行 `nvidia-smi -L`。
  确认可用 NVIDIA GPU 后选择 cuda；明确报告无设备时选择 standard。
- 首次探测没有现成 Agent 镜像时，会拉取小型官方 `ubuntu:22.04` 镜像；已有镜像则复用。
  NVIDIA runtime 已配置但探测失败、超时或驱动损坏时明确报错，避免误判为普通环境。
- 当前 CUDA 镜像限 Linux x86_64；检测到其他架构的 NVIDIA 环境时明确报错。
- 两种环境都使用 `repo-agent-sandbox:v1`。构建会更新该标签，启动校验镜像的环境标签；
  缺镜像、旧镜像未标记或标签与检测结果不匹配时，提示重新执行同一条构建命令。
  运行中的后端继续使用固定的镜像 ID；更新镜像后重启 Agent，恢复会话时也会按当前配置重建后端。

高级覆盖选项仅用于特殊需求，日常使用无需指定：

- 构建入口接受 `--profile standard|cuda`，可手动覆盖检测结果；强制 cuda 构建仍要求 x86_64，
  但不要求构建时有 GPU。任务入口接受 `--sandbox-profile standard|cuda`。
- `--sandbox-gpus` 支持 `all`、单个设备索引（如 `0`）或 `GPU-...` UUID；CUDA 环境省略时默认 `all`。
- 构建的 `--image` 与任务的 `--sandbox-image` 可选择同一个自定义镜像名称。
  显式指定的无环境标签自定义镜像允许使用，由用户负责依赖完整性；带标签的镜像仍会校验。
- standard 环境不挂载 GPU；local 模式不使用 Docker 自动检测，也不支持 GPU profile。
- Profile、GPU 与资源配置由启动配置和宿主机检测决定，模型工具参数不能修改。

以下配额仅适用于 Docker，native 不套用此表中的 CPU、内存、PID 或临时目录大小限制：

| 资源 | standard | cuda |
| --- | --- | --- |
| 容器内存 / CPU | 512 MiB / 1 | 8 GiB / 4 |
| PID 上限 | 64 | 256 |
| `/tmp` / 共享内存 | 64 MiB / 64 MiB | 2 GiB / 1 GiB |
| 单文件大小限制 | 64 MiB | 1 GiB |
| run_command 最大超时 | 120 秒 | 900 秒 |
| run_python 最大超时 | 30 秒 | 900 秒 |
| 容器总超时 | 130 秒 | 930 秒 |

默认单次命令超时为 60 秒，Python 超时为 10 秒；首次 CUDA 扩展编译显式设置 `timeout_seconds=600` 或 900。
最终回写验证使用 profile 的命令超时上限。GPU 显存不由容器内存上限约束；测试需按目标卡容量选尺寸。
禁网、非 root、只读根文件系统和原有 workspace 回写流程继续生效，不使用 privileged 模式。

## 探测和实际自检

优先使用 `get_execution_environment`，显式请求 GPU 分组：

```json
{"sections":["execution","system","runtimes","gpu"]}
```

`execution` 返回实际模式、工作区、执行权限、可写位置和 `tool_limits`；native Linux 的
`gpu_access` 还记录授权设备及启动驱动自检。`runtimes.python.executable` 是所用解释器路径。
`gpu` 检查 PyTorch/Triton、CUDA 设备、nvcc 和驱动信息；查询成功只代表获得报告。
GPU 分项 unknown 可能是依赖缺失或探测超时，不足以证明没有物理 GPU。

`operator_environment_ready` 表示 PyTorch CUDA、Triton、nvcc 同时满足整套环境自检条件。
按当前任务选择依赖：缺少 Triton 不应阻止只需要 PyTorch 的测试，缺少 nvcc 也不应直接判定
Triton kernel 无法执行。native 启动驱动自检通过不等于框架和当前算子已验证。

当 Agent 的 `sandbox` 模块可以在该解释器中导入时，也可通过 `run_command` 运行
`python -I -m sandbox.compute_probe`。`--require-gpu` 检查的是上述整套环境条件，
不只是 GPU 存在。native 环境下隔离的 `-I -m` 无法找到模块时，优先使用环境工具和项目测试，
不要将模块导入失败报告为 GPU 故障。

完整算子环境自检示例（**仅在报告和 schema 允许 900 秒超时时使用**）：

```json
{"command":["python","-I","-m","sandbox.operator_smoke"],"timeout_seconds":900}
```

将 `python` 替换为环境报告的解释器路径。native 与 Docker 均可在依赖满足且模块可导入时
使用该入口；只有 Docker 命令代理支持附加 `check_id`，native 不接受该字段。
该自检编译 CUDA 扩展、JIT 编译 Triton 加法，用 PyTorch 参考结果验证空输入、
1/33/1025/65537 个元素、float32/float16，以及硬件支持时的 bfloat16；CUDA 扩展示例
验证 float32 和非默认 stream。缺依赖或 GPU 时失败，不能以 CPU fallback 通过。
这只是环境自检，不能代替当前算子的正确性测试。

增加 `--benchmark` 可测量三个实现的 GPU event 耗时；每次分配输出，先 warm-up，
不包含首次编译。结果附带环境信息，不承诺哪个实现更快。

## 编写实际算子

内置技能引导 Agent 明确数学定义、shape/dtype/stride、设备、梯度需求，先提供参考实现，
再实现 kernel，并分别验证正确性、边界条件、stream 与性能。需要 torch.compile 或 autograd 时
应额外实现和验证对应接入，不能仅凭 eager 测试宣称支持。

优先使用执行器设置的 `TMPDIR`、`TRITON_CACHE_DIR`、`TORCH_EXTENSIONS_DIR`；
创建临时文件可用 `tempfile.gettempdir()`。Docker 使用容器 `/tmp`，native 使用每次调用的
私有临时目录，Linux native 的顶层 `/tmp` 本身只读，不应硬编码在那里新建编译缓存。
两种模式的默认临时缓存都会在调用结束后清理，所以将同一候选的编译、正确性检查、预热
和 benchmark 放在同一进程中；需要保留的实验结果写入工作区普通文件。

两种模式命令均断网；Docker 依赖预装到镜像，native 依赖预装到 项目 Python 环境或允许的
系统工具链位置。可选的主进程 Web 搜索/读取不改变命令网络权限。首次编译按
`execution.tool_limits` 设置超时：GPU 模式的命令/Python 上限通常为 900 秒，默认仍为
60/10 秒；standard 命令/Python 上限为 120/30 秒。native 没有 Docker 的外层容器总超时。

CUDA 工程的 `compile_commands.json` / `.clangd` 应使用当前执行环境有效的 Toolkit/include、
GPU 架构与源码路径：native 使用原项目路径，Docker 使用 `/workspace`。
`.cu`、`.cuh` 能被识别或语义查询成功，不意味着 nvcc 编译成功。

要求逐步优化时，固定正确性约束与基准口径，记录候选源码、输入规模、环境、重复采样方式
和性能结果，并保留最佳正确实现。当前 Runtime 可按模型决定连续调用工具，但没有强制
性能提升验收、自动保存最佳候选或专用调参调度器；这些流程由任务/技能与项目测试实现。
`--max-steps 0` 仅取消模型轮数上限，不保证持续运行到性能目标，也不解除工具超时。

## 验证边界

在没有 NVIDIA GPU 的开发机上可验证路由、配置、CLI、Docker 参数、技能加载与缺依赖时的行为。
需要在目标 Linux / WSL2 NVIDIA 环境中执行实际测试，才能确认所用驱动、框架与算子组合。
技能加载、参数校验或模拟测试通过不能替代真实 GPU 验证。

在已准备相应依赖的宿主机执行，真实测试使用显式开关：

```bash
# Docker：需要可用的 CUDA 镜像与 NVIDIA runtime
RUN_CUDA_DOCKER_TESTS=1 .venv/bin/python -m pytest -q tests/test_gpu_support.py

# Native：驱动 kernel 与隔离验证
RUN_NATIVE_GPU_TESTS=1 .venv/bin/python -m pytest -q tests/test_linux_native_gpu.py

# Native：额外验证 PyTorch、Triton 和 CUDA 扩展
RUN_NATIVE_GPU_TESTS=1 RUN_NATIVE_GPU_OPERATORS=1 .venv/bin/python -m pytest -q tests/test_linux_native_gpu.py
```

在源码安装目录运行以上命令；其他安装方式使用对应的 Python 环境。

参考：[PyTorch 官方镜像](https://github.com/orgs/pytorch/packages/container/pytorch/431045026?tag=2.7.1-cuda12.8-cudnn9-devel)、
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/docker-specialized.html)、
[Triton 加法教程](https://github.com/triton-lang/triton/blob/main/python/tutorials/01-vector-add.py)。
