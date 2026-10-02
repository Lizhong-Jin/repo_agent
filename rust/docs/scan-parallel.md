# 工作区预检与隔离策略的有限并行

rust-backend 0.5.0 增加 `SCAN_PARALLEL_VERSION=1`，保持策略 API 1、文件系统 API 2。
仅在选择 Rust 后端时生效；Python 后端以及文件工具的 find/search/read 顺序不变。

## 配置

```dotenv
AGENT_NATIVE_SCANNER=rust
AGENT_SCAN_WORKERS=2
```

`AGENT_SCAN_WORKERS` 是每次扫描的并发上限，默认或空值为 2；1 使用原串行实现，
允许范围为 1～8。可写入用户配置或项目 `.env`，也可以设置环境变量；配置命令支持
查看、保存、校验。Rust 在每次扫描开始时再次检查非法值，出错拒绝执行。
此选项需要 0.5.0 或更新扩展，旧扩展不会提供此能力。

扫描首先在调用线程处理目录，只有出现至少两个待扫描目录时才创建线程池。
单个大目录、空树和只有单条目录链的扫描不会因为该选项启动工作线程；目录内部仍按
256 项分块处理。粒度是目录，不是每个文件，因此单个超宽目录不会得到多核加速。

## 两个入口

- **Linux 工作区预检及系统/Conda 读取根策略扫描**：`src/policy_scan/engine.rs`。
  工作线程枚举目录、读取元数据、检查工作区特殊文件/硬链接、生成名称分类事实。
  协调线程处理保护路径、剪枝、逻辑与规范化路径、别名缓存、挂载分组和掩蔽输出。
  根目录依原计划依次处理，根内部并行；根之间共享单次调用的事实缓存，避免并行扫描
  别名根时重复读取相同目录。工作区仍不会错误复用外部根事实来跳过安全检查。
- **macOS 工作区预检**：`src/filesystem/mod.rs`。
  协调线程通过共享的 32 项目录句柄缓存相对打开目录，向任务移交独立 OwnedFd。
  工作线程用独立 DIR 流枚举，对每个条目执行 nofollow fstatat，并检查普通文件硬链接。
  符号链接不递归；macOS 仍允许 FIFO，与 Linux 预检的特殊文件限制保持区别。
- 共用执行器：`src/scan_pool.rs`。线程池仅存活于本次调用，不跨扫描缓存文件系统事实。

## 顺序、完成屏障和错误

任务按完成顺序归并，不等待原遍历顺序。成功返回必须同时满足：待发现目录已全部处理、
所有在途任务已收取、线程已回收、最终安全校验已完成。不能利用部分成功结果启动隔离进程。

Linux 仍在扫描完成后复核根的 canonical/dev/ino、挂载布局及保护挂载点的符号链接状态。
掩蔽结果继续使用原 outermost 去重与排序。并行模式的 Git 路径改为按 Python 解码后的
路径字符串排序，保留嵌套 Git 路径、不做 outermost 剪枝，避免线程完成顺序成为挂载顺序。
它与串行扫描具有相同路径集合，但列表顺序不承诺与原 DFS 相同。祖先先于自己的后代。

多个违规文件并存时，首个报告路径可能随调度变化；错误类型、工作区失败关闭、外部根
可掩蔽权限拒绝等语义保留。不同观察时刻仍不构成文件系统快照。

协调线程定期检查 Python 信号和原取消上下文，等待结果时每 10 ms 唤醒检查。工作线程
只读取原子停止标志，不并发调用 Python 取消对象；非 ASCII 名称仍通过 Python lower
保持 Unicode/surrogateescape 语义。扫描主体、等待与 join 均在释放 GIL 的区间内，
通道锁不跨越文件 I/O、Python 调用或结果发送。

协调线程收到失败/取消后停止派发，Drop 设置停止标志、关闭任务发送端、join 所有线程，
再传播原始错误；排队任务/未收取结果也会随通道释放。线程 panic 转成失败结果，不会
缺少回复而无限等候。阻塞中的系统调用不能由停止标志强制中断，因此慢文件系统上的
取消延迟不保证为 10 ms；回收会等这些调用返回。

## 资源边界与诊断

- 在途窗口最多 W，涵盖排队、执行、已完成但未收取的任务。只有协调线程派发，工作
  线程不会递归向任务队列追加工作。结果通道虽然采用非阻塞发送，但在途窗口限制其
  最多保存 W 个目录结果，不会出现工作线程等待协调者派发而相互阻塞。
- Linux 每个任务至多一个目录 fd；扫描期间上界 W。macOS 缓存是全扫描共享的 32 项，
  不按线程复制；加根 fd、在途目录 fd/枚举流与打开瞬间临时 fd，上界为 `33 + 2W`。
  所有上界均为本次扫描额外持有的句柄，不包括进程其他文件/库。
- 每个线程复用一个 256 项枚举块与名称缓冲；路径节点仍按引用释放。macOS 并行路径
  为每个目录任务保存一个错误定位路径，但不会为每个普通文件构造完整路径。
- **整个扫描内存并非常数**：待发现目录 frontier、每目录保留的子目录/保护事实、结果集、
  Linux 别名缓存仍随树大小增长；当前没有为这些结构设置全局字节配额。并行额外保留
  最多 W 个目录的结果，超宽目录仍可能产生较大的结果对象。

`profile_workspace` / `RustPolicyScanner.last_diagnostics` 增加 `workers`（配置上限）和
`in_flight_peak`（含已完成未收取的任务峰值，串行目录工作计为 1）。`directory_handles_peak`
在并行模式是保守高水位上界，可能大于真正同时打开的数量。枚举块指标为单块峰值，
不是全线程缓冲合计。`pending_tasks_peak` 仅统计尚未派发目录，与在途窗口分开。

策略计数保持可与 Python 对照；失败时只覆盖已汇总工作。并行阶段的 enumeration/rules/
metadata/workspace_validation 毫秒是累计工作耗时，不能相加后与 scan_ms 墙钟比较。
并行模式 `mapping_ms` 表示总墙钟减去协调线程等待结果的时间（包含启动、归并、同步
扫描、最终校验及清理），不再由总时间减各线程阶段时间；串行模式继续原口径。
挂载分组计数相同，但分组列表的发现顺序可能不同。

## 测试与测量

```sh
python -m pytest -q tests/test_rust_policy_scan.py tests/test_scan_parallel.py
python scripts/benchmark_scan_parallel.py --workers 1 2 4 --repeats 11
# 将临时树放在需要测试的真实文件系统上：
python scripts/benchmark_scan_parallel.py --temporary-parent /path/to/test-filesystem
# 单宽目录与深目录的内存/时间对照：
AGENT_SCAN_WORKERS=1 python scripts/benchmark_scan_memory.py --operations policy workspace
AGENT_SCAN_WORKERS=2 python scripts/benchmark_scan_memory.py --operations policy workspace
```

策略差分覆盖 1/2/4 线程；资源上限覆盖 1/2/4/8；包括别名、保护路径、Unicode、拒绝访问、
特殊文件、硬链接、多次取消后的 fd 回收、无序完成、worker panic。基准单独子进程测量，
临时生成并删除数据，含每次调用的线程启停，使用热缓存中位数。实际提升取决于目录分布、
元数据延迟及文件系统争用，不能由 macOS 合成树推断 WSL2/Conda 的收益。
