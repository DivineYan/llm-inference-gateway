-- 令牌桶限流（M1 §4.2 / TD-1）。
-- 取令牌 + 按时间回填 + 扣减，三步在 Redis 服务端原子完成，
-- 杜绝多实例并发下的超卖。
--
-- KEYS[1] 桶 key（如 ratelimit:svc:search）
-- ARGV[1] rate_per_sec 回填速率
-- ARGV[2] burst         桶容量上限
-- ARGV[3] now           当前时间（秒，浮点）
-- ARGV[4] requested     本次请求消耗的令牌数（通常 1）
-- 返回 {allowed(0/1), retry_after_ms} —— allowed=1 时 retry_after_ms=0

local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])

local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
local last = tonumber(redis.call('HGET', KEYS[1], 'ts'))

if tokens == nil then
  tokens = burst
  last = now
end

-- 按经过时间回填，封顶到容量
local delta = now - last
if delta < 0 then delta = 0 end
tokens = math.min(burst, tokens + delta * rate)

local allowed = 0
if tokens >= requested then
  tokens = tokens - requested
  allowed = 1
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
-- 空闲桶自动过期，避免 key 无限堆积
local ttl = math.ceil(burst / rate) + 1
redis.call('EXPIRE', KEYS[1], ttl)

if allowed == 1 then
  return {1, 0}
end

-- 还差多少令牌 → 估算多久后可用（毫秒），用于 Retry-After
local need = requested - tokens
local wait_ms = math.ceil(need / rate * 1000)
return {0, wait_ms}
