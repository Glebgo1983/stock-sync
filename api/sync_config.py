import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env.local")


def _parse_list(value):
  if not value:
    return set()
  return {item.strip() for item in value.split(",") if item.strip()}


def get_excluded_skus():
  return _parse_list(os.getenv("SYNC_EXCLUDED_SKUS"))


def get_excluded_mpfit_ids():
  return _parse_list(os.getenv("SYNC_EXCLUDED_MPFIT_IDS"))


def get_sku_aliases():
  """Manual overrides for inSales sku -> mpFit article, comma-separated
  `insales_sku:mpfit_article` pairs. For cases where mpFit's article carries
  a variant suffix (e.g. `82FACECREAM002/RICH`) that inSales' sku doesn't
  (`82FACECREAM002`) -- confirmed real examples in this shop, quantities
  matched exactly once aliased. Used only as a fallback when the direct
  sku==article match misses; doesn't affect ID-based matching once a link
  is persisted.
  """
  raw = os.getenv("SKU_ALIASES")
  if not raw:
    return {}
  aliases = {}
  for pair in raw.split(","):
    pair = pair.strip()
    if not pair or ":" not in pair:
      continue
    insales_sku, mpfit_article = pair.split(":", 1)
    insales_sku = insales_sku.strip()
    mpfit_article = mpfit_article.strip()
    if insales_sku and mpfit_article:
      aliases[insales_sku] = mpfit_article
  return aliases
