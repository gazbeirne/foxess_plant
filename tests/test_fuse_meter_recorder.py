"""Fuse meter statistics through a real Home Assistant recorder (the version the user runs)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.foxess_plant import fuse_meter
from custom_components.foxess_plant.fuse_parse import SUPPLY_EXPORT


@pytest.mark.asyncio
async def test_statistics_are_written_and_continue(recorder_mock, hass) -> None:
    sid = fuse_meter.statistic_id("ENTRY1", SUPPLY_EXPORT)
    assert await fuse_meter.async_last_statistic(hass, sid) == (None, 0.0)
    start = datetime(2026, 10, 13, 21, 0, tzinfo=UTC)
    hours = [(start + timedelta(hours=i), 0.5) for i in range(3)]
    fuse_meter.async_write_statistics(hass, SUPPLY_EXPORT, sid, fuse_meter.statistics_rows(hours, start_sum=0.0))
    await async_wait_recording_done(hass)
    last_hour, last_sum = await fuse_meter.async_last_statistic(hass, sid)
    assert last_hour == start + timedelta(hours=2) and last_sum == pytest.approx(1.5)

    # The next run carries on from there; writing the same hours again changes nothing
    more = [(last_hour + timedelta(hours=1), 0.25)]
    fuse_meter.async_write_statistics(hass, SUPPLY_EXPORT, sid, fuse_meter.statistics_rows(more, start_sum=last_sum))
    fuse_meter.async_write_statistics(hass, SUPPLY_EXPORT, sid, fuse_meter.statistics_rows(more, start_sum=last_sum))
    await async_wait_recording_done(hass)
    assert await fuse_meter.async_last_statistic(hass, sid) == (more[0][0], pytest.approx(1.75))

    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import list_statistic_ids

    meta = await get_instance(hass).async_add_executor_job(list_statistic_ids, hass, {sid})
    assert meta[0]["name"] == "Fuse Grid Export" and meta[0]["statistics_unit_of_measurement"] == "kWh"


@pytest.mark.asyncio
async def test_gas_in_cubic_metres(recorder_mock, hass) -> None:
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import list_statistic_ids

    from custom_components.foxess_plant.fuse_parse import SUPPLY_GAS

    sid = fuse_meter.statistic_id("ENTRY1", SUPPLY_GAS)
    start = datetime(2026, 10, 13, 6, 0, tzinfo=UTC)
    rows = fuse_meter.statistics_rows([(start, 0.31), (start + timedelta(hours=1), 0.12)], start_sum=0.0)
    fuse_meter.async_write_statistics(hass, SUPPLY_GAS, sid, rows, unit=fuse_meter.M3)
    await async_wait_recording_done(hass)
    meta = await get_instance(hass).async_add_executor_job(list_statistic_ids, hass, {sid})
    assert meta[0]["name"] == "Fuse Gas" and meta[0]["statistics_unit_of_measurement"] == "m³"
    assert await fuse_meter.async_last_statistic(hass, sid) == (start + timedelta(hours=1), pytest.approx(0.43))
