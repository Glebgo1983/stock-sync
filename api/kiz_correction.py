import time
from api.insales_stock_client import insales_base_url, _request_with_retry
from api.insales_kiz_client import (
  INSALES_ORDER_PAGE_SIZE,
  CIM_JOIN_SEPARATOR,
  _existing_mpfit_id_value,
  _existing_kiz_codes,
  write_kiz,
)
from api.mpfit_stock_client import mpfit_base_url, _post_with_retry
from api.mpfit_cim_client import clean_cim

# One-off correction: every order written before commit 1f7ff2b has its КИЗ
# code(s) truncated (trim_cim used to drop the last 7 characters as a
# business rule that no longer applies). This re-derives the full,
# untruncated value from mpFit's cim-codes feed and rewrites any order
# whose stored value doesn't match. Temporary -- delete this file and its
# wiring in index.py once run.

CIM_SCAN_FROM = 12337649  # covers the КИЗ field's entire lifetime (created 2026-07-21) with margin
CIM_SCAN_MAX_PAGES = 60


async def _fetch_orders_with_kiz(client):
  to_check = {}
  from_id = 0
  while True:
    response = await _request_with_retry(
      client, "GET", insales_base_url + "orders.json",
      params={"per_page": INSALES_ORDER_PAGE_SIZE, "from_id": from_id},
    )
    orders = response.json()
    if not orders:
      break
    for order in orders:
      mpfit_id = _existing_mpfit_id_value(order)
      existing = _existing_kiz_codes(order)
      if mpfit_id and existing:
        to_check[order["id"]] = {
          "mpfit_id": mpfit_id,
          "number": order.get("number"),
          "existing": existing,
        }
    from_id = max(o["id"] for o in orders) + 1
    if len(orders) < INSALES_ORDER_PAGE_SIZE:
      break
  return to_check


async def _fetch_full_cim_map(client):
  cim_map = {}
  cursor = CIM_SCAN_FROM
  pages = 0
  while pages < CIM_SCAN_MAX_PAGES:
    data = await _post_with_retry(client, mpfit_base_url + "cim-codes", {"limit": 200, "last_id": cursor})
    page = data["result"]["data"]
    if not page:
      break
    for item in page:
      order_id = item.get("order_id")
      if order_id is None:
        continue
      cim_map.setdefault(str(order_id), []).append(clean_cim(item.get("cim")))
    pages += 1
    new_last_id = data["result"].get("last_id")
    reached_end = len(page) < 200 or new_last_id is None
    if new_last_id is not None:
      cursor = new_last_id
    if reached_end:
      break
  return cim_map, cursor, pages


async def run_kiz_correction(client, dry_run: bool):
  started_at = time.monotonic()
  to_check = await _fetch_orders_with_kiz(client)
  cim_map, scanned_to, pages_scanned = await _fetch_full_cim_map(client)

  corrected = []
  unresolved = []
  for order_id, info in to_check.items():
    full_codes = cim_map.get(str(info["mpfit_id"]))
    if not full_codes:
      unresolved.append({"order_id": order_id, "number": info["number"], "mpfit_id": info["mpfit_id"]})
      continue
    new_codes = list(dict.fromkeys(full_codes))
    old_value = CIM_JOIN_SEPARATOR.join(info["existing"])
    new_value = CIM_JOIN_SEPARATOR.join(new_codes)
    if new_value != old_value:
      if not dry_run:
        await write_kiz(client, order_id, new_value)
      corrected.append({
        "order_id": order_id,
        "number": info["number"],
        "old_value": old_value,
        "new_value": new_value,
      })

  return {
    "dry_run": dry_run,
    "orders_with_kiz_checked": len(to_check),
    "cim_pages_scanned": pages_scanned,
    "cim_scanned_to": scanned_to,
    "corrected_count": len(corrected),
    "corrected": corrected,
    "unresolved_count": len(unresolved),
    "unresolved": unresolved,
    "duration_ms": int((time.monotonic() - started_at) * 1000),
  }
