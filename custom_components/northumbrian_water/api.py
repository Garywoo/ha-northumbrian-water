"""Client for the Northumbrian Water customer portal API.

Reverse engineered from the nwl.co.uk account web app. The portal is an ASP.NET
site that proxies a LoginRadius identity provider and an internal smart-metering
service. Nothing here depends on Home Assistant, so it can be exercised
standalone (see tools/probe.py).

The call chain the web app uses, and that this client reproduces:

1. ``POST /api/Auth/Login``                     {email, password}
       -> {"Response": {access_token, expires_in, refresh_token, Profile}}
2. ``POST /api/Auth/SaveUserProfile``           {access_token, expires_in, refresh_token}
       -> profile, incl. PersonId and a refresh_token; also establishes the
          server-side session tied to the cookie jar.
3. ``GET  /api/Customer/GetAccountSummary?personId=``
       -> {"Status": {...}, "Accounts": [{accountIDField, premiseIDField, ...}]}
4. ``POST /api/Customer/AddOrUpdateCustomerSession``  ["PersonId:x", "AccountId:y"]
       -> stores the active account in the server-side session.
5. ``POST /api/Customer/GetAccountDetails``     {AccountId, PremiseId, PersonId}
       -> account detail incl. Meters[].BadgeNumber (the meter serial).

   Steps 3 to 5 are not only for discovery: they are what makes the session
   able to answer step 7 at all. See the note on session state below.
6. ``POST /api/Customer/GetSmartAuthToken``     {access_token}
       -> {Access_token, Refresh_token, Id_token, Expires_in, ...}
          Id_token is the JWT used to authorise usage calls. Both tokens rotate
          on every call, so the response supersedes what was sent.
7. ``POST /api/Customer/GetHourlyWaterUsage``   {AccountId, Authorization, MeterSerial, StartDate}
       -> 24 hourly readings for that day.

Behaviours worth knowing, each established by experiment against the live site:

* **The usage endpoints depend on server-side session state, not just on
  credentials.** Logging in and calling them straight away returns a hard 401
  however good the token is. The portal only writes the state they need while
  answering the calls its account page makes on load, so a session has to walk
  ``GetAccountSummary`` -> ``AddOrUpdateCustomerSession`` ->
  ``GetAccountDetails`` first. That is what ``async_bind_account`` does, and why
  a re-login has to repeat it rather than just re-selecting the account.
* Because the state lives in the session, one cookie jar must be reused for the
  whole conversation, and the jar has to be primed with a page load so the site
  issues ``.AspNetCore.Session`` and the Azure affinity cookies in the first
  place.
* ``X-Requested-With: XMLHttpRequest`` is mandatory; the site 401s any /api call
  that does not look like an XHR.
* The two 401 shapes mean different things. A hard 401 carrying an RFC 7235
  ``ProblemDetails`` body means the session was not ready. A *stale token*
  instead yields HTTP 200 with a single-element body whose ``Status.Code`` is
  ``Unauthorized``, which is recoverable by rotating the token.
* The advertised token lifetime is wrong: the response says
  ``Expires_in: 3598`` while the Id_token's own ``exp`` claim is 600 seconds, so
  this client trusts the JWT and still retries once on an in-band Unauthorized.
* ``PersonId`` really is the literal string ``"PersonId"`` -- the site stores
  that too, and the backend resolves the person from the session. Not a bug.
* **The daily endpoint buckets by UTC day; the hourly stamps are UK local.**
  So during BST the portal's own daily graph disagrees with the sum of its own
  hourly rows by exactly the 00:00-01:00 local hour, and the two agree in
  winter. Verified over a week: the daily figure for day D always equals the
  hourly rows from 01:00 on D to 01:00 on D+1, which in BST is precisely
  00:00-00:00 UTC. This client attributes consumption to *local* days, which is
  what Home Assistant's Energy dashboard expects, so its daily totals can differ
  from the website by up to one hour's usage. That is the website being
  inconsistent with itself, not an import error.
* The hourly ``Date`` is the interval **end**, as a UK wall-clock time, so it
  must be resolved to an instant *before* an hour is subtracted. Proof: on
  2026-03-29 the portal's first row is stamped 02:00, because the hour
  00:00-01:00 GMT ends when the clock reads 02:00 BST.
* Clock-change days do not have 24 rows in the way one would guess. Spring
  forward returns 23 rows starting at 02:00. Autumn returns **24** rows, not 25:
  the portal publishes nothing for one half of the repeated hour, so that day
  has a genuine one hour gap.
* The ``smartUserTokenInfo`` cookie is set here because ``getSmartAuthToken()``
  sets it, and the server may well read it. It was not what fixed the 401, and
  whether the usage calls need it has not been isolated.
* ``GetSmartAuthToken`` is keyed on the **access** token, not the refresh token.
  The site's ``getSmartAuthToken()`` reads ``smartTokenInfo`` -- which login
  writes as the whole ``SaveUserProfile`` response -- and posts only its
  ``access_token``. Sending the wrong field yields HTTP 200 with a body that
  simply has no ``Id_token`` in it, which is why the failure surfaces as a
  missing token rather than as a rejection.

A note on method, because it cost a lot of time here: the site sets several
cookies from JavaScript, so captured HTTP traffic alone does not explain how it
authenticates, and deleting a cookie mid-session proves nothing once the portal
has cached the identity against the session. Reading the site's own scripts, and
comparing a session that works against one that does not, both beat inference
from a single failing request.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.nwl.co.uk"

EP_LOGIN = "/api/Auth/Login"
EP_SAVE_PROFILE = "/api/Auth/SaveUserProfile"
EP_ACCOUNT_SUMMARY = "/api/Customer/GetAccountSummary"
EP_CUSTOMER_SESSION = "/api/Customer/AddOrUpdateCustomerSession"
EP_ACCOUNT_DETAILS = "/api/Customer/GetAccountDetails"
EP_SMART_TOKEN = "/api/Customer/GetSmartAuthToken"
EP_HOURLY_USAGE = "/api/Customer/GetHourlyWaterUsage"
EP_DAILY_USAGE = "/api/Customer/GetDailyWaterUsage"

SITE_TZ = ZoneInfo("Europe/London")

# Refresh the Id_token this long before its own expiry claim.
TOKEN_SAFETY_MARGIN = timedelta(seconds=60)

DEFAULT_HEADERS = {
    # Required: the site 401s any /api call that does not look like an XHR.
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "*/*",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/account/?account=home&step=myUsage",
}

READING_TYPE_ACTUAL = 1


class NWLError(Exception):
    """Base error for the Northumbrian Water client."""


class NWLAuthError(NWLError):
    """The credentials themselves were rejected."""


class NWLSessionError(NWLAuthError):
    """The portal no longer holds the server-side session state we rely on.

    Distinct from a credential failure because the repair is different: the
    same password logs straight back in, and the account walk has to be
    replayed. Kept as a subclass so callers that only care that authentication
    failed still catch it, but callers that can retry -- the coordinator, and
    the recovery path in ``_async_usage_call`` -- can single it out first.
    """


class NWLApiError(NWLError):
    """The portal returned an unexpected response."""


@dataclass(frozen=True, slots=True)
class MeterInfo:
    """Identifiers needed to query usage for one meter."""

    account_id: str
    premise_id: str
    person_id: str
    meter_serial: str
    address: str | None = None


@dataclass(frozen=True, slots=True)
class HourlyReading:
    """One hour of consumption.

    ``start`` is timezone aware and marks the beginning of the hour the
    consumption falls in. The portal reports the *end* of each interval, which
    this client converts on the way in.
    """

    start: datetime
    litres: float
    cost: float
    estimated: bool


def _decode_jwt_expiry(token: str) -> datetime | None:
    """Return the ``exp`` claim of a JWT, or None if it cannot be read."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(exp, tz=UTC)


def _loads(text: str) -> Any:
    """Parse a portal response body.

    Several endpoints are served as ``text/plain`` and a couple hand back a
    JSON-encoded string containing JSON, so unwrap one extra layer if present.
    """
    try:
        value = json.loads(text)
    except ValueError as err:
        raise NWLApiError(f"Response was not JSON: {text[:200]!r}") from err
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _is_unauthorized(payload: Any) -> bool:
    """Detect the in-band 'token expired' marker the usage endpoints return."""
    if not isinstance(payload, list) or not payload:
        return False
    status = payload[0].get("Status") if isinstance(payload[0], dict) else None
    if not isinstance(status, dict):
        return False
    return str(status.get("Code", "")).lower() == "unauthorized"


def _first(mapping: dict[str, Any], *keys: str) -> Any:
    """Return the first present key.

    The portal serialises most objects twice, once as ``Meters`` and once as
    ``metersField``, and which one is populated is not consistent between
    endpoints.
    """
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


class NorthumbrianWaterClient:
    """Talks to the nwl.co.uk customer API on behalf of one login."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        *,
        request_timeout: float = 45.0,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)

        self._refresh_token: str | None = None
        # What GetSmartAuthToken is actually keyed on; see the module docstring.
        self._access_token: str | None = None
        self._person_id: str | None = None
        self._id_token: str | None = None
        self._id_token_expires: datetime | None = None
        # The meter whose usage we read. Held so the server-side session can be
        # prepared again after a re-login, which the usage endpoints require.
        self._bound_meter: MeterInfo | None = None
        # Serialises login and token rotation. The refresh token is single use,
        # so two concurrent rotations would invalidate each other.
        self._auth_lock = asyncio.Lock()

    @property
    def person_id(self) -> str | None:
        """PersonId from the authenticated profile, once logged in."""
        return self._person_id

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        """Issue one API call and return the decoded body."""
        return _loads(
            await self._request_text(method, path, json_body=json_body, params=params)
        )

    async def _request_text(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, str] | None = None,
    ) -> str:
        """Issue one API call and return the raw response body.

        Login needs the raw text as well as the parsed object, because the site
        stores the response verbatim in a cookie and the server reads it back.
        """
        url = f"{BASE_URL}{path}"
        data = json.dumps(json_body) if json_body is not None else None
        try:
            async with self._session.request(
                method,
                url,
                data=data,
                params=params,
                headers=DEFAULT_HEADERS,
                timeout=self._timeout,
            ) as response:
                text = await response.text()
                _LOGGER.debug(
                    "%s %s -> HTTP %s (%s bytes, %s cookies held)",
                    method,
                    path,
                    response.status,
                    len(text),
                    len(self._session.cookie_jar),
                )
                if response.status in (401, 403):
                    # Include the body: the portal distinguishes "no session"
                    # from "bad token" only in the response text, and the two
                    # need completely different fixes.
                    #
                    # Only the login endpoint can reject a 401 on the strength
                    # of the credentials. Anywhere else it means the session
                    # behind the cookie has gone, which no new password fixes.
                    error = NWLAuthError if path == EP_LOGIN else NWLSessionError
                    raise error(
                        f"{path} rejected the session (HTTP {response.status}); "
                        f"{len(self._session.cookie_jar)} cookies held; "
                        f"body: {text[:300]!r}"
                    )
                if response.status >= 400:
                    message = text[:200]
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            message = parsed.get("Message") or message
                    except ValueError:
                        pass
                    raise NWLApiError(
                        f"{path} failed (HTTP {response.status}): {message}"
                    )
        except TimeoutError as err:
            raise NWLApiError(f"{path} timed out") from err
        except aiohttp.ClientError as err:
            raise NWLApiError(f"{path} failed: {err}") from err

        if not text.strip():
            raise NWLApiError(f"{path} returned an empty body")
        return text

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def async_login(self) -> None:
        """Authenticate and establish the server-side session."""
        async with self._auth_lock:
            await self._login_locked()

    async def _async_prime_session(self) -> None:
        """Load a page so the site issues its session cookies.

        A browser has collected ``.AspNetCore.Session`` and the Azure affinity
        cookies from ordinary page loads long before it calls the API, and the
        session is where the portal caches an authenticated identity. Opening
        with a bare API POST instead leaves us without one.

        Done before every login rather than once per client: if we are logging
        in again because the session died, its cookie needs replacing too.
        """
        url = f"{BASE_URL}/account/"
        headers = {
            "User-Agent": DEFAULT_HEADERS["User-Agent"],
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-GB,en;q=0.9",
        }
        try:
            async with self._session.get(
                url, headers=headers, timeout=self._timeout
            ) as response:
                await response.read()
                _LOGGER.debug(
                    "Primed the session from %s (HTTP %s, %s cookies held)",
                    url,
                    response.status,
                    len(self._session.cookie_jar),
                )
        except (TimeoutError, aiohttp.ClientError) as err:
            # Not fatal on its own; the login below will fail with a clearer
            # message if the site is genuinely unreachable.
            _LOGGER.debug("Could not prime the session: %s", err)

    async def _login_locked(self) -> None:
        await self._async_prime_session()
        payload = await self._request(
            "POST",
            EP_LOGIN,
            json_body={"email": self._email, "password": self._password},
        )
        if not isinstance(payload, dict):
            # Malformed rather than refused: a refusal is well-formed JSON with no
            # Response. Reporting this as an auth failure would ask for a new
            # password over what is a portal fault.
            raise NWLApiError("Login returned an unexpected body")

        response = payload.get("Response")
        if not isinstance(response, dict):
            raise NWLAuthError(
                "Login was refused. Check the email address and password, and "
                "note that the portal locks accounts and starts requiring a "
                "CAPTCHA after repeated failures."
            )

        profile = response.get("Profile") or {}
        if isinstance(profile, dict):
            roles = profile.get("Roles") or []
            email_verified = profile.get("EmailVerified")
            if email_verified is False and "notValidated" in roles:
                raise NWLAuthError(
                    "This account's email address has not been verified yet. "
                    "Finish verification in the portal, then retry."
                )

        # Mirrors the web app: hand the tokens back so the server binds them to
        # the session cookie, and take the profile it returns as authoritative.
        saved_text = await self._request_text(
            "POST",
            EP_SAVE_PROFILE,
            json_body={
                "access_token": response.get("access_token"),
                "expires_in": response.get("expires_in"),
                "refresh_token": response.get("refresh_token"),
            },
        )
        saved = _loads(saved_text)
        if not isinstance(saved, dict):
            raise NWLApiError("SaveUserProfile returned an unexpected body")

        self._refresh_token = (
            saved.get("refresh_token") or response.get("refresh_token") or ""
        ).strip() or None
        # The site stores the SaveUserProfile response as smartTokenInfo and
        # takes the access token from there, so prefer that copy over the one
        # the login returned.
        self._access_token = (
            saved.get("access_token") or response.get("access_token") or ""
        ).strip() or None
        self._person_id = (
            str(saved.get("PersonId") or (profile or {}).get("Uid") or "").strip()
            or None
        )

        if not self._refresh_token:
            raise NWLApiError("Login succeeded but no refresh token was issued")
        if not self._access_token:
            raise NWLApiError("Login succeeded but no access token was issued")
        if not self._person_id:
            raise NWLApiError("Login succeeded but no PersonId was returned")

        # A fresh login invalidates any Id_token we were holding.
        self._id_token = None
        self._id_token_expires = None

        # The web app's login handler writes these from JavaScript, so a plain
        # HTTP client has to write them too.
        login_info = dict(response)
        login_info["Profile"] = None
        self._set_site_cookies(
            loginInfoSuccess=json.dumps(login_info),
            userProfile=saved_text,
            smartTokenInfo=saved_text,
        )

        _LOGGER.debug("Logged in to the Northumbrian Water portal")

        # A login starts a fresh server-side session, so everything the usage
        # endpoints rely on has to be re-established. This matters most on the
        # recovery path in _async_id_token: without it a session rebuilt after
        # an expiry would go straight back to returning 401.
        if self._bound_meter:
            await self._prepare_account_locked(self._bound_meter)

    def _set_site_cookies(self, **values: str) -> None:
        """Set cookies the site's own JavaScript would write.

        jquery.cookie percent-encodes values, so match that: the server decodes
        them the same way.
        """
        self._session.cookie_jar.update_cookies(
            {name: quote(value, safe="!'()*-._~") for name, value in values.items()},
            response_url=URL(BASE_URL),
        )

    async def _set_active_account(self, account_id: str) -> None:
        """Point the server-side session at one account."""
        await self._request(
            "POST",
            EP_CUSTOMER_SESSION,
            json_body=[
                f"PersonId:{self._person_id}",
                f"AccountId:{account_id}",
            ],
        )
        _LOGGER.debug("Bound the portal session to the configured account")

    async def async_bind_account(self, meter: MeterInfo) -> None:
        """Log in if needed and prepare the session to serve ``meter``'s usage.

        Selecting the account is not enough. The usage endpoints read state that
        the portal only writes while answering the calls its account page makes
        on load, so replay that whole sequence -- account summary, then account
        detail -- rather than just the session update. Skipping it is what makes
        a cold start 401 while a session that has browsed the account works.
        """
        async with self._auth_lock:
            self._bound_meter = meter
            if not self._person_id:
                # _login_locked prepares the session itself, using the meter
                # recorded above, so there is nothing more to do here.
                await self._login_locked()
            else:
                await self._prepare_account_locked(meter)

    async def async_rebuild_session(self, meter: MeterInfo) -> None:
        """Discard the current session and build a fresh one for ``meter``.

        Unlike ``async_bind_account`` this always logs in again, because the
        reason for calling it is that the portal has forgotten the session
        entirely -- replaying the account walk over the dead one would only
        collect another 401. ``_login_locked`` re-primes the cookie jar and,
        because the meter is recorded first, replays that walk itself.
        """
        async with self._auth_lock:
            self._bound_meter = meter
            await self._login_locked()

    async def _prepare_account_locked(self, meter: MeterInfo) -> None:
        """Walk the account pages so the session is ready for usage calls."""
        await self._request(
            "GET",
            EP_ACCOUNT_SUMMARY,
            params={"personId": self._person_id},
        )
        await self._set_active_account(meter.account_id)
        await self._request(
            "POST",
            EP_ACCOUNT_DETAILS,
            json_body={
                "AccountId": meter.account_id,
                "PremiseId": meter.premise_id,
                "PersonId": self._person_id,
            },
        )
        _LOGGER.debug("Session prepared for usage calls")

    async def _async_id_token(self, *, force: bool = False) -> str:
        """Return a valid smart-metering Id_token, rotating it when needed."""
        async with self._auth_lock:
            if (
                not force
                and self._id_token
                and self._id_token_expires
                and datetime.now(UTC) + TOKEN_SAFETY_MARGIN < self._id_token_expires
            ):
                return self._id_token

            if not self._access_token:
                await self._login_locked()

            try:
                token = await self._rotate_token_locked()
            except (NWLAuthError, NWLApiError):
                # The access token is single use and rotates; if it has gone
                # stale the only way back is a full login.
                _LOGGER.debug("Token rotation failed, logging in again")
                await self._login_locked()
                token = await self._rotate_token_locked()
            return token

    async def _rotate_token_locked(self) -> str:
        raw = await self._request_text(
            "POST",
            EP_SMART_TOKEN,
            json_body={"access_token": self._access_token},
        )
        payload = _loads(raw)
        if not isinstance(payload, dict) or not payload.get("Id_token"):
            # The endpoint answers a rejected token with HTTP 200 and a body
            # that just lacks the token, so name what did come back. Keys only:
            # a body that *does* carry tokens must never reach the log.
            shape = (
                ", ".join(sorted(payload))
                if isinstance(payload, dict)
                else type(payload).__name__
            )
            raise NWLSessionError(
                f"GetSmartAuthToken did not return an Id_token (returned {shape})"
            )

        # This is the credential the usage endpoints actually authenticate
        # against. The site's getSmartAuthToken() stores the response verbatim
        # in this cookie from JavaScript, and the server reads it back from
        # there; the copy in the request body is passed to the metering service
        # behind it. Without the cookie every usage call returns a hard 401 no
        # matter how good the body token is.
        self._set_site_cookies(smartUserTokenInfo=raw)

        id_token = str(payload["Id_token"])
        # The rotated tokens supersede the ones we just spent. The site writes
        # both back into smartTokenInfo and sends the new access token on its
        # next call, so hold them the same way.
        if payload.get("Access_token"):
            self._access_token = str(payload["Access_token"]).strip()
        if payload.get("Refresh_token"):
            self._refresh_token = str(payload["Refresh_token"]).strip()

        expires = _decode_jwt_expiry(id_token)
        if expires is None:
            # Fall back to the advertised lifetime, but the JWT is normally far
            # shorter lived than Expires_in claims, so stay conservative.
            expires = datetime.now(UTC) + timedelta(
                seconds=min(int(payload.get("Expires_in") or 600), 600)
            )
        self._id_token = id_token
        self._id_token_expires = expires
        # A hard 401 from a usage call means the token failed format or
        # signature validation rather than having expired, so record the shape
        # (never the value) to tell those two cases apart.
        _LOGGER.debug(
            "Rotated smart token: %s chars, %s segments, expires %s, "
            "advertised Expires_in %s",
            len(id_token),
            id_token.count(".") + 1,
            expires.isoformat(),
            payload.get("Expires_in"),
        )
        return id_token

    # ------------------------------------------------------------------
    # Account discovery
    # ------------------------------------------------------------------

    async def async_discover_meters(self) -> list[MeterInfo]:
        """Find the accounts and smart meters this login can read."""
        if not self._person_id:
            await self.async_login()
        assert self._person_id is not None

        summary = await self._request(
            "GET",
            EP_ACCOUNT_SUMMARY,
            params={"personId": self._person_id},
        )
        if not isinstance(summary, dict):
            raise NWLApiError("GetAccountSummary returned an unexpected body")

        accounts = summary.get("Accounts")
        if not isinstance(accounts, list) or not accounts:
            raise NWLApiError("No accounts are linked to this login")

        meters: list[MeterInfo] = []
        for account in accounts:
            if not isinstance(account, dict):
                continue
            account_id = str(
                _first(account, "accountIDField", "AccountID", "AccountId") or ""
            ).strip()
            premise_id = str(
                _first(account, "premiseIDField", "PremiseID", "PremiseId") or ""
            ).strip()
            if not account_id:
                continue
            address = _first(account, "propertyAddressField", "PropertyAddress")
            meters.extend(
                await self._async_meters_for_account(
                    account_id, premise_id, str(address) if address else None
                )
            )

        if not meters:
            raise NWLApiError(
                "No smart meter was found on this account. Hourly readings are "
                "only available where Northumbrian Water has fitted one."
            )
        return meters

    async def _async_meters_for_account(
        self, account_id: str, premise_id: str, address: str | None
    ) -> list[MeterInfo]:
        assert self._person_id is not None

        # The account detail endpoint reads the active account from the
        # server-side session, so set it before asking.
        await self._set_active_account(account_id)

        detail = await self._request(
            "POST",
            EP_ACCOUNT_DETAILS,
            json_body={
                "AccountId": account_id,
                "PremiseId": premise_id,
                "PersonId": self._person_id,
            },
        )
        if not isinstance(detail, dict):
            raise NWLApiError("GetAccountDetails returned an unexpected body")

        account_detail = _first(detail, "accountDetailField", "AccountDetail") or detail
        if not isinstance(account_detail, dict):
            raise NWLApiError("GetAccountDetails did not include account detail")

        raw_meters = _first(account_detail, "Meters", "metersField") or []
        if not isinstance(raw_meters, list):
            return []

        results: list[MeterInfo] = []
        for meter in raw_meters:
            if not isinstance(meter, dict):
                continue
            serial = str(_first(meter, "BadgeNumber", "badgeNumberField") or "").strip()
            if not serial:
                continue
            results.append(
                MeterInfo(
                    account_id=account_id,
                    premise_id=premise_id,
                    person_id=self._person_id,
                    meter_serial=serial,
                    address=address,
                )
            )
        return results

    # ------------------------------------------------------------------
    # Usage
    # ------------------------------------------------------------------

    async def async_get_hourly(
        self, meter: MeterInfo, day: date
    ) -> list[HourlyReading]:
        """Return the hourly readings for one local calendar day.

        An empty list means the portal has no data for that day yet, which is
        normal for the last couple of days.
        """
        return self._parse_hourly(await self.async_get_hourly_raw(meter, day), day)

    async def async_get_hourly_raw(self, meter: MeterInfo, day: date) -> Any:
        """Return the portal's unparsed hourly response for one day.

        Exists for diagnostics: the shape of a clock-change day is what reveals
        whether the timestamps are UK local time or UTC.
        """
        return await self._async_usage_call(
            EP_HOURLY_USAGE, meter, f"{day.isoformat()}T00:00:00"
        )

    async def async_get_daily_totals(
        self, meter: MeterInfo, start_day: date
    ) -> list[tuple[date, float, float]]:
        """Return the portal's own daily totals, as its usage graph shows them.

        Returns ``(day, litres, cost)``. Not used to build statistics -- the
        hourly readings are the source of truth for those -- but the portal's
        daily figures do not always equal the sum of its own hourly readings, so
        this exists to cross-check both quantities.
        """
        payload = await self._async_usage_call(
            EP_DAILY_USAGE, meter, f"{start_day.isoformat()}T00:00:00"
        )
        if not isinstance(payload, list):
            return []
        totals: list[tuple[date, float, float]] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            raw_date, litres = entry.get("Date"), entry.get("LitreValue")
            if not raw_date or litres is None:
                continue
            try:
                totals.append(
                    (
                        datetime.fromisoformat(str(raw_date)).date(),
                        float(litres),
                        float(entry.get("MonetaryValue") or 0.0),
                    )
                )
            except ValueError:
                continue
        return totals

    async def _async_usage_call(
        self, endpoint: str, meter: MeterInfo, start_date: str
    ) -> Any:
        """Call a usage endpoint, recovering from a stale token or session.

        Two failures look alike here and need different repairs, which is what
        the two 401 shapes distinguish:

        * An in-band ``Unauthorized`` payload means only the Id_token went
          stale, so rotating it is enough.
        * A hard 401 means the portal has dropped the server-side state the
          usage endpoints read. Rotating the token would succeed and fix
          nothing, because ``GetSmartAuthToken`` does not depend on that state
          -- only replaying the account walk restores it.

        The second case is routine rather than exceptional: the portal's
        session idles out long before the next poll comes round. It used to be
        recovered only by accident, when the *token* rotation happened to fail
        at the same time, and otherwise surfaced to the user as a password
        problem.
        """
        for attempt in range(2):
            token = await self._async_id_token(force=attempt > 0)
            try:
                payload = await self._request(
                    "POST",
                    endpoint,
                    json_body={
                        "AccountId": meter.account_id,
                        "Authorization": token,
                        "MeterSerial": meter.meter_serial,
                        "StartDate": start_date,
                    },
                )
            except NWLSessionError:
                if attempt:
                    raise
                _LOGGER.debug("Usage call hard-rejected the session, rebuilding it")
                await self.async_rebuild_session(meter)
                continue
            if not _is_unauthorized(payload):
                return payload
            _LOGGER.debug("Usage call reported Unauthorized, rotating token")
        raise NWLSessionError(
            "The portal kept rejecting the smart-metering token even after the "
            "session was rebuilt."
        )

    @staticmethod
    def _parse_hourly(payload: Any, day: date) -> list[HourlyReading]:
        if not isinstance(payload, list):
            return []

        readings: list[HourlyReading] = []
        # The portal reports the *end* of each interval as a naive UK wall-clock
        # time. Resolve that to an instant first, then step back one hour of
        # elapsed time -- never an hour of wall clock, which is not the same
        # thing across a clock change.
        #
        # Confirmed against the real spring-forward day, 2026-03-29. The local
        # day is 23 hours and the portal's first row is stamped 02:00, not
        # 01:00: the hour 00:00-01:00 GMT ends at the instant the clock reads
        # 02:00 BST. Subtracting an hour of wall clock first would ask for
        # 01:00 local, which did not happen that day; Python resolves such a
        # time to the pre-transition offset, landing every reading an hour late
        # and cascading 22 collisions into the following day.
        used_utc: set[datetime] = set()
        seen_ends: set[datetime] = set()
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            raw_date = entry.get("Date")
            litres = entry.get("LitreValue")
            if not raw_date or litres is None:
                continue
            try:
                naive_end = datetime.fromisoformat(str(raw_date))
            except ValueError:
                _LOGGER.debug("Skipping reading with unparseable date %r", raw_date)
                continue

            # On the autumn change one wall-clock hour happens twice. The
            # portal sends a single row for it -- 24 rows for a 25 hour day --
            # so the hour it omits simply leaves a gap, which is an honest
            # representation of data it never published. Should it ever send
            # both, the repeat is the post-change occurrence, so resolve it with
            # fold=1 and keep the two an hour apart.
            fold = 1 if naive_end in seen_ends else 0
            seen_ends.add(naive_end)
            end = naive_end.replace(tzinfo=SITE_TZ, fold=fold).astimezone(UTC)
            start = end - timedelta(hours=1)

            # Safety net. Statistics are keyed on their start time, so two
            # readings landing on one hour would silently overwrite each other
            # and corrupt the running sum. No observed day reaches this, but a
            # duplicated stamp on a future autumn change would.
            if start in used_utc:
                shifted = start + timedelta(hours=1)
                if shifted in used_utc:
                    _LOGGER.warning(
                        "Discarding duplicate reading for %s on %s; the portal "
                        "reported more than one value for that hour",
                        start.isoformat(),
                        day,
                    )
                    continue
                _LOGGER.warning(
                    "Reading for %s on %s collided with an earlier hour and was "
                    "moved to %s; check the day around a clock change",
                    start.isoformat(),
                    day,
                    shifted.isoformat(),
                )
                start = shifted
            used_utc.add(start)

            reading_type = entry.get("ReadingType")
            readings.append(
                HourlyReading(
                    start=start,
                    litres=float(litres),
                    cost=float(entry.get("MonetaryValue") or 0.0),
                    estimated=reading_type is not None
                    and int(reading_type) != READING_TYPE_ACTUAL,
                )
            )

        readings.sort(key=lambda item: item.start)
        if not readings:
            _LOGGER.debug("No hourly readings published for %s", day)
        return readings
