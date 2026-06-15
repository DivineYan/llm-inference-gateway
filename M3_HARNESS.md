# M3 Agent —— Harness（约束/优化）清单

> 本文记录 M3 编排层 agent 需要的"护栏(harness)"：哪些已在代码里落地、哪些仍需补、
> 接真实模型时还要加什么。代码注释中的 "§harness" 即指本文。
> 核心认知：**agent 的能力来自模型的自主性，风险也来自它——harness 是把自主性
> 约束在安全、可控、可观测的边界内。**

---

## 一、已实现的约束（M3 现有代码）

| # | 约束 | 位置 | 解决什么 |
|---|------|------|----------|
| H1 | **max_steps 封顶** | `react.py` 循环上界 | 防 ReAct 不收敛/死循环；超界转 failed |
| H2 | **工具错误回填模型而非崩溃** | `react.py:_exec` 捕获 ToolError/NotFound→`{"error":...}` | 让 agent 能自我纠偏，单个工具失败不炸整条循环 |
| H3 | **观测结果安全序列化** | `react.py:_dump`（json + default=str） | 不可序列化对象不会把异常抛进循环 |
| H4 | **模型调用经熔断+重试** | `model_gateway.py`（复用 M2 Circuit/Retrier） | 后端抖动不拖垮 agent；半开探针不重试 |
| H5 | **任务级幂等 + 检查点续跑** | `state.py` + runner/agent resume | 重跑不重复执行已完成步骤/不重复花 token |
| H6 | **工具全只读** | `tools/`（Tier1-3 无写操作） | 无副作用，重执行天然安全 |
| H7 | **遥测 best-effort** | `api.py` finally try/except | 观测绝不拖垮请求路径 |
| H8 | **任务状态 TTL** | `state.py` TASK_TTL | 状态不无界堆积 |
| H9 | **凭证不外泄** | `get_config` 用不含 credential 的画像 | 工具产物不泄露密钥 |
| H10 | **坏参数→可读反馈** | `registry.py` TypeError→ToolError | 模型给错参数名时得到可读错误而非 500 |
| H14 | **agent 用量纳入 M1 治理** ✅ | `model_gateway.py`(limiter+telemetry) · `agent/api.py`(鉴权+归属) | agent/skill 端点需鉴权并归属调用方；每次模型调用经 M1 限流(同调用方维度)、用量记进遥测(source=agent)——不再绕过网关无限消耗、用量可见 |
| H13 | **工具/模型调用超时** ✅ | `react.py`(asyncio.wait_for) | 挂住即硬中止；工具超时→error 观测回填，模型超时→任务 failed。**模型/工具分别用 `model_timeout_s`(默认60s)/`tool_timeout_s`(默认5s)**——真模型秒级、工具毫秒级，延迟差异大不能共用一个超时（真实 DeepSeek 跑出来的教训） |
| H12a | **观测体积截断** ✅ | `react.py:_observe` | 单条观测注入上下文前截断到 `max_obs_chars`，防爆窗口/成本 |
| H15 | **工具参数 schema 校验** ✅ | `registry.py:_validate_args` | 调用前校验必填/类型/enum/未知参，不合法→ToolError 回填模型 |
| H16 | **重复调用检测** ✅ | `react.py` 连续计数 | **连续**相同 (name,args) 超 `max_repeat`→先提示、再 failed(stuck_repeating)；换调用即重置，防卡死 |
| H17 | **修复有界** ✅ | `react.py` error_streak | 连续错误观测超 `max_repair`→failed(repair_exhausted)，防无限自纠偏 |

---

## 二、仍需补的约束（按优先级）

### P0 —— 接真实模型前

- **H11 Token/成本预算**（未做，本期不做）：每个任务设 token 上限。H1 只限"步数"不限"体量"，
  真实模型按 token 计费。接真模型时补 per-task token budget + 超限终止。
- ~~**H12 观测体积截断**~~：**H12a 已实现**（上表）。剩 **H12b 整条上下文压缩**——多步累积超
  窗口时阈值触发摘要老轮次，已在 `react.py:_maybe_compress` 留 no-op 钩子，接真模型/长任务再启用。
- ~~**H13 超时**~~ ✅ **已实现**（上表）。
- ~~**H14 agent 用量纳入治理**~~ ✅ **已实现**（上表）。**剩余**：每日 token 配额、
  模型权限校验（调用方是否允许该 model）未做，作为后续补强。

### P1 —— 健壮性（本批已落地）

- ~~**H15 参数 schema 校验**~~ ✅、~~**H16 重复检测**~~ ✅、~~**H17 修复有界**~~ ✅（见上表）。
- 备注：H15/H16/H17 共用"反馈回灌 + 有界修复"机制；真实模型的坏 tool_call 还应优先靠
  **provider 原生结构化输出（tool_use / JSON mode）从源头杜绝**，本层的校验+有界修复是兜底。

### P2 —— 安全/质量

- **H18 工具白名单/授权**：按任务或调用方限定可用工具。Tier4 写工具到来后尤其关键。
- **H19 提示注入防护**：工具 observation 回填进上下文——内部只读工具风险低，但**接外部 MCP
  server 后**，外部数据可能携带"指令"。需把工具输出当数据(分隔/转义)而非指令。
- **H20 结论可信度/拒答**：要求 agent 结论必须有工具观测支撑（grounding），不确定时说"无法确定"
  而非编造。真实模型需在系统提示里强约束 + 校验。

### 未来阶段（已在 M3.md §10 记）

- ~~**H21 Tier4 写操作护栏**~~ ✅ **已实现**（Step A 运行时覆盖层 + Step B 护栏管线
  + Tier5 渐进自治最小版）：`propose → validate → approve → apply → 审计`，幂等、白名单、
  冷却、不搞死模型；Autopilot 对"持续熔断坏后端"窄白名单自批 + 自动回滚 + 人工升级。
  详见 [M3_TIER4.md](M3_TIER4.md)。剩余：真金丝雀（需流量分流）、what-if 完整重放、
  更宽自批白名单、agent 级变更限速/kill-switch。

---

## 三、harness 与现有四层的关系

M3 的 harness 不是凭空新增——很多是**复用下层能力**：
- 模型调用的稳定性 → 复用 **M2**（熔断/重试/降级，H4）。
- agent 用量的治理 → 应复用 **M1**（限流/配额/优先级，H14 待接）。
- 决策可观测 → 复用 **M1/M2 的 decision_log + M3 遥测**（H7，trajectory 已持久化）。

> 一句话：**M3 把"模型自主决策"引入系统，harness 的职责是让这份自主性
> 不突破 M1 的治理、不绕过 M2 的保障、并全程可观测可续跑。** 当前已落地 16 条
> 护栏（H1–H10、H12a、H13–H17）；剩余主要是 H11 token 预算、H12b 上下文压缩，
> 及 P2 安全项（H18–H21），多数等接真实模型时再补。
