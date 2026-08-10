import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from api import sync_journal
from api.sync_journal import (
  build_entry,
  record_run,
  append_entries,
  get_recent_entries,
  get_summary,
  RUN_LOG_KEY,
  RUN_LOG_MAX_RUNS,
)


class FakeRedis:
  """Minimal in-memory stand-in covering just the commands sync_journal uses.

  Every command that would count against the Upstash quota is tallied in
  `.calls` so a test can assert a run costs a fixed handful of commands
  regardless of how many entries it carries (a pipeline is counted as its
  member commands, matching how Upstash bills them).
  """

  def __init__(self):
    self.lists = {}
    self.kv = {}
    self.calls = []

  # --- direct commands ---
  def lpush(self, key, value):
    self.calls.append("lpush")
    self.lists.setdefault(key, []).insert(0, value)

  def ltrim(self, key, start, end):
    self.calls.append("ltrim")
    lst = self.lists.get(key, [])
    self.lists[key] = lst[start:end + 1] if end != -1 else lst[start:]

  def mset(self, mapping):
    self.calls.append("mset")
    self.kv.update(mapping)

  def set(self, key, value):
    self.calls.append("set")
    self.kv[key] = value

  def get(self, key):
    self.calls.append("get")
    return self.kv.get(key)

  def lrange(self, key, start, end):
    self.calls.append("lrange")
    lst = self.lists.get(key, [])
    return lst[start:end + 1] if end != -1 else lst[start:]

  # --- pipeline: buffers commands, replays them on execute ---
  def pipeline(self):
    return FakePipeline(self)


class FakePipeline:
  def __init__(self, client):
    self._client = client
    self._ops = []

  def lpush(self, key, value):
    self._ops.append(("lpush", (key, value)))
    return self

  def ltrim(self, key, start, end):
    self._ops.append(("ltrim", (key, start, end)))
    return self

  def mset(self, mapping):
    self._ops.append(("mset", (mapping,)))
    return self

  def execute(self):
    for name, args in self._ops:
      getattr(self._client, name)(*args)


def _entries(n):
  return [build_entry(i, {"sku": f"SKU-{i}", "new_qty": i}, "ok") for i in range(n)]


def test_record_run_costs_a_fixed_few_commands_regardless_of_catalog_size(monkeypatch):
  fake = FakeRedis()
  monkeypatch.setattr(sync_journal, "_redis", lambda: fake)

  record_run(_entries(500), {"finished_at": "2026-08-10T00:00:00+00:00"})

  # The whole run is 3 write commands (LPUSH + LTRIM + MSET) -- NOT one per
  # variant. This is the fix that keeps us well under the Upstash free tier.
  assert fake.calls == ["lpush", "ltrim", "mset"]


def test_record_run_with_no_entries_still_records_summary_only(monkeypatch):
  fake = FakeRedis()
  monkeypatch.setattr(sync_journal, "_redis", lambda: fake)

  record_run([], {"finished_at": "2026-08-10T00:00:00+00:00"})

  assert fake.calls == ["mset"]
  assert fake.lists.get(RUN_LOG_KEY) in (None, [])


def test_get_recent_entries_flattens_runs_newest_first(monkeypatch):
  fake = FakeRedis()
  monkeypatch.setattr(sync_journal, "_redis", lambda: fake)

  record_run([build_entry(1, {"sku": "OLD"}, "ok")], {"finished_at": "t1"})
  record_run([build_entry(2, {"sku": "NEW-A"}, "ok"), build_entry(3, {"sku": "NEW-B"}, "ok")],
             {"finished_at": "t2"})

  flat = get_recent_entries(limit=10)
  assert [e["sku"] for e in flat] == ["NEW-A", "NEW-B", "OLD"]


def test_get_recent_entries_respects_limit_across_runs(monkeypatch):
  fake = FakeRedis()
  monkeypatch.setattr(sync_journal, "_redis", lambda: fake)

  record_run(_entries(80), {"finished_at": "t"})
  assert len(get_recent_entries(limit=50)) == 50


def test_summary_round_trips(monkeypatch):
  fake = FakeRedis()
  monkeypatch.setattr(sync_journal, "_redis", lambda: fake)

  record_run(_entries(3), {"finished_at": "2026-08-10T12:00:00+00:00", "ok": True})
  summary = get_summary()
  assert summary["last_run"]["ok"] is True
  assert summary["last_success_at"] == "2026-08-10T12:00:00+00:00"


def test_append_entries_records_one_run_blob(monkeypatch):
  fake = FakeRedis()
  monkeypatch.setattr(sync_journal, "_redis", lambda: fake)

  append_entries(_entries(4))
  assert fake.calls == ["lpush", "ltrim"]
  assert len(fake.lists[RUN_LOG_KEY]) == 1
  assert len(json.loads(fake.lists[RUN_LOG_KEY][0])["entries"]) == 4


def test_append_entries_noop_when_empty(monkeypatch):
  fake = FakeRedis()
  monkeypatch.setattr(sync_journal, "_redis", lambda: fake)

  append_entries([])
  assert fake.calls == []


def test_run_log_is_trimmed_to_max_runs(monkeypatch):
  fake = FakeRedis()
  monkeypatch.setattr(sync_journal, "_redis", lambda: fake)

  for i in range(RUN_LOG_MAX_RUNS + 5):
    record_run([build_entry(i, {"sku": f"S{i}"}, "ok")], {"finished_at": f"t{i}"})
  assert len(fake.lists[RUN_LOG_KEY]) == RUN_LOG_MAX_RUNS
