import os
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from redis import Redis

from src.config.database import get_db
from src.config.settings import ARTIFACT_DIR, REDIS_URL
from src.models import Dataset, WeeklyReport, WeeklyDelivery
from src.schemas.weekly import WeeklyReportInput, WeeklyPreviewInput
from src.middlewares.auth import current_user, admin_only
from src.services import weekly
from src.services.audit import record
from src.utils.serialization import weekly_report_dict, weekly_delivery_dict

router = APIRouter(prefix="/weekly")

# 与 worker.tick_weekly 共用同一把锁，保证手动触发与定时触发的并发串行化一致
PUBLISH_LOCK = "weekly:publish:lock"

ALLOWED_GROUP_BY = ("channel_day", "channel", "week", "channel_week")


def _filters_from(data) -> dict:
    if data.group_by not in ALLOWED_GROUP_BY:
        raise HTTPException(422, "分组方式仅支持 channel_day/channel/week/channel_week")
    for d in (data.date_from, data.date_to):
        if d is not None:
            try:
                from datetime import date
                date.fromisoformat(d)
            except ValueError:
                raise HTTPException(422, "日期须为 YYYY-MM-DD") from None
    if data.date_from and data.date_to and data.date_from > data.date_to:
        raise HTTPException(422, "开始日期不能晚于结束日期")
    return {"channels": data.channels or [], "date_from": data.date_from, "date_to": data.date_to}


@router.get("/reports")
def list_reports(db=Depends(get_db), u=Depends(current_user)):
    rows = db.scalars(select(WeeklyReport).order_by(WeeklyReport.id.desc())).all()
    return [weekly_report_dict(r) for r in rows]


@router.post("/reports", status_code=201)
def create_report(data: WeeklyReportInput, db=Depends(get_db), u=Depends(admin_only)):
    import time
    filters = _filters_from(data)
    if not db.get(Dataset, data.dataset_id):
        raise HTTPException(404, "源账批次不存在")
    r = WeeklyReport(
        name=data.name, dataset_id=data.dataset_id, filters=filters, group_by=data.group_by,
        audience=data.audience, enabled=True, next_run=time.time() + data.delay_seconds,
        interval_seconds=data.interval_seconds, status="queued", created_by=u.id, created_at=time.time(),
    )
    db.add(r)
    db.flush()
    record(db, u.email, "weekly.schedule", {"id": r.id, "audience": r.audience, "filters": filters})
    db.commit()
    return weekly_report_dict(r)


@router.post("/reports/{id}/toggle")
def toggle_report(id: int, db=Depends(get_db), u=Depends(admin_only)):
    r = db.get(WeeklyReport, id)
    if not r:
        raise HTTPException(404, "周报计划不存在")
    r.enabled = not r.enabled
    record(db, u.email, "weekly.toggle", {"id": id, "enabled": r.enabled})
    db.commit()
    return weekly_report_dict(r)


@router.post("/reports/{id}/run")
def run_now(id: int, db=Depends(get_db), u=Depends(admin_only)):
    """立即触发一次发布评估（同步走完整披露控制与并发串行化）。"""
    r = db.get(WeeklyReport, id)
    if not r:
        raise HTTPException(404, "周报计划不存在")
    client = Redis.from_url(REDIS_URL, socket_timeout=3)
    lock = client.lock(PUBLISH_LOCK, timeout=30, blocking_timeout=5)
    if not lock.acquire(blocking=True):
        raise HTTPException(409, "另一份周报正在分发，请稍后重试")
    try:
        delivery = weekly.publish_due(db, r)
        record(db, u.email, "weekly.run", {"id": id, "delivery": delivery.id, "status": delivery.status})
        db.commit()
        return weekly_delivery_dict(delivery)
    finally:
        try:
            lock.release()
        except Exception:
            pass
        client.close()


@router.post("/preview")
def preview(data: WeeklyPreviewInput, db=Depends(get_db), u=Depends(current_user)):
    """试看遮挡效果与拦截原因：不落库、不产生披露记录、不写下载文件。"""
    filters = _filters_from(data)
    dataset = db.get(Dataset, data.dataset_id)
    if not dataset:
        raise HTTPException(404, "源账批次不存在")
    transient = WeeklyReport(
        id=0, name="preview", dataset_id=dataset.id, filters=filters, group_by=data.group_by,
        audience=data.audience, enabled=False, next_run=0.0, interval_seconds=604800,
        status="queued", created_by=u.id, created_at=0.0,
    )
    return weekly.evaluate(db, transient)


@router.get("/deliveries")
def list_deliveries(db=Depends(get_db), u=Depends(current_user)):
    rows = db.scalars(select(WeeklyDelivery).order_by(WeeklyDelivery.id.desc()).limit(100)).all()
    return [weekly_delivery_dict(d) for d in rows]


@router.get("/deliveries/{id}/reasons")
def delivery_reasons(id: int, db=Depends(get_db), u=Depends(current_user)):
    d = db.get(WeeklyDelivery, id)
    if not d:
        raise HTTPException(404, "发布记录不存在")
    decision = d.decision or {}
    return {
        "id": d.id, "status": d.status, "audience": d.audience,
        "decision_reason": decision.get("decision_reason", ""),
        "reasons": decision.get("reasons", []),
        "rows": [
            {"row": r["label"], "suppressed": r["suppressed"], "reasons": r["reasons"]}
            for r in decision.get("rows", [])
        ],
    }


@router.post("/deliveries/{id}/recheck")
def recheck(id: int, db=Depends(get_db), u=Depends(admin_only)):
    client = Redis.from_url(REDIS_URL, socket_timeout=3)
    lock = client.lock(PUBLISH_LOCK, timeout=30, blocking_timeout=5)
    if not lock.acquire(blocking=True):
        raise HTTPException(409, "另一份周报正在分发，请稍后重试")
    try:
        d = weekly.release_held(db, id)
        record(db, u.email, "weekly.recheck", {"id": id, "status": d.status})
        db.commit()
        return weekly_delivery_dict(d)
    finally:
        try:
            lock.release()
        except Exception:
            pass
        client.close()


@router.get("/deliveries/{id}/download")
def download(id: int, db=Depends(get_db), u=Depends(current_user)):
    d = db.get(WeeklyDelivery, id)
    if not d:
        raise HTTPException(404, "发布记录不存在")
    if d.status != "published":
        raise HTTPException(409, "该周报未发布（挂起或拦截），没有可下载内容")
    path = weekly.artifact_path(d)
    if not path:
        raise HTTPException(404, "归档文件不存在，请重新触发发布")
    return FileResponse(path, filename=d.filename, media_type="text/csv; charset=utf-8")
