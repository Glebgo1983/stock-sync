import time
import httpx
from api.mpfit_stock_client import fetch_stock_map
from api.insales_stock_client import fetch_variant_map, push_quantities

UNMATCHED_SKU_PREVIEW_LIMIT = 50


def diff_variants(insales_map, mpfit_map):
  matched = {}
  unmatched_skus = []
  for sku, variant_ids in insales_map.items():
    if sku in mpfit_map:
      qty = mpfit_map[sku]
      for variant_id in variant_ids:
        matched[variant_id] = qty
    else:
      unmatched_skus.append(sku)
  return matched, unmatched_skus


async def run_stock_sync(dry_run: bool):
  started_at = time.monotonic()
  async with httpx.AsyncClient(timeout=30) as client:
    mpfit_map = await fetch_stock_map(client)
    insales_map = await fetch_variant_map(client)
    matched, unmatched_skus = diff_variants(insales_map, mpfit_map)

    batches = []
    if not dry_run and matched:
      batches = await push_quantities(client, matched)

  duration_ms = int((time.monotonic() - started_at) * 1000)
  return {
    "dry_run": dry_run,
    "mpfit_articles": len(mpfit_map),
    "insales_skus": len(insales_map),
    "matched_variants": len(matched),
    "unmatched_count": len(unmatched_skus),
    "unmatched_skus": sorted(unmatched_skus)[:UNMATCHED_SKU_PREVIEW_LIMIT],
    "batches": batches,
    "duration_ms": duration_ms,
  }
