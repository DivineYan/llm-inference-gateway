-- 熔断结果记账 + 状态转移（M2 §4.1）。每次后端调用后调用一次，原子完成。
-- 滚动时间窗用"窗口起点到期则重置计数"的轻量实现，不维护定长序列。
--
-- KEYS[1] circuit:{backend}
-- ARGV[1] now             当前时间（秒，浮点）
-- ARGV[2] success         本次结果：1 成功 / 0 失败
-- ARGV[3] window_seconds  失败率统计窗口
-- ARGV[4] failure_rate    跳闸失败率阈值
-- ARGV[5] min_samples     最小样本数
-- ARGV[6] ttl             key 过期秒数
-- 返回 new_state

local key = KEYS[1]
local now = tonumber(ARGV[1])
local success = tonumber(ARGV[2])
local window = tonumber(ARGV[3])
local rate = tonumber(ARGV[4])
local min_samples = tonumber(ARGV[5])
local ttl = tonumber(ARGV[6])

local state = redis.call('HGET', key, 'state') or 'closed'

if state == 'half_open' then
  if success == 1 then
    -- 探针成功 → 恢复闭合，清零窗口
    redis.call('HSET', key, 'state', 'closed', 'calls', 0, 'fails', 0, 'probes', 0, 'win_start', now)
  else
    -- 探针失败 → 重新打开，重置冷却
    redis.call('HSET', key, 'state', 'open', 'opened_at', now, 'calls', 0, 'fails', 0, 'probes', 0)
  end
  redis.call('EXPIRE', key, ttl)
  return redis.call('HGET', key, 'state')
end

if state == 'open' then
  return 'open'   -- 冷却期内的杂散记账忽略，状态转移由 allow 驱动
end

-- closed：滚动窗口计数，按失败率判定是否跳闸
local win_start = tonumber(redis.call('HGET', key, 'win_start'))
local calls = tonumber(redis.call('HGET', key, 'calls')) or 0
local fails = tonumber(redis.call('HGET', key, 'fails')) or 0

if (not win_start) or (now - win_start >= window) then
  win_start = now
  calls = 0
  fails = 0
end

calls = calls + 1
if success == 0 then fails = fails + 1 end

local new_state = 'closed'
if calls >= min_samples and (fails / calls) >= rate then
  new_state = 'open'
  redis.call('HSET', key, 'state', 'open', 'opened_at', now,
             'calls', calls, 'fails', fails, 'win_start', win_start)
else
  redis.call('HSET', key, 'state', 'closed',
             'calls', calls, 'fails', fails, 'win_start', win_start)
end
redis.call('EXPIRE', key, ttl)
return new_state
