-- All keys belong to one cache namespace: records, LRU, and retention deadlines.
-- Fences share the same bounded records as metadata, including after no-store.
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
local touch = tonumber(clock[1]) * 1000000 + tonumber(clock[2])
local op, key, token = ARGV[1], ARGV[2], ARGV[3]
local maxEntries, maxBytes = tonumber(ARGV[4]), tonumber(ARGV[5])
local retention, fence = tonumber(ARGV[6]), tonumber(ARGV[7])

-- Eviction or expiry of only part of the namespace invalidates all its tokens.
local present = redis.call('EXISTS', KEYS[1], KEYS[2], KEYS[3])
if present > 0 and present < 3 then redis.call('DEL', KEYS[1], KEYS[2], KEYS[3]) end
-- Lua is atomic but does not roll back writes on an execution error. Discard
-- any namespace left incomplete by an earlier failed script before reading it.
if redis.call('HGET', KEYS[1], '_dirty') then redis.call('DEL', KEYS[1], KEYS[2], KEYS[3]) end
redis.call('HSET', KEYS[1], '_dirty', 1)
local bytes = tonumber(redis.call('HGET', KEYS[1], '_bytes') or '0')
local function remove(k)
    local raw = redis.call('HGET', KEYS[1], k)
    if raw then bytes = bytes - (#raw + #k * 3 + 128) end
    redis.call('HDEL', KEYS[1], k)
    redis.call('ZREM', KEYS[2], k)
    redis.call('ZREM', KEYS[3], k)
end
-- Work is bounded by metadata_max_entries (at most 10000).
for _, k in ipairs(redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now)) do remove(k) end

local function save(record)
    remove(key)
    local raw = cjson.encode(record)
    local cost = #raw + #key * 3 + 128
    if maxEntries == 0 or cost > maxBytes then return false end
    while redis.call('ZCARD', KEYS[2]) >= maxEntries or bytes + cost > maxBytes do
        local oldest = redis.call('ZRANGE', KEYS[2], 0, 0)
        if #oldest == 0 then return false end
        remove(oldest[1])
    end
    redis.call('HSET', KEYS[1], key, raw)
    redis.call('ZADD', KEYS[2], touch, key)
    redis.call('ZADD', KEYS[3], math.max(record.retain, record.untilTime), key)
    bytes = bytes + cost
    return true
end

local raw = redis.call('HGET', KEYS[1], key)
local record = raw and cjson.decode(raw) or nil
local result
if op == 'begin' then
    -- Compare completed updates by a Redis-issued sequence, not by the latest
    -- reader. A fresh read or failed newer capture must not suppress no-store.
    -- Rotate the epoch before exceeding cjson's integer precision.
    local sequence = tonumber(redis.call('HGET', KEYS[1], '_next') or '0')
    if sequence >= 1000000000000 then
        redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
        redis.call('HSET', KEYS[1], '_dirty', 1)
        bytes = 0; sequence = 0; record = nil
    end
    sequence = sequence + 1
    local epoch = redis.call('HGET', KEYS[1], '_epoch') or token
    redis.call('HSET', KEYS[1], '_epoch', epoch, '_next', sequence)
    if not record then record = {value='', validated=0, retain=0, floor=sequence, order=0} end
    if record.retain <= now then record.value = ''; record.validated = 0 end
    record.untilTime = now + fence
    local accepted = save(record)
    result = {record.value, record.validated, now, accepted and epoch or '', sequence}
elseif op == 'commit' then
    local validated = math.min(now, tonumber(ARGV[9]))
    local saved = 0
    local sequence = tonumber(ARGV[11])
    if record and redis.call('HGET', KEYS[1], '_epoch') == token
        and sequence >= record.floor and sequence >= record.order
        and tonumber(ARGV[12]) > now then
        record.order = sequence
        record.value = ARGV[8]
        record.validated = validated
        record.retain = validated + retention
        if ARGV[10] == '1' then
            record.value = ''; record.validated = 0; record.retain = 0
        end
        if save(record) then saved = 1 end
    end
    result = {validated, now, saved}
else
    return redis.error_reply('unknown metadata operation')
end

if redis.call('ZCARD', KEYS[2]) == 0 then
    redis.call('DEL', KEYS[1], KEYS[2], KEYS[3])
else
    redis.call('HSET', KEYS[1], '_bytes', bytes)
    for _, k in ipairs(KEYS) do redis.call('PEXPIRE', k, math.max(retention, fence) + 1000) end
end
redis.call('HDEL', KEYS[1], '_dirty')
return result
