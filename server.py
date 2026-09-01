"""
Remote MCP server exposing Garmin Connect data over Streamable HTTP.

Auth model:
  - No Garmin password ever lives on this server. You run auth_setup.py locally,
    which performs the Garmin login (incl. MFA) and prints a base64 token blob.
    That blob goes into the GARMIN_TOKENS env var.
  - Access to this server is gated by a shared secret (MCP_KEY), passed either as
    ?key=... on the URL or as an Authorization: Bearer header.

Endpoints:
  GET  /healthz  -> unauthenticated liveness probe
  ANY  /mcp      -> MCP Streamable HTTP transport
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import secrets
import threading
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qs

import anyio
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("garmin-mcp")

# --------------------------------------------------------------------------
# Garmin client (lazy, thread-safe, re-auth on failure)
# --------------------------------------------------------------------------

_client: Garmin | None = None
_client_lock = threading.Lock()


def _decode_tokens() -> str:
    """Return the Garmin session as the inline JSON string login() expects.

    auth_setup.py emits base64 so the value pastes into an env var field as one
    safe line. garminconnect wants either a filesystem path or inline JSON, so
    decode here. Raw JSON is accepted too, in case the blob was pasted undecoded.
    """
    raw = os.environ.get("GARMIN_TOKENS", "").strip()
    if not raw:
        raise RuntimeError(
            "GARMIN_TOKENS is not set. Run auth_setup.py locally and paste its "
            "output into the GARMIN_TOKENS environment variable."
        )

    if raw.startswith("{"):
        token_json = raw
    else:
        # Validate early so a mangled paste fails at boot, not mid-conversation.
        try:
            token_json = base64.b64decode(raw, validate=True).decode()
        except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"GARMIN_TOKENS is neither valid base64 nor inline JSON: {exc}"
            ) from exc

    try:
        parsed = json.loads(token_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"GARMIN_TOKENS did not decode to JSON: {exc}. Re-run auth_setup.py."
        ) from exc

    if not isinstance(parsed, dict) or not parsed.get("di_refresh_token"):
        raise RuntimeError(
            "GARMIN_TOKENS is missing di_refresh_token. Re-run auth_setup.py and "
            "copy the whole single-line blob."
        )
    return token_json


def _build_client() -> Garmin:
    client = Garmin()
    client.login(_decode_tokens())
    log.info("Authenticated with Garmin Connect")
    return client


def _get_client(force_new: bool = False) -> Garmin:
    global _client
    with _client_lock:
        if _client is None or force_new:
            _client = _build_client()
        return _client


def _is_auth_error(exc: BaseException) -> bool:
    """Is this worth burning a re-login on?

    Only an expired or rejected session is. A 404 means the metric doesn't
    exist for that date, and a 429 means Garmin is already rate-limiting us —
    re-authenticating in either case wastes a login round-trip, and in the 429
    case actively makes things worse. Composite tools issue several calls each,
    so retrying indiscriminately would multiply both.
    """
    if isinstance(exc, (GarminConnectNotFoundError, GarminConnectTooManyRequestsError)):
        return False
    if isinstance(exc, GarminConnectAuthenticationError):
        return True
    text = str(exc).lower()
    return "401" in text or "unauthorized" in text or "authentic" in text


def _call_sync(method: str, *args: Any, **kwargs: Any) -> Any:
    """Call a garminconnect method, re-authenticating once if the session died."""
    try:
        return getattr(_get_client(), method)(*args, **kwargs)
    except Exception as first:  # noqa: BLE001 - the client raises a wide range
        if not _is_auth_error(first):
            raise
        log.warning("%s failed (%s); retrying with fresh session", method, first)
        try:
            return getattr(_get_client(force_new=True), method)(*args, **kwargs)
        except Exception as second:  # noqa: BLE001
            raise RuntimeError(
                f"Garmin call '{method}' failed after re-auth: {second}. "
                "Your stored tokens may have expired — re-run auth_setup.py."
            ) from second


async def call(method: str, *args: Any, **kwargs: Any) -> Any:
    """Run the blocking Garmin call off the event loop."""
    return await anyio.to_thread.run_sync(lambda: _call_sync(method, *args, **kwargs))


async def gather(sections: dict[str, tuple[str, tuple[Any, ...]]]) -> dict[str, Any]:
    """Run several Garmin calls, keeping whatever succeeds.

    Garmin returns 404 for metrics an activity or day legitimately has no data
    for — a treadmill run has no weather, a bike ride has no exercise sets, a
    watch that doesn't measure it has no respiration. One missing section
    should not fail the whole tool, so each is reported independently and empty
    ones are dropped rather than filling the response with nulls.
    """
    out: dict[str, Any] = {}
    for label, (method, args) in sections.items():
        try:
            result = prune(await call(method, *args))
        except Exception as exc:  # noqa: BLE001
            out[label] = f"unavailable: {exc}"
            continue
        if result not in (None, {}, [], ""):
            out[label] = result
    return out


# --------------------------------------------------------------------------
# Response shaping
# --------------------------------------------------------------------------

MAX_LIST = int(os.environ.get("MAX_LIST_ITEMS", "50"))


def prune(obj: Any, keep: set[str] | None = None, max_list: int = MAX_LIST) -> Any:
    """Drop nulls and truncate long arrays so raw Garmin payloads stay readable.

    Garmin returns deeply nested objects with hundreds of null fields and
    minute-by-minute arrays. Sending those verbatim wastes the context window.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if v is None or v == [] or v == {}:
                continue
            if keep and k not in keep:
                continue
            out[k] = prune(v, None, max_list)
        return out
    if isinstance(obj, list):
        trimmed = [prune(i, keep, max_list) for i in obj[:max_list]]
        if len(obj) > max_list:
            trimmed.append(f"... {len(obj) - max_list} more items truncated")
        return trimmed
    return obj


def today() -> str:
    return date.today().isoformat()


def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


def as_text(payload: Any) -> str:
    if payload is None:
        return "No data returned by Garmin for that request."
    return json.dumps(payload, indent=2, default=str)


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

mcp = MCPServer(
    name="garmin",
    instructions=(
        "Read-only access to the user's Garmin Connect data: activities, sleep, "
        "HRV, Body Battery, stress, training readiness and status, VO2 max, and "
        "body composition. Dates are ISO format (YYYY-MM-DD) and default to today."
    ),
)

ACTIVITY_FIELDS = {
    "activityId", "activityName", "startTimeLocal", "activityType", "typeKey",
    "distance", "duration", "elapsedDuration", "movingDuration", "elevationGain",
    "averageSpeed", "maxSpeed", "calories", "averageHR", "maxHR", "averagePower",
    "normPower", "aerobicTrainingEffect", "anaerobicTrainingEffect",
    "averageRunningCadenceInStepsPerMinute", "steps", "vO2MaxValue",
}


@mcp.tool(description="List recent activities, newest first. Returns summary fields only.")
async def list_activities(limit: int = 10, offset: int = 0) -> str:
    data = await call("get_activities", offset, limit)
    return as_text([prune(a, ACTIVITY_FIELDS) for a in (data or [])])


@mcp.tool(description="List activities between two dates. activity_type is optional, e.g. 'running', 'cycling'.")
async def activities_by_date(start: str, end: str, activity_type: str = "") -> str:
    data = await call("get_activities_by_date", start, end, activity_type or None)
    return as_text([prune(a, ACTIVITY_FIELDS) for a in (data or [])])


@mcp.tool(description="Full detail for one activity, including splits summary. Use an activityId from list_activities.")
async def activity_detail(activity_id: int) -> str:
    summary = await call("get_activity", str(activity_id))
    return as_text(prune(summary))


@mcp.tool(description="Daily wellness summary for a date: steps, calories, resting HR, intensity minutes, stress, Body Battery range.")
async def daily_summary(day: str = "") -> str:
    data = await call("get_user_summary", day or today())
    return as_text(prune(data))


@mcp.tool(description="Sleep data for a date: stages, duration, sleep score, overnight HRV and SpO2 summary.")
async def sleep(day: str = "") -> str:
    data = await call("get_sleep_data", day or today())
    if isinstance(data, dict):
        # Drop the minute-level movement/level arrays; keep the summary blocks.
        for noisy in ("sleepLevels", "sleepMovement", "wellnessSpO2SleepSummaryDTO",
                      "sleepHeartRate", "sleepStress", "sleepBodyBattery", "hrvData"):
            data.pop(noisy, None)
    return as_text(prune(data))


@mcp.tool(description="HRV status and overnight HRV values for a date.")
async def hrv(day: str = "") -> str:
    data = await call("get_hrv_data", day or today())
    if isinstance(data, dict):
        data.pop("hrvReadings", None)
    return as_text(prune(data))


@mcp.tool(description="Training readiness score and its contributing factors for a date.")
async def training_readiness(day: str = "") -> str:
    data = await call("get_training_readiness", day or today())
    return as_text(prune(data))


@mcp.tool(description="Training status for a date: acute/chronic load, load balance, VO2 max, and status label.")
async def training_status(day: str = "") -> str:
    data = await call("get_training_status", day or today())
    return as_text(prune(data))


@mcp.tool(description="Body Battery values across a date range (defaults to the last 7 days).")
async def body_battery(start: str = "", end: str = "") -> str:
    data = await call("get_body_battery", start or days_ago(7), end or today())
    return as_text(prune(data))


@mcp.tool(description="All-day stress breakdown for a date.")
async def stress(day: str = "") -> str:
    data = await call("get_all_day_stress", day or today())
    if isinstance(data, dict):
        data.pop("stressValuesArray", None)
        data.pop("bodyBatteryValuesArray", None)
    return as_text(prune(data))


@mcp.tool(description="Resting heart rate for a date.")
async def resting_heart_rate(day: str = "") -> str:
    data = await call("get_rhr_day", day or today())
    return as_text(prune(data))


@mcp.tool(description="VO2 max and fitness age metrics for a date.")
async def vo2_max(day: str = "") -> str:
    data = await call("get_max_metrics", day or today())
    return as_text(prune(data))


@mcp.tool(description="Predicted race times for 5K, 10K, half and full marathon over a date range.")
async def race_predictions(start: str = "", end: str = "") -> str:
    data = await call("get_race_predictions", start or days_ago(30), end or today())
    return as_text(prune(data))


@mcp.tool(description="Weigh-ins and body composition over a date range (defaults to the last 30 days).")
async def body_composition(start: str = "", end: str = "") -> str:
    data = await call("get_body_composition", start or days_ago(30), end or today())
    return as_text(prune(data))


@mcp.tool(description="Daily step counts across a date range (defaults to the last 14 days).")
async def steps_range(start: str = "", end: str = "") -> str:
    data = await call("get_daily_steps", start or days_ago(14), end or today())
    return as_text(prune(data))


# --- Tools below cover endpoints surfaced by Taxuspt/garmin_mcp (MIT), grouped
# --- so that related metrics cost one round trip and one tool schema.


@mcp.tool(description="Deep detail for one activity: splits, time in heart-rate zones, weather at the time, and strength-training exercise sets. Sections an activity has no data for are omitted.")
async def activity_breakdown(activity_id: int) -> str:
    aid = str(activity_id)
    data = await gather({
        "splits": ("get_activity_splits", (aid,)),
        "typed_splits": ("get_activity_typed_splits", (aid,)),
        "hr_zones": ("get_activity_hr_in_timezones", (aid,)),
        "weather": ("get_activity_weather", (aid,)),
        "exercise_sets": ("get_activity_exercise_sets", (aid,)),
        "gear": ("get_activity_gear", (aid,)),
    })
    return as_text(data)


@mcp.tool(description="Wellness detail for a date beyond the daily summary: floors climbed, respiration, hydration, heart-rate range, and body stats.")
async def wellness_detail(day: str = "") -> str:
    d = day or today()
    data = await gather({
        "floors": ("get_floors", (d,)),
        "respiration": ("get_respiration_data", (d,)),
        "hydration": ("get_hydration_data", (d,)),
        "heart_rates": ("get_heart_rates", (d,)),
        "stats_and_body": ("get_stats_and_body", (d,)),
    })
    # These carry minute-by-minute arrays that swamp the summary figures.
    for section, noisy in (
        ("respiration", "respirationValuesArray"),
        ("heart_rates", "heartRateValues"),
    ):
        if isinstance(data.get(section), dict):
            data[section].pop(noisy, None)
    return as_text(data)


@mcp.tool(description="Physiological fitness metrics over a date range: endurance score, hill score, lactate threshold, cycling FTP, and fitness age.")
async def fitness_metrics(start: str = "", end: str = "") -> str:
    s, e = start or days_ago(30), end or today()
    data = await gather({
        "endurance_score": ("get_endurance_score", (s, e)),
        "hill_score": ("get_hill_score", (s, e)),
        "cycling_ftp": ("get_cycling_ftp", ()),
        "fitness_age": ("get_fitnessage_data", (e,)),
    })
    try:
        lactate = prune(await call("get_lactate_threshold"))
        if lactate:
            data["lactate_threshold"] = lactate
    except Exception as exc:  # noqa: BLE001 - keyword-only args, handled separately
        data["lactate_threshold"] = f"unavailable: {exc}"
    return as_text(data)


@mcp.tool(description="Personal records across all activity types: fastest 1K/5K/10K, longest run, biggest climb, and so on.")
async def personal_records() -> str:
    data = await call("get_personal_record")
    return as_text(prune(data))


@mcp.tool(description="The user's Garmin devices, including which was used most recently and its battery status.")
async def devices() -> str:
    data = await gather({
        "devices": ("get_devices", ()),
        "last_used": ("get_device_last_used", ()),
    })
    return as_text(data)


@mcp.tool(description="Registered gear (shoes, bikes) with mileage and usage stats. Optionally pass a gear UUID for that item's detailed stats.")
async def gear(gear_uuid: str = "") -> str:
    if gear_uuid:
        return as_text(prune(await call("get_gear_stats", gear_uuid)))
    # get_gear needs the profile number, which the caller has no way to know.
    profile = await call("get_user_profile")
    profile_id = ""
    if isinstance(profile, dict):
        profile_id = str(
            profile.get("userProfileNumber")
            or profile.get("profileId")
            or profile.get("id")
            or ""
        )
    if not profile_id:
        return "Could not determine the Garmin profile number needed to list gear."
    return as_text(prune(await call("get_gear", profile_id)))


@mcp.tool(description="The user's Garmin profile: name, unit system (metric or statute), and profile settings. Useful for interpreting units in other tools.")
async def user_profile() -> str:
    data = await gather({
        "full_name": ("get_full_name", ()),
        "unit_system": ("get_unit_system", ()),
        "profile": ("get_user_profile", ()),
    })
    return as_text(data)


@mcp.tool(description="Saved workouts on the user's Garmin account, newest first.")
async def workouts(limit: int = 20) -> str:
    data = await call("get_workouts", 0, limit)
    return as_text(prune(data))


@mcp.tool(description="Totals rolled up between two dates. metric is one of: distance, duration, elevationGain, calories.")
async def progress_summary(start: str = "", end: str = "", metric: str = "distance") -> str:
    data = await call(
        "get_progress_summary_between_dates", start or days_ago(30), end or today(), metric
    )
    return as_text(prune(data))


@mcp.tool(description="List every read-only get_* method available through raw_get, with its parameters. Call this first when no dedicated tool covers what you need.")
async def list_raw_methods() -> str:
    import inspect

    lines = []
    for name in sorted(dir(Garmin)):
        if not name.startswith("get_"):
            continue
        try:
            sig = str(inspect.signature(getattr(Garmin, name))).replace("self, ", "").replace("self", "")
        except (TypeError, ValueError):
            sig = "(...)"
        lines.append(f"{name}{sig}")
    return (
        f"{len(lines)} read-only methods callable via raw_get. Return annotations "
        "are shown for reference; pass arguments positionally as strings.\n\n"
        + "\n".join(lines)
    )


@mcp.tool(description="Escape hatch: call any read-only get_* method on the python-garminconnect client by name, with positional args. Use when no dedicated tool fits.")
async def raw_get(method: str, args: list[str] | None = None) -> str:
    if not method.startswith("get_"):
        return "Only read-only get_* methods are permitted."
    data = await call(method, *(args or []))
    return as_text(prune(data))


# --------------------------------------------------------------------------
# ASGI app: key auth + health probe in front of the MCP transport
# --------------------------------------------------------------------------


class KeyAuth:
    """Gate every request on a shared secret, except the health probe."""

    def __init__(self, app: Any, key: str) -> None:
        self.app = app
        self.key = key

    @staticmethod
    async def _reject(send: Any, status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        if scope["path"] in ("/healthz", "/"):
            body = b'{"status":"ok"}'
            await send({
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())],
            })
            return await send({"type": "http.response.body", "body": body})

        supplied = ""
        qs = parse_qs(scope.get("query_string", b"").decode())
        if qs.get("key"):
            supplied = qs["key"][0]
        else:
            for name, value in scope.get("headers", []):
                if name == b"authorization":
                    token = value.decode()
                    if token.lower().startswith("bearer "):
                        supplied = token[7:]
                    break

        if not secrets.compare_digest(supplied, self.key):
            return await self._reject(send, 401, "Invalid or missing key.")

        return await self.app(scope, receive, send)


def build_app() -> Any:
    key = os.environ.get("MCP_KEY", "").strip()
    if len(key) < 24:
        raise RuntimeError(
            "MCP_KEY must be set to a random string of at least 24 characters. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )

    # Fail at boot on a mangled token paste rather than on the first tool call
    # mid-conversation. This only checks the blob's shape — whether Garmin still
    # accepts the session is discovered on first use.
    _decode_tokens()

    # DNS rebinding protection is deliberately off. It exists to stop a browser
    # on a user's own network from being tricked into calling a *localhost* MCP
    # server, and it enforces that by rejecting any Origin header not on an
    # allowlist. This server is the opposite case: public, reached from
    # Anthropic's cloud rather than a browser on your LAN, and gated by MCP_KEY.
    # An attacker who can't guess the key gains nothing from a forged Origin,
    # and an unexpected Origin from a legitimate client would 403 the connector
    # with no way to diagnose it from the Claude side. The shared secret is the
    # access control here; see KeyAuth below.
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

    inner = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        # Stateless: every request is self-contained, so Render can restart or
        # scale the instance without breaking an in-flight MCP session.
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )
    return KeyAuth(inner, key)


app = build_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
