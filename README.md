# Northumbrian Water for Home Assistant

Pulls hourly water consumption from the Northumbrian Water customer portal
(`nwl.co.uk`) and writes it into Home Assistant's long-term statistics, so it
appears in the Energy dashboard's water section with each reading on the hour it
actually belongs to.

Hourly is the finest granularity the portal exposes: it is what the website
itself requests when you click a bar in the daily usage graph.

## Requirements

- A Northumbrian Water online account with a **smart meter**. Without one the
  portal publishes no hourly data and there is nothing to import.
- Home Assistant **2024.11 or newer** (the config flow uses reauth helpers
  introduced in that release), with the `recorder` integration enabled, which it
  is by default.

## Install

### HACS (recommended)

1. In HACS, open the overflow menu (⋮) and choose **Custom repositories**.
2. Add `https://github.com/Garywoo/ha-northumbrian-water` with type
   **Integration**.
3. Search HACS for **Northumbrian Water**, download it, and restart Home
   Assistant.
4. Go to **Settings > Devices & services > Add integration** and search for
   **Northumbrian Water**.
5. Enter the email address and password you use on the website.

### Manual

1. Copy `custom_components/northumbrian_water` into your Home Assistant
   `config/custom_components/` directory, so you end up with
   `config/custom_components/northumbrian_water/manifest.json`.
2. Restart Home Assistant.
3. Continue from step 4 above.

Setup discovers the account, premise and meter serial automatically. If the
login covers more than one meter you get to pick one; add the integration again
to track another.

### Add it to the Energy dashboard

Go to **Settings > Dashboards > Energy > Water consumption > Add water source**
and pick **Water consumption &lt;meter serial&gt;** — the statistic whose id is
`northumbrian_water:water_<meter serial>`. It is in litres.

When it then offers to track costs, pick **Water cost &lt;meter serial&gt;**
(`northumbrian_water:water_cost_<meter serial>`). The portal returns a cost
alongside every hourly reading, so this is the supplier's own figure rather than
a rate guessed at locally, and it needs no maintenance when the tariff changes.

Pick the *statistic*, not one of this integration's sensors. The picker also
lists ordinary entities, and choosing `sensor.*_last_full_day_usage` gives the
dashboard a daily total it cannot interpret; HA then reports *"Last reset
missing"* and charts nothing. The consumption history is not an entity at all
(see [What it creates](#what-it-creates)) — it goes straight into the statistics tables.

## Verify before installing

`tools/probe.py` exercises the same client code the integration uses, so a clean
run means the integration will work. It needs `aiohttp` and nothing else.

```bash
python tools/probe.py --days 7
```

It prompts for the password (never echoed, never written to disk) and masks the
account identifiers in its output, so the report is safe to paste into a chat or
an issue.

`tools/test_parsing.py` checks the response parsing, including both clock
changes, and needs no credentials and no network:

```bash
python tools/test_parsing.py
```

## How it behaves

**Readings arrive late.** The portal publishes smart meter reads a day or two
after the fact, and occasionally backfills an hour it had previously reported as
empty. The integration therefore refreshes every 6 hours and re-imports a
rolling window (10 days by default, adjustable under the integration's
**Configure** button), recomputing the running total each time so corrections
land instead of being lost. The first refresh after a restart reaches back 30
days.

**Nothing is invented.** A day the portal has no data for is skipped rather than
recorded as zero, so today and yesterday simply appear once they are published.
If a request fails outright the whole refresh is abandoned and retried later,
because dropping a day that had already been imported would lower every
subsequent total and the Energy dashboard would read that as a meter reset.

**Clock changes are handled.** The portal reports the *end* of each hour as a
naive UK wall-clock time, so it is resolved to an instant before an hour is
subtracted — doing it the other way round breaks the spring change, where the
first row of the day is stamped 02:00 because 00:00–01:00 GMT ends when the
clock reads 02:00 BST. In March that gives a 23 hour day. In October the portal
returns 24 rows for the 25 hour day, publishing nothing for one half of the
repeated hour, so that day has a real one hour gap. Both shapes were taken from
the live API and are covered by tests.

**Costs are rounded to the penny per hour, so the total runs slightly low.**
The portal rounds each hourly `MonetaryValue` to two decimals, so an hour using
a litre or two reports `0.00` and its fraction of a penny is lost. Measured over
a week on one account the shortfall was 0.6p a day — about £2 a year, or 0.61%
of the volumetric charge. Summing what the portal actually reports is kept
because those are the supplier's own figures; the alternative is multiplying
litres by a unit rate, which is exact but goes stale at the next tariff change.
`python tools/compare_daily.py` prints the shortfall for your own account, along
with the implied £/m³ if you would rather switch.

Note that this is the volumetric charge only, as shown on the usage graph. It
excludes standing charges, so it is not a bill forecast.

**Daily totals can differ from the website by up to one hour's usage.** The
portal's daily graph buckets consumption by *UTC* day while its hourly data is
stamped in UK local time, so during British Summer Time its own two views
disagree by exactly the 00:00–01:00 local hour; in winter they agree. This
integration attributes each hour to the local day it falls in, which is what the
Energy dashboard expects, so it follows the hourly data rather than reproducing
the daily graph. Run `python tools/compare_daily.py` to see the comparison for
your own account.

## A note on sessions

The portal's smart-metering refresh token is **single use** and rotates on every
call. The website stores its copy in browser local storage, so if that copy is
spent by something else the site's hourly graphs stop opening until you log out
and back in. The integration is resilient to this on its own side: when a
rotation fails it logs in again and carries on.

Because each login gets its own token chain, the integration and your browser
should not interfere with each other. If you do find that opening the usage
graph in a browser stops working shortly after the integration refreshes, log
out and back in on the website and open an issue, because that would mean the
chain is shared per account rather than per login.

Repeated *failed* logins cause the portal to lock the account and start
demanding a CAPTCHA, which no integration can answer. If the password changes,
Home Assistant will prompt you to sign in again rather than retrying in a loop.

## What it creates

The config entry is titled `Account Number <account number>`, so the Integrations page
identifies which account is being billed. One device per meter, named
`NWL Water Meter (<serial>)`, carrying:

| Entity | Purpose |
| --- | --- |
| `sensor.*_last_reading_time` | Timestamp of the most recent hour imported — how far the data actually reaches. Diagnostic, **disabled by default**. |
| `sensor.*_last_full_day_usage` | Total for the most recent **complete** day, in litres. No state class on purpose, so the Energy dashboard does not offer it as a water source. |
| `sensor.*_last_full_day_cost` | Cost of the most recent complete day, in GBP. No state class, same as above. |
| `sensor.*_hours_in_last_import` | Hours written by the last refresh, with both statistic ids as attributes. Diagnostic, **disabled by default**. |

The two diagnostics are off unless you turn them on, from the entity's settings
on the device page. They are worth enabling if the Energy dashboard ever looks
wrong: `last_reading_time` shows whether the portal is still publishing, and
`hours_in_last_import` shows whether the last refresh wrote anything. Note that
`hours_in_last_import` counts the last refresh's *window*, not the whole history, so it
reads around 700 just after a reload (30 day re-import) and around 240 on
routine refreshes (11 day window). A drop is normal, not lost data.

The consumption history itself is not an entity. It lives in the statistics
table under `northumbrian_water:water_<meter serial>`, which is what the Energy
dashboard reads.

## The API, for reference

Everything below was reverse engineered from the site's network traffic and its
own JavaScript. All calls need the header `X-Requested-With: XMLHttpRequest` —
without it the site answers 401 — and must share one cookie jar, primed by
loading a page first so the site issues its session and Azure affinity cookies.

| Step | Call | Notes |
| --- | --- | --- |
| 1 | `POST /api/Auth/Login` | `{email, password}` → `Response.{access_token, expires_in, refresh_token, Profile}` |
| 2 | `POST /api/Auth/SaveUserProfile` | Hands the tokens back so the server binds them to the session; returns the profile and `PersonId` |
| 3 | `GET /api/Customer/GetAccountSummary?personId=` | → `Accounts[].{accountIDField, premiseIDField, propertyAddressField}` |
| 4 | `POST /api/Customer/AddOrUpdateCustomerSession` | `["PersonId:x", "AccountId:y"]`, sets the active account in the session |
| 5 | `POST /api/Customer/GetAccountDetails` | `{AccountId, PremiseId, PersonId}` → `Meters[].BadgeNumber` is the meter serial |
| 6 | `POST /api/Customer/GetSmartAuthToken` | `{refresh_Token}` → `{Id_token, Refresh_token, Expires_in}`; single use, rotates |
| 7 | `POST /api/Customer/GetHourlyWaterUsage` | `{AccountId, Authorization: Id_token, MeterSerial, StartDate}` → 24 readings |

Three traps worth recording:

- **Steps 3 to 5 are mandatory, even though they look like discovery.** The
  usage endpoints depend on state the portal writes into the session while
  answering them. Log in and call step 7 directly and it returns a hard 401
  however valid the token is; walk the account pages first and the identical
  request succeeds. A re-login has to repeat them, not just re-select the
  account. This is the single most confusing thing about the API: the failure
  looks like an authentication problem and is really a missing-state problem.
- **The advertised token lifetime is wrong.** Step 6 returns
  `Expires_in: 3598`, but the `Id_token` JWT's own `exp` claim is 600 seconds.
  The website works around this by retrying whenever a usage call comes back
  unauthorised. This client reads the JWT's `exp` instead, and still retries
  once as a backstop.
- **Expiry is reported in-band.** An expired token does not produce a 401. Step
  7 answers HTTP 200 with a single-element array whose `Status.Code` is
  `Unauthorized`, so the body has to be inspected.

The same endpoints exist for other granularities and take an identical payload:
`GetDailyWaterUsage`, `GetWeeklyWaterUsage`, `GetMonthlyWaterUsage` and
`GetYearlyWaterUsage`. `StartDate` behaves differently per endpoint — for hourly
it is the day to report on; for weekly and monthly the site passes a date far in
the past and gets the most recent 12 buckets back.

Essex & Suffolk Water runs the same platform on a different hostname, so
pointing `BASE_URL` in `api.py` at it would likely work, but this has not been
tested.

## Troubleshooting

Turn on debug logging:

```yaml
logger:
  default: warning
  logs:
    custom_components.northumbrian_water: debug
```

Then download diagnostics from the integration's overflow menu; identifiers are
redacted.

- **"Sign-in was refused"** — check the credentials on the website first. After
  several failed attempts the portal requires a CAPTCHA that this integration
  cannot answer; wait, then sign in on the website to clear it.
- **No data in the Energy dashboard** — check `sensor.*_hours_in_last_import`. If it
  is 0, the portal has not published anything yet for the window.
- **Nothing at all for a new smart meter** — the portal takes a while to start
  publishing after installation.

## Unofficial

This is not affiliated with or endorsed by Northumbrian Water. It relies on
undocumented endpoints that can change without warning.
