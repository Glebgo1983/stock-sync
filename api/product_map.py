import json
import os
import redis
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env.local")

redis_url = os.getenv("REDIS_URL")

PRODUCT_MAP_KEY = "mpfit_sync:product_map"


def _redis():
  return redis.Redis.from_url(redis_url, decode_responses=True)


def load_product_map():
  client = _redis()
  raw = client.hgetall(PRODUCT_MAP_KEY)
  return {mpfit_id: json.loads(value) for mpfit_id, value in raw.items()}


def save_links(new_links):
  if not new_links:
    return
  client = _redis()
  pipe = client.pipeline()
  for mpfit_id, info in new_links.items():
    pipe.hset(PRODUCT_MAP_KEY, mpfit_id, json.dumps(info))
  pipe.execute()
