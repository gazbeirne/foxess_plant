"""Fuse smart-meter readings into Home Assistant long-term statistics.

Fuse's web app shows the meter's half-hourly readings on ``/app/premises/{p}/supply/{s}/meter/hh-readings?month&year``
(React Server Components payload, ``initialHhReadings.days[].readings[]``). Each half hour has a status: ACTUAL (the
meter's real reading), ESTIMATED / FILLED (Fuse's guesses) or EMPTY. Real readings arrive within a few hours.

A sensor's history is written at the moment its state changes, so readings that arrive hours late can't go into a
sensor. They go into long-term statistics instead (``async_add_external_statistics``), which take past timestamps and
are what the Energy dashboard reads. Statistics are hourly, so each hour is its two half hours added up, and an hour
is written only when both are ACTUAL. Writing is append-only from the last stored hour: re-fetching never counts
anything twice. An hour still unconfirmed after GIVE_UP is skipped so one missing reading can't hold everything up.

Gas is read too when Fuse supplies it, so a Fuse customer needs no second integration; its unit (kWh or m³) is
taken from the readings. Only supplies with a current Fuse contract are read (onboarding / cancelled ones aren't).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .fuse_parse import SUPPLY_EXPORT, SUPPLY_GAS, SUPPLY_IMPORT, UK_TZ, FuseParseError, rsc_value

ACTUAL = "ACTUAL"
GIVE_UP = timedelta(days=3)
FETCH_EVERY = timedelta(hours=6)
RETRY_AFTER = timedelta(hours=1)
BACKFILL_MONTHS = 3
_IDS = {SUPPLY_IMPORT: "grid_import", SUPPLY_EXPORT: "grid_export", SUPPLY_GAS: "gas"}
_NAMES = {SUPPLY_IMPORT: "Fuse Grid Import", SUPPLY_EXPORT: "Fuse Grid Export", SUPPLY_GAS: "Fuse Gas"}
KWH = "kWh"
M3 = "m³"


def statistic_id(entry_id: str, supply_type: str) -> str:
    return f"foxess_plant:fuse_{_IDS[supply_type]}_{entry_id.lower()}"


def statistic_name(supply_type: str) -> str:
    return _NAMES[supply_type]


def reading_unit(slots: list[dict[str, Any]]) -> str:
    """kWh, or m³ when Fuse reports a gas meter's volume (the Energy dashboard's Gas section takes either).

    Only electricity readings have been seen (kWh); the gas unit is read from the readings, not assumed.
    """
    units = {str(s.get("unit") or "").strip().lower() for s in slots if s.get("kwh") is not None}
    return M3 if units & {"m3", "m³", "cubic_meters", "cubic metres", "cubic meters"} else KWH


def parse_hh_page(text: str) -> list[dict[str, Any]]:
    """Half-hour slots from a meter page: [{start (UTC), kwh, status}], in time order."""
    hh = rsc_value(text, "initialHhReadings")
    if not isinstance(hh, dict) or not isinstance(hh.get("days"), list):
        raise FuseParseError("no half-hourly readings on Fuse's meter page")
    slots = []
    for day in hh["days"]:
        for r in (day or {}).get("readings") or []:
            start = _slot_start(r)
            if start is None:
                continue
            reading = r.get("reading") or {}
            value = reading.get("value")
            try:
                kwh = float(value) if value not in (None, "") else None  # "kwh" holds m³ for a gas meter in m³
            except (TypeError, ValueError):
                kwh = None
            slots.append({"start": start, "kwh": kwh, "status": str(r.get("status") or ""), "unit": reading.get("unit")})
    slots.sort(key=lambda s: s["start"])
    return slots


def _slot_start(reading: dict[str, Any]) -> datetime | None:
    """A half hour's start in UTC. ``start_time_local`` carries its offset; ``utc_naive`` is the END in UTC
    (00:00-00:30 BST has utc_naive 23:30), so it's only the fallback."""
    try:
        return datetime.fromisoformat(str(reading["start_time_local"])).astimezone(UTC)
    except (KeyError, TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(str(reading["utc_naive"])).replace(tzinfo=UTC) - timedelta(minutes=30)
    except (KeyError, TypeError, ValueError):
        return None


def _hours(slots: list[dict[str, Any]]) -> dict[datetime, list[dict[str, Any]]]:
    by_hour: dict[datetime, list[dict[str, Any]]] = defaultdict(list)
    for slot in slots:
        by_hour[slot["start"].replace(minute=0, second=0, microsecond=0)].append(slot)
    return by_hour


def _confirmed(halves: list[dict[str, Any]]) -> float | None:
    """kWh for an hour whose two half hours are both real readings, else None."""
    starts = {h["start"] for h in halves if h["status"] == ACTUAL and h["kwh"] is not None}
    if len(starts) != 2:
        return None
    return round(sum(h["kwh"] for h in halves if h["status"] == ACTUAL and h["kwh"] is not None), 4)


def hours_to_write(
    slots: list[dict[str, Any]], *, after: datetime | None, now: datetime
) -> list[tuple[datetime, float]]:
    """Confirmed hours after ``after``, in order, stopping at the first one still waiting for real readings."""
    out: list[tuple[datetime, float]] = []
    for hour, halves in sorted(_hours(slots).items()):
        if after is not None and hour <= after:
            continue
        kwh = _confirmed(halves)
        if kwh is not None:
            out.append((hour, kwh))
        elif hour + timedelta(hours=1) <= now - GIVE_UP:
            continue  # never confirmed: leave a gap rather than wait forever
        else:
            break
    return out


def data_up_to(slots: list[dict[str, Any]]) -> datetime | None:
    """End of the newest real half-hour reading."""
    real = [s["start"] for s in slots if s["status"] == ACTUAL and s["kwh"] is not None]
    return max(real) + timedelta(minutes=30) if real else None


def day_total(slots: list[dict[str, Any]], day: date) -> float | None:
    """kWh for a UK day, only if all its half hours are real readings."""
    todays = [s for s in slots if s["start"].astimezone(UK_TZ).date() == day]
    if not todays or any(s["status"] != ACTUAL or s["kwh"] is None for s in todays):
        return None
    return round(sum(s["kwh"] for s in todays), 3)


def months_to_fetch(today: date, last_hour: datetime | None) -> list[tuple[int, int]]:
    """(year, month) pages to read: three months back the first time, else from the last stored hour's month."""
    if last_hour is None:
        start = (today.year * 12 + today.month - 1) - BACKFILL_MONTHS
    else:
        last = last_hour.astimezone(UK_TZ)
        start = max(last.year * 12 + last.month - 1, today.year * 12 + today.month - 1 - BACKFILL_MONTHS)
    end = today.year * 12 + today.month - 1
    return [(i // 12, i % 12 + 1) for i in range(start, end + 1)]


def statistics_rows(hours: list[tuple[datetime, float]], *, start_sum: float) -> list[dict[str, Any]]:
    """StatisticData rows: state = the hour's kWh, sum = running total."""
    total = start_sum
    rows = []
    for hour, kwh in hours:
        total = round(total + kwh, 4)
        rows.append({"start": hour, "state": kwh, "sum": total})
    return rows


# --- Recorder (thin wrappers, patched in tests) ------------------------------------------------------------------


async def async_last_statistic(hass: Any, stat_id: str) -> tuple[datetime | None, float]:
    """(start of the last stored hour, its running total), or (None, 0.0)."""
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import get_last_statistics

    last = await get_instance(hass).async_add_executor_job(get_last_statistics, hass, 1, stat_id, True, {"sum"})
    rows = (last or {}).get(stat_id) or []
    if not rows:
        return None, 0.0
    start = rows[0]["start"]
    when = datetime.fromtimestamp(start, UTC) if isinstance(start, (int, float)) else start
    return when, float(rows[0].get("sum") or 0.0)


def async_write_statistics(
    hass: Any, supply_type: str, stat_id: str, rows: list[dict[str, Any]], *, unit: str = KWH
) -> None:
    from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
    from homeassistant.components.recorder.statistics import async_add_external_statistics
    from homeassistant.const import UnitOfEnergy, UnitOfVolume
    from homeassistant.util.unit_conversion import EnergyConverter, VolumeConverter

    volume = unit == M3
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=statistic_name(supply_type),
        source="foxess_plant",
        statistic_id=stat_id,
        unit_class=VolumeConverter.UNIT_CLASS if volume else EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfVolume.CUBIC_METERS if volume else UnitOfEnergy.KILO_WATT_HOUR,
    )
    async_add_external_statistics(hass, metadata, [StatisticData(**row) for row in rows])
