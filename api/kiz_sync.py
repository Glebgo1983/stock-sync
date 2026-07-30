import time
import httpx
from api.mpfit_cim_client import fetch_cim_map_since_cursor, resolve_order_numbers, _load_cursor as _load_cim_cursor, trim_cim
from api.mpfit_stock_client import mpfit_base_url, _post_with_retry
from api.insales_kiz_client import fetch_orders_missing_kiz, write_kiz, write_mpfit_id
from api.kiz_heuristic_match import fetch_all_mpfit_orders, match_candidates
from api.sync_config import get_sku_aliases

CIM_JOIN_SEPARATOR = ", "


async def run_kiz_sync(dry_run: bool, debug: bool = False, probe_last_id: int = None, recovery_scan_from: int = None, recovery_scan_pages: int = 20):
  started_at = time.monotonic()
  async with httpx.AsyncClient(timeout=30) as client:
    candidates = await fetch_orders_missing_kiz(client, persist=not dry_run)
    if not candidates:
      result = {
        "dry_run": dry_run,
        "candidates_checked": 0,
        "matched": 0,
        "written": 0,
        "errors": [],
        "duration_ms": int((time.monotonic() - started_at) * 1000),
      }
      if debug:
        result["debug"] = {"candidates": [], "cim_keys_count": 0}
      return result

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

    result = {
      "dry_run": dry_run,
      "candidates_checked": len(candidates),
      "matched": len(matched),
      "written": written,
      "errors": errors,
      "duration_ms": int((time.monotonic() - started_at) * 1000),
    }
    if debug:
      cim_cursor_used = await _load_cim_cursor(client)
      probe_id = probe_last_id if probe_last_id is not None else cim_cursor_used
      raw_probe = await _post_with_retry(client, mpfit_base_url + "cim-codes", {"limit": 5, "last_id": probe_id})
      result["debug"] = {
        "candidates": [
          {"id": c["id"], "mpfit_id": c.get("mpfit_id")} for c in candidates
        ],
        "cim_keys_count": len(cim_by_mpfit_order),
        "matched_order_ids": [m["order_id"] for m in matched],
        "cim_cursor_used": cim_cursor_used,
        "cim_probe_last_id": probe_id,
        "cim_raw_probe": raw_probe["result"],
      }
      if recovery_scan_from is not None:
        wanted = {str(c["mpfit_id"]) for c in candidates if c.get("mpfit_id")}
        found = {}
        cursor = recovery_scan_from
        pages = 0
        while pages < recovery_scan_pages:
          data = await _post_with_retry(client, mpfit_base_url + "cim-codes", {"limit": 200, "last_id": cursor})
          page = data["result"]["data"]
          for item in page:
            order_id = str(item.get("order_id"))
            if order_id in wanted:
              found.setdefault(order_id, []).append(trim_cim(item.get("cim")))
          pages += 1
          new_last_id = data["result"].get("last_id")
          reached_end = len(page) < 200 or new_last_id is None
          if new_last_id is not None:
            cursor = new_last_id
          if reached_end:
            break
        result["debug"]["recovery_scan"] = {
          "scanned_from": recovery_scan_from,
          "scanned_to": cursor,
          "pages_scanned": pages,
          "reached_live_cursor": cursor >= cim_cursor_used,
          "found": found,
        }
    return result
