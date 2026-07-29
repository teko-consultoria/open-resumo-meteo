"""UTC forecast windows used by the three daily pêntadas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

WINDOW_LAYOUT: tuple[tuple[str, str, int, int], ...] = (
    ("p1", "1ª pêntada", 0, 5),
    ("p2", "2ª pêntada", 5, 10),
    ("p3", "3ª pêntada", 10, 14),
)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def require_aware_utc(value: datetime, *, field_name: str = "datetime") -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} deve ser datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} deve ter timezone explicito")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ForecastWindow:
    window_id: str
    label: str
    start_utc: datetime
    end_utc: datetime
    duration_days: int

    def __post_init__(self) -> None:
        start = require_aware_utc(self.start_utc, field_name="start_utc")
        end = require_aware_utc(self.end_utc, field_name="end_utc")
        object.__setattr__(self, "start_utc", start)
        object.__setattr__(self, "end_utc", end)
        if self.window_id not in {item[0] for item in WINDOW_LAYOUT}:
            raise ValueError(f"ID de janela desconhecido: {self.window_id}")
        if end <= start:
            raise ValueError("end_utc deve ser posterior a start_utc")
        if end - start != timedelta(days=self.duration_days):
            raise ValueError("duration_days nao corresponde aos limites UTC")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.window_id,
            "label": self.label,
            "start_utc": _utc_iso(self.start_utc),
            "end_utc": _utc_iso(self.end_utc),
            "duration_days": self.duration_days,
        }

def forecast_anchor_utc(issue_date: date, *, boundary_hour_utc: int = 0) -> datetime:
    if isinstance(issue_date, datetime):
        raise TypeError("issue_date deve ser date; use datetime diretamente em build_forecast_windows")
    if not isinstance(issue_date, date):
        raise TypeError("issue_date deve ser date")
    if not 0 <= boundary_hour_utc <= 23:
        raise ValueError("boundary_hour_utc deve estar entre 0 e 23")
    return datetime.combine(issue_date, time(hour=boundary_hour_utc), tzinfo=UTC)


def build_forecast_windows(
    anchor: date | datetime,
    *,
    boundary_hour_utc: int = 0,
) -> tuple[ForecastWindow, ForecastWindow, ForecastWindow]:
    """Build exactly three consecutive semi-open windows.

    A ``date`` is anchored at 00 UTC by default so the windows match the
    authorized 00z model run and its 0–120/120–240/240–336 h accumulations.
    An aware ``datetime`` is an
    explicit boundary and is preserved after conversion to UTC; naive values
    are rejected instead of being interpreted in the server's local timezone.
    """

    if isinstance(anchor, datetime):
        start = require_aware_utc(anchor, field_name="anchor")
    elif isinstance(anchor, date):
        start = forecast_anchor_utc(anchor, boundary_hour_utc=boundary_hour_utc)
    else:
        raise TypeError("anchor deve ser date ou datetime com timezone")

    windows = tuple(
        ForecastWindow(
            window_id=window_id,
            label=label,
            start_utc=start + timedelta(days=start_day),
            end_utc=start + timedelta(days=end_day),
            duration_days=end_day - start_day,
        )
        for window_id, label, start_day, end_day in WINDOW_LAYOUT
    )
    return windows  # type: ignore[return-value]
