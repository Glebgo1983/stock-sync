import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from api import mpfit_cim_client
from api.mpfit_cim_client import trim_cim, fetch_cim_map_since_cursor, CIM_SCAN_MAX_PAGES


def test_trim_cim_drops_last_seven_chars():
  assert trim_cim("0104656757971108215H2CZn") == "01046567579711082"


def test_trim_cim_handles_short_codes_without_erroring():
  assert trim_cim("123") == "123"
  assert trim_cim("1234567") == "1234567"


def test_trim_cim_handles_none_and_empty():
  assert trim_cim(None) == ""
  assert trim_cim("") == ""


def test_trim_cim_strips_whitespace_before_trimming():
  assert trim_cim("  0104656757971108215H2CZn  ") == "01046567579711082"


def _cim_page(items, last_id):
  return {"result": {"data": items, "last_id": last_id}}


async def _async_result(value):
  return value


@pytest.mark.asyncio
async def test_fetch_cim_map_since_cursor_resumes_from_saved_cursor(monkeypatch):
  # A saved cursor must be used as-is -- no tail search, no bootstrap.
  monkeypatch.setattr(mpfit_cim_client, "_load_cursor", lambda client: _async_result(1000))
  saved = {}
  monkeypatch.setattr(mpfit_cim_client, "_save_cursor", lambda value: saved.__setitem__("cursor", value))

  calls = []

  async def fake_post(client, url, body):
    calls.append(body["last_id"])
    # Single short page -- signals we've caught up to the real tail.
    return _cim_page(
      [{"cim": "0104656757971108215H2CZn", "order_id": 42, "arrival_id": None, "product_id": 1}],
      last_id=1050,
    )

  monkeypatch.setattr(mpfit_cim_client, "_post_with_retry", fake_post)
  order_map = await fetch_cim_map_since_cursor(None)
  assert calls == [1000]
  assert order_map == {"42": ["01046567579711082"]}
  assert saved["cursor"] == 1050


@pytest.mark.asyncio
async def test_fetch_cim_map_since_cursor_dry_run_does_not_move_cursor(monkeypatch):
  # Regression: a dry run must not consume codes from the feed -- caught in
  # production 2026-07-28 when a dry-run scan advanced the cursor past 6
  # real matches it never wrote, and the next real run no longer saw them.
  monkeypatch.setattr(mpfit_cim_client, "_load_cursor", lambda client: _async_result(1000))
  saved = {}
  monkeypatch.setattr(mpfit_cim_client, "_save_cursor", lambda value: saved.__setitem__("cursor", value))

  async def fake_post(client, url, body):
    return _cim_page(
      [{"cim": "0104656757971108215H2CZn", "order_id": 42, "arrival_id": None, "product_id": 1}],
      last_id=1050,
    )

  monkeypatch.setattr(mpfit_cim_client, "_post_with_retry", fake_post)
  order_map = await fetch_cim_map_since_cursor(None, persist=False)
  assert order_map == {"42": ["01046567579711082"]}
  assert saved == {}


@pytest.mark.asyncio
async def test_fetch_cim_map_since_cursor_paginates_until_short_page(monkeypatch):
  monkeypatch.setattr(mpfit_cim_client, "_load_cursor", lambda client: _async_result(0))
  saved = {}
  monkeypatch.setattr(mpfit_cim_client, "_save_cursor", lambda value: saved.__setitem__("cursor", value))

  pages = [
    _cim_page([{"cim": "A" * 20, "order_id": 1, "arrival_id": None, "product_id": 1}] * 200, last_id=200),
    _cim_page([{"cim": "B" * 20, "order_id": 2, "arrival_id": None, "product_id": 1}], last_id=201),
  ]

  async def fake_post(client, url, body):
    return pages.pop(0)

  monkeypatch.setattr(mpfit_cim_client, "_post_with_retry", fake_post)
  order_map = await fetch_cim_map_since_cursor(None)
  assert set(order_map.keys()) == {"1", "2"}
  assert saved["cursor"] == 201


@pytest.mark.asyncio
async def test_fetch_cim_map_since_cursor_stops_at_max_pages_and_saves_progress(monkeypatch):
  # A backlog bigger than CIM_SCAN_MAX_PAGES must not run unbounded -- it
  # should save however far it got, so the next run resumes from there
  # instead of scanning from the start again.
  monkeypatch.setattr(mpfit_cim_client, "_load_cursor", lambda client: _async_result(0))
  saved = {}
  monkeypatch.setattr(mpfit_cim_client, "_save_cursor", lambda value: saved.__setitem__("cursor", value))

  async def fake_post(client, url, body):
    # Always a full page -- an unbounded backlog that never runs out.
    last_id = body["last_id"] + 200
    return _cim_page([{"cim": "C" * 20, "order_id": 9, "arrival_id": None, "product_id": 1}] * 200, last_id)

  monkeypatch.setattr(mpfit_cim_client, "_post_with_retry", fake_post)
  await fetch_cim_map_since_cursor(None)
  assert saved["cursor"] == CIM_SCAN_MAX_PAGES * 200


@pytest.mark.asyncio
async def test_load_cursor_bootstraps_when_nothing_saved(monkeypatch):
  monkeypatch.setattr(mpfit_cim_client, "redis_url", "redis://127.0.0.1:1")
  monkeypatch.setattr(mpfit_cim_client, "_find_tail_last_id", lambda client: _async_result(1_000_000))
  cursor = await mpfit_cim_client._load_cursor(None)
  assert cursor == 1_000_000 - mpfit_cim_client.CIM_CURSOR_BOOTSTRAP_LOOKBACK


def test_save_cursor_degrades_silently_when_redis_unreachable(monkeypatch):
  monkeypatch.setattr(mpfit_cim_client, "redis_url", "redis://127.0.0.1:1")
  mpfit_cim_client._save_cursor(123)  # must not raise
