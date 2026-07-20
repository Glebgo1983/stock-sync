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
  stock_map = {}
  last_id = 0
  while True:
    body = {"limit": MPFIT_PAGE_LIMIT, "last_id": last_id}
    data = await _post_with_retry(client, mpfit_base_url + "products/stocks", body)
    result = data["result"]
    items = result["data"]
    for item in items:
      article = (item.get("article") or "").strip()
      if not article:
        continue
      qty = compute_available_qty(item.get("stocks"))
      stock_map[article] = stock_map.get(article, 0) + qty
    if len(items) < MPFIT_PAGE_LIMIT or result.get("last_id") is None:
      break
    last_id = result["last_id"]
  return stock_map
