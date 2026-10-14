# Fuse Energy: unofficial API notes

Notes for anyone integrating [Fuse Energy](https://www.fuseenergy.com/) (UK electricity and gas supplier, formerly
Tesseract) with Home Assistant or similar. They cover signing in, reading the **current tariff** (unit rates, rate
hours, standing charge, contract dates) and reading the **smart meter's half-hourly readings**, as found while adding
Fuse to Fox Plant in October 2026.

> **Unofficial.** Fuse publishes no API. Everything here was worked out from the Fuse web app's own traffic, using
> our own account, and can change without notice. Be gentle: a few requests a day per account is plenty (tariffs
> change at contract boundaries; meter readings arrive within hours). Never ask users for anything but their own
> sign-in.

Working code: Fox Plant's [`fuse_api.py`](../custom_components/foxess_plant/fuse_api.py) (client),
[`fuse_parse.py`](../custom_components/foxess_plant/fuse_parse.py) (parsing, rate rows, fetch rules),
[`fuse_meter.py`](../custom_components/foxess_plant/fuse_meter.py) (readings into HA statistics) and their tests
(`tests/test_fuse*.py`, built from real responses with identifiers replaced).

---

## 1. Two APIs, and which one has what

| | Mobile API `https://api.fuseenergy.com` | Website `https://www.fuseenergy.com` |
|---|---|---|
| Used by | Fuse mobile app | Fuse web app (`/app`) |
| Sign-in | Phone + SMS code, bearer tokens | Phone + SMS code, session cookie |
| Usage and cost per hour | ✅ `/api/v1/premises/{id}/chart` | ✅ (`premisesDisplayData`, daily) |
| **Current tariff** (rates, hours, standing charge) | ❌ none found | ✅ supply page payload |
| Contract dates, next price change | ❌ none found | ✅ `suppliesCurrentContracts` |
| **Half-hourly meter readings** with status | ❌ none found | ✅ meter readings page payload |

The mobile API's token does **not** work on the website, and vice versa. Fox Plant uses the website for everything
because only it exposes the tariff. We tried about 40 likely REST paths on the mobile API (`/api/v1–v6/…/contracts`,
`…/tariffs`, `…/bill`, `…/billing`, `/api/v4/properties/{id}/billing` from older notes): all `404 {"detail":"Not Found"}`.
It's a FastAPI app with no published `openapi.json`.

---

## 2. Signing in (website)

The website is a Next.js app. Small calls go through **tRPC** at `/api/trpc/<procedure>`; pages arrive as **React
Server Components** payloads (see §3).

1. **Send the code**

   ```http
   POST /api/trpc/phoneSignIn
   Content-Type: application/json

   {"phone": "+447700900123"}
   ```

   The number must be **E.164** (`+44` then the number without its leading 0). Answer:
   `{"result":{"data":{"challenge_type":"PHONE_OTP","auth_flow_token":null,"data":{"phone_number":"+44…"}}}}`.
   Fuse texts a 6-digit code and sets two cookies: `session_id` and `otp`. Keep both.

2. **Verify the code** (send the cookies back)

   ```http
   POST /api/trpc/verifyOtp
   Cookie: session_id=…; otp=…

   {"code": "123456"}
   ```

   Success: `challenge_type: "AUTHORIZED"` (the tokens in the body are redacted as `"[n/a]"`) and a new cookie
   **`app-auth`** with a **one-year** expiry. That cookie is the session; the website keeps the real tokens inside it
   and renews them itself. In our testing `app-auth` alone was enough for every later request.

   Other answers seen in the web app's code: `ADDITIONAL_INFO` (Fuse wants identity details such as date of birth or
   postcode; the web page for this is `/additional-info`, but we never saw the procedure that answers it),
   `CREATE_ACCOUNT`, `MAGIC_LINK_CHECK` (e-mail sign-in, `emailSignIn {email}`). Errors come back in the body, e.g.
   `too_many_attempts`.

3. **Check the session**: `GET /api/trpc/signedIn` → `{"result":{"data":{"signedIn":true}}}`.
   A signed-out cookie gives `signedIn: false`; data calls then return empty results, and pages redirect.

**Cookie handling gotchas**
- `app-auth` is a long encoded value containing `/` and `=`. Python's `http.cookies.SimpleCookie` **rejects** such
  values silently, so parse `Set-Cookie` headers by hand (split at the first `=`, honour `Max-Age=0` / past `Expires`
  as deletion).
- Persist every cookie change immediately; Fuse may rotate them.
- In Home Assistant, use a client session with `aiohttp.DummyCookieJar()` and send `Cookie:` yourself, so Fuse's
  cookies don't end up in HA's shared session.
- The web app sends `x-fuse-app-version: git-<hash>` and `Timezone: Europe/London`; neither was required in our tests.
  Requests with a Home Assistant user agent worked (no browser check). If you ever get a response header
  `x-amzn-waf-action`, it's AWS WAF asking for a browser challenge: back off.

**Mobile API sign-in** (for the hourly usage chart): documented and implemented in
[simonsolts/fuse-homeassistant](https://github.com/simonsolts/fuse-homeassistant) and
[yakupafsin/fuse-energy-ha](https://github.com/yakupafsin/fuse-energy-ha): `POST api.fuseenergy.com/api/v3/auth`
with `challenge_type` INITIAL → PHONE_OTP → (ADDITIONAL_INFO) → AUTHORIZED, a `Device-Id` header, the SMS sent by the
website's `phoneSignIn`, refresh at `/api/v1/auth/refresh` (both tokens rotate).

---

## 3. Making requests

**tRPC GET**: input is URL-encoded JSON.

```http
GET /api/trpc/suppliesCurrentContracts?input=%7B%22supplyFids%22%3A%5B%22<supply id>%22%5D%7D
Cookie: app-auth=…
```

Successful answers are `{"result":{"data": …}}`. **Failures often come with HTTP 200** and an `error` object in the
body (`{"error":{"json":{"message":"UNAUTHORIZED",…}}}`, sometimes nested under `data` or in a batch list), so always
check the body.

**Page payloads (RSC)**: request the app page with header `RSC: 1`:

```http
GET /app/premises/<premises id>/supply/<supply id>?month=10&year=2026
RSC: 1
Accept: text/x-component
Cookie: app-auth=…
```

The answer is a React Server Components stream: lines like `a:["$","$L3e",null,{"initialBill":{…},"premisesFid":"…"}]`.
The data you want is a prop's JSON value. Find `"initialBill":` and decode the value that follows, e.g. in Python
`json.JSONDecoder().raw_decode(text, index_after_the_colon)`. The objects we used are self-contained (no `"$…"`
references to other lines).

When inspecting a browser HAR, many response bodies are **base64** (`content.encoding: "base64"`); decode them before
searching. Browsers can also cut off streamed RSC responses when saving a HAR: if a page "has no data" in the HAR,
fetch it yourself.

---

## 4. Properties and supplies

`GET /api/trpc/customerPremises` → list of properties:

```json
[{
  "premises": {"id": "<premises id>", "address": {…}, "premises_name": "AB1 2CD"},
  "supplies": [{
    "supply_fid": "<supply id>",
    "supply_definition": {
      "supply_type": "ELEC_EXPORT", "fuel_type": "ELEC", "energy_direction": "E",
      "identifier": "<MPAN>", "names": {"full_name": "Export", "short_name": "Export"}
    },
    "switching_status": {"status": "SWITCHED_IN", "switch_in_date_uk": "…"},
    "meter_type_and_status": {"type": "SMART", "status": "ONLINE", "serial_number": "…"},
    "has_had_contract": true
  }],
  "default_date_uk": "…"
}]
```

`supply_type` is `ELEC_IMPORT`, `ELEC_EXPORT` or `GAS`. Supplies can be listed that Fuse **doesn't** supply:
`switching_status` `ONBOARDING` or `CANCELLED`, `has_had_contract: false`. Use the contracts call to tell.

`GET /api/trpc/suppliesCurrentContracts?input={"supplyFids":[…]}` (or `premisesCurrentContracts {premisesFid}`):

```json
{"contracts": {"supply_fid_to_contracts": {
  "<supply id>": {
    "current": {
      "supply_name": "Export",
      "tariff": {"discriminator": "PRICED", "tariff_id": "<tariff id>", "title": "Single Rate Variable",
                 "supply_type": "ELEC_EXPORT", "duration_days": null},
      "from_date_uk": "2026-10-01", "to_date_uk": "2026-12-31",
      "contract_details": {"type": "REGULAR", "is_active": true}
    },
    "next_update": {"update_date_uk": "2027-01-01", "reason": "PRICE_CAP"},
    "next": {"tariff": {"discriminator": "UNPRICED", "title": "Single Rate Variable",
                        "tariff_details_available_from_date_uk": "2026-12-01"},
             "from_date_uk": "2027-01-01"},
    "queued": null, "pending": null, "scheduled": null, "tariff_changeable": "YES"
  },
  "<supply not supplied by Fuse>": {"current": null, "next": null, "tariff_changeable": "NO"}
}}}
```

`current` is null for supplies Fuse doesn't supply. **No prices here**: those come from §5.

---

## 5. The current tariff

### From the supply page (structured; preferred)

`GET /app/premises/<premises id>/supply/<supply id>?month=M&year=Y` with `RSC: 1` → prop **`initialBill`**:

```json
{
  "bill_details": {"breakdowns": [{
    "supply_type": "ELEC_EXPORT",
    "element": {
      "supply_fid": "<supply id>",
      "per_billing_span": [{
        "tariff_contract_details": {
          "name": "Single Rate Variable",
          "tariff_id": "<tariff id>",
          "unit_rates": [{"rate_name": "Single", "price_per_kWh": {"amount": "-0.1300", "currency": "GBP"}}],
          "standing_charge_per_day": {"amount": "0", "currency": "GBP"},
          "fixed_tariff_details": null,
          "contract_start_date_uk": "2026-10-01",
          "contract_end_date_uk": "2026-12-31",
          "contract_type": "REGULAR"
        },
        "billing_start_date_uk": "2026-10-01",
        "billing_end_date_uk": "2026-10-13",
        "energy_flow_direction": "E",
        "per_rate_charges": [{
          "rate_name": "Single", "rate_name_shorthand": "S",
          "price_per_unit": {"amount": "-0.1300", "currency": "GBP"},
          "num_of_kWh": {"value": "58.144", "unit": "kWh"},
          "charge_time_ranges_uk": null,
          "clock_change_details": null
        }],
        "standing_charge": {"price": {"amount": "0"}, "num_of_days": 0}
      }]
    }
  }]},
  "bill_update_status": "BILL_FORECASTED"
}
```

- Prices are **pounds as strings**. **Export prices are negative** (money paid to you): `-0.1300` = 13p/kWh export.
- A month can hold several billing spans (a tariff change mid-month): take the latest span that has started.
- **Rate hours**: `per_rate_charges[].charge_time_ranges_uk` gives the hours each rate applies on a multi-rate
  (Day/Night) tariff. We have only seen it `null` (single rate), so **its shape is unknown**. If you have a
  multi-rate Fuse tariff, please share an example (identifiers removed).
- At the start of a month or for a new supply there may be no billing span yet; use the fallback below.

### From the tariff page (display text; fallback)

`GET /app/premises/<premises id>/supply/<supply id>/tariff/change` with `RSC: 1` → prop **`groups`**:

```json
[{"name": "Your tariff", "cards": [{
  "tariff": {"tariff_id": "<tariff id>", "supply_type": "ELEC_EXPORT", "title": "Single Rate Variable",
             "description_items": [
               {"type": "STANDARD", "value": {"name": "Standing charge (per day)", "value": "£0.0000"}},
               {"type": "STANDARD", "value": {"name": "Unit rate (per kWh)", "value": "-£0.1300"}}]},
  "tags": ["SINGLE_RATE", "VARIABLE"],
  "availability": "CURRENT_TARIFF_NOT_AVAILABLE_TO_PICK",
  "end_date_uk": "2026-12-31"
}]}]
```

Fine for single-rate tariffs; multi-rate cards carry no machine-readable hours.

### When prices change

`next_update.update_date_uk` is when the price changes (`PRICE_CAP` for variable tariffs, quarterly). The new prices
show on the account from that date: `next.tariff.tariff_details_available_from_date_uk` is when Fuse publishes them,
but the supply page only shows the current contract. Re-reading the tariff weekly, and daily after a contract end
date, is enough.

---

## 6. Half-hourly meter readings

`GET /app/premises/<premises id>/supply/<supply id>/meter/hh-readings?month=M&year=Y` with `RSC: 1` → prop
**`initialHhReadings`** (plus `initialPagination`):

```json
{
  "days": [{
    "date_uk": "2026-10-01",
    "readings": [{
      "meter_serial_number": "<serial>",
      "status": "ACTUAL",
      "tpr_customer_name": null,
      "reading": {"value": "0.004", "unit": "kWh"},
      "utc_naive": "2026-09-30T23:30:00.000000",
      "start_time_local": "2026-10-01T00:00:00+01:00",
      "end_time_local": "2026-10-01T00:30:00+01:00"
    }, …]
  }, …],
  "counts": {"estimated": 9, "filled": 0, "completed": 629, "total": 638, "percentage": "98.6"}
}
```

- The month's every half hour is listed: 48 a day, **50 or 46 on clock-change days**. Future slots are `EMPTY`.
- `status`: **`ACTUAL`** = the meter's real reading. `ESTIMATED` / `FILLED` = Fuse's own figures, later replaced.
  `EMPTY` = nothing yet.
- ⚠ **`utc_naive` is the slot's END in UTC**, not its start (the 00:00–00:30 BST slot has `utc_naive` 23:30).
  Use `start_time_local`, which includes its offset.
- **Lag**: real readings for the morning appeared about 5 hours later; a day is complete by the next morning.
- The values matched an independent smart-meter reader (Hildebrand Glow on the same meter) **to the Wh** every day
  we compared. They are the meter's own data.
- `tpr_customer_name` is the register (e.g. Day / Night) on multi-register meters; null on single-register.
- **Gas**: same page for a `GAS` supply; we couldn't see a gas supply with readings, so we don't know whether
  `reading.unit` is `kWh` or `m³`.
- `…/meter/hh-readings/<utc time>?month&year` is a detail view of one slot (not needed).

`GET /app/premises/<premises id>/supply/<supply id>/meter?month=M&year=Y` with `RSC: 1` holds the same
`initialHhReadings`, plus **`initialReadings`**: the meter register reading at midnight each day:

```json
{"today_uk": "2026-10-14", "reads": [{
  "read_date": "2026-10-14", "type": "REMOTE",
  "status": {"value": "PENDING", "rejection_reason_type": null},
  "register_values": [{"tpr": 378, "customer_name": "Single",
                       "register_names": {"register_name": "Single", "register_name_shorthand": "S"},
                       "reading": {"value": "1234.567", "unit": "kWh"}}],
  "utc_naive": "2026-10-14T00:00:00.000000",
  "created_at_datetime_uk": "2026-10-14T01:00:00+01:00"
}], "counts": {…}, "timeline": [{"date_uk": "…", "status": "COMPLETED"}]}
```

`type` is `REMOTE` (from the meter) or `SUBMISSION_CUSTOMER`; `status` is `APPROVED` / `PENDING` / `REJECTED`.
A day's half-hourly total equals the change between consecutive midnight readings (13.096 vs 13.095 kWh, rounding).

### Into Home Assistant

A sensor's history is written when its state changes, so readings that arrive hours late can't be put in a sensor.
Write them as **external long-term statistics** instead (`async_add_external_statistics`, hourly, with past
timestamps), which the Energy dashboard reads. Fox Plant writes an hour only when both its half hours are `ACTUAL`,
continues the running total from `get_last_statistics` so re-runs can't double count, and skips an hour still
unconfirmed after 3 days. See [`fuse_meter.py`](../custom_components/foxess_plant/fuse_meter.py).

---

## 7. Hourly usage and cost (mobile API)

`GET https://api.fuseenergy.com/api/v1/premises/{id}/chart?year&month&day` (mobile bearer token): per supply
(`ELEC_IMPORT`, `ELEC_EXPORT`, `GAS`) hourly bars with kWh and a `breakdown` of cost parts (`USAGE`, `STANDING`) to
4 decimal places. Implemented by the projects in §8. The website's `premisesDisplayData {premisesFid, index:{year,month}}`
returns the same chart by day.

---

## 8. Open questions: help wanted

- The shape of `charge_time_ranges_uk` on a multi-rate (Day/Night, EV) tariff.
- The gas meter readings' unit (`kWh` or `m³`).
- The tRPC procedure that answers an `ADDITIONAL_INFO` sign-in challenge.
- How long the `app-auth` session really lasts in practice (its cookie says a year).
- Whether a priced *next* tariff (`next.tariff.discriminator: "PRICED"`) exposes its rates before it starts.

If you find out, please open an issue on this repository.

## 9. Credits

- [LionZXY/FuseEnergyApi](https://github.com/LionZXY/FuseEnergyApi) (2024): first public notes on the mobile API,
  the refresh endpoint and the billing shapes.
- [simonsolts/fuse-homeassistant](https://github.com/simonsolts/fuse-homeassistant) (2026): the mobile sign-in
  sequence (the SMS is sent by the website's `phoneSignIn`).
- [yakupafsin/fuse-energy-ha](https://github.com/yakupafsin/fuse-energy-ha) (2026): hourly usage and cost into the
  Energy dashboard; cost-breakdown precision findings.
- Fox Plant (this repository): website sign-in with the session cookie, tariff from the supply page, contracts,
  half-hourly meter readings with status, `utc_naive` meaning.
