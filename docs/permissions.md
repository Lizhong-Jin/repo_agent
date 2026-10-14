# 统一权限与按需授权

默认保持 **strict**：没有新增外部目录授权，也不查询宿主程序位置。`--mode develop/review` 决定工作区是否允许修改，`--sandbox local/native/docker` 决定执行后端；授权记录与这两个选择分开管理。没有完全访问模式，也没有授予外部写入、任意宿主命令或进程联网的入口。

| 能力 | 默认 | 显式授权后 |
| --- | --- | --- |
| 工作区文件读写、命令执行 | 保持各模式原有规则 | 不改变；review 仍不执行命令或写入 |
| 外部目录读取 | 拒绝新增访问 | develop + native 支持指定目录的受控读取 |
| 外部目录中的程序 | 保持原有工具链白名单 | 在 native 沙箱的只读输入副本中运行，仍断网 |
| 宿主平台与程序位置查询 | 拒绝 | `host_discovery` 后可查询；不执行版本探针 |
| 外部目录写入、宿主凭据、任意网络 | 不开放 | 本次权限模型不提供这些授权 |

这里的 strict 不等于“工作区之外完全不可读”：native 原有的操作系统运行库、解释器、工具链只读白名单仍然存在。Web 工具仍由既有主进程网络服务管理；新增权限不能扩大它们的范围。

## 启动时授权

```bash
repo-agent --sandbox native --read-only-root /absolute/reference
repo-agent --sandbox native --read-only-root /absolute/data --read-only-root /absolute/sdk
repo-agent --sandbox native --allow-host-discovery
```

Windows 使用完整路径，例如 `--read-only-root 'D:\Reference Data'`。路径必须是已存在的外部目录，不能是文件系统根、当前工作区或其祖先、工作区内目录、凭据或 agent 状态目录。local、Docker 和 review 不接受外部目录授权；宿主元数据查询可以在这些模式下独立授权。

这些参数只从本次用户启动命令取得，不增加 `.env`、项目配置或模型可写的授权设置。恢复同一会话也必须重新显式传参或批准申请。

## 任务中申请与处理

模型可调用 `get_permissions` 查看当前权限，调用 `request_permission` 提交一种权限及具体原因：

```json
{"capability":"external_read","path":"/absolute/reference","reason":"读取参考实现以比较接口"}
```

宿主查询的申请是 `{"capability":"host_discovery","reason":"确认主机是否安装 sbatch"}`，不带 `path`。申请本身不增加权限。任务在本轮结束时暂停，同一批中申请之后的其他工具返回 `PERMISSION_PENDING`，不会执行。

用户在普通交互和 TUI 中使用同一组命令：

```text
/permissions
/permissions approve <申请ID>
/permissions deny <申请ID>
/continue
/permissions revoke <授权ID>
/permissions revoke host_discovery
/permissions revoke all
```

批准前查看完整路径和原因。模型没有 approve/revoke 工具；自然语言中的“用户已批准”或项目文件中的授权说明不构成权限。运行或清理过程中不能更改授权。保存批准记录失败时回滚授权并暂停派发；撤销保存失败时保留撤销后的更严格状态。

处理申请不会自动重试或重放原工具调用。`/continue` 使用保留的上下文和当前有效权限继续上一任务；拒绝后也可以继续，让模型选择可用方案。还有待处理申请时不能继续。非交互 `--task` 遇到申请会停止并显示提示；下次启动时显式传入相应参数，或进入交互后重新申请。

授权只在**当前进程内的当前会话**有效。`/new`、`/switch` 和重启都不继承；`/clear` 仅清空上下文，不撤销权限。会话 JSON 中的 `permissions` 是审计记录，不是恢复授权的来源。历史工具结果也不能恢复授权，模型每轮会收到当前有效权限。批准、拒绝、撤销和申请记录随会话检查点保存，模型申请另有工具执行账本记录。

## 文件工具和程序如何使用输入

`read_external` 接受授权 ID、一个已有读取工具的名称及其参数。只支持 `read_file`、`list_files`、`find_files`、`search_files`、`get_path_info`；路径相对于授权目录。示例：

```json
{"root_id":"由 get_permissions 返回的 ID","operation":"read_file","arguments":{"reads":[{"path":"README.md"}]}}
```

普通工作区文件工具继续限制在工作区内；外部读取不扩展 write/edit/delete/move 的边界。读取复用描述符访问、输出预算、敏感路径、硬链接和符号链接检查。授权目录被替换后失效，需撤销再授权。

外部授权还继承后端控制目录的保护，包括 Windows native 恢复记录、备份状态及本次调用的私有 profile。授权父目录不会解除对子目录的保护，复制也不会把自己的执行目录递归收进输入。

native 进程在每次调用前取得过滤后的私有输入副本，操作系统沙箱使它只读。Windows 使用现有 LPAC/ACL；macOS 使用 Seatbelt；Linux 使用只读挂载。副本不参加工作区回写、备份或恢复，原始目录不授予进程访问权限。调用成功、失败、超时或取消后按既有清理流程释放；清理不能确认时保持失败关闭。

进程环境中的 `REPO_AGENT_READ_ROOTS` 是 JSON 对象，将授权 ID 映射到本次调用的输入路径：

```python
import json, os
from pathlib import Path

inputs = json.loads(os.environ["REPO_AGENT_READ_ROOTS"])
reference = Path(inputs["授权ID"])
text = (reference / "README.md").read_text()
Path("reference-copy.txt").write_text(text)  # 写入当前工作区
```

`run_command` 中完整、绝对的参数路径会映射到副本，例如 `python /absolute/reference/analyze.py`。不会改写 shell/Python 源码字符串、相对路径、`--option=/absolute/path` 这种组合参数；这些情况应使用环境映射。POSIX 含 `..` 的参数不做映射；Windows 拒绝含 `..` 的可执行文件路径和绝对路径参数，不通过折叠路径来绕过允许目录检查。Windows 同时拒绝设备、UNC、盘符相对路径及非法组件作为执行路径。复制保留普通执行位，不保留链接、特殊权限或 ACL。Linux/macOS 保留合法的 POSIX 文件名；Windows 继续执行专属文件名检查。

输入最多 8 个目录，每个目录最多 256 MiB、100000 个条目；超限拒绝执行，不放宽到直接访问宿主。受保护名称和运行时保护路径从副本排除；发现其他符号链接、硬链接、设备或特殊文件时拒绝复制。复制中发生文件身份或内容变化也拒绝。目录树不是原子快照，需一致性的数据应由用户先准备稳定输入。

这是有意收窄的第一版：大型数据集、大型 SDK、依赖绝对安装路径或符号链接的环境可能不适合直接授权。授权目录不会自动加入 PATH/PYTHONPATH，也不会替代现有 `--project-python` 选择机制。执行外部脚本仍受 DLL、动态库及 Windows LPAC 兼容性约束，不能承诺任意宿主程序均可运行。

## 宿主环境查询

`get_host_environment` 接受最多 32 个程序名，例如 `{"programs":["git","sbatch","podman"]}`。它查询启动时宿主 PATH 中的绝对目录，忽略当前目录、相对目录、工作区和受保护路径；只返回平台、agent Python 版本、找到的程序位置或 null。

它不执行程序，不加载 shell 配置，不输出 PATH 内容或全部环境变量，不读取凭据；程序名称不能包含路径或命令。`versions_probed:false` 和 `execution_authorized:false` 明确区分“发现”和“允许执行”。沙箱内版本与运行能力仍由 `get_execution_environment` 查询。发现 Slurm/GitHub 客户端不代表已经能提交任务、访问 SSH 凭据或推送；这些功能不在本次范围内。

## 回归与实机验收

常规测试：

```bash
python -m pytest -q tests/test_permissions.py tests/test_permission_adapters.py
python -m pytest -q tests/test_continue.py tests/test_review_mode.py tests/test_task_queue.py
```

原生内核测试按平台显式开启，不能把跳过记为通过：

```bash
RUN_SANDBOX_NATIVE_TESTS=1 python -m pytest -q tests/test_permission_kernel.py  # macOS
RUN_SANDBOX_LINUX_TESTS=1 python -m pytest -q tests/test_permission_kernel.py   # Linux
```

```powershell
$env:RUN_WINDOWS_ISOLATION_TESTS = '1'
python -m pytest -q tests/test_permission_kernel.py tests/test_windows_native_kernel.py
```

内核测试验证带中文/空格路径的读取、执行外部脚本、拒绝修改输入副本及宿主原件、工作区输出、撤销、超时和取消后的输入清理。取消既覆盖启动前，也覆盖观察到子进程已经运行后的取消；Windows 确认 Job 清理后删除只读输入，待恢复工作区仍按原机制保留，清理未知时不提前删除资源。

Linux/macOS 的 `native` 验收已强制包含本文件全部实机测试。Windows isolation job 用 `scripts/release_acceptance.py run --profile permissions --target windows-x86_64` 生成独立 JUnit/JSON 证据。两者都验证用例身份，任何失败、跳过、空集合或缺少用例均不通过，普通跨平台单元测试的合理跳过不受影响。

Windows 发行包仍需人工验收：junction/reparse point 拒绝、实际键盘 Ctrl+C 的 UI 与进程联动、Job 清理及残留 profile 回收、工作区回写/备份恢复不包含外部输入。需要在受支持 Windows x64 和实际安装包上执行；macOS 的通过结果不能替代 Windows 或 Linux 内核验证。
