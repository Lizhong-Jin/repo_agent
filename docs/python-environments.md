# Python 运行时与项目环境

[文档首页](index.md) · [安装](installation.md) · [源码开发](development.md) · [构建发行包](distribution.md)

## 两套 Python 的职责

Agent 使用固定的解释器和依赖，用户项目继续使用已有的 venv/Conda。二者可采用不同 Python 版本。

| 组件 | Python 来源 |
| --- | --- |
| Agent、文件/Web 工具、可信 worker、Python 语言服务器 | 安装目录的 Agent `.venv` |
| Linux seccomp 启动器、CUDA 驱动 kernel 自检 | Agent Python；不依赖 PyTorch |
| native `run_python`、项目命令中的 `python`/`python3` | 自动发现或显式指定的项目 Python |
| 项目 PyTorch/Triton 探测 | 项目 Python |
| Python 语义分析 | Agent 启动 pylsp，配置 Jedi 使用项目解释器 |
| Docker 工具 | 镜像内的 Python，不读取宿主项目环境 |
| local | 不启用通用 Python/命令执行 |

项目解释器不需要安装 repo-agent。Git 和语言服务器的控制进程保持 Agent 的 PATH；仅项目命令的 PATH 使用每次调用生成的 Python 入口，再接项目环境的 `bin`；`python`、`python3` 均转发到选定解释器，即使该环境只有其中一个名称。不把用户激活环境中的所有变量带入沙箱，尤其不继承 API Key、代理、PYTHONPATH 或任意启动脚本。

## 受管运行时

发行版只构建平台完整包，默认生成所有支持的平台，按 `dist/<版本>/<平台>/` 保存。各包内置对应的运行时与运行依赖；源码安装和开发材料仍按目标机器准备。发布命令及全平台离线材料布局见[构建与分发](distribution.md)。

源码和发行安装默认使用 `runtime/python.lock` 固定的 CPython 3.13.15（python-build-standalone 20260924，普通 GIL、install_only_stripped）。支持 Linux/macOS 的 x86_64/ARM64；WSL2 使用 Linux 包。平台发行包的 Python wheels 以 Linux glibc 2.28+、macOS ARM64 11+ / x86_64 10.15+ 为目标；系统沙箱和语言工具链还须满足各自要求。

解释器及标准库、配套库、许可证文件来自完整的上游运行时归档，不复制构建机器上的 `.venv`。安装器先校验固定 SHA256，再解压并检查 ssl、ctypes、venv、ensurepip 和版本。没有宿主 Python 也可引导安装；在线下载需要 curl，解压需要 tar，校验使用 sha256sum 或 shasum。

运行时放在 `${XDG_DATA_HOME:-~/.local/share}/repo-agent/runtimes/<版本>-<平台>-<哈希前缀>/`。同一版本可被多套 Agent 安装复用，每套安装仍有独立 `.venv`。升级使用新的缓存目录，不原地替换旧解释器。卸载 Agent 保留共享运行时，避免破坏其他 venv；确认没有安装使用它后才可手动删除。

| 参数/环境变量 | 作用 |
| --- | --- |
| `AGENT_PYTHON=/绝对路径/bin/python` | 使用指定解释器，不自动替换或下载受管 Python |
| `AGENT_PYTHON=system` | 显式启用旧的系统 Python 搜索方式，需要完整 Python 3.11+ |
| `AGENT_PYTHON_ARCHIVE=/路径/python.tar.gz` | 从本地归档安装，仍必须匹配锁文件的 SHA256 |
| `AGENT_PYTHON_CACHE=/路径` | 覆盖运行时缓存根目录；安装后不要随意移动 |
| `--offline --wheelhouse /路径` | 禁止 Python 依赖下载，缺少材料时明确失败 |

`--check`、`--recover` 和备用卸载优先使用本安装（含待恢复事务）的 Python，再尝试已校验的运行时缓存和系统 Python，不下载或创建受管运行时；显式设置 `AGENT_PYTHON` 时只使用该选择。检查/安装需要 venv 与 ensurepip，恢复和卸载只要求 Python 3.11+ 标准库。运行时引导在 Python 安装器之前完成；后续安装失败会恢复原 Agent 环境和命令，但已校验的共享运行时保留供重试。

## 项目 Python 自动选择

native 每次建立会话时按以下顺序选择，并打印最终路径和来源：

1. `--project-python`，其次 `AGENT_PROJECT_PYTHON`。
2. 启动 Agent 时有效的 `VIRTUAL_ENV`，其次 `CONDA_PREFIX`。
3. 工作区 `.venv/bin/python`。
4. 启动时 PATH 中的 `python`，其次 `python3`。
5. 没有其他可用环境时使用 Agent Python，来源显示为 `agent fallback`。

`AGENT_PROJECT_PYTHON` 只从启动进程的环境变量读取，不从项目或用户 `.env` 加载，避免项目配置自行增加宿主环境的读取范围。

自动发现排除 Agent 自己的环境，避免把它误认为已激活的项目环境。显式指定的解释器无效时失败，不换用其他解释器。相对路径相对于工作区解析；使用常规的 `bin/python` 布局，pyenv/asdf 等 shim 请改为其背后的真实环境路径。

```bash
conda activate my-project
repo-agent
# 或明确选择，保留 venv 入口，不要手工解析成基础解释器路径
repo-agent --project-python /path/to/project/.venv/bin/python
```

选定路径在本次会话中固定；想切换解释器，应退出后重新启动。自动发现不会自动创建项目环境或安装项目依赖。

宿主只检查文件路径和 pyvenv.cfg，项目解释器的运行探测在 OS 沙箱内进行。项目目录、基础解释器目录按需只读挂载：项目命令、Python、环境探测和语言分析需要这些路径；Git、Agent 自检和轻量文件工具不因此增加一整套项目环境。现有系统读取范围（例如 Linux 的 `/usr`）仍存在，环境分离并不自动消除全部系统扫描。

项目环境继续只读（包括轻量文件工具）；沙箱命令继续断网。项目依赖安装应在启动 Agent 前由用户完成。依赖通过 `.pth` 或自定义加载器指向环境之外时，不自动扩大宿主读取范围；需整理环境或显式使用包含所需依赖的解释器。Jedi 的项目环境用于导入解析，其他诊断插件仍运行于 Agent Python，不保证所有跨 Python 版本语法都能被相同插件完整分析。

`get_execution_environment` 的 `execution.python_environments` 报告两套路径、选择来源及项目探测结果；`runtimes.python` 对应项目解释器，`runtimes.agent_python` 对应可信 worker。清理异常后的被动诊断不执行新的项目探测。
