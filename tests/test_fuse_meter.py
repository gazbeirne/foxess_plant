"""Fuse meter readings -> long-term statistics (fuse_meter.py), from the page shape the Fuse web app returned on 14 Oct 2026.

Only real readings (ACTUAL) are written, an hour only when both its half hours are real, append-only from the last
stored hour so re-fetching can't count anything twice.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from custom_components.foxess_plant import fuse_meter
from custom_components.foxess_plant.coordinator import FoxessPlantCoordinator
from custom_components.foxess_plant.fuse_parse import SUPPLY_EXPORT, UK_TZ

PREMISES = "11111111-1111-4111-8111-111111111111"
EXPORT_FID = "22222222-2222-4222-8222-222222222222"


def _slot(start_utc: datetime, kwh, status="ACTUAL"):
    """Like Fuse's: utc_naive is the half hour's END in UTC; start/end local carry their offset."""
    local = start_utc.astimezone(UK_TZ)
    return {
        "meter_serial_number": "00X0000000",
        "meter_units": None,
        "status": status,
        "tpr_customer_name": None,
        "reading": {"value": f"{kwh:.3f}", "unit": "kWh"} if kwh is not None else None,
        "utc_naive": (start_utc + timedelta(minutes=30)).replace(tzinfo=None).isoformat(timespec="microseconds"),
        "start_time_local": local.isoformat(),
        "end_time_local": (start_utc + timedelta(minutes=30)).astimezone(UK_TZ).isoformat(),
    }


def _page(days: dict[str, list[dict]]) -> str:
    hh = {"days": [{"readings": r, "date_uk": d} for d, r in days.items()], "counts": {"total": 0}}
    pagination = {"current_index": {"year": 2026, "month": 10}}
    return '0:{"b":"x"}\n2:["$","$Ld",null,{"initialPagination":%s,"initialHhReadings":%s}]\n' % (
        json.dumps(pagination),
        json.dumps(hh),
    )


def _day(day: date, kwh_per_slot=0.1, status_from=None):
    """All half hours of a UK day; from slot index status_from on they're EMPTY (not read yet)."""
    start = datetime(day.year, day.month, day.day, tzinfo=UK_TZ).astimezone(UTC)
    end = (datetime(day.year, day.month, day.day, tzinfo=UK_TZ) + timedelta(days=1)).astimezone(UTC)
    slots, t, i = [], start, 0
    while t < (datetime.combine(day + timedelta(days=1), datetime.min.time(), UK_TZ)).astimezone(UTC):
        late = status_from is not None and i >= status_from
        slots.append(_slot(t, None if late else kwh_per_slot, "EMPTY" if late else "ACTUAL"))
        t += timedelta(minutes=30)
        i += 1
    assert t == end
    return slots


def test_parse_reads_utc_starts_and_values() -> None:
    slots = fuse_meter.parse_hh_page(_page({"2026-10-13": _day(date(2026, 10, 13), 0.25)}))
    assert len(slots) == 48
    assert slots[0]["start"] == datetime(2026, 10, 12, 23, 0, tzinfo=UTC)  # 00:00 BST, though utc_naive says 23:30
    assert {s["kwh"] for s in slots} == {0.25} and {s["status"] for s in slots} == {"ACTUAL"}


def test_without_local_times_utc_naive_is_taken_as_the_end() -> None:
    slot = _slot(datetime(2026, 10, 12, 23, 0, tzinfo=UTC), 0.1)
    del slot["start_time_local"]
    assert fuse_meter.parse_hh_page(_page({"2026-10-13": [slot]}))[0]["start"] == datetime(2026, 10, 12, 23, 0, tzinfo=UTC)


def test_only_hours_with_two_real_half_hours_are_written() -> None:
    day = date(2026, 10, 14)
    slots = fuse_meter.parse_hh_page(_page({"2026-10-14": _day(day, 0.1, status_from=5)}))
    # 5 real half hours: 00:00-02:30 local -> two complete hours; 02:00 has only one real half
    now = datetime(2026, 10, 14, 7, 0, tzinfo=UTC)
    hours = fuse_meter.hours_to_write(slots, after=None, now=now)
    assert [h.astimezone(UK_TZ).strftime("%H:%M") for h, _ in hours] == ["00:00", "01:00"]
    assert all(kwh == 0.2 for _h, kwh in hours)


def test_estimates_are_never_written() -> None:
    day = _day(date(2026, 10, 14), 0.1)
    day[2]["status"] = "ESTIMATED"  # 01:00 local
    slots = fuse_meter.parse_hh_page(_page({"2026-10-14": day}))
    hours = fuse_meter.hours_to_write(slots, after=None, now=datetime(2026, 10, 14, 23, 0, tzinfo=UTC))
    assert len(hours) == 1  # stops at 01:00 and waits for the real reading


def test_append_only_from_the_last_stored_hour() -> None:
    slots = fuse_meter.parse_hh_page(_page({"2026-10-13": _day(date(2026, 10, 13), 0.1)}))
    last = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    hours = fuse_meter.hours_to_write(slots, after=last, now=datetime(2026, 10, 14, 9, 0, tzinfo=UTC))
    assert hours[0][0] == datetime(2026, 10, 13, 11, 0, tzinfo=UTC) and len(hours) == 12
    rows = fuse_meter.statistics_rows(hours, start_sum=100.0)
    assert rows[0] == {"start": hours[0][0], "state": 0.2, "sum": 100.2} and rows[-1]["sum"] == pytest.approx(102.4)


def test_a_reading_missing_for_three_days_is_skipped() -> None:
    day = _day(date(2026, 10, 1), 0.1)
    day[4]["status"] = "ESTIMATED"
    slots = fuse_meter.parse_hh_page(_page({"2026-10-01": day}))
    hours = fuse_meter.hours_to_write(slots, after=None, now=datetime(2026, 10, 14, 9, 0, tzinfo=UTC))
    assert len(hours) == 23  # the gap hour is left out, the rest carry on


def test_clock_change_day_has_25_hours() -> None:
    slots = fuse_meter.parse_hh_page(_page({"2026-10-25": _day(date(2026, 10, 25), 0.1)}))
    assert len(slots) == 50
    hours = fuse_meter.hours_to_write(slots, after=None, now=datetime(2026, 10, 27, tzinfo=UTC))
    assert len(hours) == 25 and fuse_meter.day_total(slots, date(2026, 10, 25)) == 5.0


def test_day_total_only_when_the_whole_day_is_real() -> None:
    slots = fuse_meter.parse_hh_page(
        _page({"2026-10-13": _day(date(2026, 10, 13), 0.25), "2026-10-14": _day(date(2026, 10, 14), 0.1, 5)})
    )
    assert fuse_meter.day_total(slots, date(2026, 10, 13)) == 12.0
    assert fuse_meter.day_total(slots, date(2026, 10, 14)) is None
    assert fuse_meter.data_up_to(slots) == datetime(2026, 10, 14, 1, 30, tzinfo=UTC)  # 02:30 BST


def test_months_to_fetch() -> None:
    today = date(2026, 10, 14)
    assert fuse_meter.months_to_fetch(today, None) == [(2026, 7), (2026, 8), (2026, 9), (2026, 10)]
    assert fuse_meter.months_to_fetch(today, datetime(2026, 10, 13, 22, tzinfo=UTC)) == [(2026, 10)]
    assert fuse_meter.months_to_fetch(date(2026, 11, 1), datetime(2026, 10, 31, 20, tzinfo=UTC)) == [(2026, 10), (2026, 11)]
    assert fuse_meter.months_to_fetch(date(2027, 1, 2), datetime(2026, 12, 31, 23, 30, tzinfo=UTC)) == [(2026, 12), (2027, 1)]


def test_statistic_ids_are_valid() -> None:
    from homeassistant.components.recorder.statistics import valid_statistic_id

    sid = fuse_meter.statistic_id("01JABCDEF0123456789XYZ", SUPPLY_EXPORT)
    assert valid_statistic_id(sid) and fuse_meter.statistic_name(SUPPLY_EXPORT) == "Fuse Grid Export"


# --- The coordinator run (fake web client and recorder) ---------------------------------------------------------


class _Client:
    def __init__(self, pages):
        self.pages = pages
        self.asked = []

    async def hh_readings_page(self, premises_id, supply_fid, *, year, month):
        self.asked.append((year, month))
        return self.pages.get((year, month), _page({}))


class _Fake:
    def __init__(self, client):
        self.hass = SimpleNamespace(config=SimpleNamespace(components={"recorder"}))
        self.config_entry = SimpleNamespace(entry_id="ENTRY1")
        self._fuse_state = {
            "cookies": {"app-auth": "a"},
            "tariffs": {SUPPLY_EXPORT: {"supply_type": SUPPLY_EXPORT, "supply_fid": EXPORT_FID, "premises_id": PREMISES}},
        }
        self._fuse_meter_sensor = None
        self._client = client
        self.saved = 0

    def _fuse_in_use(self):
        return True

    def _fuse_meter_targets(self):
        return [self._fuse_state["tariffs"][SUPPLY_EXPORT]]

    def _fuse_client(self):
        return self._client

    async def _async_save_fuse(self):
        self.saved += 1

    async def _async_refresh_fuse(self, *, force=False):
        raise AssertionError("not needed: the property id is saved")

    def _fuse_signin_notice(self, message):
        pass


@pytest.mark.asyncio
async def test_run_writes_new_confirmed_hours_and_remembers_the_state() -> None:
    now = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)
    client = _Client({(2026, 10): _page({"2026-10-13": _day(date(2026, 10, 13), 0.25), "2026-10-14": _day(date(2026, 10, 14), 0.1, 5)})})
    fake = _Fake(client)
    written = []

    async def last(_hass, _sid):
        return datetime(2026, 10, 13, 20, 0, tzinfo=UTC), 50.0  # stored up to 21:00 BST on the 13th

    with (
        patch.object(fuse_meter, "async_last_statistic", last),
        patch.object(fuse_meter, "async_write_statistics", lambda h, k, sid, rows: written.append((k, sid, rows))),
        patch("homeassistant.util.dt.utcnow", return_value=now),
    ):
        await FoxessPlantCoordinator._async_refresh_fuse_meter_locked(fake, force=True)
    assert client.asked == [(2026, 10)]
    kind, sid, rows = written[0]
    assert kind == SUPPLY_EXPORT and sid == "foxess_plant:fuse_grid_export_entry1"
    # 21:00-23:00 BST on the 13th (2 hours), then 00:00 and 01:00 BST on the 14th
    assert [r["start"] for r in rows] == [datetime(2026, 10, 13, h, 0, tzinfo=UTC) for h in (21, 22)] + [
        datetime(2026, 10, 13, 23, 0, tzinfo=UTC),
        datetime(2026, 10, 14, 0, 0, tzinfo=UTC),
    ]
    assert rows[-1]["sum"] == pytest.approx(50.0 + 0.5 * 2 + 0.2 * 2)
    info = fake._fuse_state["meter"][SUPPLY_EXPORT]
    assert info["yesterday_kwh"] == 12.0 and info["hours_written"] == 4 and fake.saved == 1
    assert fake._fuse_state["meter"]["last_error"] is None


@pytest.mark.asyncio
async def test_first_run_back_fills_three_months_and_tolerates_months_before_the_supply() -> None:
    now = datetime(2026, 10, 14, 6, 0, tzinfo=UTC)
    client = _Client({(2026, 10): _page({"2026-10-01": _day(date(2026, 10, 1), 0.1)}), (2026, 7): "no readings here"})
    fake = _Fake(client)
    written = []

    async def last(_hass, _sid):
        return None, 0.0

    with (
        patch.object(fuse_meter, "async_last_statistic", last),
        patch.object(fuse_meter, "async_write_statistics", lambda h, k, sid, rows: written.append(rows)),
        patch("homeassistant.util.dt.utcnow", return_value=now),
        patch("asyncio.sleep", return_value=None),
    ):
        await FoxessPlantCoordinator._async_refresh_fuse_meter_locked(fake, force=True)
    assert client.asked == [(2026, 7), (2026, 8), (2026, 9), (2026, 10)]
    assert len(written[0]) == 24 and written[0][-1]["sum"] == pytest.approx(4.8)


@pytest.mark.asyncio
async def test_runs_are_spaced_and_wait_for_sign_in() -> None:
    fake = _Fake(_Client({}))
    fake._fuse_state["meter"] = {"last_attempt_at": datetime.now(UTC).isoformat()}
    with patch.object(fuse_meter, "async_last_statistic", side_effect=AssertionError("too soon")):
        await FoxessPlantCoordinator._async_refresh_fuse_meter_locked(fake, force=False)
        fake._fuse_state["auth_failed"] = True
        await FoxessPlantCoordinator._async_refresh_fuse_meter_locked(fake, force=True)
    assert fake.saved == 0
