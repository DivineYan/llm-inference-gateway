# 待写清单（BACKLOG）

> 已确认要做、但当前刻意不写的功能（避免投机性开发）。每条带"为什么/最省做法/工作量/触发时机"。
> 其它分散的推迟项见各设计文档：harness → [M3_HARNESS.md](M3_HARNESS.md)；Tier4/5 → [M3_TIER4.md](M3_TIER4.md)；通用 → [README.md](README.md) 末尾。

---

## B1. 后端失败的历史趋势（按后端可聚合）

**现状空白**：当前没有任何地方持久、可聚合地累计"后端 X 过去 N 小时失败了多少次"。
- 熔断器 `circuit:{backend}` 只记滑动窗口内失败率（为跳闸服务，窗口外丢、空闲过 TTL 即清），是"此刻态"非历史；
- `ctx.attempts` 有每请求的失败后端，但只在 TraceStore 内存环形缓存（最近 1000）、不按后端聚合、重启即丢；
- telemetry 摘要只记**服务后端**，不含**失败后端**（见 [store.py](app/telemetry/store.py) `summarize`）。

**为什么需要**：慢性抖动发现（某后端反复跳闸又恢复，单次都被 M2 处理了，但"整周一天跳 5 次"说明该换）；供应商可靠性对比/选型；SLA 周报的失败次数/MTTR；给 `weight_rebalance_advisory` 更准的趋势信号（现仅用此刻熔断态）。

**最省做法（复用现有遥测底座，不新建子系统）**：在 `summarize(ctx)` 里从 `ctx.attempts` 取失败后端，作为字段（如 `failed_backends: [name]`）一并落进 `telemetry:traces`。这样 `query_metrics(group_by="backend")` 的失败趋势**白送**，无新增存储/组件。

**工作量**：约 10 行（summarize 加字段 + 聚合时把 failed_backends 计入）。

**触发时机**：同时挂多家 provider、需要"哪家长期更稳/谁在慢性抖动"时再做。当前实时根因排查已完整（熔断态 + attempts），不阻塞。


## B2. loop流程优化：system prompt，上下文等。