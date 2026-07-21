import time
import httpx
from api.mpfit_cim_client import fetch_cim_map, resolve_order_numbers
from api.insales_kiz_client import fetch_orders_missing_kiz, write_kiz

CIM_JOIN_SEPARATOR = ", "


async def run_kiz_sync(dry_run: bool):
  started_at = time.monotonic()
  async with httpx.AsyncClient(timeout=30) as client:
    candidates = await fetch_orders_missing_kiz(client)
    if not candidates:
      return {
        "dry_run": dry_run,
        "candidates_checked": 0,
        "matched": 0,
        "written": 0,
        "errors": [],
        "duration_ms": int((time.monotonic() - started_at) * 1000),
      }

    cim_by_mpfit_order = await fetch_cim_map(client)
    numbers = await resolve_order_numbers(client, cim_by_mpfit_order.keys())

    # Regroup codes by mpFit order `number` -- for our own orders this equals
    # the inSales order id, which is what we can match candidates against.
    codes_by_number = {}
    for mpfit_id, codes in cim_by_mpfit_order.items():
      number = numbers.get(mpfit_id)
      if number is not None:
        codes_by_number.setdefault(str(number), []).extend(codes)

    matched = []
    for candidate in candidates:
      codes = codes_by_number.get(str(candidate["id"]))
      if codes:
        matched.append({"order_id": candidate["id"], "value": CIM_JOIN_SEPARATOR.join(codes)})

    written = 0
    errors = []
    if not dry_run:
      for entry in matched:
        try:
          await write_kiz(client, entry["order_id"], entry["value"])
          written += 1
        except httpx.HTTPStatusError as e:
          errors.append({"order_id": entry["order_id"], "error": e.response.text})

    return {
      "dry_run": dry_run,
      "candidates_checked": len(candidates),
      "matched": len(matched),
      "written": written,
      "errors": errors,
      "duration_ms": int((time.monotonic() - started_at) * 1000),
    }
