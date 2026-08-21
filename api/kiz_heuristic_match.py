import os
import re
import time
import httpx
from datetime import datetime, timedelta, timezone
from api.mpfit_stock_client import mpfit_base_url, _post_with_retry

MPFIT_ORDERS_PAGE_SIZE = 200

# Optional external cache: a small always-on service (outside Vercel's 30s
# cron-job.org budget) that polls mpFit's entire order history on its own
# schedule and serves the last-known-good result over HTTPS. Added
# 2026-08-21 after the direct full-history scan below (previously the only
# path) grew to ~20-24s on its own -- combined with the other two scan
# phases in run_kiz_sync, that regularly pushed the whole /api/sync-kiz run
# past cron-job.org's 30s cap, silently auto-disabling the КИЗ sync job for
# 16 days (2026-08-05 to 2026-08-21) with zero visibility. Both env vars
# unset (the default, e.g. in Preview) means this is skipped entirely and
# behavior is identical to before -- the cache is purely additive.
KIZ_CACHE_URL = os.getenv("KIZ_CACHE_URL")
KIZ_CACHE_SECRET = os.getenv("KIZ_CACHE_SECRET")

# If the cache's own background refresh has stalled (its host down, mpFit
# unreachable from there, etc.), don't trust arbitrarily old data forever --
# fall back to the direct scan instead, same as any other cache failure.
KIZ_CACHE_MAX_AGE_SECONDS = 1800

# ApiShip encodes the inSales order's own display `number` into mpFit's
# order `number` field for orders placed via Yandex Delivery, as
# "YANDEX-ASK<number>" -- discovered 2026-08-03 investigating inSales order
# #1115 (a single-item order of a common product, which the item+time
# heuristic below couldn't disambiguate: 51 unrelated mpFit orders shared
# its exact signature within MATCH_TIME_WINDOW). Confirmed against the
# *entire* mpFit order history (2026-08-03): 11/11 orders with "ASK" in
# their number use this exact "YANDEX-" prefix, no other variant seen --
# including id 19657458 (number "YANDEX-ASK1088"), which independently
# matches inSales order 1554245481 (number 1088), already confirmed correct
# by hand in an earlier cim-code recovery. Far more reliable than item-set
# matching where it applies; only candidates still unmatched after this
# need the heuristic fallback.
ASK_NUMBER_RE = re.compile(r'ASK0*(\d+)\s*$', re.IGNORECASE)

# Confirmed real-world gap between an inSales order's created_at and its
# matching mpFit order's created_at was ~14h (inSales 1550088401 -> mpFit
# 19508774, 2026-07-27, created via ApiShip). This is generous slack for
# aggregator lag while still tight enough that an unrelated order with the
# same item composition is unlikely to fall inside the window.
MATCH_TIME_WINDOW = timedelta(days=5)


def _item_signature(items, sku_aliases=None):
  sku_aliases = sku_aliases or {}
  pairs = [(sku_aliases.get(sku, sku), quantity) for sku, quantity in items]
  return tuple(sorted(pairs))


async def _fetch_all_mpfit_orders_direct(client):
  """Page through mpFit's entire orders/list, unfiltered -- the `filter`
  param only supports matching by mpFit's own id (see resolve_order_numbers),
  which is exactly what we don't know yet here. This was cheap when first
  written (mpFit's whole order history was only ~15-16 pages of 200,
  2026-07-27) but by 2026-08-21 had grown to ~21 pages / ~24s on its own --
  see fetch_all_mpfit_orders below for the cache that now normally avoids
  paying this cost on every run. Kept as the fallback when the cache is
  unavailable or stale.
  """
  orders = []
  last_id = 0
  while True:
    body = {"limit": MPFIT_ORDERS_PAGE_SIZE, "last_id": last_id}
    data = await _post_with_retry(client, mpfit_base_url + "orders/list", body)
    result = data["result"]
    page = result["data"]
    orders.extend(page)
    if len(page) < MPFIT_ORDERS_PAGE_SIZE or result.get("last_id") is None:
      break
    last_id = result["last_id"]
  return orders


async def _fetch_from_cache():
  if not KIZ_CACHE_URL or not KIZ_CACHE_SECRET:
    return None
  try:
    # verify=False: the cache runs on a bare-IP VPS with a self-signed cert
    # (no domain available) -- KIZ_CACHE_SECRET is what actually
    # authenticates the response, TLS here is only for confidentiality of
    # order data in transit, not identity verification.
    async with httpx.AsyncClient(verify=False, timeout=8) as cache_client:
      response = await cache_client.get(KIZ_CACHE_URL, params={"key": KIZ_CACHE_SECRET})
    response.raise_for_status()
    data = response.json()
    updated_at = datetime.fromisoformat(data["updated_at"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
    if age > KIZ_CACHE_MAX_AGE_SECONDS:
      print(f"kiz orders cache stale ({age:.0f}s old), falling back to direct scan")
      return None
    return data["orders"]
  except Exception as e:
    print(f"kiz orders cache fetch failed, falling back to direct scan: {e}")
    return None


async def fetch_all_mpfit_orders(client):
  """Returns mpFit's entire order history. Prefers the external cache (see
  KIZ_CACHE_URL above) when configured and fresh; falls back to scanning
  mpFit directly (_fetch_all_mpfit_orders_direct) otherwise, so this
  degrades gracefully to the pre-cache behavior rather than failing the
  whole sync run.
  """
  cached = await _fetch_from_cache()
  if cached is not None:
    return cached
  return await _fetch_all_mpfit_orders_direct(client)


def _build_signature_index(mpfit_orders, sku_aliases=None):
  index = {}
  for order in mpfit_orders:
    raw_items = order.get("items") or []
    # Some mpFit order items have no linked product (deleted/unlinked
    # product) -- `item["product"]` is None, not a missing key. An order's
    # signature can't be trusted if any of its items are like this, so skip
    # the whole order rather than build a partial/wrong signature.
    if any(not item.get("product") for item in raw_items):
      continue
    items = [(item["product"]["article"], item["quantity"]) for item in raw_items]
    if not items:
      continue
    signature = _item_signature(items, sku_aliases)
    index.setdefault(signature, []).append(order)
  return index


def match_by_ask_number(candidates, mpfit_orders):
  """Match candidates lacking mpfit_id via the "YANDEX-ASK<number>" pattern
  (see ASK_NUMBER_RE above) against each candidate's own inSales display
  `number`. Same one-match-only and global-uniqueness rules as
  match_candidates below, for the same reason: never guess.
  """
  index = {}
  for order in mpfit_orders:
    number = order.get("number")
    if not number:
      continue
    m = ASK_NUMBER_RE.search(str(number))
    if not m:
      continue
    index.setdefault(int(m.group(1)), []).append(order)

  matches = {}
  for candidate in candidates:
    number = candidate.get("number")
    if number is None:
      continue
    same = index.get(int(number))
    if same and len(same) == 1:
      matches[candidate["id"]] = same[0]["id"]

  claim_counts = {}
  for mpfit_id in matches.values():
    claim_counts[mpfit_id] = claim_counts.get(mpfit_id, 0) + 1
  return {
    candidate_id: mpfit_id
    for candidate_id, mpfit_id in matches.items()
    if claim_counts[mpfit_id] == 1
  }


def match_candidates(candidates, mpfit_orders, sku_aliases=None):
  """For each candidate (dict with `id`, `order_lines`, `created_at`) that
  has no stored mpfit_id yet, look for an unambiguous mpFit order match by
  exact item-set (sku/article + quantity) plus a loose creation-time window.

  Returns {candidate_id: mpfit_order_id}. Zero or multiple item-set matches
  within the window are skipped, never guessed -- see the project memory on
  this investigation for why (`number` already turned out unreliable once).

  A single mpFit order can also only ever belong to one inSales order, so a
  second pass drops any mpfit_id that ends up claimed by more than one
  candidate here -- confirmed to happen in practice (common product
  combinations reordered a few days apart genuinely collide within
  MATCH_TIME_WINDOW), and there's no way to tell which candidate is the real
  match, so both/all are dropped rather than guessed.
  """
  index = _build_signature_index(mpfit_orders, sku_aliases)
  matches = {}
  for candidate in candidates:
    order_lines = candidate.get("order_lines")
    created_at = candidate.get("created_at")
    if not order_lines or created_at is None:
      continue
    items = [(line["sku"], line["quantity"]) for line in order_lines]
    signature = _item_signature(items, sku_aliases)
    same_signature = index.get(signature)
    if not same_signature:
      continue
    in_window = [
      order for order in same_signature
      if abs(datetime.fromisoformat(order["created_at"]) - created_at) <= MATCH_TIME_WINDOW
    ]
    if len(in_window) == 1:
      matches[candidate["id"]] = in_window[0]["id"]

  claim_counts = {}
  for mpfit_id in matches.values():
    claim_counts[mpfit_id] = claim_counts.get(mpfit_id, 0) + 1
  return {
    candidate_id: mpfit_id
    for candidate_id, mpfit_id in matches.items()
    if claim_counts[mpfit_id] == 1
  }
