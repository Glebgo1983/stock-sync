from urllib.parse import unquote
from datetime import datetime, timedelta
import httpx
import re
import math
import numbers
import time
import os
import redis
import secrets
import string
import random 
import hashlib
from dotenv import load_dotenv
from api.cdek_functions import get_cdek_tracking_number
from api.pochta_functions import get_pochta_tracking_number

mpfit = "https://app.mpfit.ru/api/v1/"
insales = "https://myshop-dbn10.myinsales.ru/admin/orders/"

load_dotenv(dotenv_path=".env.local")
redis_url = os.getenv("REDIS_URL")
mpfit_token = os.getenv("MPFIT_TOKEN")
mpfit_headers = {"Authorization": f"Bearer {mpfit_token}", "Content-Type": "application/json"}
insales_username = os.getenv("INSALES_USERNAME")
insales_password = os.getenv("INSALES_PASSWORD")
insales_auth = httpx.BasicAuth(username=insales_username, password=insales_password)

statuses = {
  "PRODUCTS_RESERVED": "v-obrabotke",
  "EQUIPMENT": "v-obrabotke",
  "READY_TO_SHIP": "soglasovan",
  "SHIPPED": "dispatched",
  "DELIVERY": "dispatched",
  "COMPLETE": "dostavlen",
  "REJECT": "vozvrat",
  "CANCEL": "otmenen"
}
  
async def new_order_handler(data):
  r = redis.Redis.from_url(redis_url, decode_responses=True)
  async with httpx.AsyncClient() as client:
    products = await get_products(client, 0)
    items = get_order_items(data["order_lines"], products)
    note = f"""
       Полное имя: {check(data["shipping_address"]["full_name"])};
       Телефон: {check(data["shipping_address"]["phone"])};
       Адрес: {check(data["shipping_address"]["full_delivery_address"])};
       Подъезд: {check(data["shipping_address"]["entrance"])};
       Этаж: {check(data["shipping_address"]["floor"])};
       Домофон: {check(data["shipping_address"]["doorphone"])};
       Способ доставки: {check (data["delivery_title"])}
       Комментарий к доставке: {check(data["shipping_address"]["comment"])};
       
       Способ оплаты: {check(data["payment_title"])};
       Комментарий покупателя: {check(data["comment"])};
       Комментарий продавца: {check(data["manager_comment"])}
    """
    order_id = await create_order(client, items, note, data["id"])
    r.set(f"insales-mpfit:{order_id}", "NEW")

async def get_products(client, last_id):
  url = mpfit + "products/list"
  body = {
  "limit": 200,
  "last_id": last_id
  }
  result = await client.post(url=url, headers=mpfit_headers, json=body)
  result = result.json()
  result = result["result"]
  #products = result["data"]
  if len(result["data"]) == 200:
    products = await get_products(client, result["last_id"])
    products.extend(result["data"])
    return products
  else:
    return result["data"]

def get_order_items(items, products):
  output = []
  for item in items:
    print(item)
    product = next((product for product in products if product['article'] == item["sku"]), None)
    if product is not None:
      output.append({"product_id": product["id"], "quantity": item["quantity"]})
      print(product)
  return output

async def create_order(client, items, note, id):
  url = mpfit + "orders/create"
  body = {"items": items, "shipment_date": "2024-04-20T09:20:06Z", "number": id, "note": note}
  print(body)
  result = await client.post(url=url, headers=mpfit_headers, json=body)
  result = result.json()
  return result["order"]["id"]

async def get_orders(client, id_list, last_id):
  url = mpfit + "orders/list"
  body = {"limit": 200, "last_id": 0, "filter": {"ids": id_list[:200]}}
  result = await client.post(url=url, headers=mpfit_headers, json=body)
  result = result.json()
  result = result["result"]
  id_list = id_list[200:]
  if id_list:
    orders = await get_orders(client, id_list, result["last_id"])
    orders.extend(result["data"])
    return orders
  else:
    return result["data"]

async def update_order(client, order, status):
  url = f"{insales}{order}.json"
  status = statuses[status]
  body = {"order": {"custom_status_permalink": status}}
  result = await client.put(url=url, auth=insales_auth, json=body)
  result = result.json()
  return 

async def update_orders():
  r = redis.Redis.from_url(redis_url, decode_responses=True)
  keys = r.keys("insales-mpfit:*")
  values = r.mget(keys)
  cached_orders = {key.replace("insales-mpfit:",""): value for key, value in zip(keys, values)}
  print(cached_orders)
  async with httpx.AsyncClient() as client:
    orders = await get_orders(client, list(cached_orders.keys()), 0)
    for order in orders:
      if order["status"] != cached_orders[order["id"]]:
        await update_order(client, order["number"], order["status"])
        r.set(f"insales-mpfit:{order["id"]}", order["status"])
      
async def check_order_status(client, id):
  ...

async def get_tracking_number(client, redis_client, id):
  try:
    tracking_number = await get_cdek_tracking_number(client, redis_client, id)
    if tracking_number is None:
      tracking_number = await get_pochta_tracking_number(client, redis_client, id)
    return tracking_number
  except Exception as e:
    print(e)
    return None

def check(value):
  return "" if value is None else value
