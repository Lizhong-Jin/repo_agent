# Windows Docker 测试与实机验收

[平台边界](platform-adaptation.md) · [Docker 与回写](../sandbox/README.md) · [开发指南](development.md)

目标是 Windows x86_64 原生 Python + Docker Desktop 的 Linux 容器模式。WSL 中运行 Linux
版 Agent 得到的是 Linux 证据，不能替代 Windows 文件共享、句柄、控制台和发行安装验收。
所有测试使用临时示例项目，不调用在线模型、不读取任务项目、不更改默认模式。

## 自动验收入口

源码检出需要已准备的 `.venv`（含 `requirements-dev.lock` 中的测试依赖）、Git for Windows，
以及运行中的 Docker Desktop Linux daemon。在普通用户的 PowerShell 中，从仓库根目录执行：

```powershell
.\scripts\verify-windows-docker.ps1 -Build
```

`-Build` 构建当前源码的 standard 镜像；构建需要网络。已有当前版本的镜像可以省略。
可用 `-Python` 指定解释器、`-Image` 指定镜像、`-Report` 指定报告路径；报告默认写入
`artifacts/windows-docker.json` 和 `artifacts/windows-docker.tui.json`。
脚本先执行宿主契约测试，再强制运行真实容器验收及真实容器 TUI 测试。
Docker/镜像缺失、挂载失败、能力缺失均返回非零，不跳过或回退 local/native。

完整容器验收入口也随 Python 包分发，安装版无需 pytest 即可运行：

```powershell
# 使用安装目录中运行 Agent 的 Python；不要把项目 Python 当作 Agent Python。
<Agent-Python> -X utf8 -m sandbox.docker_acceptance --report windows-docker.json
```

`<Agent-Python>` 是需替换的解释器路径。此入口不覆盖安装生命周期和 TUI；后者仍需源码测试。
修改工具代码后须重建镜像，避免宿主与镜像协议不一致。

## 覆盖与证据

| 检查 | 自动执行的断言 | 必须在哪里运行 |
| --- | --- | --- |
| 工具目录 | 实际调用当前全部 25 个沙箱工具；漏掉新工具会失败 | 真实 Docker |
| 文件工具 | 读、写、精确编辑、补丁、列目录、查文件、搜文本、路径信息、建目录、移动、删除 | Windows 实机确认共享文件行为 |
| Git | status/diff/log/show，副本基线内容 | 真实 Docker + 宿主 Git |
| LSP | Python、JS/JSX、TS/TSX、Go、C/C++ 的实际符号；Python 定义、引用、悬停、诊断、工作区符号 | 真实 standard 镜像 |
| 执行工具链 | Python 断言、Bash、Node 执行、C/C++/Go 在工作副本内编译并运行，退出时清理产物；核验 `/tmp` 仍为 noexec | 真实 standard 镜像 |
| 隔离 | 非 root、禁网、宿主项目与 `.env` 不可见、Docker socket 不可见、镜像只读 | 每个目标 daemon/平台 |
| 路径 | 中文、空格、逗号位于实际副本挂载路径；验收自身 TEMP 短路径先规范化，底层文件服务仍拒绝短别名 | Windows Docker Desktop 实机 |
| 回写恢复 | 新增/修改/删除、写回前宿主不变、宿主冲突拒绝且无部分写入、恢复副本后回写、备份恢复拒绝覆盖后续编辑、CRLF/LF 字节往返 | Windows 本地 NTFS 实机 |
| 工具内超时 | 启动忽略 TERM、脱离进程组的子进程，检查超时、容器消失、心跳停止、后续调用可用 | 每个目标 daemon/平台 |
| 容器外超时 | 缩短宿主 Docker 客户端总时限，检查相同清理不变量 | 每个目标 daemon/平台 |
| Windows 客户端进程树 | 原子加入 Job；主进程退出、超时、重复取消后，脱离控制台的子进程停止写入；新命令可运行 | `test_windows_job.py` 的 Windows 内核测试，无需 Docker |
| 主动取消 | 工作负载 READY 后取消；拒绝自动回写；独立查询 daemon 确认本次容器全部消失 | 每个目标 daemon/平台 |
| TUI Ctrl+C | 离线假模型驱动真实 Docker；输入两次 Ctrl+C；验证任务取消、队列暂停、清理 confirmed 及 `/queue resume` 后完成新任务 | Windows 实机仍须另验真实按键 |

“完整工具链”指镜像声明的 standard 工具和上述基础行为，不代表每种语言的所有 LSP 方法、
所有第三方依赖或 GPU 都已测试。GPU/CUDA、项目依赖、Windows 专属程序均不在此入口范围。

报告包含宿主平台与 Python 版本、Docker 客户端/服务端版本、daemon 架构、实际镜像 ID、容器用户、分阶段结果、工具名单、取消清理
记录及失败后保留的目录。容器清理查询失败不会被当成容器不存在；只检查本次记录的容器，
不停止其他会话或全局清理 Docker。失败后先检查报告和保留目录；确认无运行中容器再手动删除。

`success=true` 仅表示该报告范围内的断言通过。`manual_ctrl_c=not_run` 明确表示没做真实按键；
取消汇总必须为 `cleanup_status=confirmed`，TUI 报告必须为 `tui_recovery=resumed`。
即使容器已删除，只要宿主清理为 `unknown`，正常场景验收就失败；拒绝恢复分支由故障注入测试验证。

## 真实 Ctrl+C：必须在 Windows 终端验收

在 PowerShell / Windows Terminal 中执行：

```powershell
.\scripts\verify-windows-docker.ps1 -ManualCtrlC -Report artifacts/windows-docker-manual.json
```

等待 `READY: press Ctrl+C now` 后再按 Ctrl+C。入口用实际 SIGINT 处理器接入生产取消上下文，
随后检查容器消失、子进程停止写文件、宿主未回写以及后续工具调用。
提前按键或一直不按都会失败；不能用杀进程、关闭窗口、CTRL_BREAK_EVENT 或模拟取消代替。
这一步验证控制台到 SIGINT 的传递，不能替代下面的完整应用检查。

还需在真正的 `repo-agent --sandbox docker --sandbox-profile standard --new-session` 交互界面中，
使用临时测试项目做一次验收（此项需要已配置模型）：

1. 请求运行一个先写入标记文件、再等待 90 秒的命令；确认命令已开始后按 Ctrl+C，再试连续两次。
2. 确认任务标记“用户主动停止”、队列暂停、原项目没有自动回写；只检查本次会话容器，不要求其他会话停止。
3. 排队一个简单后续任务，执行 `/queue resume`。清理已确认时应继续；清理未确认时必须拒绝，记录原始提示。
4. 检查 `/diff` 中保留的副本变更，退出并恢复会话，确认没有自动重跑旧命令或自动发布取消任务的改动。
5. 保存报告、应用日志、Windows/Docker Desktop/WSL 版本、Docker 后端类型、终端类型及测试提交 SHA。

Windows Docker 的 `docker run` 客户端现在在启动时原子加入 kill-on-close Job，主线程恢复前已
归入管理范围。正常结束、超时、取消都终止 Job 并查询活动进程数为零；随后独立删除容器。
两项都确认才允许队列恢复。Job 查询、终止或句柄关闭失败仍报告 `unknown`，禁用该后端；
容器删除成功不会覆盖宿主清理失败。Job 只管理可信客户端的生命周期，不授予工具宿主执行权限。
活动进程数归零和主进程退出信号可能不同步：两项核验共用 2 秒清理期限，期间继续排空输出；
延迟退出可在期限内确认，期限耗尽或查询异常仍失败，不为第二段等待重新计时。
未启用监督的通用 Windows ProcessRunner 仍为尽力终止，不能据此推断其他宿主命令已完整回收。
本地跨平台回归通过不能替代 Windows 内核测试和 Desktop 验收通过。

## CI 与测试开关

- `host-files.yml`：跨平台宿主契约及验收程序自身的回归；Windows 内核文件及客户端 Job 测试只在 Windows 生效。
- `docker.yml`：Ubuntu 上运行实际 standard 镜像，包括完整验收和真实容器 TUI 测试，并保存 JSON。
- `windows-docker.yml`：仅手动触发，需要专用 runner 标签 `self-hosted, Windows, X64, docker-desktop`。
  runner 用户必须能访问已经启动的 Docker Desktop；普通 Windows 托管 runner 不能代替该环境。
  只在专用验收机上运行可信提交，不对任意 PR 自动执行。每次使用独立镜像标签，保留镜像供排查，
  不自动删除共享镜像；维护者可以在确认无引用后清理本次标签。无人值守 CI 不执行真实终端按键。

独立运行真实容器测试（在 Windows 中缺少条件时失败，未启用时跳过）：

```powershell
$env:RUN_WINDOWS_DOCKER_TESTS = "1"
$env:DOCKER_ACCEPTANCE_REPORT = "artifacts/windows-docker.json"
$env:DOCKER_TUI_REPORT = "artifacts/windows-docker.tui.json"
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -ra tests/test_docker_acceptance.py
```

Linux/macOS 使用 `RUN_SANDBOX_DOCKER_TESTS=1`。可用 `SANDBOX_LSP_TEST_IMAGE` 指定已构建的
standard 镜像。仅 Windows 开关在非 Windows 上不会启动实机测试。旧的
`test_windows_files.py -k real_windows_docker` 仍作为最小往返测试保留。

GPU 应按 [GPU 指南](gpu-operators.md) 在 Windows WSL2 + NVIDIA 实机另验。
网络盘、UNC、云盘重解析点、Windows ARM64、不支持的文件名和 Linux 权限位不纳入通过承诺。
Windows 文件名拒绝策略可在任意平台单测；Windows 句柄、ACL、共享和控制台行为必须实机验证。

## 是否可以把 Windows 默认模式改成 Docker

代码层面可行：安装记录已有 `docker` 模式，CLI 会沿用显式 Docker 安装的选择，
不需要先更改所有 Windows 用户的首次安装默认值。当前仍保留首次默认 `local`。

将“所有 Windows 首次安装”默认切为 Docker 的发布门槛是：

1. Windows Desktop 自动验收、真实控制台和真实 TUI 验收在目标版本上通过，并保存该提交的证据。
2. 实测本次 Job 修复：正常 Ctrl+C 后清理 confirmed 且队列恢复；故障时保留 unknown 并阻止恢复。
3. 在全新 Windows 用户环境验证安装、镜像准备、升级、卸载及恢复；无 Docker、服务未启动、
   Windows 容器模式、镜像缺失和离线环境应给出明确错误，不能隐式退回不隔离执行。
4. 明确只承诺 Linux 项目工具链，说明本地 `.venv` 不复用、文件命名/权限差异及 GPU 附加条件。

新增测试和工作流不等于这些门槛已经通过。没有 Windows 实机报告时，不应把默认模式改变当作
本次补测的附带修改；对已准备 Docker 的用户，可继续显式 `--mode docker` 安装或使用
`--sandbox docker` 启动。
