"""Imports Northumbrian Water hourly readings as long-term statistics.

Home Assistant's Energy dashboard is driven by the statistics tables rather than
by entity state history, which is what makes it the right target here: the
portal publishes readings a day or two after the fact, so there is no live state
to record. Writing external statistics lets each reading land on the hour it
actually belongs to.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import partial

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    SITE_TZ,
    HourlyReading,
    MeterInfo,
    NorthumbrianWaterClient,
    NWLApiError,
    NWLAuthError,
    NWLSessionError,
)
from .const import (
    CURRENCY_GBP,
    DEFAULT_INITIAL_DAYS,
    DOMAIN,
    UPDATE_INTERVAL_HOURS,
)

_LOGGER = logging.getLogger(__name__)

# Home Assistant 2025.2 replaced the has_mean flag with a mean_type enum. Both
# spellings are accepted here so the integration works either side of that.
try:
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_METADATA: dict[str, object] = {"mean_type": StatisticMeanType.NONE}
except ImportError:  # pragma: no cover - cores older than 2025.2
    _MEAN_METADATA = {"has_mean": False}

# Statistics metadata must also name the unit converter class, or from
# 2026.11 the write is refused. "volume" is VolumeConverter.UNIT_CLASS, the
# converter the recorder itself selects for litres; the cost statistic is a
# currency, which has no converter, and the documented spelling of that is an
# explicit None. Cores that predate the field simply ignore the extra key.
_UNIT_CLASS_VOLUME = "volume"
_UNIT_CLASS_NONE = None


@dataclass(slots=True)
class ImportResult:
    """What the most recent refresh managed to import."""

    statistic_id: str
    hours_imported: int = 0
    days_with_data: int = 0
    latest_reading_start: datetime | None = None
    latest_day_litres: float | None = None
    total_litres: float | None = None
    cost_statistic_id: str | None = None
    latest_day_cost: float | None = None
    total_cost: float | None = None


def _slug(meter_serial: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in meter_serial.lower())


def statistic_id_for(meter_serial: str) -> str:
    """Return the external statistic id used for a meter's consumption."""
    return f"{DOMAIN}:water_{_slug(meter_serial)}"


def cost_statistic_id_for(meter_serial: str) -> str:
    """Return the external statistic id used for a meter's cost.

    Kept separate from consumption because the Energy dashboard wants two
    statistics per water source: one in litres, one in the local currency.
    """
    return f"{DOMAIN}:water_cost_{_slug(meter_serial)}"


class NorthumbrianWaterCoordinator(DataUpdateCoordinator[ImportResult]):
    """Fetches recent hourly readings and writes them into statistics."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: NorthumbrianWaterClient,
        meter: MeterInfo,
        lookback_days: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {meter.meter_serial}",
            update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS),
        )
        self.client = client
        self.meter = meter
        self.lookback_days = lookback_days
        self.statistic_id = statistic_id_for(meter.meter_serial)
        self.cost_statistic_id = cost_statistic_id_for(meter.meter_serial)
        self._did_initial_import = False

    async def _async_update_data(self) -> ImportResult:
        """Pull the recent window and reconcile it with what is already stored."""
        # The first run after a restart reaches back further, so a new install
        # arrives with usable history instead of a single day.
        window_days = self.lookback_days
        if not self._did_initial_import:
            window_days = max(self.lookback_days, DEFAULT_INITIAL_DAYS)

        try:
            readings = await self._async_fetch_window(window_days)
        except NWLSessionError as err:
            # Caught before NWLAuthError, which it subclasses. The client
            # rebuilds a dropped session in-band, so reaching here means the
            # rebuild failed too -- a portal fault, not a credential one.
            # Retrying quietly is right; prompting for a password is not.
            raise UpdateFailed(str(err)) from err
        except NWLAuthError as err:
            # Surfaces in the UI as a re-authentication prompt rather than as a
            # generic failure, since only new credentials can fix it.
            raise ConfigEntryAuthFailed(str(err)) from err
        except NWLApiError as err:
            raise UpdateFailed(str(err)) from err

        self._did_initial_import = True

        if not readings:
            _LOGGER.debug(
                "No readings published in the last %s days for meter %s",
                window_days,
                self.meter.meter_serial,
            )
            return self.data or ImportResult(statistic_id=self.statistic_id)

        baseline = await self._async_baseline_sum(readings[0].start)
        cost_baseline = await self._async_baseline_sum(
            readings[0].start, self.cost_statistic_id
        )
        await self._async_write_statistics(readings, baseline)
        await self._async_write_cost_statistics(readings, cost_baseline)
        return self._summarise(readings, baseline, cost_baseline)

    async def _async_fetch_window(self, window_days: int) -> list[HourlyReading]:
        """Fetch hourly readings for the window.

        A day the portal reports as empty is simply absent from the result. That
        is normal for the most recent day or two and is stable between runs, so
        the recomputed sums stay consistent.

        A failed request is deliberately *not* swallowed. Dropping a day we had
        previously imported would lower every sum after it, which the Energy
        dashboard reads as a meter reset. Aborting the whole refresh and
        retrying later leaves the stored statistics untouched instead.
        """
        # Day boundaries follow the portal, which reports UK local time. Using
        # Home Assistant's own timezone would ask for the wrong day whenever the
        # two disagree.
        today = datetime.now(SITE_TZ).date()
        readings: list[HourlyReading] = []
        for offset in range(window_days, -1, -1):
            day: date = today - timedelta(days=offset)
            day_readings = await self.client.async_get_hourly(self.meter, day)
            readings.extend(day_readings)
        readings.sort(key=lambda item: item.start)
        return readings

    async def _async_baseline_sum(
        self, first_hour: datetime, statistic_id: str | None = None
    ) -> float:
        """Return the running sum recorded immediately before ``first_hour``.

        Re-importing a window means recomputing its cumulative sums, and those
        have to continue from whatever total already stands before the window
        starts, or the Energy dashboard would show a step change.
        """
        statistic_id = statistic_id or self.statistic_id
        lookup_start = first_hour - timedelta(days=DEFAULT_INITIAL_DAYS)
        stats = await get_instance(self.hass).async_add_executor_job(
            partial(
                statistics_during_period,
                self.hass,
                lookup_start,
                first_hour,
                {statistic_id},
                "hour",
                None,
                {"sum"},
            )
        )
        rows = stats.get(statistic_id) or []
        if not rows:
            return 0.0
        last_sum = rows[-1].get("sum")
        return float(last_sum) if last_sum is not None else 0.0

    async def _async_write_statistics(
        self, readings: list[HourlyReading], baseline: float
    ) -> None:
        """Write the window as external statistics."""
        metadata: StatisticMetaData = {
            **_MEAN_METADATA,  # type: ignore[typeddict-item]
            "has_sum": True,
            "name": f"Water consumption ({self.meter.meter_serial})",
            "source": DOMAIN,
            "statistic_id": self.statistic_id,
            "unit_class": _UNIT_CLASS_VOLUME,
            "unit_of_measurement": UnitOfVolume.LITERS,
        }

        running = baseline
        statistics: list[StatisticData] = []
        for reading in readings:
            running += reading.litres
            statistics.append(
                StatisticData(
                    start=reading.start,
                    state=reading.litres,
                    sum=running,
                )
            )

        async_add_external_statistics(self.hass, metadata, statistics)
        _LOGGER.debug(
            "Imported %s hourly statistics for %s (%.1f L in window)",
            len(statistics),
            self.statistic_id,
            running - baseline,
        )

    async def _async_write_cost_statistics(
        self, readings: list[HourlyReading], baseline: float
    ) -> None:
        """Write the window's cost as a second external statistic.

        The portal returns a ``MonetaryValue`` alongside every hourly reading,
        so the Energy dashboard's optional cost tracker can be fed from the
        supplier's own figures rather than from a rate we guess at.

        Each hourly value is rounded to the penny at source, so an hour using
        only a litre or two reports 0.00 and its fraction of a penny is lost.
        The running total therefore drifts slightly below the portal's own daily
        cost. Summing what the portal actually reports is still preferable to
        inventing a unit rate, which would go stale at the next tariff change.
        """
        currency = self.hass.config.currency or CURRENCY_GBP
        if currency != CURRENCY_GBP:
            # The figures are GBP whatever the instance is set to. Reporting
            # them as another currency would be a lie, and the Energy dashboard
            # rejects a cost statistic whose unit is not the instance currency,
            # so say so rather than failing silently.
            _LOGGER.warning(
                "Northumbrian Water bills in %s but this Home Assistant uses "
                "%s, so the water cost statistic will not be accepted by the "
                "Energy dashboard",
                CURRENCY_GBP,
                currency,
            )

        metadata: StatisticMetaData = {
            **_MEAN_METADATA,  # type: ignore[typeddict-item]
            "has_sum": True,
            "name": f"Water cost ({self.meter.meter_serial})",
            "source": DOMAIN,
            "statistic_id": self.cost_statistic_id,
            "unit_class": _UNIT_CLASS_NONE,
            "unit_of_measurement": CURRENCY_GBP,
        }

        running = baseline
        statistics: list[StatisticData] = []
        for reading in readings:
            running += reading.cost
            statistics.append(
                StatisticData(
                    start=reading.start,
                    state=reading.cost,
                    # Costs are pennies; carrying float error into a cumulative
                    # sum would show up as fractions of a penny in the UI.
                    sum=round(running, 2),
                )
            )

        async_add_external_statistics(self.hass, metadata, statistics)
        _LOGGER.debug(
            "Imported %s hourly cost statistics for %s (%.2f %s in window)",
            len(statistics),
            self.cost_statistic_id,
            running - baseline,
            CURRENCY_GBP,
        )

    def _summarise(
        self, readings: list[HourlyReading], baseline: float, cost_baseline: float
    ) -> ImportResult:
        latest = readings[-1]
        # Group by the portal's own day boundaries so the daily total lines up
        # with what the website shows.
        by_day: dict[date, float] = defaultdict(float)
        cost_by_day: dict[date, float] = defaultdict(float)
        for reading in readings:
            local_day = reading.start.astimezone(SITE_TZ).date()
            by_day[local_day] += reading.litres
            cost_by_day[local_day] += reading.cost
        days = set(by_day)

        # Report the most recent *complete* day. British Summer Time puts the
        # final reading of a UTC day into the next local day, so the newest day
        # in the window is routinely a single hour -- reporting that would show
        # a near-zero total that looks like a fault. Fall back to the newest day
        # only when nothing complete has been published yet.
        hours_per_day: Counter[date] = Counter(
            reading.start.astimezone(SITE_TZ).date() for reading in readings
        )
        complete = [day for day, hours in hours_per_day.items() if hours >= 24]
        latest_day = max(complete) if complete else max(days)
        latest_day_litres = by_day[latest_day]
        return ImportResult(
            statistic_id=self.statistic_id,
            cost_statistic_id=self.cost_statistic_id,
            latest_day_cost=round(cost_by_day[latest_day], 2),
            total_cost=round(cost_baseline + sum(r.cost for r in readings), 2),
            hours_imported=len(readings),
            days_with_data=len(days),
            latest_reading_start=latest.start,
            latest_day_litres=round(latest_day_litres, 1),
            total_litres=round(baseline + sum(r.litres for r in readings), 1),
        )
