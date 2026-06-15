-- 熔断放行判定（M2 §4.1 / TD-2）。读状态机，决定本次能否打后端。
-- 处理两件有副作用的事，必须原子：Open 到点自动转 Half-Open、半开探针名额发放。
--
-- KEYS[1] circuit:{backend}
-- ARGV[1] now               当前时间（秒，浮点）
-- ARGV[2] cooldown_seconds  Open → Half-Open 冷却时间
-- ARGV[3] half_open_probes  半开态最多放行的探针数
-- ARGV[4] ttl               key 过期秒数（空闲后自动忘记旧失败）
-- 返回 {allowed(0/1), is_probe(0/1), state}

local key = KEYS[1]
local now = tonumber(ARGV[1])
local cooldown = tonumber(ARGV[2])
local max_probes = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])

local state = redis.call('HGET', key, 'state')
if not state or state == 'closed' then
  return {1, 0, 'closed'}   -- 无记录/闭合：正常放行
end

if state == 'open' then
  local opened_at = tonumber(redis.call('HGET', key, 'opened_at')) or now
  if now - opened_at < cooldown then
    return {0, 0, 'open'}   -- 冷却中：快速失败，不打后端
  end
  -- 冷却到点：转半开，发放第一个探针
  redis.call('HSET', key, 'state', 'half_open', 'probes', 1)
  redis.call('EXPIRE', key, ttl)
  return {1, 1, 'half_open'}
end

-- half_open：探针名额未满才放行（小成本试探后端是否恢复）
local probes = tonumber(redis.call('HGET', key, 'probes')) or 0
if probes < max_probes then
  redis.call('HSET', key, 'probes', probes + 1)
  redis.call('EXPIRE', key, ttl)
  return {1, 1, 'half_open'}
end
return {0, 0, 'half_open'}
