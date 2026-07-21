import os
from api.mpfit_stock_client import mpfit_base_url, _post_with_retry

CIM_PAGE_LIMIT = 200
CIM_CODE_TRIM_LEN = 7


def _cim_max_pages():
  try:
    return int(os.getenv("SYNC_KIZ_MAX_PAGES", "50"))
  except ValueError:
    return 50


def trim_cim(cim):
  code = (cim or "").strip()
  return code[:-CIM_CODE_TRIM_LEN] if len(code) > CIM_CODE_TRIM_LEN else code


async def fetch_cim_map(client):
  """Paginate mpFit's /cim-codes feed, grouping trimmed codes by mpFit order id.

  Confirmed empirically: the endpoint ignores every filter shape tried
  (`filter.order_id`, `filter.order_ids`, `filter.ids`, a bare `order_id`) —
  it's a flat feed walked only via `last_id`, so there's no way to ask for a
  single order's codes directly. The feed spans every sales channel mpFit
  fulfills (not just inSales) and only grows over time, so this scan is
  capped at SYNC_KIZ_MAX_PAGES (default 50 pages / 10k codes) to avoid an
  ever-slower walk eventually hitting the serverless execution time limit.
  By design there's no persisted checkpoint (accepted tradeoff — see
  README); orders whose codes fall past the cap won't be found until
  reprocessed on a later run.
  """
  order_map = {}
  last_id = 0
  for _ in range(_cim_max_pages()):
    body = {"limit": CIM_PAGE_LIMIT, "last_id": last_id}
    data = await _post_with_retry(client, mpfit_base_url + "cim-codes", body)
    result = data["result"]
    items = result["data"]
    for item in items:
      order_id = item.get("order_id")
      if order_id is None:
        continue
      order_map.setdefault(str(order_id), []).append(trim_cim(item.get("cim")))
    if len(items) < CIM_PAGE_LIMIT or result.get("last_id") is None:
      break
    last_id = result["last_id"]
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
