from datetime import datetime, timedelta
from api.mpfit_stock_client import mpfit_base_url, _post_with_retry

MPFIT_ORDERS_PAGE_SIZE = 200

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


async def fetch_all_mpfit_orders(client):
  """Page through mpFit's entire orders/list, unfiltered -- the `filter`
  param only supports matching by mpFit's own id (see resolve_order_numbers),
  which is exactly what we don't know yet here. Confirmed cheap: mpFit's
  whole order history was only ~15-16 pages of 200 (2026-07-27), safe to
  scan in full on every sync run.
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
