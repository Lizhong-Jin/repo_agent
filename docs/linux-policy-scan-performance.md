# Linux native 策略扫描与自检优化

实现保持现有文件保护边界，优化单次策略生成，合并重复工作区遍历与启动自检，并提供分阶段计时。没有引入跨调用的文件状态缓存，也没有引入常驻沙箱进程。

## 实现

- `sandbox/linux_policy.py` 将纯配置 `PolicyPlan` 和单次文件系统扫描 `PolicyScan` 分开。静态模板包含扫描根、挂载根、固定保护路径索引和需要保护的父目录；工作区、只读路径或固定保护路径配置变化时重建。
- 扫描用 `scandir` 批量读取每个目录，再进行名称分类。普通文件立即丢弃；只保留目录、敏感名称及可能匹配固定保护路径的条目。跨调用不保留 `DirEntry`、文件存在性、权限或符号链接目标。
- 系统目录别名按规范路径复用目录枚举和名称分类，支持 `/usr` 已包含 `/usr/lib`、而 `/lib` 又指向 `/usr/lib` 的情况。不单凭 inode 相同合并不同挂载视图。工作区及其别名不复用扫描观察结果。
- 每个挂载路径独立应用固定保护路径和 Git 读取规则，不能因为扫描过 `/usr/lib` 就省略 `/lib` 的遮蔽挂载。固定保护路径的名称索引避免对每个普通文件遍历全部保护路径。
- 每个条目名称只转小写一次，不再逐文件构造 `Path`。挂载路径剪枝改为已选祖先集合查询，减少大量遮蔽条目之间的两两比较。
- 进入子目录前重新检查符号链接，扫描结束时复核扫描根的目标与 inode；发现变化拒绝执行。无权列出的系统目录仍遮蔽整个子树，工作区权限错误及其他 I/O 错误仍拒绝执行。

可信工具代码原本就在后端初始化时复制一次并复用；本阶段保持这一行为。每次执行仍有独立进程隔离与临时目录。通用自检和 GPU 自检现在共用一次策略生成和沙箱启动，在沙箱内分别启动可信子进程，超时仍为 30 秒和 90 秒，外层总超时为 125 秒（无 GPU 为 35 秒）。第一项失败时不运行 CUDA 检查。设备枚举、CUDA 运算结果和选定 GPU UUID 校验均保留。

这仍不是原子文件系统快照，也没有增强为按名称拦截未来文件的内核策略。Linux 上同一次进程调用中创建的敏感名称仍受原有边界约束，详见 [文件访问限制](native-sandbox.md)。

## 工作区遍历与启动次数

Linux 每次实际 `_run` 都在 `_mount_policy` 内完成工作区安全检查。启动与工具调度阶段只检查目录布局，不再提前进行另一遍文件扫描。一次目录枚举同时支持硬链接/特殊文件检查和保护规则生成；被遮蔽的 `.git`、`logs`、固定保护目录仍继续安全检查，工作区中的只读解释器目录也不跳过。macOS 保留独立的工作区检查。

| 启动配置 | 合并前策略生成次数 | 合并后 |
| --- | ---: | ---: |
| 无 GPU | 1 | 1 |
| 全部 GPU / auto 检测到 GPU | 2 | 1 |
| 指定单张 GPU，需要先枚举 | 3 | 2 |

每次实际沙箱执行仍生成新的策略；这次合并没有增加跨调用缓存。

## 计时字段

计时保存在后端诊断属性中，不附加到模型的工具输出，也不记录文件内容或命令参数：

| 属性 | 内容 |
| --- | --- |
| `startup_metrics` | 后端初始化总耗时、可信代码复制、工作区检查、每次自检运行的计时 |
| `last_tool_metrics` | 最近一次工具调用总耗时、工作区检查、该工具产生的沙箱运行列表；轻量文件工具的运行列表为空 |
| `last_run_metrics` | 请求和策略准备 `preparation_ms`、进程执行及监督清理 `process_ms`、包含临时目录清理的 `total_ms` |
| `last_policy_metrics` | 扫描总耗时、目录枚举、分类、路径映射、挂载参数生成，以及实际扫描/复用目录数、分类条目数和遮蔽条目数 |

`enumeration_ms` 包括读取目录和构造 `DirEntry` 列表；`rules_ms` 包括名称匹配与条目类型判断，后者在部分文件系统上也可能触发元数据查询，不能将两者简单解释为纯 I/O 与纯 CPU。计时在每个目录分段进行，避免逐文件读取时钟。`workspace_validation_ms` 是使用本次目录条目执行安全检查的耗时；`workspace_entries_checked` 和 `workspace_directories_scanned` 给出检查数量。`mapping_ms` 是扫描总耗时扣除枚举、分类和工作区验证后的部分，包含路径规则应用、根目录验证和结果剪枝。`materialization_ms` 单独计量挂载参数生成，不包含在 `scan_ms` 中。

Linux 的独立 `workspace_check_ms` 现在为零，工作区验证已计入每次运行的 `policy.workspace_validation_ms`，后者不包含共享的目录枚举成本，不应再加到总时间中。`startup_metrics.checks` 分别记录 `isolation_ms` 和可选的 `gpu_ms`。

`complete` 表示相应阶段正常返回，不表示被执行程序退出码为零；应同时查看正常工具结果。失败扫描也保留已收集的阶段计时。原有进程 `duration_ms` 和超时语义保持不变，策略准备尚不受进程执行超时控制。

## 可复现基准

仅扫描，不需要 bubblewrap，也不会修改指定的工作区或系统目录：

```bash
.venv/bin/python scripts/benchmark_linux_policy.py --files 100000 --repeats 7
.venv/bin/python scripts/benchmark_linux_policy.py \
  --workspace . --read-path /usr --read-path /lib \
  --read-path /bin --read-path /sbin --repeats 7
```

使用 Conda 或独立工具链时，继续添加 `--read-path` 指定实际解释器目录。参数保留符号链接的路径写法，不要提前把 `/lib` 替换成 `/usr/lib`，否则测不到别名去重效果。`--git-read` 可测量 Git 只读规则。

脚本内保留优化前的扫描和名称匹配算法；对照分支先独立检查工作区，再生成保护规则，当前分支在策略扫描中合并检查。每轮同时检查新旧保护结果一致。预热文件系统缓存后交替运行两种算法，输出中位数与 p95；这不是磁盘冷缓存测试。若扫描期间外部修改输入导致结果不同，基准会停止。

在具备 bubblewrap、libseccomp、用户命名空间权限的 Linux/WSL2 主机，还可测量真实启动及空命令：

```bash
.venv/bin/python scripts/benchmark_linux_policy.py \
  --workspace . --end-to-end --sandbox-profile standard --repeats 7
.venv/bin/python scripts/benchmark_linux_policy.py \
  --workspace . --end-to-end --sandbox-profile auto --repeats 7
```

`end_to_end` 部分使用后端真实系统、解释器和 GPU 挂载配置，记录初始化以及反复执行 `/usr/bin/true` 的全流程。`auto` 检测到 GPU 时包含真实 CUDA 自检；没有 GPU 的主机不会获得 GPU 基准数据。顶层扫描对照部分只扫描指定的 `--workspace` 和 `--read-path`，与全流程测量范围分别标注。

## 第一阶段的历史测量（工作区遍历合并前）

十万普通文件分布在模拟系统库和解释器目录，附带目录别名、敏感文件和 Git 元数据。两种算法的遮蔽路径集合一致，每组运行 7 次：

| 测量环境 | 原扫描中位数 | 优化后中位数 | 原 p95 | 优化后 p95 |
| --- | ---: | ---: | ---: | ---: |
| Linux，Python 3.12.14，Docker 内普通用户 | 1813.32 ms | 37.04 ms | 1860.10 ms | 38.58 ms |
| macOS，Python 3.13.5，运行相同 Linux 扫描算法 | 2284.87 ms | 51.62 ms | 2383.05 ms | 55.35 ms |

优化后的每轮实际枚举 404 个目录，201 次目录视图直接复用，分类 100407 个条目。内存中的跨调用扫描缓存为零。这里的 Docker 仅提供 Linux 验证环境，native 后端不增加 Docker 依赖。

此外，在同一 Linux 镜像中只读扫描实际 `/usr`、`/bin`、`/sbin`、`/lib`、`/lib64`（仅包含存在的路径），运行 5 次：原算法中位数 **738.62 ms**、p95 **742.47 ms**；优化后中位数 **40.51 ms**、p95 **41.45 ms**。优化后枚举 4526 个目录、复用 317 次目录视图、分类 38742 个条目，新旧保护集合一致。这是镜像的系统目录数据，不是 WSL2 主机数据。

这些数据只说明本次目录结构中的策略扫描收益，不能直接推算原报告中 WSL2 的 16 秒会降到多少。真实目录数量、文件系统、权限、解释器布局和 GPU 启动都会影响结果，应在目标 WSL2 主机运行全流程基准。

## 回归范围

新增测试覆盖原算法结果对照、系统目录与子目录别名、按视图配置的保护路径、Git 只读模式、新增深层敏感文件、权限恢复、不可列出但可读取的目录、符号链接替换、固定配置变化、稀疏缓存释放和计时失败路径。GPU 自检计时通过替身进程验证通用自检与 GPU 自检均被记录，现有 GPU 设备选择及隔离规则测试继续保留。

本次 Linux 验证在普通用户、无网络的临时容器中完成扫描与策略回归；该镜像没有 bubblewrap 或 NVIDIA GPU，因此没有在这里运行真实 Linux 用户命名空间和 CUDA 内核测试。公共计时改动另外通过了 macOS 的 20 项真实原生隔离测试。目标 WSL2/Linux 主机的内核验证可继续使用：

```bash
RUN_SANDBOX_LINUX_TESTS=1 .venv/bin/python -m pytest -q tests/test_linux_native.py
RUN_NATIVE_GPU_TESTS=1 .venv/bin/python -m pytest -q tests/test_linux_native_gpu.py
```
