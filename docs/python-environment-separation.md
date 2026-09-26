# Agent 与项目 Python 的解耦方案（尚未实现）

这份文档描述后续设计。此次改动只合并 Linux 的工作区遍历和启动自检，不改变当前 Python 选择方式，也没有增加项目解释器配置开关。

## 当前绑定在哪里

`NativeBackend` 将 `sys.executable` 保存为 `self.python`。这个解释器同时用于可信 worker、Linux seccomp 启动器、`run_python` 和启动自检。沙箱 PATH 也优先使用它所在的目录。Python LSP 默认通过 worker 的 `sys.executable -I -m pylsp` 启动；环境工具也会根据 worker 的 Python 报告运行环境。

因此，让 Agent 自身运行在大型 Conda 环境中，往往也把整套环境带进系统读取范围；换一个精简 Agent 虚拟环境，又可能使 `run_python` 找不到项目的 PyTorch 等依赖。

## 建议的配置与边界

分别维护以下信息：

- `agent_python`：启动 Agent 的受信任解释器。固定用于宿主服务、冻结的可信 worker、seccomp 启动器及仅依赖标准库的 CUDA 驱动自检。
- `project_python`：用户明确选择的项目解释器，例如工作区 `.venv/bin/python` 或特定 Conda 环境。用于执行用户代码与探测项目依赖。
- 项目环境的读取路径与进程环境变量：与 Agent 运行环境分别构造，按本次工具的需要加入沙箱。

初版优先使用显式配置；未设置时继续沿用 Agent Python 以保持兼容。显式指定的解释器不存在或无法使用时，应报告错误，不悄悄切回 Agent Python。后续可增加可选的 `.venv` 自动发现，并明确展示最终选择。

可以在宿主读取 `pyvenv.cfg` 等配置并解析路径，但项目 Python 的实际探测和执行必须在 OS 沙箱内完成。解释器启动可能执行环境中的初始化代码，不能为了查询 `sys.prefix` 而直接在宿主启动一个项目可修改的解释器。

项目解释器可以与 Agent 使用不同 Python 版本。可信 worker 不导入项目依赖，项目代码也无需安装 Agent 包；需要查询项目环境时，用兼容的独立小脚本输出 JSON，再由可信 worker 解析。

## 工具使用哪个环境

| 工具或组件 | 建议使用的环境 |
| --- | --- |
| Agent、技能/工具组加载、文件工具、Web 工具 | Agent 进程及其 Python 环境 |
| 可信沙箱 worker、Linux seccomp 启动器 | `agent_python` 的绝对路径，保持隔离导入 |
| `run_python` | `project_python` 的绝对路径 |
| `run_command` 中的 `python`、`python3` | 项目进程 PATH 优先指向项目环境；有歧义时使用显式解释器路径 |
| `python -m pytest`、`python -m pip` | 项目环境；相比裸 `pytest`/`pip`，更能明确解释器归属 |
| Python LSP | 受信任工具环境启动语言服务器，通过其支持的配置传入项目解释器或依赖路径 |
| Git、Node、Go 等 | 各自的工具链；由可信 worker 控制，不因项目 Python 的选择而改用项目里的同名辅助程序 |
| CUDA 驱动与内核自检 | Agent Python 的标准库 ctypes；不需要 PyTorch |
| PyTorch/Triton/项目 CUDA 程序 | 项目 Python、项目依赖及已授权的 CUDA 设备和库 |
| 环境查询工具 | 分别报告 Agent 环境与项目执行环境；项目依赖探测使用项目 Python |

不能只把项目 `bin` 插到所有进程共用的 PATH 前面。可信控制进程应使用自己的环境和绝对启动路径；项目命令使用单独构造的 PATH。否则，项目中可修改的同名程序可能被误当作可信工具运行。

Python LSP 的服务器运行时与分析目标是两个概念。不必为了分析一个 PyTorch 项目，把 PyTorch 安装到 Agent 环境，也不应仅因项目选择了另一个 Python 就自动运行项目提供的任意语言服务器。具体的环境配置取决于所选 LSP 及插件支持，需要补充跨环境补全、跳转、诊断测试。

## 解耦后的效果

Agent 可以使用精简、固定版本的依赖，项目继续使用既有 Conda/venv、PyTorch 和 CUDA 版本。升级项目包不会改变可信 worker 的依赖；升级 Agent 也不要求迁移项目环境。不同项目可选择不同 Python，工具结果可以清楚报告实际执行路径。

性能收益需要配合按工具选择挂载路径：仅运行 Agent 的工具不必挂载项目的整套 Python 环境；执行项目代码时仍须挂载并保护其依赖。若始终同时挂载两个环境，扫描量可能反而增加。当前整体挂载 `/usr` 和解释器根目录的兼容策略也需要逐步细化，单纯增加两个配置字段不会自动减少扫描。

解耦不改变 GPU 硬件授权、系统驱动要求或文件访问保护，也不自动允许在线安装依赖。项目环境是否可写是另一个权限决策，不能顺带取消现有只读工具链保护。
