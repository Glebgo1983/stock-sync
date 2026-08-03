import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.kiz_heuristic_match import match_by_ask_number


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
