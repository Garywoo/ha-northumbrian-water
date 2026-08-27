"""Verify the reverse-engineered Northumbrian Water API end to end.

Run this before installing the integration. It exercises the same client code
the integration uses, so a clean run here means the integration will work.

    python tools/probe.py

The password is read with getpass, is never echoed, and is not written anywhere.
Identifiers in the output are masked so the report is safe to share.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _load_api import load_api

_api = load_api()
NorthumbrianWaterClient = _api.NorthumbrianWaterClient
NWLError = _api.NWLError


def mask(value: str | None) -> str:
    """Show enough of an identifier to recognise it, without publishing it."""
    if not value:
        return "<none>"
    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"


async def run(email: str, password: str, days: int) -> int:
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = NorthumbrianWaterClient(session, email, password)

        print("1. Logging in ...")
        await client.async_login()
        print(f"   ok - PersonId {mask(client.person_id)}")

        print("2. Discovering accounts and meters ...")
        meters = await client.async_discover_meters()
        for meter in meters:
            print(
                f"   ok - account {mask(meter.account_id)}"
                f" / premise {mask(meter.premise_id)}"
                f" / meter {mask(meter.meter_serial)}"
            )
        meter = meters[0]

        # Check usage on this session too. It has browsed the account pages, so
        # if it works here but not on the cold session below, the difference is
        # server-side state rather than credentials.
        print("   checking usage on the discovery session ...")
        probe_day = datetime.now(ZoneInfo("Europe/London")).date() - timedelta(days=3)
        try:
            warm = await client.async_get_hourly(meter, probe_day)
            print(f"   ok - warm session returned {len(warm)} readings")
        except NWLError as err:
            print(f"   warm session failed: {err}")

    # Home Assistant starts cold: it logs in and goes straight to usage.
    # Reproduce that on a brand new session so the probe fails where it fails.
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = NorthumbrianWaterClient(session, email, password)

        print("3. Re-authenticating from cold, as the integration does ...")
        await client.async_bind_account(meter)
        print("   ok - logged in and bound to the account")
        # The usage endpoints authenticate off a server-set cookie, so which
        # cookies survived the login is the single most useful diagnostic.
        names = sorted({cookie.key for cookie in session.cookie_jar})
        print(f"   cookies held: {', '.join(names) if names else '(none)'}")

        print("4. Fetching hourly readings ...")
        # Match the portal's own day boundaries (UK local time).
        today = datetime.now(ZoneInfo("Europe/London")).date()
        total_days = 0
        total_litres = 0.0
        estimated_hours = 0
        first_day = None
        last_day = None
        empty_recent = []

        for offset in range(days, 0, -1):
            day = today - timedelta(days=offset)
            readings = await client.async_get_hourly(meter, day)
            if not readings:
                if offset <= 4:
                    empty_recent.append(day)
                else:
                    print(f"   {day}  no data published")
                continue
            day_litres = sum(r.litres for r in readings)
            estimated_hours += sum(1 for r in readings if r.estimated)
            total_days += 1
            total_litres += day_litres
            first_day = first_day or day
            last_day = day
            if offset <= 3 or offset == days:
                span = f"{readings[0].start:%Y-%m-%d %H:%M %Z}"
                print(
                    f"   {day}  {len(readings):2d} hours"
                    f"  {day_litres:7.1f} L   first hour starts {span}"
                )

        print()
        print("Summary")
        print(f"  days with data      : {total_days} of {days} requested")
        if first_day and last_day:
            print(f"  covered             : {first_day} .. {last_day}")
        print(f"  total consumption   : {total_litres:.1f} L")
        if total_days:
            print(f"  daily average       : {total_litres / total_days:.1f} L")
        print(f"  estimated hours     : {estimated_hours}")
        if empty_recent:
            lag = ", ".join(str(d) for d in empty_recent)
            print(f"  not published yet   : {lag}")
            print("  (a publishing lag of a day or two is normal)")

        if not total_days:
            print()
            print("No usage data was returned. The integration needs a smart meter")
            print("and at least some published history to import.")
            return 1

        print()
        print("All checks passed. The integration is safe to install.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email",
        default=os.environ.get("NWL_EMAIL"),
        help="portal email address (or set NWL_EMAIL)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="how many days of hourly data to sample (default 7)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show every request, status and cookie change",
    )
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="   [%(levelname)s] %(message)s",
            stream=sys.stdout,
        )

    email = args.email or input("Northumbrian Water email: ").strip()
    password = os.environ.get("NWL_PASSWORD") or getpass.getpass(
        "Northumbrian Water password (not echoed): "
    )
    if not email or not password:
        print("An email address and password are both required.", file=sys.stderr)
        return 2

    try:
        return asyncio.run(run(email, password, args.days))
    except NWLError as err:
        print(f"\nFailed: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
