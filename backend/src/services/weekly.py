"""经营周报服务：源账筛选、披露控制评估、并发串行化与幂等发布。

业务约束落点：
- 同批源账换筛选再发：filters 决定渠道日单元子集，atom 宇宙取同一数据集全集，保证向量可比。
- 同一受众拼报推断：按 audience 汇总 WeeklyDelivery.released_snapshot 重建台账。
- 并发：同一 audience 同时刻只允许一份进入发布，另一份 held，发布后按新台账重审。
- 中断恢复：idempotency_key 命中即复用既有发布记录与文件，重试 attempts+1，不新增披露。
- 下载只写发布内容：被遮行输出 MASK_VALUE，原因仅在 API/页面展示，不进下载包。
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import time

from fastapi import HTTPException
from sqlalchemy import select

from src.config.settings import ARTIFACT_DIR
from src.models import Dataset, WeeklyReport, WeeklyDelivery
from src.services import disclosure
from src.services.disclosure import MASK_VALUE


# --------------------------------------------------------------------------- #
# 源账 -> 渠道日净收入单元
# --------------------------------------------------------------------------- #
def channel_day_cells(dataset: Dataset, filters: dict) -> list[dict]:
    channels = set(filters.get("channels") or [])
    start = filters.get("date_from")
    end = filters.get("date_to")
    cells = []
    for rec in dataset.records:
        day = str(rec["day"])
        channel = str(rec["channel"])
        if channels and channel not in channels:
            continue
        if start and day < start:
            continue
        if end and day > end:
            continue
        net = round(float(rec["gross"]) - float(rec["refunds"]) - float(rec["discount"]), 4)
        cells.append({"day": day, "channel": channel, "net_income": net, "visits": int(rec["visits"])})
    cells.sort(key=lambda c: (c["channel"], c["day"]))
    return cells


def atom_universe(dataset: Dataset) -> tuple[list[str], dict[str, int]]:
    atoms = sorted({disclosure.atom_key(str(r["channel"]), str(r["day"])) for r in dataset.records})
    return atoms, {a: i for i, a in enumerate(atoms)}


def low_visit_atoms(dataset: Dataset) -> list[str]:
    """数据集内所有访问不足百次的渠道日原子（跨筛选的全局保护对象）。"""
    return sorted(
        disclosure.atom_key(str(r["channel"]), str(r["day"]))
        for r in dataset.records
        if int(r["visits"]) < disclosure.VISIT_THRESHOLD
    )


def audience_snapshots(db, audience: str, exclude_delivery_id: int | None = None,
                       exclude_slot: int | None = None) -> list[dict]:
    """同一受众所有已成功发布、可下载的披露快照（held/blocked 不计入）。

    exclude_slot: 并发评估时排除同一发布窗口的其他快照，得到“独立决策”；
    历史窗口的快照始终保留（已能下载的内容都要算进台账）。
    """
    stmt = select(WeeklyDelivery).where(
        WeeklyDelivery.audience == audience,
        WeeklyDelivery.status == "published",
    )
    snapshots = []
    for d in db.scalars(stmt):
        if exclude_delivery_id is not None and d.id == exclude_delivery_id:
            continue
        if exclude_slot is not None and f"slot-{exclude_slot}" in (d.idempotency_key or ""):
            continue
        snap = d.released_snapshot or {}
        if snap.get("visible_atoms"):
            snapshots.append({"audience": audience, "visible_atoms": snap["visible_atoms"]})
    return snapshots


# --------------------------------------------------------------------------- #
# 评估（不写库）：供“试看遮挡效果”与正式发布共用同一套判定
# --------------------------------------------------------------------------- #
def evaluate(db, report: WeeklyReport, extra_snapshots: list[dict] | None = None,
             exclude_slot: int | None = None) -> dict:
    dataset = db.get(Dataset, report.dataset_id)
    if not dataset:
        raise HTTPException(409, "周报关联的源账批次不存在")
    cells = channel_day_cells(dataset, report.filters or {})
    if not cells:
        raise HTTPException(422, "当前筛选没有命中任何渠道日数据")
    rows = disclosure.build_report_rows(cells, report.group_by)
    _, atom_index = atom_universe(dataset)
    snapshots = audience_snapshots(db, report.audience, exclude_slot=exclude_slot)
    if extra_snapshots:
        snapshots = snapshots + extra_snapshots
    decision = disclosure.evaluate_release(
        rows, atom_index, report.audience, snapshots,
        protected_atoms=low_visit_atoms(dataset),
    )
    decision["audience"] = report.audience
    decision["filters"] = report.filters or {}
    decision["group_by"] = report.group_by
    return decision


# --------------------------------------------------------------------------- #
# 发布（worker 调用）：并发串行化 + 幂等
# --------------------------------------------------------------------------- #
def _idempotency_key(report: WeeklyReport, run_slot: int) -> str:
    # run_slot 按周期对齐，同一计划发布窗口内的中断重试落到同一键
    return f"wr-{report.id}-slot-{run_slot}"


def _render_csv(decision: dict) -> bytes:
    out = io.StringIO()
    out.write("﻿")
    writer = csv.writer(out)
    writer.writerow(["对账行", "访问量", "日净收入(CNY)", "发布状态"])
    for row in decision["rows"]:
        writer.writerow([
            row["label"],
            row["visits"],
            MASK_VALUE if row["suppressed"] else row["net_income"],
            "遮挡" if row["suppressed"] else "可见",
        ])
    return out.getvalue().encode("utf-8")


def _slot_conflicts(db, report: WeeklyReport, run_slot: int) -> list[WeeklyDelivery]:
    """同受众、同一发布窗口内已先行落库的其他周报发布/挂起记录。"""
    out = []
    for other in db.scalars(
        select(WeeklyDelivery).where(
            WeeklyDelivery.audience == report.audience,
            WeeklyDelivery.status.in_(("published", "held")),
        )
    ):
        if other.weekly_report_id == report.id:
            continue
        if f"slot-{run_slot}" in (other.idempotency_key or ""):
            out.append(other)
    return out


def publish_due(db, report: WeeklyReport, now: float | None = None) -> WeeklyDelivery | None:
    now = now if now is not None else time.time()
    run_slot = int(now // max(report.interval_seconds, 1))
    key = _idempotency_key(report, run_slot)

    existing = db.scalar(select(WeeklyDelivery).where(WeeklyDelivery.idempotency_key == key))
    if existing:
        # 中断恢复：复用原发布记录与文件，仅累加尝试次数，绝不新增披露
        existing.attempts += 1
        existing.updated_at = now
        if existing.status == "held":
            return _recheck_held(db, report, existing, now)
        report.last_decision = existing.decision or {}
        report.status = existing.status
        return existing

    # 无本窗口记录但上一窗口仍挂起：到期自动按最新台账重审，恢复原次发布
    pending_held = db.scalar(
        select(WeeklyDelivery).where(
            WeeklyDelivery.weekly_report_id == report.id,
            WeeklyDelivery.status == "held",
        ).order_by(WeeklyDelivery.id)
    )
    if pending_held is not None:
        return _recheck_held(db, report, pending_held, now)

    # 独立决策：只看历史窗口台账（排除同窗口并发者，避免把并发者误计入自身历史）
    standalone = evaluate(db, report, exclude_slot=run_slot)

    # 合并泄露检测：同窗口先发者已能下载的向量并入后，
    # 是否有受保护（访问<100）的渠道日原子从“推不出”变成“可精确推断”
    conflicts = _slot_conflicts(db, report, run_slot)
    dataset = db.get(Dataset, report.dataset_id)
    _, atom_index = atom_universe(dataset)
    ledger_vectors = disclosure.vectors_of(
        [a for snap in audience_snapshots(db, report.audience, exclude_slot=run_slot)
         for a in snap["visible_atoms"]],
        atom_index,
    )
    conflict_vectors = disclosure.vectors_of(
        [a for c in conflicts for a in (c.released_snapshot or {}).get("visible_atoms", [])],
        atom_index,
    )
    leaked = disclosure.protected_atom_leakage(
        atom_index, low_visit_atoms(dataset),
        ledger_vectors + conflict_vectors,
        disclosure.vectors_of(standalone["visible_atoms"], atom_index),
    )
    if conflicts and leaked:
        decision = disclosure.mark_held(standalone)
        decision["decision_reason"] = (
            disclosure.REASON_TEXT[disclosure.REASON_HELD]
            + "；可反推受保护渠道日：" + "、".join(leaked)
        )
    else:
        decision = standalone

    delivery = WeeklyDelivery(
        weekly_report_id=report.id,
        audience=report.audience,
        idempotency_key=key,
        status=decision["status"],
        released_snapshot={"audience": report.audience, "visible_atoms": decision["visible_atoms"]},
        decision=decision,
        attempts=1,
        created_at=now,
        updated_at=now,
    )
    db.add(delivery)
    db.flush()

    if decision["status"] == "published":
        _write_artifact(delivery, decision)
    report.last_decision = decision
    report.status = decision["status"]
    report.next_run = now + report.interval_seconds
    return delivery


def _recheck_held(db, report: WeeklyReport, delivery: WeeklyDelivery, now: float) -> WeeklyDelivery:
    """挂起周报在另一份发布后按最新台账重审：通过则补发，仍泄露则保持拦截结论。"""
    # 先发那份已落库为 published，常规评估会自动把它并入同受众台账
    decision = evaluate(db, report)

    delivery.status = decision["status"]
    delivery.decision = decision
    delivery.released_snapshot = {"audience": report.audience, "visible_atoms": decision["visible_atoms"]}
    delivery.updated_at = now
    if decision["status"] == "published":
        _write_artifact(delivery, decision)
    report.last_decision = decision
    report.status = decision["status"]
    report.next_run = now + report.interval_seconds
    return delivery


def _write_artifact(delivery: WeeklyDelivery, decision: dict) -> None:
    data = _render_csv(decision)
    delivery.filename = f"weekly-{delivery.weekly_report_id}-delivery-{delivery.id}.csv"
    delivery.checksum = hashlib.sha256(data).hexdigest()
    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    with open(os.path.join(ARTIFACT_DIR, delivery.filename), "wb") as f:
        f.write(data)


def release_held(db, delivery_id: int) -> WeeklyDelivery:
    """管理员手动放行挂起周报：强制按当前台账重审（页面“重审另一份”入口）。"""
    delivery = db.get(WeeklyDelivery, delivery_id)
    if not delivery:
        raise HTTPException(404, "发布记录不存在")
    report = db.get(WeeklyReport, delivery.weekly_report_id)
    if not report:
        raise HTTPException(404, "周报计划不存在")
    if delivery.status != "held":
        raise HTTPException(409, "仅挂起中的周报需要重审")
    return _recheck_held(db, report, delivery, time.time())


def artifact_path(delivery: WeeklyDelivery) -> str | None:
    if delivery.status != "published" or not delivery.filename:
        return None
    path = os.path.join(ARTIFACT_DIR, delivery.filename)
    return path if os.path.exists(path) else None
