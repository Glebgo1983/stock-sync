import asyncio
import os
import time
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env.local")

mpfit_base_url = "https://app.mpfit.ru/api/v1/"
mpfit_token = os.getenv("MPFIT_TOKEN")
mpfit_headers = {"Authorization": f"Bearer {mpfit_token}", "Content-Type": "application/json"}

MPFIT_PAGE_LIMIT = 200
MPFIT_RATE_LIMIT_PER_MINUTE = 60

_request_timestamps = []


async def _respect_rate_limit():
  now = time.monotonic()
  window_start = now - 60
  while _request_timestamps and _request_timestamps[0] < window_start:
    _request_timestamps.pop(0)
  if len(_request_timestamps) >= MPFIT_RATE_LIMIT_PER_MINUTE:
    sleep_for = _request_timestamps[0] + 60 - now
    if sleep_for > 0:
      await asyncio.sleep(sleep_for)
  _request_timestamps.append(time.monotonic())


async def _post_with_retry(client, url, body, retries=2):
  for attempt in range(retries + 1):
    await _respect_rate_limit()
    response = await client.post(url=url, headers=mpfit_headers, json=body)
    if response.status_code == 429 and attempt < retries:
      await asyncio.sleep(2 * (attempt + 1))
      continue
    response.raise_for_status()
    return response.json()
  response.raise_for_status()


def compute_available_qty(stocks):
  # `free` is unreserved stock for regular products; `can_collect` is what a
  # smart-product (kit) can be assembled into on demand. A product only ever
  # has one of the two meaningfully populated, so summing max() per warehouse
  # covers both cases without a separate call to /products/list for product_type.
  total = 0
  for row in stocks or []:
    free = row.get("free") or 0
    can_collect = row.get("can_collect") or 0
    total += max(free, can_collect)
  return total


async def fetch_stock_map(client):
  # `id` is assumed to be mpFit's internal product id, used as the durable
  # matching key (see api/product_map.py). Unverified against official docs —
  # the docs site is JS-rendered and didn't expose field names when checked.
  #
  # Returns both indexes: `by_article` (fallback/first-time matching, also
  # what /api/sync-stock summary counts) and `by_id` (primary ID-based
  # lookup — includes items without an article, since a persisted link
  # doesn't need one).
  by_article = {}
  by_id = {}
  last_id = 0
  while True:
    body = {"limit": MPFIT_PAGE_LIMIT, "last_id": last_id}
    data = await _post_with_retry(client, mpfit_base_url + "products/stocks", body)
    result = data["result"]
    items = result["data"]
    for item in items:
      article = (item.get("article") or "").strip()
      qty = compute_available_qty(item.get("stocks"))
      item_id = item.get("id")
      mpfit_id = str(item_id) if item_id is not None else None
      if mpfit_id is not None:
        by_id[mpfit_id] = by_id.get(mpfit_id, 0) + qty
      if article:
        entry = by_article.setdefault(article, {"qty": 0, "mpfit_id": mpfit_id})
        entry["qty"] += qty
    if len(items) < MPFIT_PAGE_LIMIT or result.get("last_id") is None:
      break
    last_id = result["last_id"]
  return {"by_article": by_article, "by_id": by_id}
