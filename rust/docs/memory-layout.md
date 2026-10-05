# 扫描内存布局与测量

0.4.0 在保持 Linux API_VERSION=1、文件系统 FILESYSTEM_API_VERSION=2 的前提下增加
内部紧凑元数据与诊断能力。公开 `stat/stat_many` 仍提供完整 stat_result；文件工具通过
可选 `scan_metadata` 能力读取不可变的七字段观察值。0.3.0 API 2 扩展仍可由适配器使用
原 Rust stat_many 路径；运行错误不会触发 Python 扫描回退。

0.5.0 进一步接入预检/策略有限并行，新增线程与资源指标的解释见
[有限并行扫描](scan-parallel.md)。以下结构在并行模式下由各线程复用或由协调者管理。

## 数据布局和生命周期

- `src/directory_batch.rs`：256 项目录块，名称放在可复用的连续字节区，条目存偏移和
  d_type。块保留 readdir 顺序及原始路径字节。Linux 策略与 macOS 工作区预检都逐块
  处理，避免先为整目录创建独立名称对象。需要留存的保护事实/子目录仍会保存。
- `src/path_nodes.rs`：父节点编号、单份 NUL 结尾名称与显式引用计数。待扫描任务和
  macOS 目录缓存存编号；已完成节点迭代释放，空槽复用，父链仍被引用时保留。
  不是整棵目录树常驻内存。槽位容量保持本次扫描高水位，名称随节点释放。
- Linux 队列用同一相对节点链重建逻辑路径和规范化路径，仍保留两者的规则语义。
  可复用 PathBuf 存当前路径；macOS 成功预检无需逐文件创建完整路径，出错时才重建。
  节点编号不代替 fd 身份、符号链接拒绝及挂载别名复核。
- Linux 热点统计改用枚举索引的固定数组和字段存在位图，导出时保持原来的键和类型；
  ASCII 名称小写匹配复用缓冲区，非 ASCII 仍遵循当前 Python 解释器的 Unicode 规则。
- `src/filesystem/metadata.rs` 的紧凑结果只保存 mode、dev、ino、nlink、size、mtime_ns、
  ctime_ns。名称以一个不可变 Python bytes 块跨语言传递，Rust 在该块内直接借用
  NUL 结尾的组件进行 fstatat，避免每个名称重复创建 Vec/CString。
  结果为只读 PyO3 对象，避免完整 tuple、额外字段 dict、stat_result 的多层中间对象。
  实际文件打开后的 fstat、普通文件/硬链接检查及读取前后签名比较继续执行。

文件工具的 `names()` 仍返回完整名称列表，搜索仍按原语义排序；不能把预检的 256 项
工作块理解为所有扫描内存均为常数。待遍历目录、保护结果、别名事实缓存和排序名称
仍可能随数据规模增长，新增指标用于分别观察这些成本。

## 日常测量

```sh
python scripts/benchmark_scan_memory.py
python scripts/benchmark_scan_memory.py --operations policy-read --files 20000
python scripts/benchmark_scan_memory.py --operations find search --files 2000
```

每个操作在单独子进程中测量，数据树由父进程创建和删除；预热后计时，tracemalloc
单独再运行一次，不把它的开销算进耗时。操作含义：

- `policy`：Linux 工作区策略扫描，包含工作区逐文件安全检查。
- `policy-read`：空工作区加独立读取根，主要观察类似系统/Conda 读取树的名称扫描。
- `workspace`：macOS 风格工作区硬链接预检。
- `metadata`：每批 128 项的目录 reader 元数据观察，包含 Python 路径保护与结果使用。
- `find/search`：完整 native 文件工具调用，不包括隔离进程启动和模型请求。

普通 wheel 的 Rust 分配统计显示 null；不能将 null 理解为没有分配。
`process_peak_rss_bytes` 是包含导入、Python、Rust 和 libc 的进程高水位，不能通过
两次高水位相减得到操作的准确峰值。`python_traced_peak_bytes` 来自 tracemalloc，
不覆盖 Rust 原生堆；精简 PyO3 对象所用的 Python 分配仍计入它。

## 可选 Rust 分配测量构建

`allocation-profile` 是开发用 Cargo feature，普通发行 wheel 不启用它，不承担全局
原子分配计数开销。以下在 Linux/macOS 源码环境执行，使用同一个 Agent Python：

```sh
profile_dir=$(mktemp -d)
CARGO_TARGET_DIR="$profile_dir/target" PYO3_PYTHON="$(command -v python)" \
  cargo build --manifest-path rust/Cargo.toml --release --locked \
  --features extension-module,allocation-profile
mkdir "$profile_dir/module"
# macOS；Linux 将 dylib 改为 so：
cp "$profile_dir/target/release/librust_backend.dylib" \
  "$profile_dir/module/rust_backend.abi3.so"
python scripts/benchmark_scan_memory.py --extension-dir "$profile_dir/module"
```

构建中间文件放在工作区外，避免 Cargo 硬链接干扰 native 检查。测量库不安装进日常
环境，也不作为发行 wheel。用旧/新两份 module 目录可以对比版本。

`allocation_stats(reset_peak=False)` 返回扩展 Rust 分配器的累计 allocation 次数、
reallocation 次数、累计请求字节、当前存活请求字节、存活请求字节高水位。基准取
调用前后的计数差，并在操作前重置高水位。累计请求字节包括 realloc 的新请求大小，
不是“实际复制了多少内存”；高水位不包括 allocator 元数据、Python 和 libc 内部堆。
这些计数是进程内扩展级别的，不能在同时运行其他 Rust 扩展任务时归因给单次扫描。

## 结构指标和元数据转换耗时

`RustPolicyScanner.last_diagnostics` 提供最近一次调用的额外结构诊断，保持已有
跨后端 `ScanResult.metrics` 契约不变。Rust `profile_workspace(root)` 返回同类诊断：

| 字段 | 测量范围 |
| --- | --- |
| path_nodes_created / path_nodes_peak | 创建节点总数 / 同时存活节点峰值 |
| path_name_copied_bytes | 节点保存名称复制的字节，含 NUL；不代表所有字符串复制 |
| path_materialized_bytes | Linux 两类完整路径及 macOS 并行目录任务路径的重建字节量；不含输出结果路径 |
| pending_tasks_peak | 待扫描目录任务峰值 |
| directory_handles_peak | 串行 fd 峰值 / 并行保守高水位上界，非进程全部 fd |
| enumeration_entries_peak | 当前枚举工作块条目峰值，最多 256 |
| enumeration_buffer_bytes_peak | 单个可复用枚举缓冲区容量峰值，非全线程合计 |

失败时策略诊断可能只覆盖已完成阶段，不是完成证明；仍以原来的 complete/error 契约
判定成功。macOS profile_workspace 与普通预检一样，遇到错误或取消会抛出异常。

`profile_metadata(fd, packed_names)` 额外执行一次观察，分开报告原生验证/句柄固定/
fstatat 的 `observe_ms` 和创建精简 Python 结果对象的 `python_conversion_ms`。
不包括 Python 适配器的名称打包、策略检查和错误对象转换；benchmark 的 metadata
总耗时包含这些上层成本。所有 profiling 接口仍保留取消与非法名称校验。

<a id="多线程按原顺序提交的后续设计"></a>

## 文件工具内部扫描的当前边界

单次文件工具的路径枚举、文本读取、排序与预算推进仍由 Python 协调，Rust 提供目录
访问和批量元数据。当前没有单次 find/search/read 内部的有序多线程文本检索管线。
预检和隔离策略扫描已实现的目录并行、批次与资源上限见[有限并行扫描](scan-parallel.md)。

Runtime 可以并发执行多个独立的只读工具调用，每次调用保留自己的读取预算和文件
访问对象；这与一次扫描内部是否并行是两个层次。详见[工具并发策略](../../docs/tools.md#并发调度策略)。
