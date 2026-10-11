# Podman 分发与项目依赖评估

评估日期：2026-10-11。本文是实施建议；当前后端、安装器和发行包仍使用 Docker，
没有新增 Podman 模式，也没有放宽宿主目录访问。Windows Docker 清理修复及验证入口见
[Windows Docker 验收](windows-docker-acceptance.md)。

## 能否在发行包中自带 Podman

可以分发，但“自带客户端”和“离线运行所需组件齐全”是两个不同交付范围。
Podman 本身的 Apache-2.0 许可允许满足条件的二进制再分发，应保留许可证、适用的 NOTICE
和修改说明。附带的运行时、辅助程序及 Linux 系统镜像须逐项核对许可证和相应源码交付义务，
不能把整个运行环境都视为 Apache-2.0；也不自动获得商标授权。
依据：[Podman LICENSE](https://github.com/podman-container-tools/podman/blob/main/LICENSE)。

| 发行内容 | 能解决什么 | 仍需具备或准备什么 |
| --- | --- | --- |
| 本机 Podman CLI / 官方安装包 | 无需用户另找客户端安装来源 | Windows/macOS 的 machine、宿主虚拟化、Agent 工具链镜像 |
| CLI + 对应 provider/架构的官方 machine 镜像与辅助程序 | 可在离线环境准备 Linux 运行环境 | 宿主系统功能启用、磁盘与权限、Agent 工具链镜像 |
| 上述组件 + Agent OCI 镜像归档 | 可设计完整离线容器发行包 | 首次初始化、导入、验证、后续补丁与更新维护 |

Windows/macOS 的 Podman 容器运行在 Linux machine 中，不是把一个 EXE 放进 ZIP 就能工作。
当前上游 Windows 6.x 指南要求 Windows 11、硬件虚拟化以及预先安装 WSL2 或 Hyper-V；
启用系统功能可能需要管理员操作和重启。Hyper-V 还受系统版本与用户组限制。
应以选定且测试过的 Podman 版本锁定支持矩阵，不能沿用 Agent 自身较宽的 Windows 声明。
依据：[上游 Windows 指南](https://github.com/podman-container-tools/podman/blob/main/docs/tutorials/podman-for-windows.md)。

官方 `podman machine init --image` 支持本地镜像路径，因此离线供应 machine 有明确入口；
须使用官方支持的镜像，并匹配宿主 CLI 与 guest 的版本。macOS 同样需要 machine；Linux
可直接运行，但依然需要 OCI runtime、网络/存储辅助组件及合适的内核、用户命名空间设置。
依据：[machine init](https://docs.podman.io/en/stable/markdown/podman-machine-init.1.html)、
[安装依赖](https://podman.io/docs/installation)。Podman Desktop 也提供受限网络安装方案，
但本项目只需 CLI/machine，无需把完整 GUI 应用作为强制依赖。
参考：[受限环境安装](https://podman-desktop.io/docs/proxy)。

建议保留基础 Agent 包，增加按操作系统、架构发布的**可选容器运行包**：

1. `release.json` 记录每个组件的来源、版本、SHA256、架构、provider、许可证清单，以及
   machine/Agent 镜像摘要；构建与安装均校验，签名校验沿用组件官方能力。
2. 先检测系统条件，复用明确选择且已验证的引擎，或初始化 Agent 专用命名 machine。
   不覆盖用户现有默认连接、machine 或共享镜像；使用显式 connection 执行。
3. 容器运行包与 Agent 程序分别更新。升级可回滚；卸载只删除安装记录证明属于本应用的资源，
   不卸载共享 Podman 或清理用户其他容器。
4. 完整离线版额外附带 machine 与 standard/CUDA 镜像归档。实际压缩包、解压及运行时磁盘
   占用应由锁定产物测量；目前没有构建这些产物，不能承诺包体大小或免安装启动。

当前 `scripts/build_release.py` 分发 Python/应用及依赖，不包含上述容器运行包。
后续实现需扩展发行清单、安装事务、doctor、升级恢复和卸载，而不仅是复制 `podman.exe`。

## 迁移涉及的现有接口

当前 `sandbox/docker.py`、`sandbox/environment.py`、`sandbox/build.py` 和安装器的镜像
构建、事务、卸载等路径直接调用 Docker。应增加引擎接口，分别适配：

- 引擎身份、能力探测和 `info` 字段，显式连接与镜像摘要。
- rootless 用户映射、私有副本写权限、SELinux 挂载行为、资源上限和只读根目录的 tmpfs。
- Windows/macOS machine 文件共享和路径转换；远程挂载源属于服务端，不能假设是客户端路径。
- GPU 参数、创建/删除容器及错误分类；复用取消清理协议，但单独测试 Podman 的客户端和 machine 辅助进程。

Podman 的命令与 Docker 相似并不足以证明这些契约一致。尤其宿主客户端 Job 不管理远端
容器，不能省略独立删除及核验。迁移后须重跑完整工具链、回写恢复、超时和取消验收。
挂载、只读目录等语义见 [podman run](https://docs.podman.io/en/stable/markdown/podman-run.1.html)。

## 能否在容器里使用宿主依赖

可以有条件导入，Docker 与 Podman 在二进制兼容性上没有本质区别。
当前 `SandboxPolicy.excluded()` 排除 `.venv`、`venv`、`node_modules` 等，后端只挂载筛选后的
工作副本与请求文件；没有“自动使用宿主依赖”或任意额外依赖挂载选项。

| 宿主依赖 | Linux 容器内的可用性 | 建议交付方式 |
| --- | --- | --- |
| 纯 Python/JS 源码、平台无关 wheel、资源文件 | 通常可用，仍须匹配语言版本与传递依赖，检查 OS 专属 API | 从锁文件重建，或导入明确选定的依赖包 |
| Windows `.exe/.dll/.pyd`、macOS Mach-O/`.dylib`、本机 Node 扩展 | 不能作为普通 Linux 依赖直接执行/import | 下载或在容器构建 Linux 对应版本 |
| Linux ELF/`.so`、Linux 原生 Python/Node 扩展 | 需 CPU 架构、解释器 ABI、libc 和系统库相容 | 优先镜像内重建；经验证的依赖可只读供应 |
| 整个 `.venv`/Conda 环境 | 不能承诺直接搬运，存在绝对路径、解释器及平台绑定 | 使用锁文件在镜像内重建环境 |
| pip/npm 包缓存、Linux wheelhouse | 可作为离线输入；须筛选目标平台，缓存未必完整 | 构建阶段导入，记录哈希与依赖版本 |
| CUDA 库、驱动、GPU 工具链 | 不能用复制宿主驱动代替 GPU 接入；还需容器运行时支持及版本匹配 | CUDA 镜像 + 已支持的 GPU 接入与专项验收 |

Python 官方明确指出虚拟环境通常不可搬移；wheel 标签区分解释器、ABI 和平台。
依据：[venv 可移植性](https://docs.python.org/3/library/venv.html)、
[依赖包兼容标签](https://packaging.python.org/en/latest/specifications/platform-compatibility-tags/)。
WSL 中准备的 Linux 依赖应按 Linux 行评估，不能与 Windows 本机依赖混为一谈。

推荐先支持**项目依赖镜像**：选定锁文件，在准备阶段构建 Linux 依赖，保留 Agent 镜像的
入口与能力标记，固化为项目镜像；执行任务时继续禁网、只读根目录和显式回写。
现有 Docker 后端可选择自定义镜像，但尚没有自动解析项目锁文件、构建和维护依赖镜像的流程。
准备阶段可以联网或使用离线 wheelhouse；当前执行阶段禁网、根目录只读，临时目录也不跨调用
保留，因此不能把“运行任务时临时安装依赖”当作持久方案。

若以后增加依赖挂载，应由宿主配置明确选择，只读挂载到专用目录，拒绝链接绕过及受保护路径，
设置容量限制并核对目标架构；运行产生的缓存另写临时目录。不能为解决依赖可见性而挂载整个
用户目录、原项目、凭据或引擎 socket，也不能把依赖同步混入工作区回写和备份恢复。

结论：可选 Podman 运行包与 Linux 项目依赖镜像都可实施；它们是两项独立建设。
更换引擎不会自动获得宿主依赖兼容性，也不能替代 Windows 实机通过的发布证据。
