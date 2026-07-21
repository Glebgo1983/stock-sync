import os
from api.insales_stock_client import insales_base_url, _request_with_retry

INSALES_ORDER_PAGE_SIZE = 100

# id of the "КИЗ" custom order field (Настройки -> Параметры заказов, type
# Field::TextField). Created 2026-07-21, admin-only visibility confirmed via
# show_in_result/show_in_checkout/for_buyer = false. Override via env if the
# field is ever recreated (its id would change).
KIZ_FIELD_ID = int(os.getenv("INSALES_KIZ_FIELD_ID", "134636313"))


def _existing_kiz_value(order):
  for fv in order.get("fields_values", []):
    if fv.get("field_id") == KIZ_FIELD_ID:
      return fv.get("value")
  return None


async def fetch_orders_missing_kiz(client, max_orders=500):
  """Page through orders (oldest-id-first, same proven from_id pagination as
  fetch_variant_map) and return those without a КИЗ value yet.

  created_at_min/created_at_max look unsupported by this endpoint (tested:
  a narrow date range still returned the shop's full order history), so
  this doesn't try to bound by date -- only by max_orders, a safety cap
  against scanning an unbounded order history on every run.
  """
  candidates = []
  from_id = 0
  scanned = 0
  while scanned < max_orders:
    url = insales_base_url + "orders.json"
    params = {"per_page": INSALES_ORDER_PAGE_SIZE, "from_id": from_id}
    response = await _request_with_retry(client, "GET", url, params=params)
    orders = response.json()
    if not orders:
      break
    for order in orders:
      if _existing_kiz_value(order):
        continue
      candidates.append({"id": order["id"], "number": order.get("number")})
    scanned += len(orders)
    if len(orders) < INSALES_ORDER_PAGE_SIZE:
      break
    from_id = max(order["id"] for order in orders) + 1
  return candidates


async def write_kiz(client, order_id, value):
  url = insales_base_url + f"orders/{order_id}.json"
  body = {"order": {"fields_values_attributes": [{"field_id": KIZ_FIELD_ID, "value": value}]}}
  return await _request_with_retry(client, "PUT", url, json=body)
