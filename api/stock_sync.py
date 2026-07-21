import time
from datetime import datetime, timezone
import httpx
from api.mpfit_stock_client import fetch_stock_map
from api.insales_stock_client import fetch_variant_map, push_quantities
from api.product_map import load_product_map, save_links
from api.sync_config import get_excluded_mpfit_ids, get_excluded_skus
from api.sync_journal import build_entry, record_run
from api.sync_lock import sync_lock

UNMATCHED_SKU_PREVIEW_LIMIT = 50
EXCLUDED_SKU_PREVIEW_LIMIT = 50


def diff_variants(insales_map, mpfit_stock, product_map, excluded_skus=frozenset(), excluded_mpfit_ids=frozenset()):
  """Match mpFit stock to inSales variants.

  Pass 1 trusts persisted ID-based links (product_map) — the primary
  matching mode required by spec, robust to article/sku being renamed on
  either side after the link was created.

  Pass 2 falls back to article==sku matching for anything pass 1 didn't
  resolve (new products, or a persisted link whose inSales variant no longer
  exists). Matches found this way are returned as `new_links` for the caller
  to persist, so future runs resolve them in pass 1 instead.

  A product excluded by sku or by mpFit id (`excluded_skus` /
  `excluded_mpfit_ids`, sourced from env config) is skipped entirely in both
  passes — not pushed, not linked, not counted as unmatched.
  """
  by_article = mpfit_stock["by_article"]
  by_id = mpfit_stock["by_id"]
  insales_by_variant_id = {
    variant["variant_id"]: variant
    for variants in insales_map.values()
    for variant in variants
  }

  matched = {}
  excluded = set()
  for mpfit_id, link in product_map.items():
    variant = insales_by_variant_id.get(link.get("insales_variant_id"))
    sku = variant.get("sku") if variant else link.get("sku")
    if mpfit_id in excluded_mpfit_ids or sku in excluded_skus:
      if sku:
        excluded.add(sku)
      continue
    qty = by_id.get(mpfit_id)
    if variant is None or qty is None:
      continue
    matched[variant["variant_id"]] = {
      "sku": sku,
      "product_id": variant.get("product_id"),
      "title": variant.get("title"),
      "previous_qty": variant.get("quantity"),
      "new_qty": qty,
      "mpfit_id": mpfit_id,
    }
  id_matched_count = len(matched)

  new_links = {}
  unmatched_skus = []
  for sku, variants in insales_map.items():
    if sku in excluded_skus or sku in excluded:
      excluded.add(sku)
      continue
    entry = by_article.get(sku)
    if entry is None:
      if not any(variant["variant_id"] in matched for variant in variants):
        unmatched_skus.append(sku)
      continue
    mpfit_id = str(entry["mpfit_id"]) if entry.get("mpfit_id") is not None else None
    if mpfit_id and mpfit_id in excluded_mpfit_ids:
      excluded.add(sku)
      continue
    for variant in variants:
      variant_id = variant["variant_id"]
      if variant_id in matched:
        continue
      matched[variant_id] = {
        "sku": sku,
        "product_id": variant.get("product_id"),
        "title": variant.get("title"),
        "previous_qty": variant.get("quantity"),
        "new_qty": entry["qty"],
        "mpfit_id": mpfit_id,
      }
      if mpfit_id:
        new_links[mpfit_id] = {
          "insales_variant_id": variant_id,
          "insales_product_id": variant.get("product_id"),
          "sku": sku,
          "barcode": variant.get("barcode"),
        }

  stats = {
    "id_matched": id_matched_count,
    "article_matched": len(matched) - id_matched_count,
    "excluded": len(excluded),
  }
  return matched, unmatched_skus, sorted(excluded), new_links, stats


def _build_journal_entries(matched, batches):
  entries = []
  for batch in batches:
    result = "error" if "error" in batch else "ok"
    error = batch.get("error")
    for variant_id in batch["variant_ids"]:
      entries.append(build_entry(variant_id, matched[variant_id], result, error))
  return entries


async def run_stock_sync(dry_run: bool):
  with sync_lock() as lock_state:
    if lock_state == "busy":
      return {
        "skipped": True,
        "reason": "sync_already_running",
        "dry_run": dry_run,
      }
    return await _run_stock_sync_locked(dry_run)


async def _run_stock_sync_locked(dry_run: bool):
  started_at_dt = datetime.now(timezone.utc)
  started_at = time.monotonic()
  async with httpx.AsyncClient(timeout=30) as client:
    mpfit_stock = await fetch_stock_map(client)
    insales_map = await fetch_variant_map(client)
    try:
      product_map = load_product_map()
    except Exception as e:
      # No persisted links yet, or Redis unavailable — fall back to
      # article-only matching for this run rather than failing the sync.
      print(f"product map load failed, falling back to article-only matching: {e}")
      product_map = {}

    matched, unmatched_skus, excluded_skus, new_links, match_stats = diff_variants(
      insales_map, mpfit_stock, product_map,
      excluded_skus=get_excluded_skus(),
      excluded_mpfit_ids=get_excluded_mpfit_ids(),
    )

    batches = []
    entries = []
    if not dry_run and matched:
      quantities = {variant_id: info["new_qty"] for variant_id, info in matched.items()}
      batches = await push_quantities(client, quantities)
      entries = _build_journal_entries(matched, batches)

  duration_ms = int((time.monotonic() - started_at) * 1000)
  summary = {
    "started_at": started_at_dt.isoformat(),
    "finished_at": datetime.now(timezone.utc).isoformat(),
    "duration_ms": duration_ms,
    "dry_run": dry_run,
    "mpfit_articles": len(mpfit_stock["by_article"]),
    "mpfit_products_with_id": len(mpfit_stock["by_id"]),
    "insales_skus": len(insales_map),
    "matched_variants": len(matched),
    "id_matched_variants": match_stats["id_matched"],
    "article_matched_variants": match_stats["article_matched"],
    "unmatched_count": len(unmatched_skus),
    "excluded_count": match_stats["excluded"],
    "success_count": sum(1 for e in entries if e["result"] == "ok"),
    "error_count": sum(1 for e in entries if e["result"] == "error"),
  }

  if not dry_run:
    if entries:
      try:
        record_run(entries, summary)
      except Exception as e:
        # Journal write failure must not fail the sync itself — stock was
        # already pushed to inSales by this point.
        print(f"sync journal write failed: {e}")
    if new_links:
      try:
        save_links(new_links)
      except Exception as e:
        print(f"product map save failed: {e}")

  return {
    **summary,
    "unmatched_skus": sorted(unmatched_skus)[:UNMATCHED_SKU_PREVIEW_LIMIT],
    "excluded_skus": excluded_skus[:EXCLUDED_SKU_PREVIEW_LIMIT],
    "batches": batches,
  }
