import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.kiz_heuristic_match import match_by_ask_number, match_candidates


def _mpfit_order(mpfit_id, number):
  return {"id": mpfit_id, "number": number}


def _candidate(candidate_id, number):
  return {"id": candidate_id, "number": number}


def test_match_by_ask_number_matches_yandex_ask_pattern():
  candidates = [_candidate("insales-1", 1088)]
  mpfit_orders = [_mpfit_order(19657458, "YANDEX-ASK1088")]
  assert match_by_ask_number(candidates, mpfit_orders) == {"insales-1": 19657458}


def test_match_by_ask_number_ignores_orders_without_ask_pattern():
  candidates = [_candidate("insales-1", 1088)]
  mpfit_orders = [_mpfit_order(19657458, "OZON-FBO-1088"), _mpfit_order(1, "1088")]
  assert match_by_ask_number(candidates, mpfit_orders) == {}


def test_match_by_ask_number_drops_ambiguous_duplicate_claims():
  # Same as match_candidates: never guess when more than one inSales
  # candidate would claim the same mpFit order.
  candidates = [_candidate("insales-1", 1088), _candidate("insales-2", 1088)]
  mpfit_orders = [_mpfit_order(19657458, "YANDEX-ASK1088")]
  assert match_by_ask_number(candidates, mpfit_orders) == {}


def test_match_by_ask_number_handles_leading_zeros_in_suffix():
  candidates = [_candidate("insales-1", 42)]
  mpfit_orders = [_mpfit_order(1, "YANDEX-ASK0042")]
  assert match_by_ask_number(candidates, mpfit_orders) == {"insales-1": 1}


def test_match_by_ask_number_skips_candidates_without_number():
  candidates = [_candidate("insales-1", None)]
  mpfit_orders = [_mpfit_order(1, "YANDEX-ASK1088")]
  assert match_by_ask_number(candidates, mpfit_orders) == {}


def test_match_candidates_handles_order_line_without_sku():
  # inSales order_line.sku can be None (e.g. a manually added line item
  # with no linked product/variant). Sorting the item signature must not
  # crash trying to compare None against a real article string -- confirmed
  # this happened live 2026-08-21 the first time a real order like this
  # reached this code path (500 Internal Server Error, "'<' not supported
  # between instances of 'NoneType' and 'str'").
  created_at = datetime(2026, 8, 1)
  candidates = [{
    "id": "insales-1",
    "order_lines": [{"sku": None, "quantity": 1}],
    "created_at": created_at,
  }]
  mpfit_orders = [{
    "id": 1,
    "created_at": "2026-08-01T00:00:00",
    "items": [{"product": {"article": "ABC"}, "quantity": 1}],
  }]
  assert match_candidates(candidates, mpfit_orders) == {}


def test_match_candidates_matches_normally_when_skus_present():
  created_at = datetime(2026, 8, 1)
  candidates = [{
    "id": "insales-1",
    "order_lines": [{"sku": "ABC", "quantity": 1}],
    "created_at": created_at,
  }]
  mpfit_orders = [{
    "id": 1,
    "created_at": "2026-08-01T00:00:00",
    "items": [{"product": {"article": "ABC"}, "quantity": 1}],
  }]
  assert match_candidates(candidates, mpfit_orders) == {"insales-1": 1}
