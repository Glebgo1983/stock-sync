import json
import os
from datetime import datetime, timezone
import redis
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env.local")

redis_url = os.getenv("REDIS_URL")

JOURNAL_KEY = "mpfit_sync:log"
JOURNAL_MAX_ENTRIES = 2000
SUMMARY_KEY = "mpfit_sync:last_run"
LAST_SUCCESS_KEY = "mpfit_sync:last_success_at"


def _redis():
  return redis.Redis.from_url(redis_url, decode_responses=True)


def _now_iso():
  return datetime.now(timezone.utc).isoformat()


def build_entry(variant_id, info, result, error=None):
  return {
    "timestamp": _now_iso(),
    "sku": info.get("sku"),
    "mpfit_id": info.get("mpfit_id"),
    "insales_variant_id": variant_id,
    "insales_product_id": info.get("product_id"),
    "title": info.get("title"),
    "previous_qty": info.get("previous_qty"),
    "new_qty": info.get("new_qty"),
    "result": result,
    "error": error,
  }


def append_entries(entries):
  if not entries:
    return
  client = _redis()
  pipe = client.pipeline()
  for entry in entries:
    pipe.lpush(JOURNAL_KEY, json.dumps(entry))
  pipe.ltrim(JOURNAL_KEY, 0, JOURNAL_MAX_ENTRIES - 1)
  pipe.execute()


def record_run(entries, summary):
  append_entries(entries)
  client = _redis()
  pipe = client.pipeline()
  pipe.set(SUMMARY_KEY, json.dumps(summary))
  pipe.set(LAST_SUCCESS_KEY, summary["finished_at"])
  pipe.execute()


def get_summary():
  client = _redis()
  raw = client.get(SUMMARY_KEY)
  return {
    "last_run": json.loads(raw) if raw else None,
    "last_success_at": client.get(LAST_SUCCESS_KEY),
  }


def get_recent_entries(limit=100):
  client = _redis()
  raw_entries = client.lrange(JOURNAL_KEY, 0, limit - 1)
  return [json.loads(e) for e in raw_entries]
