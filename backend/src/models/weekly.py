from sqlalchemy import String, JSON, Double, ForeignKey, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class WeeklyReport(Base):
    """经营周报分发计划（同批源账可换筛选建立多份，按受众做披露控制）。"""
    __tablename__ = 'weekly_reports'
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'))
    # 筛选条件与口径：channel 白名单、起止日期、分组方式（channel_day/channel/week/channel_week）
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    group_by: Mapped[str] = mapped_column(String(24), default='channel_day')
    audience: Mapped[str] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run: Mapped[float] = mapped_column(Double)
    interval_seconds: Mapped[int] = mapped_column(default=604800)
    # 最近一次评估快照（含遮挡与原因），供页面试看与拦截原因查询
    last_decision: Mapped[dict] = mapped_column(JSON, default=dict)
    # queued 待评估 / held 并发挂起待重审 / blocked 拦截 / published 已发布
    status: Mapped[str] = mapped_column(String(20), default='queued')
    created_by: Mapped[int] = mapped_column(ForeignKey('users.id'))
    created_at: Mapped[float] = mapped_column(Double)


class WeeklyDelivery(Base):
    """周报发布记录。重试复用同一条记录（idempotency_key 唯一），不新增披露。"""
    __tablename__ = 'weekly_deliveries'
    id: Mapped[int] = mapped_column(primary_key=True)
    weekly_report_id: Mapped[int] = mapped_column(ForeignKey('weekly_reports.id'))
    audience: Mapped[str] = mapped_column(String(120))
    # 同一发布批次的幂等键：report_id + 计划发布时间窗；恢复重试命中即复用
    idempotency_key: Mapped[str] = mapped_column(String(120), unique=True)
    status: Mapped[str] = mapped_column(String(20))  # published / held / blocked
    filename: Mapped[str] = mapped_column(String(160), default='')
    checksum: Mapped[str] = mapped_column(String(64), default='')
    # 实际进入下载包的可见向量（写入同受众披露台账）与逐行遮挡原因
    released_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    decision: Mapped[dict] = mapped_column(JSON, default=dict)
    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)
