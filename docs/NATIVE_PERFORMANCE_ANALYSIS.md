# Native 模式性能排查：macOS 与 Linux / WSL

排查日期：2026-09-25。本文记录本次会话中的源码检查与 WSL 实测结果，尚未实施性能修复。

## 结论

当前 Linux native 的主要瓶颈位于 `LinuxNativeBackend._mount_policy()`：每次启动沙箱进程前，都递归遍历工作区、系统目录、Python 安装及依赖目录，为受保护文件生成遮蔽挂载规则。

macOS native 使用 Seatbelt 的路径和正则规则，生成策略时不枚举系统或 Python 目录中的所有文件。因此，两端虽然共用工具接口和大部分生命周期代码，策略准备的成本却完全不同。

当前 WSL 实测中，空命令的保护策略扫描约 16.09 秒，后续沙箱启动、命令执行及收尾合计约 54 毫秒；读取 README 的后续执行阶段约 271 毫秒。Linux 的执行阶段已经处于毫秒量级，十几秒的主要开销发生在启动执行进程之前。

## 排查环境与测量范围

| 项目 | 本次确认的信息 |
| --- | --- |
| Agent 源码目录 | `/home/haruhikage/repo_agent-main` |
| 测试工作区 | `/home/haruhikage/agent-test/repo-agent-test1` |
| 启动入口 | `~/.local/bin/repo-agent` 链接到源码目录下 `.venv/bin/repo-agent` |
| Python | `.venv/bin/python`，基于 `/home/haruhikage/anaconda3`；安装记录为 Python 3.14.6 |
| 工作区文件系统 | WSL 内的 ext4，非 `/mnt/c` |
| 默认 GPU 状态 | `auto` 检测到 WSL2 GPU，启用 CUDA 自检 |
| macOS 数据来源 | 用户反馈启动和工具调用通常为数十至数百毫秒；本次没有访问 Mac 进行完整实测 |

测量直接使用 WSL 内项目现有解释器，对实际后端方法增加进程内计时包装；未修改项目实现，未调用模型。实际工具验证使用读取 README 和执行 `/usr/bin/true`。

下文的“启动耗时”指 `NativeBackend(...)` 初始化，不是从 CLI 进程启动到交互界面就绪的完整耗时。数据是本次现场测量，不是冷缓存、跨机器或大样本基准，绝对数值不是稳定保证。

## 实测证据

### 后端初始化和工具调用

| 操作 | 总耗时 | 保护策略扫描 | 后续进程运行阶段 |
| --- | ---: | ---: | ---: |
| `auto` 初始化，检测到 GPU | 32.44 秒 | 两次：15.66 秒、16.01 秒 | 通用自检约 0.13 秒；GPU 自检约 0.62 秒 |
| `standard` 初始化 | 15.67 秒 | 15.52 秒 | 通用自检约 0.13 秒 |
| `standard` 读取 README | 15.57 秒 | 15.29 秒 | 约 0.27 秒 |
| `standard` 执行 `/usr/bin/true` | 16.15 秒 | 16.09 秒 | 约 0.054 秒 |

“后续进程运行阶段”是 `ProcessRunner.run()` 的计时，包含相关进程启动、输出收集和监督收尾，不是纯工具业务逻辑耗时。各项分别计时并经过四舍五入，总耗时也包含少量未单列步骤。

其他测量：

- 后端相关模块导入约 0.10～0.11 秒。
- 测试工作区的 `_check_workspace()` 约 0.3 毫秒。
- macOS `seatbelt_profile()` 策略文本生成在当前 WSL 中运行 1,000 次，中位耗时约 0.21 毫秒，生成文本约 4.4 KB。这仅验证策略生成函数的成本，不包含 Seatbelt 加载或 Mac 上的真实执行。

### Linux 保护策略扫描的目录分布

另一次对 `_mount_policy()` 的分项测量如下。耗时包含目录遍历期间的逐项保护规则判断；计数为按现有规则遍历到的条目，已被剪枝遮蔽的目录内部不在计数中。

| 扫描根目录 | 遍历到的文件数 | 耗时 |
| --- | ---: | ---: |
| `/home/haruhikage/anaconda3` | 243,563 | 7.33 秒 |
| `/usr` | 64,207 | 4.61 秒 |
| `/lib`，实际指向 `/usr/lib` | 15,708 | 3.45 秒 |
| 项目 `.venv` | 9,589 | 0.35 秒 |
| 测试工作区 | 15 | 0.0004 秒 |

该次分项测量总耗时约 15.82 秒，还包括 `/bin`、`/sbin`、少量配置目录等；为单独分析环境目录，未创建或扫描真实的临时工具代码副本。文件数包含别名路径的重复枚举，不能解释为唯一文件总量。

### 既有测试日志

测试工作区 `logs/` 下的 trace 日志与上述现象一致。例如 `session_20260925_192147_8da90e9a8bea.trace.jsonl` 中：

- 两次 `read_file` 分别约 21.70 秒、21.41 秒。
- 普通写入和编辑文件也普遍需要约 21～23 秒。
- 不同工具都出现接近的固定开销，符合每次调用前重复扫描的特征。

历史日志与当前复测的绝对耗时不同，具体原因未进一步测定；两者均显示相同的主要瓶颈。

部分模型轮次另有约 67～77 秒的耗时，并产生大量思考内容。这属于模型响应等待，不能用来解释普通文件工具固定二十多秒的执行耗时。

## 两种后端的实现差异

| 环节 | macOS native | Linux native | 性能意义 |
| --- | --- | --- | --- |
| 隔离入口 | Seatbelt / `sandbox-exec` | Bubblewrap namespaces + seccomp | Linux 步骤更多，但本次运行阶段仍为毫秒量级 |
| 受保护文件 | 将路径和正则规则交给内核 | 先枚举已有文件，再逐项遮蔽 | 本次差距的主要来源 |
| 系统及解释器目录 | 声明可读范围，不枚举目录内部文件 | 每次调用重新遍历 | 成本随环境文件数量增长 |
| 工作区检查 | 扫描硬链接 | 同样扫描，并拒绝特殊文件 | 两端共有；当前工作区很小 |
| 启动自检 | 通用隔离自检 | 通用自检，GPU 启用时再做 CUDA 自检 | 当前 WSL 初始化扫描两遍 |
| 文件工具 | 独立 Python worker | 独立 Python worker | 共用设计，不是本次数量级差异的来源 |
| 工具代码副本 | 初始化时复制可信工具代码 | 相同 | 不是每次工具调用都重复制 |
| 进程监督 | 通过 macOS 进程接口监督 | `/proc` 监督，另有 PID namespace | 存在平台差异，但计时未显示为主瓶颈 |

### macOS：生成规则，访问时检查

`sandbox/native.py` 的 `seatbelt_profile()` 生成目录允许规则、只读规则和受保护名称的正则拒绝规则。`_sandbox_command()` 将策略写入 `policy.sb`，随后启动 `sandbox-exec`。

策略准备主要取决于允许目录、保护规则及相关祖先路径的数量，不取决于 `/usr`、Homebrew 或 Python 安装中有多少文件。操作系统在进程实际访问文件时执行对应限制。

macOS 仍会执行共同的工作区硬链接扫描，因此大工作区也可能增加耗时；不能把整个 macOS native 调用描述为与文件数量完全无关。

### Linux：先全量搜索，再启动沙箱

调用链如下：

```text
NativeBackend.execute()
  → _check_workspace()                 工作区安全检查
  → _run()
      → LinuxNativeBackend._sandbox_command()
          → _mount_policy()            全量遍历工作区、系统、解释器、依赖
          → 生成只读挂载和保护路径遮蔽参数
      → ProcessRunner.run()
          → bwrap
          → Python linux_exec.py       安装 seccomp
          → 工具 worker 或目标命令
```

`read_file` 等文件工具也走这条路径，因此读取一个小文件同样需要先扫描几十万个环境文件。Linux 还额外启动可信 Python launcher 加载 seccomp，再执行目标程序；这部分已包含在较短的进程运行阶段内。

### Anaconda 与别名重复扫描

两端都将 `sys.base_prefix` 纳入读取范围，但只有 Linux 会枚举其内部文件。当前 `.venv` 来自 Anaconda，因此整个 Anaconda 安装目录进入 Linux 扫描范围，约占本次扫描时间的近一半。

`_outermost()` 保留路径原有写法，并按字面父子关系裁剪目录。这使 `/lib` 和 `/usr` 同时保留；当前系统 `/lib` 指向 `/usr/lib`，造成部分内容重复遍历。

扫描结果可以考虑复用，但挂载路径仍需正确覆盖：不能直接删除别名对应的保护规则，否则同一内容可能经另一路径可见。

### GPU 放大初始化成本

macOS 当前不探测 NVIDIA CUDA。Linux 默认 `auto` 在检测到可用 GPU 后，执行通用隔离自检和 CUDA kernel 自检；两次 `_run()` 都会重新生成挂载保护策略。

所以当前约 32 秒的后端初始化主要是两次各约 16 秒的扫描叠加。GPU 自检进程本身约 0.62 秒，不是启动延迟的主要部分。

## 优化方向及边界

1. **优先优化 `_mount_policy()`。** 将变化频繁的工作区与运行环境目录分开处理，研究可复用的运行环境扫描结果，并设计可靠的变更检测和失效机制。
2. **复用别名目录的扫描结果。** 区分实际目录身份和最终挂载路径，减少重复遍历，同时保持每条访问路径的保护覆盖。
3. **缩小运行环境范围。** 使用较小、专用的 Python 环境可减少 Anaconda 带来的扫描量，但这是缓解措施；每次扫描整个 `/usr` 等目录的问题仍然存在。
4. **补齐性能观测。** 分别记录工作区检查、保护策略扫描、沙箱启动/执行和清理耗时，避免把策略准备成本统称为“工具执行慢”。
5. **扩展基准覆盖。** 现有 `scripts/benchmark_native_scan.py` 只比较工作区检查算法，明确不包含 mount-policy scan，无法验证当前主瓶颈是否得到修复。

保护语义必须保留：macOS 的动态路径规则能阻止访问新创建的受保护名称；Linux 当前遮蔽的是调用开始时已存在的路径，下一次调用再重新扫描。文件工具本身另有名称保护。项目文档已说明这项平台差异。

因此，不能直接跳过所有扫描，或永久缓存第一次扫描结果。只读挂载限制的是沙箱内写入，不代表宿主机上的运行环境永远不会发生变化。缓存失效、Git 只读例外、新增保护文件、受保护符号链接和无法扫描的目录都需要纳入设计与验证。

暂不需要 GPU 时，可以使用：

```bash
repo-agent --sandbox native --sandbox-profile standard
```

本次后端初始化由约 32.44 秒降至 15.67 秒，但普通工具调用仍需约 15～16 秒，无法解决主要问题。优化扫描之后，剩余运行阶段的实测值说明存在恢复到毫秒量级的空间；这不是尚未实现的优化效果承诺。

## 源码定位与后续验证

以下行号对应排查时版本，后续修改可能变化：

| 位置 | 内容 |
| --- | --- |
| `sandbox/native.py:35`，`seatbelt_profile()` | macOS 策略文本生成 |
| `sandbox/native.py:282`，`_sandbox_command()` | macOS 策略落盘及 sandbox-exec 启动参数 |
| `sandbox/native.py:351`，`execute()` | 两端共有的调用前工作区检查 |
| `sandbox/linux_native.py:22`，`_outermost()` | 保留别名写法的路径裁剪 |
| `sandbox/linux_native.py:60`，`_read_paths()` | 系统、Python 前缀和依赖读取范围 |
| `sandbox/linux_native.py:88`，`_mount_policy()` | 本次主要性能瓶颈 |
| `sandbox/linux_native.py:140`，`_sandbox_command()` | 每次执行前生成挂载策略 |
| `sandbox/linux_native.py:219` / `271` | 通用自检 / GPU 自检 |
| `sandbox/linux_exec.py` | Linux seccomp launcher |
| `scripts/benchmark_native_scan.py:78` | 现有基准不覆盖挂载策略扫描 |
| `docs/native-sandbox.md` | 两个平台的保护语义与限制 |

后续复测应使用同一工作区和解释器，分别比较 `auto` / `standard` 初始化、批量 `read_file` 和 `/usr/bin/true`；记录各阶段中位数及长尾，并注明冷热缓存和环境规模。任何扫描缓存或去重修复，都应同时验证已有 native/Linux 保护测试及真实沙箱行为。

另一个观测缺口：`cli/main.py` 在第 287 行附近创建 native 后端，在第 326 行附近才创建 `Tracer`。因此现有 `session_start` 日志不覆盖之前的 native 初始化，不能用它与首个任务之间的间隔直接推算启动耗时。工具侧的挂载策略准备也发生在 `ProcessRunner.run()` 之前，单看子进程 `duration_ms` 或执行超时不足以描述整个工具调用的等待时间。