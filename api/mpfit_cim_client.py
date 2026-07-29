import os
import redis
from api.mpfit_stock_client import mpfit_base_url, _post_with_retry

CIM_CODE_TRIM_LEN = 7

redis_url = os.getenv("REDIS_URL")

# Starting point for the exponential search below. The feed's real tail was
# ~12.76M when last measured (2026-07-21) via binary search (there is no API
# support for "give me the end" directly, or for filtering by order at all —
# both confirmed empirically). Starting near that instead of 0 keeps the
# search to a handful of requests; it only gets slower (not wrong) as the
# feed grows well past this, so it's fine to leave stale for a long time.
# Only used now to bootstrap the cursor below on its very first run.
CIM_TAIL_SEARCH_HINT = int(os.getenv("SYNC_KIZ_TAIL_HINT", "10000000"))

# The /cim-codes endpoint rejects any limit above this with a 422 (confirmed
# empirically 2026-07-27 -- 1000 and 5000 both failed, 200 works).
CIM_CODES_MAX_LIMIT = 200

CIM_CURSOR_REDIS_KEY = "mpfit_sync:kiz_cim_cursor"

# Caps how many pages a single run scans forward from the stored cursor --
# keeps one run comfortably within mpFit's 60/min rate limit and
# cron-job.org's 30s timeout (confirmed capped 2026-07-27, can't be raised).
# If a run's real backlog is bigger than this, the cursor still advances as
# far as it got and the next run picks up exactly there -- delayed, never
# skipped.
CIM_SCAN_MAX_PAGES = 40

# One-time bootstrap only, when no cursor has been saved yet (first deploy
# of this mechanism, or a fresh Redis instance): start this far back from
# the real tail instead of from last_id 0, which would scan the entire
# platform-wide feed history since it began. ~15 days of headroom at the
# growth rate measured 2026-07-28 (~30k id units/day) -- generous enough to
# catch any order still realistically awaiting its КИЗ code.
CIM_CURSOR_BOOTSTRAP_LOOKBACK = 500_000


def trim_cim(cim):
  code = (cim or "").strip()
  return code[:-CIM_CODE_TRIM_LEN] if len(code) > CIM_CODE_TRIM_LEN else code


async def _has_data_after(client, last_id):
  data = await _post_with_retry(client, mpfit_base_url + "cim-codes", {"limit": 1, "last_id": last_id})
  return len(data["result"]["data"]) > 0


async def _find_tail_last_id(client):
  """Locate a last_id close to the current end of the /cim-codes feed.

  Exponential search outward from CIM_TAIL_SEARCH_HINT to bracket the true
  end, then binary search to converge on it. Costs roughly
  2*log2(distance from hint to the true end) requests -- a handful in
  practice as long as the hint stays in the right order of magnitude.
  """
  lo = 0
  hi = max(CIM_TAIL_SEARCH_HINT, 1)
  if not await _has_data_after(client, hi):
    # Hint overshot (feed shrank or hint was set too high) -- search inward.
    lo, hi = 0, hi
  else:
    lo = hi
    hi *= 2
    while await _has_data_after(client, hi):
      lo = hi
      hi *= 2
  while hi - lo > 1:
    mid = (lo + hi) // 2
    if await _has_data_after(client, mid):
      lo = mid
    else:
      hi = mid
  return lo


def _redis():
  return redis.Redis.from_url(redis_url, decode_responses=True)


async def _load_cursor(client):
  try:
    value = _redis().get(CIM_CURSOR_REDIS_KEY)
  except Exception as e:
    # Covers both a reachable-but-erroring Redis and REDIS_URL being unset
    # entirely (from_url(None) raises AttributeError, not a RedisError) --
    # either way, degrade to a fresh bootstrap rather than failing the sync.
    print(f"cim cursor read failed, bootstrapping instead: {e}")
    value = None
  if value is not None:
    return int(value)
  tail_last_id = await _find_tail_last_id(client)
  return max(tail_last_id - CIM_CURSOR_BOOTSTRAP_LOOKBACK, 0)


def _save_cursor(value):
  try:
    _redis().set(CIM_CURSOR_REDIS_KEY, str(value))
  except Exception as e:
    print(f"cim cursor save failed (Redis unavailable?), next run will re-bootstrap: {e}")


async def fetch_cim_map_since_cursor(client):
  """Fetch every /cim-codes record from the last run's saved cursor up to
  the current tail, grouped by mpFit order id, trimmed per business rule.

  Replaces the old "last N records" recency window: that approach silently
  and permanently lost codes once enough *other* mpFit clients' traffic (the
  feed is shared platform-wide, not just this shop) pushed a code past the
  small recent window before this integration's own order-matching caught up
  to it -- confirmed happening in production 2026-07-28 (mpFit order
  19508976, shipped 2026-07-26, КИЗ code lost). A persisted cursor means a
  run can never skip a record: worst case a big backlog takes a few runs
  (capped at CIM_SCAN_MAX_PAGES each) to fully catch up, never a permanent
  loss.
  """
  cursor = await _load_cursor(client)
  order_map = {}
  pages = 0
  while pages < CIM_SCAN_MAX_PAGES:
    data = await _post_with_retry(client, mpfit_base_url + "cim-codes", {"limit": 200, "last_id": cursor})
    page = data["result"]["data"]
    for item in page:
      order_id = item.get("order_id")
      if order_id is None:
        continue
      order_map.setdefault(str(order_id), []).append(trim_cim(item.get("cim")))
    pages += 1
    new_last_id = data["result"].get("last_id")
    reached_end = len(page) < 200 or new_last_id is None
    if new_last_id is not None:
      cursor = new_last_id
    if reached_end:
      break
  _save_cursor(cursor)
  return order_map


async def resolve_order_numbers(client, mpfit_order_ids):
  """Resolve mpFit order id -> mpFit order `number`.

  For orders created by this integration, `number` was set to the inSales
  order id at creation time (see api/functions.py:create_order), so this is
  how we join mpFit-side cim-codes back to an inSales order. Orders mpFit
  fulfills for other channels (e.g. Wildberries) have unrelated `number`
  values — those simply won't match any real inSales order id downstream,
  which is the intended filtering behavior, not an error case.
  """
  ids = list(mpfit_order_ids)
  numbers = {}
  for i in range(0, len(ids), 200):
    batch = ids[i:i + 200]
    body = {"limit": 200, "last_id": 0, "filter": {"ids": batch}}
    data = await _post_with_retry(client, mpfit_base_url + "orders/list", body)
    for order in data["result"]["data"]:
      numbers[str(order["id"])] = order.get("number")
  return numbers
