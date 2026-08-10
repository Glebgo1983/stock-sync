import json
import os
from datetime import datetime, timezone
import redis
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env.local")

redis_url = os.getenv("REDIS_URL")

# The journal is stored one record PER RUN (a whole batch of per-variant
# entries as a single JSON blob), NOT one Redis list element per variant.
#
# Why: a stock-sync run pushes the entire mapped catalog every time, so the
# old "one LPUSH per variant" cost ~N Redis commands per run (N = catalog
# size). With a ~10-minute cron that alone was ~90% of the Upstash free-tier
# command quota (measured 2026-08-10: catalog ~104 variants => ~449k of the
# 500k monthly commands). Collapsing a run into a single LPUSH'd blob makes
# journaling a fixed handful of commands per run regardless of catalog size.
#
# get_recent_entries() flattens run records back into the per-variant entry
# stream that /api/sync-log returns, so that endpoint's response shape is
# unchanged. (The old per-variant list under JOURNAL_KEY is simply left
# behind — it's a debug log, no migration needed; it ages out on its own.)
RUN_LOG_KEY = "mpfit_sync:runs"
RUN_LOG_MAX_RUNS = 500
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


def _push_run(pipe, entries, finished_at=None):
  """Queue a single run record (all its per-variant entries) plus the trim
  onto an existing pipeline -- exactly two commands regardless of len(entries).
  """
  record = {"finished_at": finished_at or _now_iso(), "entries": entries}
  pipe.lpush(RUN_LOG_KEY, json.dumps(record))
  pipe.ltrim(RUN_LOG_KEY, 0, RUN_LOG_MAX_RUNS - 1)


def append_entries(entries):
  """Record a batch of entries as one run record (used by the mpFit webhook
  path). record_run() is the main stock-sync entry point.
  """
  if not entries:
    return
  client = _redis()
  pipe = client.pipeline()
  _push_run(pipe, entries)
  pipe.execute()


def record_run(entries, summary):
  """Persist one stock-sync run in a single pipeline: the run's entries as one
  blob (skipped when there were none) plus the two summary keys via MSET.
  """
  client = _redis()
  pipe = client.pipeline()
  if entries:
    _push_run(pipe, entries, finished_at=summary.get("finished_at"))
  pipe.mset({SUMMARY_KEY: json.dumps(summary), LAST_SUCCESS_KEY: summary["finished_at"]})
  pipe.execute()


def get_summary():
  client = _redis()
  raw = client.get(SUMMARY_KEY)
  return {
    "last_run": json.loads(raw) if raw else None,
    "last_success_at": client.get(LAST_SUCCESS_KEY),
  }


def get_recent_entries(limit=100):
  """Flatten the most-recent run records back into the flat per-variant entry
  stream /api/sync-log expects. A single stock-sync run typically already
  holds far more than `limit` entries, so reading at most `limit` run records
  (one LRANGE) is always enough -- each run holds >= 1 entry.
  """
  client = _redis()
  raw_runs = client.lrange(RUN_LOG_KEY, 0, max(limit, 1) - 1)
  entries = []
  for raw in raw_runs:
    try:
      run = json.loads(raw)
    except (ValueError, TypeError):
      continue
    entries.extend(run.get("entries", []))
    if len(entries) >= limit:
      break
  return entries[:limit]
