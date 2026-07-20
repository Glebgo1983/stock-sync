import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.mpfit_stock_client import compute_available_qty
from api.stock_sync import diff_variants


def test_compute_available_qty_simple_product():
  stocks = [
    {"warehouse_id": 1, "free": 5, "booked": 2, "can_collect": 0},
    {"warehouse_id": 2, "free": 3, "booked": 0, "can_collect": 0},
  ]
  assert compute_available_qty(stocks) == 8


def test_compute_available_qty_smart_product_uses_can_collect():
  stocks = [
    {"warehouse_id": 1, "free": 0, "booked": 0, "can_collect": 7},
  ]
  assert compute_available_qty(stocks) == 7


def test_compute_available_qty_handles_nulls_and_empty():
  stocks = [
    {"warehouse_id": 1, "free": None, "booked": None, "can_collect": None},
  ]
  assert compute_available_qty(stocks) == 0
  assert compute_available_qty([]) == 0
  assert compute_available_qty(None) == 0


def test_diff_variants_matches_by_sku():
  insales_map = {
    "SKU-1": [101],
    "SKU-2": [201, 202],
    "SKU-MISSING": [301],
  }
  mpfit_map = {
    "SKU-1": 10,
    "SKU-2": 0,
  }
  matched, unmatched = diff_variants(insales_map, mpfit_map)
  assert matched == {101: 10, 201: 0, 202: 0}
  assert unmatched == ["SKU-MISSING"]


def test_diff_variants_no_matches_leaves_matched_empty():
  insales_map = {"SKU-X": [1]}
  mpfit_map = {}
  matched, unmatched = diff_variants(insales_map, mpfit_map)
  assert matched == {}
  assert unmatched == ["SKU-X"]
