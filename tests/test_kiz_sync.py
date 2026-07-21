import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.mpfit_cim_client import trim_cim


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
