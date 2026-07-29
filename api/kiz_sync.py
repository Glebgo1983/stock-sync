import time
import httpx
from api.mpfit_cim_client import fetch_cim_map_since_cursor, resolve_order_numbers
from api.insales_kiz_client import fetch_orders_missing_kiz, write_kiz, write_mpfit_id
from api.kiz_heuristic_match import fetch_all_mpfit_orders, match_candidates
from api.sync_config import get_sku_aliases

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

    cim_by_mpfit_order = await fetch_cim_map_since_cursor(client, persist=not dry_run)

    # Orders created after the "MPfit id" field existed (2026-07-27) carry
    # their mpFit order id directly -- match those straight off
    # cim_by_mpfit_order, no resolution needed. Older orders have no value
    # there and fall back to the previous number-matching path, which
    # requires resolving mpFit order id -> `number` first. `number` turned
    # out unreliable as a join key on its own (see insales_kiz_client.py) --
    # this fallback is best-effort for pre-existing orders only.
    needs_number_match = [c for c in candidates if not c.get("mpfit_id")]
    numbers = await resolve_order_numbers(client, cim_by_mpfit_order.keys()) if needs_number_match else {}
    codes_by_number = {}
    for mpfit_id, codes in cim_by_mpfit_order.items():
      number = numbers.get(mpfit_id)
      if number is not None:
        codes_by_number.setdefault(str(number), []).extend(codes)

    # Second-line fallback for candidates still without an mpfit_id: `number`
    # only works for orders this integration's own create_order actually ran
    # for, which today is effectively none (inSales has no webhook configured
    # to /api/create -- see project memory). Most real orders reach mpFit via
    # ApiShip under mpFit's own auto-number instead, so match those by exact
    # item-set (sku/article + quantity) plus a loose creation-time window.
    # Mutates each candidate dict in place so the lookup below picks it up.
    heuristic_errors = []
    if needs_number_match:
      mpfit_orders = await fetch_all_mpfit_orders(client)
      heuristic_matches = match_candidates(needs_number_match, mpfit_orders, get_sku_aliases())
      for candidate in needs_number_match:
        mpfit_id = heuristic_matches.get(candidate["id"])
        if not mpfit_id:
          continue
        candidate["mpfit_id"] = str(mpfit_id)
        if not dry_run:
          try:
            await write_mpfit_id(client, candidate["id"], mpfit_id)
          except httpx.HTTPStatusError as e:
            heuristic_errors.append({"order_id": candidate["id"], "error": e.response.text})

    matched = []
    for candidate in candidates:
      if candidate.get("mpfit_id"):
        codes = cim_by_mpfit_order.get(str(candidate["mpfit_id"]))
      else:
        codes = codes_by_number.get(str(candidate["id"]))
      if codes:
        matched.append({"order_id": candidate["id"], "value": CIM_JOIN_SEPARATOR.join(codes)})

    written = 0
    errors = list(heuristic_errors)
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
