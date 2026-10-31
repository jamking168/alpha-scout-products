#!/usr/bin/env python3
"""fomc_diff.py「SEP 四种状态」单测（2026-10-31）。

覆盖：
  ① 404 + 非 SEP 月（2026-10-28）→ no_sep_meeting（正常缺失）
  ② 404 + SEP 月（2026-12-09）    → fetch_failed（异常，附 URL）
  ③ 网络错误                      → fetch_failed
  ④ 正常表 → verified，数值与改前口径一致（回归）
  ⑤ 200 但页里没表               → unverified（原语义）
  ⑥ render_cn 三种状态的人话区分
全部打桩 `http_get`，**不真连网**。
"""
import importlib.util
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parent
MOD = HERE.parent / "fomc_diff.py"


def _load():
    spec = importlib.util.spec_from_file_location("fomc_diff_under_test", MOD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FAKE_TABLE = """<table>
<tr><td>2026</td><td>2027</td><td>2028</td><td>2029</td><td>Longer run</td></tr>
<tr><td>Federal funds rate</td><td>4.1</td><td>4.1</td><td>3.9</td><td>3.6</td><td>3.2</td></tr>
<tr><td>June projection</td><td>3.8</td><td>3.6</td><td>3.4</td><td>3.3</td><td>3.1</td></tr>
</table>"""

FRED = {"current": None, "previous": None, "bp": None, "change_date": None}
STMT = {"status": "incomplete"}
FW = {"status": "unreachable"}


class SepStatusTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _fetch(self, retval):
        self.mod.http_get = lambda url, ua=None, timeout=30: retval
        return None

    # ① 非 SEP 月 + 404 → no_sep_meeting
    def test_404_non_sep_month_is_no_sep_meeting(self):
        self._fetch((None, b"HTTP Error 404: Not Found"))
        r = self.mod.fetch_sep_median("2026-10-28")
        self.assertEqual(r["parse_status"], "no_sep_meeting")
        self.assertIn("fomcprojtabl20261028.htm", r["url"])
        self.assertIn("3/6/9/12", r["note"])
        self.assertIsNone(r["median"])

    # ② SEP 月 + 404 → fetch_failed（官方页可能改名/未发布，必须当异常）
    def test_404_sep_month_is_fetch_failed(self):
        self._fetch((None, b"HTTP Error 404: Not Found"))
        r = self.mod.fetch_sep_median("2026-12-09")
        self.assertEqual(r["parse_status"], "fetch_failed")
        self.assertIn("HTTP 404", r["note"])
        self.assertIn("fomcprojtabl20261209.htm", r["url"])

    def test_404_sep_month_from_calendar_not_month_number(self):
        """日历说该发布 SEP 时，即使月份不是 3/6/9/12 也算异常（避免日历与月份口径打架时静默）。"""
        self.mod.FOMC_2026 = list(self.mod.FOMC_2026) + [("2026-11-05", True)]
        self._fetch((None, b"HTTP Error 404: Not Found"))
        r = self.mod.fetch_sep_median("2026-11-05")
        self.assertEqual(r["parse_status"], "fetch_failed")

    # ③ 网络错误 → fetch_failed
    def test_network_error_is_fetch_failed(self):
        self._fetch((None, b"<urlopen error timed out>"))
        r = self.mod.fetch_sep_median("2026-12-09")
        self.assertEqual(r["parse_status"], "fetch_failed")
        self.assertIn("网络错误", r["note"])

    def test_network_error_on_non_sep_month_is_still_failure(self):
        """非 SEP 月但**不是 404**（超时等）→ 不能当成「本次不发布」，必须是 fetch_failed。"""
        self._fetch((None, b"<urlopen error timed out>"))
        r = self.mod.fetch_sep_median("2026-10-28")
        self.assertEqual(r["parse_status"], "fetch_failed")

    def test_500_is_fetch_failed(self):
        self._fetch((500, b"oops"))
        r = self.mod.fetch_sep_median("2026-10-28")
        self.assertEqual(r["parse_status"], "fetch_failed")
        self.assertIn("HTTP 500", r["note"])

    # ④ verified 回归：数值口径与改前一致
    def test_verified_path_values_unchanged(self):
        self._fetch((200, FAKE_TABLE.encode()))
        r = self.mod.fetch_sep_median("2026-03-18")
        self.assertEqual(r["parse_status"], "verified")
        self.assertEqual(r["years"], ["2026", "2027", "2028", "2029", "Longer run"])
        self.assertEqual(r["median"], ["4.1", "4.1", "3.9", "3.6", "3.2"])
        self.assertEqual(r["june"], ["3.8", "3.6", "3.4", "3.3", "3.1"])

    # ⑤ 200 但没表 → unverified（原语义）
    def test_page_without_sep_table_is_unverified(self):
        self._fetch((200, b"<html><body><p>no table here</p></body></html>"))
        r = self.mod.fetch_sep_median("2026-03-18")
        self.assertEqual(r["parse_status"], "unverified")
        self.assertIsNone(r["median"])

    def test_never_returns_none(self):
        """JSON 里 sep 必须是对象（含 parse_status），不能是 null。"""
        for retval in ((None, b"HTTP Error 404: Not Found"), (500, b"x"), (200, b"<html></html>")):
            self._fetch(retval)
            for date in ("2026-10-28", "2026-12-09"):
                self.assertIsInstance(self.mod.fetch_sep_median(date), dict)


class RenderSepStatusTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _render(self, sep):
        return self.mod.render_cn("2026-10-28", "2026-09-16", FRED, STMT, sep, FW, "/tmp/x.json")

    def test_no_sep_meeting_wording(self):
        out = self._render({"url": "u", "parse_status": "no_sep_meeting",
                            "years": None, "median": None, "june": None, "note": "x"})
        self.assertIn("不发布 SEP", out)
        self.assertIn("非数据缺失", out)
        self.assertNotIn("抓取失败", out)
        # 只看 SEP 那一段，别被【1】FRED 的「获取失败」误伤
        sep_lines = [l for l in out.splitlines() if "点阵图" in l]
        self.assertTrue(sep_lines)
        self.assertFalse([l for l in sep_lines if "获取失败" in l or "未知" in l], sep_lines)

    def test_fetch_failed_wording(self):
        out = self._render({"url": "https://x/fomcprojtabl20261209.htm", "parse_status": "fetch_failed",
                            "years": None, "median": None, "june": None, "note": "HTTP 404"})
        self.assertIn("抓取失败", out)
        self.assertIn("HTTP 404", out)
        self.assertIn("fomcprojtabl20261209.htm", out)
        self.assertIn("数据缺失，不是「无点阵图」", out)
        self.assertNotIn("不发布 SEP", out)

    def test_unverified_wording_unchanged(self):
        out = self._render({"url": "u", "parse_status": "unverified",
                            "years": None, "median": None, "june": None, "note": "表结构未通过校验"})
        self.assertIn("解析未通过校验", out)

    def test_verified_table_rendered(self):
        out = self._render({"url": "u", "parse_status": "verified",
                            "years": ["2026", "2027", "2028", "2029", "Longer run"],
                            "median": ["4.1", "4.1", "3.9", "3.6", "3.2"],
                            "june": ["3.8", "3.6", "3.4", "3.3", "3.1"]})
        self.assertIn("本次中位数", out)
        self.assertIn("2026", out)
        self.assertIn("Longer run", out)


# ---- 2026-11-01 追加：June 对照行容忍空格子（P3）----

HTML_JUNE_WITH_BLANK = """<table>
<tr><td>2026</td><td>2027</td><td>2028</td><td>2029</td><td>Longer run</td></tr>
<tr><td>Federal funds rate</td><td>4.1</td><td>4.1</td><td>3.9</td><td>3.6</td><td>3.2</td></tr>
<tr><td>June projection</td><td>3.8</td><td>3.6</td><td>3.4</td><td></td><td>3.1</td></tr>
</table>"""

HTML_JUNE_ALL_TEXT = """<table>
<tr><td>2026</td><td>2027</td><td>2028</td><td>2029</td><td>Longer run</td></tr>
<tr><td>Federal funds rate</td><td>4.1</td><td>4.1</td><td>3.9</td><td>3.6</td><td>3.2</td></tr>
<tr><td>June projection</td><td>n.a.</td><td>see note</td><td>tbd</td><td>—</td><td>3.6–4.1</td></tr>
</table>"""

HTML_MEDIAN_WITH_BLANK = """<table>
<tr><td>2026</td><td>2027</td><td>2028</td><td>2029</td><td>Longer run</td></tr>
<tr><td>Federal funds rate</td><td>4.1</td><td>4.1</td><td>3.9</td><td></td><td>3.2</td></tr>
<tr><td>June projection</td><td>3.8</td><td>3.6</td><td>3.4</td><td>3.3</td><td>3.1</td></tr>
</table>"""


class TestJuneRowLenient(unittest.TestCase):
    """June 对照行：空格子只占位、不整行作废；非法文本仍整行作废；median 行不放宽。"""

    def setUp(self):
        self.mod = _load()

    def _fetch_html(self, html):
        self.mod.http_get = lambda url, ua=None, timeout=30: (200, html.encode())
        return self.mod.fetch_sep_median("2026-09-16")

    def test_june_with_one_blank_cell_keeps_five_columns(self):
        r = self._fetch_html(HTML_JUNE_WITH_BLANK)
        self.assertEqual(r["parse_status"], "verified")
        self.assertEqual(r["june"], ["3.8", "3.6", "3.4", "", "3.1"])
        self.assertEqual(len(r["june"]), len(r["years"]))     # 与年份列等长，不丢列

    def test_june_all_text_or_blank_still_rejected(self):
        r = self._fetch_html(HTML_JUNE_ALL_TEXT)
        self.assertEqual(r["parse_status"], "verified")       # median 本身是好的
        self.assertEqual(r["june"], [])                        # 对照行整行作废
        self.assertNotIn(None, r["june"])

    def test_june_all_blank_is_rejected(self):
        html = HTML_JUNE_WITH_BLANK.replace(
            "<td>3.8</td><td>3.6</td><td>3.4</td><td></td><td>3.1</td>",
            "<td></td><td>—</td><td>n/a</td><td></td><td></td>")
        self.mod.http_get = lambda url, ua=None, timeout=30: (200, html.encode())
        r = self.mod.fetch_sep_median("2026-09-16")
        self.assertEqual(r["june"], [], "非空少于 3 个就不能当对照数据")

    def test_median_row_stays_strict(self):
        """median 行有一个空格子 → 仍是 unverified（没放宽主行）。"""
        r = self._fetch_html(HTML_MEDIAN_WITH_BLANK)
        self.assertEqual(r["parse_status"], "unverified")
        self.assertIsNone(r["median"])

    def test_render_shows_dash_for_blank_june_cell(self):
        out = self.mod.render_cn(
            "2026-09-16", "2026-06-17", FRED, STMT,
            {"url": "u", "parse_status": "verified",
             "years": ["2026", "2027", "2028", "2029", "Longer run"],
             "median": ["4.1", "4.1", "3.9", "3.6", "3.2"],
             "june": ["3.8", "3.6", "3.4", "", "3.1"]},
            FW, "/tmp/x.json")
        line_2029 = [l for l in out.splitlines() if l.strip().startswith("2029")][0]
        self.assertIn("-", line_2029)
        self.assertNotIn("None", out)
        # 有值的列仍显示数字与 bp 差
        line_2026 = [l for l in out.splitlines() if l.strip().startswith("2026")][0]
        self.assertIn("3.8", line_2026)
        self.assertIn("+30bp", line_2026)

    def test_normalize_helper_directly(self):
        f = self.mod._normalize_june_cells
        self.assertEqual(f(["3.8", "3.6", "3.4", "", "3.1"]), ["3.8", "3.6", "3.4", "", "3.1"])
        self.assertEqual(f([""]), None)                     # 非空不足 3 个
        self.assertEqual(f(["3.8", "3.6", "abc", "", "3.1"]), None)   # 有非法文本
        self.assertEqual(f(["3.8", "3.6", "3.4", "3.3", "3.1", "3.6–4.1"]),
                         ["3.8", "3.6", "3.4", "3.3", "3.1"])        # 超出 5 列截断
        self.assertEqual(f(["3.8", "3.6", "3.4"]), ["3.8", "3.6", "3.4", "", ""])  # 缺列补 ''
        self.assertEqual(f(["11.0", "3.6", "3.4", "", "3.1"]), None)  # 超 10% 不合法


if __name__ == "__main__":
    unittest.main()
