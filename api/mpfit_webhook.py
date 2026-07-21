import os
from dotenv import load_dotenv
from api.insales_stock_client import push_quantities
from api.mpfit_stock_client import compute_available_qty
from api.product_map import load_product_map
from api.sync_journal import append_entries, build_entry

load_dotenv(dotenv_path=".env.local")

webhook_secret = os.getenv("MPFIT_WEBHOOK_SECRET")


def _extract_product_payload(body):
  """mpFit's exact webhook envelope isn't confirmed against official docs
  (the docs site is JS-rendered and didn't expose it when checked). This
  defensively unwraps the common envelope shapes vendors use and falls back
  to the raw body — the assumption is the product entity itself (article/id/
  stocks) matches the shape already confirmed from `products/stocks`.
  """
  if not isinstance(body, dict):
    return {}
  for key in ("data", "payload", "object", "product"):
    nested = body.get(key)
    if isinstance(nested, dict):
      return nested
  return body


def parse_stock_event(body):
  """Extract article/mpfit_id/qty from a webhook payload.

  `has_stock_data` is deliberately tracked separately from `qty`: mpFit
  exposes 30+ webhook event types and most likely not all of them (e.g.
  order-lifecycle events) carry a full per-warehouse stock breakdown for the
  product. If `stocks` is simply absent from the payload, we must NOT infer
  qty=0 — that would zero out a real stock level based on missing data
  rather than an actual change. Callers should skip acting when
  `has_stock_data` is False and let the periodic sync catch up instead.
  """
  product = _extract_product_payload(body)
  article = (product.get("article") or "").strip()
  item_id = product.get("id")
  mpfit_id = str(item_id) if item_id is not None else None
  stocks = product.get("stocks")
  has_stock_data = isinstance(stocks, list)
  qty = compute_available_qty(stocks) if has_stock_data else None
  return {
    "mpfit_id": mpfit_id,
    "article": article,
    "qty": qty,
    "has_stock_data": has_stock_data,
  }


async def handle_stock_event(client, body):
  """Apply one mpFit stock-change webhook event, if we can.

  Only acts on products that already have a persisted ID-based link
  (api/product_map.py) — a brand-new product first needs the article-based
  match from a periodic sync to establish that link. This keeps the webhook
  path simple and avoids guessing at an unconfirmed inSales single-sku
  lookup endpoint; subsequent webhooks for the same product resolve
  instantly once the link exists.
  """
  event = parse_stock_event(body)

  if not event["has_stock_data"]:
    return {"applied": False, "reason": "event_payload_missing_stock_data"}

  if not event["mpfit_id"]:
    return {"applied": False, "reason": "no_mpfit_id_in_payload"}

  try:
    product_map = load_product_map()
  except Exception as e:
    print(f"webhook: product map unavailable: {e}")
    return {"applied": False, "reason": "product_map_unavailable"}

  link = product_map.get(event["mpfit_id"])
  if link is None:
    return {"applied": False, "reason": "no_persisted_link_yet"}

  variant_id = link["insales_variant_id"]
  info = {
    "sku": event["article"] or link.get("sku"),
    "product_id": link.get("insales_product_id"),
    "title": None,
    # Not fetched here to avoid an extra inSales round trip per webhook
    # event on an unconfirmed single-variant lookup endpoint — the periodic
    # sync's journal entries carry the accurate previous/new pair instead.
    "previous_qty": None,
    "new_qty": event["qty"],
    "mpfit_id": event["mpfit_id"],
  }

  batches = await push_quantities(client, {variant_id: event["qty"]})
  batch = batches[0] if batches else {}
  result = "error" if "error" in batch else "ok"
  entry = build_entry(variant_id, info, result, batch.get("error"))
  try:
    append_entries([entry])
  except Exception as e:
    print(f"webhook: journal write failed: {e}")

  return {
    "applied": result == "ok",
    "mpfit_id": event["mpfit_id"],
    "variant_id": variant_id,
    "new_qty": event["qty"],
    "result": result,
  }
