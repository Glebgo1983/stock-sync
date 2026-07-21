import os
from api.mpfit_stock_client import mpfit_base_url, _post_with_retry

CIM_CODE_TRIM_LEN = 7

# Starting point for the exponential search below. The feed's real tail was
# ~12.76M when last measured (2026-07-21) via binary search (there is no API
# support for "give me the end" directly, or for filtering by order at all —
# both confirmed empirically). Starting near that instead of 0 keeps the
# search to a handful of requests; it only gets slower (not wrong) as the
# feed grows well past this, so it's fine to leave stale for a long time.
CIM_TAIL_SEARCH_HINT = int(os.getenv("SYNC_KIZ_TAIL_HINT", "10000000"))


def _recent_count():
  try:
    return int(os.getenv("SYNC_KIZ_RECENT_COUNT", "10"))
  except ValueError:
    return 10


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


async def fetch_recent_cim_map(client):
  """Fetch only the most recent SYNC_KIZ_RECENT_COUNT codes (default 10),
  grouped by mpFit order id, trimmed per business rule.

  No persisted checkpoint by design (explicit request, to avoid a hard Redis
  dependency) -- this always looks at whatever is newest on each run, not a
  contiguous window since the last run. If more than SYNC_KIZ_RECENT_COUNT
  codes land between runs, the ones in between are never seen. Accepted
  tradeoff for a lightweight, Redis-free check; already-filled inSales
  orders are skipped either way, so re-seeing the same recent codes on
  every run is harmless.
  """
  tail_last_id = await _find_tail_last_id(client)
  data = await _post_with_retry(
    client, mpfit_base_url + "cim-codes", {"limit": _recent_count(), "last_id": tail_last_id},
  )
  order_map = {}
  for item in data["result"]["data"]:
    order_id = item.get("order_id")
    if order_id is None:
      continue
    order_map.setdefault(str(order_id), []).append(trim_cim(item.get("cim")))
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
