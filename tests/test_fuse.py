"""Fuse Energy tariffs (fuse_parse.py / fuse_api.py), from the shapes the Fuse web app returned on 13 Oct 2026.

The fixtures are real responses for a single-rate export supply with every identifier, postcode, MPAN and meter
serial replaced. Fuse's multi-rate time windows (charge_time_ranges_uk) have only been seen as null, so the
multi-rate tests use assumed shapes.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from multidict import CIMultiDict

from custom_components.foxess_plant.coordinator import FoxessPlantCoordinator
from custom_components.foxess_plant.fuse_api import (
    FuseApiError,
    FuseAuthRequired,
    FuseWebClient,
    build_fuse_cache,
    fetch_fuse_tariffs,
    parse_set_cookie,
)
from custom_components.foxess_plant.fuse_parse import (
    SUPPLY_EXPORT,
    SUPPLY_IMPORT,
    FuseParseError,
    FuseTariff,
    apply_contract,
    contracted_supplies,
    describe_rates,
    electricity_supplies,
    fetch_due,
    money_p,
    normalize_phone,
    parse_time_ranges,
    rate_rows,
    rsc_value,
    tariff_from_bill,
    tariff_from_cards,
    trpc_error,
)
from custom_components.foxess_plant.models import TariffConfig

UK = ZoneInfo("Europe/London")
PREMISES = "11111111-1111-4111-8111-111111111111"
EXPORT_FID = "22222222-2222-4222-8222-222222222222"
OTHER_FID = "33333333-3333-4333-8333-333333333333"
TARIFF_ID = "44444444-4444-4444-8444-444444444444"


def _span(rates, charges, flow="E", start="2026-10-01"):
    return {
        "tariff_contract_details": {
            "name": "Single Rate Variable",
            "tariff_id": TARIFF_ID,
            "unit_rates": rates,
            "standing_charge_per_day": {"amount": "0", "currency": "GBP"},
            "payment_method": "DIRECT_DEBIT",
            "fixed_tariff_details": None,
            "contract_start_date_uk": "2026-10-01",
            "contract_end_date_uk": "2026-12-31",
            "contract_type": "REGULAR",
            "is_cheapest_available": False,
            "tariff_disclaimers": [],
        },
        "billing_start_date_uk": start,
        "billing_end_date_uk": "2026-10-13",
        "energy_flow_direction": flow,
        "per_rate_charges": charges,
        "standing_charge": {"price": {"amount": "0", "currency": "GBP"}, "num_of_days": 0},
        "meter_serial_number": "00X0000000",
    }


EXPORT_BILL = {
    "period_view": {"type": "MONTH", "view": {"year": 2026, "month": 10}},
    "premises_fid": PREMISES,
    "premises_name": "AB1 2CD",
    "bill_details": {
        "breakdowns": [
            {
                "supply_type": "ELEC_EXPORT",
                "element": {
                    "supply_fid": EXPORT_FID,
                    "per_billing_span": [
                        _span(
                            [{"rate_name": "Single", "price_per_kWh": {"amount": "-0.1300", "currency": "GBP"}}],
                            [
                                {
                                    "rate_name": "Single",
                                    "rate_name_shorthand": "S",
                                    "price_per_unit": {"amount": "-0.1300", "currency": "GBP"},
                                    "charge_time_ranges_uk": None,
                                    "clock_change_details": None,
                                }
                            ],
                        )
                    ],
                },
            }
        ],
        "supply_properties": [{"supply_type": "ELEC_EXPORT", "details": {"mpan_core": 1234567890123}}],
    },
    "bill_update_status": "BILL_FORECASTED",
}

CONTRACTS = {
    "contracts": {
        "supply_fid_to_contracts": {
            OTHER_FID: {"queued": None, "pending": None, "current": None, "next": None, "tariff_changeable": "NO"},
            EXPORT_FID: {
                "current": {
                    "supply_fid": EXPORT_FID,
                    "supply_name": "Export",
                    "tariff": {"discriminator": "PRICED", "tariff_id": TARIFF_ID, "title": "Single Rate Variable"},
                    "from_date_uk": "2026-10-01",
                    "to_date_uk": "2026-12-31",
                },
                "next_update": {"update_date_uk": "2027-01-01", "reason": "PRICE_CAP"},
                "next": {
                    "tariff": {
                        "discriminator": "UNPRICED",
                        "title": "Single Rate Variable",
                        "supply_type": "ELEC_EXPORT",
                        "tariff_details_available_from_date_uk": "2026-12-01",
                    },
                    "from_date_uk": "2027-01-01",
                },
                "tariff_changeable": "YES",
            },
        }
    }
}

CARD_GROUPS = [
    {
        "name": "Your tariff",
        "cards": [
            {
                "tariff": {
                    "tariff_id": TARIFF_ID,
                    "supply_type": "ELEC_EXPORT",
                    "title": "Single Rate Variable",
                    "description_items": [
                        {"type": "STANDARD", "value": {"name": "Standing charge (per day)", "value": "£0.0000", "tags": []}},
                        {"type": "STANDARD", "value": {"name": "Unit rate (per kWh)", "value": "-£0.1300", "tags": []}},
                    ],
                },
                "tags": ["SINGLE_RATE", "VARIABLE"],
                "availability": "CURRENT_TARIFF_NOT_AVAILABLE_TO_PICK",
                "end_date_uk": "2026-12-31",
            }
        ],
    }
]

PREMISES_DATA = [
    {
        "premises": {"id": PREMISES},
        "supplies": [
            {"supply_fid": OTHER_FID, "supply_definition": {"supply_type": "GAS", "identifier": "9"}},
            {"supply_fid": EXPORT_FID, "supply_definition": {"supply_type": "ELEC_EXPORT", "identifier": "1234567890123"}},
        ],
    }
]


def _rsc(key: str, value) -> str:
    """A React Server Components payload line like the web app's."""
    return '0:{"b":"x"}\na:["$","$L3e",null,{"%s":%s,"premisesFid":"%s"}]\n' % (key, json.dumps(value), PREMISES)


# --- Parsing ----------------------------------------------------------------------------------------------------


def test_export_tariff_from_the_bill() -> None:
    t = tariff_from_bill(rsc_value(_rsc("initialBill", EXPORT_BILL), "initialBill"), EXPORT_FID, today=date(2026, 10, 13))
    assert t is not None and t.supply_type == SUPPLY_EXPORT and t.source == "bill"
    assert t.name == "Single Rate Variable" and t.tariff_id == TARIFF_ID
    assert [(r.name, r.p_per_kwh, r.windows) for r in t.rates] == [("Single", 13.0, None)]  # paid 13p, positive
    assert t.standing_p_per_day == 0.0 and (t.start_date, t.end_date) == ("2026-10-01", "2026-12-31")
    assert describe_rates(t) == "13.00p"


def test_other_supply_or_no_span_gives_nothing() -> None:
    assert tariff_from_bill(EXPORT_BILL, OTHER_FID, today=date(2026, 10, 13)) is None
    empty = json.loads(json.dumps(EXPORT_BILL))
    empty["bill_details"]["breakdowns"][0]["element"]["per_billing_span"] = []
    assert tariff_from_bill(empty, EXPORT_FID, today=date(2026, 10, 13)) is None


def test_contract_dates_and_next_price_change() -> None:
    t = tariff_from_bill(EXPORT_BILL, EXPORT_FID, today=date(2026, 10, 13))
    apply_contract(t, contracted_supplies(CONTRACTS)[EXPORT_FID])
    assert (t.next_update_date, t.next_update_reason, t.next_prices_from) == ("2027-01-01", "PRICE_CAP", "2026-12-01")
    assert list(contracted_supplies(CONTRACTS)) == [EXPORT_FID]  # supplies without a current contract are skipped


def test_tariff_from_the_tariff_card() -> None:
    t = tariff_from_cards(CARD_GROUPS, EXPORT_FID, SUPPLY_EXPORT)
    assert t is not None and t.source == "cards" and t.rates[0].p_per_kwh == 13.0 and t.standing_p_per_day == 0.0
    assert t.end_date == "2026-12-31" and not t.fixed


def test_electricity_supplies_skip_gas() -> None:
    assert [s["supply_fid"] for s in electricity_supplies(PREMISES_DATA)] == [EXPORT_FID]


def test_prices_pounds_to_pence_with_export_flipped() -> None:
    assert money_p("-0.1300", supply_type=SUPPLY_EXPORT) == 13.0
    assert money_p("0.2944", supply_type=SUPPLY_IMPORT) == 29.44
    assert money_p("£0.0000", supply_type=SUPPLY_EXPORT) == 0.0
    with pytest.raises(FuseParseError):
        money_p("n/a", supply_type=SUPPLY_IMPORT)


def test_time_ranges_in_the_shapes_we_accept() -> None:
    assert parse_time_ranges(None) is None
    assert parse_time_ranges([{"start": "00:30", "end": "07:30"}]) == [(30, 450)]
    assert parse_time_ranges([{"start_time_uk": "23:00:00", "end_time_uk": "06:00:00"}]) == [(1380, 1440), (0, 360)]
    assert parse_time_ranges(["02:00-05:00"]) == [(120, 300)]
    assert parse_time_ranges({"ranges": [[{"hour": 1, "minute": 0}, {"hour": 4, "minute": 0}]]}) == [(60, 240)]
    with pytest.raises(FuseParseError):
        parse_time_ranges([{"when": "night"}])


def _two_rate_bill(night_ranges):
    bill = json.loads(json.dumps(EXPORT_BILL))
    breakdown = bill["bill_details"]["breakdowns"][0]
    breakdown["supply_type"] = "ELEC_IMPORT"
    breakdown["element"]["per_billing_span"] = [
        _span(
            [
                {"rate_name": "Day", "price_per_kWh": {"amount": "0.2944"}},
                {"rate_name": "Night", "price_per_kWh": {"amount": "0.1279"}},
            ],
            [
                {"rate_name": "Day", "price_per_unit": {"amount": "0.2944"}, "charge_time_ranges_uk": None},
                {"rate_name": "Night", "price_per_unit": {"amount": "0.1279"}, "charge_time_ranges_uk": night_ranges},
            ],
            flow="I",
        )
    ]
    return bill


def test_two_rate_import_rows_follow_the_windows_across_a_clock_change() -> None:
    t = tariff_from_bill(_two_rate_bill([{"start": "00:30", "end": "07:30"}]), EXPORT_FID, today=date(2026, 10, 13))
    assert t.supply_type == SUPPLY_IMPORT and describe_rates(t) == "Day 29.44p · Night 12.79p"
    # 25 Oct 2026 has 25 local hours (BST ends); every half hour still gets the right local price
    start = datetime(2026, 10, 25, 0, 0, tzinfo=UK)
    rows = rate_rows(t, start, datetime(2026, 10, 26, 0, 0, tzinfo=UK))
    assert len(rows) == 50
    local = {
        (datetime.fromisoformat(r["valid_from"]).astimezone(UK).strftime("%H:%M"), r["value_inc_vat"]) for r in rows
    }
    assert ("00:00", 29.44) in local and ("00:30", 12.79) in local and ("07:00", 12.79) in local
    assert ("07:30", 29.44) in local and ("01:30", 12.79) in local


def test_two_rates_without_hours_are_refused() -> None:
    with pytest.raises(FuseParseError, match="which hours"):
        tariff_from_bill(_two_rate_bill(None), EXPORT_FID, today=date(2026, 10, 13))


def test_tariff_survives_storage() -> None:
    t = tariff_from_bill(_two_rate_bill([{"start": "00:30", "end": "07:30"}]), EXPORT_FID, today=date(2026, 10, 13))
    again = FuseTariff.from_dict(json.loads(json.dumps(t.to_dict())))
    assert again == t


def test_trpc_errors_in_the_body() -> None:
    assert trpc_error({"result": {"data": {"signedIn": True}}}) is None
    assert trpc_error({"error": {"json": {"message": "UNAUTHORIZED", "code": -32001}}}) == "UNAUTHORIZED"
    assert trpc_error([{"error": {"data": {"errorCode": "too_many_attempts"}}}]) == "too_many_attempts"


def test_phone_numbers() -> None:
    assert normalize_phone("07700 900123") == "+447700900123"
    assert normalize_phone("+44 7700 900123") == "+447700900123"
    assert normalize_phone("00447700900123") == "+447700900123"
    with pytest.raises(FuseParseError):
        normalize_phone("123")


def test_set_cookie_values_with_slashes_and_deletes() -> None:
    assert parse_set_cookie("app-auth=ab/cd+ef==; Path=/; Max-Age=31536000; HttpOnly; Secure") == ("app-auth", "ab/cd+ef==")
    assert parse_set_cookie("otp=; Path=/; Max-Age=0") == ("otp", None)
    assert parse_set_cookie("otp=x; Expires=Thu, 01 Jan 1970 00:00:00 GMT") == ("otp", None)
    assert parse_set_cookie("garbage") is None


# --- When to fetch ----------------------------------------------------------------------------------------------

NOW = datetime(2026, 10, 13, 18, 0, tzinfo=UTC)
TODAY = date(2026, 10, 13)


def _state(**over):
    t = tariff_from_bill(EXPORT_BILL, EXPORT_FID, today=TODAY)
    apply_contract(t, contracted_supplies(CONTRACTS)[EXPORT_FID])
    base = {
        "cookies": {"app-auth": "x"},
        "tariffs": {SUPPLY_EXPORT: t.to_dict()},
        "last_fetch_at": (NOW - timedelta(hours=2)).isoformat(),
        "last_attempt_at": (NOW - timedelta(hours=2)).isoformat(),
    }
    return {**base, **over}


def _due(state, now=NOW, today=TODAY, needed=(SUPPLY_EXPORT,)):
    return fetch_due(state, needed=needed, now=now, today=today)


def test_fetch_rules() -> None:
    assert not _due(_state())  # fresh
    assert not _due(_state(cookies={}))  # not signed in: wait for the user
    assert not _due(_state(auth_failed=True))
    assert _due(_state(), needed=(SUPPLY_EXPORT, SUPPLY_IMPORT))  # a needed tariff is missing
    assert _due(_state(last_fetch_at=(NOW - timedelta(days=8)).isoformat()))  # weekly
    # a failure waits an hour, even if a tariff is missing
    failed = _state(last_error="HTTP 503", last_attempt_at=(NOW - timedelta(minutes=20)).isoformat())
    assert not _due(failed, needed=(SUPPLY_EXPORT, SUPPLY_IMPORT))
    assert _due({**failed, "last_attempt_at": (NOW - timedelta(hours=2)).isoformat()}, needed=(SUPPLY_IMPORT,))


def test_fetch_when_next_prices_are_published_and_after_the_contract_ends() -> None:
    dec1 = datetime(2026, 12, 1, 9, 0, tzinfo=UTC)
    stale = _state(last_fetch_at=(dec1 - timedelta(days=2)).isoformat())
    assert _due(stale, now=dec1, today=date(2026, 12, 1))
    assert not _due(_state(last_fetch_at=dec1.isoformat()), now=dec1 + timedelta(hours=3), today=date(2026, 12, 1))
    jan1 = datetime(2027, 1, 1, 9, 0, tzinfo=UTC)
    assert _due(_state(last_fetch_at=(jan1 - timedelta(days=1)).isoformat()), now=jan1, today=date(2027, 1, 1))
    # once that day
    assert not _due(_state(last_fetch_at=jan1.isoformat()), now=jan1 + timedelta(hours=2), today=date(2027, 1, 1))


# --- Rate cache and config --------------------------------------------------------------------------------------


def test_cache_from_saved_tariffs() -> None:
    imp = tariff_from_bill(_two_rate_bill([{"start": "00:30", "end": "07:30"}]), EXPORT_FID, today=TODAY).to_dict()
    exp = tariff_from_bill(EXPORT_BILL, EXPORT_FID, today=TODAY).to_dict()
    cache = build_fuse_cache(imp, exp, now=NOW)
    assert cache["tariff_type"] == "economy7" and cache["import_tariff_code"] == "Single Rate Variable"
    assert cache["current_import_p_per_kwh"] == 29.44 and cache["current_export_p_per_kwh"] == 13.0
    assert cache["import_standing_p_per_day"] == 0.0 and cache["agreement_valid_to"] == "2026-12-31"
    assert cache["schedule"] and len(cache["import_rates"]) == len(cache["export_rates"]) > 96


def test_fuse_export_config() -> None:
    cfg = TariffConfig.from_dict({"export_provider": "fuse"})
    assert cfg.export_provider == "fuse" and cfg.export_supplier.enabled and cfg.export_supplier.provider == "fuse"
    assert cfg.export_supplier.native_credentials_configured()
    both = TariffConfig.from_dict(
        {"dynamic": {"enabled": True, "provider": "fuse", "source": "native"}, "export_provider": "fuse"}
    )
    assert both.dynamic.native_fuse() and both.dynamic.native_api() and both.export_provider == "same"


class _Fake:
    def __init__(self, tariff, fuse_state):
        self.plant = SimpleNamespace(tariff=tariff)
        self._export_supplier_cache: dict = {}
        self._octopus_cache: dict = {}
        self._fuse_state = fuse_state
        self.hass = SimpleNamespace(states=SimpleNamespace(get=lambda _e: None))

    def _export_entity_rows(self):
        return []

    def _fuse_rows(self, kind):
        return FoxessPlantCoordinator._fuse_rows(self, kind)


def test_export_rows_come_from_the_saved_fuse_tariff() -> None:
    fake = _Fake(TariffConfig.from_dict({"export_provider": "fuse"}), _state())
    rows = FoxessPlantCoordinator._export_override_rows(fake)
    assert rows and {r["value_inc_vat"] for r in rows} == {13.0}
    fake._fuse_state = {}
    assert FoxessPlantCoordinator._export_override_rows(fake) is None  # nothing saved yet: band rates instead


# --- Client and fetch (fake web session) ------------------------------------------------------------------------


class _Resp:
    def __init__(self, status=200, body="", cookies=()):
        self.status = status
        self._body = body if isinstance(body, str) else json.dumps(body)
        self.headers = CIMultiDict([("Set-Cookie", c) for c in cookies])

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, routes):
        self.routes = routes  # path prefix -> _Resp
        self.sent = []

    def request(self, method, url, **kwargs):
        path = url.split("fuseenergy.com", 1)[1]
        self.sent.append((method, path, kwargs.get("headers", {}).get("Cookie"), kwargs.get("json")))
        for prefix, resp in self.routes.items():
            if path.startswith(prefix):
                return resp
        return _Resp(404, "{}")


@pytest.mark.asyncio
async def test_sign_in_keeps_and_persists_the_cookies() -> None:
    saved = []

    async def on_cookies(c):
        saved.append(dict(c))

    session = _Session(
        {
            "/api/trpc/phoneSignIn": _Resp(
                body={"result": {"data": {"challenge_type": "PHONE_OTP"}}},
                cookies=["session_id=s1; Path=/", "otp=o/1==; Path=/; Max-Age=600"],
            ),
            "/api/trpc/verifyOtp": _Resp(
                body={"result": {"data": {"challenge_type": "AUTHORIZED", "data": {"access_token": "[n/a]"}}}},
                cookies=["app-auth=a/b=c; Path=/; Max-Age=31536000", "tracking=1; Path=/"],
            ),
        }
    )
    client = FuseWebClient(session, on_cookies=on_cookies)
    await client.send_code("+447700900123")
    await client.verify_code("123 456")
    assert client.cookies == {"session_id": "s1", "otp": "o/1==", "app-auth": "a/b=c"}  # others ignored
    assert saved[-1] == client.cookies
    assert session.sent[1][2] == "session_id=s1; otp=o/1==" and session.sent[1][3] == {"code": "123 456".strip()}


@pytest.mark.asyncio
async def test_extra_identity_questions_and_wrong_codes_are_explained() -> None:
    session = _Session({"/api/trpc/verifyOtp": _Resp(body={"result": {"data": {"challenge_type": "ADDITIONAL_INFO"}}})})
    client = FuseWebClient(session, cookies={"otp": "o"})
    with pytest.raises(FuseApiError, match="extra identity"):
        await client.verify_code("123456")
    session.routes["/api/trpc/verifyOtp"] = _Resp(body={"error": {"json": {"message": "invalid_code"}}})
    with pytest.raises(FuseApiError, match="invalid_code"):
        await client.verify_code("123456")
    with pytest.raises(FuseApiError, match="Send Code"):
        await FuseWebClient(session).verify_code("123456")


@pytest.mark.asyncio
async def test_fetch_reads_the_export_tariff() -> None:
    session = _Session(
        {
            "/api/trpc/signedIn": _Resp(body={"result": {"data": {"signedIn": True}}}),
            "/api/trpc/customerPremises": _Resp(body={"result": {"data": PREMISES_DATA}}),
            "/api/trpc/suppliesCurrentContracts": _Resp(body={"result": {"data": CONTRACTS}}),
            f"/app/premises/{PREMISES}/supply/{EXPORT_FID}?": _Resp(body=_rsc("initialBill", EXPORT_BILL)),
        }
    )
    tariffs = await fetch_fuse_tariffs(FuseWebClient(session, cookies={"app-auth": "a"}), today=TODAY)
    assert list(tariffs) == [SUPPLY_EXPORT]
    t = tariffs[SUPPLY_EXPORT]
    assert t["rates"] == [{"name": "Single", "p_per_kwh": 13.0, "windows": None}]
    assert t["identifier"] == "1234567890123" and t["next_prices_from"] == "2026-12-01"
    assert all(c == "app-auth=a" for _m, _p, c, _b in session.sent)


@pytest.mark.asyncio
async def test_fetch_falls_back_to_the_tariff_card() -> None:
    no_span = json.loads(json.dumps(EXPORT_BILL))
    no_span["bill_details"]["breakdowns"] = []
    session = _Session(
        {
            "/api/trpc/signedIn": _Resp(body={"result": {"data": {"signedIn": True}}}),
            "/api/trpc/customerPremises": _Resp(body={"result": {"data": PREMISES_DATA}}),
            "/api/trpc/suppliesCurrentContracts": _Resp(body={"result": {"data": CONTRACTS}}),
            f"/app/premises/{PREMISES}/supply/{EXPORT_FID}/tariff/change": _Resp(body=_rsc("groups", CARD_GROUPS)),
            f"/app/premises/{PREMISES}/supply/{EXPORT_FID}?": _Resp(body=_rsc("initialBill", no_span)),
        }
    )
    tariffs = await fetch_fuse_tariffs(FuseWebClient(session, cookies={"app-auth": "a"}), today=TODAY)
    assert tariffs[SUPPLY_EXPORT]["source"] == "cards" and tariffs[SUPPLY_EXPORT]["rates"][0]["p_per_kwh"] == 13.0


@pytest.mark.asyncio
async def test_signed_out_sessions_need_a_new_code() -> None:
    session = _Session({"/api/trpc/signedIn": _Resp(body={"result": {"data": {"signedIn": False}}})})
    with pytest.raises(FuseAuthRequired):
        await fetch_fuse_tariffs(FuseWebClient(session, cookies={"app-auth": "a"}), today=TODAY)
    with pytest.raises(FuseAuthRequired):
        await fetch_fuse_tariffs(FuseWebClient(session), today=TODAY)
    session.routes = {"/api/trpc/signedIn": _Resp(307, "")}
    with pytest.raises(FuseAuthRequired):
        await fetch_fuse_tariffs(FuseWebClient(session, cookies={"app-auth": "a"}), today=TODAY)
