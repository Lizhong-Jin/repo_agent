# Web 页面读取

[返回 README](../README.md) · [Web 搜索](web-search.md)

`web_fetch` 获取公开 HTTP(S) 网页，将正文存为当前进程内的不可变快照，再按行返回。第一版支持 HTML、纯文本和 JSON；不执行 JavaScript，不登录，不读取 PDF，不自动下载页面资源。

## 启用

```bash
repo-agent config set AGENT_WEB_FETCH_ENABLED true
repo-agent --sandbox native
```

默认关闭；配置后重新启动 Agent。启动时显示 `[Web 读取]` 表示已注册。local、native 和 Docker 模式均由主进程提供此工具，命令、Python 和语言服务器的隔离规则保持不变。网页读取不需要 Brave 或其他搜索密钥；要同时搜索，另行配置 `web_search`。

## 首次读取与缓存分页

每批最多 3 个目标。首次通过 URL 读取，默认从第 1 行返回最多 80 行；可在同一目标中设置 `start_line` 和 `line_count`：

```json
{
  "targets": [
    {"url": "https://docs.python.org/3/library/asyncio-task.html", "line_count": 80}
  ]
}
```

成功项包含：

| 字段 | 含义 |
| --- | --- |
| `index`、`success` | 输入位置（从 0 开始）及该项成败 |
| `ref_id` | 当前进程内的不可变快照引用 |
| `requested_url`、`final_url` | 原始 URL、经过校验和跳转后的最终 URL |
| `title`、`fetched_at`、`content_type` | 标题、获取时间（UTC）、内容类型；获取时间不是发布时间 |
| `content` | 实际提取的正文，不生成摘要 |
| `start_line`、`end_line`、`total_lines` | 从 1 开始的正文行号；末行包含在结果中 |
| `truncated`、`next_start_line` | 本次是否只返回部分正文，以及继续读取的位置 |
| `links`、`links_truncated` | 去重的绝对链接列表，以及是否有链接因预算被省略 |
| `long_lines_wrapped` | 是否将超过 512 字符的原始长行分段 |

复制返回的 `ref_id`，用 `next_start_line` 继续读取：

```json
{
  "targets": [
    {"ref_id": "doc_0123456789abcdef0123456789abcdef", "start_line": 81, "line_count": 80}
  ]
}
```

示例引用仅展示格式，实际应使用工具返回的值。`url` 和 `ref_id` 必须且只能提供一个，模型不能指定方法、Cookie、认证头或代理。`line_count` 为 1–200；正文超过整批预算时，实际行数可能更少。`next_start_line=null` 表示已读到末尾；从中间开始读取时，`truncated` 仍为 `true`，因为本次并未返回整个快照。空页面返回空正文、`total_lines=0`、`start_line=1`、`end_line=0`。

行号基于首次提取后固定的正文，不是原始 HTML 行号。超长行会稳定分段，代码块标记会保留，但分段后的正文不能当作原始源码逐字复制。缓存读取不会访问网络或重新提取内容。

## 缓存与错误

每次显式读取 URL 都重新抓取并生成新的 `ref_id`，即使 URL 相同。旧引用永远保留旧内容，直到失效。

缓存仅在当前 WebBackend 实例内共享，默认有效期 30 分钟（读取不延长），最多 32 个快照，按序列化 UTF-8 大小合计最多 32 MiB。达到容量限制时淘汰最久未使用的快照。已知过期或淘汰的引用返回 `REF_EXPIRED`，未知或其他进程的引用返回 `REF_NOT_FOUND`；失效引用的记录最多保留 128 个，较旧记录可能返回 `REF_NOT_FOUND`。退出 Agent 会清空缓存，恢复历史会话不会恢复网页快照。任何引用失效都不会静默重新下载，须显式提供 URL 才会产生新快照。

外层沿用 `ToolResult`。`success=true` 表示批次处理完毕，不代表每一项都成功；逐项查看 `data.results[].success`。成功和失败保持输入顺序，失败通过 `index` 对应请求。错误含 `code`、固定说明和 `retryable`，HTTP 错误另含状态码，不回传远端错误正文或异常详情。

错误包括 `BLOCKED_URL`、`TIMEOUT`、`NETWORK_ERROR`、`HTTP_ERROR`、`RATE_LIMITED`、`REDIRECT_LIMIT`、`UNSUPPORTED_CONTENT_TYPE`、`UNSUPPORTED_CONTENT_ENCODING`、`UNSUPPORTED_ENCODING`、`INVALID_RESPONSE`、`RESPONSE_TOO_LARGE`、`CONTENT_TOO_COMPLEX`、`OUTPUT_TOO_LARGE`、`INVALID_RANGE`、`REF_EXPIRED`、`REF_NOT_FOUND` 和兜底 `FETCH_ERROR`。整体输入结构错误返回外层 `INVALID_ARGUMENTS`，后端不可用返回 `FETCH_UNAVAILABLE`。不自动重试。

## 正文与访问边界

HTML 优先提取明确的 `main`、`role=main` 或 `article` 内容；候选正文不足 200 字符时保守回退到完整文档。移除脚本、样式、导航等内容，保留标题、代码块、表格和链接。JSON 规范化缩进，纯文本保留正文。链接相对最终 URL 解析，不遵循页面 `<base>` 改写，也不自动打开链接、图片、脚本或 meta refresh。动态页面可能只有静态框架或空正文。

安全和资源策略：

- 只接受公开 HTTP(S) URL，限制 80/443 端口；拒绝凭证、歧义地址、回环、私网、链路本地、非公网及云元数据地址，额外拦截 IPv6 映射/转换地址和特殊用途网段。
- 解析全部 IPv4/IPv6 DNS 答案；任一地址不允许则整项目标拒绝。实际连接使用本次已验证的 IP；在发送 TLS/HTTP 数据前检查 socket 对端地址和端口与所选地址一致。Host、TLS SNI 和证书校验继续使用原始域名。
- 最多跟随 5 次 HTTP 重定向，每跳重新校验 URL、DNS 和实际连接，不共享 Cookie、认证信息或连接。不继承环境代理，始终校验 TLS 证书。
- 与搜索共享最多 3 个并发请求，每项总期限 20 秒，覆盖排队、DNS、跳转和读取；持续收到数据不会刷新总期限。操作系统 DNS 解析可能稍后结束，但不会恢复已取消的 HTTP 请求。
- 响应传输大小和解压后大小均限制 5 MiB，流式限制 gzip 解压；超限报错，不缓存残缺正文。提取文本最多 5 Mi 字符、100,000 行，并限制 HTML 深度、节点数、超长标记和文本展开。
- 整批返回序列化 JSON 最多 24,000 字符，每项均分预算，正文按完整缓存行返回。链接最多缓存 128 条，并受本次输出预算限制。

网页属于不可信资料，响应包装明确标记 `untrusted=true`。网页中的指令不能授权命令执行、文件读取或上传。URL 会外发给目标网站，URL 和正文会进入工具消息历史，并可随会话快照保存；追踪日志只保存参数摘要和调用状态，不复制完整 URL 或网页正文；不要在 URL 中放入密钥或未经授权外发的项目内容。

实现入口：`tools/web_tools.py`；网络策略：`tools/_internal/web_http.py`；正文处理：`tools/_internal/web_content.py`；快照缓存：`tools/_internal/web_pages.py`。工具只在主进程组装，不进入 `create_default_tools()` 或 sandbox worker，也不改变执行后端健康状态。

设计参考：[OWASP SSRF 防护](https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html)、[HTTP Core 网络后端](https://www.encode.io/httpcore/network-backends/)、[Python 地址分类](https://docs.python.org/3/library/ipaddress.html)。

## 验证

```bash
python -m pytest tests/test_web_fetch.py tests/test_web_fetch_cli.py tests/test_web_search.py tests/test_web_search_cli.py -q
```

测试替换 DNS 和 TCP 流，保留真实 HTTP 解析及目标校验逻辑；覆盖 DNS 重绑定、实际对端检查、IPv6、逐跳校验、TLS 主机名、无凭证请求、总超时、解压上限、内容结构、缓存隔离与分页、输出预算，以及 local/native 主进程调用链。真实网络已验证 Python 官方文档和公开 JSON 页面的读取与缓存分页。
