"""披露控制引擎单元测试（纯标准库，python3 -m unittest 即可运行）。

覆盖业务规则：
- 访问不足百次主遮挡（恰好 100 不遮、99 遮）
- 单份报告内合计行反推保护
- 同受众多份周报差值/线性推断 -> 二次遮挡；不同受众不关联
- 两份周报并发分发：合起来会泄露 -> held，先发一份后重审另一份
- 分发恢复幂等：重试不新增披露记录、可见内容一致
- 全部遮挡 -> blocked
"""
import os
import sys
import unittest

# 兼容两种测试落位：仓库内 /workspace/test，或规范的 test-artifacts/gb-1018/tests
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (
    os.path.join(_HERE, "..", "backend"),
    os.path.join(_HERE, "..", "..", "..", "workspace", "backend"),
):
    if os.path.isdir(os.path.join(_candidate, "src")):
        sys.path.insert(0, os.path.abspath(_candidate))
        break

from src.services.disclosure import (  # noqa: E402
    VISIT_THRESHOLD,
    REASON_PRIMARY,
    REASON_INFERENCE,
    REASON_HELD,
    REASON_TOTAL,
    IntegerSpan,
    DisclosureLedger,
    atom_key,
    build_report_rows,
    evaluate_release,
    mark_held,
)


def cell(day, channel, net, visits):
    return {"day": day, "channel": channel, "net_income": net, "visits": visits}


def index_of(rows_lists):
    atoms = sorted({a for rows in rows_lists for r in rows for a in r["atoms"]})
    return {a: i for i, a in enumerate(atoms)}


def snapshot_of(decision, audience="G"):
    return [{"audience": audience, "visible_atoms": decision["visible_atoms"]}]


# 固定场景：同一渠道两天，day1 高访问（可披露），day2 低访问（受保护）
CELLS = [
    cell("2026-09-01", "X", 1000.0, 500),
    cell("2026-09-02", "X", 2000.0, 40),
]


class IntegerSpanTests(unittest.TestCase):
    def test_basic_independence_and_span(self):
        s = IntegerSpan(3)
        self.assertTrue(s.add([1, 0, 0]))
        self.assertTrue(s.add([0, 1, 0]))
        self.assertFalse(s.add([2, 3, 0]))
        self.assertTrue(s.contains([2, 3, 0]))
        self.assertFalse(s.contains([0, 0, 1]))

    def test_difference_attack_detection(self):
        # [1,1,0] 与 [1,1,1] 之差 = [0,0,1]，第三个原子可被精确推断
        s = IntegerSpan(3)
        s.add([1, 1, 0])
        s.add([1, 1, 1])
        self.assertTrue(s.contains([0, 0, 1]))
        self.assertFalse(s.contains([0, 1, 0]))

    def test_negative_coefficients(self):
        s = IntegerSpan(4)
        s.add([1, 1, 1, 0])
        s.add([1, 1, 0, 0])
        self.assertTrue(s.contains([0, 0, 1, 0]))


class PrimarySuppressionTests(unittest.TestCase):
    def test_threshold_boundary(self):
        cells = [
            cell("2026-09-01", "A", 1000.0, VISIT_THRESHOLD),       # 恰好 100 -> 不遮
            cell("2026-09-01", "B", 800.0, VISIT_THRESHOLD - 1),    # 99 -> 遮
        ]
        rows = build_report_rows(cells, "channel_day")
        idx = index_of([rows])
        d = evaluate_release(rows, idx, "G", [])
        by_key = {r["key"]: r for r in d["rows"]}
        self.assertFalse(by_key[atom_key("A", "2026-09-01")]["suppressed"])
        low = by_key[atom_key("B", "2026-09-01")]
        self.assertTrue(low["suppressed"])
        self.assertIn(REASON_PRIMARY, low["reasons"])
        self.assertNotIn(low["atoms"], d["visible_atoms"])

    def test_internal_total_anti_complement(self):
        # 仅两行、day2 被遮：合计可见即可反推 day2，合计必须连带遮挡
        rows = build_report_rows(CELLS, "channel_day")
        idx = index_of([rows])
        d = evaluate_release(rows, idx, "G", [])
        total = next(r for r in d["rows"] if r["key"] == "__total__")
        self.assertTrue(total["suppressed"])
        self.assertIn(REASON_TOTAL, total["reasons"])
        # 高访问的 day1 仍照发
        day1 = next(r for r in d["rows"] if r["key"] == atom_key("X", "2026-09-01"))
        self.assertFalse(day1["suppressed"])

    def test_all_suppressed_is_blocked(self):
        cells = [cell("2026-09-01", "X", 1.0, 10), cell("2026-09-02", "X", 2.0, 20)]
        rows = build_report_rows(cells, "channel_day")
        idx = index_of([rows])
        d = evaluate_release(rows, idx, "G", [])
        self.assertEqual(d["status"], "blocked")
        self.assertEqual(d["visible_atoms"], [])


class CrossReportInferenceTests(unittest.TestCase):
    def test_difference_attack_between_two_reports(self):
        # 周报 A：渠道 X 两天合计（聚合行访问量 540，不触发渠道日百次规则，可发布）
        rows_a = build_report_rows(CELLS, "channel")
        idx = index_of([rows_a])
        d_a = evaluate_release(rows_a, idx, "G", [])
        self.assertEqual(d_a["status"], "published")
        channel_row = next(r for r in d_a["rows"] if r["key"] == "X")
        self.assertFalse(channel_row["suppressed"])

        # 周报 B：换筛选只发高访问 day1 的渠道日行
        rows_b = build_report_rows([CELLS[0]], "channel_day")
        idx_b = index_of([rows_a, rows_b])
        d_b = evaluate_release(rows_b, idx_b, "G", snapshot_of(d_a))
        day1 = d_b["rows"][0]
        # day1 高访问、台账中的渠道合计向量无法单独推出 day1 -> 照发
        self.assertFalse(day1["suppressed"])

        # 真正的差值攻击：台账先有 day1，再发“两天明细+合计”，
        # 合计与 day1 之差即受保护的 day2 -> 合计（及 day2）必须遮挡
        rows_total = build_report_rows(CELLS, "channel_day")
        d_total = evaluate_release(rows_total, idx_b, "G", snapshot_of(d_b))
        total = next(r for r in d_total["rows"] if r["key"] == "__total__")
        self.assertTrue(total["suppressed"])
        protected = next(r for r in d_total["rows"] if r["key"] == atom_key("X", "2026-09-02"))
        self.assertTrue(protected["suppressed"])
        self.assertTrue(
            any(r["code"] in (REASON_INFERENCE, REASON_TOTAL, REASON_PRIMARY) for r in d_total["reasons"])
        )

    def test_other_audience_is_not_linked(self):
        rows_a = build_report_rows(CELLS, "channel_day")
        idx = index_of([rows_a])
        d_a = evaluate_release(rows_a, idx, "G1", [])
        # 另一受众拿到同样的高访问 day1，不应受 G1 台账影响
        rows_b = build_report_rows([CELLS[0]], "channel_day")
        d_b = evaluate_release(rows_b, idx, "G2", snapshot_of(d_a, "G1"))
        self.assertFalse(d_b["rows"][0]["suppressed"])


class ConcurrentHeldTests(unittest.TestCase):
    def test_held_then_recheck_after_first_release(self):
        # 两份“渠道日明细”周报并发：单独评估都能发，但合起来 + 合计会泄露低访问 day2
        rows_a = build_report_rows(CELLS, "channel_day")
        rows_b = build_report_rows(CELLS, "channel_day")
        idx = index_of([rows_a, rows_b])

        d_a = evaluate_release(rows_a, idx, "G", [])
        d_b = evaluate_release(rows_b, idx, "G", [])
        # 并发冲突：B 先挂起，不写入台账、不产出可下载文件
        held_b = mark_held(d_b)
        self.assertEqual(held_b["status"], "held")
        self.assertIn(REASON_HELD, [r["code"] for r in held_b["reasons"]])

        # A 先发；恢复后 B 按“已含 A 的台账”重审，结论与单独评估不同且不会泄露 day2
        recheck = evaluate_release(rows_b, idx, "G", snapshot_of(d_a))
        protected = next(r for r in recheck["rows"] if r["key"] == atom_key("X", "2026-09-02"))
        self.assertTrue(protected["suppressed"])
        # 重审是确定性的：同输入再算一次结果一致（恢复原次发布）
        recheck2 = evaluate_release(rows_b, idx, "G", snapshot_of(d_a))
        self.assertEqual(
            [[r["key"], r["suppressed"]] for r in recheck["rows"]],
            [[r["key"], r["suppressed"]] for r in recheck2["rows"]],
        )


class IdempotencyTests(unittest.TestCase):
    def test_retry_same_snapshot_no_new_disclosure(self):
        rows = build_report_rows([CELLS[0]], "channel_day")  # 仅高访问行
        idx = index_of([rows])
        first = evaluate_release(rows, idx, "G", [])
        retry = evaluate_release(rows, idx, "G", [])
        self.assertEqual(first["visible_atoms"], retry["visible_atoms"])

        ledger = DisclosureLedger(idx)
        for atoms in first["visible_atoms"]:
            v = [0] * len(idx)
            for a in atoms:
                v[idx[a]] = 1
            self.assertTrue(ledger.publish(v))   # 首次写入，新增方向
            self.assertFalse(ledger.publish(v))  # 重试重复，不新增披露
            self.assertTrue(ledger.is_inferable(v))


class CrossFilterAtomProtectionTests(unittest.TestCase):
    def test_aggregate_row_suppressed_when_protected_atom_not_a_row(self):
        # 受保护的 day2 低访问原子不在本报告行里；B 只发渠道聚合行，
        # 台账已有 day1 -> 聚合与 day1 之差即 day2，聚合行必须被追加遮挡。
        cells_a = [cell("2026-09-01", "X", 1000.0, 500)]
        rows_a = build_report_rows(cells_a, "channel_day")
        idx = index_of([rows_a])
        d_a = evaluate_release(rows_a, idx, "G", [])
        snap = snapshot_of(d_a)

        cells_b = [cell("2026-09-01", "X", 1000.0, 500), cell("2026-09-02", "X", 2000.0, 40)]
        rows_b = build_report_rows(cells_b, "channel")  # 仅一个聚合行，day2 不是独立行
        idx_b = index_of([rows_a, rows_b])
        protected = [atom_key("X", "2026-09-02")]
        d_b = evaluate_release(rows_b, idx_b, "G", snap, protected_atoms=protected)
        agg = next(r for r in d_b["rows"] if r["key"] == "X")
        self.assertTrue(agg["suppressed"])
        self.assertIn(REASON_INFERENCE, agg["reasons"])

    def test_no_protected_atom_no_overblocking(self):
        cells = [cell("2026-09-01", "X", 1000.0, 500), cell("2026-09-02", "X", 2000.0, 600)]
        rows = build_report_rows(cells, "channel")
        idx = index_of([rows])
        d = evaluate_release(rows, idx, "G", [], protected_atoms=[])
        self.assertFalse(any(r["suppressed"] for r in d["rows"] if r["key"] != "__total__"))

    def test_republish_already_visible_value_is_not_overblocked(self):
        # 同一受众再发一份只含“已可见行”的周报：不扩展披露空间，应照发而非 blocked
        cells = [cell("2026-09-01", "Y", 900.0, 700), cell("2026-09-02", "X", 2000.0, 40)]
        rows_full = build_report_rows(cells, "channel_day")
        idx = index_of([rows_full])
        protected = [atom_key("X", "2026-09-02")]
        d_full = evaluate_release(rows_full, idx, "G", [], protected_atoms=protected)
        snap = snapshot_of(d_full)

        rows_y = build_report_rows([cells[0]], "channel_day")
        d_re = evaluate_release(rows_y, idx, "G", snap, protected_atoms=protected)
        self.assertEqual(d_re["status"], "published")
        self.assertFalse(any(r["suppressed"] for r in d_re["rows"] if r["key"] != "__total__"))

    def test_mixed_detail_and_aggregate_blocks_difference_attack(self):
        # 同一报告内：高访问日明细 + 渠道聚合并存，聚合−明细 可反推低访问日 -> 聚合与合计连带遮挡
        cells = [cell("2026-11-02", "直营", 11000.0, 420), cell("2026-11-03", "直营", 9000.0, 86)]
        rows = build_report_rows(cells, "channel_day") + build_report_rows(cells, "channel")
        idx = index_of([rows])
        protected = [atom_key("直营", "2026-11-03")]
        d = evaluate_release(rows, idx, "G", [], protected_atoms=protected)
        by_key = {r["key"]: r for r in d["rows"]}
        self.assertTrue(by_key["直营"]["suppressed"])
        self.assertTrue(by_key["__total__"]["suppressed"])
        self.assertFalse(by_key[atom_key("直营", "2026-11-02")]["suppressed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
