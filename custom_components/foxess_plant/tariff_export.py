"""Export pricing, chosen separately from import.

Import comes from a supplier API (Octopus / E.ON Next), a Home Assistant sensor, or the band schedule. Export can
come from the same place ("same"), the band schedule ("manual", e.g. a fixed SEG typed in), a Home Assistant
sensor ("entity"), or a *different* supplier's API ("octopus" / "eon_next" / "fuse", with its own login) — e.g. E.ON
Next import with Octopus Outgoing or Fuse export.

Every consumer (SmartCharge, savings, tariff sensors) reads export from the one rate cache; the coordinator lays
the chosen export rows over it (``_apply_manual_export_to_cache``), so nothing else needs to know where they
came from.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.util import dt as dt_util

SAME = "same"
MANUAL = "manual"
ENTITY = "entity"
SUPPLIERS = ("octopus", "eon_next", "fuse")
PROVIDERS = (SAME, MANUAL, ENTITY, *SUPPLIERS)
SUPPLIER_LABELS = {"octopus": "Octopus", "eon_next": "E.ON Next", "fuse": "Fuse"}

# A separate export supplier is polled at most hourly; a failure waits as long (the Solcast lesson)
REFRESH = timedelta(minutes=55)


def import_supplier(dynamic: Any) -> str | None:
    """The import supplier whose API also prices export under "same", if any."""
    if dynamic is None or not dynamic.enabled:
        return None
    provider = str(dynamic.provider or "octopus")
    return provider if provider in SUPPLIERS else None


def normalize_export_provider(raw: Any, *, dynamic: Any, export_source: str) -> str:
    """Validate the saved choice; migrate configs from before it existed.

    Before: one supplier did both, with ``export_from_api`` False meaning "export typed into the bands"; without a
    supplier, ``export_source`` said schedule or sensor.
    """
    value = str(raw or "").strip().lower()
    supplier = import_supplier(dynamic)
    if value not in PROVIDERS:
        if supplier and dynamic.source == "native":
            value = SAME if dynamic.export_from_api else MANUAL
        elif supplier:  # Octopus via its Home Assistant integration's sensors
            value = SAME
        else:
            value = ENTITY if export_source == "entity" else MANUAL
    if value in SUPPLIERS and value == supplier:
        value = SAME  # same supplier: one login does both
    if value == SAME and supplier is None:
        value = ENTITY if export_source == "entity" else MANUAL
    return value


def export_mode(tariff: Any) -> str:
    """"import" (from the import supplier), "manual", "entity" or "supplier"."""
    provider = getattr(tariff, "export_provider", SAME)
    if provider == SAME:
        return "import"
    if provider in SUPPLIERS:
        return "supplier"
    return provider


def export_label(tariff: Any) -> str:
    provider = getattr(tariff, "export_provider", SAME)
    if provider == SAME:
        return SUPPLIER_LABELS.get(import_supplier(tariff.dynamic) or "", "import supplier")
    return {MANUAL: "Manual Rates", ENTITY: "Home Assistant sensor"}.get(provider) or SUPPLIER_LABELS[provider]


def flat_rows(value_p: float, *, now=None, hours: int = 48) -> list[dict[str, Any]]:
    """Half-hour rows at one price from the last half hour onwards (a sensor that only gives today's price)."""
    now = now or dt_util.utcnow()
    start = now.replace(minute=0 if now.minute < 30 else 30, second=0, microsecond=0)
    return [
        {
            "valid_from": (start + timedelta(minutes=30 * i)).isoformat(),
            "valid_to": (start + timedelta(minutes=30 * (i + 1))).isoformat(),
            "value_inc_vat": round(float(value_p), 4),
        }
        for i in range(hours * 2)
    ]


async def fetch_octopus_export(client: Any, *, account_number: str, export_mpan: str | None) -> dict[str, Any]:
    """Export rates only, from an Octopus account that may have no import meter (import is elsewhere)."""
    from .octopus_api import OctopusApiError
    from .octopus_tariff import (
        _iso_period,
        _pick_meter,
        classify_tariff_code,
        enrich_export_meters_via_graphql,
        find_product_for_tariff,
        is_variable_tariff_type,
        list_account_meters,
    )

    account = await client.get_account(account_number)
    _imports, exports = list_account_meters(account)
    exports = await enrich_export_meters_via_graphql(client, account_number, exports)
    if not exports:
        raise OctopusApiError("No export (Outgoing / SEG) meter on this Octopus account")
    meter = _pick_meter(exports, export_mpan, role="export", allow_first=True)
    if not meter.tariff_code:
        raise OctopusApiError(f"Export meter {meter.mpan} has no active tariff agreement")
    product = await find_product_for_tariff(client, meter.tariff_code)
    if product is None:
        raise OctopusApiError(f"Could not find the Octopus product for export tariff {meter.tariff_code}")
    tariff_type = classify_tariff_code(meter.tariff_code)
    local_now = dt_util.as_local(dt_util.utcnow())
    ahead = timedelta(hours=50) if is_variable_tariff_type(tariff_type) else timedelta(days=2)
    rates = await client.get_unit_rates(
        product,
        meter.tariff_code,
        period_from=_iso_period((local_now - timedelta(hours=2)).astimezone(dt_util.UTC)),
        period_to=_iso_period((local_now + ahead).astimezone(dt_util.UTC)),
    )
    if not rates:
        raise OctopusApiError(f"Export tariff {meter.tariff_code} returned no unit rates")
    return {
        "export_rates": rates,
        "export_tariff_code": meter.tariff_code,
        "export_mpan": meter.mpan,
        "export_meters": [
            {"mpan": m.mpan, "serial": m.serial, "tariff_code": m.tariff_code, "display_name": m.display_name}
            for m in exports
        ],
    }


async def fetch_eon_next_export(client: Any, *, account_number: str | None, export_mpan: str | None) -> dict[str, Any]:
    """Export rates only, from an E.ON Next login (import with another supplier)."""
    from .eon_next_api import EonNextApiError, discover_eon_next_meters, meter_list_for_cache
    from .eon_next_parse import pick_meter

    _imports, exports, _numbers = await discover_eon_next_meters(client, account_number=account_number)
    if not exports:
        raise EonNextApiError("No export meter on this E.ON Next login")
    try:
        meter = pick_meter(exports, export_mpan, role="export", preferred_account=account_number, allow_first=True)
    except ValueError as err:
        raise EonNextApiError(str(err)) from err
    if meter is None or not meter.get("tariff_code"):
        raise EonNextApiError("E.ON Next export meter has no active tariff agreement")
    now = dt_util.utcnow()
    midnight = dt_util.as_local(now).replace(hour=0, minute=0, second=0, microsecond=0)
    rates = await client.get_unit_rates(meter, dt_util.as_utc(midnight) - timedelta(hours=1), now + timedelta(days=2))
    if not rates:
        raise EonNextApiError(f"E.ON Next returned no export rates for {meter['tariff_code']}")
    return {
        "export_rates": rates,
        "export_tariff_code": meter["tariff_code"],
        "export_mpan": meter["mpan"],
        "export_meters": meter_list_for_cache(exports),
    }
