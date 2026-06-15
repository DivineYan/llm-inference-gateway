# M3 Tier4 —— 写操作护栏（已实现 Step A+B）

> 对应 harness H21。Tier4 让 agent 能**提案改配置**（降权/调限流/调水位/复位熔断），
> 在严格护栏内、经人工 approve 才生效。决策基线：①agent 只活慢环 ②Redis 运行时
> 覆盖层 ③先只做人工 approve（自动回滚后续）。
>
> **实现状态**：Step A（运行时覆盖层）+ Step B（护栏管线）+ Tier5（渐进自治最小版，§5b）已落地。
> 测试：`tests/test_m3_t11.py`（覆盖层）、`test_m3_t12.py`（网关读有效值）、`test_m3_t13.py`
> （端到端护栏）、`test_m3_t14.py`（Autopilot 自批/回滚/升级）。
> **本期简化/未做**：what-if 为基于近期遥测的轻量预估（非完整 SWRR/令牌桶重放）；
> `reset_circuit` 直接清熔断态（未做"探活通过才允许"，后端仍坏会自动重新跳闸）；
> 自动回滚、canary、文件热重载未做。

---

## 0. 为什么要护栏 + 时间尺度的根本认知

随便改生产配置不可接受。但更关键的认知是**时间尺度分层**——这决定了 Tier4 的适用边界：

| | 快环（反射） | 慢环（决策） |
|---|---|---|
| 负责 | **M2 已建好**：熔断/重试/降级 | **Tier4 agent 写操作** |
| 尺度 | 毫秒~秒 | 分钟~小时 |
| 触发 | 瞬时异常 | **持续/结构性**问题 |

**结论：熔断/失效这类瞬时事件由 M2 自动反射处理，agent 不参与、也追不上。Tier4 只针对"持续 N 分钟"的结构性问题**（某后端持续挂、限流长期顶格、容量趋势上涨）。因为目标条件本身是分钟级的，"模拟→人审→生效"花几秒到几分钟**完全赶得上趟**——这化解了"模拟赶不上瞬时事件"的顾虑：我们压根不用 agent 去追瞬时事件。

> 触发判据是"**持续了多久**"，不是"此刻是否异常"。刚跳一次的熔断 → 不碰（M2 会恢复）；
> 连续 10 分钟失败率 >50% → 才提案降权。

---

## 1. 护栏管线

```
① propose   agent 产出结构化变更提案 ChangeProposal
            {id, target, from, to, rationale, trigger:"持续10min失败率>50%"}
                │（沿用 Tier3 advisory 产出，加"可执行变更描述"）
② validate  a) 静态校验：改后配置合法？（无死 fallback、模型仍有≥1健康后端、阈值在合理区间）
            b) what-if 重放：用最近遥测在新配置下重算，出"预测影响报告"（见 §3）
                │
③ approve   ⭐人工闸门（关键控制点）：提案+预测报告 → 人看 → 批准
            │  慢环、人审分钟级——没问题，只针对分钟级的持续问题
④ apply     写入 Redis 运行时覆盖层（§2）；幂等（按提案 id 去重）；快照旧值
            │  （自动回滚=后续增强，本期不做）
⑤ audit     全程留痕：谁提/改了啥/谁批/何时生效/结果（不可变审计流水）
```

---

## 2. apply 机制：Redis 运行时覆盖层 ⭐（Tier4 的工作量大头）

**现状障碍**：M1/M2 启动时一次性加载配置、无热更新（NFR-6 是改文件+重启）。Tier4 要运行时改配置，
必须先建一个**覆盖层**——这是 Tier4 的前置依赖与主要工作量，护栏管线搭在它之上。

**覆盖键（Redis）**
```
config:override:backend:{name}:weight     = 1
config:override:backend:{name}:healthy    = false
config:override:caller:{caller_id}:rate_per_sec = 80
config:override:thresholds:high_watermark = 15
```

**读取优先级**：网关组件取值时"**有覆盖用覆盖、否则用文件值**"。封装成一个 `EffectiveConfig.get(field, fallback)`。

**谁来读 + 热路径考量**（重要设计点）：
- Router（weight/healthy）、RateLimiter（rate/burst）、Scheduler（watermark）都在请求热路径上。
  **不要每请求去 Redis 查覆盖**（加延迟）。做法：每实例维护**内存覆盖快照**，靠 Redis pub/sub
  （`config:override:changed`）或短轮询刷新；覆盖很少变（人审过的），刷新滞后 <1s 完全可接受。
- Router 的 SWRR 选择器在覆盖变更时**重建**（不是每请求重算）。

**回滚**：删除覆盖键即恢复文件值（天然可逆）。**多实例一致**：覆盖在 Redis，pub/sub 通知所有实例刷新——与 M1/M2 把动态状态放 Redis 的风格一致。

---

## 3. dry-run：离线 what-if（按改动类型，秒级出结果）

无法不放真实流量就"真模拟"生产；可行的是**拿最近 N 分钟遥测在新配置下重算**——不依赖等真实流量，秒级：

| 改动类型 | what-if 怎么做（我们有数据） |
|---|---|
| 调权重 | 最近请求流在新权重下重跑 SWRR → 预测各后端新负载分布 → 检查接收方余量 |
| 调限流 | 该调用方最近请求时间戳在新令牌桶下重放 → 预测放行/拒绝数 |
| 调水位 | 重放在途序列 → 预测新阈值下抢占次数 |
| 复位熔断/翻 healthy | 不重放，改为**主动探活**：发合成请求确认后端真好了，再允许 |

> 对实在无法离线预测的，更好的工具是 **apply + 监控 + 自动回滚**（事后可逆替代事前模拟）——本期不做，列为后续。

---

## 4. 安全要点

- **尊重 M2 状态**：不允许 close 一个后端还坏着的熔断（探活不过则拒绝提案）——防与快环打架。
- **爆炸半径限制**：单次只动一个目标；变更幅度设上限（如权重一次最多降一档）；同目标设冷却期（X 分钟内不重复改）。
- **幂等**：apply 按提案 id 去重，重放安全（TD-4 同思路）。
- **审计不可变**：提案/审批/生效/回滚全留痕，可追责。
- **写操作仍走 M1 治理**：Tier4 工具同样属于 agent，受 H14（鉴权/归属）约束；写工具应有独立白名单（H18）。

---

## 5. 数据结构与接口（概念）

```
ChangeProposal: { id, created_ts, target, field, from, to, rationale, trigger,
                  predicted_effect, status: proposed|approved|applied|rejected|rolled_back }
```
| 接口 | 用途 |
|---|---|
| `POST /v1/changes`（agent 内部产出） | 提交提案（带 validate 报告） |
| `GET  /v1/changes` | 列待审/历史提案 |
| `POST /v1/changes/{id}/approve` | 人工批准 → 触发 apply |
| `POST /v1/changes/{id}/reject` | 驳回 |
| `GET  /v1/changes/{id}` | 看提案+预测+审计 |

---

## 5b. 渐进自治（Tier5，最小可跑版已实现）

把"人工 approve"渐进替换为"窄白名单自批 + 自动回滚 + 人工升级"。**当前白名单只批一种**：
排除持续熔断的坏后端（`backend:{name}:healthy=false`）——最安全、最可逆、信号最明确。

```
Autopilot.run_cycle（可挂调度 / POST /v1/autopilot/run 触发）：
  ① 检测：后端连续 sustained 个周期都熔断(streak 计数) → 算"持续"
       └ 已摘除的后端跳过（防反复）
  ② 提案 healthy=false（经 Tier4 propose：校验不搞死模型 + 冷却）
  ③ 策略闸：在白名单内且 valid → 自批 apply；否则留 proposed 升级给人
  ④ 回滚监控：apply 后盯模型 error_rate 一个窗口
       ├ 变差 → 自动回滚(删覆盖) + 升级
       └ 改善/持平 → 保留
```

**为什么不是真 sandbox**：真隔离 sandbox / 金丝雀需要流量分流/镜像（未具备）。这里用
**可逆 apply + 监控回滚**替代"事前模拟"——改下去随时能撤回，比赶不上趟的离线模拟更实在。

**防失控护栏**：只对"持续"熔断动手（远慢于 M2 快环，防打架）；同字段冷却防抖；
不搞死模型；超白名单一律升级给人。实现见 `app/agent/autopilot.py`，测试 `tests/test_m3_t14.py`。

**未做（后续放宽时再补）**：真金丝雀（需流量分流）、更宽白名单、agent 级变更速率限制/
kill-switch、自批后端恢复时的自动复权。

## 6. 实施路线（两步，先后不可颠倒）

```
Step A  运行时覆盖层（前置，工作量大头）
        - EffectiveConfig + Redis 覆盖键 + 内存快照 + pub/sub 刷新
        - 改 Router/RateLimiter/Scheduler 读取路径走 EffectiveConfig
        - 验证：改覆盖键 → 多实例生效；删键 → 回滚
Step B  护栏管线（搭在 A 上）
        - ChangeProposal + 写工具(set_weight/set_rate/set_watermark/reset_circuit)
        - validate(静态+what-if) + 人工 approve 接口 + audit
        - 写工具纳入白名单(H18)、尊重 M2 状态、爆炸半径与冷却
```

**本期不做（明确推迟）**：自动回滚、canary/影子流量、文件热重载、写操作的多级审批工作流。

---

*Tier4 设计锁定。实现按 §6 两步走，Step A（运行时覆盖层）是前置。*
