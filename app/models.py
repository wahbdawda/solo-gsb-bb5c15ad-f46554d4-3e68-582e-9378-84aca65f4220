"""Pydantic 请求/响应模型。时间戳对外同时提供 Unix 秒与 ISO 8601 两种形式。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field


def epoch_to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def parse_timestamp(value: Any) -> float:
    """接受 Unix 秒（数字或数字字符串）或 ISO 8601 字符串，统一为 Unix 秒。"""
    if isinstance(value, bool):
        raise ValueError("invalid timestamp")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                raise ValueError(f"unparseable timestamp: {value!r}") from None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    raise ValueError(f"unsupported timestamp type: {type(value).__name__}")


Timestamp = Annotated[float, BeforeValidator(parse_timestamp)]


# ---------- 请求 ----------

class ObservationIn(BaseModel):
    reference: float = Field(description="参考值（真值）")
    tolerance: float = Field(ge=0, description="容差：读数偏离组中位数超过该值即判为异常")
    readings: list[float] = Field(min_length=3, description="同一时点的读数，至少 3 次")
    timestamp: Timestamp = Field(
        default_factory=lambda: datetime.now(timezone.utc).timestamp(),
        description="观测时点：Unix 秒或 ISO 8601，缺省为当前时间",
    )


class CorrectionIn(BaseModel):
    timestamp: Timestamp = Field(description="待校正读数的时点：Unix 秒或 ISO 8601")
    reading: float = Field(description="待校正的原始读数")


class ObservationReviseIn(ObservationIn):
    revision: int = Field(ge=1, description="客户端持有的当前版本号；与服务端不一致时拒绝修订（409）")


# ---------- 响应 ----------

class ReadingOut(BaseModel):
    value: float
    is_outlier: bool


class ObservationOut(BaseModel):
    id: int
    revision: int
    timestamp: float
    timestamp_iso: str
    reference: float
    tolerance: float
    group_median: float
    bias: float
    readings: list[ReadingOut]
    outliers: list[float]


class RevisionOut(BaseModel):
    revision: int = Field(description="该快照对应的版本（修订前版本）")
    changed_at: float
    changed_at_iso: str
    old_timestamp: float
    old_timestamp_iso: str
    old_reference: float
    old_tolerance: float
    old_bias: float
    old_readings: list[float] = Field(description="修订前原始读数（保持提交顺序）")


class IntervalOut(BaseModel):
    t_start: float
    t_end: float
    start_iso: str
    end_iso: str
    bias_start: float
    bias_end: float


class EndpointOut(BaseModel):
    observation_id: int
    timestamp: float
    timestamp_iso: str
    bias: float
    outliers: list[float]


class CorrectionOut(BaseModel):
    timestamp: float
    timestamp_iso: str
    reading: float
    bias: float
    corrected: float
    interval: IntervalOut | None = Field(description="所用插值区间；恰落在观测点上时为 null")
    endpoints: list[EndpointOut] = Field(description="区间端点观测及其异常读数明细")
