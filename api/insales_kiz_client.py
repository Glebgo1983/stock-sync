import html
import os
import redis
from datetime import datetime
from api.insales_stock_client import insales_base_url, _request_with_retry

INSALES_ORDER_PAGE_SIZE = 100

# Shared with kiz_sync.py: how multiple КИЗ codes for the same order are
# joined into the single custom field's string value.
CIM_JOIN_SEPARATOR = ", "

redis_url = os.getenv("REDIS_URL")

# Two independent cursors -- see fetch_orders_missing_kiz docstring for why
# one cursor alone (either kind) isn't enough:
# - "tail" always advances every run regardless of what it finds, so recent
#   orders are always reached within a few runs no matter how large or
#   permanently-unresolved the older backlog is.
# - "retry_floor" only advances past orders already confirmed to have a
#   КИЗ, so a still-open order is never dropped from consideration just
#   because it's old.
ORDER_SCAN_TAIL_REDIS_KEY = "insales_sync:kiz_order_scan_tail"
ORDER_SCAN_RETRY_FLOOR_REDIS_KEY = "insales_sync:kiz_order_scan_retry_floor"

# Caps how many pages each of the two scans below does per run -- mirrors
# CIM_SCAN_MAX_PAGES (mpfit_cim_client.py): keeps one run within
# cron-job.org's 30s timeout and inSales rate limits. If a scan's window is
# bigger than its budget, its cursor still advances as far as it got and
# the next run resumes exactly there -- delayed, never skipped.
#
# Lowered from 20 to 8 (2026-08-21): measured ~680ms/page against real
# inSales data, so 20+20=40 pages cost ~27s on its own -- combined with the
# other two scan phases in run_kiz_sync, this was the main reason the whole
# run kept exceeding cron-job.org's 30s cap after the 16-day backlog from
# the job being disabled (2026-08-05 to 2026-08-21, see project memory).
# 8+8=16 pages costs ~11s, leaving comfortable headroom; a bigger backlog
# just takes proportionally more runs to catch up, same as before.
ORDER_SCAN_TAIL_MAX_PAGES = 8
ORDER_SCAN_RETRY_MAX_PAGES = 8

# id of the "КИЗ" custom order field (Настройки -> Параметры заказов, type
# Field::TextField). Created 2026-07-21, admin-only visibility confirmed via
# show_in_result/show_in_checkout/for_buyer = false. Override via env if the
# field is ever recreated (its id would change).
KIZ_FIELD_ID = int(os.getenv("INSALES_KIZ_FIELD_ID", "134636313"))

# id of the "MPfit id" custom order field -- stores the mpFit order's own
# internal id, written at creation time (api/functions.py:new_order_handler)
# right after mpFit returns it. Created 2026-07-27. This exists because
# matching by mpFit's `number` field turned out unreliable: order 1550088401
# had no mpFit order at all under that number despite a real, shipped mpFit
# order existing for it -- `number` isn't a trustworthy join key. Storing the
# id we already got back from mpFit at creation time removes the guesswork
# entirely for any order created after this field existed. Orders created
# before it have no value here and fall back to the old number-matching path.
MPFIT_ID_FIELD_ID = int(os.getenv("INSALES_MPFIT_ID_FIELD_ID", "134982953"))


def _existing_kiz_value(order):
  for fv in order.get("fields_values", []):
    if fv.get("field_id") == KIZ_FIELD_ID:
      return fv.get("value")
  return None


def _existing_kiz_codes(order):
  # inSales returns this TextField's value with special characters
  # (', <, &, ...) HTML-entity-encoded (e.g. "'" -> "&#39;") -- confirmed
  # 2026-07-31 when a raw-vs-encoded mismatch made a correction script
  # rewrite the same two codes back and forth forever, every run. Decoding
  # here keeps every comparison against mpFit's raw codes correct no matter
  # how inSales chooses to represent the stored value.
  value = html.unescape(_existing_kiz_value(order) or "")
  return [c.strip() for c in value.split(CIM_JOIN_SEPARATOR) if c.strip()] if value else []


def _expected_kiz_count(order_lines):
  """Orders can have multiple items/units, each with its own КИЗ code -- an
  order isn't fully resolved just because it has *a* value, only once it has
  one code per unit. Falls back to 1 when order_lines is unavailable, same
  as the old any-value-means-done behavior for that case.
  """
  if not order_lines:
    return 1
  return max(sum(int(line.get("quantity") or 1) for line in order_lines), 1)


def _existing_mpfit_id_value(order):
  for fv in order.get("fields_values", []):
    if fv.get("field_id") == MPFIT_ID_FIELD_ID:
      return fv.get("value")
  return None


def _redis():
  return redis.Redis.from_url(redis_url, decode_responses=True)


def _load_int(key, default):
  try:
    value = _redis().get(key)
  except Exception as e:
    # Covers both a reachable-but-erroring Redis and REDIS_URL being unset
    # entirely -- either way, degrade to the default rather than failing
    # the sync.
    print(f"kiz order scan cursor read failed for {key}, defaulting to {default}: {e}")
    return default
  return int(value) if value is not None else default


def _save_int(key, value):
  try:
    _redis().set(key, str(value))
  except Exception as e:
    print(f"kiz order scan cursor save failed for {key} (Redis unavailable?): {e}")


def _make_candidate(order):
  mpfit_id = _existing_mpfit_id_value(order)
  order_lines = order.get("order_lines") or []
  created_at = order.get("created_at")
  candidate = {
    "id": order["id"],
    "number": order.get("number"),
    "mpfit_id": mpfit_id,
    "existing_codes": _existing_kiz_codes(order),
    "expected_kiz_count": _expected_kiz_count(order_lines),
    "created_at": datetime.fromisoformat(created_at) if created_at else None,
  }
  # order_lines itself is only needed by the heuristic matcher for candidates
  # still missing an mpfit_id -- skip keeping it around otherwise.
  if not mpfit_id:
    candidate["order_lines"] = order_lines
  return candidate


async def _scan_orders_missing_kiz(client, from_id, max_pages):
  """Page forward from from_id (oldest-id-first, same from_id pagination as
  fetch_variant_map), collecting orders still missing a КИЗ, up to
  max_pages. Returns (candidates_by_id, next_from_id, found_any_open).
  """
  candidates_by_id = {}
  found_open = False
  pages = 0
  while pages < max_pages:
    url = insales_base_url + "orders.json"
    params = {"per_page": INSALES_ORDER_PAGE_SIZE, "from_id": from_id}
    response = await _request_with_retry(client, "GET", url, params=params)
    orders = response.json()
    if not orders:
      break
    for order in orders:
      existing_codes = _existing_kiz_codes(order)
      expected = _expected_kiz_count(order.get("order_lines") or [])
      if len(existing_codes) >= expected:
        continue
      found_open = True
      candidates_by_id[order["id"]] = _make_candidate(order)
    pages += 1
    from_id = max(order["id"] for order in orders) + 1
    if len(orders) < INSALES_ORDER_PAGE_SIZE:
      break
  return candidates_by_id, from_id, found_open


async def fetch_orders_missing_kiz(client, persist=True):
  """Return every inSales order still missing a КИЗ value, via two
  independent forward scans over the same from_id pagination:

  Replaces the old design (fixed max_orders=500, always starting at
  from_id=0): once the shop's total order count passed ~500, that could
  only ever see the oldest ~500 orders in the shop's history and
  structurally could never reach recently-created ones -- confirmed root
  cause of "recent orders never get КИЗ" 2026-07-30.

  A single moving cursor can't fix this correctly on its own: advancing
  past every order it scans (regardless of outcome) reaches recent orders
  reliably, but permanently drops any order that's still genuinely waiting
  on its КИЗ (it may arrive on mpFit's side much later -- there's no way to
  know an order will never resolve). Advancing only past *confirmed
  resolved* orders avoids that loss, but can then get stuck forever behind
  a single old order that never resolves, and would then never reach new
  orders again -- reproducing this same bug in a different shape.

  So this uses both, independently:
  - "tail" scan: always advances forward every run regardless of what it
    finds, guaranteeing recent orders are reached within a few runs no
    matter how large or permanently-unresolved the older backlog is.
  - "retry" scan: only advances past orders already confirmed to have a
    КИЗ, so a still-open order is retried on every run until it resolves,
    no matter how old it is or how far the tail scan has moved past it.
  Each is capped at its own *_MAX_PAGES per run, mirroring
  mpFit's cim-codes cursor (mpfit_cim_client.py) for the same
  rate-limit/timeout reasons; a bigger backlog just takes more runs to
  fully catch up, never a skip.

  persist=False (dry runs) must not move either saved cursor forward,
  mirroring fetch_cim_map_since_cursor's persist=False handling -- a dry
  run must not affect what the next real run scans.

  created_at_min/created_at_max look unsupported by this endpoint (tested:
  a narrow date range still returned the shop's full order history), so
  this doesn't try to bound by date -- only by the two scans' page caps.
  """
  tail = _load_int(ORDER_SCAN_TAIL_REDIS_KEY, 0)
  retry_floor = _load_int(ORDER_SCAN_RETRY_FLOOR_REDIS_KEY, 0)

  tail_candidates, next_tail, _ = await _scan_orders_missing_kiz(client, tail, ORDER_SCAN_TAIL_MAX_PAGES)
  retry_candidates, next_retry_floor, retry_found_open = await _scan_orders_missing_kiz(
    client, retry_floor, ORDER_SCAN_RETRY_MAX_PAGES
  )

  merged = {**retry_candidates, **tail_candidates}

  if persist:
    _save_int(ORDER_SCAN_TAIL_REDIS_KEY, next_tail)
    # Only advance the retry floor past confirmed-resolved orders -- if this
    # run's retry window found nothing open, it's safe to jump straight to
    # wherever it scanned up to; otherwise stay at the first still-open id
    # seen, so it's retried again next run.
    new_retry_floor = min(retry_candidates) if retry_found_open else next_retry_floor
    _save_int(ORDER_SCAN_RETRY_FLOOR_REDIS_KEY, new_retry_floor)

  return list(merged.values())


async def write_kiz(client, order_id, value):
  url = insales_base_url + f"orders/{order_id}.json"
  body = {"order": {"fields_values_attributes": [{"field_id": KIZ_FIELD_ID, "value": value}]}}
  return await _request_with_retry(client, "PUT", url, json=body)


async def write_mpfit_id(client, order_id, mpfit_id):
  url = insales_base_url + f"orders/{order_id}.json"
  body = {"order": {"fields_values_attributes": [{"field_id": MPFIT_ID_FIELD_ID, "value": str(mpfit_id)}]}}
  return await _request_with_retry(client, "PUT", url, json=body)
