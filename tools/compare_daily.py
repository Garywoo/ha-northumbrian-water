"""Compare the portal's daily totals against its own hourly readings.

The website's daily usage graph does not always agree with the sum of the hourly
readings it serves for the same day, so the Energy dashboard can differ from the
app by a few litres. This works out which convention the daily graph uses, over
enough days to tell a rule from a coincidence.

    python tools/compare_daily.py --days 30

For each day it prints:

  same-day    sum of the hourly buckets from 00:00 to 23:00 local. This is what
              the integration imports, and it matches how the portal labels its
              own hourly bars (the first bucket of a day is keyed "12:00am").
  shifted     sum of the buckets from 01:00 to 00:00 the next day, i.e. every
              reading treated as belonging to the hour after the one the portal
              labels it with.

The password is read with getpass, is never echoed, and is not written anywhere.
Output contains no identifiers, so it is safe to share.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _load_api import load_api

_api = load_api()
NorthumbrianWaterClient = _api.NorthumbrianWaterClient
NWLError = _api.NWLError
SITE_TZ = ZoneInfo("Europe/London")


async def run(email: str, password: str, days: int, start: date | None) -> int:
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = NorthumbrianWaterClient(session, email, password)
        meters = await client.async_discover_meters()
        meter = meters[0]
        await client.async_bind_account(meter)

        if start is not None:
            first = start
        else:
            first = datetime.now(SITE_TZ).date() - timedelta(days=days)

        # One extra day: the shifted total for the last day needs the first
        # bucket of the day after it.
        litres_at: dict[datetime, float] = {}
        cost_at: dict[datetime, float] = {}
        for offset in range(days + 1):
            day = first + timedelta(days=offset)
            for reading in await client.async_get_hourly(meter, day):
                local = reading.start.astimezone(SITE_TZ)
                litres_at[local] = reading.litres
                cost_at[local] = reading.cost

        rows = await client.async_get_daily_totals(meter, first)
        portal_daily = {day: litres for day, litres, _ in rows}
        portal_cost = {day: cost for day, _, cost in rows}
        # The daily endpoint ignores StartDate and serves only a recent window,
        # so say what was actually compared rather than implying --days were.
        requested = {first + timedelta(days=n) for n in range(days)}
        covered = sorted(requested & set(portal_daily))
        if len(covered) < days:
            print()
            print(
                f"Note: asked for {days} day(s) from {first}, but the daily "
                f"endpoint only covers {len(covered)}"
                + (f" ({covered[0]} .. {covered[-1]})" if covered else "")
                + "."
            )
            print(
                "      It ignores StartDate and returns only recent days, so "
                "older days cannot be compared."
            )
        if not portal_daily:
            print("The daily endpoint returned nothing; cannot compare.")
            return 1

    def window(day: date, start_hour: int) -> tuple[float, int]:
        total, found = 0.0, 0
        base = datetime.combine(day, datetime.min.time(), tzinfo=SITE_TZ)
        for step in range(24):
            hour = base + timedelta(hours=start_hour + step)
            if hour in litres_at:
                total += litres_at[hour]
                found += 1
        return round(total, 1), found

    def cost_window(day: date, start_hour: int) -> float:
        total = 0.0
        base = datetime.combine(day, datetime.min.time(), tzinfo=SITE_TZ)
        for step in range(24):
            total += cost_at.get(base + timedelta(hours=start_hour + step), 0.0)
        return round(total, 2)

    offsets = {
        datetime.combine(
            first + timedelta(days=n), datetime.min.time(), tzinfo=SITE_TZ
        ).strftime("%Z")
        for n in range(days)
    }
    print()
    print(f"UK clock over this window: {', '.join(sorted(offsets))}")
    print(f"{'day':<12}{'portal':>9}{'same-day':>10}{'shifted':>9}   matches")
    print("-" * 55)

    tally: defaultdict[str, int] = defaultdict(int)
    for offset in range(days):
        day = first + timedelta(days=offset)
        if day not in portal_daily:
            continue
        portal = round(portal_daily[day], 1)
        same, n_same = window(day, 0)
        shifted, n_shifted = window(day, 1)
        if n_same < 24 or n_shifted < 24:
            # A partial day cannot settle anything either way.
            print(f"{day!s:<12}{portal:9.0f}{same:10.0f}{shifted:9.0f}   (partial)")
            tally["partial"] += 1
            continue

        same_ok = abs(same - portal) < 0.5
        shifted_ok = abs(shifted - portal) < 0.5
        if same_ok and shifted_ok:
            verdict = "both (identical)"
            tally["both"] += 1
        elif same_ok:
            verdict = "same-day"
            tally["same-day"] += 1
        elif shifted_ok:
            verdict = "shifted"
            tally["shifted"] += 1
        else:
            verdict = f"neither (off by {same - portal:+.0f} / {shifted - portal:+.0f})"
            tally["neither"] += 1
        print(f"{day!s:<12}{portal:9.0f}{same:10.0f}{shifted:9.0f}   {verdict}")

    # Cost is reported per hour rounded to the penny, so hours using a litre or
    # two report 0.00 and their fraction is lost. Compare against the portal's
    # own daily cost over the window it actually uses (shifted) to isolate that
    # rounding from the day-boundary difference above.
    print()
    print(f"{'day':<12}{'portal £':>10}{'hourly £':>10}{'lost':>8}{'implied £/m3':>14}")
    print("-" * 55)
    lost_total = 0.0
    counted = 0
    for offset in range(days):
        day = first + timedelta(days=offset)
        if day not in portal_cost:
            continue
        _, n_shifted = window(day, 1)
        if n_shifted < 24:
            continue
        pcost = round(portal_cost[day], 2)
        hcost = cost_window(day, 1)
        lost = round(pcost - hcost, 2)
        lost_total += lost
        counted += 1
        plitres = portal_daily.get(day) or 0.0
        rate = (pcost / plitres * 1000) if plitres else 0.0
        print(f"{day!s:<12}{pcost:10.2f}{hcost:10.2f}{lost:8.2f}{rate:14.2f}")
    if counted:
        per_day = lost_total / counted
        print()
        print(
            f"  rounding loses {lost_total:.2f} over {counted} day(s)"
            f" = {per_day:.3f}/day, about {per_day * 365:.2f}/year"
        )

    print()
    print("Verdict")
    for key in ("same-day", "shifted", "both", "neither", "partial"):
        if tally[key]:
            print(f"  {key:<18} {tally[key]} day(s)")

    decisive = tally["same-day"] + tally["shifted"]
    if decisive and not tally["neither"]:
        winner = "shifted" if tally["shifted"] > tally["same-day"] else "same-day"
        if tally[winner] == decisive:
            print()
            print(f"  The daily graph consistently uses the '{winner}' convention.")
    elif tally["neither"]:
        print()
        print("  Neither convention explains every day, so the daily figures are")
        print("  not a plain re-bucketing of the hourly data.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default=os.environ.get("NWL_EMAIL"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument(
        "--start",
        help=(
            "first day to compare, YYYY-MM-DD (default: --days ago). Use a "
            "winter date to test the convention outside British Summer Time."
        ),
    )
    args = parser.parse_args()

    start: date | None = None
    if args.start:
        try:
            start = date.fromisoformat(args.start)
        except ValueError:
            print(f"--start must be YYYY-MM-DD, not {args.start!r}", file=sys.stderr)
            return 2

    email = args.email or input("Northumbrian Water email: ").strip()
    password = os.environ.get("NWL_PASSWORD") or getpass.getpass(
        "Northumbrian Water password (not echoed): "
    )
    if not email or not password:
        print("An email address and password are both required.", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(email, password, args.days, start))
    except NWLError as err:
        print(f"\nFailed: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
