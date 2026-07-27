import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.mpfit_cim_client import trim_cim, _recent_count, CIM_CODES_MAX_LIMIT


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


def test_recent_count_clamps_to_api_max(monkeypatch):
  # /v1/cim-codes returns 422 above this limit (confirmed empirically
  # 2026-07-27) -- a misconfigured env value must degrade to the safe max
  # instead of taking every sync-kiz run down with an unhandled 500.
  monkeypatch.setenv("SYNC_KIZ_RECENT_COUNT", "1000")
  assert _recent_count() == CIM_CODES_MAX_LIMIT


def test_recent_count_passes_through_values_within_limit(monkeypatch):
  monkeypatch.setenv("SYNC_KIZ_RECENT_COUNT", "50")
  assert _recent_count() == 50
