# 平台适配边界

[文档首页](index.md) · [项目架构](../Project_Architecture.md) · [开发指南](development.md)

平台相关公共能力集中在 `host_support`。当前支持 macOS/Linux 的目录布局、默认执行模式、安装登记、会话格式、沙箱隔离和清理结果；Windows 仅有部分标识与布局描述，尚无发行目标或原生沙箱。

## 职责与依赖

| 模块 | 负责 | 调用方继续负责 |
| --- | --- | --- |
| `platforms` | 操作系统/架构规范化、显式发行目标、wheel 标签与归档后缀 | 指定构建宿主、发行目标或执行环境，不能混用 |
| `paths` | XDG 用户目录、安装命令与 Python 环境布局 | 配置优先级、资源归属、创建/删除时机 |
| `python_environments` | 按原优先级选择项目解释器，不执行项目代码 | `sandbox/project_python.py` 决定允许读取的目录并拒绝过宽授权 |
| `filesystem` | 无链接跟随的描述符操作、目录遍历和文件服务机制 | 工具路径策略、文件类型/大小检查、回写冲突与备份 |
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

源码的 `install.sh` / `uninstall.sh` 与发行入口 `install-release.sh` 都委托给 `scripts/installer-entry.sh`，统一选择引导解释器。发行 schema 2 仅保留后一个顶层入口，并通过 `--uninstall` 提供备用卸载；具体操作见[安装与卸载](installation.md#卸载)。

开发安装新增顶层包后，需要在已准备依赖的环境中刷新 editable 登记：

```bash
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
```

增加模块后执行 `python3 build_manifest.py --write`。`tests/test_host_support.py` 在复制的引导包中使用 `-I -S` 验证标准库启动；构建测试检查实际 wheel/sdist/Docker 上下文包含公共模块。

## 扩展与验证

增加平台时依次实现布局、锁与文件保护、进程清理、系统集成和具体后端，再加入发行目标。当前 Windows 的系统/架构识别及 Python 布局描述只是扩展入口，不表示安装或隔离执行可用。

默认回归覆盖各平台策略、文件防护、并发锁、取消、会话与安装恢复；`tests/test_host_support.py` 另覆盖公共层的导入边界、安装/沙箱 PATH 一致性、原子写失败和归档路径。macOS Seatbelt、Linux namespace、Docker 与 GPU 的真实验收仍使用[开发指南](development.md#开发环境与验证)中的独立开关，模拟平台测试不能替代实际内核验证。
