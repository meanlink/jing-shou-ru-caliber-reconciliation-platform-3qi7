"""经营周报统计披露控制（脱敏与推断拦截）引擎。

规则来源（业务口径）：
1. 访问不足百次（visits < VISIT_THRESHOLD）的渠道日净收入必须遮住（主遮挡）。
2. 同批源账换筛选再发时，若同一受众能用多份周报拼出被遮金额（线性推断/差值攻击），
   追加二次遮挡挡住会泄露的项，其余照发。
3. 两份周报并发分发且合起来会泄露时，先发一份、另一份按发布后的台账重审（held）。
4. 分发中断后恢复按原次发布；重试不新增披露记录；已能下载的内容都计入台账。
5. 页面可试看遮挡效果、查拦截原因；下载以实际发布内容为准。

实现要点（仅依赖 Python 标准库）：
- 每个分组行 = 渠道日原子单元（atom，形如 channel|YYYY-MM-DD）上的 0/1 关联向量。
- 已发布可见行构成披露基空间；若候选行的向量落在该空间内，其金额可被精确推出，必须拦截。
- 整数高斯消元做线性相关判定，避免浮点误差；金额是否相等不参与判定，向量可推出即拦截。
"""
from __future__ import annotations

from typing import Iterable

VISIT_THRESHOLD = 100
MASK_VALUE = "***"

# 拦截/遮挡原因码（页面按此展示中文解释）
REASON_PRIMARY = "primary_low_visits"          # 访问不足百次，主遮挡
REASON_INFERENCE = "secondary_inference_risk"  # 可由同受众已发布周报推断（差值攻击）
REASON_TOTAL = "secondary_protect_total"       # 保护合计行所需的连带遮挡
REASON_HELD = "concurrent_review_pending"      # 并发分发，先发另一份后重审
REASON_EMPTY = "empty_release_blocked"         # 遮挡后无可发布内容

REASON_TEXT = {
    REASON_PRIMARY: "访问不足100次，净收入按规则主遮挡",
    REASON_INFERENCE: "同一受众可由已发布周报（含差值拼合）推断出该金额，追加遮挡",
    REASON_TOTAL: "为避免经合计行反推，连带遮挡",
    REASON_HELD: "与另一份周报并发分发且合并存在泄露，待先发完成后重审",
    REASON_EMPTY: "全部行均被遮挡，无可用发布内容",
}


# --------------------------------------------------------------------------- #
# 整数线性代数：判断向量能否由已知基空间线性表示
# --------------------------------------------------------------------------- #
class IntegerSpan:
    """整数向量张成空间的增量式基（精确整数消元，用主元列字典）。

    每个基向量维护首个非零列（pivot）；新向量反复消元，归零即可线性表示。
    消元用交叉相乘（cur[p]*basis - basis[p]*cur），避免异号地板除法截断；
    权重可以是任意整数（分组行通常是 0/1，差值攻击会产生 -1 系数）。
    """

    def __init__(self, width: int):
        self.width = width
        self._pivots: dict[int, list[int]] = {}

    @staticmethod
    def _first_nonzero(vec: list[int]) -> int:
        for i, val in enumerate(vec):
            if val != 0:
                return i
        return -1

    def reduce(self, vec: Iterable[int]) -> list[int]:
        """返回 vec 被当前基空间消元后的残余向量；全 0 表示可线性表示。"""
        cur = list(vec)
        for _ in range(self.width + 1):
            p = self._first_nonzero(cur)
            if p < 0 or p not in self._pivots:
                return cur
            basis = self._pivots[p]
            a, b = cur[p], basis[p]
            # 主元列：b*a - a*b = 0，严格向更右的列推进
            cur = [b * x - a * y for x, y in zip(cur, basis)]
        return cur

    def contains(self, vec: Iterable[int]) -> bool:
        return not any(self.reduce(vec))

    def add(self, vec: Iterable[int]) -> bool:
        """加入向量；已在空间内返回 False，新增独立方向返回 True。"""
        cur = self.reduce(vec)
        p = self._first_nonzero(cur)
        if p < 0:
            return False
        # 反向简化已有基向量，保持阶梯形（主元列同样归零）
        for col, basis in list(self._pivots.items()):
            if basis[p]:
                self._pivots[col] = [cur[p] * x - basis[p] * y for x, y in zip(basis, cur)]
        self._pivots[p] = cur
        return True


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
def atom_key(channel: str, day: str) -> str:
    return f"{channel}|{day}"


def build_report_rows(cells: list[dict], group_by: str) -> list[dict]:
    """把渠道日单元聚成周报行。

    输入 cell: {"day","channel","net_income","visits"}
    group_by:
      - channel_day：每个渠道日一行（规则原文的“渠道日净收入”）
      - channel     ：按渠道汇总
      - week        ：按自然周汇总
      - channel_week：按渠道×自然周汇总
    每行带 atoms（原子键列表）、关联向量（构建台账时再映射到列）、visits、金额。
    """
    if group_by not in ("channel_day", "channel", "week", "channel_week"):
        raise ValueError(f"未知周报分组：{group_by}")

    def group_of(cell: dict) -> tuple[str, list]:
        if group_by == "channel_day":
            return atom_key(cell["channel"], cell["day"]), [cell]
        if group_by == "channel":
            return cell["channel"], [cell]
        year, week, _ = iso_week(cell["day"])
        if group_by == "week":
            return f"{year}-W{week:02d}", [cell]
        return f"{cell['channel']}|{year}-W{week:02d}", [cell]

    buckets: dict[str, list[dict]] = {}
    order: list[str] = []
    for cell in cells:
        key, _ = group_of(cell)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(cell)

    rows = []
    for key in order:
        members = buckets[key]
        atoms = sorted(atom_key(c["channel"], c["day"]) for c in members)
        low_atoms = sorted(
            atom_key(c["channel"], c["day"]) for c in members if int(c["visits"]) < VISIT_THRESHOLD
        )
        rows.append({
            "key": key,
            "label": key,
            "atoms": atoms,
            "low_visit_atoms": low_atoms,
            "visits": sum(int(c["visits"]) for c in members),
            "net_income": round(sum(float(c["net_income"]) for c in members), 4),
            "suppressed": False,
            "reasons": [],
        })
    return rows


def iso_week(day: str) -> tuple[int, int, int]:
    from datetime import date
    y, m, d = (int(x) for x in str(day).split("-"))
    return date(y, m, d).isocalendar()


def with_total_row(rows: list[dict], atoms_universe: list[str]) -> dict:
    """合计行 = 全部原子的并集（每个原子权重 1）。"""
    seen = set()
    atoms = []
    for row in rows:
        for a in row["atoms"]:
            if a not in seen:
                seen.add(a)
                atoms.append(a)
    return {
        "key": "__total__",
        "label": "合计",
        "atoms": atoms,
        "visits": sum(r["visits"] for r in rows),
        "net_income": round(sum(r["net_income"] for r in rows), 4),
        "suppressed": False,
        "reasons": [],
    }


def vectorize(rows: Iterable[dict], atom_index: dict[str, int]) -> list[list[int]]:
    width = len(atom_index)
    vectors = []
    for row in rows:
        v = [0] * width
        for a in row["atoms"]:
            v[atom_index[a]] = 1
        vectors.append(v)
    return vectors


# --------------------------------------------------------------------------- #
# 披露台账：同一受众已成功下载/发布的全部可见行
# --------------------------------------------------------------------------- #
class DisclosureLedger:
    """同一受众（audience）的历史披露基空间。

    持久化形态由服务层负责（见 weekly 服务的 released_snapshot JSON）。
    本类只做内存计算：加入已发布行、判定候选行是否可推断。
    """

    def __init__(self, atom_index: dict[str, int], visible_vectors: list[list[int]] | None = None):
        self.atom_index = atom_index
        self.span = IntegerSpan(len(atom_index))
        if visible_vectors:
            for v in visible_vectors:
                self.span.add(v)

    def is_inferable(self, vector: list[int]) -> bool:
        return self.span.contains(vector)

    def publish(self, vector: list[int]) -> bool:
        return self.span.add(vector)


def _load_ledger(audience: str, atom_index: dict[str, int], released_snapshots: list[dict]) -> DisclosureLedger:
    """从历史已发布快照重建台账。

    released_snapshots: [{"audience","visible_atoms":[[atom...], ...]}]
    只取同一受众、且实际进入下载包的行（重试复用同一快照，不会重复计数）。
    """
    ledger = DisclosureLedger(atom_index)
    width = len(atom_index)
    for snap in released_snapshots:
        if snap.get("audience") != audience:
            continue
        for atoms in snap.get("visible_atoms", []):
            v = [0] * width
            for a in atoms:
                if a in atom_index:
                    v[atom_index[a]] = 1
            ledger.span.add(v)
    return ledger


# --------------------------------------------------------------------------- #
# 核心评估：主遮挡 + 推断拦截 + 连带遮挡
# --------------------------------------------------------------------------- #
def evaluate_release(
    rows: list[dict],
    atom_index: dict[str, int],
    audience: str,
    released_snapshots: list[dict],
    include_total: bool = True,
    protected_atoms: list[str] | None = None,
) -> dict:
    """评估一份周报对某受众能否发布。

    protected_atoms：跨筛选的全局受保护渠道日原子（访问<100）。
    即使这些原子不是当前报告的行，台账∪当前可见行也不得把它们新推断出来。

    返回：
    {
      "status": "published" | "held" | "blocked",
      "rows": [带 suppressed/reasons 的行（含合计）],
      "visible_rows": [...], "suppressed_rows": [...],
      "visible_atoms": [[...]],       # 成功发布时写入台账的向量内容
      "reasons": [行级原因汇总],
      "decision_reason": 总体说明,
    }
    """
    all_rows = [dict(r, reasons=list(r.get("reasons", []))) for r in rows]
    if include_total:
        all_rows.append(with_total_row(rows, sorted(atom_index)))

    vectors = vectorize(all_rows, atom_index)
    for row, vec in zip(all_rows, vectors):
        row["vector"] = vec

    # 1) 主遮挡：规则作用于渠道日单元（visits < 100 的渠道日净收入必须遮住）。
    #    channel_day 分组下每行即一个渠道日；聚合行若整体访问量不足百次同样遮挡。
    #    聚合行内含低访问渠道日时，先记录标记，由第 3~4 步的推断/合计保护决定是否连带遮挡。
    for row in all_rows:
        if row["key"] == "__total__":
            continue
        if row["visits"] < VISIT_THRESHOLD:
            row["suppressed"] = True
            row["reasons"].append(REASON_PRIMARY)

    # 2) 同受众历史台账仅作为判定基准，不直接遮挡：
    #    向量已在台账内的行是“已可见值的重复发布”，不扩展披露空间、不产生新泄露，
    #    应照发；真正要挡的是会让受保护原子从“推不出”变“可推出”的行，见第 5.5 步。
    ledger = _load_ledger(audience, atom_index, released_snapshots)

    # 3) 报告内部闭合推断 + 合计行保护：
    #    判定空间 = 同受众历史披露台账 ∪ 当前报告可见行。
    #    任何被遮行只要落进这个合并空间（差值攻击），就继续追加二次遮挡直到稳定。
    def combined_span() -> IntegerSpan:
        span = DisclosureLedger(atom_index)
        # 以历史台账为起点（复制其基）
        for vec in ledger.span._pivots.values():
            span.span.add(vec)
        for row in all_rows:
            if not row["suppressed"]:
                span.span.add(row["vector"])
        return span.span

    def greedy_secondary() -> None:
        for _ in range(len(all_rows) + 1):
            span = combined_span()
            leaking = [
                row for row in all_rows
                if row["suppressed"] and span.contains(row["vector"])
            ]
            if not leaking:
                return
            # 选一个当前可见、且能切断泄露路径的行做连带遮挡：
            # 优先原子数最少（信息损失最小）的非合计可见行。
            target = leaking[0]
            candidates = [
                r for r in all_rows
                if not r["suppressed"] and r["key"] != "__total__"
                and any(v != 0 for v in [a and b for a, b in zip(r["vector"], target["vector"])])
            ]
            candidates.sort(key=lambda r: (len(r["atoms"]), r["visits"]))
            if not candidates:
                # 没有可连带的明细行时，合计行本身成为泄露通道
                total = next((r for r in all_rows if r["key"] == "__total__"), None)
                if total and not total["suppressed"]:
                    total["suppressed"] = True
                    total["reasons"].append(REASON_TOTAL)
                    continue
                return
            pick = candidates[0]
            pick["suppressed"] = True
            pick["reasons"].append(REASON_INFERENCE)

    greedy_secondary()

    # 4) 合计行保护：把合计并入判定空间后，若任一被遮行因此变得可推断（含经台账差值反推），
    #    则遮挡合计行。
    total_row = next((r for r in all_rows if r["key"] == "__total__"), None)
    if total_row and not total_row["suppressed"]:
        span_with_total = combined_span()
        span_with_total.add(total_row["vector"])
        if any(span_with_total.contains(r["vector"]) for r in all_rows if r["suppressed"]):
            total_row["suppressed"] = True
            total_row["reasons"].append(REASON_TOTAL)

    # 5) 终判：再跑一轮稳定性检查
    greedy_secondary()

    # 5.5) 原子级反推保护（核心）：台账 ∪ 当前可见行不得“新推出”任何受保护渠道日原子。
    #      受保护原子可以不是本报告的行（被筛选排除），但仍可能经聚合/差值被拼出。
    protected = [a for a in (protected_atoms or []) if a in atom_index]
    if protected:
        width = len(atom_index)
        for _ in range(len(all_rows) + 1):
            span = combined_span()
            leaking_atoms = []
            for atom in protected:
                unit = [0] * width
                unit[atom_index[atom]] = 1
                if not ledger.is_inferable(unit) and span.contains(unit):
                    leaking_atoms.append(atom)
            if not leaking_atoms:
                break
            atom = leaking_atoms[0]
            # 优先遮挡直接包含该原子、原子数最少的可见明细行（信息损失最小）
            candidates = [
                r for r in all_rows
                if not r["suppressed"] and r["key"] != "__total__" and atom in r["atoms"]
            ]
            if not candidates:  # 纯跨组线性组合时，退回任意有原子重叠的可见行
                candidates = [
                    r for r in all_rows
                    if not r["suppressed"] and r["key"] != "__total__"
                    and any(a in r["atoms"] for a in protected)
                ]
            if not candidates:
                total_row0 = next((r for r in all_rows if r["key"] == "__total__"), None)
                if total_row0 and not total_row0["suppressed"]:
                    total_row0["suppressed"] = True
                    total_row0["reasons"].append(REASON_TOTAL)
                    continue
                break
            pick = min(candidates, key=lambda r: (len(r["atoms"]), r["visits"]))
            pick["suppressed"] = True
            pick["reasons"].append(REASON_INFERENCE)
        greedy_secondary()

    # 6) 兜底：所有明细行都被遮时，合计行不得单独发布（合计即被遮值之和）
    detail_rows = [r for r in all_rows if r["key"] != "__total__"]
    total_row = next((r for r in all_rows if r["key"] == "__total__"), None)
    if total_row and not total_row["suppressed"] and detail_rows and all(r["suppressed"] for r in detail_rows):
        total_row["suppressed"] = True
        total_row["reasons"].append(REASON_TOTAL)

    for r in all_rows:
        r.pop("vector", None)

    visible = [r for r in all_rows if not r["suppressed"]]
    suppressed = [r for r in all_rows if r["suppressed"]]

    reasons_summary = []
    for r in suppressed:
        for code in r["reasons"]:
            reasons_summary.append({"row": r["label"], "code": code, "reason": REASON_TEXT[code]})

    # 合计行可能与某明细行同向量，台账按唯一向量记录，避免重试时重复计数
    unique_vectors = []
    seen = set()
    for r in visible:
        key = tuple(r["atoms"])
        if key not in seen:
            seen.add(key)
            unique_vectors.append(r["atoms"])

    if not visible:
        return {
            "status": "blocked",
            "rows": all_rows,
            "visible_rows": [],
            "suppressed_rows": suppressed,
            "visible_atoms": [],
            "reasons": reasons_summary + [{"row": "*", "code": REASON_EMPTY, "reason": REASON_TEXT[REASON_EMPTY]}],
            "decision_reason": REASON_TEXT[REASON_EMPTY],
        }

    return {
        "status": "published",
        "rows": all_rows,
        "visible_rows": visible,
        "suppressed_rows": suppressed,
        "visible_atoms": unique_vectors,
        "reasons": reasons_summary,
        "decision_reason": "通过主遮挡与推断检查，按发布内容交付" if not suppressed else "部分行遮挡后交付，其余照发",
    }


def mark_held(decision: dict) -> dict:
    """并发冲突时把可发布决策降级为 held（先发另一份，之后按新台账重审）。"""
    decision = dict(decision)
    decision["status"] = "held"
    decision["decision_reason"] = REASON_TEXT[REASON_HELD]
    decision.setdefault("reasons", []).append(
        {"row": "*", "code": REASON_HELD, "reason": REASON_TEXT[REASON_HELD]}
    )
    return decision


def protected_atom_leakage(
    atom_index: dict[str, int],
    protected_atoms: list[str],
    ledger_vectors: list[list[int]],
    candidate_vectors: list[list[int]],
) -> list[str]:
    """检测候选周报与既有台账合并后，新变得可推断的受保护原子。

    - protected_atoms：访问不足百次的渠道日原子（规则强制保护对象）。
    - ledger_vectors：同一受众已能下载的披露向量（含同窗口先发者）。
    - candidate_vectors：当前周报独立评估后的可见向量。
    返回“台账单独推不出、但合并后能推出”的受保护原子键（即新增泄露）。
    """
    width = len(atom_index)
    base = IntegerSpan(width)
    for v in ledger_vectors:
        base.add(v)
    merged = IntegerSpan(width)
    for v in ledger_vectors:
        merged.add(v)
    for v in candidate_vectors:
        merged.add(v)

    leaked = []
    for atom in protected_atoms:
        if atom not in atom_index:
            continue
        vec = [0] * width
        vec[atom_index[atom]] = 1
        if not base.contains(vec) and merged.contains(vec):
            leaked.append(atom)
    return leaked


def vectors_of(atoms_lists: list[list[str]], atom_index: dict[str, int]) -> list[list[int]]:
    width = len(atom_index)
    out = []
    for atoms in atoms_lists:
        v = [0] * width
        for a in atoms:
            if a in atom_index:
                v[atom_index[a]] = 1
        out.append(v)
    return out
