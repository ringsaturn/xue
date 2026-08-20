from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True, order=True)
class GfsRun:
    time: datetime

    def __post_init__(self) -> None:
        if self.time.tzinfo is None:
            raise ValueError("GFS run time must include a timezone")
        normalized = self.time.astimezone(UTC)
        if normalized.minute or normalized.second or normalized.microsecond:
            raise ValueError("GFS run time must be aligned to an hour")
        if normalized.hour not in (0, 6, 12, 18):
            raise ValueError("GFS run hour must be 00, 06, 12, or 18 UTC")
        object.__setattr__(self, "time", normalized)

    @property
    def id(self) -> str:
        return self.time.strftime("%Y%m%d%H")

    @property
    def date(self) -> str:
        return self.time.strftime("%Y%m%d")

    @property
    def cycle(self) -> str:
        return self.time.strftime("%H")

    def valid_time(self, forecast_hour: int) -> datetime:
        return self.time + timedelta(hours=forecast_hour)


@dataclass(frozen=True)
class GribFrame:
    path: Path
    band: int
    variable_id: str
    run_time: datetime
    valid_time: datetime
    forecast_hour: int
    unit: str
