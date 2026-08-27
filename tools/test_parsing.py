"""Offline checks for the response parsing, using shapes captured from the site.

These need no credentials and no network access:

    python tools/test_parsing.py
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _load_api import load_api

_api = load_api()
NorthumbrianWaterClient = _api.NorthumbrianWaterClient
_decode_jwt_expiry = _api._decode_jwt_expiry
_is_unauthorized = _api._is_unauthorized
_loads = _api._loads

LONDON = ZoneInfo("Europe/London")
FAILURES: list[str] = []


def check(name: str, actual: object, expected: object) -> None:
    if actual == expected:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}: expected {expected!r}, got {actual!r}")
        FAILURES.append(name)


def test_hourly_summer() -> None:
    """A normal BST day: the portal's end-of-hour becomes a start-of-hour."""
    print("hourly readings, British Summer Time")
    payload = [
        {
            "Date": f"2026-08-14T{hour:02d}:00:00",
            "MonetaryValue": 0.07,
            "Key": "12:00 AM|1:00 AM",
            "LitreValue": hour,
            "ReadingType": 1,
            "Status": None,
        }
        for hour in range(1, 24)
    ] + [
        {
            "Date": "2026-08-15T00:00:00",
            "MonetaryValue": 0.0,
            "Key": "11:00 PM|12:00 AM",
            "LitreValue": 24,
            "ReadingType": 1,
            "Status": None,
        }
    ]

    readings = NorthumbrianWaterClient._parse_hourly(payload, date(2026, 8, 14))
    check("count", len(readings), 24)
    # The first entry ends at 01:00 local, so it covers midnight to 01:00, which
    # is 23:00 UTC the previous day during BST.
    check("first start", readings[0].start, datetime(2026, 8, 13, 23, 0, tzinfo=UTC))
    check("first local hour", readings[0].start.astimezone(LONDON).hour, 0)
    check("last start", readings[-1].start, datetime(2026, 8, 14, 22, 0, tzinfo=UTC))
    check("last local hour", readings[-1].start.astimezone(LONDON).hour, 23)
    check("hours are contiguous", len({r.start for r in readings}), 24)
    check("total litres", sum(r.litres for r in readings), sum(range(1, 25)))
    check("none estimated", any(r.estimated for r in readings), False)


def test_hourly_dst_autumn() -> None:
    """The autumn clock change, as the portal really reports it.

    On 2025-10-26 the UK went back an hour at 02:00 BST, so the local day was
    25 hours long -- but the portal returned only 24 rows, stamped 01:00 through
    to 00:00 the next day with no repeat. It publishes nothing for the repeated
    01:00-02:00 hour, so the correct result is 24 readings with a one hour gap
    rather than an invented 25th.

    This shape was taken from the live API, not assumed: an earlier version of
    this test fabricated a duplicated 02:00 stamp and asserted 25 readings, a
    day the portal never produces.
    """
    print("hourly readings, autumn clock change (real shape: 24 rows, 25h day)")
    stamps = [f"2025-10-26T{h:02d}:00:00" for h in range(1, 24)]
    payload = [
        {"Date": stamp, "LitreValue": 10, "MonetaryValue": 0.0, "ReadingType": 1}
        for stamp in stamps
    ] + [{"Date": "2025-10-27T00:00:00", "LitreValue": 10, "ReadingType": 1}]

    readings = NorthumbrianWaterClient._parse_hourly(payload, date(2025, 10, 26))
    check("count", len(readings), 24)
    check("distinct UTC hours", len({r.start for r in readings}), 24)
    check("no litres lost", sum(r.litres for r in readings), 240)
    # Local midnight BST is 23:00 UTC the day before.
    check("first start", readings[0].start, datetime(2025, 10, 25, 23, 0, tzinfo=UTC))
    check("first local hour", readings[0].start.astimezone(LONDON).hour, 0)
    check("last local hour", readings[-1].start.astimezone(LONDON).hour, 23)
    # The hour the portal omitted leaves exactly one gap, and nothing overlaps.
    starts = sorted(r.start for r in readings)
    gaps = [(b - a).total_seconds() / 3600 - 1 for a, b in pairwise(starts)]
    check("no overlapping hours", all(g >= 0 for g in gaps), True)
    check("exactly one missing hour", sum(gaps), 1)


def test_hourly_dst_autumn_duplicate_stamp() -> None:
    """A duplicated stamp must not collapse two hours into one.

    The portal has not been seen to do this -- it sends 24 rows for the 25 hour
    day -- but if it ever sent both halves of the repeated hour, the duplicate
    would be **01:00**, not 02:00:

        01:00-02:00 BST ends at 00:00 UTC, where the clock reads 01:00 BST
        01:00-02:00 GMT ends at 01:00 UTC, where the clock reads 01:00 GMT

    Both stamp as "01:00", and that is the genuinely ambiguous wall-clock hour,
    so ``fold`` can separate them. 02:00 is not ambiguous: the instant the clock
    reached 02:00 BST it became 01:00 GMT.
    """
    print("hourly readings, duplicated stamp (defensive)")
    stamps = ["01:00", "01:00"] + [f"{h:02d}:00" for h in range(2, 24)]
    payload = [
        {"Date": f"2025-10-26T{stamp}:00", "LitreValue": 10, "ReadingType": 1}
        for stamp in stamps
    ] + [{"Date": "2025-10-27T00:00:00", "LitreValue": 10, "ReadingType": 1}]

    readings = NorthumbrianWaterClient._parse_hourly(payload, date(2025, 10, 26))
    check("count", len(readings), 25)
    check("distinct UTC hours", len({r.start for r in readings}), 25)
    check("no litres lost", sum(r.litres for r in readings), 250)
    starts = [r.start for r in readings]
    check(
        "hours are contiguous",
        starts == [starts[0] + timedelta(hours=i) for i in range(25)],
        True,
    )
    # The two halves of the repeated hour land an hour apart, not on top of each
    # other, and the day still starts at local midnight.
    check("first start", starts[0], datetime(2025, 10, 25, 23, 0, tzinfo=UTC))
    check(
        "repeated hour, first half", starts[1], datetime(2025, 10, 26, 0, 0, tzinfo=UTC)
    )
    check(
        "repeated hour, second half",
        starts[2],
        datetime(2025, 10, 26, 1, 0, tzinfo=UTC),
    )


def test_hourly_dst_spring() -> None:
    """The spring clock change, as the portal really reports it.

    On 2026-03-29 the UK jumped from 01:00 GMT to 02:00 BST, so the local day
    was 23 hours. The portal's first row is stamped **02:00**, not 01:00: the
    hour 00:00-01:00 GMT ends at the instant the clock reads 02:00 BST.

    That stamp is why the interval end has to be resolved to an instant before
    an hour is subtracted. Taking the hour off the wall clock first asks for
    01:00 local, which did not occur that day, putting every reading an hour
    late and cascading collisions into the 30th. This shape came from the live
    API; an earlier version of this test assumed a 01:00 first stamp and so
    passed while the real day was broken.
    """
    print("hourly readings, spring clock change (real shape: 23 rows, 23h day)")
    stamps = [f"2026-03-29T{h:02d}:00:00" for h in range(2, 24)]
    payload = [
        {"Date": stamp, "LitreValue": 10, "ReadingType": 1} for stamp in stamps
    ] + [{"Date": "2026-03-30T00:00:00", "LitreValue": 10, "ReadingType": 1}]

    readings = NorthumbrianWaterClient._parse_hourly(payload, date(2026, 3, 29))
    check("count", len(readings), 23)
    check("distinct UTC hours", len({r.start for r in readings}), 23)
    check("no litres lost", sum(r.litres for r in readings), 230)
    # The day starts at local midnight in GMT, which is midnight UTC.
    check("first start", readings[0].start, datetime(2026, 3, 29, 0, 0, tzinfo=UTC))
    check("first local hour", readings[0].start.astimezone(LONDON).hour, 0)
    # The last hour must stay inside the 29th, not spill into the 30th.
    check("last start", readings[-1].start, datetime(2026, 3, 29, 22, 0, tzinfo=UTC))
    check("last local hour", readings[-1].start.astimezone(LONDON).hour, 23)
    check(
        "last reading is still the 29th locally",
        readings[-1].start.astimezone(LONDON).date(),
        date(2026, 3, 29),
    )
    starts = [r.start for r in readings]
    check(
        "hours are contiguous",
        starts == [starts[0] + timedelta(hours=i) for i in range(23)],
        True,
    )


def test_estimated_flag() -> None:
    print("estimated readings")
    payload = [
        {"Date": "2026-08-14T01:00:00", "LitreValue": 5, "ReadingType": 1},
        {"Date": "2026-08-14T02:00:00", "LitreValue": 5, "ReadingType": 2},
    ]
    readings = NorthumbrianWaterClient._parse_hourly(payload, date(2026, 8, 14))
    check("actual not flagged", readings[0].estimated, False)
    check("estimate flagged", readings[1].estimated, True)


def test_unauthorized_marker() -> None:
    """The live shape returned when the Id_token has expired."""
    print("in-band Unauthorized detection")
    expired = [
        {
            "Date": None,
            "MonetaryValue": 0.0,
            "Key": None,
            "LitreValue": 0,
            "ReadingType": 0,
            "Status": {"Code": "Unauthorized", "Message": None},
        }
    ]
    check("expired detected", _is_unauthorized(expired), True)
    check(
        "good payload not flagged",
        _is_unauthorized([{"Date": "2026-08-14T01:00:00", "Status": None}]),
        False,
    )
    check("empty list not flagged", _is_unauthorized([]), False)
    check(
        "unauthorized yields no readings",
        NorthumbrianWaterClient._parse_hourly(expired, date(2026, 8, 14)),
        [],
    )


def test_loads_unwraps_double_encoding() -> None:
    print("response decoding")
    check("plain array", _loads('[{"LitreValue": 1}]'), [{"LitreValue": 1}])
    check("double encoded", _loads('"[{\\"LitreValue\\": 1}]"'), [{"LitreValue": 1}])
    check("object", _loads('{"Id_token": "x"}'), {"Id_token": "x"})


def test_jwt_expiry() -> None:
    print("JWT expiry parsing")
    # {"exp": 1787187960} with the signature dropped; only the claim is read.
    token = "eyJhbGciOiJSUzI1NiJ9.eyJleHAiOjE3ODcxODc5NjB9.sig"
    check(
        "exp decoded",
        _decode_jwt_expiry(token),
        datetime.fromtimestamp(1787187960, tz=UTC),
    )
    check("garbage tolerated", _decode_jwt_expiry("not-a-jwt"), None)


def test_malformed_entries_skipped() -> None:
    print("malformed entries")
    payload = [
        {"Date": None, "LitreValue": 5},
        {"Date": "2026-08-14T01:00:00", "LitreValue": None},
        {"Date": "nonsense", "LitreValue": 5},
        {"Date": "2026-08-14T02:00:00", "LitreValue": 7, "ReadingType": 1},
    ]
    readings = NorthumbrianWaterClient._parse_hourly(payload, date(2026, 8, 14))
    check("only the valid row survives", len(readings), 1)
    check("value kept", readings[0].litres, 7.0)
    check(
        "non-list payload",
        NorthumbrianWaterClient._parse_hourly({}, date(2026, 8, 14)),
        [],
    )


def main() -> int:
    for test in (
        test_hourly_summer,
        test_hourly_dst_autumn,
        test_hourly_dst_autumn_duplicate_stamp,
        test_hourly_dst_spring,
        test_estimated_flag,
        test_unauthorized_marker,
        test_loads_unwraps_double_encoding,
        test_jwt_expiry,
        test_malformed_entries_skipped,
    ):
        test()
        print()

    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed: {', '.join(FAILURES)}")
        return 1
    print("All parsing checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
