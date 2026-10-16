# 统一权限与按需授权

默认保持 **strict**：没有新增外部目录授权，也不查询宿主程序位置。`--mode develop/review` 决定工作区是否允许修改，`--sandbox local/native/docker` 决定执行后端；授权记录与这两个选择分开管理。没有完全访问模式，也没有授予外部写入、任意宿主命令或进程联网的入口。

| 能力 | 默认 | 显式授权后 |
| --- | --- | --- |
| 工作区文件读写、命令执行 | 保持各模式原有规则 | 不改变；review 仍不执行命令或写入 |
| 外部目录读取 | 拒绝新增访问 | develop + native 支持指定目录的受控读取 |
| 外部目录中的程序 | 保持原有工具链白名单 | 在 native 沙箱的只读输入副本中运行，仍断网 |
| 宿主平台与程序位置查询 | 拒绝 | `host_discovery` 后可查询；不执行版本探针 |
| 受控网络、依赖安装、GitHub | 无服务授权 | develop 中按具体操作批准一次；不开放进程联网 |
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

它不执行程序，不加载 shell 配置，不输出 PATH 内容或全部环境变量，不读取凭据；程序名称不能包含路径或命令。`versions_probed:false` 和 `execution_authorized:false` 明确区分“发现”和“允许执行”。沙箱内版本与运行能力仍由 `get_execution_environment` 查询。发现 Slurm/GitHub 客户端不代表已经能提交任务、访问 SSH 凭据或推送；GitHub 使用下述独立服务，后台任务与 Slurm 暂不支持。

## 受控网络、依赖和 GitHub

这四个工具仅由宿主 CLI 在 develop 下注册，不进入容器/原生 worker 工厂。首次调用只准备申请并暂停；用户查看 `/permissions` 中的完整 `scope`，执行 `/permissions approve <ID>` 后 `/continue`。再次调用参数完全匹配时，服务先消耗授权，再进行 I/O。每个授权仅能执行一次，不允许自动重试；失败、超时或取消都不返还授权。更改 URL、仓库、分支、消息、文件内容或锁文件均需要新的申请。模型仍不能批准自己的申请。

申请绑定整个操作：依赖安装包含锁文件摘要及所有包的地址与摘要，GitHub 文件发布包含目标分支、预期提交、消息和每个文件的摘要/大小。最多同时保留 8 个操作授权。`/permissions revoke <ID>`、`revoke all`、新会话和重启仍有效；历史快照和审计收据不能恢复授权。服务串行执行，后端清理状态不健康时拒绝操作。

### 网络读取

`network_request` 示例：

```json
{"url":"https://example.org/reference.txt","reason":"读取指定参考资料"}
```

只支持公网 HTTPS 443 的 GET，不接受自定义 headers、body、凭据、代理、cookie 或重定向。审批包含完整 URL（包括查询参数）；不要把秘密放在 URL 中。解析出的所有 IP 及实际连接地址都必须为公网地址，连接固定到已检查的地址。响应最多 256 KiB，显示最多 24000 字符，按不可信 UTF-8 文本处理。单个 HTTP 请求总时限 60 秒，连接 10 秒、读写各 20 秒；多包安装/多步 GitHub 操作会涉及多个有界请求，Ctrl+C 可中断。操作系统 DNS 解析本身可能稍后才结束，但取消后的结果会丢弃，不会继续发起 HTTP。

这不会给 `run_command`、`run_python`、shell 或语言服务器开放网络，也不会扩大原有 `web_search` / `web_fetch` 的权限。普通网络工具不携带 GitHub token。

### Python 和 npm 锁定依赖

两种安装都由宿主下载、验证摘要并解析归档，只写工作区的新目录 `.agent-dependencies/<锁文件 SHA-256>`。不在宿主运行 pip、npm、包代码或安装脚本，不修改全局环境、现有虚拟环境或工作区顶层 `node_modules`。安装完成后显式在沙箱中引用返回路径；安装不代表包可信，后续代码仍受原有沙箱限制。local 可以准备文件，但仍不能执行代码。

Python 工具为 `install_python_dependencies`，参数示例：

```json
{"lock_path":"deps.lock.json","reason":"安装审核过的 Python 依赖"}
```

`deps.lock.json` 使用以下结构；示例地址和摘要是占位值，须替换为实际 wheel 下载地址与经过核对的 SHA-256：

```json
{"version":1,"wheels":[{"url":"https://files.pythonhosted.org/packages/example-1.0-py3-none-any.whl","sha256":"替换为实际的64位小写十六进制摘要"}]}
```

只接受 PyPI 文件域名上的 `py3-none-any.whl`，且 WHEEL 元数据声明 purelib。最多 32 个 wheel；所有传递依赖也必须明确列入锁文件。当前不解析 requirements/依赖版本、不支持源码包或平台二进制 wheel、不处理 `.data` 安装布局、不安装命令行入口，拒绝 `.pth`、启动钩子和动态库。Python 中使用 `sys.path.insert(0, '.agent-dependencies/<返回摘要>')` 后正常导入。GPU/编译型依赖仍需可信镜像或预置环境。

npm 工具为 `install_npm_dependencies`：

```json
{"lock_path":"package-lock.json","reason":"安装锁文件中的 npm 包"}
```

接受标准 `lockfileVersion: 3` 的 `packages` 锁记录，保留嵌套 `node_modules` 布局。每个非根记录必须有 `registry.npmjs.org` 的 `.tgz` resolved 地址和 SHA-512 integrity。最多 64 个包；不支持 link/workspace、Git/本地来源、私有 registry 或 bundled dependencies。不做版本求解、平台可选依赖筛选、生命周期脚本、原生编译或 `.bin` shim 生成；需要这些步骤的包不能视为可用。可以用沙箱中的 `require('./.agent-dependencies/<返回摘要>/node_modules/包名')`；不会自动改变 PATH/NODE_PATH 或项目的模块搜索路径。

两种归档都拒绝路径逃逸、链接/特殊文件、敏感名称、Windows 设备名/ADS、大小写冲突及文件/目录冲突。单个下载最多 32 MiB，整次下载最多 64 MiB，安装文件总量最多 128 MiB。全部包验证通过后才写入；不覆盖已有版本目录。`repo-agent-manifest.json` 最后写入，表示此次安装已完成，包含锁信息与文件摘要，但不是不可篡改的授权凭证。失败/取消可能留下不完整的新目录；没有完成收据时不得使用，应检查后删除该目录，再重新申请。临时文件会清理，已有版本不回滚或覆盖。

| 执行模式 | Python | npm |
| --- | --- | --- |
| Linux/macOS native | 安装后可显式导入 | 有受支持 Node 工具链时可显式加载 |
| Windows native | 经现有私有副本执行与回写流程 | 明确拒绝，使用 Docker；不放宽现有 `node_modules` 排除规则 |
| Docker（含 Windows 宿主） | 安装到会话私有工作副本，供容器使用 | 同左，需要镜像提供 Node |
| local | 仅准备依赖文件 | 仅准备依赖文件 |
| review | 不注册服务 | 不注册服务 |

Docker 安装不会立即写入原项目，回写/备份仍走现有流程；`node_modules` 仍从宿主回写中排除，npm 环境应视为会话内环境并保留锁文件。不要把回写后的 manifest 单独当作 npm 包已经回写的证明；新会话按锁文件重新准备。Windows native 的 Python 依赖仍需实际 LPAC 运行验收，不能用 macOS 结果替代。

### GitHub

`github` 通过固定 `api.github.com/repos/<owner>/<repo>` 的 REST 服务访问，不调用宿主 git/gh、SSH、credential helper 或 Git hook。支持下列操作，每次都需要具体审批：

| operation | 其他参数 | 行为 |
| --- | --- | --- |
| `repository` | `repo` | 仓库详情 |
| `pulls` / `branches` / `runs` | `repo` | PR、分支或 Actions runs 首屏（最多 30 条） |
| `pull` / `run` / `jobs` | `repo`, `number` | PR 详情、run 详情或 run 的 jobs 首屏 |
| `create_pull_request` | `repo`, `head`, `base`, `title`, `body` | 创建 draft PR |
| `publish_files` | `repo`, `branch`, `expected_sha`, `message`, `files` | 将批准的工作区文件快照发布为现有分支上的新提交 |

```json
{"operation":"runs","repo":"owner/project","reason":"检查最近 CI"}
```

```json
{"operation":"publish_files","repo":"owner/project","branch":"feature/change","expected_sha":"0123456789012345678901234567890123456789","message":"Fix parser","files":["src/parser.py"],"reason":"发布审核过的修改"}
```

文件发布最多 32 个文件、每个 1 MiB、合计 4 MiB。上传前重新检查全部文件摘要，更新前再次检查远端分支，并且只做 `force:false` 更新。这不是原子的 compare-and-swap；最终检查与更新之间仍有竞争窗口，由 GitHub 拒绝非快进更新。新增树项统一为普通文件（100644），不处理可执行位或符号链接。它发布文件内容，不推送本地 Git 历史、不创建分支、不删除文件、不强推、不合并 PR；`.git`、`.env`、凭据路径、硬链接等仍不可读。受保护分支会遵守 GitHub 自身规则。

公开查询可无 token；私有查询和写入使用启动进程提供的 `REPO_AGENT_GITHUB_TOKEN`。用专用、最小仓库范围的 token；读取和写入所需权限由 GitHub 端控制。该变量不加入项目 `.env` 配置、不作为模型参数、不放入授权快照，不传递给命令沙箱，只发送到固定 GitHub API。缺失凭据时写入失败关闭，不寻找宿主其他凭据。远端内容是不可信数据，输出会截断；当前不下载 Actions 日志、不重跑/取消 Actions、不管理 secrets、release 或 issue。

多步发布失败可能留下未引用的 Git 对象；请求发出后超时/取消也可能意味着远端写入已生效。此时授权已消耗，先通过新审批查询远端状态，不能自动重试写入。GitHub 写入是外部副作用，不受本地工作区备份恢复撤销。API 语义参见 [Git refs](https://docs.github.com/en/rest/git/refs) 和 [Pull requests](https://docs.github.com/en/rest/pulls/pulls)。

## 回归与实机验收

常规测试：

```bash
python -m pytest -q tests/test_permissions.py tests/test_permission_adapters.py
python -m pytest -q tests/test_scoped_services.py
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

服务回归覆盖精确单次审批、同批调用暂停、并发防重复消费、撤销/重启、审批保存失败回滚、SSRF/DNS/实际连接地址/重定向/大小限制、取消关闭连接、包摘要/恶意归档/写入中断、Docker 私有工作区以及 GitHub 文件变化和分支竞争。测试使用合成 token、包和 HTTP 响应，不对真实 GitHub 仓库写入。新增 native 内核用例验证已安装 Python 包可导入、宿主 token 不进入子进程、对实际监听端口的连接被拒绝；自动进入三平台 mandatory permissions 验收，跳过不能算通过。

Windows/Linux 实机还须核验各自内核用例；Windows Docker 须用实际镜像验证 Python/npm 导入、安装后的后续调用、Ctrl+C、工作区回写/备份恢复仍排除 npm 内容，并确认无残留容器或 Job 进程。真实 GitHub token 权限、组织策略与 TLS/公网连通性需在测试仓库逐次审批验收（查询、draft PR、文件发布及分支竞争），不能用模拟响应宣称已通过远端集成验收。

Linux/macOS 的 `native` 验收已强制包含本文件全部实机测试。Windows 独立的 `windows-permissions` job 用 `scripts/release_acceptance.py run --profile permissions --target windows-x86_64` 生成 JUnit/JSON 证据，不与 Git/LSP 能力探针共用时间预算。Windows 两组任务均在不继承系统包的 venv 中安装锁定依赖；内核测试显式选择测试解释器，避免扫描和复制 runner 全局 Python 的无关依赖。验收验证用例身份，任何失败、跳过、空集合或缺少用例均不通过，普通跨平台单元测试的合理跳过不受影响。

Windows 发行包仍需人工验收：junction/reparse point 拒绝、实际键盘 Ctrl+C 的 UI 与进程联动、Job 清理及残留 profile 回收、工作区回写/备份恢复不包含外部输入。需要在受支持 Windows x64 和实际安装包上执行；macOS 的通过结果不能替代 Windows 或 Linux 内核验证。
