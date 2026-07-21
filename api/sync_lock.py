import os
from contextlib import contextmanager
import redis
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env.local")

redis_url = os.getenv("REDIS_URL")

LOCK_KEY = "mpfit_sync:lock"
DEFAULT_LOCK_TTL_SECONDS = 600


def _lock_ttl():
  try:
    return int(os.getenv("SYNC_LOCK_TTL_SECONDS", DEFAULT_LOCK_TTL_SECONDS))
  except ValueError:
    return DEFAULT_LOCK_TTL_SECONDS


@contextmanager
def sync_lock():
  """Distributed lock preventing overlapping stock-sync runs via Redis.

  Yields "acquired" (caller owns the lock, proceed), "busy" (another run
  already holds it — caller should skip), or "unavailable" (Redis
  unreachable, so the lock can't be enforced — caller proceeds unprotected
  rather than making the whole sync hard-depend on Redis being up, matching
  how product_map/sync_journal degrade elsewhere in this codebase).

  The lock auto-expires after SYNC_LOCK_TTL_SECONDS (default 600s) so a run
  that crashes without releasing it can't block sync forever.
  """
  lock = None
  acquired = False
  try:
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    lock = client.lock(LOCK_KEY, timeout=_lock_ttl())
    acquired = lock.acquire(blocking=False)
    state = "acquired" if acquired else "busy"
  except Exception as e:
    # Covers both a reachable-but-erroring Redis (redis.exceptions.RedisError)
    # and REDIS_URL being unset entirely (from_url(None) raises AttributeError,
    # not a RedisError) — either way, degrade instead of failing the sync.
    print(f"sync lock unavailable, proceeding without it: {e}")
    state = "unavailable"

  try:
    yield state
  finally:
    if acquired:
      try:
        lock.release()
      except redis.exceptions.LockError:
        # Already expired (TTL hit) and possibly claimed by another run —
        # nothing to release.
        pass
