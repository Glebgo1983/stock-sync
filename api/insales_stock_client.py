import asyncio
import os
import httpx
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env.local")

insales_shop_domain = os.getenv("INSALES_SHOP_DOMAIN")
insales_base_url = f"https://{insales_shop_domain}/admin/"
insales_username = os.getenv("INSALES_USERNAME") or ""
insales_password = os.getenv("INSALES_PASSWORD") or ""
insales_auth = httpx.BasicAuth(username=insales_username, password=insales_password)

INSALES_PAGE_SIZE = 100
INSALES_UPDATE_BATCH_SIZE = 100


async def _request_with_retry(client, method, url, retries=2, **kwargs):
  for attempt in range(retries + 1):
    response = await client.request(method, url, auth=insales_auth, **kwargs)
    if response.status_code == 429 and attempt < retries:
      await asyncio.sleep(2 * (attempt + 1))
      continue
    if response.status_code >= 500 and attempt < retries:
      await asyncio.sleep(2 * (attempt + 1))
      continue
    response.raise_for_status()
    return response
  response.raise_for_status()


async def fetch_variant_map(client):
  variant_map = {}
  from_id = 0
  while True:
    url = insales_base_url + "products.json"
    params = {
      "per_page": INSALES_PAGE_SIZE,
      "from_id": from_id,
      # No variant_fields filter: confirmed against a real response that
      # inSales silently drops `quantity` when variant_fields is passed,
      # even when quantity is explicitly listed — it's only present on the
      # full/default variant representation.
    }
    response = await _request_with_retry(client, "GET", url, params=params)
    products = response.json()
    if not products:
      break
    for product in products:
      for variant in product.get("variants", []):
        sku = (variant.get("sku") or "").strip()
        if not sku:
          continue
        variant_map.setdefault(sku, []).append({
          "variant_id": variant["id"],
          "product_id": variant.get("product_id"),
          "title": variant.get("title") or product.get("title"),
          "quantity": variant.get("quantity"),
          "sku": sku,
          "barcode": variant.get("barcode"),
        })
    if len(products) < INSALES_PAGE_SIZE:
      break
    from_id = max(product["id"] for product in products) + 1
  return variant_map


def _chunk(items, size):
  for i in range(0, len(items), size):
    yield items[i:i + size]


async def push_quantities(client, variant_id_to_qty):
  entries = [{"id": variant_id, "quantity": qty} for variant_id, qty in variant_id_to_qty.items()]
  url = insales_base_url + "products/variants_group_update.json"
  batches = []
  for batch in _chunk(entries, INSALES_UPDATE_BATCH_SIZE):
    variant_ids = [e["id"] for e in batch]
    try:
      response = await _request_with_retry(client, "PUT", url, json={"variants": batch})
      batches.append({"size": len(batch), "status": response.status_code, "variant_ids": variant_ids})
    except httpx.HTTPStatusError as e:
      batches.append({
        "size": len(batch),
        "status": e.response.status_code,
        "error": e.response.text,
        "variant_ids": variant_ids,
      })
  return batches
