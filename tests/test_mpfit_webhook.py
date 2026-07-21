import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api.mpfit_webhook import parse_stock_event


def test_parse_stock_event_reads_top_level_shape():
  body = {"id": 42, "article": "SKU-1", "stocks": [{"free": 5, "can_collect": 0}]}
  event = parse_stock_event(body)
  assert event == {"mpfit_id": "42", "article": "SKU-1", "qty": 5, "has_stock_data": True}


def test_parse_stock_event_unwraps_data_envelope():
  body = {"event": "stock.updated", "data": {"id": 7, "article": "SKU-2", "stocks": [{"free": 3}]}}
  event = parse_stock_event(body)
  assert event["mpfit_id"] == "7"
  assert event["qty"] == 3
  assert event["has_stock_data"] is True


def test_parse_stock_event_unwraps_payload_envelope():
  body = {"type": "product.updated", "payload": {"id": 9, "article": "SKU-3", "stocks": []}}
  event = parse_stock_event(body)
  assert event["mpfit_id"] == "9"
  assert event["qty"] == 0
  assert event["has_stock_data"] is True


def test_parse_stock_event_missing_stocks_key_does_not_imply_zero():
  # An event type that doesn't carry a stock breakdown (e.g. an order status
  # change) must not be treated as "qty is 0" — that would zero out a real
  # stock level based on absent data.
  body = {"id": 1, "article": "SKU-4"}
  event = parse_stock_event(body)
  assert event["has_stock_data"] is False
  assert event["qty"] is None


def test_parse_stock_event_no_id_anywhere():
  body = {"article": "SKU-5", "stocks": [{"free": 1}]}
  event = parse_stock_event(body)
  assert event["mpfit_id"] is None
  assert event["has_stock_data"] is True


def test_parse_stock_event_smart_product_uses_can_collect():
  body = {"id": 3, "article": "SKU-6", "stocks": [{"free": 0, "can_collect": 4}]}
  event = parse_stock_event(body)
  assert event["qty"] == 4
