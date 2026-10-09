# Windows native 隔离执行与清理

[平台适配](platform-adaptation.md#原生后端) · [原生沙箱](native-sandbox.md)

Windows x86_64 native 已接入工厂、能力报告、安装器与 CLI。执行使用 LPAC + Job Object；文件工具由宿主机上的可信文件服务操作原项目；进程工具在过滤后的私有项目副本中执行，确认进程树清理后逐调用回写。macOS/Linux 保持原有直接修改项目的行为。Windows 安装仍默认 local；本机启动必须通过真实隔离自检，失败不降级。

## 使用

需要 Windows 10 1809+ / Windows 11 x86_64、本地 NTFS 和完整的 Python 3.11+ 安装或常规 venv。所需 Windows API、文件权限或 Python 布局不可用时会直接报错。建议先在目标 Windows 主机运行下文的实机验收。

```powershell
# ZIP 首次默认 local；native 的 Python 语言服务依赖需要联网补齐。
.\install_release.ps1 --mode native --languages python
repo-agent --root C:\projects\example --sandbox native
repo-agent --root C:\projects\example --sandbox native --project-python C:\projects\example\.venv\Scripts\python.exe
```

当前自动配置限 Python；Git 工具需要标准 Git for Windows 安装。Windows ZIP 仍只携带核心依赖，不承诺 native 离线安装。`run_command` 支持已授权的 Python、Git、System32 以及项目副本中的 `.exe`；需要 Shell 时显式调用 `cmd.exe /d /c ...`。已有 `run_shell` 是 Bash 接口，Windows 上不支持。任意外部工具目录、GUI、GPU、Node/Go/clangd 工具链和任意 Python 启动包装器不在此实现的授权范围内。

## 模块边界

| 模块 | 职责 |
| --- | --- |
| `sandbox/native_execution.py` | 可替换执行接口及清理错误约定 |
| `host_support/windows_isolation.py` | ctypes Win32 绑定、身份、LPAC 启动属性、Job、令牌与进程查询 |
| `host_support/windows_processes.py` | 单次资源作用域、有界输出、超时/取消、进程树清理 |
| `host_support/windows_security.py` | 私有目录 ACL、拒绝重解析点的权限设置、安全删除 |
| `host_support/windows_recovery.py` | 持久化意图、活动租约、跨重启回收 |
| `sandbox/windows_execution.py` | 接入执行接口和清理状态 |
| `sandbox/windows_native.py` | 平台策略、工具接入、真实隔离自检 |
| `sandbox/windows_python.py` | Python 布局检查和私有副本重定位 |
| `sandbox/windows_workspace.py` | 过滤快照、冲突检查、回写及备份 |

Windows DLL 延迟加载，不改变 POSIX 执行器或 Windows local/Docker 的进程行为。执行内核不依赖 PowerShell 模块或第三方 Python 库。

## 文件访问与回写

每次调用复制可信 worker、所选 Python 及项目文件到新 profile。文件复制使用共享的句柄相对服务，拒绝重解析点、硬链接和特殊文件；过滤密钥文件、配置保护路径、虚拟环境和缓存。Python 只复制常规运行时目录；venv 的 `home` 只在副本中改写，原环境保持不变，外部 system-site-packages 不开放。推荐使用 `python -m ...`，复制来的第三方 console launcher 可能内嵌原解释器路径，不能保证可重定位。

ACL 只设置在本次拥有的私有目录：运行时、请求和 Git 元数据只读，项目副本及临时目录可写。禁止受限身份改 DACL/owner，使用 OWNER RIGHTS 限制所有者隐式改权；不在真实项目或 Python 安装目录上临时授权。可信状态及备份目录不向隔离进程开放。

进程的 cwd 和 Python 路径是私有副本路径。命令参数中的独立绝对路径会映射已授权项目/Python 根；代码字符串内嵌的原宿主绝对路径不改写，不能依赖其可访问性。临时目录和运行时修改不跨调用保留。

执行完成（包括非零退出或超时）后，只有确认整个 Job 已退出才检查、回写项目文件。回写复用已有冲突检查与备份事务：外部同时修改的文件不会直接被覆盖；多文件回写并非整体原子事务。取消、宿主异常死亡、清理不明或回写失败会保留副本供恢复，不自动覆盖原项目。失败的后端停止接受后续执行。回写成功后删除副本与本次备份；这不是任务级撤销功能。文件工具本身仍直接写原项目。回写跟踪文件内容变更；只创建/删除空目录的进程操作不会跨调用保留。

默认项目快照上限 256 MiB；每个运行时复制上限 2 GiB。这些是准备/回写检查，不是进程磁盘配额。每次复制 Python/Git 与项目会增加延迟和磁盘开销，大型环境应先评估。支持的 Git 读取调用复制只读对象、索引和引用，重建最小配置，不加载仓库 hooks、filters 或外部 alternates；链接 worktree 元数据通过既有受管工作区接口处理。

`get_execution_environment` 报告 `writeback_mode=per_call_after_cleanup`、`process_workspace=filtered_private_copy` 和最近回写结果。Docker 的 `--sandbox-writeback on-success`、`/apply` 不适用于此逐调用流程。

## 隔离与清理

每次调用新建随机 AppContainer profile，无网络或目录 capability。`SECURITY_CAPABILITIES` 配合 `ALL_APPLICATION_PACKAGES_OPT_OUT` 创建 LPAC，不继承普通 AppContainer 对 `ALL APPLICATION PACKAGES` 的额外访问。系统对受限应用共享的基础资源仍可能可见；它不是虚拟机。

创建时禁用 Win32k 与扩展点，使用 `CREATE_SUSPENDED`，通过 `PROC_THREAD_ATTRIBUTE_JOB_LIST` 在创建时原子加入私有 Job；恢复主线程前核验 Job、AppContainer SID 和零 capability。任何属性或检查失败都拒绝执行。环境由可信层显式提供，句柄白名单只有标准输入输出，stdin 立即 EOF。

Job 设置 `KILL_ON_JOB_CLOSE`、禁止 breakaway。正常退出也终止残余子孙进程；超时/取消后在独立的有界期限内验证 `ActiveProcesses == 0` 和主进程已结束。stdout/stderr 持续排空并保留有界头尾。查询、终止、句柄或 profile 释放失败会传播不确定状态并禁用后端。

启动自检实际执行 Python，验证宿主秘密读写拒绝、私有运行时只读、网络连接拒绝、项目副本可写。自检不会回写测试文件。进程树、断网等更完整的保证还需下文内核测试验收。

## 异常恢复

创建 profile 前持久化 UUID 意图记录，并持有文件租约。真实后端使用随机命名的 Global Job，句柄不继承；宿主被强杀后内核终止进程。重启时只处理本应用记录的精确身份，跳过仍持有租约或 Job 未清空的资源。查询失败保留记录，不凭 PID 猜测已退出。

```powershell
repo-agent native-cleanup list
repo-agent native-cleanup collect
# 明确丢弃某次保留副本及其回写备份（不可恢复）：
repo-agent native-cleanup discard <32位ID或profile名称>
```

`list` 显示活动、可回收、保留及失败记录；保留原因包含项目副本和恢复记录位置。自动回收和 `collect` 不丢弃执行中断/回写失败留下的工作，也不自动应用副本。需要恢复时先检查这些位置并手工比对文件/备份，再决定是否丢弃。活动资源不能强制丢弃。回收只在 Job 已空后进行，通过句柄删除链接自身，不跟随 junction 删除外部目录。空的 `.lock` 文件有意保留，避免不同回收进程锁住不同 inode 的竞态。

## 验证

跨平台策略及失败注入测试：

```bash
python -m pytest -q tests/test_windows_native.py tests/test_windows_isolation.py tests/test_native_execution_adapter.py
```

Windows x64 的 Visual Studio x64 Native Tools 环境，安装 `requirements-dev.lock` 和 `requirements-lsp.lock` 后：

```powershell
$env:RUN_WINDOWS_ISOLATION_TESTS = "1"
python -m pytest -q -ra tests/test_windows_isolation_kernel.py tests/test_windows_native_kernel.py
```

内核测试覆盖令牌、LPAC 对普通 AppContainer 授权的隔离、越界读写、断网、子孙进程、breakaway、有界输出、超时、取消、启动失败及宿主被强杀；完整后端测试覆盖启动自检、Python/worker/Git/LSP、venv 重定位、回写、保留记录和包含 junction/硬链接/只读文件的清理。

CI 提供 Windows x64 × Python 3.11/3.13 独立作业；缺 SDK/API 时失败，不能以跳过冒充通过。macOS/Linux 跳过真实 Windows 内核用例；配置 CI 和跨平台单测通过不等于完成 Windows 实机验收。

实现依据 Microsoft 的 [AppContainer/LPAC](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)、[进程创建属性](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-updateprocthreadattribute)、[Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) 和 [profile 删除](https://learn.microsoft.com/en-us/windows/win32/api/userenv/nf-userenv-deleteappcontainerprofile)契约。
