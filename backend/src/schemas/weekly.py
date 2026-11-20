from pydantic import BaseModel, Field


class WeeklyReportInput(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    dataset_id: int
    audience: str = Field(default="经营周会 · 本地收件箱", min_length=2, max_length=120)
    group_by: str = Field(default="channel_day")
    channels: list[str] = Field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
    interval_seconds: int = Field(default=604800, ge=60, le=2592000)
    delay_seconds: int = Field(default=0, ge=0, le=2592000)


class WeeklyPreviewInput(BaseModel):
    dataset_id: int
    audience: str = Field(default="经营周会 · 本地收件箱", min_length=2, max_length=120)
    group_by: str = Field(default="channel_day")
    channels: list[str] = Field(default_factory=list)
    date_from: str | None = None
    date_to: str | None = None
