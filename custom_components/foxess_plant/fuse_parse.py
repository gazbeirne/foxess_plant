"""Fuse Energy tariffs from its web app (pure functions, no Home Assistant).

Fuse publishes no API. Its web app (www.fuseenergy.com/app) is a Next.js site: small JSON calls go through tRPC
(``/api/trpc/<procedure>``) and pages arrive as React Server Components payloads. The current tariff - unit rate per
register, the hours each rate applies, standing charge, contract dates - is only in the supply page's payload, as
``initialBill.bill_details.breakdowns[].element.per_billing_span[]``. The tariff-change page has the same tariff as
display text (``groups[].cards[].tariff.description_items``), used when there's no billing period yet.

Prices are pounds as strings. Export prices are negative (money to the customer); Fox Plant keeps export as a
positive p/kWh, so the sign is flipped for ELEC_EXPORT.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

FUSE_PROVIDER = "fuse"
SUPPLY_IMPORT = "ELEC_IMPORT"
SUPPLY_EXPORT = "ELEC_EXPORT"
SUPPLY_TYPES = (SUPPLY_IMPORT, SUPPLY_EXPORT)
SUPPLY_GAS = "GAS"  # meter readings only (fuse_meter.py); Fox Plant doesn't price gas
UK_TZ = ZoneInfo("Europe/London")
DAY_MINUTES = 24 * 60

# Background fetches: a good fetch is kept for a week (Fuse prices change at contract boundaries, which we know);
# a failed one waits an hour (the Solcast lesson).
REFETCH_AFTER = timedelta(days=7)
RETRY_AFTER = timedelta(hours=1)


class FuseParseError(ValueError):
    """Fuse answered, but not in a shape we understand."""


@dataclass
class FuseRate:
    name: str
    p_per_kwh: float
    # Local (UK) minutes from midnight, [start, end). None = the rest of the day.
    windows: list[tuple[int, int]] | None = None


@dataclass
class FuseTariff:
    supply_fid: str
    supply_type: str
    name: str
    rates: list[FuseRate]
    standing_p_per_day: float = 0.0
    tariff_id: str | None = None
    identifier: str | None = None  # MPAN
    premises_id: str | None = None  # for the meter-readings pages (fuse_meter.py)
    start_date: str | None = None  # YYYY-MM-DD (UK)
    end_date: str | None = None
    fixed: bool = False
    next_update_date: str | None = None  # when the price changes (e.g. the next price cap)
    next_update_reason: str | None = None
    next_prices_from: str | None = None  # when Fuse publishes the next prices
    source: str = "bill"  # "bill" or "cards"

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["rates"] = [
            {"name": r.name, "p_per_kwh": r.p_per_kwh, "windows": [list(w) for w in r.windows] if r.windows else None}
            for r in self.rates
        ]
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FuseTariff:
        rates = [
            FuseRate(
                name=str(r.get("name") or ""),
                p_per_kwh=float(r.get("p_per_kwh") or 0.0),
                windows=[(int(a), int(b)) for a, b in r["windows"]] if r.get("windows") else None,
            )
            for r in raw.get("rates") or []
        ]
        known = {f for f in cls.__dataclass_fields__ if f != "rates"}
        return cls(rates=rates, **{k: v for k, v in raw.items() if k in known})

    @property
    def multi_rate(self) -> bool:
        return len(self.rates) > 1


# --- Transport shapes -------------------------------------------------------------------------------------------


def rsc_value(text: str, key: str) -> Any:
    """The JSON value after the first ``"key":`` in a React Server Components payload (None if absent)."""
    needle = f'"{key}":'
    decoder = json.JSONDecoder()
    pos = text.find(needle)
    while pos >= 0:
        try:
            value, _end = decoder.raw_decode(text, pos + len(needle))
            return value
        except ValueError:
            pos = text.find(needle, pos + 1)
    return None


def trpc_error(payload: Any) -> str | None:
    """Fuse's reason when a tRPC call failed (tRPC answers many failures with HTTP 200 and the reason in the body)."""
    if isinstance(payload, list):
        return next(filter(None, map(trpc_error, payload)), None)
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if error is None:
        result = payload.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        inner = data.get("error") if isinstance(data, dict) else None
        return _error_text(inner) if inner else None
    return _error_text(error) or "unknown error"


def _error_text(error: Any) -> str | None:
    if isinstance(error, str):
        return error or None
    if not isinstance(error, dict):
        return None
    for key in ("errorCode", "message"):
        if error.get(key) not in (None, ""):
            return str(error[key])
    for key in ("json", "data"):
        if (nested := error.get(key)) is not None and (text := _error_text(nested)):
            return text
    return str(error["code"]) if error.get("code") not in (None, "") else None


def trpc_data(payload: Any) -> Any:
    """``result.data`` of a tRPC answer; raises FuseParseError with Fuse's reason otherwise."""
    if (reason := trpc_error(payload)) is not None:
        raise FuseParseError(reason)
    try:
        return payload["result"]["data"]
    except (KeyError, TypeError) as err:
        raise FuseParseError("unexpected answer from Fuse") from err


def normalize_phone(raw: str) -> str:
    """UK mobile in international form: '07700 900123' -> '+447700900123'."""
    digits = re.sub(r"[^\d+]", "", str(raw or ""))
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif digits.startswith("0"):
        digits = "+44" + digits[1:]
    elif digits and not digits.startswith("+"):
        digits = "+" + digits
    if not re.fullmatch(r"\+\d{10,15}", digits):
        raise FuseParseError("Enter the mobile number on your Fuse account in international format, e.g. +447700900123")
    return digits


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    return f"{phone[:3]}•••{phone[-3:]}" if len(phone) > 6 else "•••"


# --- Prices and time windows ------------------------------------------------------------------------------------


def money_p(amount: Any, *, supply_type: str) -> float:
    """Pounds (string) to p/kWh; export is flipped so money paid to the customer is positive."""
    try:
        pence = float(str(amount).replace("£", "").replace(",", "").strip()) * 100.0
    except (TypeError, ValueError) as err:
        raise FuseParseError(f"unreadable price {amount!r}") from err
    if supply_type == SUPPLY_EXPORT:
        pence = -pence
    return round(pence, 4) + 0.0  # + 0.0 turns -0.0 into 0.0


def _minutes(value: Any) -> int | None:
    """'07:00', '07:00:00', '2026-10-01T07:00:00', {'hour': 7, 'minute': 0} or 420 -> minutes from midnight."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if 0 <= value <= DAY_MINUTES else None
    if isinstance(value, dict):
        hour = value.get("hour", value.get("hours"))
        minute = value.get("minute", value.get("minutes", 0))
        try:
            return int(hour) * 60 + int(minute or 0)
        except (TypeError, ValueError):
            return None
    m = re.search(r"(\d{1,2}):(\d{2})", str(value or ""))
    if not m:
        return None
    total = int(m.group(1)) * 60 + int(m.group(2))
    return total if total <= DAY_MINUTES else None


_START_KEYS = ("start", "from", "start_time", "start_time_uk", "from_time", "start_uk", "begin")
_END_KEYS = ("end", "to", "end_time", "end_time_uk", "to_time", "end_uk", "until")


def parse_time_ranges(raw: Any) -> list[tuple[int, int]] | None:
    """Fuse's ``charge_time_ranges_uk`` as [start, end) minute windows; None when not given.

    Only single-rate tariffs have been seen (null). The shape for multi-rate tariffs is unknown, so this accepts the
    common forms and raises rather than guess when it can't read one. A window crossing midnight is split in two.
    """
    if raw in (None, "", []):
        return None
    if isinstance(raw, dict):
        for key in ("ranges", "time_ranges", "items", "values"):
            if isinstance(raw.get(key), list):
                return parse_time_ranges(raw[key])
        raw = [raw]
    if not isinstance(raw, list):
        raw = [raw]
    windows: list[tuple[int, int]] = []
    for item in raw:
        start = end = None
        if isinstance(item, dict):
            lowered = {str(k).lower(): v for k, v in item.items()}
            start = next((_minutes(lowered[k]) for k in _START_KEYS if k in lowered), None)
            end = next((_minutes(lowered[k]) for k in _END_KEYS if k in lowered), None)
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            start, end = _minutes(item[0]), _minutes(item[1])
        elif isinstance(item, str):
            times = re.findall(r"\d{1,2}:\d{2}", item)
            if len(times) == 2:
                start, end = _minutes(times[0]), _minutes(times[1])
        if start is None or end is None:
            raise FuseParseError(f"can't read Fuse's rate times {item!r}")
        if end == 0:
            end = DAY_MINUTES
        if end <= start:
            windows += [(start, DAY_MINUTES), (0, end)]
        else:
            windows.append((start, end))
    return [w for w in windows if w[1] > w[0]] or None


def check_rates(rates: list[FuseRate]) -> None:
    if not rates:
        raise FuseParseError("Fuse returned no unit rates")
    if len(rates) > 1 and sum(1 for r in rates if not r.windows) > 1:
        raise FuseParseError(
            "Fuse didn't say which hours each rate applies to (" + ", ".join(r.name for r in rates) + ")"
        )


# --- Tariff from the bill / the tariff cards --------------------------------------------------------------------


def _amount(obj: Any) -> Any:
    return obj.get("amount") if isinstance(obj, dict) else obj


def tariff_from_bill(bill: Any, supply_fid: str, *, today: date) -> FuseTariff | None:
    """The supply's tariff from the latest billing period in the bill (None if the bill has none yet)."""
    if not isinstance(bill, dict):
        return None
    details = bill.get("bill_details") or {}
    breakdown = next(
        (
            b
            for b in details.get("breakdowns") or []
            if isinstance(b, dict) and (b.get("element") or {}).get("supply_fid") == supply_fid
        ),
        None,
    )
    if breakdown is None:
        return None
    element = breakdown["element"]
    supply_type = str(breakdown.get("supply_type") or "")
    spans = [s for s in element.get("per_billing_span") or [] if isinstance(s, dict) and s.get("tariff_contract_details")]
    if not spans:
        return None
    today_s = today.isoformat()
    current = [s for s in spans if (s.get("billing_start_date_uk") or "") <= today_s]
    span = max(current or spans, key=lambda s: s.get("billing_start_date_uk") or "")
    contract = span["tariff_contract_details"]
    if span.get("energy_flow_direction") == "E":
        supply_type = SUPPLY_EXPORT
    elif span.get("energy_flow_direction") == "I" and not supply_type:
        supply_type = SUPPLY_IMPORT
    windows_by_rate = {
        str(c.get("rate_name") or ""): parse_time_ranges(c.get("charge_time_ranges_uk"))
        for c in span.get("per_rate_charges") or []
        if isinstance(c, dict)
    }
    rates = [
        FuseRate(
            name=str(r.get("rate_name") or "Rate"),
            p_per_kwh=money_p(_amount(r.get("price_per_kWh")), supply_type=supply_type),
            windows=windows_by_rate.get(str(r.get("rate_name") or "")),
        )
        for r in contract.get("unit_rates") or []
        if isinstance(r, dict)
    ]
    if not rates:  # older shape: prices only on the per-rate charges
        rates = [
            FuseRate(
                name=str(c.get("rate_name") or "Rate"),
                p_per_kwh=money_p(_amount(c.get("price_per_unit")), supply_type=supply_type),
                windows=windows_by_rate.get(str(c.get("rate_name") or "")),
            )
            for c in span.get("per_rate_charges") or []
            if isinstance(c, dict) and c.get("price_per_unit")
        ]
    check_rates(rates)
    standing = _amount(contract.get("standing_charge_per_day"))
    return FuseTariff(
        supply_fid=supply_fid,
        supply_type=supply_type,
        name=str(contract.get("name") or "Fuse tariff"),
        tariff_id=contract.get("tariff_id"),
        rates=rates,
        # Standing charge is a cost, never flipped
        standing_p_per_day=money_p(standing, supply_type=SUPPLY_IMPORT) if standing not in (None, "") else 0.0,
        start_date=contract.get("contract_start_date_uk"),
        end_date=contract.get("contract_end_date_uk"),
        fixed=bool(contract.get("fixed_tariff_details")),
        source="bill",
    )


def tariff_from_cards(groups: Any, supply_fid: str, supply_type: str) -> FuseTariff | None:
    """Single-rate tariff from the tariff-change page's "Your tariff" card (display text only)."""
    cards = [c for g in groups or [] if isinstance(g, dict) for c in g.get("cards") or [] if isinstance(c, dict)]
    card = next((c for c in cards if str(c.get("availability") or "").startswith("CURRENT")), None)
    if card is None:
        return None
    tariff = card.get("tariff") or {}
    unit = standing = None
    unit_names: list[str] = []
    for item in tariff.get("description_items") or []:
        value = (item or {}).get("value") or {}
        name = str(value.get("name") or "")
        lname = name.lower()
        if "unit rate" in lname:
            unit_names.append(name)
            unit = value.get("value")
        elif "standing" in lname:
            standing = value.get("value")
    if len(unit_names) != 1 or unit is None:
        return None  # multi-rate cards carry no machine-readable hours
    return FuseTariff(
        supply_fid=supply_fid,
        supply_type=supply_type,
        name=str(tariff.get("title") or "Fuse tariff"),
        tariff_id=tariff.get("tariff_id"),
        rates=[FuseRate(name="Single", p_per_kwh=money_p(unit, supply_type=supply_type))],
        standing_p_per_day=money_p(standing, supply_type=SUPPLY_IMPORT) if standing else 0.0,
        end_date=card.get("end_date_uk"),
        fixed="FIXED" in (card.get("tags") or []),
        source="cards",
    )


def apply_contract(tariff: FuseTariff, contract: dict[str, Any] | None) -> FuseTariff:
    """Contract dates and the next price change, from suppliesCurrentContracts."""
    if not isinstance(contract, dict):
        return tariff
    current = contract.get("current") or {}
    tariff.start_date = current.get("from_date_uk") or tariff.start_date
    tariff.end_date = current.get("to_date_uk") or tariff.end_date
    tariff.tariff_id = tariff.tariff_id or (current.get("tariff") or {}).get("tariff_id")
    nxt = contract.get("next_update") or {}
    tariff.next_update_date = nxt.get("update_date_uk")
    tariff.next_update_reason = nxt.get("reason")
    tariff.next_prices_from = ((contract.get("next") or {}).get("tariff") or {}).get(
        "tariff_details_available_from_date_uk"
    )
    return tariff


def contracted_supplies(contracts_data: Any) -> dict[str, dict[str, Any]]:
    """supply_fid -> contract, for supplies Fuse currently supplies (tRPC suppliesCurrentContracts data)."""
    by_fid = (((contracts_data or {}).get("contracts") or {}).get("supply_fid_to_contracts")) or {}
    return {fid: c for fid, c in by_fid.items() if isinstance(c, dict) and c.get("current")}


def electricity_supplies(premises_data: Any) -> list[dict[str, Any]]:
    """[{premises_id, supply_fid, supply_type, identifier}] for every electricity import/export supply."""
    return account_supplies(premises_data, SUPPLY_TYPES)


def account_supplies(premises_data: Any, types: tuple[str, ...] = (*SUPPLY_TYPES, SUPPLY_GAS)) -> list[dict[str, Any]]:
    """[{premises_id, supply_fid, supply_type, identifier}] for the account's supplies of the given types."""
    out = []
    for entry in premises_data or []:
        if not isinstance(entry, dict):
            continue
        premises_id = (entry.get("premises") or {}).get("id")
        for sup in entry.get("supplies") or []:
            definition = (sup or {}).get("supply_definition") or {}
            if definition.get("supply_type") in types and premises_id and sup.get("supply_fid"):
                out.append(
                    {
                        "premises_id": premises_id,
                        "supply_fid": sup["supply_fid"],
                        "supply_type": definition["supply_type"],
                        "identifier": definition.get("identifier"),
                    }
                )
    return out


# --- Rate rows --------------------------------------------------------------------------------------------------


def rate_for_minute(tariff: FuseTariff, minute: int) -> float:
    default = None
    for rate in tariff.rates:
        if rate.windows is None:
            default = rate
        elif any(a <= minute < b for a, b in rate.windows):
            return rate.p_per_kwh
    return (default or tariff.rates[0]).p_per_kwh


def rate_rows(tariff: FuseTariff, start: datetime, end: datetime, *, tz: ZoneInfo = UK_TZ) -> list[dict[str, Any]]:
    """Half-hour rows (Octopus API shape, p/kWh) covering [start, end).

    Fuse prices hold until the contract ends; after that the next prices may not be published yet, so the current
    ones carry on (the status says so) rather than leaving SmartCharge without prices.
    """
    utc = ZoneInfo("UTC")
    slot = start.astimezone(utc).replace(second=0, microsecond=0)
    slot = slot.replace(minute=0 if slot.minute < 30 else 30)
    rows = []
    while slot < end:
        nxt = slot + timedelta(minutes=30)
        local = slot.astimezone(tz)
        rows.append(
            {
                "valid_from": slot.isoformat(),
                "valid_to": nxt.isoformat(),
                "value_inc_vat": rate_for_minute(tariff, local.hour * 60 + local.minute),
            }
        )
        slot = nxt
    return rows


def window_rows(tariff: FuseTariff, now: datetime, *, tz: ZoneInfo = UK_TZ) -> list[dict[str, Any]]:
    """Rows from local midnight (minus an hour) to two days ahead - the span the schedule builder and planner use."""
    midnight = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return rate_rows(tariff, midnight - timedelta(hours=1), now + timedelta(days=2), tz=tz)


def describe_rates(tariff: FuseTariff) -> str:
    """'13.00p' or 'Day 29.44p · Night 12.79p'."""
    return " · ".join(
        (f"{r.name} " if tariff.multi_rate else "") + f"{r.p_per_kwh:.2f}p" for r in tariff.rates
    )


# --- When to fetch ----------------------------------------------------------------------------------------------


def fetch_due(state: dict[str, Any], *, needed: tuple[str, ...], now: datetime, today: date) -> bool:
    """Should a background fetch run? (A Fetch press always does.)

    Waits for the user after a sign-in failure; an hour after any failed attempt; otherwise fetches when a needed
    tariff is missing, once a day after a contract ends or the next prices are published, and weekly to keep the
    rates and the sign-in fresh.
    """
    if not state.get("cookies", {}).get("app-auth") or state.get("auth_failed"):
        return False
    last_attempt = parse_iso(state.get("last_attempt_at"))
    if state.get("last_error") and last_attempt and now - last_attempt < RETRY_AFTER:
        return False
    tariffs = state.get("tariffs") or {}
    if any(kind not in tariffs for kind in needed):
        return True
    last_fetch = parse_iso(state.get("last_fetch_at"))
    if last_fetch is None or now - last_fetch >= REFETCH_AFTER:
        return True
    fetched_day = last_fetch.astimezone(UK_TZ).date().isoformat()
    today_s = today.isoformat()
    for kind in needed:
        t = tariffs[kind]
        if t.get("end_date") and t["end_date"] < today_s and fetched_day < today_s:
            return True
        if t.get("next_prices_from") and t["next_prices_from"] <= today_s and fetched_day < t["next_prices_from"]:
            return True
    return False


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("UTC"))
