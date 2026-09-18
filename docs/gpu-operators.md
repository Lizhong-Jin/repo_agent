# CUDA、Triton 与 PyTorch 算子开发

此功能包括 CUDA 文件符号查询、GPU 沙箱运行配置、GPU 开发镜像、环境探测、
算子环境自检，以及内置 `$gpu-kernel-development` 技能。Triton/PyTorch 是 Python 库，
其 `.py` 文件继续使用 pylsp；`.cu`、`.cuh` 使用 clangd 的 `cuda` 语言标识。

## 在 Linux NVIDIA 机器准备

当前 GPU 镜像基线面向 Linux x86_64，使用官方
`pytorch/pytorch:2.7.1-cuda12.8-cudnn9-devel`：PyTorch 2.7.1、CUDA Toolkit 12.8、
cuDNN 9，配套固定 Triton 3.3.1。包含 nvcc、C/C++ 编译器、Ninja、pytest 和语言服务器。
升级时应一起检查 PyTorch、Triton、Toolkit、驱动及 GPU 架构兼容性。

宿主机需要 NVIDIA GPU、兼容驱动、Docker 和 NVIDIA Container Toolkit。
镜像包含 CUDA 用户态工具链，不包含宿主机内核驱动；GPU 通过 Docker 的 `--gpus` 挂载。
Mac 的 MPS 或 Linux CPU 模式不能替代 CUDA/Triton GPU 验证。

## 统一构建与启动

普通开发和 GPU 算子开发都只需以下入口，macOS 和 Linux 使用相同命令：

```bash
# 首次使用或更新沙箱代码后构建，不需要模型配置
./run_agent.sh --build-sandbox

# 完成项目模型配置后启动任意任务
./run_agent.sh "任务内容"
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
  更新镜像后启动新会话；已有会话继续使用其固定的镜像。

高级覆盖选项仅用于特殊需求，日常使用无需指定：

- 构建入口接受 `--profile standard|cuda`，可手动覆盖检测结果；强制 cuda 构建仍要求 x86_64，
  但不要求构建时有 GPU。任务入口接受 `--sandbox-profile standard|cuda`。
- `--sandbox-gpus` 支持 `all`、单个设备索引（如 `0`）或 `GPU-...` UUID；CUDA 环境省略时默认 `all`。
- 构建的 `--image` 与任务的 `--sandbox-image` 可选择同一个自定义镜像名称。
  显式指定的无环境标签自定义镜像允许使用，由用户负责依赖完整性；带标签的镜像仍会校验。
- standard 环境不挂载 GPU；local 模式不使用 Docker 自动检测，也不支持 GPU profile。
- Profile、GPU 与资源配置由启动配置和宿主机检测决定，模型工具参数不能修改。

资源上限对比：

| 资源 | standard | cuda |
| --- | --- | --- |
| 容器内存 / CPU | 512 MiB / 1 | 8 GiB / 4 |
| PID 上限 | 64 | 256 |
| `/tmp` / 共享内存 | 64 MiB / 64 MiB | 2 GiB / 1 GiB |
| 单文件大小限制 | 64 MiB | 1 GiB |
| run_command 最大超时 | 120 秒 | 900 秒 |
| run_python 最大超时 | 30 秒 | 900 秒 |
| 容器总超时 | 130 秒 | 930 秒 |

默认工具超时仍为原来的短超时；首次 CUDA 扩展编译显式设置 `timeout_seconds=600` 或 900。
最终回写验证使用 profile 的命令超时上限。GPU 显存不由容器内存上限约束；测试需按目标卡容量选尺寸。
禁网、非 root、只读根文件系统和原有 workspace 回写流程继续生效，不使用 privileged 模式。

## 探测和实际自检

模型可以使用 `run_command` 执行：

```json
{"command":["python","-I","-m","sandbox.compute_probe"],"timeout_seconds":60}
```

探测报告包含 torch/Triton 版本、torch CUDA 版本、GPU 名称与计算能力、显存和 nvcc。
普通探测退出 0 只代表信息收集完成，不能认定 GPU 可用；加 `--require-gpu` 时缺依赖或 GPU 会失败。

真实算子环境自检：

```json
{"command":["python","-I","-m","sandbox.operator_smoke"],"timeout_seconds":900,"check_id":"gpu-environment-smoke"}
```

它编译 CUDA 扩展、JIT 编译 Triton 加法，使用 PyTorch 参考结果验证：
空输入、1/33/1025/65537 个元素、float32/float16，以及硬件支持时的 bfloat16；
CUDA 扩展示例验证 float32，并覆盖非默认 stream。缺少 GPU 明确失败，不能以 CPU fallback 通过。
这只是环境自检，不代表任何新算子已经正确。

增加 `--benchmark` 可测量三个实现的 GPU event 耗时；每种实现每次分配输出，先 warm-up，
不包含首次编译。结果包含环境信息，不承诺哪个实现更快。

在宿主机运行完整沙箱链路验证：

```bash
RUN_CUDA_DOCKER_TESTS=1 .venv/bin/pytest -q tests/test_gpu_support.py
```

## 编写实际算子

内置技能引导 Agent 明确数学定义、shape/dtype/stride、设备、梯度需求，先提供参考实现，
再实现 kernel，并分别验证正确性、边界条件、stream 与性能。需要 torch.compile 或 autograd 时
应额外实现和验证对应接入，不能仅凭 eager 测试宣称支持。

编译与 JIT 缓存写入 `/tmp`，相关环境变量会传到命令子进程。每次工具调用仍启动独立容器，
临时缓存不会跨调用保留，因此尽量在同一次命令中编译、测试和测量。
运行期禁网；额外 Python 包与项目库需在派生镜像构建阶段预装。

CUDA 复杂工程应提供 `compile_commands.json` 或 `.clangd`，包含正确 Toolkit/include、GPU 架构
与容器路径；文件类型被识别或符号查询成功不能替代 nvcc 编译。头文件 `.cuh` 默认按 CUDA 处理。

## 验证边界

在没有 NVIDIA GPU 的开发机上可验证路由、配置、CLI、Docker 参数、技能加载与缺依赖时的行为。
只有目标 Linux GPU 机器上通过实际算子自检后，才能确认该驱动/硬件组合可运行 CUDA/Triton 算子。

参考：[PyTorch 官方镜像](https://github.com/orgs/pytorch/packages/container/pytorch/431045026?tag=2.7.1-cuda12.8-cudnn9-devel)、
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/docker-specialized.html)、
[Triton 加法教程](https://github.com/triton-lang/triton/blob/main/python/tutorials/01-vector-add.py)。
