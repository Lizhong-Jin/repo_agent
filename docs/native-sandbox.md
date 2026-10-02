# macOS / Linux 原生沙箱

[文档首页](index.md) · [项目首页](../README.md) · [Docker 沙箱](../sandbox/README.md)

macOS/Linux 首次安装默认使用 `native`（已显式安装其他模式时，以安装记录为准）：文件工具通过受控的轻量文件服务直接修改原项目；Git 工具、命令、Python、环境探测和语言服务器通过 macOS Seatbelt 或 Linux Bubblewrap + seccomp 执行。首次默认的 `repo-agent` 等同于 `repo-agent --sandbox native`：

```bash
repo-agent
repo-agent --sandbox native
repo-agent --sandbox native --root /path/to/project
```

不需要 Docker 或镜像。可信控制进程使用 Agent Python，项目代码使用自动发现的项目 Python（见 [环境规则](python-environments.md)），以及 `.venv` 内的语言服务、Homebrew LLVM 路径、`/opt/homebrew/bin`、`/usr/local/bin` 和系统目录中的工具。运行 `./install.sh --mode native` 会安装 Python 语言服务，按用户安装选项准备额外语言服务；macOS 可通过 Homebrew 补齐缺少的 Node.js、Go、LLVM，Linux 须先用发行版包管理器安装系统工具链，并把 JS/TS、Go 语言服务放入专用 `.venv`。需要时可用 `--languages python` 缩小范围。安装和 `repo-agent doctor --mode native` 会实际查询示例符号；工具执行过程中缺少依赖仍会报错，不临时下载或绕过沙箱。隔离执行工具默认完全断网，包括 localhost，所以依赖下载和需要本地服务的测试不能在沙箱内运行。轻量文件工具只调用受信任的文件操作代码，不启动子进程或发起网络请求。

## 轻量文件工具与隔离执行工具

macOS 和 Linux native 共用以下分层，模型可见的工具参数不变：

| 执行层 | 工具 | 保护方式 |
| --- | --- | --- |
| 轻量文件服务 | `read_file`、`write_file`、`edit_file`、`apply_patch`、`list_files`、`find_files`、`search_files`、`make_directory`、`delete_file`、`move_file`、`get_path_info` | 主进程内的受信任实现；按实际目标检查权限；不启动 worker、不扫描整个工作区或系统依赖树 |
| 操作系统隔离 | `run_command`、`run_python`、Git、LSP、正常状态下的 `get_execution_environment` | 保留原有 Seatbelt / Bubblewrap + seccomp、工作区检查、进程监督与超时清理 |

工具类的 `execution_kind` 是程序内部元数据，不是模型参数。统一调度器区分 `HOST_CONTROL`、`TRUSTED_FILE`、`TRUSTED_NETWORK`、`SANDBOXED_PROCESS`，所有具体工具必须显式声明；缺失或无效声明在注册时被拒绝，不再使用默认分类。只有文件工具工厂明确登记的内置实现能进入 native 轻量层；未注册工具直接返回 `UNKNOWN_TOOL`。Docker 仍在容器工作副本中执行文件和进程工具，local 的工具可用范围不变；可选 Web 工具仍单独注册。完整规则见[工具执行调度](tools.md#工具执行调度)。

轻量文件服务只允许工作区内的文件操作，继续拒绝敏感名称、会话状态目录、宿主配置的保护路径、越界目标和多重硬链接文件。工作区内的解释器和依赖目录仍只读。读取普通文件时检查实际打开的文件类型、硬链接数及前后身份；写入使用目录句柄定位、独占创建临时文件与原子替换。底层读写和目录遍历不跟随检查后新出现的符号链接；允许的文件别名先解析为工作区内目标，递归搜索不遍历目录符号链接。

这是受信任文件代码的应用层边界，**不是任意代码的 OS 沙箱**。不得在文件工具中执行项目模块、插件、命令或语言服务器。目录句柄减少路径替换风险，但不承诺抵御拥有同一用户权限的恶意宿主进程持续移动目录、修改文件或攻击 Agent 本身；多文件 patch 也不是事务。需要独立工作副本及更强隔离时使用 Docker。

启动时仍验证隔离与 GPU：Linux 将通用隔离自检和 CUDA 自检放在同一次沙箱启动中，由两个独立子进程分别执行，保留各自超时和失败诊断。选择单张 GPU 时仍先在沙箱内枚举设备，再收缩授权后执行合并自检。普通文件操作不承担全量扫描开销；Linux 隔离执行仍在每次调用前重新扫描，但现在会在单次策略生成内复用目录别名的扫描结果，并复用不包含文件状态的静态策略模板。批量接口见 [sandbox/policy_scan.py](../sandbox/policy_scan.py)，默认由 [sandbox/linux_policy.py](../sandbox/linux_policy.py) 的 Python 实现执行，可显式选择 Rust 实现，后端计时字段见下方说明。

文本搜索使用目录作用域读取接口：同一目录内复用父目录句柄与枚举元数据，逐文件打开后仍验证身份、类型、硬链接和读取前后签名。达到搜索预算或发生异常时立即关闭目录作用域。作用域固定的是目录身份，不是文件系统原子快照。递归遍历在单次扫描内最多缓存 32 个目录句柄，从最近祖先相对打开；实际写操作不使用该缓存。元数据以最多 128 项的小批次读取，list/find 复用候选信息，Rust 接口保留纳秒时间戳和逐项错误。批量不省略实际打开文件后的检查，也不等同于一次系统调用完成所有 stat。无匹配的大小写敏感查询跳过逐行处理；其余匹配使用惰性行迭代，保持编码、换行、顺序和截断语义。可通过 `scripts/benchmark_search_files.py` 比较逐条目重开目录与目录作用域复用；该脚本仅测试轻量文件服务，不包含完整隔离启动。

### Rust 扫描与文件系统后端

可选 Rust 策略扫描：源码安装自动尝试编译扩展，发行包安装直接使用包内匹配平台的预编译 wheel，无需安装端编译器。确认安装成功后设置 `AGENT_NATIVE_SCANNER=rust` 并重新启动；默认值仍为 `python`。扩展安装失败不影响核心安装。它切换 Linux 隔离执行前的策略扫描和 macOS 工作区硬链接检查，同时切换两个平台 native 文件工具的目录枚举、相对路径遍历和批量元数据后端。Linux 工作区预检继续合并在 Linux 策略扫描中；macOS 隔离仍使用 Seatbelt。两平台 native 文件系统后端需要 兼容 FILESYSTEM_API_VERSION=2 的扩展（当前版本 0.5.0）；glob/路径规则与文本匹配仍由 Python 执行。显式选择 Rust 后，扩展缺失、版本不兼容或扫描失败仍会报错，不在运行时自动切换实现。缺少扩展的旧发行包可另行安装，见 [扩展构建说明](../rust/docs/policy-scan.md)。

## Linux 实现、依赖与权限

Linux 后端无需 Docker、镜像或常驻服务，以当前普通用户运行，不调用 sudo。需要 Python 3.11+、Bubblewrap 0.8+、`libseccomp.so.2`，以及可用的 user/mount/PID/network/IPC/UTS namespace。Debian/Ubuntu 用户先安装系统包，再安装 Agent：

```bash
sudo apt-get install bubblewrap libseccomp2
./install.sh --mode native --skip-toolchains
repo-agent --sandbox native
```

Fedora 的对应包是 `bubblewrap libseccomp`。需要其他语言时，先准备 Node.js/npm、Go 1.25+ 或 clangd，再使用 `repo-agent toolchains install` 安装受管语言服务。安装器不自动提权或修改 Linux 系统包。某些发行版的 AppArmor、sysctl 策略或外层容器会禁止非特权 namespace；启动自检失败会明确报错，不会自动放宽安全策略或退回 local。

`sandbox/native.py` 的 `create_native_backend()` 选择 `sandbox/linux_native.py`；`sandbox/native_common.py` 管理共用工具接口、受信任代码副本和调用生命周期，`host_support` 提供输出收集与进程监督。每次隔离执行调用创建独立 namespace，挂载原工作区及私有临时目录；`sandbox/linux_exec.py` 在执行项目代码前加载 seccomp，禁止 sockets（含 Unix socket）、io_uring、硬链接及重新配置 namespace/mount 等系统调用，子进程继承这些限制。匿名 `socketpair` 保留给 Node/libuv 等进程内部通信，不能用于连接宿主机服务。

| 路径或资源 | Linux native 权限 |
| --- | --- |
| 工作区普通文件、每次调用的私有临时目录 | 可读写，原项目修改立即生效 |
| `/usr`、`/bin`、`/sbin`、`/lib`、`/lib64`、Agent Python、按需加入的项目环境及依赖、工具实现副本 | 只读；工作区内的解释器目录也只读 |
| `/etc` | 仅映射少量加载器/时区文件，不挂载整个目录 |
| 其他项目、个人目录 | 未映射即不可见；白名单工具链路径例外 |
| 已有 `.env`、密钥后缀文件、凭据目录、配置指定的保护路径 | 每次调用前重新扫描，以无访问权限的只读空文件/目录遮蔽 |
| `.git` | 普通命令不可读写，仅 Git 状态/差异工具可只读访问 |
| `/proc`、`/dev` | 私有进程和最小设备视图；设备文件系统及 `/dev/shm` 的临时写入不影响宿主机 |

**Linux 与 macOS 的保护规则存在差异。** Linux 挂载规则不能按名称禁止尚不存在的文件：命令/Python 可以创建 `new.key` 或 `.env`，并在同一次调用内访问它，下一次调用会重新遮蔽。文件工具自身始终拒绝这些名称。不要在工具运行期间由另一个进程向原本不存在的路径写入秘密；当前后端不承诺保护此类并发变化。macOS 使用动态路径规则。

已有硬链接会被拒绝，新建硬链接被 seccomp 禁止；普通符号链接不能扩大可见文件范围。工作区中的受保护路径如果是符号链接，Linux 拒绝该调用；只读工具链中的此类链接通过遮蔽父目录处理。已有 socket、FIFO、设备文件也会让工作区检查失败，避免暴露宿主机通信通道。固定保护路径和项目内解释器目录的祖先会成为挂载点，防止移动祖先后在下次调用绕过保护。

扫描工作区外的只读系统目录时，若遇到无权列出的子目录（例如 WSL 的 `/lib/modules/.../lost+found`），会将整个子目录遮蔽为不可访问的只读空目录后继续。不能只跳过扫描：某些目录虽然不能列出文件名，仍允许读取已知路径。工作区（包括其中的只读工具链）扫描权限不足、其他 I/O 错误或遮蔽挂载失败时仍拒绝执行。安装和诊断输出会保留异常类型及具体路径，无需通过 sudo 或修改系统目录权限绕过。

工作区不能位于只读系统/解释器目录内，也不能覆盖 `/proc`、`/dev`、`/sys` 或后端控制目录。没有映射宿主机 `/run`、Docker socket；GPU 设备在自动检测启用或显式强制 CUDA 时映射，见下节。Linux 额外使用独立 PID namespace，其 init 退出时由内核清理 namespace 内的进程；CPU、内存和进程数仍无配额限制。

Linux 外层监督读取 `/proc/<pid>/stat` 的 PID、父 PID、进程组和启动时钟；按字节解析，进程名含非 UTF-8 字节、换行或括号不会破坏快照。进程已退出与身份不可读分开处理，无法核验时保持清理状态为 `unknown`。

Python 和内核支持时，发送信号使用 `pidfd_open` / `pidfd_send_signal`：先绑定进程句柄，再核验启动时间，避免核验后 PID 被复用而把信号发给另一进程。每次信号操作后关闭句柄。API 缺失或内核返回 `ENOSYS` 时，回退到原有的启动时间核验加 PID 信号路径；`EPERM` / `EACCES` 不当作旧内核处理，记录具体失败阶段及 errno。`cleanup_diagnostics` 的 `via` 区分 `pidfd` 与 `pid`。参考 [Python os 文档](https://docs.python.org/3.11/library/os.html#os.pidfd_open)和 [signal 文档](https://docs.python.org/3.11/library/signal.html#signal.pidfd_send_signal)。

pidfd 改善进程身份与信号发送的可靠性，不负责发现全部后代；Linux native 的后代生命周期还由 PID namespace 和 Bubblewrap 的 `--die-with-parent` 管理。这里不要求创建 cgroup，也不增加系统提权要求。

自检实际验证工作区写入、目录外文件不可见、网络与硬链接被禁止、限制被子进程继承，以及嵌套 user namespace 被禁止。具体挂载列表见 `LinuxNativeBackend._read_paths()`。相关机制参考 [Bubblewrap 官方说明](https://github.com/containers/bubblewrap)。

## Linux / WSL2 原生 GPU

无需 Docker、镜像或 NVIDIA Container Toolkit。Linux / WSL2 native 默认使用 `--sandbox-profile auto`：发现 NVIDIA CUDA 设备后自动开放全部 GPU，并在沙箱内验证 CUDA kernel；没有发现则使用不开放 GPU 的 standard 环境。macOS 保持普通 native，不探测 NVIDIA CUDA。

普通 Linux 以 `/dev/nvidiaN` 为候选标志；WSL2 需同时存在 `/dev/dxg` 和 Windows 提供的 CUDA 驱动库，避免将仅有非 NVIDIA 显卡的 WSL2 误判为 CUDA。已检测到设备但驱动、UVM、权限或 kernel 自检异常时明确报错，不静默降级；可显式选择 standard 关闭 GPU。`--sandbox-gpus` 或 cuda profile 则强制要求 GPU，设备不存在也会报错。

```bash
# Linux / WSL2：默认自动检测，有 NVIDIA CUDA GPU 即启用
repo-agent --sandbox native

# 强制关闭 GPU，不做 GPU 检测或挂载
repo-agent --sandbox native --sandbox-profile standard

# 强制启用全部 GPU；Linux 也可按 nvidia-smi 索引/完整 UUID 选单卡
repo-agent --sandbox native --sandbox-profile cuda
repo-agent --sandbox native --sandbox-profile cuda --sandbox-gpus 0
repo-agent --sandbox native --sandbox-gpus GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# WSL2：在发行版内运行，当前只支持全部 GPU
repo-agent --sandbox native --sandbox-profile cuda --sandbox-gpus all
```

需要宿主机已有可用的 NVIDIA CUDA 驱动。普通 Linux 还需当前用户可访问 `/dev/nvidiactl`、`/dev/nvidia-uvm` 和 GPU 设备节点；UVM 不存在时先由管理员在宿主机加载驱动模块。程序不会运行 sudo、加载内核模块或更改设备权限。单卡选择还需要系统 `nvidia-smi`：在沙箱内查询其索引、设备 minor number 和 UUID，避免把索引误当作 `/dev/nvidiaN` 的编号。

仅逐个挂载获准的 `/dev/nvidiaN`、共享控制节点和 UVM 节点；NVIDIA `/proc/driver/nvidia` 信息只读。WSL2 使用单个 `/dev/dxg` 和已有的 `/usr/lib/wsl/lib` 驱动库，这个共享设备不能按显卡拆分授权，因此拒绝单卡索引/UUID。WSL2 驱动由 Windows 提供，不应在发行版内安装 Linux 显卡驱动，见 [NVIDIA WSL 指南](https://docs.nvidia.com/cuda/wsl-user-guide/)。

启动时先验证原有隔离，再使用 `libcuda.so.1` 创建 CUDA context、分配显存、JIT 编译并执行一个小型 PTX kernel，读取结果核对。任一设备无法计算、驱动库缺失、超时或选择不匹配都会拒绝启动，不以 CPU fallback 通过。此探测使用 Agent Python，不依赖 PyTorch 或 nvcc；PyTorch、Triton、CUDA 扩展需提前在选定的项目 Python 环境中安装，系统驱动和工具链单独准备。

自动识别系统 `/usr/local/cuda` 或 `/opt/cuda`（解析后的目录须位于 `/usr` 或 `/opt`），只读映射工具链并设置 CUDA_HOME/PATH；发行版安装在 `/usr/bin` 的 nvcc 也可通过系统 PATH 使用。不会继承宿主机任意 CUDA_HOME、LD_LIBRARY_PATH 或 CUDA_VISIBLE_DEVICES。CUDA、Triton 和 PyTorch 扩展缓存放在本次调用的私有临时目录，调用结束删除，因此可能重复编译。GPU 模式命令和 Python 的最大超时都为 900 秒，默认仍分别为 60/10 秒，首次编译应显式设置 `timeout_seconds`。

文件保护、禁止联网、独立进程 namespace 和 seccomp 保持生效。`get_execution_environment` 的 execution 部分报告 `gpu_access`（请求的 profile、实际 profile、授权设备、启动探测结果和无显存配额），gpu 部分仍报告框架依赖及实际可用性。GPU 驱动由宿主机共享，native 不提供显存/算力配额、独占访问或恶意 GPU 程序之间的强隔离；单卡设备映射与 CUDA_VISIBLE_DEVICES 不等价于多租户安全边界。MIG、NVSwitch/Fabric Manager、MPS、ROCm/AMD 及分布式网络通信不在当前支持范围；需要这些环境时保持失败并单独适配，不开放整个 `/dev`、`/sys` 或宿主机 socket。

单元测试和普通 Linux namespace 回归不能替代真实 Linux/WSL2 驱动验证。真实硬件测试入口见本文末尾。

## macOS 实现与权限

`sandbox/macos_native.py` 实现 Seatbelt 策略、读取范围和自检，继承 `sandbox/native_common.py` 的公共调用流程，并通过 `sandbox/native.py` 的工厂选择。工具调用接口与 Docker 共用约定，但直接访问原项目。每次隔离执行调用用 `/usr/bin/sandbox-exec` 加载宿主机生成的 Seatbelt 策略。命令和 Python 由外层主进程直接启动沙箱进程、监督子进程并收集输出；Git、环境探测和 LSP 工具通过独立 worker 执行；纯文件工具使用上面的轻量文件服务。模型、密钥、会话和日志由外部主进程管理。

- 允许读取工作区、系统库、当前 Python 安装及依赖、常见系统工具链；目录元数据查询范围较宽，文件内容读取仍受策略控制。
- 仅工作目录和每次调用的私有临时目录可写。解释器及其依赖目录只读，即使位于项目内也不允许安装或修改。
- `.env`、密钥文件、凭据目录、`.codex`、`.agents`、日志及配置指定的受保护路径限制由内核执行；模型通过命令/Python 也不能绕过这些路径规则。
- `.git` 禁止写入；仅 `git_status` / `git_diff` 工具允许读取 Git 元数据。普通命令中的 Git 操作可能被拒绝，提交、切换分支等应由用户在终端执行。
- 拒绝包含已有普通文件硬链接的工作区，并禁止沙箱内创建硬链接。符号链接目标仍必须满足内核路径策略。
- 工具环境不继承 API Key、代理设置、SSH Agent socket 或 Python 启动变量。HOME、缓存及临时目录指向本次调用的私有目录。
- 开始会话时复制受信任的工具实现，worker 通过隔离的 Python 启动方式加载副本；修改 Agent 自身项目不会改变当前会话的工具实现。

启动自检实际验证目录外文件读写被拒绝、网络被拒绝、子进程继承限制、工作目录写入和硬链接限制。自检失败直接报错，不自动切换为 local 或无限制执行。Linux 使用上述独立后端；Windows ZIP 提供 local / Docker 适配，不提供 native。需要本文的原生隔离能力时，Windows 用户应在具备 namespace 能力的 WSL2 中使用 Linux 包。

### 文件访问范围

工作目录由启动目录或 `--root` 决定。文件工具还会执行应用层工作区检查；命令、Python 和语言服务器则受 Seatbelt 的内核规则约束。

| 路径或资源 | 当前权限 |
| --- | --- |
| 工作区内普通文件 | 可读写、创建、删除，保护规则优先 |
| 每次工具调用的私有临时目录 | 可读写，调用后清理，不跨调用保留 |
| 当前 Python 环境及依赖、系统库、受信任工具实现和工具链目录 | 只读，即使 Python 环境位于工作区内也是如此 |
| 其他项目、工作区外的个人文档 | 默认不允许读取内容或写入；白名单目录例外 |
| `.env` / `.env.*`、`.ssh`、`.aws`、私钥后缀文件、`.codex`、`.agents`、`logs` 等 | 禁止读写，名称匹配不区分大小写 |
| 会话状态目录、`AGENT_ENV_FILE` / `AGENT_LOG_DIR` 指定的路径 | 禁止读写 |
| `.git` | 禁止写；仅 `git_status` / `git_diff` 工具可读 |

系统读取白名单还包括 `/usr`、`/bin`、`/sbin`、`/opt/homebrew`、部分 `/Library` 和 `/System` 子目录、`/private/etc` 及必要的系统数据库目录；具体列表见 `MacOSNativeBackend._read_paths()`。这意味着隔离规则不是“只能读取项目”，也不保证隐藏白名单外的文件元数据。当前没有按命令临时追加任意目录权限的 CLI 开关。

保护规则按路径和文件名执行，不扫描内容判断是否含密钥。普通源码里硬编码的密钥仍可能被读到。符号链接不能扩大目标权限；已有普通文件硬链接会让工作区检查失败，新建硬链接也被禁止。

## 每次调用的工作区检查

完整工作区检查仍在启动和每次隔离执行工具调用前执行，不按 `.gitignore`、`.venv` 或日志目录剪枝，也不跨调用缓存。Linux 将硬链接、特殊文件检查与保护路径策略生成合并到同一次工作区遍历；即使目录已被整体遮蔽，也继续检查其中的硬链接和特殊文件。每次实际启动沙箱（包括自检）都执行新的合并扫描，不跨调用复用文件状态；同一次扫描内，只读目录的多个符号链接视图可复用目录枚举与名称分类结果，并分别生成各挂载位置的保护规则。轻量文件工具只检查实际访问的目标；无关文件中的硬链接或特殊文件不会阻止读取普通源码，但访问这些不安全目标仍被拒绝。

### 后端计时字段

后端提供以下内部属性，供开发者区分初始化、策略准备和进程运行成本；它们不附加到模型的工具输出，不记录文件内容或命令参数。

| 属性 | 内容 |
| --- | --- |
| `startup_metrics` | 初始化总耗时、可信代码复制、工作区检查与启动自检 |
| `last_tool_metrics` | 最近一次工具调用总耗时与沙箱运行列表；轻量文件工具的运行列表为空 |
| `last_run_metrics` | 请求和策略准备、进程执行及监督清理、总耗时 |
| `last_policy_metrics`（Linux） | 目录枚举、名称分类、工作区验证、路径映射与挂载参数生成的耗时及数量 |

Linux 的工作区检查合并在策略扫描中，独立的 `workspace_check_ms` 为零；`policy.workspace_validation_ms` 不含共享目录枚举成本，不应再次加到总时间中。`complete` 仅表示相应阶段正常返回，不表示命令退出码为零。策略准备不受进程执行超时控制，不能只用子进程耗时代表完整工具调用。

## 文件、会话与限制

修改立即生效，没有 Docker 工作副本、`/apply`、自动回写或回写备份。任务出错、中断、关闭会话都不会撤销已写入文件。会话历史可照常保存、改名和恢复；切换 local/native/docker 时会提示核实当前文件状态。

旧配置中的 `AGENT_SANDBOX_WRITEBACK=on-success` 只在 Docker 模式生效，不影响默认 native 或显式 local 启动。显式对 local/native 传 `--sandbox-writeback on-success` 会报错。镜像和回写前验证不适用于 native；CUDA profile 在 Linux native 中仅授权 GPU 并提高工具超时上限，不应用 Docker 资源配额。

native 为隔离执行工具提供平台内核级的文件/网络隔离，不提供 Docker 等价的 CPU、内存、进程数配额或环境可复现性。主进程通过 PID、内核启动时间及父子关系跟踪本次调用的进程；超时先发 TERM，再对仍存活的进程发 KILL，回收直接子进程并核验。已经观察到的子进程即使改变进程组或会话也继续跟踪；僵尸进程不当作仍在执行的进程。信号操作失败会记录阶段、PID、信号和 errno，最终是否清理成功以核验结果为准。

进程快照仍是尽力监督，不等价于 cgroup：在两次采样之间创建并迅速脱离父进程的后代可能漏检，仍受继承的沙箱策略约束。`cleanup_status=confirmed` 表示已跟踪的进程不再运行且输出管道已关闭，不代表对任意后台进程的绝对保证。

### 超时结果与恢复

- 超时仍返回已收集的 stdout/stderr，每路保留最多 32 KiB 原始字节的首尾内容，截断时附加标记。`status=timed_out`、`timed_out=true`、`output_complete=false` 提醒模型这是部分结果。尚未从程序内部缓冲区刷新到管道的内容无法取回。
- 输出活动不延长总超时；清理和最后的输出收集通常最多额外耗时约 3 秒。超时前写入的项目文件仍然保留。
- `cleanup_status` 与执行状态分开：`confirmed` 表示清理已核验，`unknown` 表示无法确认，`cleanup_error` 和 `cleanup_diagnostics` 给出原因。正常超时且清理已确认时，可以继续使用工具。取消操作也会先清理，再向上传播。
- 清理无法确认时暂停后续命令、Python、语言服务及文件写入，返回 `NATIVE_UNHEALTHY`。`read_file`、`list_files`、`find_files`、`search_files`、`get_path_info` 仍通过轻量文件服务的路径检查执行；`get_execution_environment` 返回无需启动探测程序的环境信息及清理诊断。读取成功不会自动解除该状态；检查遗留进程后使用 `--new-session` 重启。
- worker 自身异常、超时或返回无效协议时，也保留有限的 stdout/stderr，并标记 `output_kind=worker_protocol`；其中可能是未完成的工具协议文本。

Apple 将 `sandbox-exec` 标记为弃用；不同 macOS 版本和外层沙箱可能不支持它。启动自检是必要条件，并非完整的安全审计。需要严格的资源限额、可控依赖或工作副本回写时，继续选择 Docker。

## 主进程 Web 工具

已实现可选的 `web_search` 和 `web_fetch`，由 CLI 在主进程注册，local/native/Docker 均可使用。
搜索通过 `AGENT_WEB_SEARCH_PROVIDER=brave` 和 `BRAVE_SEARCH_API_KEY` 启用；网页读取
通过 `AGENT_WEB_FETCH_ENABLED=true` 独立启用，不需要搜索密钥。默认均关闭。

网络请求由沙箱外的受控后端执行；native 命令、Python 和语言服务器继续断网，Web 请求失败
不会解除隔离或改变 native 健康状态。搜索不自动抓取结果网页，抓取也不会开放包安装或
项目网络访问。抓取限制公开 HTTP(S) 地址，校验 DNS、实际对端及每次重定向。
网页内容不能授权本地命令、读取或上传文件；搜索词和 URL 会外发，应遵守用户任务范围。

参数、限制与测试见 [Web 搜索](web-search.md) 和 [Web 页面读取](web-fetch.md)。

## 验证

以下命令在已安装开发依赖的源码目录运行，见[开发环境](development.md#开发环境与验证)。常规测试验证平台分派、缺失依赖时拒绝执行、挂载策略、CLI 默认值、配置兼容和会话模式。真实 macOS 测试：

```bash
RUN_SANDBOX_NATIVE_TESTS=1 .venv/bin/python -m pytest tests/test_native_sandbox.py tests/test_project_python.py tests/test_git_execution_policy.py tests/test_apply_patch.py -q
```

测试在临时目录验证直接写入、脚本执行、凭据保护、目录越界、符号链接、硬链接、网络限制、子进程继承、Git 查询、超时输出保留、脱离会话的子进程清理及故障后的只读诊断，不调用真实模型。

真实 Linux 测试（建议以普通用户执行）：

```bash
RUN_SANDBOX_LINUX_TESTS=1 .venv/bin/python -m pytest tests/test_linux_native.py tests/test_linux_process_supervisor.py tests/test_process_supervisor.py tests/test_process_runner.py tests/test_run_command.py tests/test_git_execution_policy.py tests/test_apply_patch.py -q
```

Linux 测试检查文件与网络隔离、符号链接、已有硬链接、敏感路径每次调用重新遮蔽、保护目录祖先禁止移动、只读 Git、Python 语言服务（已安装时）、脱离会话且忽略 TERM 的子进程清理、正常退出后的双重 fork 后台进程清理、持续输出下的总超时与双流截断、取消后的恢复、非 UTF-8 进程名，以及默认 CLI 会话保存。无需 Linux 的单元测试还覆盖 PID 复用、pidfd 句柄关闭、旧内核回退及权限失败。

Linux 在 macOS 上可借助临时 Linux 虚拟机或容器进行验证；嵌套测试环境需要允许 user/PID/mount namespace，外层容器的 `/proc` 遮蔽也可能导致 `Can't mount proc`。应调整独立测试环境，不放宽产品的 Bubblewrap/seccomp 策略。这不改变正常 Linux native 的权限要求；测试以普通用户执行，GPU 验证另行启用。

真实 NVIDIA Linux / WSL2（缺少硬件或 CUDA 不可用时失败，不跳过或回退 CPU）：

```bash
RUN_NATIVE_GPU_TESTS=1 .venv/bin/python -m pytest tests/test_linux_native_gpu.py -q
# Linux 单卡验证；WSL2 仅支持 all
RUN_NATIVE_GPU_TESTS=1 NATIVE_TEST_GPUS=0 .venv/bin/python -m pytest tests/test_linux_native_gpu.py -q
# 同时验证 CUDA 扩展、PyTorch 和 Triton；需预装这些依赖及 Toolkit
RUN_NATIVE_GPU_TESTS=1 RUN_NATIVE_GPU_OPERATORS=1 .venv/bin/python -m pytest tests/test_linux_native_gpu.py -q
```

硬件测试验证默认 auto 启用 GPU、实际 kernel、子进程 GPU 访问、GPU 启用时仍禁止目录越界/凭据读取/网络，以及显式 standard 不暴露 GPU。可选算子测试复用 `sandbox.operator_smoke` 的 CUDA 扩展和 Triton 计算正确性检查。

### WSL 驱动存储与元数据开销

Linux native 会识别 `/usr/lib/wsl/drivers` 上来源为 `drivers` 的 WSL `9p` 挂载。
`standard` 在沙箱内用空的只读目录覆盖该存储及 `/lib` 等挂载别名，策略生成不再递归扫描它。
`auto` 检测到 WSL CUDA 或显式 `cuda` 时，先在隔离进程中通过 `libdxcore` 查询当前适配器的
驱动包位置；查询阶段驱动存储仍隐藏，只允许已授权的 `/dev/dxg` 设备。然后只读恢复所需的
NVIDIA 驱动包，继续执行既有的隔离与 CUDA kernel 自检。适配器查询方式参考
[NVIDIA libnvidia-container](https://github.com/NVIDIA/libnvidia-container/blob/main/src/dxcore.c)。

恢复的是驱动包目录，便于兼容驱动动态加载的附属库；不会恢复整个 Windows 驱动存储。
这些包仍适用敏感文件遮蔽规则，每次执行都重新扫描。查询失败、返回越界路径、包变为符号链接，
或驱动挂载/目录身份变化时拒绝执行，不自动暴露所有驱动或退回 CPU。Windows 更新驱动或更换
适配器后应重启会话。没有识别到上述 WSL 挂载的系统仍使用普通 Linux 策略。

普通 Linux 也受目录规模、挂载别名和文件系统元数据延迟影响。常见的本地 ext4/xfs 通常没有
WSL 驱动 9p 挂载的跨系统访问成本；网络盘、FUSE 或缓慢存储仍可能产生类似延迟。
首次访问目录时使用 `O_DIRECTORY | O_NOFOLLOW` 打开并按文件描述符枚举，合并目录类型检查
与打开操作；复用别名枚举结果时仍重新检查目录类型。每次扫描还重新确认根目录身份与挂载表。
这些检查不是文件系统快照，也不构成对任意并发文件替换的完整防护。

`last_policy_metrics` 新增：

- `metadata_ms`：目录打开及别名复用前类型检查耗时；根路径解析、末尾核验等仍计入 `mapping_ms`。
- `directory_opens`、`metadata_checks`：成功打开目录及别名类型检查次数。
- `by_root_mount`：按暴露根路径与实际挂载点分组的枚举、分类、元数据耗时及次数，含文件系统类型。
- WSL CUDA 的 `startup_metrics.checks.driver_discovery_ms`：额外驱动发现沙箱的完整耗时；
  `startup_metrics.runs` 同时保留发现阶段与隔离/CUDA 自检阶段各自的策略和进程计时。

以下命令在 WSL 或真实 Linux 中输出完整启动与空命令耗时，可对比 standard 和 auto：

```bash
.venv/bin/python scripts/benchmark_linux_policy.py --workspace "$PWD" --end-to-end --sandbox-profile standard --repeats 5
.venv/bin/python scripts/benchmark_linux_policy.py --workspace "$PWD" --end-to-end --sandbox-profile auto --repeats 5
```

检查输出中的 `end_to_end`：包含 `wsl_driver_packages`、启动明细、空命令中位数/p95 和每次
调用的 `by_root_mount`。脚本顶层 `before/after` 是同等扫描范围下的算法对照，不启用 WSL
驱动挂载收窄；评估 WSL 实际收益应使用 `end_to_end`。真实 GPU 验证命令见上文，硬件测试
同时检查 CUDA 可运行、GPU 模式只暴露已选驱动包、standard 下驱动存储为空。

Rust 0.5+ 预检和隔离策略扫描默认最多 2 个目录任务并行，可用 `AGENT_SCAN_WORKERS=1` 关闭，或设为 2～8。仅有一条目录链时不启动线程池。资源边界、顺序和取消语义见 [有限并行扫描](../rust/docs/scan-parallel.md)。
