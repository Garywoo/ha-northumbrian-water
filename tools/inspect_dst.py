"""Work out whether the portal's hourly timestamps are UK local time or UTC.

Both readings of the data explain what we see in August, but they need opposite
fixes in winter, so guessing is not good enough. The clock-change days settle it,
because a UK local day is not 24 hours long on either of them:

    spring forward   local day has 23 hours (01:00 does not exist)
    autumn back      local day has 25 hours (01:00 happens twice)

    if a clock-change day returns 24 rows      -> timestamps are UTC
    if it returns 23 or 25 rows, or repeats an
    hour, or skips one                         -> timestamps are UK local time

Normal days are fetched too, as a control: they must return 24 either way, so a
control that misbehaves means something else is wrong and the verdict is void.

    python tools/inspect_dst.py

The password is read with getpass, is never echoed, and is not written anywhere.
Output contains no identifiers, so it is safe to share.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _load_api import load_api

_api = load_api()
NorthumbrianWaterClient = _api.NorthumbrianWaterClient
NWLError = _api.NWLError
SITE_TZ = ZoneInfo("Europe/London")

# Clock changes are the last Sunday of March and October.
PROBES: tuple[tuple[date, str, int], ...] = (
    (date(2026, 3, 29), "spring forward", 23),
    (date(2025, 10, 26), "autumn back", 25),
    (date(2026, 3, 22), "control, a week earlier", 24),
    (date(2025, 10, 19), "control, a week earlier", 24),
)


def describe(rows: list[dict]) -> tuple[int, str, str, list[str]]:
    """Return the row count, first and last raw stamp, and any oddities."""
    stamps: list[datetime] = []
    for row in rows:
        raw = row.get("Date")
        if not raw:
            continue
        try:
            stamps.append(datetime.fromisoformat(str(raw)))
        except ValueError:
            continue
    if not stamps:
        return 0, "-", "-", ["no parseable timestamps"]

    notes: list[str] = []
    seen: set[datetime] = set()
    for stamp in stamps:
        if stamp in seen:
            notes.append(f"repeats {stamp:%H:%M}")
        seen.add(stamp)
    for earlier, later in pairwise(stamps):
        gap = (later - earlier).total_seconds() / 3600
        if gap != 1:
            notes.append(f"{gap:+.0f}h step at {earlier:%H:%M}")
    return (
        len(stamps),
        f"{stamps[0]:%Y-%m-%d %H:%M}",
        f"{stamps[-1]:%Y-%m-%d %H:%M}",
        notes,
    )


async def run(email: str, password: str) -> int:
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        client = NorthumbrianWaterClient(session, email, password)
        meters = await client.async_discover_meters()
        meter = meters[0]
        await client.async_bind_account(meter)

        # Confirm what the daily endpoint actually covers, since it appears to
        # ignore StartDate and only serve recent days.
        daily = await client.async_get_daily_totals(meter, date(2026, 1, 12))
        if daily:
            days = sorted(day for day, _, _ in daily)
            print(
                f"Daily endpoint asked for 2026-01-12, returned "
                f"{days[0]} .. {days[-1]} ({len(days)} days)"
            )
        print()

        results: list[tuple[date, str, int, int, str, str, list[str]]] = []
        for day, label, local_hours in PROBES:
            try:
                rows = await client.async_get_hourly_raw(meter, day)
            except NWLError as err:
                print(f"{day}  {label}: failed ({err})")
                continue
            if not isinstance(rows, list):
                print(f"{day}  {label}: unexpected response shape")
                continue
            count, first, last, notes = describe(rows)
            results.append((day, label, local_hours, count, first, last, notes))

    if not results:
        print("No data came back for any probe day; cannot decide.")
        return 1

    print(f"{'day':<12}{'what':<26}{'local hrs':>10}{'rows':>6}   first .. last")
    print("-" * 78)
    for day, label, local_hours, count, first, last, notes in results:
        print(f"{day!s:<12}{label:<26}{local_hours:>10}{count:>6}   {first} .. {last}")
        for note in notes:
            print(f"{'':<54}   ! {note}")

    changes = [r for r in results if r[2] != 24]
    controls = [r for r in results if r[2] == 24]
    print()
    print("Verdict")

    bad_control = [r for r in controls if r[3] not in (0, 24)]
    if bad_control:
        print("  Control days did not return 24 rows, so this test is void.")
        return 1
    usable = [r for r in changes if r[3] > 0]
    if not usable:
        print("  No clock-change day returned data, so the question is unresolved.")
        print("  The portal may not keep history that far back.")
        return 1

    utc_like = all(count == 24 and not notes for _, _, _, count, _, _, notes in usable)
    local_like = all(
        count == local_hours or notes
        for _, _, local_hours, count, _, _, notes in usable
    )
    if utc_like:
        print("  Every clock-change day returned a clean 24 rows.")
        print("  -> The timestamps are UTC. Parse them as UTC and keep treating")
        print("     them as interval ends.")
    elif local_like:
        print("  Clock-change days follow the local calendar, not a 24-hour grid.")
        print("  -> The timestamps are UK local time, and are interval STARTS.")
        print("     Stop subtracting an hour.")
    else:
        print("  The days disagree with both readings; inspect the rows above.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", default=os.environ.get("NWL_EMAIL"))
    args = parser.parse_args()

    email = args.email or input("Northumbrian Water email: ").strip()
    password = os.environ.get("NWL_PASSWORD") or getpass.getpass(
        "Northumbrian Water password (not echoed): "
    )
    if not email or not password:
        print("An email address and password are both required.", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(email, password))
    except NWLError as err:
        print(f"\nFailed: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
