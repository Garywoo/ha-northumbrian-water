# Northumbrian Water integration: developer guide

This is the technical companion to the user-facing [README](../README.md). It covers how the integration is put together, the reverse-engineered portal API it depends on, and how to test changes. Nothing here is needed to install or use the integration.

## Repository layout

| Path | What it is |
| --- | --- |
| `custom_components/northumbrian_water/api.py` | Standalone portal client. No Home Assistant imports, so the tools can load it directly. |
| `custom_components/northumbrian_water/coordinator.py` | `DataUpdateCoordinator` that fetches the window and writes external statistics. |
| `custom_components/northumbrian_water/sensor.py` | The four summary and diagnostic sensors. |
| `custom_components/northumbrian_water/config_flow.py` | Setup, meter picker, reauth, and the options flow (lookback days). |
| `custom_components/northumbrian_water/diagnostics.py` | Diagnostics download, with credentials and identifiers redacted. |
| `custom_components/northumbrian_water/const.py` | Tunables: lookback (10 days), initial import (30 days), refresh interval (6 hours). |
| `custom_components/northumbrian_water/brand/` | Icons and logos that HA 2026.3+ serves directly. Keep the `@2x` icons: HA's fallback for `dark_icon@2x.png` skips `dark_icon.png`, so without them dark mode shows the navy icon. |
| `tools/` | Offline parsing tests and live diagnostic scripts (see [Testing](#testing)). |
| `.github/workflows/validate.yml` | HACS and hassfest validation. |

## Architecture

```
config entry ──► NorthumbrianWaterClient (api.py)       one aiohttp session + cookie jar
                        │
                        ▼
              NorthumbrianWaterCoordinator              every 6 h
                ├─ fetch lookback+1 days of hourly readings (UK local days)
                ├─ read baseline sum just before the window from the recorder
                ├─ async_add_external_statistics  → northumbrian_water:water_<serial>       (L)
                ├─ async_add_external_statistics  → northumbrian_water:water_cost_<serial>  (GBP)
                └─ ImportResult ──► sensors
```

### Setup

`async_setup_entry` builds a dedicated client session with its own `aiohttp.CookieJar`, so the portal's cookies never mix with the rest of Home Assistant. It then calls `async_bind_account` before the first refresh, because the usage endpoints only work after the account walk (see [API traps](#api-traps)).

### Statistics, not entity state

The portal publishes readings a day or two late, so there is no live value to record as state. The coordinator writes **external statistics** instead, which lets each hour land at its true start time.

- Statistic ids are `northumbrian_water:water_<slug>` and `northumbrian_water:water_cost_<slug>`. The slug is the meter serial, lower-cased, with non-alphanumerics replaced by `_`.
- Every refresh re-imports the whole window and recomputes cumulative `sum`, continuing from the last `sum` recorded before the window. The baseline lookup searches the 30 days before the window. Late backfills and corrections are therefore absorbed rather than lost.
- The cost sum is rounded to 2 dp at each step so float error never shows as fractions of a penny.
- Metadata sets `mean_type` (HA 2025.2+) or the older `has_mean` flag, whichever the core supports, and `unit_class` (`"volume"` for litres, `None` for currency). From 2026.11 a write without `unit_class` is refused.
- If the instance currency is not GBP a warning is logged, because the Energy dashboard will not accept the cost statistic.

### Refresh window

| Situation | Days requested |
| --- | --- |
| First refresh after start or reload | `max(lookback, 30)` + today |
| Routine refresh | `lookback` (default 10, options range 2 to 60) + today |

Day boundaries come from `Europe/London`, not the HA instance's timezone, so the right portal day is always requested.

A day with no data is simply absent. A failed request aborts the **whole** refresh (`UpdateFailed`). Silently dropping a day that was imported earlier would lower every later `sum`, and the Energy dashboard would read that as a meter reset.

### Sensors

| Key | Source | Notes |
| --- | --- | --- |
| `last_full_day_usage` | Litres for the newest local day with 24 or more hours | No `state_class`, so the Energy dashboard never offers it as a source and HA does not record duplicate statistics. |
| `last_full_day_cost` | GBP for the same day | No `state_class`, same reasoning. |
| `last_reading_time` | Start of newest imported hour | Diagnostic, disabled by default. |
| `hours_in_last_import` | Row count of last window | Diagnostic, disabled by default. `MEASUREMENT`. Attributes: `statistic_id`, `cost_statistic_id`, `days_with_data`. |

"Complete day" matters because during BST the newest local day often holds only a single hour. The newest partial day is used only when no complete day exists yet.

Unique ids are `<serial>_<key>`, and the device identifier is `(northumbrian_water, <serial>)`.

### Error model

| Exception (api.py) | Meaning | Setup maps to | Refresh maps to | Config flow shows |
| --- | --- | --- | --- | --- |
| `NWLSessionError` (subclass of `NWLAuthError`) | Portal dropped server-side session state | `ConfigEntryNotReady` | `UpdateFailed` | `cannot_connect` |
| `NWLAuthError` | Credentials refused | `ConfigEntryAuthFailed` → reauth | `ConfigEntryAuthFailed` → reauth | `invalid_auth` |
| `NWLApiError` | Anything else unexpected: HTTP ≥ 400, timeouts, bad JSON | `ConfigEntryNotReady` | `UpdateFailed` | `cannot_connect` |

`NWLSessionError` is always caught **before** `NWLAuthError`. A dropped session must never prompt the user for a new password. Only a 401/403 from `/api/Auth/Login` itself raises `NWLAuthError`. A 401/403 from any other endpoint raises `NWLSessionError`.

### Config flow

- The unique id is the meter serial. Adding the same meter twice aborts with `already_configured`.
- Reauth re-runs discovery and aborts with `wrong_account` if the credentials resolve to a different meter, or shows `meter_missing` if the stored serial is no longer listed.
- The options flow subclasses `OptionsFlowWithReload`, so the entry reloads itself when lookback changes. This is why the minimum HA version is 2025.8.

## Portal API

Everything here was reverse engineered from the site's network traffic and its own JavaScript. The portal is an ASP.NET site that proxies a LoginRadius identity provider and an internal smart-metering service. Base URL: `https://www.nwl.co.uk`.

### Requirements for every call

- Header `X-Requested-With: XMLHttpRequest`. Without it every `/api` call returns 401.
- One cookie jar for the whole conversation, **primed** by a `GET /account/` first, so the site issues `.AspNetCore.Session` and the Azure affinity cookies. The client re-primes before every login.
- The client also sends a browser `User-Agent`, `Origin` and `Referer` and `Content-Type: application/json` (see `DEFAULT_HEADERS`).
- Several responses are `text/plain`, and some are a JSON string that itself contains JSON. `_loads` unwraps one extra layer.

### Call sequence

| Step | Call | Body / params | Returns |
| --- | --- | --- | --- |
| 0 | `GET /account/` | – | Session and affinity cookies |
| 1 | `POST /api/Auth/Login` | `{email, password}` | `Response.{access_token, expires_in, refresh_token, Profile}`. No `Response` means refused. |
| 2 | `POST /api/Auth/SaveUserProfile` | `{access_token, expires_in, refresh_token}` | Profile incl. `PersonId` and tokens. Binds the tokens to the session. |
| 3 | `GET /api/Customer/GetAccountSummary` | `?personId=` | `Accounts[].{accountIDField, premiseIDField, propertyAddressField}` |
| 4 | `POST /api/Customer/AddOrUpdateCustomerSession` | `["PersonId:<id>", "AccountId:<id>"]` | Sets the active account in the session |
| 5 | `POST /api/Customer/GetAccountDetails` | `{AccountId, PremiseId, PersonId}` | `Meters[].BadgeNumber` is the meter serial |
| 6 | `POST /api/Customer/GetSmartAuthToken` | `{access_token}` | `{Access_token, Refresh_token, Id_token, Expires_in}`. Both tokens rotate. |
| 7 | `POST /api/Customer/GetHourlyWaterUsage` | `{AccountId, Authorization: <Id_token>, MeterSerial, StartDate: "YYYY-MM-DDT00:00:00"}` | Up to 24 hourly rows |

After step 1 the client writes the cookies the site's JavaScript would set itself (`loginInfoSuccess`, `userProfile`, `smartTokenInfo`), percent-encoded the way `jquery.cookie` does. After step 6 it writes `smartUserTokenInfo` as the raw response.

Field names are inconsistent between endpoints. Most objects appear both as `Meters` and as `metersField`, so the client reads whichever is populated (`_first`).

### Hourly row shape

```json
{
  "Date": "2026-08-14T01:00:00",
  "LitreValue": 1,
  "MonetaryValue": 0.07,
  "Key": "12:00 AM|1:00 AM",
  "ReadingType": 1,
  "Status": null
}
```

- `Date` is the **end** of the hour, as a naive UK wall-clock time. The client resolves it to an instant in `Europe/London` and *then* subtracts one hour of elapsed time.
- `ReadingType` 1 is an actual read. Any other value is flagged `estimated` on `HourlyReading`, although the flag is not currently used when writing statistics.
- `MonetaryValue` is rounded to the penny per hour at source.

### Other granularities

`GetDailyWaterUsage`, `GetWeeklyWaterUsage`, `GetMonthlyWaterUsage` and `GetYearlyWaterUsage` take the same payload. `StartDate` means something different per endpoint. For hourly it is the day to report on. For weekly and monthly the site passes a date far in the past and gets the 12 most recent buckets. Only the daily endpoint is wrapped (`async_get_daily_totals`), and only for cross-checking by `tools/compare_daily.py`.

### API traps

- **Steps 3 to 5 are mandatory, not discovery.** The usage endpoints read state the portal writes while answering them. Log in and call step 7 directly, and you get a hard 401 however valid the token is. Walk the account pages first and the identical request succeeds. A re-login must replay the whole walk (`_prepare_account_locked`), not just step 4. The failure looks like an authentication problem but is really missing session state.
- **Step 6 is keyed on the access token, not the refresh token.** The site's `getSmartAuthToken()` posts the `access_token` from `smartTokenInfo`. Send the wrong field and you get HTTP 200 with no `Id_token` in the body, a missing token rather than a rejection. The client treats that as `NWLSessionError`.
- **Tokens rotate on every step 6 call.** The response's `Access_token` and `Refresh_token` supersede the ones sent. Rotation is serialised with `_auth_lock` because two concurrent rotations would invalidate each other.
- **The advertised lifetime is wrong.** `Expires_in` says 3598 s, but the `Id_token` JWT's `exp` claim is 600 s. The client trusts `exp` (with a 60 s safety margin), falls back to `min(Expires_in, 600)` if the JWT cannot be decoded, and still retries once as a backstop.
- **There are two kinds of 401, and they need different fixes.**
  - HTTP 200 with a single-element array whose `Status.Code` is `Unauthorized` means a stale `Id_token`. Rotate it.
  - A real HTTP 401 with an RFC 7235 `ProblemDetails` body means the session state is gone. Rotating won't help; `async_rebuild_session` logs in again and replays the walk. This happens routinely, because the portal session idles out between 6-hourly polls.
- **`PersonId` sent to step 4** is whatever step 2 returned. The site itself sometimes stores the literal string `"PersonId"`, and the backend resolves the person from the session. That is not a bug.
- **Daily totals are UTC days while hourly stamps are UK local.** During BST the portal's daily figure for day D equals the hourly rows from 01:00 on D to 01:00 on D+1. The integration uses local days.
- **Clock-change days.** Spring forward returns 23 rows, and the first is stamped 02:00. Autumn returns **24** rows, not 25: one half of the repeated hour is never published. If a duplicate stamp ever appears it is resolved with `fold=1`, and any residual collision is shifted forward or discarded with a warning, because statistics keyed on the same start would overwrite each other.
- **Failed logins lock the account.** After several failed attempts the portal demands a CAPTCHA. Avoid test loops with bad credentials.

Essex & Suffolk Water appears to run the same platform on a different hostname. Pointing `BASE_URL` at it might work but has not been tested.

### Sessions and the browser

Each login gets its own token chain, so the integration and a user's browser session should not interfere. If opening the usage graph in a browser starts failing shortly after the integration refreshes, the chain is shared per account rather than per login, and that would need investigating.

### A note on method

The site sets several cookies from JavaScript, so captured HTTP traffic alone does not explain how it authenticates. Deleting a cookie mid-session proves little once the portal has cached the identity against the session. Reading the site's own scripts, and diffing a working session against a failing one, both beat inferring from a single failing request.

## Testing

All tools load `api.py` through `tools/_load_api.py`, so they need **no Home Assistant install**. Only `aiohttp` is required, and Python 3.11+.

### Offline parsing tests (no credentials, no network)

```bash
python tools/test_parsing.py
```

These use response shapes captured from the live site, and cover:

- a normal BST day (end-of-hour → start-of-hour conversion)
- the autumn change (24 rows), including a hypothetical duplicated stamp
- the spring change (23 rows, first stamped 02:00)
- the estimated-reading flag
- in-band `Unauthorized` detection
- double-encoded JSON unwrapping
- JWT `exp` decoding
- skipping malformed rows

The script exits non-zero if any check fails. Add a case here whenever parsing changes.

### Live tools (need a real account)

Each tool prompts for the password with `getpass`, so it is never echoed or stored. You can also set `NWL_EMAIL` / `NWL_PASSWORD`, or pass `--email`. Output masks or omits identifiers, so it is safe to paste into an issue.

| Command | Purpose |
| --- | --- |
| `python tools/probe.py [--days 7] [--debug]` | End-to-end check: login, discovery, a usage call on the warm session, then a **cold** session bound the way HA does it, then N days of hourly data. `--debug` logs every request, status and cookie count. |
| `python tools/compare_daily.py [--days 30] [--start YYYY-MM-DD]` | Compares the portal's daily totals with same-day and one-hour-shifted sums of its hourly rows, and reports the cost rounding shortfall. Use a winter `--start` to test outside BST. |
| `python tools/inspect_dst.py` | Fetches the 2026-03-29 and 2025-10-26 clock-change days plus control days, to confirm the timestamps are UK local time. |

Use the live tools sparingly. Each run is a real login, and repeated failures trigger the CAPTCHA lock.

### Testing in Home Assistant

1. Copy `custom_components/northumbrian_water` into a test instance's `config/custom_components/` and restart.
2. Enable debug logging for `custom_components.northumbrian_water`.
3. Check **Developer tools > Statistics** for both statistic ids, and enable the two diagnostic sensors.
4. Reload the entry and confirm `hours_in_last_import` jumps to roughly 700 (the 30-day window), then drops to roughly 240 on the next routine refresh.
5. Download diagnostics and confirm nothing identifying survives redaction.

### Continuous integration

`.github/workflows/validate.yml` runs on push, on pull requests, weekly and on demand:

- **hassfest** validates `manifest.json`, translations and the rest of the integration layout.
- **HACS validation** is skipped while the repository is private, because `hacs/action` reads files over unauthenticated raw URLs. The `brands` check stays ignored until the domain is in `home-assistant/brands`.

The offline parsing tests are not wired into CI yet.

## Releasing

- Bump `version` in `manifest.json`.
- Keep `hacs.json`'s `homeassistant` minimum in step with the newest core API the code imports (currently `OptionsFlowWithReload`, 2025.8).
- `strings.json` and `translations/en.json` must stay identical.
