# AI 推理调度平台 —— 网关 + 保障 + Agent 编排（M1 + M2 + M3）

LLM 推理中间件的**唯一入口**：所有横切关注点（鉴权、限流、调度、路由、熔断、重试、降级）
在此收口，下层只执行被允许的请求。M1 实现 Layer 1 网关接入层，M2 实现 Layer 4 保障层，
M3 实现 Layer 2 Agent 编排层（平台内置 AIOps Copilot），后端用进程内 mock 顶替
（可换真实模型适配器，编排零改动）。

详细设计见 [M1.md](M1.md)（网关）、[M2.md](M2.md)（保障层）、[M3.md](M3.md)（Agent 编排层）；
agent 护栏清单见 [M3_HARNESS.md](M3_HARNESS.md)。

## 请求流水线

一个带凭证的请求依次穿过 4 道闸门，再进入保障执行器分发；每道要么放行、要么拦截并给出明确原因码：

```
POST /v1/infer
  ① Authenticator  凭证→画像，校验模型权限      401 unauthenticated / 403 model_not_allowed
  ② RateLimiter    令牌桶，机器=svc / 人类=user   429 rate_limited
  ③ Scheduler      在途水位 + 迟滞，抢占低优       429 preempted
  ④ Router         健康后端 SWRR → 有序候选列表    503 no_backend
  ⑤ 保障执行器     候选迭代 × 熔断 × 重试 × 降级
      ├ 熔断 CircuitBreaker  后端 Open → 快速失败跳过
      ├ 重试 Retrier         瞬时故障指数退避+抖动重试
      └ 降级 Degrader        换备用模型 / 兜底（503 served_fallback，标 degraded）
  → 返回结果（成功 / 降级 / 兜底）+ 留存 decision_log / attempts / 耗时
```

- **②限流** 保单个调用方公平性；**③调度** 保系统紧张时高优先级业务；**⑤保障** 保后端不稳时对外可用。
- 三层"换路"粒度不同：**重试**（同后端瞬时故障）→ **候选迭代**（同模型换后端）→ **降级**（换模型/兜底）。
- 限流额度、在途水位、**熔断状态全部存 Redis**，多实例全局一致；令牌桶与熔断状态转移由 Lua 原子完成。
- 在途用 sorted set（`trace_id`→时间戳）记录，崩溃实例的条目按时间自动过期，不泄漏水位。
- 整段保障执行（含重试退避）都在 inflight guard 内：退避期间请求仍计入水位（语义正确）。

## 目录结构

```
app/
  main.py            app 工厂：加载配置 / 连 Redis / 组装流水线 / 注册异常处理
  api.py             对外接口：/v1/infer · /health · /debug/trace/{id}
  config_models.py   配置 pydantic 模型        config_loader.py  YAML→内存+查表
  context.py         RequestContext（贯穿全程的随行包，含保障决策字段）
  errors.py          GatewayError + reason 码（M1 五个 + M2 served_fallback）
  redis_client.py    Redis 连接 + Lua 加载       inflight.py  在途 sorted-set + guard
  observability.py   结构化日志 + TraceStore（决策链留存）
  mock_backend.py    成功/5xx/超时/慢/参数错 mock + 带 retryable 的错误类型
  lua/               token_bucket（限流） · watermark（水位迟滞） · circuit_allow/circuit_record（熔断）
  gates/             authenticator · rate_limiter · scheduler · router · pipeline
  safeguard/         circuit（熔断三态机） · retry（退避） · degrade（降级） · executor（编排⑤）
config.yaml          调用方画像 / 后端列表 / 阈值 / safeguard 参数（改配置不改代码，NFR-6）
scripts/             multi_instance_demo.py  真·两进程全局一致性演示
tests/               test_t1..t10 + test_m2_t1..t8 + test_multi_instance
```

## 环境与运行

依赖 Python 3.11（conda 环境 `agent_project`）+ 本机 Redis（`127.0.0.1:6379`）。

```powershell
$PY = "D:\Python\envs\agent_project\python.exe"

# 安装依赖
& $PY -m pip install -r requirements.txt

# 跑全部自测（70 项：M1 35 + M2 35）
& $PY -m pytest -q

# 启动网关
& $PY -m uvicorn app.main:app --port 8000

# 真·两进程全局一致性演示（启两个 uvicorn，证明限流额度跨进程合并）
& $PY scripts/multi_instance_demo.py
```

测试用 Redis db 15、演示用 db 14，与默认 db 0 隔离。

### 试一下

```powershell
# 合法请求
curl -X POST http://127.0.0.1:8000/v1/infer -H "Authorization: Bearer key-search-machine" `
     -H "Content-Type: application/json" -d '{"model":"gpt","input":"hello"}'

# 查某次请求的完整决策链
curl http://127.0.0.1:8000/debug/trace/<trace_id>

# 网关存活 + 后端健康 + 当前水位
curl http://127.0.0.1:8000/health
```

## 验收清单 ↔ 证据

**M1 网关（M1 §9）**

| 验收项 | 证据 |
|--------|------|
| 无/错凭证 → 401 unauthenticated | `tests/test_t3.py` |
| 无权模型 → 403 model_not_allowed | `tests/test_t3.py` |
| 机器按 svc、人类按 user 维度限流 → 429 rate_limited | `tests/test_t4.py` |
| 紧张时高优放行 / 低优 429 preempted；回落到解除线恢复（迟滞） | `tests/test_t5.py` |
| preempted 与 rate_limited 可区分 | `tests/test_t5.py` |
| 多后端按权重分布（3:1） | `tests/test_t6.py` |
| 无可用后端 → 503 no_backend | `tests/test_t6.py` |
| 完整 decision_log（闸门/结果/各段耗时） | `tests/test_t8.py` · `/debug/trace` |
| **多实例限流/水位全局一致** | `tests/test_multi_instance.py` + `scripts/multi_instance_demo.py` |

**M2 保障层（M2 §10）**

| 验收项 | 证据 |
|--------|------|
| 后端连续失败 → 熔断打开 → 快速失败；冷却后半开探针 → 成功恢复/失败重开 | `tests/test_m2_t3.py` |
| 偶发超时按指数退避+抖动重试；不可重试错误不重试 | `tests/test_m2_t2.py` |
| 主模型全挂 → 降级到备用模型，响应标 `degraded:true` + `fallback_model` | `tests/test_m2_t5.py` · `test_m2_t6.py` |
| 主备都挂 → 503 `served_fallback` 兜底响应，平台不崩溃 | `tests/test_m2_t6.py` |
| `/health` 展示各后端熔断状态；`/debug/trace` 展示保障决策（attempts） | `tests/test_m2_t7.py` |
| **多实例熔断状态全局一致**（A 跳闸，B 立刻快速失败） | `tests/test_m2_t8.py` |

## M3 —— Agent 编排层（平台内置 AIOps Copilot）

对本平台自身运行做**根因诊断 / 运营报表 / 优化建议**（全程只读）。两种执行模式
共享一套底座（工具/状态/模型/遥测），模型调用复用 M2 保障层：

```
ReAct Agent    探索式诊断（reason→act→observe，有界）   /v1/agent
Workflow Runner 确定式报表/建议（声明式 skill，可续跑）  /v1/skills/{name}/run
共享底座：mini-MCP 工具注册表 · 任务检查点(续跑) · ModelGateway(熔断+重试) · 遥测聚合
```

**新增目录（清晰分层，不堆 app/ 根）**
```
app/model/        模型调用契约 ModelClient + MockModelClient（接真模型只换适配器）
app/telemetry/    Tier0 遥测底座：trace 摘要按时间入 Redis + 窗口/维度聚合
app/agent/
  tools/          mini-MCP 注册表 + 6 只读工具 + 2 优化建议工具（+外部 MCP 适配口）
  state.py        任务状态机 + 检查点（断点续跑，TD-4）
  model_gateway   编排层模型调用入口（复用 M2 熔断/重试）
  workflow.py     Workflow Runner      skills/  报表与建议 skill
  react.py        ReAct 诊断 Agent（招牌）
  api.py          /v1/agent · /v1/skills(/run) · /v1/tools · /v1/tasks/{id}
```

### M3 接口与示例

```powershell
# 列出可用工具 / skill
curl http://127.0.0.1:8000/v1/tools
curl http://127.0.0.1:8000/v1/skills

# ReAct 诊断（需鉴权，归属到调用方）
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/agent `
  -Headers @{ Authorization = "Bearer key-search-machine" } -ContentType "application/json" `
  -Body '{"goal":"为什么部分 gpt 请求异常？","task_id":"diag1"}'

# 跑一个报表 skill（同 task_id 重提交 = 断点续跑）
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/skills/usage_report/run `
  -Headers @{ Authorization = "Bearer key-search-machine" } -ContentType "application/json" `
  -Body '{"task_id":"rep1","params":{"window_seconds":3600}}'

# 查任务状态机 + 中间结果（agent：轨迹+结论；workflow：各步输出）
curl http://127.0.0.1:8000/v1/tasks/diag1

# Tier4 写护栏：提案改配置（不立即生效）→ 人工批准 → 生效到运行时覆盖层
$h = @{ Authorization = "Bearer key-search-machine" }
$p = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/v1/changes -Headers $h `
     -ContentType "application/json" -Body '{"field":"backend:gpt-a:weight","value":1,"rationale":"持续失败,降权"}'
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/v1/changes/$($p.id)/approve" -Headers $h
curl http://127.0.0.1:8000/health   # backends[].weight 已变 + overrides 列出活跃覆盖
```

### 接入真实模型（OpenAI / DeepSeek / Qwen / Kimi）

四家均为 **OpenAI 兼容协议**，一个 `OpenAICompatibleClient` 全覆盖（按 backend 的
`base_url` + `api_key_env` + `real_model` 区分）。**API key 走环境变量，绝不写进配置。**
ReAct 用**原生 function-calling**（provider 保证 tool_calls 合法）。接真模型**编排零改动**——
只在 config 加一个 `provider: openai_compatible` 的后端（见 config.yaml 注释示例）。

```powershell
# 设置任一 provider 的 key，跑端到端冲烟（直连文本 + 真实 ReAct 工具回合）
$env:DEEPSEEK_API_KEY="sk-..."
D:\Python\envs\agent_project\python.exe scripts/real_model_smoke.py deepseek
```

> 适配器映射/解析/错误→BackendError 由 `tests/test_m3_t16.py` 用 httpx MockTransport
> 确定性覆盖（不连网）；真后端的故障同样吃 M2 熔断/重试/降级。

### 流量生成 / 压测（攒真实遥测 + SLA）

`scripts/load_gen.py` 按真实配比打 /v1/infer，覆盖全部 reason 码，输出吞吐/延迟分位/reason 分布。
配套 `config.loadtest.yaml` 故意制造 成功/限流/抢占/无后端/兜底 五类结果。

```powershell
# 进程内（免起服务，攒遥测 + 看 reason 分布）
D:\Python\envs\agent_project\python.exe scripts/load_gen.py --concurrency 30 --duration 12
# 真 HTTP SLA（先用压测配置起网关）
$env:CONFIG_PATH="config.loadtest.yaml"; uvicorn app.main:app --port 8000
D:\Python\envs\agent_project\python.exe scripts/load_gen.py --url http://127.0.0.1:8000 -c 50 -d 20
```

> 用途：①压测网关 SLA（吞吐/p99）②攒出含各类故障的真实遥测，作为 Agent 评测集的数据底座
> （下一步：注入已知故障的标注场景 + 结构化/LLM-judge 打分）。

### 性能基准（LLM 网关视角）

LLM 后端调用耗时数秒，网关 ms 级开销可忽略——所以**不测 QPS，测"网关开销"与"并发持有容量"**
（`scripts/bench_llm.py`，慢 mock 模拟 2s 的 LLM）：

- **网关额外开销**：后端固定 200ms、并发 1 时，e2e p50 ≈ 217ms → **网关纯逻辑开销 ~17ms**，
  相对数秒的真实 LLM 调用**可忽略**（<1%）。
- **并发容量(单进程/单事件循环)**：吞吐随并发上升但**很快见顶 ~135 req/s**，且并发越高 p99
  延迟越爆（2000 并发时 p50≈10s ≫ 后端 2s）。

| 并发 | 吞吐 req/s | p99 ms | 解读 |
|---|---|---|---|
| 50 | 19 | ~2900 | 接近理想 |
| 500 | 90 | ~5000 | 开始排队 |
| 2000 | 135 | ~11000 | 单循环饱和 |

> **结论(可信、面试可讲)**：单个异步进程**不能无限持有并发慢请求**——单事件循环的每请求
> CPU/编排开销在高并发下成为上限(~135/实例)。**LLM 网关的扩展轴是水平的**(uvicorn `--workers N`
> / 多实例)，而本平台**所有状态都在 Redis(Lua 原子)**，加实例即线性扩展、全局一致——架构本就为此设计。
> （上表为进程内单循环测量，是下界；真实多 worker 部署按 worker 数倍增。）

### M3 验收 ↔ 证据

| 验收项 | 证据 |
|--------|------|
| 模型调用契约 mock/真实可换；70 项 M1/M2 测试保持绿 | `tests/test_m3_t1.py` |
| 遥测按窗口/维度聚合 count/error_rate/延迟/outcome | `tests/test_m3_t2.py` |
| mini-MCP 工具读真实 telemetry/config/health；get_config 不泄露凭证 | `tests/test_m3_t3.py` |
| 任务状态机 + 检查点续跑 | `tests/test_m3_t4.py` |
| Workflow 报表端到端 + 中途失败续跑不重跑已完成步骤 | `tests/test_m3_t5.py` |
| **ReAct 诊断：注入熔断 → agent 调工具定位根因；max_steps 防死循环；续跑零模型调用** | `tests/test_m3_t6.py` |
| Tier3 优化建议（降权 / 限流，只读） | `tests/test_m3_t7.py` |
| 编排层端点端到端贯通 | `tests/test_m3_t8.py` |
| agent 模型调用受 M1 治理（限流 + 归属 + 遥测可见，H14） | `tests/test_m3_t9.py` |
| agent 护栏：超时/观测截断/重复检测/参数校验/修复有界 | `tests/test_m3_t10.py` |
| **Tier4 运行时覆盖层**：weight/healthy/rate/watermark 改键即生效、删键回滚 | `tests/test_m3_t11.py` · `test_m3_t12.py` |
| **Tier4 写护栏**：agent 提案 → 人工 approve → 生效；幂等/白名单/冷却/不搞死模型 | `tests/test_m3_t13.py` |
| Tier5 Autopilot：窄白名单自批 / 回滚 / 升级；后台周期巡检 + 领导锁 | `tests/test_m3_t14.py` |
| **真实模型适配器**（OpenAI 兼容，原生 FC）映射/解析/错误→BackendError | `tests/test_m3_t16.py` |
| **真模型端到端**：DeepSeek 直连文本 + ReAct 工具回合诊断跑通 | `scripts/real_model_smoke.py`（实跑通过） |

## 后续与取舍

- **M2**：总失败统一兜底为 503 `served_fallback`（不单独返回 504，遵循 FR-4.3）。
- **M3 Tier1-3 只读 + Tier4 写护栏**：诊断/报表/建议为只读；改配置走 Tier4
  `propose → 人工 approve → apply`（[M3_TIER4.md](M3_TIER4.md)），agent 只提案、人点头才生效。
- **agent 护栏（harness）**：已落地 10 条基础护栏；接真实模型前优先补 token/成本预算、
  观测体积截断、超时等——详见 [M3_HARNESS.md](M3_HARNESS.md)。
- **通用未做**：低优先级排队/降速（P2）、后端动态健康检查与调权、配置热更新、
  Prometheus 指标与完整链路追踪（M4）、节点/工具并发（P2）、外部 MCP server 接入。
