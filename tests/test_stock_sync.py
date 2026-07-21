import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api import sync_lock as sync_lock_module
from api.mpfit_stock_client import compute_available_qty
from api.stock_sync import diff_variants, _build_journal_entries
from api.sync_config import get_excluded_mpfit_ids, get_excluded_skus
from api.sync_journal import build_entry


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


def test_compute_available_qty_prefers_free_when_both_populated():
  # Real production incident (2026-07-21): mpFit returned free=203,
  # can_collect=9819 for a plain (non-kit) product. max() picked 9819 and
  # that got pushed to inSales as the sellable quantity, which was wrong —
  # free is the trustworthy number whenever it's actually populated.
  stocks = [
    {"warehouse_id": 97, "free": 203, "booked": 2, "can_collect": 9819},
  ]
  assert compute_available_qty(stocks) == 203


def _variant(variant_id, sku, product_id=1, title="T", quantity=0, barcode=None):
  return {
    "variant_id": variant_id, "product_id": product_id, "title": title,
    "quantity": quantity, "sku": sku, "barcode": barcode,
  }


def test_diff_variants_first_seen_product_matches_by_article_and_persists_link():
  insales_map = {
    "SKU-1": [_variant(101, "SKU-1", quantity=3, barcode="111")],
    "SKU-2": [
      _variant(201, "SKU-2", product_id=2, quantity=1),
      _variant(202, "SKU-2", product_id=2, quantity=5),
    ],
    "SKU-MISSING": [_variant(301, "SKU-MISSING", product_id=3)],
  }
  mpfit_stock = {
    "by_article": {
      "SKU-1": {"qty": 10, "mpfit_id": "m1"},
      "SKU-2": {"qty": 0, "mpfit_id": "m2"},
    },
    "by_id": {"m1": 10, "m2": 0},
  }
  matched, unmatched, excluded, new_links, stats = diff_variants(insales_map, mpfit_stock, {})
  assert matched[101] == {
    "sku": "SKU-1", "product_id": 1, "title": "T",
    "previous_qty": 3, "new_qty": 10, "mpfit_id": "m1",
  }
  assert matched[201]["new_qty"] == 0
  assert matched[202]["new_qty"] == 0
  assert unmatched == ["SKU-MISSING"]
  assert excluded == []
  assert stats == {"id_matched": 0, "article_matched": 3, "excluded": 0}
  assert new_links["m1"] == {
    "insales_variant_id": 101, "insales_product_id": 1, "sku": "SKU-1", "barcode": "111",
  }


def test_diff_variants_prefers_persisted_id_link_even_if_article_changed():
  # mpFit no longer reports the article this link was created with, and
  # inSales' sku has since changed too — the persisted ID link must still
  # resolve the match.
  insales_map = {
    "NEW-SKU": [_variant(101, "NEW-SKU", quantity=3, barcode="111")],
  }
  mpfit_stock = {"by_article": {}, "by_id": {"m1": 10}}
  product_map = {
    "m1": {"insales_variant_id": 101, "insales_product_id": 1, "sku": "OLD-SKU", "barcode": "111"},
  }
  matched, unmatched, excluded, new_links, stats = diff_variants(insales_map, mpfit_stock, product_map)
  assert matched[101]["new_qty"] == 10
  assert matched[101]["mpfit_id"] == "m1"
  assert unmatched == []
  assert excluded == []
  assert stats == {"id_matched": 1, "article_matched": 0, "excluded": 0}
  assert new_links == {}


def test_diff_variants_stale_link_falls_back_to_article_and_relinks():
  # Persisted link points at a variant id that no longer exists in inSales
  # (deleted/recreated product) — should fall back to article matching and
  # produce a fresh link.
  insales_map = {"SKU-1": [_variant(202, "SKU-1", quantity=1)]}
  mpfit_stock = {"by_article": {"SKU-1": {"qty": 6, "mpfit_id": "m1"}}, "by_id": {"m1": 6}}
  product_map = {
    "m1": {"insales_variant_id": 999, "insales_product_id": 1, "sku": "SKU-1", "barcode": None},
  }
  matched, unmatched, excluded, new_links, stats = diff_variants(insales_map, mpfit_stock, product_map)
  assert matched[202]["new_qty"] == 6
  assert stats == {"id_matched": 0, "article_matched": 1, "excluded": 0}
  assert new_links["m1"]["insales_variant_id"] == 202


def test_diff_variants_handles_article_with_slash_and_spaces():
  sku = "AB/12-34 X"
  insales_map = {sku: [_variant(5, sku, product_id=2, quantity=0)]}
  mpfit_stock = {"by_article": {sku: {"qty": 4, "mpfit_id": "m9"}}, "by_id": {"m9": 4}}
  matched, unmatched, excluded, new_links, stats = diff_variants(insales_map, mpfit_stock, {})
  assert matched[5]["new_qty"] == 4
  assert unmatched == []


def test_diff_variants_no_matches_leaves_matched_empty():
  insales_map = {"SKU-X": [_variant(1, "SKU-X", product_id=9, quantity=2)]}
  mpfit_stock = {"by_article": {}, "by_id": {}}
  matched, unmatched, excluded, new_links, stats = diff_variants(insales_map, mpfit_stock, {})
  assert matched == {}
  assert unmatched == ["SKU-X"]
  assert excluded == []
  assert new_links == {}


def test_diff_variants_excludes_by_sku_even_with_persisted_id_link():
  insales_map = {"SKU-1": [_variant(101, "SKU-1", quantity=3)]}
  mpfit_stock = {"by_article": {"SKU-1": {"qty": 10, "mpfit_id": "m1"}}, "by_id": {"m1": 10}}
  product_map = {
    "m1": {"insales_variant_id": 101, "insales_product_id": 1, "sku": "SKU-1", "barcode": None},
  }
  matched, unmatched, excluded, new_links, stats = diff_variants(
    insales_map, mpfit_stock, product_map, excluded_skus={"SKU-1"},
  )
  assert matched == {}
  assert unmatched == []
  assert excluded == ["SKU-1"]
  assert new_links == {}
  assert stats == {"id_matched": 0, "article_matched": 0, "excluded": 1}


def test_diff_variants_excludes_by_mpfit_id_on_first_seen_article_match():
  insales_map = {"SKU-1": [_variant(101, "SKU-1", quantity=3)]}
  mpfit_stock = {"by_article": {"SKU-1": {"qty": 10, "mpfit_id": "m1"}}, "by_id": {"m1": 10}}
  matched, unmatched, excluded, new_links, stats = diff_variants(
    insales_map, mpfit_stock, {}, excluded_mpfit_ids={"m1"},
  )
  assert matched == {}
  assert unmatched == []
  assert excluded == ["SKU-1"]
  assert new_links == {}


def test_build_journal_entries_marks_ok_and_error_batches():
  matched = {
    101: {"sku": "SKU-1", "product_id": 1, "title": "T1", "previous_qty": 3, "new_qty": 10, "mpfit_id": "m1"},
    202: {"sku": "SKU-2", "product_id": 2, "title": "T2", "previous_qty": 1, "new_qty": 0, "mpfit_id": "m2"},
  }
  batches = [
    {"size": 1, "status": 200, "variant_ids": [101]},
    {"size": 1, "status": 500, "error": "boom", "variant_ids": [202]},
  ]
  entries = _build_journal_entries(matched, batches)
  by_id = {e["insales_variant_id"]: e for e in entries}
  assert by_id[101]["result"] == "ok"
  assert by_id[101]["error"] is None
  assert by_id[101]["previous_qty"] == 3
  assert by_id[101]["new_qty"] == 10
  assert by_id[202]["result"] == "error"
  assert by_id[202]["error"] == "boom"


def test_build_entry_shape():
  info = {"sku": "SKU-1", "mpfit_id": "m1", "product_id": 1, "title": "T1", "previous_qty": 3, "new_qty": 10}
  entry = build_entry(101, info, "ok")
  assert entry["insales_variant_id"] == 101
  assert entry["sku"] == "SKU-1"
  assert entry["mpfit_id"] == "m1"
  assert entry["result"] == "ok"
  assert entry["error"] is None
  assert "timestamp" in entry


def test_get_excluded_skus_parses_comma_separated_env(monkeypatch):
  monkeypatch.setenv("SYNC_EXCLUDED_SKUS", " SKU-1, SKU-2 ,,SKU-3")
  assert get_excluded_skus() == {"SKU-1", "SKU-2", "SKU-3"}


def test_get_excluded_skus_empty_when_unset(monkeypatch):
  monkeypatch.delenv("SYNC_EXCLUDED_SKUS", raising=False)
  assert get_excluded_skus() == set()


def test_get_excluded_mpfit_ids_parses_comma_separated_env(monkeypatch):
  monkeypatch.setenv("SYNC_EXCLUDED_MPFIT_IDS", "m1,m2")
  assert get_excluded_mpfit_ids() == {"m1", "m2"}


def test_lock_ttl_defaults_when_unset(monkeypatch):
  monkeypatch.delenv("SYNC_LOCK_TTL_SECONDS", raising=False)
  assert sync_lock_module._lock_ttl() == sync_lock_module.DEFAULT_LOCK_TTL_SECONDS


def test_lock_ttl_reads_env_override(monkeypatch):
  monkeypatch.setenv("SYNC_LOCK_TTL_SECONDS", "120")
  assert sync_lock_module._lock_ttl() == 120


def test_lock_ttl_falls_back_on_invalid_value(monkeypatch):
  monkeypatch.setenv("SYNC_LOCK_TTL_SECONDS", "not-a-number")
  assert sync_lock_module._lock_ttl() == sync_lock_module.DEFAULT_LOCK_TTL_SECONDS


def test_sync_lock_yields_unavailable_when_redis_unreachable(monkeypatch):
  monkeypatch.setattr(sync_lock_module, "redis_url", "redis://127.0.0.1:1")
  with sync_lock_module.sync_lock() as state:
    assert state == "unavailable"


def test_sync_lock_yields_unavailable_when_redis_url_unset(monkeypatch):
  # from_url(None) raises AttributeError, not redis.exceptions.RedisError —
  # this must still degrade to "unavailable" rather than propagate.
  monkeypatch.setattr(sync_lock_module, "redis_url", None)
  with sync_lock_module.sync_lock() as state:
    assert state == "unavailable"
