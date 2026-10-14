# 发行验收标准与流程

[文档首页](index.md) · [构建与分发](distribution.md) · [开发指南](development.md)

发行验收针对确定提交生成的具体压缩包。源码测试通过、构建成功、某个平台可以安装，分别是不同的结论。`release-acceptance.yml` 将源码、内核、发行安装和升级证据汇总；只有所有必需项通过，才生成可供发布的原始资产与验收清单。它不自动创建或上传 GitHub Release。

## 必需标准

| 检查 | 通过条件 |
| --- | --- |
| 来源与版本 | 同一提交、版本、工作流运行及尝试；标签运行时标签必须为 `v<pyproject.toml 版本>` |
| 源码 | Ruff、默认回归、跨平台契约、Windows 隔离契约、锁文件导出、分发清单与文档检查通过 |
| Rust | 四平台预编译 wheel 构建、导入与契约验证通过；四个 Unix 完整包必须携带扩展，安装后再次验证导入 |
| native | Linux/macOS 四平台真实内核测试通过，必需用例及参数实例均实际执行，无跳过 |
| Docker | Ubuntu standard 镜像构建、真实工具、隔离、回写、超时、取消和 TUI 验收通过 |
| 完整包 | 五平台在匹配的真实系统/架构上安装；删除下载目录后启动、配置管理、重装、恢复和卸载通过 |
| 已安装程序 | 从安装环境导入程序，使用真实文件工具；Unix 另使用真实 native 命令，验证交付受阻、修复、人工验收、新进程恢复、队列与合并 |
| 升级 | 五平台使用同一较旧正式版本实际安装升级，保留配置、会话历史和等待任务，旧版已有的等待项不自动执行；尚无队列功能的旧版检查升级后为空队列；旧安装保留 |
| 资产 | 测试前后及汇总时 SHA256 一致；直接复制测试过的包，不重新构建 |

必需检查失败、跳过、取消、报告缺失、空测试集、用例身份或参数缺失，均不得放行。普通默认回归仍允许非目标平台/可选硬件测试跳过；严格的“零跳过”只作用于专门选择的必需验收集，不能把整个 pytest 套件中的平台跳过一律判为故障。

## 自动验收平台

| 发行目标 | 托管 runner | 包内 Python | 已安装执行模式 |
| --- | --- | --- | --- |
| linux-x86_64 | ubuntu-22.04 | `runtime/python.lock` 固定版本 | local 安装生命周期 + native 交付 |
| linux-arm64 | ubuntu-24.04-arm | 同上 | local 安装生命周期 + native 交付 |
| macos-x86_64 | macos-15-intel | 同上 | local 安装生命周期 + native 交付 |
| macos-arm64 | macos-14 | 同上 | local 安装生命周期 + native 交付 |
| windows-x86_64 | windows-latest | 同上 | local 安装生命周期 + local 人工交付 |

源码兼容回归继续覆盖 Python 3.11/3.13；发行包使用自己的受管解释器。Linux 内核和完整包验收共用 `.github/scripts/prepare-linux-isolation.sh`：安装 Bubblewrap/libseccomp，检查 `--disable-userns` 与 `--assert-userns-disabled`；系统版本不支持时，校验 SHA256 后构建固定的 Bubblewrap 0.11.0，保留 Ubuntu 22.04 的 libc 基线。脚本仅在一次性托管 runner 上解除阻挡用户 namespace 的 AppArmor 开关，不属于用户机器的安装步骤。

Linux 依赖准备按后端相同的 `/usr/bin:/bin:/usr/local/bin` 顺序查找 Bubblewrap；需要升级时替换实际选中的可执行文件，并对该路径验证版本和安全选项，避免 shell 检查新版而后端仍调用旧版。

macOS 通用内核验收显式使用 `standard` 配置，验证 CPU 工具和 Seatbelt 隔离，不要求托管 runner 提供 Metal 设备。真实 Metal 计算仍由 `tests/test_macos_metal.py` 的独立可选硬件测试验证；通用验收通过不代表 Metal 可用。

Windows native 仍为实验性：安全拒绝且清理确认可以满足隔离契约，但不算工具功能可用。Windows 内核作业另上传 `windows-native-capabilities-<Python版本>`，逐项记录 available / unavailable；完整功能承诺需在实际目标环境开启 `REQUIRE_WINDOWS_NATIVE_FULL_TOOLS=1` 验收。

## 触发与发布

1. 更新版本、锁文件和说明，提交全部变更。
2. 在 Actions 手动运行 **Release acceptance**，选择候选提交所在分支。`previous_tag=auto` 从最近 100 个发行记录中选择版本低于候选版的最高正式版本；也可填写一个明确的较旧正式标签。缺少可用基线或对应平台包时失败，不静默省略升级。
3. 工作流在同一运行中调用源码、Rust、Docker、native 工作流，再构建五平台包。Rust wheel 取自本次运行，不借用其他运行中的同名产物。
4. `Release gate` 无论前置作业成功与否都会尝试执行，检查所有作业状态、九份平台验收报告、JUnit 用例身份和资产哈希。
5. 从 `approved-release-<提交SHA>` 下载五个平台原包、各自 `.sha256` 与 `release-acceptance.json`。检查已知限制及平台报告。
6. 标签 `v*` 推送也会启动完整流程。标签验收通过后生成构建来源证明；正式发布使用此次标签运行验收过的文件，并确认 **Attest qualified tag assets** 成功。再将这些原文件和验收清单上传 Release，不重新打包。

手动候选验收不签发标签来源证明。来源证明需要仓库支持 GitHub artifact attestations；功能受仓库可见性和账户方案约束，详见 [GitHub 官方说明](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)。工作流不会修改仓库的标签保护、分支规则、Release 不可变设置或发布权限；这些仍由维护者配置。

报告绑定 `run_attempt`。重跑时使用 **Re-run all jobs**；只重跑失败作业会混入旧尝试的成功报告，最终门槛会拒绝。标签必须指向实际待发布提交，不能把其他提交的绿色结果用于当前包。

## 证据与本地复核

- `acceptance-native-<平台>`：`acceptance.json` 和 `tests.xml`。
- `acceptance-package-<平台>`：同上，加实际测试的发行归档；记录升级基线名称与 SHA256。
- `approved-release-<SHA>`：五个原包、校验文件及统一清单；清单记录测试数、证据摘要和前置作业结果。
- 默认回归额外保存 JUnit，Docker 保留原有 JSON 验收证据。失败作业也尝试上传诊断材料。

工作流 artifacts 有保留期限；发布时把统一清单一同上传 Release，并按需要留存详细验收 artifacts。

本地先准备匹配平台的完整包与旧版归档，安装开发依赖和实际隔离依赖。示例：

```bash
python scripts/release_acceptance.py run --profile native \
  --target macos-arm64 --output artifacts/native

REPO_AGENT_PREVIOUS_ARCHIVE=/path/to/previous.tar.gz \
  python scripts/release_acceptance.py run --profile package \
  --target macos-arm64 --archive /path/to/repo-agent-VERSION-macos-arm64.tar.gz \
  --output artifacts/package
```

`--output` 不应复用其他验收的目录。本地结果记录当前 Git HEAD，但不能证明未提交内容属于该提交；正式发行以干净 checkout 的 Actions 证据为准。普通开发回归不设置归档变量，不会安装或升级真实发行版。

## 离线与兼容承诺的边界

- Linux/macOS 发行安装、重装和升级的子进程在系统级断网环境中执行；使用独立 HOME、状态、运行时缓存与包内 Python。已安装的 native 验收使用后端自身的断网隔离，宿主仅运行固定模型脚本；macOS 不叠加两层 Seatbelt。
- Windows 使用 `--offline` 和本地归档验证，没有配置系统级断网；因此不能将其结果表述为“已通过系统级断网验收”。
- Unix 的 Rust 导入是必需检查，不能把可选组件安装失败的告警当作正式含 Rust 包验收成功。Windows 不分发 Rust 扩展。
- 发行安装验证不等于“离线构建 Docker 镜像”。安装测试中的 Docker 入口仍用模拟程序检查资源；真实 Docker 作业从源码构建镜像，可能下载构建依赖。
- 托管 runner 版本是已验收环境，不是最低系统版本证明。manylinux/macOS wheel 标签表示扩展兼容基线，不能替代整个发行包在最低 OS 版本上的验证。
- GPU、Metal/MPS、Windows Docker Desktop、完整 Windows native、多语言可选工具链和真实模型服务仍使用各自专项验收。没有对应证据时，发行说明应标为未验证或实验性，不能提升支持承诺。

## 维护入口

`release_acceptance.py` 管理必需用例、结构化证据和最终门槛；`release_baseline.py` 选择并下载升级基线；`installed_acceptance.py` / `upgrade_session.py` 在安装环境中运行固定模型或无模型验收。测试不读取真实模型密钥、不调用在线模型。

Python 运行/开发/构建依赖使用哈希锁文件；CI 固定 uv 0.7.18、maturin 1.9.6、Rust 1.89.0。工具升级应与对应回归一起提交。Actions 主版本标签及托管系统镜像仍可变化，当前并非逐字节可复现构建承诺。
