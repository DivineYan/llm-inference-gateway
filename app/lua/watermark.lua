-- 全局容量水位判定 + 迟滞状态机（M1 §4.3 / TD-7）。
-- 在途请求以 trace_id 为成员、入场时间戳为分值存在 sorted set 里，
-- 这里先按时间清掉过期（疑似泄漏）的条目，再以 ZCARD 作为当前水位。
-- 两条水位线 + 持久化的 mode 实现迟滞，避免临界点抖动。
--
-- KEYS[1] inflight:reqs（sorted set）
-- KEYS[2] scheduler:mode（normal/tense）
-- ARGV[1] high          警戒线
-- ARGV[2] low           解除线
-- ARGV[3] stale_before  早于此时间戳的在途条目视为泄漏，清除
-- 返回 {inflight_count, mode}

redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, tonumber(ARGV[3]))  -- 删除 score 在 [min, max] 范围的元素。
local inflight = redis.call('ZCARD', KEYS[1]) --统计当前在途请求数

local high = tonumber(ARGV[1])
local low = tonumber(ARGV[2])

local mode = redis.call('GET', KEYS[2])
if not mode then mode = 'normal' end

if mode == 'normal' then
  if inflight >= high then mode = 'tense' end
else
  -- 已处于紧张：直到回落到解除线以下才恢复（迟滞）
  if inflight <= low then mode = 'normal' end
end

redis.call('SET', KEYS[2], mode)
return {inflight, mode}
