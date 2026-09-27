# 平台适配边界

[文档首页](index.md) · [项目架构](../Project_Architecture.md) · [开发指南](development.md)

平台相关公共能力集中在 `host_support`。macOS/Linux 保持原有目录布局、安装、会话与沙箱行为。Windows 已接入共享文件访问、文件锁、原子存储与 Docker 快照/回写；新增自带 Python 的 Windows x86_64 ZIP 与 PowerShell 安装/卸载入口；仍不提供 Windows 原生沙箱。Windows 内核和 Docker Desktop 的实际验收须在对应环境执行，不能从 macOS/Linux 的通过结果推断。

| 平台 | 发行包与入口 | 首次安装默认模式 | 当前执行能力 |
| --- | --- | --- | --- |
| macOS ARM64/x86_64 | tar.gz，`install-release.sh` | native | Seatbelt、local、Docker |
| Linux ARM64/x86_64（含 WSL2） | tar.gz，`install-release.sh` | native | Bubblewrap/seccomp、local、Docker；GPU 另验收 |
| Windows x86_64 | ZIP，`install_release.ps1` | local | 文件/Git、Docker Desktop Linux 容器适配；无 native |

Windows ARM64 未列为发行目标；WSL2 属于 Linux 执行环境。表中列出代码提供的能力，各平台仍须通过相应实机验收。

## 职责与依赖

| 模块 | 负责 | 调用方继续负责 |
| --- | --- | --- |
| `platforms` | 操作系统/架构规范化、显式发行目标、wheel 标签与归档后缀 | 指定构建宿主、发行目标或执行环境，不能混用 |
| `paths` | XDG 用户目录、安装命令与 Python 环境布局 | 配置优先级、资源归属、创建/删除时机 |
| `python_environments` | 按原优先级选择项目解释器，不执行项目代码 | `sandbox/project_python.py` 决定允许读取的目录并拒绝过宽授权 |
| `filesystem` | 无链接跟随的描述符操作、目录遍历和文件服务机制 | 工具路径策略、文件类型/大小检查、回写冲突与备份 |
| `windows_install` | Windows 用户命令 `.exe` 及归属记录、HKCU PATH 比较后更新/恢复 | 与共用安装事务和卸载归属检查配合，不修改系统 PATH |
| `windows_files` | Windows 句柄相对操作、重解析点防护、目录枚举、重命名与锁 | Windows 文件名限制、平台验收；不能降级为先检查路径再按绝对路径写入 |
| `locking`、`storage` | 文件描述符锁、同目录暂存与原子替换 | 锁持有期限、序列化、刷新要求、多文件事务和恢复 |
| `processes`、`supervision` | 启动、管道读取、进程组与后代清理、结果信息 | 执行授权、环境变量、LSP 协议、安装重试 |
| `languages` | 工具链与 LSP 声明、可信 PATH、沙箱基础环境 | 依赖下载与安装事务；Linux GPU 扩展环境 |
| `integration` | 命令链接状态、shell PATH 修改计划 | 用户确认、修改记录、回滚和卸载归属 |
| `execution` | 用于 CLI 参数检查和展示的后端能力 | 可信适配器注册、实际隔离与运行时健康状态 |
| `diagnostics`、`probes` | 结构化诊断与平台依赖可用性检查 | 展示、修复流程及后端的实际隔离自检 |
| `archives` | 归档路径采用统一的 POSIX 表示并拒绝跨平台歧义 | 清单版本、哈希、解压大小和必要文件检查 |

公共模块不反向导入 `cli`、`agent`、`tools` 或 `sandbox`。运行时服务只依赖标准库；`ReleaseTarget.wheel_platforms()` 中的 `packaging` 是延迟导入的构建依赖，安装引导不调用它。

不要将所有文件访问统一为同一种授权策略。local 文件工具、native 可信文件服务、Docker 工作副本的操作位置与保证不同。`tools/_internal/file_access.py` 为底层文件服务注入 `PathPolicy`，保护文件名和工作区规则仍属于工具层。新增平台必须实现所需的文件保护；不支持安全描述符操作时明确报错，不删除无链接跟随检查来继续执行。

原子替换针对单个文件。配置备份、会话索引锁、安装回滚和 Docker 回写记录继续各自管理，多文件写入不因此变成事务。锁文件关闭后保留原 inode，避免删除锁文件导致并发锁分裂。

## 原生后端

`sandbox/native_common.py` 管理可信代码副本、单次调用、工具路由、项目解释器探测、健康状态与临时目录。`macos_native.py` 管理 Seatbelt 策略、读取范围和自检；`linux_native.py` 管理 Bubblewrap、挂载、seccomp 和 GPU。两者分别继承公共基类。

CLI 使用 `create_native_backend()`。旧 `NativeBackend` 构造入口及 `seatbelt_profile` 导入继续兼容；新增实现直接继承 `NativeBackendBase`。`sandbox/backend.py` 的执行协议不包含 Docker 专有的快照和回写接口。

能力描述仅供选择和展示，不能作为执行授权。`ToolDispatcher` 继续识别可信适配器，后端继续进行真实自检；清理未确认时仍进入不健康状态，不回退到未隔离执行。

进程服务也不构成沙箱。一次性命令保留有界输出，LSP 保留持续双向通信，安装下载保留联网和重试；三者共享生命周期机制，但不共享一份环境变量策略。

## 构建与引导

`build_manifest.py` 将 `host_support` 纳入 wheel、sdist、Docker 上下文和发行引导包。native 的可信 worker 副本也包含它。直接执行安装/诊断脚本时，`cli/_bootstrap.py` 从脚本位置加入可信安装根目录，不从任务目录加载公共模块。

无 Python 时的 Shell 引导仍独立运行，继续读取 `runtime/python.lock`。不要为了统一 Python 代码，让准备 Python 的步骤反过来依赖 Python。

源码的 `install.sh` / `uninstall.sh` 与发行入口 `install-release.sh` 都委托给 `scripts/installer-entry.sh`，统一选择引导解释器。发行 schema 2 仅保留后一个顶层入口，并通过 `--uninstall` 提供备用卸载；Windows schema 3 ZIP 仅保留 `install_release.ps1`，使用同一套 Python 安装/卸载事务和用户布局，首次默认 local；具体操作见[安装与卸载](installation.md#卸载)。

开发安装新增顶层包后，需要在已准备依赖的环境中刷新 editable 登记：

```bash
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
```

增加模块后执行 `python3 build_manifest.py --write`。`tests/test_host_support.py` 在复制的引导包中使用 `-I -S` 验证标准库启动；构建测试检查实际 wheel/sdist/Docker 上下文包含公共模块。

## 扩展与验证

增加平台时依次实现布局、锁与文件保护、进程清理、系统集成和具体后端，再加入发行目标。文件服务可用不代表原生进程隔离或安装发布已经完成。

默认回归覆盖各平台策略、文件防护、并发锁、取消、会话与安装恢复；`tests/test_host_support.py` 另覆盖公共层的导入边界、安装/沙箱 PATH 一致性、原子写失败和归档路径。macOS Seatbelt、Linux namespace、Docker 与 GPU 的真实验收仍使用[开发指南](development.md#开发环境与验证)中的独立开关，模拟平台测试不能替代实际内核验证。

## Windows 文件服务

目标为 Windows 10 1809+ / Windows 11 x86_64、Python 3.11+，优先在本地 NTFS 项目和用户私有状态目录验收。API 或文件系统不能提供所需保证时直接报错，不回退到不安全的路径操作。

- `NtCreateFile` 逐个打开路径组件，使用 `FILE_OPEN_REPARSE_POINT`，拒绝符号链接、junction 和其他重解析点；内容访问拒绝多硬链接文件。目录枚举、删除与重命名基于已打开的句柄，防止检查后的路径替换重定向操作。
- Windows local 文件工具自动进入共享 `FileAccess`；macOS/Linux local 的原有执行路径不变。原生后端与 Windows 文件服务不是同一项能力。
- 锁使用同步 `LockFileEx`，支持阻塞及非阻塞竞争，关闭描述符释放锁，不修改空锁文件内容。`storage` 在同一目录暂存，刷新后按目录句柄原子替换，失败清理暂存文件。
- Windows 不提供 POSIX 的用户/组权限位和可执行位。新文件继承目录 ACL；`set_file_mode` 只映射只读属性，不声称 `0600` 等同于私有 ACL。用户应将状态目录保留在自己的用户目录下。Docker 的 Windows 宿主机指纹忽略可执行位，按字节保留 CRLF/LF；POSIX 指纹与权限处理不变。
- Docker 快照使用 Git for Windows，明确关闭基线创建过程中的自动换行转换，不继承用户 Git 配置。回写/恢复仍先做冲突检查、保存备份与意图，再逐文件应用；多文件修改不是整体原子事务。
- 回写前拒绝大小写冲突、备用数据流、设备名、尾随空格/句点等歧义名称。短文件名别名不作为文件访问入口。UNC 路径遵循同样的句柄检查，但网络共享、云盘重解析点、跨系统移动的会话尚不在验收范围。

共享契约测试（含真实跨进程锁、会话/SQLite/诊断存储、快照与备份恢复）：

```powershell
python -m pytest -q tests/test_host_file_contracts.py tests/test_windows_files.py
```

`.github/workflows/host-files.yml` 配置了 Windows/macOS/Linux × Python 3.11/3.13 的六组契约测试，运行 `test_host_file_contracts.py`、`test_windows_files.py`、`test_windows_release.py`。另有 Windows Python 3.13 作业构建 ZIP、设置归档变量后执行真实安装生命周期，并上传测试产物。Windows 文件测试包含 junction 场景、只读文件原子替换和全部 local 文件工具；非 Windows 环境跳过 Windows 内核用例。

工作流配置不等于运行成功。当前 CI 没有运行全量默认套件、Ruff 或真实 Docker Desktop 回写测试；Windows Docker 回写需下面的独立开关与镜像。发布前应检查对应提交的实际作业结果，而非将本地模拟测试或上传配置当作实机证据。

已安装 Git for Windows、已启动 Docker Desktop 的 Linux 容器模式且本地已有 `repo-agent-sandbox:v1` 时，可单独运行真实容器往返测试：

```powershell
$env:RUN_WINDOWS_DOCKER_TESTS = "1"
python -m pytest -q tests/test_windows_files.py -k real_windows_docker
```

该 Docker 测试不会测试 Windows 安装器、GPU 或完整进程树清理。ZIP 的 Windows 安装验收见[构建与分发](distribution.md#校验失败处理与安装验收)，跨平台契约测试通过不代表 Windows 实机验收完成。底层实现依据 Microsoft 的 [NtCreateFile](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile)、[文件重命名](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information)、[文件删除](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntddk/ns-ntddk-_file_disposition_information_ex) 和 [LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex) 接口契约。
