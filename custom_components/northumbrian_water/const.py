"""Constants for the Northumbrian Water integration."""

from __future__ import annotations

DOMAIN = "northumbrian_water"

CONF_ACCOUNT_ID = "account_id"
CONF_PREMISE_ID = "premise_id"
CONF_PERSON_ID = "person_id"
CONF_METER_SERIAL = "meter_serial"
CONF_ADDRESS = "address"
CONF_LOOKBACK_DAYS = "lookback_days"

# The portal only publishes smart-meter reads after a delay of a couple of days,
# and occasionally backfills an hour it previously reported as empty. Every
# refresh therefore re-imports this many days and recomputes the running sum, so
# late arrivals correct themselves instead of being lost.
DEFAULT_LOOKBACK_DAYS = 10

# How far back the initial import reaches on first setup.
DEFAULT_INITIAL_DAYS = 30

# The portal serves naive local timestamps.
SITE_TIMEZONE = "Europe/London"

# Northumbrian Water bills in sterling, so the cost statistic is always GBP
# regardless of what this Home Assistant instance is configured to use.
CURRENCY_GBP = "GBP"

UPDATE_INTERVAL_HOURS = 6
