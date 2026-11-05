#!/usr/bin/env python3
"""x402_revenue_track 新鲜度断言 + 目录抖动提示的测试（unittest，无需 pytest 与网络）。

跑法：
  cd x402-tracker && python3 -m unittest discover -s tests -v
  # 或 python3 tests/test_freshness.py

覆盖需求单验收 1（构造假快照：4 天 → 警告；1 天 → 无警告；全无 measured → 不崩）与验收 2 的
数字口径（用 data/snapshots/2026-11-05.json 复算 → 8 天，且 6 行指标与现文件逐行一致）。
测试全部写临时目录，不碰 data/。
"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
DATA = PROJECT / "data"
sys.path.insert(0, str(PROJECT))
import x402_revenue_track as trt  # noqa: E402


def rec(name, vol, last_at, status="measured", buyers=3):
    return {
        "name": name, "slug": name.lower(), "min_price_usd": 0.01,
        "assessment": {"traction": {"status": status, "volume_usd_30d": vol,
                                    "tx_count_30d": 10, "unique_buyers_30d": buyers,
                                    "last_settlement_at": last_at,
                                    "top_buyer_share_30d": 0.5}},
    }


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class FreshnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, recs, policy="warn"):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = trt.run(recs, {"total": len(recs)}, self.out, stale_policy=policy)
        latest = Path(self.out, "latest.md").read_text(encoding="utf-8")
        series = [json.loads(x) for x in
                  Path(self.out, "series.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        return code, buf.getvalue(), latest, series

    def test_stale_four_days_warns(self):
        recs = [rec("A", 100.0, iso(self.now - timedelta(days=4)))]
        code, out, latest, series = self._run(recs)
        self.assertEqual(code, 0)                     # 默认 warn：照常 exit 0
        self.assertIn("STALE settlement_feed: 4 days", out)
        first = latest.splitlines()[0]
        self.assertTrue(first.startswith("> ⚠ 数据源结算层已停更 4 天"), first)
        self.assertIn("趋势不可读", first)
        self.assertEqual(series[-1]["settlement_stale_days"], 4)
        self.assertEqual(series[-1]["settlement_max_last_at"], iso(self.now - timedelta(days=4)))

    def test_fresh_one_day_no_warning(self):
        recs = [rec("A", 100.0, iso(self.now - timedelta(days=1)))]
        code, out, latest, series = self._run(recs)
        self.assertEqual(code, 0)
        self.assertNotIn("STALE", out)
        self.assertNotIn("停更", latest)
        self.assertTrue(latest.startswith("# x402 真实收入快照 · "))
        self.assertEqual(series[-1]["settlement_stale_days"], 1)

    def test_no_measured_listings_does_not_crash(self):
        recs = [rec("A", None, None, status="unmeasured"), rec("B", None, None, status="unmeasured")]
        code, out, latest, series = self._run(recs)
        self.assertEqual(code, 0)
        self.assertIsNone(series[-1]["settlement_stale_days"])
        self.assertIsNone(series[-1]["settlement_max_last_at"])
        self.assertNotIn("停更", latest)
        self.assertIn("有 traction 测量: 0", latest)

    def test_policy_fail_exits_3(self):
        recs = [rec("A", 100.0, iso(self.now - timedelta(days=9)))]
        code, out, _latest, _s = self._run(recs, policy="fail")
        self.assertEqual(code, 3)
        self.assertIn("STALE settlement_feed: 9 days", out)

    def test_policy_fail_fresh_still_zero(self):
        recs = [rec("A", 100.0, iso(self.now - timedelta(days=2)))]
        code, _out, _latest, _s = self._run(recs, policy="fail")
        self.assertEqual(code, 0)

    def test_series_delta_suffix_on_second_run(self):
        day1 = [rec("A", 100.0, iso(self.now - timedelta(days=1))),
                rec("B", 50.0, iso(self.now - timedelta(days=1)))]
        _c, _o, latest1, _s1 = self._run(day1)
        self.assertNotIn("较上次", latest1)           # 首次运行没有上一条 → 整句省略

        day2 = [rec("A", 100.0, iso(self.now - timedelta(days=2)))]
        _c, _o, latest2, series2 = self._run(day2)
        self.assertIn("total 较上次 -1，measured 较上次 -1", latest2)
        self.assertIn("目录换血", latest2)
        self.assertEqual(len(series2), 2)

    def test_series_keys_preserved_and_extended(self):
        """老键一个不许少、不许改名；只允许新增两个。"""
        legacy = {"captured_at_utc", "directory", "listed_total", "traction_measured",
                  "traction_unmeasured", "services_paid_30d", "pct_paid_30d",
                  "market_volume_usd_30d", "market_volume_usd_per_day", "market_tx_30d",
                  "avg_settlement_usd", "top1_share_pct", "top3_share_pct", "top5_share_pct",
                  "min_price_median_usd", "min_price_p10_usd", "min_price_max_usd",
                  "top_earners", "api_meta"}
        _c, _o, _l, series = self._run([rec("A", 1.0, iso(self.now))])
        keys = set(series[-1])
        self.assertTrue(legacy <= keys, legacy - keys)
        self.assertEqual(keys - legacy, {"settlement_max_last_at", "settlement_stale_days"})


class RealSnapshotRegression(unittest.TestCase):
    """用 2026-11-05 的真实快照复算：必须是 8 天，且 6 行指标与现文件逐行一致。"""

    def setUp(self):
        self.snap_path = DATA / "snapshots" / "2026-11-05.json"
        self.latest_path = DATA / "latest.md"
        if not self.snap_path.exists() or not self.latest_path.exists():
            self.skipTest("缺 2026-11-05 快照或 latest.md")

    def test_stale_days_is_eight_on_real_snapshot(self):
        snap = json.loads(self.snap_path.read_text(encoding="utf-8"))
        max_at, days = trt.settlement_staleness(snap["services"], snap["summary"]["captured_at_utc"])
        self.assertEqual(max_at, "2026-10-28T12:29:21Z")
        self.assertEqual(days, 8)

    def test_six_metric_lines_match_current_file(self):
        snap = json.loads(self.snap_path.read_text(encoding="utf-8"))
        s = snap["summary"]
        max_at, days = trt.settlement_staleness(snap["services"], s["captured_at_utc"])
        prev = None
        rows = [json.loads(x) for x in (DATA / "series.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        if len(rows) >= 2:
            prev = rows[-2]
        new_lines = trt.render_latest(s, prev, days, max_at)
        # 去掉警告段（前两行）与 R2 括号说明，逐行对比老文件
        body = [ln for ln in new_lines if not ln.startswith("> ⚠ 数据源结算层已停更")]
        body = [ln for ln in body if ln.strip() != "" or True]
        old = self.latest_path.read_text(encoding="utf-8").splitlines()
        old_metrics = [ln for ln in old if ln.startswith("- ")]
        new_metrics = [ln.split("（total 较上次")[0] for ln in body if ln.startswith("- ")]
        self.assertEqual(len(old_metrics), 6)
        self.assertEqual(new_metrics, old_metrics)


if __name__ == "__main__":
    unittest.main(verbosity=2)
