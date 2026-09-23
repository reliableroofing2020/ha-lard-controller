#!/usr/bin/env python3
"""LARD board-priority controller (Supervisor add-on).

Observe+publish always. Braiins writes ONLY when BOTH:
  - add-on option enable_writes is true, AND
  - input_boolean.lard_board_priority_enable is ON.

Never reboot miner. Never touch SRNE charge/BMS/grid.
Never turn on switch.solar_miner_auto_enable.
Never nohup / pgrep / detached keep-alive — this process runs in the
foreground; a crash exits non-zero so Supervisor restarts the container.
"""
from __future__ import annotations

import json
import re
import os
import socket
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------------------
# Probed live entities — do not guess
# ---------------------------------------------------------------------------
ENT_ENABLE = "input_boolean.lard_board_priority_enable"
ENT_MODE_REQ = "input_select.lard_miner_mode_request"
ENT_DESIRED_HA = "sensor.lard_miner_mode_desired_ha"
ENT_SOC = "sensor.srne_12k_pro_1_battery"
ENT_SOLAR_AVAIL = "sensor.solar_miner_solar_available"
ENT_PV = "sensor.srne_12k_pro_1_pv_power"
ENT_SOC_STATE = "sensor.solar_miner_soc_state"
ENT_FAULT = "sensor.solar_miner_fault_reason"
ENT_STALE = "binary_sensor.solar_miner_critical_stale"
ENT_HB = "binary_sensor.lard_api_heartbeat"
ENT_OLD_AUTO = "switch.solar_miner_auto_enable"
ENT_FAN_MAX = "input_number.lard_fan_max_pct"
# Optional per-mode envelope helpers. Legacy fan-ceiling policy only.
# Missing → add-on options (TBD/measured). Not the native temperature-target knob.
ENT_COOLING_ONE_MAX = "input_number.lard_cooling_one_board_max_pct"
ENT_COOLING_TWO_MAX = "input_number.lard_cooling_two_board_max_pct"
ENT_COOLING_THREE_MAX = "input_number.lard_cooling_three_board_max_pct"
ENT_COOLING_PAUSED_MAX = "input_number.lard_cooling_paused_max_pct"
# Operator cooling setpoints are Fahrenheit (same unit as chip_temp_f).
# Preferred entity IDs:
ENT_COOLING_TARGET_F = "input_number.lard_cooling_target_f"
ENT_COOLING_HOT_F = "input_number.lard_cooling_hot_f"
ENT_COOLING_DANGEROUS_F = "input_number.lard_cooling_dangerous_f"
# Compatibility IDs. The "_c" suffix is historical and awkward: the numeric
# state is °F, not °C. Used only when the matching _f entity has no number.
ENT_COOLING_TARGET_C = "input_number.lard_cooling_target_c"
ENT_COOLING_HOT_C = "input_number.lard_cooling_hot_c"
ENT_COOLING_DANGEROUS_C = "input_number.lard_cooling_dangerous_c"
ENT_COOLING_ENVELOPE_MIN = "input_number.lard_cooling_envelope_min_pct"
ENT_COOLING_ENVELOPE_MAX = "input_number.lard_cooling_envelope_max_pct"
# Optional HA fence. Missing is not a competing writer. ON denies arming.
# Created by the operator while upstairs-AC / pause scripts / resume spam
# are still live. This add-on does not create the entity.
ENT_COMPETING_WRITER = "binary_sensor.lard_competing_writer"

# Heartbeat / health entities published every loop (not /local JSON)
ENT_CTRL_ONLINE = "binary_sensor.lard_controller_online"
ENT_CTRL_LAST_SEEN = "sensor.lard_controller_last_seen"
ENT_CTRL_REQUESTED = "sensor.lard_controller_requested_mode"
ENT_CTRL_ACTUAL = "sensor.lard_controller_actual_mode"
ENT_CTRL_ERROR = "sensor.lard_controller_error"
ENT_CTRL_FAILS = "sensor.lard_controller_api_fail_count"
ENT_CTRL_BRAIINS_OK = "sensor.lard_controller_last_braiins_ok"
ENT_CTRL_POWER = "sensor.lard_controller_power_w"
ENT_CTRL_BOARDS = "sensor.lard_controller_boards"
# 0.1.7 observability. State is the health class; attributes carry the txn.
ENT_CTRL_HEALTH = "sensor.lard_controller_health"

# Entities this process must never write
FORBIDDEN_HA_WRITES = frozenset(
    {
        ENT_OLD_AUTO,
        "switch.solar_miner_auto_enable",
        "number.lard_power_target",
        "number.lard",
    }
)

MODES = ("PAUSED", "ONE_BOARD", "TWO_BOARD", "THREE_BOARD")
# Verified physical miner modes only. Never a controller lifecycle label.
OBSERVED_MINER_MODES = ("PAUSED", "ONE_BOARD", "TWO_BOARD", "THREE_BOARD")
# Controller lifecycle. Not a claim about hashboards or pause state.
CONTROLLER_STATES = (
    "OBSERVING",
    "DISARMED",
    "ARMED",
    "APPLYING",
    "WAITING_FOR_BRAIINS",
    "RUNNING",
    "FAULT_LATCHED",
    "ERROR",
    # Reserved. No in-repo path enters it. There is no maintenance PATCH tool.
    "MAINTENANCE_LOCKOUT",
)
# Compatibility values historically published on sensor.lard_controller_actual_mode.
# A value is a verified physical miner mode only when it is also in
# OBSERVED_MINER_MODES. APPLYING, ERROR, WAITING_FOR_BRAIINS, and
# FAULT_LATCHED are controller states kept here so existing dashboards
# do not lose the latch. Read observed_miner_mode for the physical mode.
ACTUAL_MODES = OBSERVED_MINER_MODES + (
    "APPLYING",
    "ERROR",
    "WAITING_FOR_BRAIINS",
    "FAULT_LATCHED",
)
BOARD_MAP = {
    "PAUSED": ["1"],
    "ONE_BOARD": ["1"],
    "TWO_BOARD": ["1", "2"],
    "THREE_BOARD": ["1", "2", "3"],
}
RANK = {"PAUSED": 0, "ONE_BOARD": 1, "TWO_BOARD": 2, "THREE_BOARD": 3}

# GET /api/v1/miner/details `status` — proto MinerStatus + REST names/ints.
# Do not map board topology onto these; pause/running is independent of hashboards.
_PAUSED_STATUS = frozenset(
    {
        3,
        "3",
        "paused",
        "miner_status_paused",
        "user_pause",
        "userpause",
    }
)
_OPERATIONAL_STATUS = frozenset(
    {
        2,
        "2",
        "normal",
        "miner_status_normal",
        "running",
    }
)

# Optional post-warmup sanity only. Power=0 during resume warmup is not a failure.
# Do not treat these as mature/full-hashrate gates inside the resume window.
SANITY_POWER_W = 10.0
SANITY_HASHRATE = 0.01

# Operator thermal policy for the fan-ceiling owner only.
# Restore unconstrained auto (max_fan_speed=100) at this chip °F.
# Does not add a new mining-pause rule; existing fault/SOC pause paths stay as-is.
CHIP_ABORT_F = 180
FAN_MAX_DEFAULT = 100
FAN_MAX_MIN = 0
FAN_MAX_MAX = 100
MIN_REQUIRED_FANS = 2
# Pause-verify: watts at/under this count as idle (~0 W) for cooling writes.
COOLING_IDLE_POWER_W = 10.0
# Placeholders — TBD/measured. Do not treat as final site values.
COOLING_ONE_BOARD_MAX_PCT = 70
COOLING_TWO_BOARD_MAX_PCT = 85
COOLING_THREE_BOARD_MAX_PCT = 100
COOLING_PAUSED_MAX_PCT = 100
COOLING_DWELL_S = 10 * 60
COOLING_STABILIZE_S = 5
# After a cooling PUT, BOSminer may not accept ResumeMining until config/process
# settles. Default is conservative; 0.1.5's 5s stabilize + immediate resume 500ed.
COOLING_RESUME_SETTLE_S = 20
# Kept so 0.1.6 imports stay valid. 0.1.7 does not loop ResumeMining on 500.
COOLING_RESUME_BACKOFF_S = (5, 10, 20)
ADDON_VERSION = "0.1.14"
# Optional cooling-helper misses. Monotonic. One diagnostic per window.
HELPER_MISS_BACKOFF_START_S = 30.0
HELPER_MISS_BACKOFF_MAX_S = 600.0
# Recovery readiness is advisory. It never sets the write gate.
RECOVERY_READY_POLLS = 5
# Consecutive coherent polls required before an active
# telemetry_sustained_unavailable ERROR may clear. Same count as
# RECOVERY_READY_POLLS. Spaced by poll_seconds (one count per ordinary
# tick). This counter does not arm writes and is not recovery_ready.
SUSTAINED_TELEMETRY_RECOVERY_POLLS = RECOVERY_READY_POLLS
# Cooling policy. These names only matter when cooling_control_enabled is
# explicitly true. Default is false: Braiins OS owns cooling, and neither
# policy schedules a cooling transaction.
# legacy_fan_ceiling keeps the 0.1.5–0.1.8 per-board max_fan_speed path,
# still gated by auto_fan_ceiling_enabled (default false).
COOLING_POLICY_NATIVE = "native_auto_target"
COOLING_POLICY_LEGACY = "legacy_fan_ceiling"
COOLING_POLICIES = frozenset({COOLING_POLICY_NATIVE, COOLING_POLICY_LEGACY})
# OpenAPI CoolingAutoMode: target/hot/dangerous are Temperature.degree_c,
# allowed range 0–200 °C. These LARD *add-on option* defaults follow published
# Braiins Toolbox examples (target 70, hot 85, dangerous 95) for BOS ≥ 25.01.
# They are internal Celsius fallbacks when no HA helper state exists.
# They are not a claimed 26.09 firmware default, and they are not the
# operator unit. Home Assistant helpers are Fahrenheit (see TEMP_F_*).
# Model min/max/default live on GET /api/v1/configuration/constraints
# and are observed, not invented.
TEMP_C_MIN = 0
TEMP_C_MAX = 200
COOLING_TARGET_C = 70
COOLING_HOT_C = 85
COOLING_DANGEROUS_C = 95
# Exact °F equivalents of the Toolbox Celsius defaults (integer °F).
# 70 °C → 158 °F, 85 °C → 185 °F, 95 °C → 203 °F.
# A site that was on 79 °C hot uses 174 °F (round-trips to 79 °C).
COOLING_TARGET_F = 158
COOLING_HOT_F = 185
COOLING_DANGEROUS_F = 203
# Helper slider band in the package (sensible mining temps). The converter
# still accepts any °F that lands in OpenAPI 0–200 °C after rounding.
# 32 °F = 0 °C and 392 °F = 200 °C are that full mapping.
TEMP_F_OPENAPI_MIN = 32
TEMP_F_OPENAPI_MAX = 392
HELPER_F_SLIDER_MIN = 100
HELPER_F_SLIDER_MAX = 250
# Wide safety envelope. Braiins modulates PWM inside this band.
# 0 min is omitted from the PUT (same as the legacy optional-min rule).
COOLING_ENVELOPE_MIN_PCT = 0
COOLING_ENVELOPE_MAX_PCT = 100
# Post-write settle and bounded recovery. 0 W during these windows is not ERROR.
COOLING_SETTLE_S = 45
TRANSITION_POLL_S = 10
EXPECTED_RECOVERY_S = 240
MAXIMUM_RECOVERY_S = 600
POST_RETRY_RECOVERY_S = 180
STABLE_HASH_POLLS = 3
TELEMETRY_FAILURES_BEFORE_ERROR = 3
MAX_RESUME_RETRIES_PER_TXN = 1

# Phase 1 miner-plane classes. Deterministic. Do not invent a transition
# from zero watts, a stuck APPLYING label, or an HTTP 200 alone.
TELEMETRY_CLASSES = (
    "API_UNREACHABLE",
    "AUTHENTICATION_FAILED",
    "BOSMINER_UNAVAILABLE",
    "REQUIRED_TELEMETRY_MALFORMED",
    "VALID_PAUSED",
    "VALID_TRANSITION",
    "RUNNING_HEALTHY",
    "FAULT_LATCHED",
    "WAITING_FOR_BRAIINS",
    "UNKNOWN",
)
_BOSMINER_MARKERS = (
    "connection refused",
    "os error 111",
    "errno 111",
    "econnrefused",
    "bosminer is not running",
    "bosminer api connection",
    "bosminer_not_running",
    "bosminer not running",
)
_AUTH_MARKERS = (
    "authentication",
    "invalid authentication",
    "missing or invalid authentication",
    "invalid token",
    "unauthorized",
)

# Published health classification. Finer cooling phases stay on attributes.
# INTERRUPTED_MANUAL_REVIEW is a reload/cancel hold: observe-only, no auto-resume.
HEALTH_CLASSES = (
    "HASHING",
    "PAUSED",
    "APPLYING",
    "RECOVERING",
    "UNKNOWN",
    "DEGRADED_NEEDS_ATTENTION",
    "INTERRUPTED_MANUAL_REVIEW",
    "ERROR",
)
TERMINAL_HEALTH = frozenset({"DEGRADED_NEEDS_ATTENTION", "ERROR", "INTERRUPTED_MANUAL_REVIEW"})
# Active error string set only by the idle sustained-read path.
# Recovery clears last_error. The outage stays in last_fault_*.
SUSTAINED_TELEMETRY_ERROR = "telemetry_sustained_unavailable"
# Coherent miner classes that may replace that latch. UNKNOWN, a bare
# "applying" label, and every failure class are not in this set.
RECOVERY_TELEMETRY_CLASSES = frozenset(
    {"RUNNING_HEALTHY", "VALID_PAUSED", "VALID_TRANSITION"}
)
# Closed cooling/config outcomes. A later good read must not clear these.
COOLING_FAULT_TERMINALS = frozenset({"degraded", "error", "interrupted"})
# In-flight cooling txn marker. Monotonic deadlines are NOT stored here.
INFLIGHT_TXN_NAME = "cooling_txn_inflight.json"
# Independent miner-side lifecycle tokens for VALID_TRANSITION and for
# extending a cooling recovery window. Source is GET /api/v1/miner/details
# (status, detailed_status phase, pause reason), parsed by parse_mining_state.
# Parser flags starting / preheating / ramping are equality checks on those
# same miner fields. None of these are LARD desired_mode, requested mode,
# actual_mode, or controller_state.
# "applying" is intentionally absent. It is a controller request / interim
# label, not a Braiins miner lifecycle, and must not qualify on its own.
# Match whole tokens, never substrings: "init" does not match "reinitializing".
# "running", "paused", unknown strings, and bare watts are not in this set.
LEGITIMATE_LIFECYCLE_TOKENS = frozenset(
    {
        "cooldown",
        "cooling_down",
        "preheating",
        "preheat",
        "startup",
        "starting",
        "init",
        "initializing",
        "autotuning",
        "autotune",
        "tuning",
        "tuner",
        "ramping",
        "ramp",
        "quick_ramping",
        "quickramping",
        "warming",
        "warmup",
        "booting",
    }
)
# Explicit miner faults. A telemetry timeout is not in this set.
HARD_FAULT_TOKENS = (
    "overheat",
    "overtemp",
    "thermal_fault",
    "hw_fault",
    "hardware_fault",
    "board_fault",
    "asic_fault",
    "psu_fault",
    "power_supply_fault",
    "fan_failure",
    "critical_fault",
    "unrecoverable",
)

# Policy timings from the uploaded actuator — do not invent a new energy policy
BOARD_POLL_S = 5
WARMUP_S = 30
# Resume Stage A/C confirmation window. Board topology uses board_wait_seconds.
RESUME_WAIT_S = 120
# Transient Braiins 5xx (board/ramp transitions). Do not ERROR on a single 500.
API_5XX_BACKOFF_S = (2, 5, 10, 20)
ANTI_FLAP_UP_S = 10 * 60
ANTI_FLAP_DOWN_S = 5 * 60
SETTLE_AFTER_BOARD_S = 15 * 60
SOLAR_AVG_WINDOW_S = 12 * 60
TWO_HOLD_S = 10 * 60
THREE_HOLD_S = 10 * 60
DOWN_HOLD_S = 5 * 60
# Braiins phases already parsed from miner/details — not new endpoints.
TRANSITIONAL_PHASES = frozenset(
    {
        "starting",
        "preheating",
        "preheat",
        "ramping",
        "ramp",
        "quick_ramping",
        "quickramping",
        "warming",
        "warmup",
    }
)

# Structural deny before any HTTP. Device reboot, factory reset, Start, and
# BOSminer Restart never leave this process. Cooling recovery has no
# last-resort Start or Restart.
BRAIINS_DENY_PATHS = (
    "/actions/reboot",
    "/system/reboot",
    "/actions/factory-reset",
    "/factory-reset",
    "/actions/start",
    "/actions/restart",
)


# ---------------------------------------------------------------------------
# Settings / secrets
# ---------------------------------------------------------------------------
@dataclass
class Settings:
    miner_url: str = "http://192.168.1.113"
    poll_seconds: int = 10
    board_wait_seconds: int = 60
    ha_base_url: str = ""
    enable_writes: bool = False
    power_target_w: int = 944
    braiins_username: str = "root"
    braiins_password: str = ""
    ha_token: str = ""
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    timezone: str = "America/Chicago"
    health_port: int = 8099
    data_dir: Path = field(default_factory=lambda: Path("/data"))
    share_dir: Path = field(default_factory=lambda: Path("/share"))
    # Cooling envelopes are placeholders (TBD/measured). One profile per board-count.
    cooling_dwell_seconds: int = COOLING_DWELL_S
    cooling_stabilize_seconds: int = COOLING_STABILIZE_S
    cooling_resume_settle_seconds: int = COOLING_RESUME_SETTLE_S
    cooling_one_board_max_fan_pct: int = COOLING_ONE_BOARD_MAX_PCT
    cooling_two_board_max_fan_pct: int = COOLING_TWO_BOARD_MAX_PCT
    cooling_three_board_max_fan_pct: int = COOLING_THREE_BOARD_MAX_PCT
    cooling_paused_max_fan_pct: int = COOLING_PAUSED_MAX_PCT
    cooling_one_board_min_fan_pct: int = 0
    cooling_two_board_min_fan_pct: int = 0
    cooling_three_board_min_fan_pct: int = 0
    cooling_paused_min_fan_pct: int = 0
    # 0.1.7 recovery. auto_fan_ceiling_enabled stays false until a proof run.
    # 0.1.9: native_auto_target is the default policy. It does not arm writes.
    # 0.1.10: these three options stay internal °C. HA helpers are °F.
    # 0.1.11: Braiins owns cooling. This flag defaults false so neither
    # native_auto_target nor legacy_fan_ceiling schedules a cooling PUT,
    # a pause-for-cooling, or a thermal-abort fan write.
    cooling_writes_only_when_paused: bool = True
    auto_fan_ceiling_enabled: bool = False
    cooling_control_enabled: bool = False
    cooling_policy: str = COOLING_POLICY_NATIVE
    cooling_target_temperature_c: int = COOLING_TARGET_C
    cooling_hot_temperature_c: int = COOLING_HOT_C
    cooling_dangerous_temperature_c: int = COOLING_DANGEROUS_C
    cooling_envelope_min_fan_pct: int = COOLING_ENVELOPE_MIN_PCT
    cooling_envelope_max_fan_pct: int = COOLING_ENVELOPE_MAX_PCT
    cooling_settle_seconds: int = COOLING_SETTLE_S
    transition_poll_interval_seconds: int = TRANSITION_POLL_S
    expected_recovery_seconds: int = EXPECTED_RECOVERY_S
    maximum_recovery_seconds: int = MAXIMUM_RECOVERY_S
    post_retry_recovery_seconds: int = POST_RETRY_RECOVERY_S
    stable_hash_poll_count: int = STABLE_HASH_POLLS
    telemetry_failures_before_error: int = TELEMETRY_FAILURES_BEFORE_ERROR
    resume_retry_enabled: bool = True
    max_resume_retries_per_transaction: int = MAX_RESUME_RETRIES_PER_TXN
    coalesce_pending_cooling_requests: bool = True

    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:
            return ZoneInfo("America/Chicago")


def _read_json(path: Path) -> dict:
    try:
        if path.is_file():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def _truthy(val) -> bool:
    if isinstance(val, bool):
        return val
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    """Resolve config: add-on options, /data/secrets.json, then env.

    Password/token never have a compiled-in default.
    """
    s = Settings()
    options = _read_json(Path(os.environ.get("LARD_OPTIONS", "/data/options.json")))
    secrets = _read_json(Path(os.environ.get("LARD_SECRETS", "/data/secrets.json")))
    if not secrets:
        # Local/dev fallback next to the script
        secrets = _read_json(Path(__file__).resolve().parent / "secrets.json")

    def pick(*names, default=""):
        for name in names:
            if name in options and options[name] not in (None, ""):
                return options[name]
            if name in secrets and secrets[name] not in (None, ""):
                return secrets[name]
            env = os.environ.get(name)
            if env not in (None, ""):
                return env
        return default

    s.miner_url = str(pick("miner_url", "LARD_MINER_URL", default=s.miner_url)).rstrip("/")
    s.poll_seconds = int(pick("poll_seconds", "LARD_POLL_SECONDS", default=s.poll_seconds))
    s.board_wait_seconds = int(
        pick("board_wait_seconds", "LARD_BOARD_WAIT_SECONDS", default=s.board_wait_seconds)
    )
    s.ha_base_url = str(pick("ha_base_url", "LARD_HA_BASE_URL", default="")).rstrip("/")
    s.enable_writes = _truthy(pick("enable_writes", "LARD_ENABLE_WRITES", default=False))
    s.power_target_w = int(pick("power_target_w", "LARD_POWER_TARGET_W", default=s.power_target_w))
    s.braiins_username = str(
        pick("braiins_username", "LARD_BRAIINS_USERNAME", default=s.braiins_username)
    )
    s.braiins_password = str(
        pick(
            "braiins_password",
            "BRAIINS_PASSWORD",
            "LARD_BRAIINS_PASSWORD",
            default="",
        )
    )
    s.ha_token = str(
        pick(
            "ha_token",
            "hass_token",
            "HASS_TOKEN",
            "LARD_HA_TOKEN",
            default="",
        )
    )
    s.mqtt_host = str(pick("mqtt_host", "LARD_MQTT_HOST", default=""))
    s.mqtt_port = int(pick("mqtt_port", "LARD_MQTT_PORT", default=s.mqtt_port))
    s.mqtt_username = str(pick("mqtt_username", "LARD_MQTT_USERNAME", default=""))
    s.mqtt_password = str(pick("mqtt_password", "LARD_MQTT_PASSWORD", default=""))
    s.timezone = str(pick("timezone", "TZ", "LARD_TIMEZONE", default=s.timezone))
    s.health_port = int(os.environ.get("LARD_HEALTH_PORT", s.health_port))
    s.cooling_dwell_seconds = int(
        pick("cooling_dwell_seconds", "LARD_COOLING_DWELL_SECONDS", default=s.cooling_dwell_seconds)
    )
    s.cooling_stabilize_seconds = int(
        pick(
            "cooling_stabilize_seconds",
            "LARD_COOLING_STABILIZE_SECONDS",
            default=s.cooling_stabilize_seconds,
        )
    )
    s.cooling_resume_settle_seconds = int(
        pick(
            "cooling_resume_settle_seconds",
            "LARD_COOLING_RESUME_SETTLE_SECONDS",
            default=s.cooling_resume_settle_seconds,
        )
    )

    def _pct_opt(*names, default=100):
        raw = pick(*names, default=default)
        try:
            n = int(round(float(raw)))
        except (TypeError, ValueError):
            n = int(default)
        return max(FAN_MAX_MIN, min(FAN_MAX_MAX, n))

    s.cooling_one_board_max_fan_pct = _pct_opt(
        "cooling_one_board_max_fan_pct", default=s.cooling_one_board_max_fan_pct
    )
    s.cooling_two_board_max_fan_pct = _pct_opt(
        "cooling_two_board_max_fan_pct", default=s.cooling_two_board_max_fan_pct
    )
    s.cooling_three_board_max_fan_pct = _pct_opt(
        "cooling_three_board_max_fan_pct", default=s.cooling_three_board_max_fan_pct
    )
    s.cooling_paused_max_fan_pct = _pct_opt(
        "cooling_paused_max_fan_pct", default=s.cooling_paused_max_fan_pct
    )
    s.cooling_one_board_min_fan_pct = _pct_opt(
        "cooling_one_board_min_fan_pct", default=s.cooling_one_board_min_fan_pct
    )
    s.cooling_two_board_min_fan_pct = _pct_opt(
        "cooling_two_board_min_fan_pct", default=s.cooling_two_board_min_fan_pct
    )
    s.cooling_three_board_min_fan_pct = _pct_opt(
        "cooling_three_board_min_fan_pct", default=s.cooling_three_board_min_fan_pct
    )
    s.cooling_paused_min_fan_pct = _pct_opt(
        "cooling_paused_min_fan_pct", default=s.cooling_paused_min_fan_pct
    )

    def _int_opt(*names, default=0, lo=0, hi=86400):
        raw = pick(*names, default=default)
        try:
            n = int(round(float(raw)))
        except (TypeError, ValueError):
            n = int(default)
        return max(lo, min(hi, n))

    s.cooling_writes_only_when_paused = _truthy(
        pick(
            "cooling_writes_only_when_paused",
            "LARD_COOLING_WRITES_ONLY_WHEN_PAUSED",
            default=s.cooling_writes_only_when_paused,
        )
    )
    s.auto_fan_ceiling_enabled = _truthy(
        pick(
            "auto_fan_ceiling_enabled",
            "LARD_AUTO_FAN_CEILING_ENABLED",
            default=s.auto_fan_ceiling_enabled,
        )
    )
    # Absent, null, empty, or malformed stays false. Only an explicit true
    # arms cooling writes. native_auto_target / legacy_fan_ceiling stay inert.
    s.cooling_control_enabled = _truthy(
        pick(
            "cooling_control_enabled",
            "LARD_COOLING_CONTROL_ENABLED",
            default=False,
        )
    )
    policy_raw = str(
        pick("cooling_policy", "LARD_COOLING_POLICY", default=s.cooling_policy)
    ).strip().lower()
    # Unknown / malformed policy fails closed to native target ownership.
    # That mode does not schedule fan-ceiling chasing and does not arm writes.
    s.cooling_policy = policy_raw if policy_raw in COOLING_POLICIES else COOLING_POLICY_NATIVE

    def _temp_opt(*names, default=COOLING_TARGET_C):
        return _int_opt(*names, default=default, lo=TEMP_C_MIN, hi=TEMP_C_MAX)

    s.cooling_target_temperature_c = _temp_opt(
        "cooling_target_temperature_c",
        "LARD_COOLING_TARGET_TEMPERATURE_C",
        default=s.cooling_target_temperature_c,
    )
    s.cooling_hot_temperature_c = _temp_opt(
        "cooling_hot_temperature_c",
        "LARD_COOLING_HOT_TEMPERATURE_C",
        default=s.cooling_hot_temperature_c,
    )
    s.cooling_dangerous_temperature_c = _temp_opt(
        "cooling_dangerous_temperature_c",
        "LARD_COOLING_DANGEROUS_TEMPERATURE_C",
        default=s.cooling_dangerous_temperature_c,
    )
    s.cooling_envelope_min_fan_pct = _pct_opt(
        "cooling_envelope_min_fan_pct", default=s.cooling_envelope_min_fan_pct
    )
    s.cooling_envelope_max_fan_pct = _pct_opt(
        "cooling_envelope_max_fan_pct", default=s.cooling_envelope_max_fan_pct
    )
    s.cooling_settle_seconds = _int_opt(
        "cooling_settle_seconds",
        "LARD_COOLING_SETTLE_SECONDS",
        default=s.cooling_settle_seconds,
        lo=0,
        hi=3600,
    )
    s.transition_poll_interval_seconds = _int_opt(
        "transition_poll_interval_seconds",
        "LARD_TRANSITION_POLL_INTERVAL_SECONDS",
        default=s.transition_poll_interval_seconds,
        lo=1,
        hi=120,
    )
    s.expected_recovery_seconds = _int_opt(
        "expected_recovery_seconds",
        "LARD_EXPECTED_RECOVERY_SECONDS",
        default=s.expected_recovery_seconds,
        lo=0,
        hi=7200,
    )
    s.maximum_recovery_seconds = _int_opt(
        "maximum_recovery_seconds",
        "LARD_MAXIMUM_RECOVERY_SECONDS",
        default=s.maximum_recovery_seconds,
        lo=0,
        hi=7200,
    )
    s.post_retry_recovery_seconds = _int_opt(
        "post_retry_recovery_seconds",
        "LARD_POST_RETRY_RECOVERY_SECONDS",
        default=s.post_retry_recovery_seconds,
        lo=0,
        hi=7200,
    )
    s.stable_hash_poll_count = _int_opt(
        "stable_hash_poll_count",
        "LARD_STABLE_HASH_POLL_COUNT",
        default=s.stable_hash_poll_count,
        lo=1,
        hi=20,
    )
    s.telemetry_failures_before_error = _int_opt(
        "telemetry_failures_before_error",
        "LARD_TELEMETRY_FAILURES_BEFORE_ERROR",
        default=s.telemetry_failures_before_error,
        lo=1,
        hi=20,
    )
    s.resume_retry_enabled = _truthy(
        pick(
            "resume_retry_enabled",
            "LARD_RESUME_RETRY_ENABLED",
            default=s.resume_retry_enabled,
        )
    )
    s.max_resume_retries_per_transaction = _int_opt(
        "max_resume_retries_per_transaction",
        "LARD_MAX_RESUME_RETRIES_PER_TRANSACTION",
        default=s.max_resume_retries_per_transaction,
        lo=0,
        hi=1,
    )
    s.coalesce_pending_cooling_requests = _truthy(
        pick(
            "coalesce_pending_cooling_requests",
            "LARD_COALESCE_PENDING_COOLING_REQUESTS",
            default=s.coalesce_pending_cooling_requests,
        )
    )

    data_override = os.environ.get("LARD_DATA_DIR")
    if data_override:
        s.data_dir = Path(data_override)
    share_override = os.environ.get("LARD_SHARE_DIR")
    if share_override:
        s.share_dir = Path(share_override)
    return s


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
class Logger:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.log_path = settings.data_dir / "controller.log"

    def now_local(self) -> str:
        return datetime.now(self.settings.tz()).strftime("%Y-%m-%d %H:%M:%S %Z")

    def __call__(self, msg: str) -> None:
        line = f"{self.now_local()} {msg}"
        print(line, flush=True)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a") as f:
                f.write(line + "\n")
        except Exception:
            pass


def utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class WritePermission:
    """Process-local Braiins write gate.

    Starts disarmed. Reload disarms. A recovery-ready streak does not arm it.
    ``permitted`` is true only for the duration of one already-authorized call.
    """

    def __init__(self) -> None:
        self.permitted = False
        self.reason = "startup_disarmed"
        self.source = "startup"
        self.controller_state = "DISARMED"

    def disarm(self, source: str, reason: str = "startup_disarmed") -> None:
        self.permitted = False
        self.reason = reason
        self.source = source


def write_blocked_record(
    *,
    requested_operation: str,
    source: str,
    controller_state: str,
    enable_writes: bool,
    reason: str,
) -> dict[str, Any]:
    """Denial returned before a Braiins request body or socket exists.

    ``network_write_sent`` is always false. Older callers still read
    ``denied``, ``write_blocked``, ``op``, and ``reason``.
    """
    return {
        "result": "WRITE_BLOCKED",
        "requested_operation": requested_operation,
        "source": source,
        "controller_state": controller_state or "DISARMED",
        "enable_writes": bool(enable_writes),
        "reason": reason,
        "network_write_sent": False,
        "denied": True,
        "write_blocked": True,
        "op": requested_operation,
    }


def _redact_summary(text: str, limit: int = 120) -> str:
    """Short failure text with credential-shaped fields removed."""
    raw = str(text or "").replace("\n", " ")
    redacted = re.sub(
        r"(?i)(password|token|authorization|secret)(\"?\s*[:=]\s*)([^,\s}\]]+)",
        r"\1\2REDACTED",
        raw,
    )
    if len(redacted) > limit:
        return redacted[:limit] + "…"
    return redacted


# In-repo miner write paths. Home Assistant automations are not in this list.
# See docs/phase1-writer-inventory.md. MAINTENANCE_LOCKOUT is not an entry.
WRITER_INVENTORY = (
    {
        "path": "Braiins.pause",
        "location": "app/controller.py Braiins.pause",
        "network": "PUT /api/v1/actions/pause",
        "direct": True,
        "user_reachable": "only after enable_writes and the HA master gate, via apply_mode",
        "gate_before_network": "_blocked_write then Controller._authorize_device_write",
        "disposition": "gated",
        "status": "active",
    },
    {
        "path": "Braiins.resume",
        "location": "app/controller.py Braiins.resume",
        "network": "PUT /api/v1/actions/resume",
        "direct": True,
        "user_reachable": "only after enable_writes and the HA master gate, via apply_mode",
        "gate_before_network": "_blocked_write then Controller._authorize_device_write",
        "disposition": "gated",
        "status": "active",
    },
    {
        "path": "Braiins.set_power",
        "location": "app/controller.py Braiins.set_power",
        "network": "PUT /api/v1/performance/power-target",
        "direct": True,
        "user_reachable": "only after enable_writes and the HA master gate, via apply_mode",
        "gate_before_network": "_blocked_write before the watt body is built",
        "disposition": "gated",
        "status": "active",
    },
    {
        "path": "Braiins.patch_boards",
        "location": "app/controller.py Braiins.patch_boards",
        "network": "PATCH /api/v1/miner/hw/hashboards",
        "direct": True,
        "user_reachable": "only after enable_writes and the HA master gate, via apply_mode",
        "gate_before_network": "_blocked_write before the hashboard body is built",
        "disposition": "gated",
        "status": "active",
    },
    {
        "path": "Braiins.set_cooling_auto",
        "location": "app/controller.py Braiins.set_cooling_auto",
        "network": "PUT /api/v1/cooling/mode",
        "direct": True,
        "user_reachable": "cooling_control_enabled and enable_writes, via a cooling transaction",
        "gate_before_network": "cooling_control_enabled check, then _blocked_write, before the auto body",
        "disposition": "gated",
        "status": "active",
    },
    {
        "path": "Braiins.start",
        "location": "app/controller.py Braiins.start",
        "network": "PUT /api/v1/actions/start",
        "direct": True,
        "user_reachable": "no",
        "gate_before_network": "BRAIINS_DENY_PATHS inside _call before ensure_auth and before any socket",
        "disposition": "structurally_denied",
        "status": "retired",
    },
    {
        "path": "Braiins.restart",
        "location": "app/controller.py Braiins.restart",
        "network": "PUT /api/v1/actions/restart",
        "direct": True,
        "user_reachable": "no",
        "gate_before_network": "BRAIINS_DENY_PATHS inside _call before ensure_auth and before any socket",
        "disposition": "structurally_denied",
        "status": "retired",
    },
    {
        "path": "Braiins._call reboot/factory-reset",
        "location": "app/controller.py BRAIINS_DENY_PATHS",
        "network": "PUT reboot or factory-reset",
        "direct": True,
        "user_reachable": "no",
        "gate_before_network": "BRAIINS_DENY_PATHS before ensure_auth and before any socket",
        "disposition": "structurally_denied",
        "status": "retired",
    },
    {
        "path": "Controller._cooling_escalate_start",
        "location": "app/controller.py",
        "network": "none",
        "direct": False,
        "user_reachable": "no",
        "gate_before_network": "helper returns false and does not call the client",
        "disposition": "retired",
        "status": "retired",
    },
    {
        "path": "Controller._cooling_escalate_restart",
        "location": "app/controller.py",
        "network": "none",
        "direct": False,
        "user_reachable": "no",
        "gate_before_network": "helper returns false and does not call the client",
        "disposition": "retired",
        "status": "retired",
    },
    {
        "path": "Controller.apply_mode",
        "location": "app/controller.py Controller.tick / apply_mode",
        "network": "indirect pause, resume, power-target, hashboard PATCH",
        "direct": False,
        "user_reachable": "tick when enable_writes and the HA master gate are both on",
        "gate_before_network": "tick returns before apply_mode when writes are disallowed; each command re-checks _authorize_device_write",
        "disposition": "gated",
        "status": "active",
    },
    {
        "path": "Controller.request_cooling_ceiling",
        "location": "app/controller.py",
        "network": "indirect cooling PUT",
        "direct": False,
        "user_reachable": "explicit call only; refused while cooling control is off",
        "gate_before_network": "_cooling_control_enabled, _policy_denial, then _call_device",
        "disposition": "gated",
        "status": "active",
    },
    {
        "path": "Controller.request_temperature_policy",
        "location": "app/controller.py",
        "network": "indirect cooling PUT",
        "direct": False,
        "user_reachable": "explicit call only; refused while cooling control is off",
        "gate_before_network": "_cooling_control_enabled, _policy_denial, then _call_device",
        "disposition": "gated",
        "status": "active",
    },
    {
        "path": "health HTTP server",
        "location": "app/controller.py start_health_server",
        "network": "none (GET /health /status / only)",
        "direct": False,
        "user_reachable": "read-only",
        "gate_before_network": "no POST/PUT/PATCH handler",
        "disposition": "read_only",
        "status": "active",
    },
)


def board_patch_readback(expect, actual, patch_http: int | None) -> str:
    """HTTP 200 on hashboard PATCH is accepted, not applied.

    ``verified`` only when the readback ids match ``expect``.
    Expected ``[1, 2, 3]`` with actual ``[1]`` or ``[]`` is ``faulted_unverified``.
    """
    exp = sorted(norm_board_id(x) for x in (expect or []) if norm_board_id(x))
    act = sorted(norm_board_id(x) for x in (actual or []) if norm_board_id(x))
    if exp != act:
        return "faulted_unverified"
    if patch_http != 200:
        return "unverified"
    return "verified"


def classify_miner_telemetry(
    *,
    ok: bool,
    http_code: int | None = None,
    error_text: str = "",
    paused: bool = False,
    user_paused: bool = False,
    running: bool = False,
    positive_lifecycle: bool = False,
    power_w: float | None = None,
    critical_fault: bool = False,
    malformed: bool = False,
    board_unverified: bool = False,
) -> str:
    """Map one read onto a Phase 1 telemetry class.

    Zero watts is not a fault when the read is ``VALID_PAUSED`` or
    ``VALID_TRANSITION``. ``VALID_TRANSITION`` is returned only when the
    caller already has coherent lifecycle evidence. A stuck APPLYING label,
    a bare 0 W, or HTTP 200 without a matching board readback is not that
    evidence. Connection refused / bosminer not running is
    ``BOSMINER_UNAVAILABLE`` and is not a successful read.
    """
    text = (error_text or "").lower()
    auth = http_code == 401 or any(marker in text for marker in _AUTH_MARKERS)
    if auth:
        return "AUTHENTICATION_FAILED"
    bosminer = http_code == 412 or any(marker in text for marker in _BOSMINER_MARKERS)
    if bosminer:
        return "BOSMINER_UNAVAILABLE"
    if malformed or "malformed" in text or "required_telemetry_malformed" in text:
        return "REQUIRED_TELEMETRY_MALFORMED"
    if board_unverified or critical_fault:
        return "FAULT_LATCHED"
    if not ok:
        return "API_UNREACHABLE"
    if paused or user_paused:
        return "VALID_PAUSED"
    if positive_lifecycle:
        return "VALID_TRANSITION"
    if (
        running
        and power_w is not None
        and float(power_w) > COOLING_IDLE_POWER_W
    ):
        return "RUNNING_HEALTHY"
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Home Assistant client
# ---------------------------------------------------------------------------
class HA:
    def __init__(self, bases: list[str], token: str, log: Logger):
        self.bases = [b.rstrip("/") for b in bases if b]
        self.token = token
        self.log = log
        self.fail_count = 0
        # True when the last state() read failed at the transport (404, refused).
        # A client that simply has no value leaves this false so tests can
        # populate a helper on the next tick without waiting out backoff.
        self.last_read_absent = False

    def _req(self, method: str, path: str, body=None, timeout=30, count_failure: bool = True):
        if not self.token:
            raise RuntimeError("HA token missing (SUPERVISOR_TOKEN or ha_token / secrets)")
        if not self.bases:
            raise RuntimeError("HA base URL missing")
        data = None if body is None else json.dumps(body).encode()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        last = None
        for base in self.bases:
            url = base + path
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode()
                    try:
                        return resp.status, json.loads(raw) if raw else {}
                    except Exception:
                        return resp.status, {"raw": raw[:500]}
            except Exception as e:
                last = e
                continue
        if count_failure:
            self.fail_count += 1
        raise RuntimeError(f"HA {method} {path} failed: {last}")

    def state(self, entity_id: str, *, quiet: bool = False):
        """Read one entity. ``quiet`` misses do not increment the API fail counter.

        Optional cooling helpers use quiet mode so a missing ``lard_cooling_*_f``
        alias is not a miner telemetry failure.
        """
        self.last_read_absent = False
        try:
            code, data = self._req(
                "GET",
                f"/api/states/{entity_id}",
                count_failure=not quiet,
            )
            if code == 200 and isinstance(data, dict):
                return data.get("state")
            self.last_read_absent = True
        except Exception as e:
            self.last_read_absent = True
            if not quiet:
                self.log(f"ha_state_err {entity_id}: {e}")
                self.fail_count += 1
            return None
        return None

    def set_state(self, entity_id: str, state, attributes=None):
        if entity_id in FORBIDDEN_HA_WRITES:
            self.log(f"REFUSED HA write to forbidden entity {entity_id}")
            return
        body = {"state": state, "attributes": attributes or {}}
        try:
            self._req("POST", f"/api/states/{entity_id}", body)
        except Exception as e:
            self.log(f"ha_set_state_err {entity_id}: {e}")
            self.fail_count += 1

    def fire_event(self, event_type: str, data=None):
        """POST /api/events/<type>. Failures are logged; they never change miner state."""
        try:
            self._req("POST", f"/api/events/{event_type}", data or {})
        except Exception as e:
            self.log(f"ha_event_err {event_type}: {e}")


def ha_bases(settings: Settings) -> list[str]:
    bases: list[str] = []
    if settings.ha_base_url:
        bases.append(settings.ha_base_url)
    # Supervisor proxy when running as an add-on (homeassistant_api: true)
    if os.environ.get("SUPERVISOR_TOKEN"):
        bases.append("http://supervisor/core")
    bases.extend(
        [
            "http://supervisor/core",
            "http://homeassistant.local:8123",
            "http://127.0.0.1:8123",
        ]
    )
    # de-dupe, keep order
    out: list[str] = []
    seen = set()
    for b in bases:
        b = b.rstrip("/")
        if b and b not in seen:
            out.append(b)
            seen.add(b)
    return out


def ha_token(settings: Settings) -> str:
    return (
        os.environ.get("SUPERVISOR_TOKEN")
        or settings.ha_token
        or os.environ.get("HASS_TOKEN")
        or ""
    )


# ---------------------------------------------------------------------------
# Cooling profile (one envelope per major board-count state)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CoolingProfile:
    """Cooling-mode body for one gated PUT.

    Legacy fan-ceiling profiles set only min/max fan percent.
    Native temperature-target profiles also set CoolingAutoMode
    target/hot/dangerous temperatures. Fan percent on that path is a wide
    safety envelope, not a PWM actuator.
    """

    name: str
    max_fan_speed: int
    min_fan_speed: int | None = None
    minimum_required_fans: int | None = None
    target_temperature_c: int | None = None
    hot_temperature_c: int | None = None
    dangerous_temperature_c: int | None = None

    def matches(self, other: CoolingProfile | None) -> bool:
        if other is None:
            return False
        if self.max_fan_speed != other.max_fan_speed:
            return False
        if (self.min_fan_speed or 0) != (other.min_fan_speed or 0):
            return False
        # Fan-only profiles (both sides omit target) keep the 0.1.5 compare.
        if self.target_temperature_c is None and other.target_temperature_c is None:
            return True
        return (
            self.target_temperature_c == other.target_temperature_c
            and self.hot_temperature_c == other.hot_temperature_c
            and self.dangerous_temperature_c == other.dangerous_temperature_c
        )

    def extra_auto(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if self.min_fan_speed:
            extra["min_fan_speed"] = int(self.min_fan_speed)
        if self.minimum_required_fans is not None:
            extra["minimum_required_fans"] = int(self.minimum_required_fans)
        elif self.max_fan_speed >= FAN_MAX_DEFAULT:
            extra["minimum_required_fans"] = MIN_REQUIRED_FANS
        for key, value in (
            ("target_temperature", self.target_temperature_c),
            ("hot_temperature", self.hot_temperature_c),
            ("dangerous_temperature", self.dangerous_temperature_c),
        ):
            if value is None:
                continue
            # OpenAPI Temperature is {"degree_c": number}, range 0–200.
            extra[key] = {"degree_c": int(value)}
        return extra


# ---------------------------------------------------------------------------
# Braiins OS+ REST (API ~1.8.0)
# ---------------------------------------------------------------------------
# Words that prove a hashboard entry when the firmware has no separate health enum.
_BOARD_HEALTH_OK_WORDS = frozenset({"ok", "healthy", "good", "normal", "nominal"})
_BOARD_HEALTH_BAD_WORDS = frozenset(
    {
        "unhealthy",
        "dead",
        "fault",
        "faulty",
        "failed",
        "failure",
        "missing",
        "bad",
        "error",
        "sick",
        "critical",
    }
)
# GET /api/v1/miner/errors component names that are safety-relevant.
_BOARD_SAFETY_COMPONENTS = ("hashboard", "board", "asic", "fan", "psu", "power_supply")


def _unwrap_int(val) -> int | None:
    if isinstance(val, dict):
        if "value" in val:
            val = val.get("value")
        elif "chips_count" in val:
            val = val.get("chips_count")
        else:
            return None
    if isinstance(val, bool) or val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def _as_optional_bool(val) -> bool | None:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return bool(val)
    if isinstance(val, str):
        s = val.strip().lower()
        if s in {"1", "true", "yes"}:
            return True
        if s in {"0", "false", "no"}:
            return False
    return None


def _board_text_blob(board: dict) -> str:
    parts: list[str] = []
    for key in ("health", "status", "state", "fault", "error", "condition", "message"):
        val = board.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip().lower().replace("-", "_"))
        elif isinstance(val, dict):
            parts.append(json.dumps(val, default=str).lower().replace("-", "_"))
    return " ".join(parts)


def _canonical_safety_token(*parts: str) -> str:
    """Map board/error text onto the same HARD_FAULT_TOKENS used everywhere else."""
    blob = " ".join(p for p in parts if p).lower().replace("-", "_").replace(" ", "_")
    spaced = " ".join(p for p in parts if p).lower().replace("-", " ").replace("_", " ")
    for tok in HARD_FAULT_TOKENS:
        if tok in blob or tok.replace("_", " ") in spaced:
            return tok
    compact = blob
    if "fan" in compact and any(k in compact for k in ("fail", "fault", "broken", "error")):
        return "fan_failure"
    if ("psu" in compact or "power_supply" in compact) and any(
        k in compact for k in ("fail", "fault", "error")
    ):
        return "psu_fault"
    if "asic" in compact and any(k in compact for k in ("fail", "fault", "error")):
        return "asic_fault"
    if "board" in compact and any(k in compact for k in ("fail", "fault", "error", "unhealthy")):
        return "board_fault"
    return ""


def _safety_fault_from_errors(errors) -> str:
    if not isinstance(errors, list):
        return ""
    tokens: list[str] = []
    for err in errors:
        if not isinstance(err, dict):
            continue
        texts = [str(err.get("message") or "")]
        for code in err.get("error_codes") or []:
            if isinstance(code, dict):
                texts.append(str(code.get("code") or ""))
                texts.append(str(code.get("reason") or ""))
                texts.append(str(code.get("hint") or ""))
            elif code:
                texts.append(str(code))
        components = []
        for comp in err.get("components") or []:
            if isinstance(comp, dict):
                components.append(str(comp.get("name") or ""))
            elif comp:
                components.append(str(comp))
        blob = " ".join(texts + components)
        token = _canonical_safety_token(blob)
        if token:
            tokens.append(token)
            continue
        comp_blob = " ".join(components).lower()
        if any(name in comp_blob for name in _BOARD_SAFETY_COMPONENTS) and any(
            k in blob.lower() for k in ("fail", "fault", "error", "unhealthy", "broken")
        ):
            forced = _canonical_safety_token(comp_blob + " fault")
            if forced:
                tokens.append(forced)
    # Stable unique order.
    out: list[str] = []
    for tok in tokens:
        if tok not in out:
            out.append(tok)
    return " ".join(out)


def _parse_one_hashboard(board: dict) -> dict[str, Any]:
    """One GET /api/v1/miner/hw/hashboards entry. Incomplete/unknown is not proven."""
    bid = norm_board_id(board.get("id"))
    if not bid:
        bid = norm_board_id(board.get("hashboard_id"))
    enabled = None
    if "enabled" in board:
        enabled = _as_optional_bool(board.get("enabled"))
    elif "is_enabled" in board:
        enabled = _as_optional_bool(board.get("is_enabled"))
    chips = _unwrap_int(board.get("chips_count"))
    stale = bool(board.get("stale") or board.get("board_stale"))
    stats = board.get("stats") if isinstance(board.get("stats"), dict) else {}
    if isinstance(stats, dict) and stats.get("stale"):
        stale = True
    blob = _board_text_blob(board)
    fault = _canonical_safety_token(blob)
    healthy_flag = _as_optional_bool(board.get("healthy")) if "healthy" in board else None
    health_word = str(board.get("health") or board.get("status") or board.get("state") or "").strip().lower()
    health_word = health_word.replace("-", "_").replace(" ", "_")
    explicit_unhealthy = healthy_flag is False or health_word in _BOARD_HEALTH_BAD_WORDS
    if health_word and any(bad in health_word for bad in _BOARD_HEALTH_BAD_WORDS):
        explicit_unhealthy = True
    if fault:
        explicit_unhealthy = True
    complete = bool(bid) and enabled is not None and chips is not None and chips > 0
    proven = (
        complete
        and enabled is True
        and not stale
        and not explicit_unhealthy
        and not fault
        and (healthy_flag is not False)
        and (not health_word or health_word in _BOARD_HEALTH_OK_WORDS or healthy_flag is True)
    )
    # A firmware payload with no health enum is proven only by a complete enabled
    # entry (id, enabled, chips_count > 0) and the absence of a fault/stale flag.
    if healthy_flag is None and not health_word and complete and enabled and not stale and not fault:
        proven = True
    reason = "proven"
    if not bid:
        reason = "missing_id"
    elif enabled is None:
        reason = "enabled_unknown"
    elif chips is None or chips <= 0:
        reason = "chips_count_incomplete"
    elif not enabled:
        reason = "disabled"
    elif stale:
        reason = "stale"
    elif fault:
        reason = fault
    elif explicit_unhealthy:
        reason = "unhealthy"
    return {
        "id": bid,
        "enabled": enabled,
        "chips_count": chips,
        "stale": stale,
        "complete": complete,
        "proven_healthy": bool(proven),
        "explicit_unhealthy": bool(explicit_unhealthy),
        "fault": fault,
        "reason": reason,
    }


def parse_board_health_payload(hashboards_body, errors=None, *, errors_checked: bool = True) -> dict[str, Any]:
    """Fail-closed board health from existing Braiins REST payloads.

    ``GET /api/v1/miner/hw/hashboards`` (proto ``Hashboard``): ``id``,
    ``enabled`` / ``is_enabled``, ``chips_count`` (u32 or ``{"value": n}``),
    optional ``stats``, plus any ``healthy`` / ``health`` / ``status`` /
    ``state`` / ``fault`` / ``stale`` fields the firmware includes.
    ``GET /api/v1/miner/errors`` (proto ``MinerError``): ``message``,
    ``error_codes[].code/reason``, ``components[].name``.

    Expected board count is NOT ``len(hashboards)``. Callers compare reports
    to configured ``BOARD_MAP`` / a prior full discovery. A short list does
    not redefine how many boards must be proven. Malformed, incomplete,
    stale, unknown, or unchecked errors are not healthy.
    """
    safety = _safety_fault_from_errors(errors) if errors_checked else ""
    base = {
        "verified": False,
        "healthy": False,
        "stale": False,
        "incomplete": True,
        "malformed": False,
        "errors_checked": bool(errors_checked),
        "safety_fault": safety,
        "reason": "unverified",
        "boards": [],
        "reported_ids": [],
    }
    if not errors_checked:
        base["reason"] = "errors_not_checked"
        base["stale"] = True
        return base
    if hashboards_body is None:
        base["reason"] = "hashboards_missing"
        base["stale"] = True
        return base
    if not isinstance(hashboards_body, dict):
        base["malformed"] = True
        base["reason"] = "hashboards_malformed"
        return base
    if hashboards_body.get("stale") or hashboards_body.get("partial"):
        base["stale"] = bool(hashboards_body.get("stale"))
        base["incomplete"] = True
        base["reason"] = "hashboards_stale" if hashboards_body.get("stale") else "hashboards_partial"
        return base
    raw_boards = hashboards_body.get("hashboards")
    if "hashboards" not in hashboards_body or not isinstance(raw_boards, list):
        base["malformed"] = True
        base["reason"] = "hashboards_malformed"
        return base
    reports: list[dict[str, Any]] = []
    for entry in raw_boards:
        if not isinstance(entry, dict):
            base["malformed"] = True
            base["reason"] = "hashboard_entry_malformed"
            base["boards"] = reports
            return base
        reports.append(_parse_one_hashboard(entry))
    reported_ids = [r["id"] for r in reports if r.get("id")]
    incomplete = any(not r.get("complete") for r in reports) or not reports
    stale = any(r.get("stale") for r in reports)
    explicit_bad = any(r.get("explicit_unhealthy") or r.get("fault") for r in reports)
    all_enabled_proven = bool(reports) and all(
        (not r.get("enabled")) or r.get("proven_healthy") for r in reports
    )
    # Top-level healthy means every enabled reported board is proven and the
    # payload itself is current. It does not mean the configured board count
    # was satisfied — a 2-long list of healthy boards is still not 3 expected.
    healthy = (
        bool(reports)
        and not incomplete
        and not stale
        and not explicit_bad
        and not safety
        and all_enabled_proven
        and all(r.get("proven_healthy") for r in reports if r.get("enabled"))
    )
    verified = not stale and not base["malformed"]
    reason = "ok" if healthy else "not_proven"
    if not reports:
        reason = "no_boards"
    elif stale:
        reason = "stale"
    elif safety:
        reason = safety
    elif explicit_bad:
        reason = "unhealthy_board"
    elif incomplete:
        reason = "incomplete"
    return {
        "verified": verified,
        "healthy": bool(healthy and verified),
        "stale": stale,
        "incomplete": incomplete or not reports,
        "malformed": False,
        "errors_checked": True,
        "safety_fault": safety,
        "reason": reason,
        "boards": reports,
        "reported_ids": reported_ids,
    }


class Braiins:
    def __init__(self, settings: Settings, log: Logger):
        self.settings = settings
        self.log = log
        self.token = None
        self.token_ts = 0.0
        self.fail_count = 0
        self.last_ok_iso = ""
        self._io_lock = threading.RLock()
        # Unbound (None) keeps direct client tests on the pre-gate path.
        # Controller binds a WritePermission that starts disarmed.
        self.write_permission: WritePermission | None = None

    def _blocked_write(self, op: str):
        """Return a WRITE_BLOCKED tuple before any request body is built, or None.

        ``enable_writes`` false blocks even when no Controller has bound a gate.
        A bound disarmed gate also blocks. A permitted gate, or an unbound
        client whose option ``enable_writes`` is true, may proceed.
        """
        gate = self.write_permission
        writes_on = bool(self.settings.enable_writes)
        if writes_on and (gate is None or gate.permitted):
            return None
        if not writes_on:
            reason = "enable_writes_false"
        else:
            reason = (gate.reason if gate is not None else None) or "writes_disarmed"
        controller_state = "DISARMED"
        if gate is not None:
            controller_state = getattr(gate, "controller_state", None) or "DISARMED"
        record = write_blocked_record(
            requested_operation=op,
            source="braiins",
            controller_state=controller_state,
            enable_writes=writes_on,
            reason=reason,
        )
        self.log(
            "write blocked "
            f"result=WRITE_BLOCKED op={op} requested_operation={op} "
            f"source=braiins reason={reason} "
            f"controller_state={record['controller_state']} "
            f"enable_writes={str(writes_on).lower()} "
            f"network_write_sent=false "
            f"state={json.dumps(record, sort_keys=True)}"
        )
        return 0, record

    @property
    def miner(self) -> str:
        return self.settings.miner_url.rstrip("/")

    def ensure_auth(self):
        if self.token and (time.time() - self.token_ts) < 50 * 60:
            return
        if not self.settings.braiins_password:
            raise RuntimeError("braiins password missing (options / /data/secrets.json / env)")
        req = urllib.request.Request(
            self.miner + "/api/v1/auth/login",
            data=json.dumps(
                {
                    "username": self.settings.braiins_username,
                    "password": self.settings.braiins_password,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            j = json.loads(resp.read().decode())
        self.token = j.get("token")
        self.token_ts = time.time()
        if not self.token:
            raise RuntimeError("braiins login missing token")
        self.last_ok_iso = utc_iso()

    def _call(self, method: str, path: str, body=None, timeout=30):
        lowered = "/" + path.lower().lstrip("/")
        if any(lowered.endswith(deny) or deny in lowered for deny in BRAIINS_DENY_PATHS):
            raise RuntimeError(f"refused Braiins path {path} (structurally denied before HTTP)")
        # Serialize all Braiins I/O so a cooling transition cannot race other writers.
        with self._io_lock:
            return self._call_locked(method, path, body=body, timeout=timeout)

    def _call_locked(self, method: str, path: str, body=None, timeout=30):
        self.ensure_auth()
        headers = {
            "Authorization": self.token,  # raw token, not Bearer — Braiins OS+ 1.8
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.miner + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                try:
                    j = json.loads(raw) if raw else {}
                except Exception:
                    j = {"raw": raw[:1000]}
                self.last_ok_iso = utc_iso()
                # Successful authenticated I/O clears transient fail poisoning.
                if resp.status == 200:
                    self.fail_count = 0
                return resp.status, j
        except urllib.error.HTTPError as e:
            raw = e.read().decode() if e.fp else ""
            if e.code == 401:
                self.token = None
            self.fail_count += 1
            try:
                j = json.loads(raw) if raw else {}
            except Exception:
                j = {"raw": raw[:1000]}
            return e.code, j
        except Exception:
            self.fail_count += 1
            raise

    def pause(self):
        blocked = self._blocked_write("pause")
        if blocked is not None:
            return blocked
        return self._call("PUT", "/api/v1/actions/pause")

    def resume(self):
        blocked = self._blocked_write("resume")
        if blocked is not None:
            return blocked
        return self._call("PUT", "/api/v1/actions/resume")

    def start(self):
        """Structurally denied. Raises in ``_call`` before any HTTP."""
        return self._call("PUT", "/api/v1/actions/start")

    def restart(self):
        """Structurally denied. Raises in ``_call`` before any HTTP."""
        return self._call("PUT", "/api/v1/actions/restart")

    def set_power(self, watt: int):
        blocked = self._blocked_write("set_power")
        if blocked is not None:
            return blocked
        return self._call("PUT", "/api/v1/performance/power-target", {"watt": int(watt)})

    def get_power_target(self):
        return self._call("GET", "/api/v1/performance/power-target")

    def patch_boards(self, enable: bool, ids: list[str]):
        blocked = self._blocked_write("patch_boards")
        if blocked is not None:
            return blocked
        if not ids:
            return 200, {}
        return self._call(
            "PATCH",
            "/api/v1/miner/hw/hashboards",
            {"enable": bool(enable), "hashboard_ids": [str(i) for i in ids]},
        )

    def enabled_ids(self):
        code, hb = self._call("GET", "/api/v1/miner/hw/hashboards")
        out = []
        for b in (hb or {}).get("hashboards") or []:
            if not isinstance(b, dict):
                continue
            en = b.get("enabled")
            if en is None:
                en = b.get("is_enabled")
            if en:
                bid = norm_board_id(b.get("id"))
                if not bid:
                    bid = norm_board_id(b.get("hashboard_id"))
                if bid:
                    out.append(bid)
        return sorted(out), code, hb

    def get_miner_errors(self):
        """GET /api/v1/miner/errors — MinerError list (message, codes, components)."""
        return self._call("GET", "/api/v1/miner/errors")

    def board_health(self):
        """Current hashboard health from existing Braiins reads. Fail closed.

        Uses only APIs this client already depends on, plus the documented
        errors list:

        - ``GET /api/v1/miner/hw/hashboards`` — ``hashboards[].id``,
          ``enabled``/``is_enabled``, ``chips_count``, ``stats``, and any
          ``healthy``/``health``/``status``/``fault``/``stale`` fields.
        - ``GET /api/v1/miner/errors`` — ``errors[].message``,
          ``error_codes[].code/reason``, ``components[].name``.

        HTTP failure, malformed JSON, a missing ``hashboards`` list, incomplete
        entries (no id, unknown enabled, missing/zero ``chips_count``), an
        explicit stale/partial flag, or an unknown health value is not
        healthy and not verified. There is no cached previous verdict.
        Expected board count is the caller's configured/discovered topology,
        not the length of this response.
        """
        try:
            code, hb = self._call("GET", "/api/v1/miner/hw/hashboards")
        except Exception as e:
            return {
                "verified": False,
                "healthy": False,
                "stale": True,
                "incomplete": True,
                "malformed": False,
                "errors_checked": False,
                "safety_fault": "",
                "reason": f"hashboards_exc:{e}",
                "boards": [],
                "reported_ids": [],
            }
        if code != 200 or not isinstance(hb, dict):
            return {
                "verified": False,
                "healthy": False,
                "stale": True,
                "incomplete": True,
                "malformed": not isinstance(hb, dict),
                "errors_checked": False,
                "safety_fault": "",
                "reason": f"hashboards_http_{code}",
                "boards": [],
                "reported_ids": [],
            }
        try:
            ecode, ej = self.get_miner_errors()
        except Exception as e:
            return {
                "verified": False,
                "healthy": False,
                "stale": True,
                "incomplete": True,
                "malformed": False,
                "errors_checked": False,
                "safety_fault": "",
                "reason": f"errors_exc:{e}",
                "boards": [],
                "reported_ids": [],
            }
        if ecode != 200 or not isinstance(ej, dict):
            return {
                "verified": False,
                "healthy": False,
                "stale": True,
                "incomplete": True,
                "malformed": False,
                "errors_checked": False,
                "safety_fault": "",
                "reason": f"errors_http_{ecode}",
                "boards": [],
                "reported_ids": [],
            }
        errors = ej.get("errors")
        if errors is None:
            errors = []
        if not isinstance(errors, list):
            return {
                "verified": False,
                "healthy": False,
                "stale": True,
                "incomplete": True,
                "malformed": True,
                "errors_checked": False,
                "safety_fault": "",
                "reason": "errors_malformed",
                "boards": [],
                "reported_ids": [],
            }
        return parse_board_health_payload(hb, errors, errors_checked=True)

    def approx_power_w(self):
        """Best-effort live watts. Missing stats are not a write failure."""
        for path in ("/api/v1/miner/stats", "/api/v1/miner", "/api/v1/miner/details"):
            try:
                code, j = self._call("GET", path)
            except Exception:
                continue
            if code != 200 or not isinstance(j, dict):
                continue
            found = _find_number(
                j,
                (
                    "approx_consumption",
                    "power_consumption",
                    "wattage",
                    "power",
                    "watt",
                ),
            )
            if found is not None:
                return found
        return None

    def set_cooling_auto(self, max_fan_speed: int, extra_auto: dict | None = None):
        """PUT /api/v1/cooling/mode tagged union. The only cooling mutate path.

        Integer max_fan_speed is percent 0–100, not a 0.6 ratio.
        Temperature fields, when present, travel in extra_auto as
        {"target_temperature": {"degree_c": N}, ...} on the auto object.

        Refused before HTTP unless cooling_control_enabled is explicitly true.
        Braiins OS owns cooling in the default posture. A bound write gate
        that is disarmed returns before the auto body is built.
        """
        if not bool(self.settings.cooling_control_enabled):
            raise RuntimeError(
                "refused Braiins path /api/v1/cooling/mode "
                "(cooling_control_disabled before HTTP)"
            )
        blocked = self._blocked_write("cooling_put")
        if blocked is not None:
            return blocked
        n = clamp_fan_max_pct(max_fan_speed)
        auto: dict[str, Any] = dict(extra_auto or {})
        auto["max_fan_speed"] = n
        if n >= FAN_MAX_DEFAULT:
            auto.setdefault("minimum_required_fans", MIN_REQUIRED_FANS)
        return self._call("PUT", "/api/v1/cooling/mode", {"auto": auto})

    def get_cooling_state(self):
        """GET /api/v1/cooling/state — fans rpm/target_speed_ratio + highest temp. Not /mode (405)."""
        return self._call("GET", "/api/v1/cooling/state")

    def get_miner_configuration(self):
        """GET /api/v1/configuration/miner — cooling mode/target readback. Not a write."""
        return self._call("GET", "/api/v1/configuration/miner")

    def miner_details(self):
        """GET /api/v1/miner/details — already used for live watts; also carries status."""
        return self._call("GET", "/api/v1/miner/details")

    def mining_state(self, details=None):
        """Pause / mining phase from miner details (no new Braiins endpoints)."""
        if details is None:
            code, details = self.miner_details()
        else:
            code = 200
        parsed = parse_mining_state(details if isinstance(details, dict) else {})
        return parsed, code, details

    def approx_hashrate(self):
        """Best-effort live hashrate from GET /api/v1/miner/stats. Optional sanity only."""
        try:
            code, j = self._call("GET", "/api/v1/miner/stats")
        except Exception:
            return None
        if code != 200 or not isinstance(j, dict):
            return None
        stats = j.get("miner_stats") if isinstance(j.get("miner_stats"), dict) else j
        return _find_number(
            stats,
            (
                "real_hashrate",
                "hashrate",
                "gigahashrate",
                "ghs",
            ),
        )


def clamp_fan_max_pct(val) -> int:
    """Integer u32 percent 0–100. Floats are rounded, then clamped."""
    try:
        n = int(round(float(val)))
    except (TypeError, ValueError):
        return FAN_MAX_DEFAULT
    return max(FAN_MAX_MIN, min(FAN_MAX_MAX, n))


def _degree_c(node) -> float | None:
    """Read Temperature.degree_c. Does not invent a value when the field is absent."""
    if not isinstance(node, dict):
        return None
    raw = node.get("degree_c")
    if raw is None and isinstance(node.get("temperature"), dict):
        raw = node["temperature"].get("degree_c")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_configured_cooling(body) -> dict[str, Any]:
    """Cooling readback from GET /api/v1/configuration/miner.

    Schema: temperature.mode.auto|manual|immersion|hydro → Cooling*Mode.
    GET /cooling/state is telemetry and is not parsed here.
    """
    out: dict[str, Any] = {
        "mode": None,
        "target_temperature_c": None,
        "hot_temperature_c": None,
        "dangerous_temperature_c": None,
        "min_fan_speed": None,
        "max_fan_speed": None,
        "minimum_required_fans": None,
    }
    if not isinstance(body, dict):
        return out
    temperature = body.get("temperature")
    if not isinstance(temperature, dict):
        return out
    mode = temperature.get("mode")
    if not isinstance(mode, dict):
        return out
    for name in ("auto", "manual", "immersion", "hydro", "disabled"):
        block = mode.get(name)
        if not isinstance(block, dict):
            continue
        out["mode"] = name
        for src, dest in (
            ("target_temperature", "target_temperature_c"),
            ("hot_temperature", "hot_temperature_c"),
            ("dangerous_temperature", "dangerous_temperature_c"),
        ):
            deg = _degree_c(block.get(src))
            if deg is not None:
                out[dest] = deg
        for src, dest in (
            ("min_fan_speed", "min_fan_speed"),
            ("max_fan_speed", "max_fan_speed"),
        ):
            if block.get(src) is None:
                continue
            try:
                out[dest] = clamp_fan_max_pct(block.get(src))
            except (TypeError, ValueError):
                pass
        fans = block.get("minimum_required_fans")
        if fans is not None:
            try:
                out["minimum_required_fans"] = int(fans)
            except (TypeError, ValueError):
                pass
        break
    return out


def celsius_to_fahrenheit(c) -> float:
    return float(c) * 9.0 / 5.0 + 32.0


def fahrenheit_to_celsius_int(f) -> int:
    """Integer °C for Braiins Temperature.degree_c.

    c = round((f - 32) * 5 / 9). Called only when a PUT body (or the
    desired profile that becomes that body) is built. Helper state stays °F.
    """
    return int(round((float(f) - 32.0) * 5.0 / 9.0))


def parse_helper_temp(raw) -> float | None:
    """Numeric helper state, or None when missing / unreadable.

    Does not substitute a default and does not convert units.
    """
    if raw in (None, "unknown", "unavailable", ""):
        return None
    if isinstance(raw, bool):
        return None
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return None
    if n != n or n in (float("inf"), float("-inf")):
        return None
    return n


def operator_setpoints_to_degree_c(
    target_f, hot_f, dangerous_f
) -> tuple[int, int, int] | None:
    """Validate target < hot < dangerous in °F, then convert for Braiins.

    Returns integer degree_c values, or None to refuse the PUT.
    After conversion each value must sit in OpenAPI 0–200 and stay strictly
    ordered, so two Fahrenheit steps cannot collapse onto the same integer.
    """
    try:
        target_f = float(target_f)
        hot_f = float(hot_f)
        dangerous_f = float(dangerous_f)
    except (TypeError, ValueError):
        return None
    if not (target_f < hot_f < dangerous_f):
        return None
    target_c = fahrenheit_to_celsius_int(target_f)
    hot_c = fahrenheit_to_celsius_int(hot_f)
    danger_c = fahrenheit_to_celsius_int(dangerous_f)
    if not (TEMP_C_MIN <= target_c < hot_c < danger_c <= TEMP_C_MAX):
        return None
    return target_c, hot_c, danger_c


def parse_cooling_telemetry(state) -> dict[str, Any]:
    """Best-effort chip °F / fan RPM / fan % / envelope from GET /api/v1/cooling/state."""
    out: dict[str, Any] = {
        "chip_temp_f": None,
        "chip_temp_c": None,
        "fan_rpm": None,
        "fan_pct": None,
        "max_fan_speed": None,
        "min_fan_speed": None,
        "fans": [],
    }
    if not isinstance(state, dict):
        return out
    ht = state.get("highest_temperature")
    degree_c = None
    if isinstance(ht, dict):
        temp = ht.get("temperature")
        if isinstance(temp, dict) and temp.get("degree_c") is not None:
            degree_c = temp.get("degree_c")
        elif ht.get("degree_c") is not None:
            degree_c = ht.get("degree_c")
    if degree_c is None:
        degree_c = _find_number(state, ("degree_c", "chip_temp", "temperature"))
    try:
        if degree_c is not None:
            out["chip_temp_c"] = float(degree_c)
            out["chip_temp_f"] = celsius_to_fahrenheit(degree_c)
    except (TypeError, ValueError):
        pass
    fans = state.get("fans") if isinstance(state.get("fans"), list) else []
    rpms: list[int] = []
    pcts: list[float] = []
    for fan in fans:
        if not isinstance(fan, dict):
            continue
        rpm = fan.get("rpm")
        ratio = fan.get("target_speed_ratio")
        rec: dict[str, Any] = {}
        try:
            if rpm is not None:
                rec["rpm"] = int(rpm)
                rpms.append(int(rpm))
        except (TypeError, ValueError):
            pass
        try:
            if ratio is not None:
                pct = float(ratio) * 100.0 if float(ratio) <= 1.0 else float(ratio)
                rec["pct"] = round(pct, 1)
                pcts.append(pct)
        except (TypeError, ValueError):
            pass
        if rec:
            out["fans"].append(rec)
    if rpms:
        out["fan_rpm"] = max(rpms)
    if pcts:
        out["fan_pct"] = round(max(pcts), 1)
    max_fan = _find_number(state, ("max_fan_speed",))
    min_fan = _find_number(state, ("min_fan_speed",))
    try:
        if max_fan is not None:
            out["max_fan_speed"] = clamp_fan_max_pct(max_fan)
    except (TypeError, ValueError):
        pass
    try:
        if min_fan is not None:
            out["min_fan_speed"] = clamp_fan_max_pct(min_fan)
    except (TypeError, ValueError):
        pass
    return out


def thermal_fault_name(fault) -> bool:
    if fault in (None, "ok", "unknown", "unavailable", "none", ""):
        return False
    s = str(fault).strip().lower()
    return any(
        key in s
        for key in ("thermal", "cooling", "overtemp", "overheat", "chip_temp", "chip_hot")
    )


def _summarize_http_body(body, limit: int = 240) -> str:
    if body is None:
        return ""
    try:
        raw = json.dumps(body, default=str) if not isinstance(body, str) else body
    except Exception:
        raw = str(body)
    raw = raw.replace("\n", " ")
    if len(raw) > limit:
        return raw[:limit] + "…"
    return raw


def _find_number(obj, keys: tuple[str, ...]):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
            found = _find_number(v, keys)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_number(item, keys)
            if found is not None:
                return found
    return None


def _norm_status_token(val) -> str:
    if val is None or isinstance(val, bool):
        return ""
    if isinstance(val, (int, float)):
        if float(val) == int(val):
            return str(int(val))
        return str(val)
    return str(val).strip().lower().replace("-", "_")


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def norm_board_id(val) -> str:
    """Normalize hashboard id so 1, '1', and 1.0 all compare as '1'."""
    if val is None or isinstance(val, bool):
        return ""
    if isinstance(val, (int, float)):
        if float(val) == int(val):
            return str(int(val))
        return str(val)
    s = str(val).strip()
    if not s or s.lower() in {"none", "null"}:
        return ""
    try:
        f = float(s)
        if f == int(f):
            return str(int(f))
    except ValueError:
        pass
    return s


def is_http_5xx(code) -> bool:
    try:
        return 500 <= int(code) < 600
    except (TypeError, ValueError):
        return False


def error_is_http_5xx(err: str) -> bool:
    if not err:
        return False
    token = str(err).rsplit("_", 1)[-1]
    return is_http_5xx(token)


def metric_trend_rising(samples, min_delta: float = 0.0) -> bool:
    """True when a metric has begun rising (delta/trend), not a mature target."""
    vals = [float(v) for v in samples if v is not None]
    if len(vals) < 2:
        return False
    return vals[-1] > vals[0] + min_delta


def _first_phase(obj):
    """Return mining phase from a proto-JSON oneof tree (no new endpoints)."""
    phases = (
        "stopped",
        "starting",
        "running",
        "stopping",
        "preheating",
        "preheat",
        "ramping",
        "ramp",
        "quick_ramping",
        "warming",
        "warmup",
    )
    if isinstance(obj, dict):
        nested = obj.get("status")
        if isinstance(nested, dict):
            found = _first_phase(nested)
            if found:
                return found
        for phase in phases:
            if phase in obj:
                return phase
        for v in obj.values():
            found = _first_phase(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _first_phase(item)
            if found:
                return found
    return None


def _has_named_key(obj, names: tuple[str, ...]) -> bool:
    want = {n.lower().replace("-", "_") for n in names}
    for key in _walk_keys(obj):
        if key.lower().replace("-", "_") in want:
            return True
    return False


# Braiins StopDetailedReason / StartDetailedReason / RunDetailedReason oneofs.
# Used only for diagnosis (cooling PUT vs unreadiness vs hard fail).
_STATUS_REASON_KEYS = (
    "user_pause",
    "thermal_pause",
    "application_unavailable",
    "unsupported_hardware",
    "dead_pools",
    "missing_license",
    "dps_cooldown",
    "delayed_start",
    "cooling_down",
    "waiting_while_cold",
    "defrosting",
    "preheating",
    "normal",
    "unspecified",
    "none",
)
_NOT_STARTED_STATUS = frozenset(
    {
        1,
        "1",
        "not_started",
        "miner_status_not_started",
    }
)


def _first_reason(obj) -> str:
    """First detailed_status reason oneof key (user_pause, cooling_down, …)."""
    if isinstance(obj, dict):
        reason = obj.get("reason")
        if isinstance(reason, dict) and reason:
            return str(next(iter(reason.keys())))
        for key in _STATUS_REASON_KEYS:
            if key in obj:
                return key
        for v in obj.values():
            found = _first_reason(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _first_reason(item)
            if found:
                return found
    return ""


def _bosminer_uptime_s(details) -> float | None:
    """GET /api/v1/miner/details bosminer_uptime_s. 0 means bosminer is not running."""
    if not isinstance(details, dict):
        return None
    raw = details.get("bosminer_uptime_s")
    if raw is None:
        raw = details.get("bosminer_uptime")
    if raw is None:
        raw = _find_number(details, ("bosminer_uptime_s", "bosminer_uptime"))
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def parse_mining_state(details) -> dict[str, Any]:
    """Parse pause / mining phase from GET /api/v1/miner/details JSON.

    Handles legacy `status` (MINER_STATUS_PAUSED / NORMAL or REST ints/names)
    and `detailed_status` oneof (stopped.user_pause / running / starting).
    Also surfaces bosminer process readiness: `bosminer_uptime_s == 0` means
    the process is not running (OpenAPI), which is distinct from user_pause.
    """
    if not isinstance(details, dict):
        details = {}
    status_raw = details.get("status")
    detailed = details.get("detailed_status")
    if detailed is None:
        detailed = details.get("detailedStatus")
    if not isinstance(detailed, dict):
        detailed = {}

    phase = _first_phase(detailed) or ""
    token = _norm_status_token(status_raw)
    status_paused = status_raw in _PAUSED_STATUS or token in {
        "3",
        "paused",
        "miner_status_paused",
        "user_pause",
        "userpause",
    }
    status_normal = status_raw in _OPERATIONAL_STATUS or token in {
        "2",
        "normal",
        "miner_status_normal",
        "running",
    }
    status_not_started = status_raw in _NOT_STARTED_STATUS or token in {
        "1",
        "not_started",
        "miner_status_not_started",
    }
    user_paused = _has_named_key(details, ("user_pause", "userPause")) or status_paused
    pause_reason = _first_reason(detailed) or _first_reason(details)

    if not phase and token in TRANSITIONAL_PHASES | {"running", "stopped", "stopping"}:
        phase = token

    running = phase == "running" or (
        status_normal and phase not in {"stopped", "stopping", "starting"} | TRANSITIONAL_PHASES
    )
    starting = phase == "starting"
    preheating = phase in {"preheating", "preheat", "warming", "warmup"}
    ramping = phase in {"ramping", "ramp", "quick_ramping", "quickramping"}
    paused = (user_paused or status_paused or phase in {"stopped", "stopping"}) and not running
    if running:
        user_paused = False

    uptime = _bosminer_uptime_s(details)
    if uptime is None:
        miner_ready = None
    else:
        miner_ready = uptime > 0
    not_started = bool(status_not_started or miner_ready is False)
    if miner_ready is None and not_started:
        miner_ready = False

    return {
        "status_raw": status_raw,
        "phase": phase,
        "user_paused": bool(user_paused and not running),
        "paused": bool(paused),
        "running": bool(running),
        "starting": bool(starting),
        "preheating": bool(preheating),
        "ramping": bool(ramping),
        "pause_reason": pause_reason,
        "bosminer_uptime_s": uptime,
        "miner_ready": miner_ready,
        "not_started": bool(not_started),
    }


@dataclass
class MinerObservation:
    enabled_ids: list[str] = field(default_factory=list)
    boards_ok: bool = False
    status_raw: Any = None
    phase: str = ""
    user_paused: bool = False
    paused: bool = False
    running: bool = False
    starting: bool = False
    preheating: bool = False
    ramping: bool = False
    power_w: float | None = None
    hashrate: float | None = None
    details_ok: bool = False
    ok: bool = False
    pause_reason: str = ""
    bosminer_uptime_s: float | None = None
    miner_ready: bool | None = None
    not_started: bool = False
    # Fail closed. A missing live board-health read is not healthy.
    boards_healthy: bool = False
    board_stale: bool = False
    board_health_verified: bool = False
    board_reports: list = field(default_factory=list)
    safety_fault: str = ""
    board_health_reason: str = ""


class CoolingResult:
    """Explicit cooling-transaction result.

    Truthy only for a closed success: hashing, intentional confirmed pause, or
    same-value noop. ``degraded`` and ``error`` are closed and falsy.
    ``interim`` (operational / applying inside the recovery window) is not
    closed and is not success.
    """

    _SUCCESS = frozenset({"hashing", "paused", "noop"})
    _CLOSED = frozenset({"hashing", "paused", "noop", "degraded", "error"})

    def __init__(self, outcome: str):
        self.outcome = outcome
        self.closed = outcome in self._CLOSED

    def __bool__(self) -> bool:
        return self.closed and self.outcome in self._SUCCESS

    def __repr__(self) -> str:
        return f"CoolingResult({self.outcome!r}, closed={self.closed})"


def _txn_result(outcome: str) -> CoolingResult:
    return CoolingResult(outcome)


# ---------------------------------------------------------------------------
# MQTT (optional) — discovery + LWT so online goes off if the add-on dies
# ---------------------------------------------------------------------------
class MqttPublisher:
    def __init__(self, settings: Settings, log: Logger):
        self.settings = settings
        self.log = log
        self.client = None
        self.ok = False
        self._lock = threading.Lock()

    def discover_broker(self, token: str) -> bool:
        if self.settings.mqtt_host:
            return True
        if not token:
            return False
        try:
            req = urllib.request.Request(
                "http://supervisor/services/mqtt",
                headers={"Authorization": f"Bearer {token}"},
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                j = json.loads(resp.read().decode() or "{}")
            data = j.get("data") or j
            host = data.get("host") or data.get("broker")
            if not host:
                return False
            self.settings.mqtt_host = str(host)
            if data.get("port"):
                self.settings.mqtt_port = int(data["port"])
            if data.get("username") and not self.settings.mqtt_username:
                self.settings.mqtt_username = str(data["username"])
            if data.get("password") and not self.settings.mqtt_password:
                self.settings.mqtt_password = str(data["password"])
            self.log(f"mqtt discovered host={self.settings.mqtt_host}:{self.settings.mqtt_port}")
            return True
        except Exception as e:
            self.log(f"mqtt_discover_skip: {e}")
            return False

    def start(self) -> bool:
        if not self.settings.mqtt_host:
            return False
        try:
            import paho.mqtt.client as mqtt  # type: ignore
        except Exception as e:
            self.log(f"mqtt paho missing, REST publish only: {e}")
            return False
        try:
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id="lard_controller",
                clean_session=True,
            )
        except Exception:
            client = mqtt.Client(client_id="lard_controller", clean_session=True)
        if self.settings.mqtt_username:
            client.username_pw_set(self.settings.mqtt_username, self.settings.mqtt_password)
        client.will_set("lard/controller/availability", "offline", qos=1, retain=True)

        def _on_connect(c, _u, _f, rc, *args):
            if rc == 0 or str(rc) in {"Success", "0"}:
                c.publish("lard/controller/availability", "online", qos=1, retain=True)
                self.ok = True
            else:
                self.log(f"mqtt connect rc={rc}")
                self.ok = False

        client.on_connect = _on_connect
        try:
            client.connect(self.settings.mqtt_host, int(self.settings.mqtt_port), 30)
            client.loop_start()
            self.client = client
            self._publish_discovery()
            self.log("mqtt connected — heartbeat via discovery + LWT")
            return True
        except Exception as e:
            self.log(f"mqtt_connect_failed REST fallback: {e}")
            self.ok = False
            return False

    def _publish_discovery(self):
        if not self.client:
            return
        device = {
            "identifiers": ["lard_controller"],
            "name": "LARD Controller",
            "manufacturer": "LARD",
            "model": "Board-priority Braiins actuator",
            "sw_version": ADDON_VERSION,
        }
        sensors = [
            (
                "binary_sensor",
                "online",
                {
                    "name": "LARD Controller Online",
                    "state_topic": "lard/controller/online",
                    "payload_on": "on",
                    "payload_off": "off",
                    "device_class": "connectivity",
                },
            ),
            ("sensor", "last_seen", {"name": "LARD Controller Last Seen", "state_topic": "lard/controller/last_seen"}),
            (
                "sensor",
                "requested_mode",
                {"name": "LARD Controller Requested Mode", "state_topic": "lard/controller/requested_mode"},
            ),
            (
                "sensor",
                "actual_mode",
                {"name": "LARD Controller Actual Mode", "state_topic": "lard/controller/actual_mode"},
            ),
            ("sensor", "error", {"name": "LARD Controller Error", "state_topic": "lard/controller/error"}),
            (
                "sensor",
                "api_fail_count",
                {
                    "name": "LARD Controller API Fail Count",
                    "state_topic": "lard/controller/api_fail_count",
                    "state_class": "measurement",
                },
            ),
            (
                "sensor",
                "last_braiins_ok",
                {"name": "LARD Controller Last Braiins OK", "state_topic": "lard/controller/last_braiins_ok"},
            ),
            (
                "sensor",
                "power_w",
                {
                    "name": "LARD Controller Power",
                    "state_topic": "lard/controller/power_w",
                    "unit_of_measurement": "W",
                    "device_class": "power",
                    "state_class": "measurement",
                },
            ),
            ("sensor", "boards", {"name": "LARD Controller Boards", "state_topic": "lard/controller/boards"}),
        ]
        for platform, object_id, extra in sensors:
            payload = {
                "unique_id": f"lard_controller_{object_id}",
                "object_id": f"lard_controller_{object_id}",
                "availability_topic": "lard/controller/availability",
                "payload_available": "online",
                "payload_not_available": "offline",
                "device": device,
            }
            payload.update(extra)
            topic = f"homeassistant/{platform}/lard_controller_{object_id}/config"
            self.client.publish(topic, json.dumps(payload), qos=1, retain=True)

    def publish_heartbeat(self, fields: dict[str, Any]) -> bool:
        if not self.client or not self.ok:
            return False
        with self._lock:
            try:
                mapping = {
                    "online": "lard/controller/online",
                    "last_seen": "lard/controller/last_seen",
                    "requested_mode": "lard/controller/requested_mode",
                    "actual_mode": "lard/controller/actual_mode",
                    "error": "lard/controller/error",
                    "api_fail_count": "lard/controller/api_fail_count",
                    "last_braiins_ok": "lard/controller/last_braiins_ok",
                    "power_w": "lard/controller/power_w",
                    "boards": "lard/controller/boards",
                }
                for key, topic in mapping.items():
                    if key in fields:
                        self.client.publish(topic, str(fields[key]), qos=1, retain=True)
                self.client.publish("lard/controller/availability", "online", qos=1, retain=True)
                return True
            except Exception as e:
                self.log(f"mqtt_pub_err: {e}")
                self.ok = False
                return False


# ---------------------------------------------------------------------------
# Health HTTP (0.0.0.0:8099) — in-process thread, not a detached shell
# ---------------------------------------------------------------------------
class HealthState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_loop_ts = 0.0
        self.last_status: dict[str, Any] = {}
        self.started_ts = time.time()
        self.lock = threading.Lock()

    def touch(self) -> None:
        with self.lock:
            self.last_loop_ts = time.time()

    def set_status(self, status: dict[str, Any]) -> None:
        with self.lock:
            self.last_status = dict(status)
            self.last_loop_ts = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            last = self.last_loop_ts
            status = dict(self.last_status)
        age = None if last == 0 else time.time() - last
        stale_after = 2 * max(1, int(self.settings.poll_seconds))
        healthy = last > 0 and age is not None and age < stale_after
        return {
            "healthy": healthy,
            "last_loop_age_s": None if age is None else round(age, 3),
            "stale_after_s": stale_after,
            "uptime_s": round(time.time() - self.started_ts, 1),
            "status": status,
        }


def start_health_server(health: HealthState, port: int, log: Logger) -> ThreadingHTTPServer:
    state = health

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _send(self, code: int, payload, content_type="application/json"):
            body = payload if isinstance(payload, (bytes, bytearray)) else payload.encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            snap = state.snapshot()
            if self.path.split("?", 1)[0] in {"/health", "/health/"}:
                code = 200 if snap["healthy"] else 503
                self._send(code, json.dumps(snap, indent=2) + "\n")
                return
            if self.path.split("?", 1)[0] in {"/status", "/status/"}:
                self._send(200, json.dumps(snap.get("status") or {}, indent=2) + "\n")
                return
            if self.path.split("?", 1)[0] in {"/", "/index.html"}:
                self._send(200, _status_html(snap), "text/html; charset=utf-8")
                return
            self._send(404, json.dumps({"error": "not found"}) + "\n")

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    t = threading.Thread(target=httpd.serve_forever, name="lard-health", daemon=True)
    t.start()
    log(f"health listening 0.0.0.0:{port}/health")
    return httpd


def _status_html(snap: dict) -> str:
    st = snap.get("status") or {}
    healthy = snap.get("healthy")
    rows = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>"
        for k, v in st.items()
        if k not in {"secrets", "password", "token"}
    )
    badge = "ok" if healthy else "down"
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta http-equiv="refresh" content="10"/>
<title>LARD Controller</title>
<style>
 body {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        background:#111; color:#e8e4d9; margin:24px; }}
 h1 {{ font-size:18px; font-weight:600; }}
 .badge {{ display:inline-block; padding:2px 8px; border-radius:4px; }}
 .ok {{ background:#1f6f43; }}
 .down {{ background:#8b2e2e; }}
 table {{ border-collapse:collapse; width:min(880px,100%); }}
 th,td {{ text-align:left; padding:6px 10px; border-bottom:1px solid #333; vertical-align:top; }}
 th {{ color:#9aa; width:240px; font-weight:500; }}
 a {{ color:#8fc1ff; }}
</style></head>
<body>
<h1>LARD Controller <span class="badge {badge}">{badge}</span></h1>
<p>Health: <a href="/health">/health</a> · JSON: <a href="/status">/status</a>
 · writes stay off until <code>enable_writes</code> and the HA master gate are both on.</p>
<table>{rows or "<tr><td>waiting for first loop…</td></tr>"}</table>
</body></html>
"""


def _esc(v) -> str:
    s = str(v)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Controller — energy policy copied from the uploaded actuator
# ---------------------------------------------------------------------------
class Controller:
    def __init__(self, ha: HA, braiins: Braiins, settings: Settings, log: Logger, health: HealthState):
        self.ha = ha
        self.b = braiins
        self.settings = settings
        self.log = log
        self.health = health
        self.mqtt = MqttPublisher(settings, log)
        self.solar_samples: deque[tuple[float, float]] = deque()
        self.actual_mode = "PAUSED"
        self.confirmed_operational = "PAUSED"
        self.desired_mode = "PAUSED"
        self.reason = "boot"
        self.last_error = ""
        self.last_transition_ts = 0.0
        self.last_board_change_ts = 0.0
        self.mode_entered_ts = time.time()
        self.two_ok_since = None
        self.three_ok_since = None
        self.down_ok_since = None
        self.acting = False
        self.dry_reads = 0
        self.power_w = None
        self.boards_str = ""
        self.mode_request = "AUTO"
        self.miner_paused = False
        self.mining_phase = ""
        self._resume_ts = 0.0
        self._braiins_mutex = threading.RLock()
        self._cooling_transition_active = False
        self._cooling_applied: CoolingProfile | None = None
        self._cooling_desired: CoolingProfile | None = None
        self._cooling_last_change_ts = 0.0
        self._fan_max_seen: int | None = None
        self._temp_policy_seen: tuple | None = None
        self._temperature_policy_valid = True
        self._temperature_policy_invalid_logged = False
        self._configured_cooling: dict[str, Any] = {}
        self._miner_prev_ok = False
        self._fan_max_missing_logged = False
        self._chip_temp_f = None
        self._fan_rpm = None
        self._fan_pct = None
        self._thermal_abort_active = False
        self._live_max_fan_speed = None
        self._live_min_fan_speed = None
        self._cooling_txn_active = False
        self._cooling_txn_id = ""
        self._txn_seq = 0
        self._cooling_phase = "IDLE"
        self._health_class = "UNKNOWN"
        self.write_permission = WritePermission()
        if hasattr(self.b, "write_permission"):
            self.b.write_permission = self.write_permission
        self.telemetry_class = "UNKNOWN"
        self.observed_state = "UNKNOWN"
        self.controller_state = "DISARMED"
        self.write_gate = "DISARMED"
        self.observed_miner_mode = "UNVERIFIED"
        self._api_reachable = False
        self._required_telemetry_fresh = False
        self._last_write_blocked: dict[str, Any] | None = None
        self._endpoints: dict[str, dict[str, Any]] = {}
        self._last_verified_miner_mode = ""
        self._fault_latched = False
        self._verification_ctx = {
            "transaction_id": "",
            "entered_mono": 0.0,
            "expected_operation": "",
            "evidence_deadline_mono": 0.0,
            "last_valid_telemetry_mono": 0.0,
            "reason": "",
            "evidence": "",
        }
        self.recovery_ready = False
        self._valid_poll_streak = 0
        self._auth_ok = False
        self._bosminer_available = False
        self._critical_fault = False
        self._transition_evidence = False
        self._transition_evidence_mono = 0.0
        self._apply_depth = 0
        self._helper_miss_until: dict[str, float] = {}
        self._helper_backoff: dict[str, float] = {}
        self._helper_logged_until: dict[str, float] = {}
        self._last_transport = ""
        self._last_http_code: int | None = None
        self._telemetry_last_success_mono = 0.0
        self._board_unverified = False
        self._pending_profile: CoolingProfile | None = None
        self._pending_explicit = False
        self._pending_hold_logged: tuple | None = None
        self._cooling_terminal_kind = ""
        self._cooling_terminal_gen = 0
        self._resume_gate = ""
        self._settle_complete = False
        self._recovery_interim_logged = False
        self._resume_retries_used = 0
        self._resume_retry_used = False
        self._last_cooling_result = ""
        self._last_resume_result = ""
        self._lifecycle_reason = ""
        self._telemetry_freshness = "UNKNOWN"
        self._telemetry_fail_streak = 0
        self._telemetry_fault = False
        self._telemetry_last_success_ts = 0.0
        # Historical sustained-read outage. Not the current error.
        # last_error is cleared on recovery; these fields stay.
        self._last_fault_reason = ""
        self._last_fault_class = ""
        self._last_fault_timestamp = 0.0
        self._last_fault_count = 0
        self._sustained_fault_open = False
        self._sustained_telemetry_recovery_pending = False
        self._sustained_telemetry_recovery_polls = 0
        self._sustained_telemetry_recovery_mono = 0.0
        self._last_good_power_w = None
        self._last_good_boards = ""
        self._last_obs: MinerObservation | None = None
        self._recovery_started_ts = 0.0
        self._recovery_elapsed_s = 0.0
        self._recovery_limit_s = 0.0
        self._primary_recovery_started_ts = 0.0
        self._primary_recovery_deadline_ts = 0.0
        self._txn_lock = threading.RLock()
        self._emit_lock = threading.Lock()
        self._emit_inflight: str | None = None
        self._emit_overlap_violations = 0
        self._txn_owner: int | None = None
        self._owner_txn_id = ""
        self._cancel_requested = False
        self._reload_hold = False
        self._emit_generation = 0

    def writes_allowed(self, enable_on: bool) -> bool:
        """Braiins writes require BOTH the add-on option and the HA gate."""
        return bool(self.settings.enable_writes) and bool(enable_on)

    def fnum(self, entity_id: str, default=None):
        st = self.ha.state(entity_id)
        try:
            if st in (None, "unknown", "unavailable", ""):
                return default
            return float(st)
        except Exception:
            return default

    def update_solar_avg(self) -> float:
        w = self.fnum(ENT_SOLAR_AVAIL)
        if w is None:
            w = self.fnum(ENT_PV, 0.0) or 0.0
        now = time.time()
        self.solar_samples.append((now, float(w)))
        while self.solar_samples and (now - self.solar_samples[0][0]) > SOLAR_AVG_WINDOW_S:
            self.solar_samples.popleft()
        if not self.solar_samples:
            return float(w)
        return sum(v for _, v in self.solar_samples) / len(self.solar_samples)

    def faults_block(self):
        if self.ha.state(ENT_HB) != "on":
            return "heartbeat_lost"
        if self.ha.state(ENT_STALE) == "on":
            return "critical_stale"
        fault = self.ha.state(ENT_FAULT)
        if fault and fault not in ("ok", "unknown", "unavailable", None, "soc_hard_pause"):
            return f"fault:{fault}"
        return None

    def compute_desired(self, solar_avg: float):
        """Resolve requested mode, then apply the uploaded board-priority policy.

        AUTO reads sensor.lard_miner_mode_desired_ha when it holds a valid mode.
        If that template is unknown/unavailable, fall back to the uploaded
        local energy policy (same thresholds — not a new policy).
        Manual input_select values are honored as-is.
        """
        req = self.ha.state(ENT_MODE_REQ) or "AUTO"
        self.mode_request = req

        if req in MODES:
            return req, f"manual_override:{req}"

        ha_desired = self.ha.state(ENT_DESIRED_HA)
        if ha_desired in MODES:
            candidate, reason = ha_desired, f"ha_desired:{ha_desired}"
            soc = self.fnum(ENT_SOC)
            if soc is not None and soc <= 30:
                return "PAUSED", "soc_hard_pause_le_30"
            fb = self.faults_block()
            if fb:
                return "PAUSED", fb
            return self._anti_flap(candidate, reason, solar_avg)

        return self._compute_desired_local(solar_avg)

    def _compute_desired_local(self, solar_avg: float):
        """Exact uploaded AUTO policy (SOC / solar / hold / night-cap)."""
        soc = self.fnum(ENT_SOC)
        solar_now = self.fnum(ENT_SOLAR_AVAIL)
        if solar_now is None:
            solar_now = self.fnum(ENT_PV, 0.0) or 0.0

        if soc is None:
            return "PAUSED", "soc_unavailable"
        if soc <= 30:
            return "PAUSED", "soc_hard_pause_le_30"

        fb = self.faults_block()
        if fb:
            return "PAUSED", fb

        sunny = soc > 30 and solar_avg >= 1500
        weak_ok = soc >= 50
        if not (sunny or weak_ok):
            return "PAUSED", f"await_one_board soc={soc:.0f} solar_avg={solar_avg:.0f}"

        mode = "ONE_BOARD"
        reason = "sunny_path" if sunny and soc < 50 else ("soc_ge_50" if weak_ok else "one_board")

        now = time.time()
        two_cond = soc >= 70 and solar_now >= 800
        three_cond = soc >= 85 and solar_now >= 1100

        if two_cond:
            if self.two_ok_since is None:
                self.two_ok_since = now
            elif (now - self.two_ok_since) >= TWO_HOLD_S:
                mode = "TWO_BOARD"
                reason = f"two_board_hold soc={soc:.0f} solar={solar_now:.0f}"
        else:
            self.two_ok_since = None

        if mode == "TWO_BOARD" and three_cond:
            if self.three_ok_since is None:
                self.three_ok_since = now
            elif (now - self.three_ok_since) >= THREE_HOLD_S:
                mode = "THREE_BOARD"
                reason = f"three_board_hold soc={soc:.0f} solar={solar_now:.0f}"
        elif not three_cond:
            self.three_ok_since = None

        candidate = mode

        if candidate == "THREE_BOARD" and (soc <= 75 or solar_now < 900):
            if self.down_ok_since is None:
                self.down_ok_since = now
            elif (now - self.down_ok_since) >= DOWN_HOLD_S:
                candidate = "TWO_BOARD"
                reason = f"down_three_to_two soc={soc:.0f} solar={solar_now:.0f}"
        elif candidate in ("TWO_BOARD", "THREE_BOARD") and (soc <= 65 or solar_now < 500):
            if self.down_ok_since is None:
                self.down_ok_since = now
            elif (now - self.down_ok_since) >= DOWN_HOLD_S:
                candidate = "ONE_BOARD"
                reason = f"down_to_one soc={soc:.0f} solar={solar_now:.0f}"
        else:
            self.down_ok_since = None

        if candidate in ("TWO_BOARD", "THREE_BOARD") and solar_avg < 800:
            candidate = "ONE_BOARD"
            reason = f"night_cap_one solar_avg={solar_avg:.0f}"

        return self._anti_flap(candidate, reason, solar_avg)

    def _settled_mode(self) -> str:
        """Last confirmed operational mode — never APPLYING/ERROR for policy holds."""
        if self.actual_mode in RANK:
            return self.actual_mode
        if self.confirmed_operational in RANK:
            return self.confirmed_operational
        return "PAUSED"

    def _anti_flap(self, candidate: str, reason: str, _solar_avg: float):
        now = self._wall()
        settled = self._settled_mode()
        if candidate != settled:
            elapsed = now - self.mode_entered_ts
            going_up = RANK[candidate] > RANK.get(settled, 0)
            going_down = RANK[candidate] < RANK.get(settled, 0)
            if going_up and settled != "PAUSED" and elapsed < ANTI_FLAP_UP_S:
                return settled, f"anti_flap_up wait={int(ANTI_FLAP_UP_S - elapsed)}s ({reason})"
            if going_down and candidate != "PAUSED" and elapsed < ANTI_FLAP_DOWN_S:
                return settled, f"anti_flap_down wait={int(ANTI_FLAP_DOWN_S - elapsed)}s ({reason})"
            if (
                going_up
                and settled != "PAUSED"
                and (now - self.last_board_change_ts) < SETTLE_AFTER_BOARD_S
            ):
                wait = int(SETTLE_AFTER_BOARD_S - (now - self.last_board_change_ts))
                return settled, f"settle_after_board wait={wait}s"
        return candidate, reason

    def _now(self) -> float:
        """Monotonic clock for txn deadlines, settle, retry, and budgets.

        Wall clock is not used here. An NTP step must not stretch or compress
        a recovery window. Tests replace this method with a fake clock.
        """
        return time.monotonic()

    def _wall(self) -> float:
        """Wall clock for human-facing stamps only. Never a txn deadline."""
        return time.time()

    def _sleep(self, seconds: float) -> None:
        """Sleep in short chunks so /health stays fresh during long backoffs."""
        remaining = float(seconds)
        while remaining > 1e-9:
            self.health.touch()
            chunk = min(5.0, remaining)
            time.sleep(chunk)
            remaining -= chunk

    def _resume_wait_s(self) -> int:
        return int(RESUME_WAIT_S)

    def _board_wait_s(self) -> int:
        return max(60, int(self.settings.board_wait_seconds))

    def _policy_denial(self) -> str | None:
        """Shared write policy. None means the gates currently allow a write."""
        if self._reload_hold or self._health_class == "INTERRUPTED_MANUAL_REVIEW":
            return "interrupted_manual_review"
        if not bool(self.settings.enable_writes):
            return "enable_writes_false"
        try:
            enable_on = self.ha.state(ENT_ENABLE) == "on"
        except Exception:
            enable_on = False
        if not enable_on:
            return "master_gate_off"
        try:
            old_auto = self.ha.state(ENT_OLD_AUTO) == "on"
        except Exception:
            old_auto = True
        if old_auto:
            return "old_auto_on"
        if self.competing_writer_blocks_arming():
            return "competing_writer"
        return None

    def competing_writer_blocks_arming(self) -> str | None:
        """Arming denial hook. Missing entity is not a writer and is not polled hot.

        ``switch.solar_miner_auto_enable`` is checked separately so its
        existing refusal string stays stable. This hook is the optional
        ``binary_sensor.lard_competing_writer`` fence (upstairs AC mode
        writes, pause/resume scripts, resume-button spam).
        """
        try:
            raw = self._read_cooling_helper(ENT_COMPETING_WRITER, cache_miss=True)
        except Exception:
            return None
        if raw == "on":
            return "competing_writer"
        return None

    def _authorize_device_write(self, action: str) -> str | None:
        """Fail-closed reason, or None when this exact command may be emitted.

        Called again immediately before the socket/client call. Cooling
        commands also require the live transaction id, a non-terminal phase
        that allows that command, fresh pause for a cooling PUT, and a retry
        count still inside the per-transaction maximum.
        """
        if action in {"start", "restart", "reboot"}:
            return "structural_deny"
        policy = self._policy_denial()
        if policy:
            return policy
        cooling = action in {
            "pause",
            "cooling_put",
            "resume",
            "failsafe_pause",
            "failsafe_restore",
        }
        # Mining pause/resume use mode_pause / mode_resume and are not in this set.
        # Cooling pause, cooling PUT, and thermal-abort restore stay denied
        # while Braiins owns cooling.
        if cooling and not self._cooling_control_enabled():
            return "cooling_control_disabled"
        if self._health_class in TERMINAL_HEALTH or (
            not self._cooling_txn_active and self._cooling_terminal_kind in {"degraded", "error"}
        ):
            return "terminal"
        if cooling:
            if not self._cooling_txn_active or self._txn_owner != threading.get_ident():
                return "no_txn_owner"
            if (
                not self._owner_txn_id
                or self._owner_txn_id != self._cooling_txn_id
            ):
                return "txn_id_mismatch"
            if self._cancel_requested:
                return "cancelled"
            phase = self._cooling_phase
            allowed = {
                "pause": {"PAUSE_REQUESTED"},
                "cooling_put": {"COOLING_APPLYING"},
                "resume": {"RESUME_REQUESTED", "RETRY_RESUME_ONCE"},
            }.get(action)
            if allowed is not None and phase not in allowed:
                return f"phase_{phase or 'none'}"
            if action == "resume" and phase == "RETRY_RESUME_ONCE":
                limit = int(self.settings.max_resume_retries_per_transaction or 0)
                if not self.settings.resume_retry_enabled or self._resume_retries_used > limit:
                    return "retry_limit"
            obs = self._last_obs
            if action == "pause" and obs is not None and obs.ok and self._hard_fault(obs):
                return "hard_fault"
            if action == "cooling_put":
                if self._telemetry_freshness != "FRESH":
                    return "stale"
                if obs is None or not obs.ok:
                    return "stale"
                if self._hard_fault(obs):
                    return "hard_fault"
                if not self._cooling_paused_idle(obs):
                    return "not_paused"
                if obs.running and not self._cooling_paused_idle(obs):
                    return "hashing"
            return None
        me = threading.get_ident()
        if self._cooling_txn_active and self._txn_owner not in (None, me):
            return "other_owner"
        if self._cancel_requested and self._cooling_txn_active:
            return "cancelled"
        return None

    def _log_write_blocked(self, op: str, source: str, reason: str) -> None:
        """Structured WRITE_BLOCKED. Emitted before any Braiins write body is built."""
        record = write_blocked_record(
            requested_operation=op,
            source=source,
            controller_state=self.controller_state,
            enable_writes=bool(self.settings.enable_writes),
            reason=reason,
        )
        record["actual_mode"] = self.actual_mode
        record["observed_miner_mode"] = self.observed_miner_mode
        record["observed_state"] = self.observed_state
        record["recovery_ready"] = bool(self.recovery_ready)
        record["telemetry_class"] = self.telemetry_class
        record["writes_permitted"] = bool(self.write_permission.permitted)
        record["cooling_control_enabled"] = bool(self.settings.cooling_control_enabled)
        self._last_write_blocked = record
        self.write_permission.controller_state = self.controller_state
        if not self.settings.enable_writes:
            self._drop_stale_write_queue(reason)
        self.log(
            "write blocked "
            f"result=WRITE_BLOCKED op={op} requested_operation={op} "
            f"source={source} reason={reason} "
            f"controller_state={self.controller_state} "
            f"enable_writes={str(bool(self.settings.enable_writes)).lower()} "
            f"network_write_sent=false "
            f"state={json.dumps(record, sort_keys=True)}"
        )

    def _deny_write(self, action: str, reason: str) -> None:
        """Log a denial. A mid-transaction denial holds the miner for review."""
        self._log_write_blocked(action, "controller", reason)
        self._log_txn(f"write_denied action={action} reason={reason}")
        self._emit("lard_write_denied", action=action, reason=reason)
        self._last_cooling_result = f"denied_{reason}"
        if self._cooling_txn_active:
            self._cancel_requested = True
            self._reload_hold = True
            if self._health_class not in {"DEGRADED_NEEDS_ATTENTION", "ERROR"}:
                self._health_class = "INTERRUPTED_MANUAL_REVIEW"
                self._cooling_phase = "INTERRUPTED"
            self.last_error = f"write_denied:{action}:{reason}"

    def _call_device(self, action: str, fn):
        """Re-check authorization immediately before a device-changing call.

        Returns None when denied. The client function is not called.
        ``_emit_lock`` serializes device calls so two commands cannot overlap.
        """
        reason = self._authorize_device_write(action)
        if reason:
            self._deny_write(action, reason)
            return None
        with self._emit_lock:
            reason = self._authorize_device_write(action)
            if reason:
                self._deny_write(action, reason)
                return None
            if self._emit_inflight:
                self._emit_overlap_violations += 1
                self._deny_write(action, "overlap")
                return None
            self._emit_inflight = action
            # Permit only this already-authorized call. The gate returns to
            # disarmed immediately after, including on reload and on recovery.
            self.write_permission.permitted = True
            try:
                return fn()
            finally:
                self.write_permission.permitted = False
                self.write_permission.reason = "call_finished_disarmed"
                self._emit_inflight = None

    def _device_tuple(self, action: str, fn):
        result = self._call_device(action, fn)
        if result is None:
            body = dict(self._last_write_blocked or {})
            body.setdefault("denied", True)
            body.setdefault("network_write_sent", False)
            body.setdefault("result", "WRITE_BLOCKED")
            return 0, body
        return result

    def _denied_body(self, body) -> bool:
        return isinstance(body, dict) and bool(body.get("denied"))

    def _claim_cooling_owner(self) -> None:
        self._cooling_txn_active = True
        self._txn_owner = threading.get_ident()
        self._cancel_requested = False
        self._write_inflight_marker()

    def _release_cooling_owner(self) -> None:
        self._cooling_txn_active = False
        self._txn_owner = None
        self._clear_inflight_marker()

    def _inflight_path(self) -> Path:
        return Path(self.settings.data_dir) / INFLIGHT_TXN_NAME

    def _write_inflight_marker(self) -> None:
        """Persist phase for reload detection. Do not store a monotonic deadline."""
        payload = {
            "txn_id": self._cooling_txn_id,
            "phase": self._cooling_phase,
            "wall_iso": utc_iso(),
            "monotonic_deadline": None,
            "resume_on_load": False,
        }
        try:
            self._inflight_path().parent.mkdir(parents=True, exist_ok=True)
            self._inflight_path().write_text(json.dumps(payload))
        except Exception as e:
            self.log(f"inflight marker skip: {e}")

    def _clear_inflight_marker(self) -> None:
        try:
            self._inflight_path().unlink(missing_ok=True)
        except Exception:
            return

    def cancel_cooling_transaction(self, reason: str = "cancel") -> None:
        """Invalidate the in-flight txn. Do not Resume, Start, Restart, or PUT."""
        with self._txn_lock:
            self._cancel_requested = True
            self._emit_generation += 1
            self._cooling_txn_id = ""
            self._reload_hold = True
            self._health_class = "INTERRUPTED_MANUAL_REVIEW"
            self._cooling_phase = "INTERRUPTED"
            self.last_error = f"cancelled:{reason}"
            self._log_txn(f"cancel reason={reason} — no resume, no start, no restart")
            self._emit("lard_cooling_interrupted", reason=reason, source="cancel")

    def reconcile_after_reload(self) -> str:
        """New process. Drop in-memory deadlines. Never auto-resume a paused txn.

        A monotonic timestamp from a previous process is not a deadline here.
        Default posture after an interrupted marker is observe-only manual review.
        """
        raw = None
        path = self._inflight_path()
        try:
            if path.is_file():
                raw = json.loads(path.read_text())
        except Exception:
            raw = {"txn_id": "", "phase": "unreadable", "malformed": True}
        self._clear_inflight_marker()
        self.write_permission.disarm("reload", "reload_disarmed")
        self.write_gate = "ARMED" if self.settings.enable_writes else "DISARMED"
        if self._fault_latched or self.actual_mode == "FAULT_LATCHED":
            self.controller_state = "FAULT_LATCHED"
        else:
            self.controller_state = "DISARMED"
            self.observed_miner_mode = "UNVERIFIED"
        self.write_permission.controller_state = self.controller_state
        self._primary_recovery_started_ts = 0.0
        self._primary_recovery_deadline_ts = 0.0
        self._recovery_started_ts = 0.0
        self._recovery_limit_s = 0.0
        self._recovery_elapsed_s = 0.0
        self._cooling_txn_active = False
        self._cooling_txn_id = ""
        self._owner_txn_id = ""
        self._txn_owner = None
        self._cancel_requested = False
        self._pending_profile = None
        self._pending_explicit = False
        self._emit_generation += 1
        if not raw:
            self._reload_hold = False
            return "clean"
        prior = str(raw.get("txn_id") or "")
        prior_phase = str(raw.get("phase") or "")
        # Ignore any deadline number a crashed process might have written.
        self._reload_hold = True
        self._health_class = "INTERRUPTED_MANUAL_REVIEW"
        self._cooling_phase = "INTERRUPTED"
        self._cooling_terminal_kind = "interrupted"
        self.last_error = "interrupted_manual_review"
        self._lifecycle_reason = "reload_interrupted"
        self._log_txn(
            f"reload_interrupted prior_txn={prior} prior_phase={prior_phase} "
            f"monotonic_deadline_ignored=1 — no auto resume"
        )
        self._emit(
            "lard_cooling_interrupted",
            reason="reload",
            prior_txn=prior,
            prior_phase=prior_phase,
        )
        return "interrupted"

    def _http_retry(self, fn, what: str):
        """Call fn() -> (code, body). Retry transient 5xx; fail only after backoff."""
        last_code, last_body = None, None
        attempts = (0,) + API_5XX_BACKOFF_S
        for i, delay in enumerate(attempts):
            if delay:
                self.log(f"{what} transient http={last_code} retry_in={delay}s")
                self._sleep(delay)
            code, body = fn()
            last_code, last_body = code, body
            if code == 200 or not is_http_5xx(code):
                return code, body
        self.log(f"{what} sustained http={last_code} after backoff")
        return last_code, last_body

    def _observe_retrying(self) -> MinerObservation:
        last = MinerObservation()
        attempts = (0,) + API_5XX_BACKOFF_S
        for i, delay in enumerate(attempts):
            if delay:
                self.log(f"observe transient {self.last_error} retry_in={delay}s")
                self._sleep(delay)
            last = self.observe_miner()
            if last.ok:
                return last
            if not error_is_http_5xx(self.last_error or ""):
                return last
        return last

    def _normalize_board_ids(self, enabled_ids) -> list[str]:
        enabled_ids = [norm_board_id(x) for x in (enabled_ids or []) if norm_board_id(x)]
        if any(i in enabled_ids for i in ("2", "3")) and "1" not in enabled_ids:
            enabled_ids = ["1"] + [i for i in enabled_ids if i != "1"]
        return enabled_ids

    def _boards_match(self, actual_ids, expect_ids) -> bool:
        return sorted(self._normalize_board_ids(actual_ids)) == sorted(self._normalize_board_ids(expect_ids))

    def _incomplete_boards(self, actual_ids, expect_ids) -> bool:
        """Empty/unknown read — transient during pause/ramp. A full ONE/TWO/THREE
        topology that is simply smaller than expect is a real mismatch (PATCH)."""
        actual = set(self._normalize_board_ids(actual_ids))
        expect = set(self._normalize_board_ids(expect_ids))
        if not expect:
            return False
        if not actual:
            return True
        if actual < expect:
            actual_l = sorted(actual)
            if any(self._boards_match(actual_l, BOARD_MAP[m]) for m in BOARD_MAP):
                return False
            return True
        return False

    def _known_board_ids(self) -> list[str]:
        raw = (self.boards_str or "").strip()
        if not raw or raw in {"none", "unknown"}:
            return []
        return self._normalize_board_ids(raw.split(","))

    def _boards_already_satisfied(self, mode: str, obs: MinerObservation | None = None) -> bool:
        """Stage B is done: topology already matches, or PAUSED↔ONE_BOARD same map."""
        if mode not in BOARD_MAP:
            return False
        expect = BOARD_MAP[mode]
        if obs is not None and obs.boards_ok and self._boards_match(obs.enabled_ids, expect):
            return True
        # A populated non-matching read wins over last-known / identical-map skip.
        if (
            obs is not None
            and obs.boards_ok
            and obs.enabled_ids
            and not self._incomplete_boards(obs.enabled_ids, expect)
            and not self._boards_match(obs.enabled_ids, expect)
        ):
            return False
        known = self._known_board_ids()
        if known and self._boards_match(known, expect):
            return True
        settled = self._settled_mode()
        same_map = settled in BOARD_MAP and self._boards_match(BOARD_MAP[settled], expect)
        if not same_map:
            return False
        # Identical map (PAUSED and ONE_BOARD are both [1]). Skip PATCH/wait
        # unless we positively see a different populated topology.
        if obs is None or not obs.boards_ok or self._incomplete_boards(obs.enabled_ids, expect):
            return True
        return self._boards_match(obs.enabled_ids, expect)

    def _read_boards_once(self):
        try:
            actual, code, _ = self.b.enabled_ids()
            return actual, code
        except Exception as e:
            self.last_error = f"read_boards_exc:{e}"
            return None, None

    def _ensure_boards(self, enabled_ids: list[str]) -> bool:
        expect = self._normalize_board_ids(enabled_ids)
        if self._boards_match(self._known_board_ids(), expect):
            self.log(f"boards already match {expect}, skip PATCH/wait")
            return True

        last_actual, last_code = [], None
        for delay in (0,) + API_5XX_BACKOFF_S:
            if delay:
                self.log(
                    f"read_boards retry_in={delay}s last_http={last_code} last_actual={last_actual}"
                )
                self._sleep(delay)
            actual, code = self._read_boards_once()
            last_actual, last_code = actual, code
            if actual is None and code is None:
                return False
            if is_http_5xx(code):
                continue
            if code != 200:
                self.last_error = f"read_boards_http_{code}"
                return False
            if self._boards_match(actual, expect):
                self.boards_str = ",".join(sorted(expect)) if expect else "none"
                self.log(f"boards already match {expect}, skip PATCH/wait")
                return True
            if self._incomplete_boards(actual, expect):
                self.log(f"boards incomplete actual={actual} expect={expect}")
                continue
            return self._set_boards(expect)

        if last_code == 200 and self._boards_match(last_actual or [], expect):
            self.boards_str = ",".join(sorted(expect)) if expect else "none"
            return True
        if last_code is not None and last_code != 200:
            self.last_error = f"read_boards_http_{last_code}"
            return False
        if self._boards_match(self._known_board_ids(), expect):
            self.log(f"boards last-known match {expect}, skip PATCH/wait after empty polls")
            return True
        return self._set_boards(expect)

    def _set_boards(self, enabled_ids: list[str]) -> bool:
        enabled_ids = self._normalize_board_ids(enabled_ids)
        # Never PATCH/wait if the live set already matches (int vs str included).
        actual, code = self._read_boards_once()
        if code == 200 and actual is not None and self._boards_match(actual, enabled_ids):
            self.boards_str = ",".join(sorted(enabled_ids)) if enabled_ids else "none"
            self.log(f"boards already match {enabled_ids} before PATCH, skip wait")
            return True

        all_ids = ["1", "2", "3"]
        to_enable = [i for i in all_ids if i in enabled_ids]
        to_disable = [i for i in all_ids if i not in enabled_ids]

        if to_disable:
            code, _ = self._http_retry(
                lambda: self._device_tuple(
                    "patch_boards", lambda: self.b.patch_boards(False, to_disable)
                ),
                "PATCH disable",
            )
            self.log(f"PATCH disable {to_disable} http={code}")
            if self._denied_body(_) or code == 0:
                return False
            if code != 200:
                self.last_error = f"board_disable_http_{code}"
                return False
        if to_enable:
            code, _ = self._http_retry(
                lambda: self._device_tuple(
                    "patch_boards", lambda: self.b.patch_boards(True, to_enable)
                ),
                "PATCH enable",
            )
            self.log(f"PATCH enable {to_enable} http={code}")
            if self._denied_body(_) or code == 0:
                return False
            if code != 200:
                self.last_error = f"board_enable_http_{code}"
                return False

        # HTTP 200 on hashboard PATCH = accepted, not applied.
        # Poll GET; empty/partial/5xx are transient. Compare with normalized ids.
        expect = sorted(to_enable)
        deadline = self._now() + self._board_wait_s()
        empty_attempt = 0
        last_actual: list[str] = []
        while self._now() < deadline:
            self.health.touch()
            try:
                actual, code, _ = self.b.enabled_ids()
            except Exception as e:
                self.log(f"board_poll exc: {e}")
                actual, code = [], 0
            last_actual = list(actual or [])
            self.log(f"board_poll expect={expect} actual={actual} http={code}")
            # PATCH HTTP 200 is not success. Readback must match.
            if board_patch_readback(expect, actual, code) == "verified":
                self._board_unverified = False
                self.last_board_change_ts = self._wall()
                self.boards_str = ",".join(sorted(expect)) if expect else "none"
                return True
            transient = is_http_5xx(code) or (
                code == 200 and self._incomplete_boards(actual, expect)
            )
            if transient and empty_attempt < len(API_5XX_BACKOFF_S):
                delay = API_5XX_BACKOFF_S[empty_attempt]
                empty_attempt += 1
                self.log(f"board_poll transient retry_in={delay}s")
                self._sleep(delay)
                continue
            self._sleep(BOARD_POLL_S)
        status = board_patch_readback(expect, last_actual, 200)
        self._board_unverified = status != "verified"
        self.telemetry_class = "FAULT_LATCHED"
        self.observed_state = "FAULT_LATCHED"
        self._fault_latched = True
        self.controller_state = "FAULT_LATCHED"
        self.last_error = (
            f"board_wait_timeout expect={expect} readback={status}"
            "|board_verification_timeout_or_mismatch"
        )
        self.log(
            f"board_readback faulted_unverified expect={expect} "
            f"readback={status} patch_http=200"
        )
        return False

    def _ensure_power_target(self) -> bool:
        try:
            code, payload = self.b.get_power_target()
            current = _find_number(payload, ("watt", "wattage", "power")) if isinstance(payload, dict) else None
            if code == 200 and current is not None and int(round(current)) == int(self.settings.power_target_w):
                return True
        except Exception as e:
            self.log(f"power_target_read_skip: {e}")
        code, body = self._http_retry(
            lambda: self._device_tuple(
                "set_power", lambda: self.b.set_power(self.settings.power_target_w)
            ),
            "power_target",
        )
        self.log(f"power_target http={code}")
        if self._denied_body(body) or code == 0:
            return False
        if code != 200:
            self.last_error = f"power_target_http_{code}"
            return False
        return True

    def read_fan_max_pct(self) -> int:
        raw = self.ha.state(ENT_FAN_MAX)
        if raw in (None, "unknown", "unavailable", ""):
            if not self._fan_max_missing_logged:
                self.log(
                    f"fan_max helper {ENT_FAN_MAX} missing/unavailable — "
                    f"defaulting to {FAN_MAX_DEFAULT}. Install ha_packages/lard_fan_max.yaml"
                )
                self._fan_max_missing_logged = True
            return FAN_MAX_DEFAULT
        return clamp_fan_max_pct(raw)

    def ensure_fan_max_helper(self) -> None:
        """Idempotent HA helper. REST cannot create a real input_number; POST state if missing."""
        try:
            raw = self.ha.state(ENT_FAN_MAX)
            if raw not in (None, "unknown", "unavailable", ""):
                return
            self.ha.set_state(
                ENT_FAN_MAX,
                FAN_MAX_DEFAULT,
                {
                    "friendly_name": "LARD Fan Max %",
                    "min": FAN_MAX_MIN,
                    "max": FAN_MAX_MAX,
                    "step": 1,
                    "mode": "box",
                    "unit_of_measurement": "%",
                    "icon": "mdi:fan",
                    "source": "lard_controller",
                },
            )
            self.log(
                f"ensured {ENT_FAN_MAX}={FAN_MAX_DEFAULT} via HA state API "
                "(install ha_packages/lard_fan_max.yaml for a real helper slider)"
            )
        except Exception as e:
            self.log(f"fan_max helper ensure failed: {e}")
        self._install_fan_max_package()

    def ensure_cooling_target_helpers(self) -> None:
        """State stubs for native setpoints. REST cannot create a real input_number.

        Operator unit is °F. The compatibility entity IDs still end in _c;
        the stub number is Fahrenheit (158 from a 70 °C option, not 70).
        Preferred *_f entities are not stubbed: a phantom _f state would
        hide a real _c helper. This writes Home Assistant state only.
        It does not call the miner.
        """
        specs = (
            (
                ENT_COOLING_TARGET_C,
                int(round(celsius_to_fahrenheit(self.settings.cooling_target_temperature_c))),
                "LARD Cooling Target °F",
                "mdi:thermometer",
                "°F",
                HELPER_F_SLIDER_MIN,
                HELPER_F_SLIDER_MAX,
                "slider",
            ),
            (
                ENT_COOLING_HOT_C,
                int(round(celsius_to_fahrenheit(self.settings.cooling_hot_temperature_c))),
                "LARD Cooling Hot °F",
                "mdi:thermometer-high",
                "°F",
                HELPER_F_SLIDER_MIN,
                HELPER_F_SLIDER_MAX,
                "slider",
            ),
            (
                ENT_COOLING_DANGEROUS_C,
                int(round(celsius_to_fahrenheit(self.settings.cooling_dangerous_temperature_c))),
                "LARD Cooling Dangerous °F",
                "mdi:thermometer-alert",
                "°F",
                HELPER_F_SLIDER_MIN,
                HELPER_F_SLIDER_MAX,
                "slider",
            ),
            (
                ENT_COOLING_ENVELOPE_MIN,
                self.settings.cooling_envelope_min_fan_pct,
                "LARD Cooling Envelope Min %",
                "mdi:fan",
                "%",
                FAN_MAX_MIN,
                FAN_MAX_MAX,
                "box",
            ),
            (
                ENT_COOLING_ENVELOPE_MAX,
                self.settings.cooling_envelope_max_fan_pct,
                "LARD Cooling Envelope Max %",
                "mdi:fan",
                "%",
                FAN_MAX_MIN,
                FAN_MAX_MAX,
                "box",
            ),
        )
        for entity_id, initial, name, icon, unit, lo, hi, mode in specs:
            try:
                raw = self.ha.state(entity_id)
                if raw not in (None, "unknown", "unavailable", ""):
                    continue
                attrs = {
                    "friendly_name": name,
                    "min": lo,
                    "max": hi,
                    "step": 1,
                    "mode": mode,
                    "unit_of_measurement": unit,
                    "icon": icon,
                    "source": "lard_controller",
                }
                if unit == "°F":
                    attrs["operator_unit"] = "°F"
                    attrs["entity_id_note"] = (
                        "historical _c suffix; numeric state is Fahrenheit"
                    )
                self.ha.set_state(entity_id, int(initial), attrs)
                self.log(
                    f"ensured {entity_id}={initial} {unit} via HA state API "
                    "(install ha_packages/lard_cooling_target.yaml for real helpers)"
                )
            except Exception as e:
                self.log(f"cooling target helper ensure failed {entity_id}: {e}")

    def _install_fan_max_package(self) -> None:
        """Copy shipped YAML packages into /config/packages when that dir already exists."""
        dest_dir = Path("/config/packages")
        if not dest_dir.is_dir():
            return
        for name in (
            "lard_fan_max.yaml",
            "lard_cooling_profiles.yaml",
            "lard_cooling_target.yaml",
        ):
            dest = dest_dir / name
            candidates = [
                Path(__file__).resolve().parent / "ha_packages" / name,
                Path(__file__).resolve().parent.parent / "ha_packages" / name,
                Path(f"/app/ha_packages/{name}"),
            ]
            src = next((p for p in candidates if p.is_file()), None)
            if src is None:
                continue
            try:
                text = src.read_text()
                if dest.is_file():
                    continue
                dest.write_text(text)
                self.log(f"installed {dest} — reload input_number or restart Core to load the helper")
            except Exception as e:
                self.log(f"{name} package install skip: {e}")

    def _refresh_cooling_telemetry(self) -> None:
        try:
            code, state = self.b.get_cooling_state()
        except Exception as e:
            self._note_endpoint(
                "cooling",
                ok=False,
                failure_class="COOLING_UNAVAILABLE",
                summary=str(e),
            )
            self.log(f"cooling_state skip: {e}")
            return
        if code != 200:
            cooling_class = (
                "BOSMINER_UNAVAILABLE"
                if code == 412
                or any(
                    marker in _summarize_http_body(state).lower()
                    for marker in _BOSMINER_MARKERS
                )
                else "COOLING_UNAVAILABLE"
            )
            self._note_endpoint(
                "cooling",
                ok=False,
                failure_class=cooling_class,
                summary=_summarize_http_body(state) or f"http_{code}",
            )
            self.log(f"cooling_state http={code} body={_summarize_http_body(state)}")
            return
        self._note_endpoint("cooling", ok=True)
        tel = parse_cooling_telemetry(state if isinstance(state, dict) else {})
        self._chip_temp_f = tel.get("chip_temp_f")
        self._fan_rpm = tel.get("fan_rpm")
        self._fan_pct = tel.get("fan_pct")
        self._live_max_fan_speed = tel.get("max_fan_speed")
        self._live_min_fan_speed = tel.get("min_fan_speed")
        self._refresh_configured_cooling()

    def _refresh_configured_cooling(self) -> None:
        """Observe configured mode and setpoints. Never writes."""
        fn = getattr(self.b, "get_miner_configuration", None)
        if not callable(fn):
            return
        try:
            code, body = fn()
        except Exception as e:
            self.log(f"configuration/miner skip: {e}")
            return
        if code != 200 or not isinstance(body, dict):
            self.log(f"configuration/miner http={code}")
            return
        self._configured_cooling = parse_configured_cooling(body)

    def _thermal_abort_needed(self) -> bool:
        if thermal_fault_name(self.ha.state(ENT_FAULT)):
            return True
        if self._chip_temp_f is not None and float(self._chip_temp_f) >= CHIP_ABORT_F:
            return True
        return False

    def _unconstrained_cooling(self, name: str = "ABORT") -> CoolingProfile:
        """Open the fan envelope to 100%. Keep native setpoints on that path.

        A partial auto PUT that omits target_temperature clears it on this
        firmware. Thermal abort must not drop the temperature target.
        """
        if self._native_temperature_policy():
            policy = self.desired_temperature_policy()
            if policy is not None:
                return CoolingProfile(
                    name=name,
                    max_fan_speed=FAN_MAX_DEFAULT,
                    min_fan_speed=policy.min_fan_speed,
                    minimum_required_fans=MIN_REQUIRED_FANS,
                    target_temperature_c=policy.target_temperature_c,
                    hot_temperature_c=policy.hot_temperature_c,
                    dangerous_temperature_c=policy.dangerous_temperature_c,
                )
        return CoolingProfile(
            name=name,
            max_fan_speed=FAN_MAX_DEFAULT,
            min_fan_speed=None,
            minimum_required_fans=MIN_REQUIRED_FANS,
        )

    def _read_pct_entity(self, entity_id: str, default: int) -> int:
        raw = self.ha.state(entity_id)
        if raw in (None, "unknown", "unavailable", ""):
            return clamp_fan_max_pct(default)
        return clamp_fan_max_pct(raw)

    def _cooling_control_enabled(self) -> bool:
        """True only when the operator explicitly re-armed LARD cooling writes.

        Default false. Braiins OS owns fan PWM, temperature target, and
        thermal fan response. Hashboard / pause / resume / power-target
        mining control does not consult this flag.
        """
        return bool(self.settings.cooling_control_enabled)

    def _legacy_auto_cool_armed(self) -> bool:
        """Fan-ceiling attach on apply_mode. Native policy does not use this.

        Thermal abort still opens the envelope through the gated sequence
        only when cooling control is explicitly enabled. While Braiins owns
        cooling, board-count changes do not PUT cooling and do not pause
        for a fan write.
        """
        if not self._cooling_control_enabled():
            return False
        if self._thermal_abort_active:
            return True
        if self._native_temperature_policy():
            return False
        return bool(self.settings.auto_fan_ceiling_enabled)

    def _native_temperature_policy(self) -> bool:
        """True unless the operator explicitly selected the legacy fan-ceiling policy."""
        return str(self.settings.cooling_policy).strip().lower() != COOLING_POLICY_LEGACY

    def _ha_state(self, entity_id: str, *, quiet: bool) -> Any:
        fn = self.ha.state
        try:
            return fn(entity_id, quiet=quiet)
        except TypeError:
            return fn(entity_id)

    def _note_helper_miss(self, entity_id: str) -> None:
        """Cache a missing optional helper. One log line per backoff window.

        Uses the monotonic clock. A miss is not miner telemetry, and while
        cooling control is off it does not increment the HA API fail counter
        (the read is quiet).
        """
        delay = float(self._helper_backoff.get(entity_id, HELPER_MISS_BACKOFF_START_S))
        until = self._now() + delay
        self._helper_miss_until[entity_id] = until
        self._helper_backoff[entity_id] = min(delay * 2.0, HELPER_MISS_BACKOFF_MAX_S)
        if self._helper_logged_until.get(entity_id, 0.0) <= self._now():
            self.log(
                f"helper_miss entity={entity_id} optional=true "
                f"backoff_s={delay:g} cooling_control={str(self._cooling_control_enabled()).lower()} "
                "— cached miss, not miner telemetry"
            )
            self._helper_logged_until[entity_id] = until

    def _read_cooling_helper(self, entity_id: str, *, cache_miss: bool) -> Any:
        """Quiet read. Optional aliases negative-cache transport misses only.

        The number is returned unchanged. No unit conversion.
        """
        now = self._now()
        if cache_miss:
            until = self._helper_miss_until.get(entity_id)
            if until is not None and now < until:
                return None
        raw = self._ha_state(entity_id, quiet=True)
        if raw not in (None, "unknown", "unavailable", ""):
            self._helper_miss_until.pop(entity_id, None)
            self._helper_backoff.pop(entity_id, None)
            return raw
        if cache_miss and bool(getattr(self.ha, "last_read_absent", False)):
            self._note_helper_miss(entity_id)
        return None

    def _operator_temp_f(self, f_entity: str, c_entity: str, default_c: int) -> float | None:
        """One setpoint in operator °F.

        Canonical entity is the verified historical id
        ``input_number.lard_cooling_*_c``. Its numeric state is °F. The ``_c``
        suffix is not unit metadata and is not converted. Optional ``_f``
        aliases are consulted only when the canonical helper has no number,
        and a missing alias is a cached miss with backoff — it is not polled
        every tick and it is not a miner telemetry failure.
        If neither helper has a number, express the add-on option (internal
        °C) as °F so the order check uses one unit. The option is not
        reinterpreted as Fahrenheit. Conversion to ``degree_c`` still happens
        only inside ``operator_setpoints_to_degree_c`` when a cooling profile
        is built, and cooling writes stay off unless cooling control is on.
        """
        canonical = parse_helper_temp(self._read_cooling_helper(c_entity, cache_miss=False))
        if canonical is not None:
            return canonical
        optional = parse_helper_temp(self._read_cooling_helper(f_entity, cache_miss=True))
        if optional is not None:
            return optional
        try:
            return celsius_to_fahrenheit(default_c)
        except (TypeError, ValueError):
            return None

    def temperature_setpoints(self) -> tuple[int, int, int] | None:
        """Operator °F setpoints converted to integer degree_c for the PUT.

        Order is checked in °F first. Conversion is
        c = round((f - 32) * 5 / 9). OpenAPI 0–200 °C is checked after that,
        and the converted integers must stay strictly ordered. Else refuse.
        """
        target_f = self._operator_temp_f(
            ENT_COOLING_TARGET_F,
            ENT_COOLING_TARGET_C,
            self.settings.cooling_target_temperature_c,
        )
        hot_f = self._operator_temp_f(
            ENT_COOLING_HOT_F,
            ENT_COOLING_HOT_C,
            self.settings.cooling_hot_temperature_c,
        )
        danger_f = self._operator_temp_f(
            ENT_COOLING_DANGEROUS_F,
            ENT_COOLING_DANGEROUS_C,
            self.settings.cooling_dangerous_temperature_c,
        )
        if target_f is None or hot_f is None or danger_f is None:
            return None
        return operator_setpoints_to_degree_c(target_f, hot_f, danger_f)

    def desired_temperature_policy(self) -> CoolingProfile | None:
        """Automatic cooling setpoint plus a wide fan envelope. Not per-board fan max.

        Temperatures on the returned profile are already integer °C for the
        PUT body. Helpers were read as °F and converted here.
        """
        temps = self.temperature_setpoints()
        if temps is None:
            self._temperature_policy_valid = False
            return None
        target, hot, danger = temps
        min_n = self._read_pct_entity(
            ENT_COOLING_ENVELOPE_MIN, self.settings.cooling_envelope_min_fan_pct
        )
        max_n = self._read_pct_entity(
            ENT_COOLING_ENVELOPE_MAX, self.settings.cooling_envelope_max_fan_pct
        )
        # OpenAPI: max_fan_speed must be greater than min_fan_speed.
        if max_n <= min_n:
            self._temperature_policy_valid = False
            return None
        self._temperature_policy_valid = True
        min_opt = None if min_n <= 0 else min_n
        fans = MIN_REQUIRED_FANS if max_n >= FAN_MAX_DEFAULT else None
        return CoolingProfile(
            "TEMPERATURE_TARGET",
            max_n,
            min_opt,
            fans,
            target_temperature_c=target,
            hot_temperature_c=hot,
            dangerous_temperature_c=danger,
        )

    def _temperature_policy_signature(self) -> tuple | None:
        profile = self.desired_temperature_policy()
        if profile is None:
            return None
        return (
            profile.target_temperature_c,
            profile.hot_temperature_c,
            profile.dangerous_temperature_c,
            profile.min_fan_speed or 0,
            profile.max_fan_speed,
        )

    def _note_temperature_policy(self) -> bool:
        """True when the operator setpoint/envelope changed after the seed sample.

        The first sample is a seed. It does not schedule a cooling PUT.
        While writes are disarmed, tick absorbs the signature so arming writes
        does not replay an observe-only change.
        """
        sig = self._temperature_policy_signature()
        if sig is None:
            if not self._temperature_policy_invalid_logged:
                self.log(
                    "temperature policy invalid "
                    "(need target < hot < dangerous in °F, then "
                    "degree_c = round((f - 32) * 5 / 9) inside 0–200 "
                    "and still strictly ordered, and max_fan > min_fan) "
                    "— no cooling PUT"
                )
                self._temperature_policy_invalid_logged = True
            return False
        self._temperature_policy_invalid_logged = False
        if self._temp_policy_seen is None:
            self._temp_policy_seen = sig
            return False
        return sig != self._temp_policy_seen

    def _mark_temperature_policy_seen(self) -> None:
        sig = self._temperature_policy_signature()
        if sig is not None:
            self._temp_policy_seen = sig

    def _absorb_temperature_policy_while_disarmed(self) -> None:
        sig = self._temperature_policy_signature()
        if sig is not None:
            self._temp_policy_seen = sig

    def desired_cooling_profile(self, mode: str, *, abort: bool = False) -> CoolingProfile:
        """Resolve the cooling body for this tick.

        Native policy ignores board count. Legacy policy still builds one
        fan-ceiling envelope per board-count state.
        """
        if abort:
            self._thermal_abort_active = True
            return self._unconstrained_cooling("ABORT")
        self._thermal_abort_active = False
        if self._native_temperature_policy():
            policy = self.desired_temperature_policy()
            if policy is not None:
                return policy
            if self._cooling_applied is not None:
                return self._cooling_applied
            return CoolingProfile("INVALID", FAN_MAX_DEFAULT, None, MIN_REQUIRED_FANS)
        key = mode if mode in RANK else "PAUSED"
        max_defaults = {
            "ONE_BOARD": self.settings.cooling_one_board_max_fan_pct,
            "TWO_BOARD": self.settings.cooling_two_board_max_fan_pct,
            "THREE_BOARD": self.settings.cooling_three_board_max_fan_pct,
            "PAUSED": self.settings.cooling_paused_max_fan_pct,
        }
        min_defaults = {
            "ONE_BOARD": self.settings.cooling_one_board_min_fan_pct,
            "TWO_BOARD": self.settings.cooling_two_board_min_fan_pct,
            "THREE_BOARD": self.settings.cooling_three_board_min_fan_pct,
            "PAUSED": self.settings.cooling_paused_min_fan_pct,
        }
        helper_ents = {
            "ONE_BOARD": ENT_COOLING_ONE_MAX,
            "TWO_BOARD": ENT_COOLING_TWO_MAX,
            "THREE_BOARD": ENT_COOLING_THREE_MAX,
            "PAUSED": ENT_COOLING_PAUSED_MAX,
        }
        max_n = self._read_pct_entity(helper_ents[key], max_defaults[key])
        min_n = clamp_fan_max_pct(min_defaults[key])
        envelope = self.read_fan_max_pct()
        max_n = min(max_n, envelope)
        if min_n <= 0 or min_n > max_n:
            min_n_opt = None
        else:
            min_n_opt = min_n
        fans = MIN_REQUIRED_FANS if max_n >= FAN_MAX_DEFAULT else None
        return CoolingProfile(key, max_n, min_n_opt, fans)

    def _live_cooling_profile(self) -> CoolingProfile | None:
        if self._native_temperature_policy():
            cfg = self._configured_cooling or {}
            if cfg.get("mode") == "auto" and cfg.get("target_temperature_c") is not None:
                max_n = cfg.get("max_fan_speed")
                min_n = cfg.get("min_fan_speed") or None
                hot = cfg.get("hot_temperature_c")
                danger = cfg.get("dangerous_temperature_c")
                return CoolingProfile(
                    "CONFIGURED",
                    FAN_MAX_DEFAULT if max_n is None else int(max_n),
                    None if not min_n else int(min_n),
                    cfg.get("minimum_required_fans"),
                    target_temperature_c=int(round(float(cfg["target_temperature_c"]))),
                    hot_temperature_c=None if hot is None else int(round(float(hot))),
                    dangerous_temperature_c=None if danger is None else int(round(float(danger))),
                )
            return None
        if self._live_max_fan_speed is None:
            return None
        min_n = self._live_min_fan_speed if self._live_min_fan_speed else None
        return CoolingProfile("LIVE", int(self._live_max_fan_speed), min_n)

    def _cooling_dwell_blocks(self) -> bool:
        dwell = int(self.settings.cooling_dwell_seconds or 0)
        if dwell <= 0 or not self._cooling_last_change_ts:
            return False
        return (self._now() - self._cooling_last_change_ts) < dwell

    def _note_fan_max_helper(self) -> bool:
        """True when the envelope helper changed this tick (not the first seed)."""
        n = self.read_fan_max_pct()
        if self._fan_max_seen is None:
            self._fan_max_seen = n
            return False
        if n != self._fan_max_seen:
            self._fan_max_seen = n
            return True
        return False

    def _cooling_needed(self, profile: CoolingProfile, *, already_paused: bool) -> bool:
        """Idempotent skip when desired==applied. Unknown live + running → do not pause."""
        if profile.matches(self._cooling_applied):
            return False
        if self._cooling_applied is not None:
            return True
        live = self._live_cooling_profile()
        if live is not None:
            if profile.matches(live):
                self._cooling_applied = profile
                return False
            return True
        return bool(already_paused)

    def _cooling_should_transition(
        self,
        profile: CoolingProfile,
        *,
        abort: bool = False,
        helper_changed: bool = False,
        policy_changed: bool = False,
    ) -> bool:
        """Cooling-only transition while the operating mode is already confirmed.

        Native policy schedules a write only for an operator setpoint/envelope
        change (or thermal abort). Board-count and lard_fan_max_pct changes
        do not. Legacy policy keeps the fan-ceiling compare, still gated later
        by auto_fan_ceiling_enabled. Neither policy schedules anything while
        cooling control is disabled.
        """
        if not self._cooling_control_enabled():
            return False
        if self._native_temperature_policy() and not abort:
            if not self._temperature_policy_valid:
                return False
            if profile.matches(self._cooling_applied):
                return False
            if self._cooling_dwell_blocks():
                return False
            if not policy_changed:
                return False
            return True
        if profile.matches(self._cooling_applied):
            return False
        if abort:
            return True
        if self._cooling_dwell_blocks():
            return False
        if self._cooling_applied is not None:
            return True
        live = self._live_cooling_profile()
        if live is not None:
            if profile.matches(live):
                self._cooling_applied = profile
                return False
            return True
        return bool(helper_changed)

    def _cooling_idle_power(self, obs: MinerObservation) -> bool:
        if obs.power_w is None:
            return False
        return float(obs.power_w) <= COOLING_IDLE_POWER_W

    def _cooling_paused_idle(self, obs: MinerObservation) -> bool:
        """user_pause + ~0 W. Intentional pause during a cooling transition is not ERROR."""
        if not obs.ok:
            return False
        if not (obs.user_paused or self._paused_confirmed(obs)):
            return False
        if obs.running:
            return False
        return self._cooling_idle_power(obs)

    def _set_applying_cooling(self) -> None:
        self.actual_mode = "APPLYING"
        self._cooling_transition_active = True
        self.last_error = ""

    def _restore_known_good_cooling(self, previous: CoolingProfile | None) -> None:
        target = previous or self._unconstrained_cooling("PAUSED")
        try:
            extra = target.extra_auto()
            result = self._call_device(
                "failsafe_restore",
                lambda: self.b.set_cooling_auto(target.max_fan_speed, extra or None),
            )
            if result is None:
                self.log("cooling restore denied")
                return
            code, body = result
            self.log(
                f"cooling restore known-good max={target.max_fan_speed} "
                f"http={code} body={_summarize_http_body(body)}"
            )
            if code == 200:
                self._cooling_applied = target
        except Exception as e:
            self.log(f"cooling restore failed: {e}")

    def _pause_safely(self) -> None:
        try:
            result = self._call_device("failsafe_pause", lambda: self.b.pause())
            if result is None:
                self.log("cooling fail-safe pause denied")
                return
            code, _ = result
            self.log(f"cooling fail-safe pause http={code}")
        except Exception as e:
            self.log(f"cooling fail-safe pause: {e}")

    def _cooling_fail(self, err: str, previous: CoolingProfile | None) -> None:
        """Pause first, restore known-good if possible, stay paused, ERROR.

        Never hand off to legacy writers. Restore is still a cooling PUT, so
        it must not run live while hashing — pause/idle before rewrite.
        """
        self._log_txn(f"cooling fail err={err} — pause + restore + ERROR (no legacy handoff)")
        self._pause_safely()
        try:
            self._wait_until(self._cooling_paused_idle, "cooling_fail_idle_wait", timeout_s=30)
        except Exception:
            pass
        self._restore_known_good_cooling(previous)
        self._pause_safely()
        self.last_error = err
        self.actual_mode = "ERROR"
        self._health_class = "ERROR"
        self._cooling_phase = "ERROR"
        self._cooling_transition_active = False
        self._cooling_txn_active = False
        self._last_cooling_result = err
        self._close_cooling_terminal("error")
        self._note_pending_held("ERROR")
        self._emit("lard_cooling_error", why=err)

    def _confirm_cooling(self, profile: CoolingProfile) -> bool:
        try:
            code, state = self.b.get_cooling_state()
        except Exception as e:
            self.last_error = f"cooling_confirm_exc:{e}"
            return False
        if code != 200:
            self.last_error = f"cooling_confirm_http_{code}"
            return False
        tel = parse_cooling_telemetry(state if isinstance(state, dict) else {})
        self._chip_temp_f = tel.get("chip_temp_f")
        self._fan_rpm = tel.get("fan_rpm")
        self._fan_pct = tel.get("fan_pct")
        self._live_max_fan_speed = tel.get("max_fan_speed")
        self._live_min_fan_speed = tel.get("min_fan_speed")
        live_max = tel.get("max_fan_speed")
        if live_max is not None and clamp_fan_max_pct(live_max) != profile.max_fan_speed:
            self.last_error = (
                f"cooling_confirm_mismatch requested={profile.max_fan_speed} live={live_max}"
            )
            return False
        live_min = tel.get("min_fan_speed")
        if profile.min_fan_speed and live_min is not None:
            if clamp_fan_max_pct(live_min) != int(profile.min_fan_speed):
                self.last_error = (
                    f"cooling_confirm_min_mismatch requested={profile.min_fan_speed} live={live_min}"
                )
                return False
        if live_max is None:
            self.log(
                "cooling confirm: state has no max_fan_speed field; PUT 200 + GET 200 accepted"
            )
        if not self._confirm_temperature_fields(profile, state if isinstance(state, dict) else {}):
            return False
        return True

    def _confirm_temperature_fields(self, profile: CoolingProfile, state: dict) -> bool:
        """Confirm target temps when a readback actually carries them.

        GET /cooling/state has no setpoint. A missing field is not a mismatch.
        GET /configuration/miner is the setpoint readback when the client has it.
        """
        if profile.target_temperature_c is None:
            return True
        self._refresh_configured_cooling()
        sources: list[dict] = []
        auto = state.get("auto") if isinstance(state.get("auto"), dict) else None
        if auto:
            sources.append(auto)
        cfg = self._configured_cooling
        if cfg.get("mode"):
            sources.append(
                {
                    "target_temperature": {"degree_c": cfg.get("target_temperature_c")},
                    "hot_temperature": {"degree_c": cfg.get("hot_temperature_c")},
                    "dangerous_temperature": {"degree_c": cfg.get("dangerous_temperature_c")},
                }
            )
        if not sources:
            self.log(
                "cooling confirm: no temperature readback "
                "(cooling/state has no setpoint; configuration/miner unread); "
                "PUT 200 + GET 200 accepted"
            )
            return True
        expected = {
            "target_temperature": profile.target_temperature_c,
            "hot_temperature": profile.hot_temperature_c,
            "dangerous_temperature": profile.dangerous_temperature_c,
        }
        for src in sources:
            for key, want in expected.items():
                if want is None:
                    continue
                got = _degree_c(src.get(key))
                if got is None:
                    continue
                if int(round(got)) != int(want):
                    self.last_error = (
                        f"cooling_confirm_temp_mismatch field={key} requested={want} live={got}"
                    )
                    return False
        return True

    def _log_txn(self, msg: str) -> None:
        op = self._cooling_txn_id or "-"
        self.log(f"op={op} phase={self._cooling_phase} health={self._health_class} {msg}")

    def _assign_txn_id(self) -> str:
        self._txn_seq += 1
        self._cooling_txn_id = f"cool-{int(self._now())}-{self._txn_seq}"
        self._resume_retries_used = 0
        self._resume_retry_used = False
        self._recovery_elapsed_s = 0.0
        self._recovery_limit_s = 0.0
        self._primary_recovery_started_ts = 0.0
        self._primary_recovery_deadline_ts = 0.0
        self._recovery_interim_logged = False
        self._settle_complete = False
        self._resume_gate = ""
        self._owner_txn_id = self._cooling_txn_id
        self._write_inflight_marker()
        return self._cooling_txn_id

    def _emit(self, event_type: str, **extra) -> None:
        desired = None if self._cooling_desired is None else self._cooling_desired.max_fan_speed
        effective = self._live_max_fan_speed
        if effective is None and self._cooling_applied is not None:
            effective = self._cooling_applied.max_fan_speed
        pending = None if self._pending_profile is None else self._pending_profile.max_fan_speed
        payload = {
            "op": self._cooling_txn_id or "",
            "phase": self._cooling_phase,
            "health": self._health_class,
            "desired_ceiling": desired,
            "effective_ceiling": effective,
            "pending_ceiling": pending,
            "resume_retry": bool(self._resume_retry_used),
            "lifecycle_reason": self._lifecycle_reason,
            "telemetry_freshness": self._telemetry_freshness,
        }
        payload.update(extra)
        self._log_txn(
            "event="
            + event_type
            + " "
            + " ".join(f"{k}={v}" for k, v in payload.items())
        )
        fn = getattr(self.ha, "fire_event", None)
        if not fn:
            return
        try:
            fn(event_type, payload)
        except Exception as e:
            self._log_txn(f"event_skip {event_type}: {e}")

    def _set_cooling_phase(self, phase: str, *, health: str | None = None) -> None:
        if self._health_class in TERMINAL_HEALTH and phase not in {
            "DEGRADED_NEEDS_ATTENTION",
            "ERROR",
            "INTERRUPTED",
        }:
            return
        prev = self._cooling_phase
        self._cooling_phase = phase
        if phase in {"PAUSE_REQUESTED", "COOLING_APPLYING", "COOLING_SETTLING", "RESUME_REQUESTED"}:
            self.actual_mode = "APPLYING"
            self._health_class = health or "APPLYING"
            self.last_error = ""
        elif phase == "PAUSED_CONFIRMED":
            self.actual_mode = "APPLYING"
            self._health_class = health or "PAUSED"
            self.last_error = ""
        elif phase in {"RECOVERING", "RETRY_RESUME_ONCE"}:
            self.actual_mode = "APPLYING"
            self._health_class = "RECOVERING"
        elif phase == "HASHING":
            self._health_class = "HASHING"
        elif phase == "DEGRADED_NEEDS_ATTENTION":
            self._health_class = "DEGRADED_NEEDS_ATTENTION"
        elif phase == "ERROR":
            self._health_class = "ERROR"
            self.actual_mode = "ERROR"
        events = {
            "PAUSE_REQUESTED": "lard_cooling_pause_requested",
            "PAUSED_CONFIRMED": "lard_cooling_paused_confirmed",
            "COOLING_APPLYING": "lard_cooling_applying",
            "COOLING_SETTLING": "lard_cooling_settling",
            "RESUME_REQUESTED": "lard_cooling_resume_requested",
            "RECOVERING": "lard_cooling_recovering",
            "RETRY_RESUME_ONCE": "lard_cooling_retry_resume",
            "HASHING": "lard_cooling_hashing",
            "DEGRADED_NEEDS_ATTENTION": "lard_cooling_degraded",
            "ERROR": "lard_cooling_error",
        }
        if prev != phase and phase in events:
            self._emit(events[phase])

    def _defer_profile(
        self,
        profile: CoolingProfile,
        reason: str,
        *,
        queue: bool,
        explicit: bool = False,
    ) -> None:
        if queue and self.settings.coalesce_pending_cooling_requests:
            self._pending_profile = profile
            if explicit:
                self._pending_explicit = True
            self._log_txn(
                f"coalesce_pending max={profile.max_fan_speed} reason={reason} explicit={explicit}"
            )
            self._emit("lard_cooling_coalesced", reason=reason, pending=profile.max_fan_speed)
            return
        self._log_txn(f"defer_reject max={profile.max_fan_speed} reason={reason}")
        self._emit("lard_cooling_deferred", reason=reason, pending=profile.max_fan_speed)

    def _lifecycle_text(self, obs: MinerObservation) -> str:
        parts = []
        for val in (obs.phase, obs.pause_reason, obs.status_raw):
            if val in (None, ""):
                continue
            parts.append(str(val).strip().lower().replace("-", " "))
        return " ".join(parts)

    def _hard_fault(self, obs: MinerObservation) -> bool:
        """Single hard-fault predicate. Stale or missing telemetry is not a fault."""
        if not obs.ok:
            return False
        text = self._lifecycle_text(obs)
        if getattr(obs, "safety_fault", ""):
            text = f"{text} {obs.safety_fault}".strip()
        blob = text.replace(" ", "_")
        spaced = text
        return any(tok in blob or tok in spaced for tok in HARD_FAULT_TOKENS)

    def _lifecycle_exact_tokens(self, obs: MinerObservation) -> set[str]:
        """Whole words plus adjacent pairs. Substrings do not count."""
        blob = self._lifecycle_text(obs)
        words: list[str] = []
        cur: list[str] = []
        for ch in blob:
            if ch.isalnum():
                cur.append(ch)
            elif cur:
                words.append("".join(cur))
                cur = []
        if cur:
            words.append("".join(cur))
        tokens = set(words)
        for left, right in zip(words, words[1:]):
            tokens.add(f"{left}_{right}")
        return tokens

    def _positive_lifecycle(self, obs: MinerObservation) -> bool:
        """Independent miner-side transitional evidence only.

        Allowed, and only from the current ``MinerObservation`` filled by
        ``parse_mining_state`` on ``GET /api/v1/miner/details``:

        - parser flags ``starting``, ``preheating``, ``ramping`` (equality on
          the miner ``detailed_status`` phase, not on a LARD label)
        - exact tokens in ``LEGITIMATE_LIFECYCLE_TOKENS`` taken from miner
          ``phase``, ``pause_reason``, or ``status`` (cooldown, cooling_down,
          preheat/preheating, startup/starting, init/initializing, autotune/
          tuning/tuner, ramping/ramp/quick_ramping, warming/warmup, booting)

        Not positive: the word ``applying`` (controller request / cooling
        interim), LARD ``desired_mode`` / ``actual_mode`` / ``controller_state``,
        blanket ``running``, "not paused", unqualified watts or hashrate,
        unknown strings, ``miner_ready is False``, a failed or stale read, or
        ``init`` inside ``reinitializing``. Hard fault wins over any token.
        """
        if not obs.ok or self._hard_fault(obs):
            return False
        if obs.starting or obs.preheating or obs.ramping:
            return True
        tokens = set(self._lifecycle_exact_tokens(obs))
        tokens.discard("applying")
        return bool(tokens & LEGITIMATE_LIFECYCLE_TOKENS)

    def _controller_label_only(self, obs: MinerObservation) -> bool:
        """True when the only lifecycle word is the controller label ``applying``.

        A live ``running`` or paused miner is not this case, even at 0 W.
        """
        if not obs.ok or obs.running or obs.paused or obs.user_paused:
            return False
        if obs.starting or obs.preheating or obs.ramping:
            return False
        if self._paused_confirmed(obs):
            return False
        tokens = set(self._lifecycle_exact_tokens(obs))
        tokens.discard("applying")
        if tokens & LEGITIMATE_LIFECYCLE_TOKENS:
            return False
        blob = self._lifecycle_text(obs)
        return "applying" in blob.split()

    def _expected_board_ids(self, mode: str) -> list[str]:
        """Configured topology. Never the length of a partial hashboards response."""
        if mode in BOARD_MAP and mode != "PAUSED":
            return list(BOARD_MAP[mode])
        return []

    def _expected_boards_proven(self, obs: MinerObservation, mode: str) -> bool:
        """Each configured board must be proven on this read.

        Expected ids come from ``BOARD_MAP`` (the authoritative topology for
        the mode), not from ``len(board_reports)``. A 2-board payload does
        not become "expected == 2". A shorter or partial response cannot
        shrink the configured set.
        """
        expect = self._normalize_board_ids(self._expected_board_ids(mode))
        if not expect:
            return False
        reports = {}
        for rec in obs.board_reports or []:
            if not isinstance(rec, dict):
                return False
            bid = norm_board_id(rec.get("id"))
            if not bid:
                return False
            reports[bid] = rec
        if len(reports) < len(expect):
            return False
        for bid in expect:
            rec = reports.get(bid)
            if not rec or not rec.get("proven_healthy"):
                return False
        for rec in reports.values():
            if rec.get("explicit_unhealthy") or rec.get("fault") or rec.get("stale"):
                return False
            if rec.get("enabled") and not rec.get("proven_healthy"):
                return False
        return True

    def _hashing_sample_ok(self, obs: MinerObservation, mode: str) -> bool:
        """One stable-hash sample. Full HASHING needs several of these in a row.

        Board health must be proven by the current read. Cached health, a
        missing hook, a partial board list, or watts/TH alone are not enough.
        """
        if not obs.ok or self._hard_fault(obs):
            return False
        if obs.user_paused or self._is_paused(obs) or not obs.running:
            return False
        if obs.power_w is None or float(obs.power_w) <= COOLING_IDLE_POWER_W:
            return False
        if obs.hashrate is None or float(obs.hashrate) < SANITY_HASHRATE:
            return False
        if obs.safety_fault:
            return False
        if not obs.board_health_verified or obs.board_stale or not obs.boards_healthy:
            return False
        if not self._expected_boards_proven(obs, mode):
            return False
        if mode in BOARD_MAP and mode != "PAUSED":
            if not self._boards_match(obs.enabled_ids, BOARD_MAP[mode]):
                return False
        elif not obs.enabled_ids:
            return False
        return True

    def _note_endpoint(
        self,
        name: str,
        *,
        ok: bool,
        failure_class: str = "",
        summary: str = "",
    ) -> None:
        """Record one existing read. Does not start another poll."""
        slot = self._endpoints.setdefault(
            name,
            {
                "last_success_mono": 0.0,
                "last_failure_mono": 0.0,
                "last_failure_class": "",
                "last_failure_summary": "",
                "fresh": False,
            },
        )
        now = self._now()
        if ok:
            slot["last_success_mono"] = now
            slot["fresh"] = True
            slot["last_failure_class"] = ""
            slot["last_failure_summary"] = ""
            return
        slot["fresh"] = False
        slot["last_failure_mono"] = now
        slot["last_failure_class"] = failure_class or "UNAVAILABLE"
        slot["last_failure_summary"] = _redact_summary(summary)

    def _endpoint_fresh(self, name: str) -> bool:
        return bool((self._endpoints.get(name) or {}).get("fresh"))

    def _read_failure_class(self, code, body) -> str:
        text = _summarize_http_body(body)
        return classify_miner_telemetry(
            ok=False,
            http_code=int(code) if code is not None else None,
            error_text=text,
            malformed="malformed" in (text or "").lower(),
        )

    def _drop_stale_write_queue(self, reason: str) -> None:
        """Forget a coalesced cooling profile so a later arm cannot replay it."""
        if self._pending_profile is None and not self._pending_explicit:
            return
        self._pending_profile = None
        self._pending_explicit = False
        self._pending_hold_logged = None
        self._log_txn(f"pending_dropped reason={reason} — no replay")

    def _power_fresh(self) -> bool:
        return bool(self._required_telemetry_fresh and self.power_w is not None)

    def _boards_fresh(self) -> bool:
        return bool(
            self._required_telemetry_fresh
            and self._endpoint_fresh("boards")
            and self._endpoint_fresh("details")
        )

    def _observability_fields(self) -> dict[str, Any]:
        """Read-only controller plane. Does not poll the miner."""
        endpoints = {
            name: {
                "fresh": bool(slot.get("fresh")),
                "last_success_mono": slot.get("last_success_mono") or 0.0,
                "last_failure_mono": slot.get("last_failure_mono") or 0.0,
                "last_failure_class": slot.get("last_failure_class") or "",
                "last_failure_summary": slot.get("last_failure_summary") or "",
            }
            for name, slot in self._endpoints.items()
        }
        return {
            "api_reachable": bool(self._api_reachable),
            "bosminer_available": bool(self._bosminer_available),
            "required_telemetry_fresh": bool(self._required_telemetry_fresh),
            "health_classification": self.telemetry_class,
            "endpoints": endpoints,
            "fault_reason": self.last_error or "",
            "consecutive_good_polls": int(self._valid_poll_streak),
            "last_txn_result": self._last_cooling_result or "",
            "last_verified_boards": self._last_good_boards or "",
            "write_gate": self.write_gate,
            "power_fresh": self._power_fresh(),
            "boards_fresh": self._boards_fresh(),
            "cooling_fresh": self._endpoint_fresh("cooling"),
        }

    def _clear_transient_read_error(self) -> None:
        """Drop a bare read_* miss. Keep a compound verification reason."""
        err = self.last_error or ""
        if err.startswith("read_") and "|" not in err:
            self.last_error = ""

    def _telemetry_failure_holds_phase(self) -> bool:
        """A missed read must not cancel a cooling txn or a verification latch.

        WAITING_FOR_BRAIINS still expires on its own monotonic deadline.
        Sustained misses do not rewrite that state into ERROR.
        """
        if self._cooling_txn_active or self._fault_latched:
            return True
        if self.actual_mode in {"FAULT_LATCHED", "WAITING_FOR_BRAIINS"}:
            return True
        if self.controller_state in {"FAULT_LATCHED", "WAITING_FOR_BRAIINS"}:
            return True
        return False

    def _note_telemetry_success(self) -> None:
        self._telemetry_fail_streak = 0
        self._telemetry_fault = False
        self._telemetry_freshness = "FRESH"
        self._telemetry_last_success_ts = self._wall()
        self._telemetry_last_success_mono = self._now()
        if self.power_w is not None:
            self._last_good_power_w = self.power_w
        if self.boards_str:
            self._last_good_boards = self.boards_str
        self._clear_transient_read_error()
        # One coherent sample may count toward clearing the sustained-read
        # latch. The error stays until SUSTAINED_TELEMETRY_RECOVERY_POLLS.
        self._account_sustained_telemetry_recovery()

    def _remember_sustained_telemetry_fault(self) -> None:
        """Record the outage once per episode. Recovery does not erase it."""
        if (
            self._sustained_fault_open
            and self._last_fault_reason == SUSTAINED_TELEMETRY_ERROR
        ):
            return
        self._last_fault_count += 1
        self._last_fault_reason = SUSTAINED_TELEMETRY_ERROR
        self._last_fault_class = "ERROR"
        self._last_fault_timestamp = float(self._wall())
        self._sustained_fault_open = True

    def _coherent_board_state(self, obs: MinerObservation) -> bool:
        """Required boards are present, verified, and a known 1/2/3 topology.

        Empty, partial, stale, unverified, or non-contiguous sets are not
        coherent. Watts are not a substitute.
        """
        if not obs.board_health_verified or obs.board_stale or not obs.boards_healthy:
            return False
        if obs.safety_fault:
            return False
        ids = frozenset(norm_board_id(i) for i in obs.enabled_ids if norm_board_id(i))
        if not ids:
            return False
        mode = ""
        for name in ("ONE_BOARD", "TWO_BOARD", "THREE_BOARD"):
            if ids == frozenset(BOARD_MAP[name]):
                mode = name
                break
        if not mode:
            return False
        return self._expected_boards_proven(obs, mode)

    def _sustained_telemetry_latch_active(self) -> bool:
        """Active error is this outage. Other ERROR strings are not."""
        if (self.last_error or "") != SUSTAINED_TELEMETRY_ERROR:
            return False
        if self._health_class != "ERROR":
            return False
        if self._fault_latched or self.actual_mode == "FAULT_LATCHED":
            return False
        return True

    def _sustained_telemetry_structurally_blocked(self) -> bool:
        """Cooling/config ownership. Do not count or clear across these."""
        if self._cooling_txn_active or self._cooling_transition_active or self._apply_depth:
            return True
        if self._reload_hold:
            return True
        if self._cooling_terminal_kind in COOLING_FAULT_TERMINALS:
            return True
        return False

    def _sustained_telemetry_sample_coherent(self) -> bool:
        """This poll is a verified recovery sample. Not a structural blocker check."""
        obs = getattr(self, "_last_obs", None)
        if (
            self._telemetry_freshness != "FRESH"
            or self._telemetry_fail_streak != 0
            or self._telemetry_fault
        ):
            return False
        if not self._auth_ok or not self._bosminer_available or not self._api_reachable:
            return False
        if not self._required_telemetry_fresh:
            return False
        if not self._endpoint_fresh("boards") or not self._endpoint_fresh("details"):
            return False
        if obs is None or not obs.ok or not obs.boards_ok or not obs.details_ok:
            return False
        if self._hard_fault(obs) or self._critical_fault:
            return False
        if self.telemetry_class not in RECOVERY_TELEMETRY_CLASSES:
            return False
        if not self._coherent_board_state(obs):
            return False
        if self.telemetry_class == "VALID_PAUSED" and not self._paused_confirmed(obs):
            return False
        if self.telemetry_class == "VALID_TRANSITION" and not self._positive_lifecycle(obs):
            return False
        if self.telemetry_class == "RUNNING_HEALTHY" and not obs.running:
            return False
        return True

    def _sustained_telemetry_recovery_allowed(self) -> bool:
        """True only on the poll that may clear the sustained-read ERROR.

        The sticky cause must be exactly ``telemetry_sustained_unavailable``.
        Required boards and details must be fresh and coherent. The live class
        must be RUNNING_HEALTHY, VALID_PAUSED, or evidence-backed
        VALID_TRANSITION. Hard faults, FAULT_LATCHED, auth/API/bosminer loss,
        write or cooling terminals, and an active cooling or config transaction
        block recovery. The coherent-poll count must already be
        ``SUSTAINED_TELEMETRY_RECOVERY_POLLS``. This does not arm writes.
        """
        return (
            self._sustained_telemetry_latch_active()
            and not self._sustained_telemetry_structurally_blocked()
            and self._sustained_telemetry_sample_coherent()
            and self._sustained_telemetry_recovery_polls >= SUSTAINED_TELEMETRY_RECOVERY_POLLS
        )

    def _sustained_telemetry_poll_interval(self) -> float:
        """Ordinary loop spacing. One recovery count per this interval."""
        return max(1.0, float(self.settings.poll_seconds or 1))

    def _sustained_telemetry_spacing_elapsed(self) -> bool:
        last = float(self._sustained_telemetry_recovery_mono or 0.0)
        if last <= 0.0:
            return True
        return (float(self._now()) - last) >= self._sustained_telemetry_poll_interval()

    def _reset_sustained_telemetry_recovery_polls(self) -> None:
        """A bad required read starts the five-poll streak over."""
        self._sustained_telemetry_recovery_polls = 0
        self._sustained_telemetry_recovery_mono = float(self._now())
        self._sustained_telemetry_recovery_pending = False

    def _restore_open_sustained_telemetry_error(self) -> None:
        """Keep the active sustained-read error until the five-poll clear.

        A miss overwrites ``last_error`` with ``read_*`` and the transient
        clearer drops that. While this episode is still open, put the
        sustained-unavailable string back. A different fault string, a
        cooling terminal, or a fault latch is left alone.
        """
        if not self._sustained_fault_open or self._health_class != "ERROR":
            return
        if self._fault_latched or self.actual_mode == "FAULT_LATCHED":
            return
        if self._sustained_telemetry_structurally_blocked():
            return
        err = self.last_error or ""
        if err == SUSTAINED_TELEMETRY_ERROR:
            return
        if err and not (err.startswith("read_") and "|" not in err):
            return
        self.last_error = SUSTAINED_TELEMETRY_ERROR

    def _account_sustained_telemetry_recovery(self) -> None:
        """Count at most one coherent poll per ``poll_seconds``.

        Missing, malformed, stale, or contradictory samples reset the count.
        Structural blockers do not increment and do not clear. The active
        error stays until the count reaches ``SUSTAINED_TELEMETRY_RECOVERY_POLLS``.
        """
        self._sustained_telemetry_recovery_pending = False
        if self._fault_latched or self.actual_mode == "FAULT_LATCHED":
            self._reset_sustained_telemetry_recovery_polls()
            return
        if not self._sustained_telemetry_latch_active():
            return
        if self._sustained_telemetry_structurally_blocked():
            return
        if not self._sustained_telemetry_sample_coherent():
            self._reset_sustained_telemetry_recovery_polls()
            return
        if not self._sustained_telemetry_spacing_elapsed():
            return
        self._sustained_telemetry_recovery_polls += 1
        self._sustained_telemetry_recovery_mono = float(self._now())
        if self._sustained_telemetry_recovery_polls >= SUSTAINED_TELEMETRY_RECOVERY_POLLS:
            self._sustained_telemetry_recovery_pending = True

    def _mode_for_enabled_boards(self, obs: MinerObservation) -> str:
        ids = frozenset(norm_board_id(i) for i in obs.enabled_ids if norm_board_id(i))
        for name in ("ONE_BOARD", "TWO_BOARD", "THREE_BOARD"):
            if ids == frozenset(BOARD_MAP[name]):
                return name
        if self.actual_mode in BOARD_MAP and self.actual_mode != "PAUSED":
            return self.actual_mode
        return "ONE_BOARD"

    def _recovered_idle_health(self, obs: MinerObservation) -> tuple[str, str] | None:
        """Health and phase after a sustained-read latch, from this observation.

        VALID_TRANSITION is miner lifecycle evidence only. The controller word
        ``applying`` is not that evidence and does not produce RECOVERING.
        """
        if self.telemetry_class == "VALID_TRANSITION" and self._positive_lifecycle(obs):
            return ("RECOVERING", "IDLE")
        if self.telemetry_class == "VALID_PAUSED" and self._paused_confirmed(obs):
            return ("PAUSED", "IDLE")
        if self.telemetry_class == "RUNNING_HEALTHY" and obs.running:
            sample = self._mode_for_enabled_boards(obs)
            if self._hashing_sample_ok(obs, sample):
                return ("HASHING", "IDLE")
            return ("RECOVERING", "IDLE")
        return None

    def _finish_sustained_telemetry_recovery(self, obs: MinerObservation | None) -> bool:
        """Clear the active sustained-read error and publish idle health.

        Returns False without changing last_error, health, or phase when the
        observation is not a verified recovery. Does not arm writes or send
        a miner command.
        """
        if obs is None or not self._sustained_telemetry_recovery_allowed():
            return False
        recovered = self._recovered_idle_health(obs)
        if recovered is None:
            return False
        health, idle_phase = recovered
        self.last_error = ""
        self._sustained_fault_open = False
        if self.actual_mode == "ERROR":
            inferred = self.infer_actual_mode(obs)
            if inferred and inferred != "ERROR":
                self.actual_mode = inferred
                if inferred in RANK:
                    self.confirmed_operational = inferred
        self._health_class = health
        if (
            idle_phase
            and self._cooling_phase == "ERROR"
            and not self._cooling_txn_active
            and not self._cooling_transition_active
        ):
            self._cooling_phase = idle_phase
        self._sync_mode_contract(obs)
        return True

    def _note_telemetry_failure(self, where: str) -> None:
        """First misses are UNKNOWN/STALE. They never cancel an in-flight cooling txn."""
        self._reset_sustained_telemetry_recovery_polls()
        self._telemetry_fail_streak += 1
        if self._telemetry_last_success_ts:
            self._telemetry_freshness = "STALE"
        else:
            self._telemetry_freshness = "UNKNOWN"
        self._log_txn(
            f"telemetry_timeout where={where} attempt={self._telemetry_fail_streak} "
            f"freshness={self._telemetry_freshness} last_known_health={self._health_class} "
            f"last_phase={self._cooling_phase} last_success_ts={self._telemetry_last_success_ts} "
            f"last_power_w={self._last_good_power_w} last_boards={self._last_good_boards}"
        )
        threshold = max(1, int(self.settings.telemetry_failures_before_error or 3))
        if self._telemetry_fail_streak >= threshold:
            self._telemetry_fault = True
            self._emit(
                "lard_telemetry_error",
                where=where,
                attempt=self._telemetry_fail_streak,
            )
            if self._telemetry_failure_holds_phase():
                self._log_txn(
                    "telemetry sustained during txn — do not cancel or overwrite txn phase"
                )
            else:
                self._health_class = "ERROR"
                self.actual_mode = "ERROR"
                self._cooling_phase = "ERROR"
                self.last_error = SUSTAINED_TELEMETRY_ERROR
                self._remember_sustained_telemetry_fault()
            self._restore_open_sustained_telemetry_error()
            return
        self._emit(
            "lard_telemetry_unknown",
            where=where,
            freshness=self._telemetry_freshness,
            attempt=self._telemetry_fail_streak,
        )
        self._clear_transient_read_error()
        self._restore_open_sustained_telemetry_error()
        if self._telemetry_failure_holds_phase():
            return
        if self._health_class not in {"DEGRADED_NEEDS_ATTENTION", "ERROR", "FAULT_LATCHED"}:
            self._health_class = "UNKNOWN"

    def _note_tick_observation(self, obs: MinerObservation | None, where: str) -> None:
        """Freshness note for one tick read. Armed and observe-only share this.

        Success is ``obs.ok``: required boards and details came back usable.
        Watts, hashrate, or an unverified board-health payload are not that
        read. A miss notes failure and does not mark freshness FRESH.
        ``publish`` advances idle health only after a FRESH note.
        """
        if obs is not None and obs.ok:
            self._note_telemetry_success()
            return
        self._note_telemetry_failure(where)

    def _poll_interval(self) -> float:
        return max(1.0, float(self.settings.transition_poll_interval_seconds or TRANSITION_POLL_S))

    def _settle_seconds(self) -> float:
        """0.1.7 settle. A zero cooling_settle_seconds keeps the 0.1.6 knob working."""
        primary = float(self.settings.cooling_settle_seconds or 0)
        if primary > 0:
            return primary
        return float(self.settings.cooling_resume_settle_seconds or 0)

    def _cooling_already_effective(self, profile: CoolingProfile) -> bool:
        if profile.matches(self._cooling_applied):
            return True
        live = self._live_cooling_profile()
        if profile.matches(live):
            self._cooling_applied = profile
            return True
        return False

    def _cooling_refuse_reason(self, obs: MinerObservation) -> str | None:
        if not obs.ok:
            return "unavailable"
        if self._hard_fault(obs):
            return "hard_fault"
        blob = self._lifecycle_text(obs)
        for key in (
            "cooling_down",
            "preheating",
            "preheat",
            "initializing",
            "rebooting",
            "reboot",
        ):
            if key in blob:
                return key.replace(" ", "_")
        if "applying" in blob and not self._cooling_txn_active:
            return "applying"
        return None

    def _close_cooling_terminal(self, kind: str) -> None:
        """Record the terminal that just closed this transaction.

        A later coalesced apply must see this generation. DEGRADED and ERROR
        block automatic pending apply until a newer success terminal replaces
        them. Automatic apply requires a fresh transaction that reaches HASHING.
        """
        self._cooling_terminal_kind = kind
        self._cooling_terminal_gen += 1

    def _note_pending_held(self, reason: str) -> None:
        """Keep a coalesced ceiling visible. Do not issue device commands."""
        pending = self._pending_profile
        if pending is None:
            return
        key = (str(reason), int(pending.max_fan_speed))
        if self._pending_hold_logged == key:
            return
        self._pending_hold_logged = key
        self._log_txn(
            f"pending_held max={pending.max_fan_speed} reason={reason} "
            f"health={self._health_class} terminal={self._cooling_terminal_kind} "
            f"— no auto apply; explicit operator action required"
        )
        self._emit(
            "lard_cooling_pending_held",
            reason=reason,
            pending=pending.max_fan_speed,
        )

    def _auto_apply_pending_allowed(self) -> bool:
        """Coalesced pending may start another txn only after successful HASHING.

        Confirmed PAUSED, DEGRADED, and ERROR are not automatic success.
        An in-flight transaction cannot apply its own pending.
        """
        if self._cooling_txn_active or self._reload_hold or self._cancel_requested:
            return False
        if self._health_class in TERMINAL_HEALTH:
            return False
        if self._cooling_terminal_kind in {"degraded", "error", "interrupted"}:
            return False
        return self._health_class == "HASHING"

    def _finish_hashing(self, mode: str) -> CoolingResult:
        self._last_cooling_result = self._last_cooling_result or "verified"
        self._set_cooling_phase("HASHING", health="HASHING")
        self._log_txn(f"recovery_hashing mode={mode}")
        self.last_error = ""
        self._cooling_transition_active = False
        if mode in RANK:
            self._mark_confirmed(mode)
        else:
            self.actual_mode = mode
        self._health_class = "HASHING"
        self._close_cooling_terminal("hashing")
        return _txn_result("hashing")

    def _stage_c_ready(self, obs: MinerObservation, mode: str, hist: dict[str, list]) -> bool:
        """Ramp is healthy: transitional phase and watts or hashrate have started rising."""
        if not obs.ok or self._is_paused(obs) or obs.user_paused:
            return False
        if mode in BOARD_MAP and mode != "PAUSED":
            if not self._boards_match(obs.enabled_ids, BOARD_MAP[mode]):
                return False
        if not self._transitional_operational(obs):
            return False
        self._record_trend(obs, hist)
        return self._trend_rising(hist)

    def _note_recovery_interim(self, mode: str, kind: str) -> CoolingResult:
        """operational/applying stay inside the open recovery transaction.

        Updates visible RECOVERING/APPLYING diagnostics only. Does not clear
        ``_cooling_txn_active``, release ownership, move the original maximum
        deadline, or count as success.
        """
        if self._health_class in TERMINAL_HEALTH or self._cancel_requested:
            return CoolingResult("interim")
        self._health_class = "RECOVERING"
        if self._cooling_phase not in {"RECOVERING", "RETRY_RESUME_ONCE"}:
            self._cooling_phase = "RECOVERING"
        if kind == "applying":
            self.actual_mode = "APPLYING"
        if not self._recovery_interim_logged:
            self._recovery_interim_logged = True
            self._log_txn(
                f"recovery_interim kind={kind} mode={mode} "
                f"txn_active={self._cooling_txn_active} "
                f"primary_deadline_ts={self._primary_recovery_deadline_ts} "
                f"(non-terminal; deadlines and retry still apply)"
            )
        return CoolingResult("interim")

    def _finish_operational(self, mode: str) -> CoolingResult:
        """Non-terminal. Running at low watts is not a closed recovery."""
        return self._note_recovery_interim(mode, "operational")

    def _finish_applying(self, mode: str) -> CoolingResult:
        """Non-terminal. A ramp is not a closed recovery."""
        return self._note_recovery_interim(mode, "applying")

    def _finish_degraded(self, mode: str, why: str) -> CoolingResult:
        self._set_cooling_phase("DEGRADED_NEEDS_ATTENTION")
        self.last_error = f"degraded_needs_attention:{why}"
        self.actual_mode = "APPLYING"
        self._log_txn(f"degraded mode={mode} why={why} (not ERROR)")
        self._cooling_transition_active = False
        self._close_cooling_terminal("degraded")
        self._note_pending_held("DEGRADED_NEEDS_ATTENTION")
        return _txn_result("degraded")

    def _fail_hard(self, obs: MinerObservation) -> CoolingResult:
        why = self._lifecycle_text(obs) or "hard_fault"
        if getattr(obs, "safety_fault", ""):
            why = f"{why} {obs.safety_fault}".strip()
        self.last_error = f"hard_fault:{why}"
        self._set_cooling_phase("ERROR", health="ERROR")
        self._log_txn(f"hard_fault {why}")
        self._cooling_transition_active = False
        self._close_cooling_terminal("error")
        self._note_pending_held("ERROR")
        return _txn_result("error")

    def _classify_settle_obs(self, obs: MinerObservation) -> str:
        """``hard_fault``, ``not_clean``, or ``clean``. Missing telemetry is not a fault."""
        if not obs.ok:
            self._note_telemetry_failure("settle")
            return "not_clean"
        self._note_telemetry_success()
        if self._hard_fault(obs):
            return "hard_fault"
        return "clean"

    def _settle_after_cooling(self, profile: CoolingProfile) -> str:
        """Poll through the post-write settle. Do not resume just because PUT returned.

        Every poll uses ``_hard_fault``. A hard fault aborts the settle and
        returns ``hard_fault`` so the caller must not ResumeMining. A stale or
        failed read is ``not_clean``: not a hard fault, and not authorization
        to resume blindly.
        """
        self._settle_complete = False
        self._set_cooling_phase("COOLING_SETTLING", health="APPLYING")
        settle = self._settle_seconds()
        poll = self._poll_interval()
        self._log_txn(f"settle_begin seconds={settle} poll={poll}")
        if settle <= 0:
            obs = self.observe_miner()
            kind = self._classify_settle_obs(obs)
            if kind == "hard_fault":
                self._fail_hard(obs)
                self._log_txn("settle_abort hard_fault seconds=0 — no resume")
                return "hard_fault"
            if kind == "not_clean":
                self._log_txn("settle_poll telemetry_not_clean seconds=0 — not a resume authorization")
            self._confirm_cooling(profile)
            self._settle_complete = True
            self._log_txn("settle_done seconds=0")
            return "settled" if kind == "clean" else "not_clean"
        deadline = self._now() + settle
        saw_not_clean = False
        while self._now() < deadline:
            if self._cancel_requested or self._owner_txn_id != self._cooling_txn_id:
                self._log_txn("settle_cancelled — no resume")
                return "cancelled"
            obs = self.observe_miner()
            self._refresh_cooling_telemetry()
            kind = self._classify_settle_obs(obs)
            self._log_txn(
                f"settle_poll kind={kind} paused={obs.paused} user_paused={obs.user_paused} "
                f"power_w={obs.power_w} phase={obs.phase} reason={obs.pause_reason} "
                f"live_max={self._live_max_fan_speed} chip_temp_f={self._chip_temp_f}"
            )
            if kind == "hard_fault":
                self._fail_hard(obs)
                self._log_txn("settle_abort hard_fault — no resume, no retry")
                return "hard_fault"
            if kind == "not_clean":
                saw_not_clean = True
                self._log_txn(
                    "settle_poll telemetry_not_clean — not a hard fault, not a resume authorization"
                )
            remaining = deadline - self._now()
            if remaining <= 0:
                break
            self._sleep(min(poll, remaining))
        self._confirm_cooling(profile)
        self._settle_complete = True
        self._log_txn("settle_done")
        return "not_clean" if saw_not_clean else "settled"

    def _recovery_window(
        self,
        mode: str,
        *,
        limit_s: float,
        expected_s: float,
        label: str,
    ) -> str:
        """Return hashing, exhausted, or error.

        ``operational`` and ``applying`` are non-terminal. They may update
        visible RECOVERING/APPLYING state but they do not end the window,
        clear the transaction, or move the original maximum deadline.
        """
        self._set_cooling_phase("RECOVERING", health="RECOVERING")
        start = self._now()
        self._recovery_started_ts = start
        self._recovery_limit_s = float(limit_s)
        # The primary 600s deadline is fixed for this transaction. Re-observing
        # APPLYING or running must not start it over. The post-retry window is
        # its own shorter deadline and does not move the primary one.
        if label == "primary":
            self._primary_recovery_started_ts = start
            self._primary_recovery_deadline_ts = start + float(limit_s)
        elif self._primary_recovery_deadline_ts:
            self._log_txn(
                f"primary_deadline_kept label={label} "
                f"primary_deadline_ts={self._primary_recovery_deadline_ts}"
            )
        deadline = start + float(limit_s)
        poll = self._poll_interval()
        stable_need = max(1, int(self.settings.stable_hash_poll_count or STABLE_HASH_POLLS))
        stable = 0
        operational = 0
        hist: dict[str, list] = {"power": [], "hash": []}
        self._log_txn(
            f"recovery_begin label={label} expected_s={expected_s} limit_s={limit_s} "
            f"stable_polls={stable_need} poll_s={poll} "
            f"primary_deadline_ts={self._primary_recovery_deadline_ts}"
        )
        while True:
            elapsed = self._now() - start
            self._recovery_elapsed_s = elapsed
            obs = self.observe_miner()
            self._refresh_cooling_telemetry()
            if not obs.ok:
                self._note_telemetry_failure(f"recovery_{label}")
                stable = 0
            else:
                self._note_telemetry_success()
                self._lifecycle_reason = self._lifecycle_text(obs)
                if self._hard_fault(obs):
                    self._fail_hard(obs)
                    return "error"
                if self._hashing_sample_ok(obs, mode):
                    stable += 1
                    operational = 0
                    self._log_txn(
                        f"recovery_poll label={label} elapsed={int(elapsed)} "
                        f"stable={stable}/{stable_need} power_w={obs.power_w} "
                        f"hashrate={obs.hashrate} phase={obs.phase} "
                        f"reason={obs.pause_reason} boards={obs.enabled_ids} "
                        f"boards_healthy={obs.boards_healthy} "
                        f"board_health_verified={obs.board_health_verified} "
                        f"chip_temp_f={self._chip_temp_f} "
                        f"txn_active={self._cooling_txn_active} "
                        f"primary_deadline_ts={self._primary_recovery_deadline_ts}"
                    )
                    if stable >= stable_need:
                        return "hashing"
                elif self._active_confirmed(mode, obs):
                    # Running with the expected boards is interim only.
                    # 0 W / missing hashrate does not close the txn.
                    stable = 0
                    operational += 1
                    self._note_recovery_interim(mode, "operational")
                    self._log_txn(
                        f"recovery_poll label={label} elapsed={int(elapsed)} "
                        f"operational={operational}/{stable_need} power_w={obs.power_w} "
                        f"boards_healthy={obs.boards_healthy} "
                        f"board_health_verified={obs.board_health_verified} "
                        f"interim=operational txn_active={self._cooling_txn_active} "
                        f"positive={self._positive_lifecycle(obs)} "
                        f"primary_deadline_ts={self._primary_recovery_deadline_ts}"
                    )
                    if self._cancel_requested or self._owner_txn_id != self._cooling_txn_id:
                        self._log_txn(f"recovery_cancelled label={label}")
                        return "exhausted"
                    if elapsed >= float(expected_s) and not self._positive_lifecycle(obs):
                        self._log_txn(
                            f"recovery_no_progress label={label} elapsed={int(elapsed)} "
                            f"(past expected, running without named lifecycle)"
                        )
                        return "exhausted"
                else:
                    stable = 0
                    operational = 0
                    interim_kind = ""
                    if self._stage_c_ready(obs, mode, hist):
                        interim_kind = "applying"
                        self._note_recovery_interim(mode, "applying")
                        self._log_txn(
                            f"recovery_ramp label={label} elapsed={int(elapsed)} "
                            f"power_w={obs.power_w} hashrate={obs.hashrate} "
                            f"interim=applying txn_active={self._cooling_txn_active} "
                            f"primary_deadline_ts={self._primary_recovery_deadline_ts} "
                            f"(APPLYING stays inside the window)"
                        )
                    self._log_txn(
                        f"recovery_poll label={label} elapsed={int(elapsed)} stable=0 "
                        f"power_w={obs.power_w} hashrate={obs.hashrate} phase={obs.phase} "
                        f"reason={obs.pause_reason} lifecycle={self._lifecycle_reason} "
                        f"positive={self._positive_lifecycle(obs)} boards={obs.enabled_ids} "
                        f"boards_healthy={obs.boards_healthy} board_stale={obs.board_stale} "
                        f"board_health_verified={obs.board_health_verified} "
                        f"interim={interim_kind or '-'} "
                        f"chip_temp_f={self._chip_temp_f} "
                        f"txn_active={self._cooling_txn_active} "
                        f"primary_deadline_ts={self._primary_recovery_deadline_ts}"
                    )
                    if self._cancel_requested or self._owner_txn_id != self._cooling_txn_id:
                        self._log_txn(f"recovery_cancelled label={label}")
                        return "exhausted"
                    if elapsed >= float(expected_s) and not self._positive_lifecycle(obs):
                        self._log_txn(
                            f"recovery_no_progress label={label} elapsed={int(elapsed)} "
                            f"(past expected, lifecycle not positive)"
                        )
                        return "exhausted"
            if self._cancel_requested or (
                self._owner_txn_id and self._owner_txn_id != self._cooling_txn_id
            ):
                self._log_txn(f"recovery_cancelled label={label}")
                return "exhausted"
            if self._now() >= deadline:
                self._log_txn(f"recovery_limit label={label} elapsed={int(elapsed)}")
                return "exhausted"
            self._sleep(poll)

    def _resume_once(self, label: str) -> int | None:
        if label == "retry":
            self._set_cooling_phase("RETRY_RESUME_ONCE", health="RECOVERING")
        else:
            self._set_cooling_phase("RESUME_REQUESTED", health="APPLYING")
        result = self._call_device("resume", lambda: self.b.resume())
        if result is None:
            self._log_txn(f"resume_denied label={label}")
            return None
        if label == "retry":
            self._resume_retry_used = True
            self._resume_retries_used += 1
        try:
            code, body = result
        except Exception as e:
            self._log_txn(f"resume_exc label={label} exc={e}")
            code, body = 500, {"exc": str(e)}
        self._last_resume_result = f"http_{code}"
        self._log_txn(
            f"resume label={label} http={code} body={_summarize_http_body(body)} "
            f"retries_used={self._resume_retries_used}"
        )
        if int(code) == 200:
            self._resume_ts = self._now()
        return int(code)

    def _take_pending_if_terminal(self) -> CoolingProfile | None:
        """Consume coalesced pending only after successful HASHING.

        DEGRADED and ERROR keep the pending ceiling visible and issue no
        further device command. A stale caller must re-check
        ``_auto_apply_pending_allowed`` immediately before any command.
        """
        if not self.settings.coalesce_pending_cooling_requests:
            return None
        if self._cooling_txn_active or not self._auto_apply_pending_allowed():
            self._note_pending_held(self._health_class or self._cooling_terminal_kind or "not_success")
            return None
        pending = self._pending_profile
        if pending is None:
            return None
        if not self._auto_apply_pending_allowed():
            self._note_pending_held("stale_callback")
            return None
        self._pending_profile = None
        self._pending_explicit = False
        self._pending_hold_logged = None
        if pending.matches(self._cooling_applied):
            return None
        return pending

    def _gate_resume(self, label: str) -> str:
        """Re-check the hard-fault predicate immediately before ResumeMining.

        Returns ``ok``, ``hard_fault``, or ``not_clean``. A failed/stale read
        is not a hard fault and is not a clean authorization to resume.
        """
        self._resume_gate = label
        try:
            obs = self.observe_miner()
            if not obs.ok:
                self._note_telemetry_failure(f"resume_gate_{label}")
                self._log_txn(
                    f"resume_gate label={label} telemetry_not_clean "
                    f"— not a hard fault; refuse blind resume"
                )
                return "not_clean"
            self._note_telemetry_success()
            if self._hard_fault(obs):
                self._fail_hard(obs)
                self._log_txn(f"resume_gate label={label} hard_fault — abort resume")
                return "hard_fault"
            self._log_txn(f"resume_gate label={label} clean")
            return "ok"
        finally:
            self._resume_gate = ""

    def _cooling_obs_fields(self) -> dict[str, Any]:
        desired = None if self._cooling_desired is None else self._cooling_desired.max_fan_speed
        effective = self._live_max_fan_speed
        if effective is None and self._cooling_applied is not None:
            effective = self._cooling_applied.max_fan_speed
        pending = None if self._pending_profile is None else self._pending_profile.max_fan_speed
        remaining: int | str = ""
        if self._recovery_limit_s and self._health_class == "RECOVERING":
            remaining = max(0, int(round(self._recovery_limit_s - self._recovery_elapsed_s)))
        return {
            "cooling_txn_id": self._cooling_txn_id,
            "cooling_phase": self._cooling_phase,
            "health_class": self._health_class,
            "desired_ceiling": desired,
            "effective_ceiling": effective,
            "pending_ceiling": pending,
            "last_cooling_result": self._last_cooling_result,
            "last_resume_result": self._last_resume_result,
            "recovery_elapsed_s": int(self._recovery_elapsed_s or 0),
            "recovery_remaining_s": remaining,
            "resume_retry_used": bool(self._resume_retry_used),
            "lifecycle_reason": self._lifecycle_reason,
            "telemetry_freshness": self._telemetry_freshness,
            "board_health_verified": bool(
                getattr(self._last_obs, "board_health_verified", False)
            ),
            "board_health_reason": str(getattr(self._last_obs, "board_health_reason", "") or ""),
            "telemetry_fail_streak": self._telemetry_fail_streak,
            "telemetry_last_success_ts": self._telemetry_last_success_ts,
            # current_error / active_fault follow last_error (the active fault).
            # last_fault_* is the retained sustained-read history and is not
            # the health sensor state.
            "current_error": self.last_error or "",
            "active_fault": bool(self.last_error),
            "last_fault_reason": self._last_fault_reason,
            "last_fault_timestamp": self._last_fault_timestamp,
            "last_fault_class": self._last_fault_class,
            "last_fault_count": int(self._last_fault_count),
            "sustained_telemetry_recovery_polls": int(self._sustained_telemetry_recovery_polls),
            "sustained_telemetry_recovery_required": int(SUSTAINED_TELEMETRY_RECOVERY_POLLS),
            "auto_fan_ceiling_enabled": bool(self.settings.auto_fan_ceiling_enabled),
            "cooling_writes_only_when_paused": bool(self.settings.cooling_writes_only_when_paused),
            "cooling_policy": self.settings.cooling_policy,
            "cooling_control_enabled": bool(self.settings.cooling_control_enabled),
            "telemetry_class": self.telemetry_class,
            "observed_state": self.observed_state,
            "observed_miner_mode": self.observed_miner_mode,
            "controller_state": self.controller_state,
            "last_verified_miner_mode": self._last_verified_miner_mode,
            "actual_mode_is_physical": self.actual_mode in OBSERVED_MINER_MODES,
            "verification_transaction_id": self._verification_ctx.get("transaction_id") or "",
            "verification_entered_mono": self._verification_ctx.get("entered_mono") or 0.0,
            "verification_expected_operation": self._verification_ctx.get("expected_operation") or "",
            "verification_evidence_deadline_mono": self._verification_ctx.get("evidence_deadline_mono") or 0.0,
            "verification_last_valid_telemetry_mono": self._verification_ctx.get("last_valid_telemetry_mono") or 0.0,
            "verification_reason": self._verification_ctx.get("reason") or "",
            "verification_evidence": self._verification_ctx.get("evidence") or "",
            "requested_mode": self.desired_mode,
            "recovery_ready": bool(self.recovery_ready),
            "recovery_valid_polls": int(self._valid_poll_streak),
            "writes_permitted": bool(self.write_permission.permitted),
            "auth_ok": bool(self._auth_ok),
            "bosminer_available": bool(self._bosminer_available),
            "desired_target_c": None
            if self._cooling_desired is None
            else self._cooling_desired.target_temperature_c,
            "configured_cooling_mode": self._configured_cooling.get("mode"),
            "configured_target_c": self._configured_cooling.get("target_temperature_c"),
            **self._observability_fields(),
        }

    def _sync_idle_health(self, obs: MinerObservation | None) -> None:
        if self._cooling_txn_active or self._cooling_transition_active:
            self._sustained_telemetry_recovery_pending = False
            return
        if self._fault_latched or self.actual_mode == "FAULT_LATCHED":
            self._sustained_telemetry_recovery_pending = False
            self._health_class = "FAULT_LATCHED"
            return
        if self._sustained_telemetry_recovery_pending:
            self._sustained_telemetry_recovery_pending = False
            # Only the sustained-read latch is eligible. A failed check leaves
            # ERROR, last_error, and phase untouched. Other terminals never
            # set this flag and stay on the early return below.
            self._finish_sustained_telemetry_recovery(obs)
            return
        if self._health_class in {"DEGRADED_NEEDS_ATTENTION", "ERROR", "FAULT_LATCHED"}:
            return
        if self.actual_mode == "APPLYING":
            self._health_class = "APPLYING"
            return
        if obs is None or not obs.ok:
            return
        if self._paused_confirmed(obs):
            self._health_class = "PAUSED"
            self._cooling_phase = "IDLE"
            return
        sample_mode = self.actual_mode if self.actual_mode in BOARD_MAP else "ONE_BOARD"
        if self._hashing_sample_ok(obs, sample_mode):
            self._health_class = "HASHING"
            self._cooling_phase = "IDLE"
            return
        if obs.running:
            self._health_class = "RECOVERING"
            return
        self._health_class = "UNKNOWN"

    def request_cooling_ceiling(self, pct: int, *, resume_mode: str | None = None) -> bool:
        """Legacy explicit fan-ceiling transaction. Never a live mid-hash PUT.

        Refused while cooling control is disabled, and while cooling_policy is
        native_auto_target. A fan-only auto PUT can clear target_temperature
        on this firmware. Use the temperature policy (or the wide envelope
        helpers) instead, and only after cooling_control_enabled is explicit.
        """
        if not self._cooling_control_enabled():
            self._log_write_blocked(
                "request_cooling_ceiling", "controller", "cooling_control_disabled"
            )
            self._log_txn(
                "request_cooling_ceiling refused — cooling_control_disabled; "
                "Braiins owns cooling (no pause, no PUT)"
            )
            self._emit("lard_cooling_deferred", reason="cooling_control_disabled")
            return False
        if self._native_temperature_policy():
            self._log_txn(
                "request_cooling_ceiling refused — native policy; "
                "fan ceiling is legacy and a fan-only PUT can clear target_temperature"
            )
            self._emit("lard_cooling_deferred", reason="native_policy_fan_ceiling_legacy")
            return False
        n = clamp_fan_max_pct(pct)
        fans = MIN_REQUIRED_FANS if n >= FAN_MAX_DEFAULT else None
        profile = CoolingProfile("EXPLICIT", n, None, fans)
        self._cooling_desired = profile
        with self._txn_lock:
            if self._cooling_txn_active:
                self._defer_profile(profile, "txn_active", queue=True, explicit=True)
                return False
            policy = self._policy_denial()
            if policy or self._health_class in TERMINAL_HEALTH:
                self._log_write_blocked(
                    "request_cooling_ceiling", "controller", policy or "terminal"
                )
                self._log_txn(
                    f"write_denied action=request_cooling reason={policy or 'terminal'}"
                )
                self._emit(
                    "lard_write_denied",
                    action="request_cooling",
                    reason=policy or "terminal",
                )
                return False
        mode = resume_mode or (
            self.desired_mode if self.desired_mode in RANK else self._settled_mode()
        )
        if mode not in RANK:
            mode = "ONE_BOARD"
        return self._gated_cooling_transition(profile, mode)

    def request_temperature_policy(self, *, resume_mode: str | None = None) -> bool:
        """One gated Automatic-cooling policy write. Not a fan-ceiling chase.

        Refuses when cooling control is disabled, setpoints are unordered, or
        writes are disarmed. Does not Start, Restart, or reboot. Still
        pause-confirms before PUT when cooling control is explicitly enabled.
        """
        if not self._cooling_control_enabled():
            self._log_write_blocked(
                "request_temperature_policy", "controller", "cooling_control_disabled"
            )
            self._log_txn(
                "request_temperature_policy refused — cooling_control_disabled; "
                "Braiins owns cooling (no pause, no PUT)"
            )
            self._emit("lard_cooling_deferred", reason="cooling_control_disabled")
            return False
        profile = self.desired_temperature_policy()
        if profile is None:
            self._log_txn("temperature policy invalid — refuse cooling PUT")
            self._emit("lard_cooling_deferred", reason="invalid_temperature_policy")
            return False
        self._cooling_desired = profile
        with self._txn_lock:
            if self._cooling_txn_active:
                self._defer_profile(profile, "txn_active", queue=True, explicit=True)
                return False
            policy = self._policy_denial()
            if policy or self._health_class in TERMINAL_HEALTH:
                self._log_write_blocked(
                    "request_temperature_policy", "controller", policy or "terminal"
                )
                self._log_txn(
                    f"write_denied action=request_temperature_policy reason={policy or 'terminal'}"
                )
                self._emit(
                    "lard_write_denied",
                    action="request_temperature_policy",
                    reason=policy or "terminal",
                )
                return False
        mode = resume_mode or (
            self.desired_mode if self.desired_mode in RANK else self._settled_mode()
        )
        if mode not in RANK:
            mode = "ONE_BOARD"
        self._mark_temperature_policy_seen()
        return self._gated_cooling_transition(profile, mode)

    def run_plain_pause_resume(self, mode: str) -> bool:
        """Pause, confirm, resume once, then bounded recovery. No cooling PUT."""
        with self._braiins_mutex:
            self._assign_txn_id()
            self._claim_cooling_owner()
            self._cooling_transition_active = True
            try:
                obs = self.observe_miner()
                self._log_txn(
                    f"plain_pause_resume pre_state mode={mode} power_w={obs.power_w} "
                    f"phase={obs.phase} paused={obs.paused}"
                )
                if not self._ensure_paused_idle_for_cooling():
                    return False
                return self._resume_after_cooling(self._cooling_applied, mode)
            finally:
                self._release_cooling_owner()
                self._cooling_transition_active = False

    def _ensure_paused_idle_for_cooling(self) -> bool:
        """Pause, then two consecutive miner-state polls. HTTP 200 alone is not confirmation."""
        self._set_cooling_phase("PAUSE_REQUESTED", health="APPLYING")
        poll = self._poll_interval()
        deadline = self._now() + max(float(self._board_wait_s()), poll * 4)
        sent = False
        consecutive = 0
        while self._now() < deadline:
            obs = self.observe_miner()
            if not obs.ok:
                self._note_telemetry_failure("pause_confirm")
                consecutive = 0
                self._sleep(poll)
                continue
            self._note_telemetry_success()
            if self._hard_fault(obs):
                self._fail_hard(obs)
                return False
            if self._cooling_paused_idle(obs):
                consecutive += 1
                self._log_txn(
                    f"pause_poll consecutive={consecutive} power_w={obs.power_w} "
                    f"reason={obs.pause_reason}"
                )
                if consecutive >= 2:
                    self._set_cooling_phase("PAUSED_CONFIRMED", health="PAUSED")
                    self.actual_mode = "APPLYING"
                    self.last_error = ""
                    self._log_txn("pause_confirmed")
                    return True
            else:
                consecutive = 0
                if not sent:
                    result = self._call_device("pause", lambda: self.b.pause())
                    if result is None:
                        self._log_txn("pause_denied — fail closed")
                        return False
                    try:
                        code, body = result
                    except Exception as e:
                        self._log_txn(f"pause_exc {e}")
                        code, body = 500, {"exc": str(e)}
                    sent = True
                    self._log_txn(f"pause http={code} body={_summarize_http_body(body)}")
                    if code != 200 and not is_http_5xx(code):
                        self.last_error = f"cooling_pause_http_{code}"
                        self._cooling_fail(self.last_error, self._cooling_applied)
                        return False
                    if is_http_5xx(code):
                        self._sleep(poll)
                        result = self._call_device("pause", lambda: self.b.pause())
                        if result is None:
                            self._log_txn("pause_retry_denied — fail closed")
                            return False
                        try:
                            code, body = result
                        except Exception as e:
                            code, body = 500, {"exc": str(e)}
                        self._log_txn(f"pause_retry http={code} body={_summarize_http_body(body)}")
                        if code != 200:
                            self.last_error = f"cooling_pause_http_{code}"
                            self._cooling_fail(self.last_error, self._cooling_applied)
                            return False
            self._sleep(poll)
        self._log_txn("pause_not_confirmed")
        self._finish_degraded("PAUSED", "pause_not_confirmed")
        return False

    def _apply_cooling_while_paused(self, profile: CoolingProfile) -> bool:
        """PUT tagged auto envelope, confirm it stuck. Never while hashing."""
        if not self._cooling_control_enabled():
            self._log_txn("refuse cooling PUT reason=cooling_control_disabled")
            self._last_cooling_result = "refused_cooling_control_disabled"
            self._emit("lard_cooling_deferred", reason="cooling_control_disabled")
            return False
        self._set_cooling_phase("COOLING_APPLYING", health="APPLYING")
        obs = self.observe_miner()
        paused_idle = self._cooling_paused_idle(obs)
        hashing = bool(obs.ok and obs.running and not paused_idle)
        if hashing or (self.settings.cooling_writes_only_when_paused and not paused_idle):
            reason = "hashing" if hashing else "not_paused"
            self._log_txn(f"refuse cooling PUT reason={reason}")
            self._last_cooling_result = f"refused_{reason}"
            self._emit("lard_cooling_deferred", reason=reason)
            return False
        previous = self._cooling_applied
        extra = profile.extra_auto()
        before = self._readiness_snapshot("pre_cooling_put")
        try:
            code, body = self._http_retry(
                lambda: self._device_tuple(
                    "cooling_put",
                    lambda: self.b.set_cooling_auto(profile.max_fan_speed, extra or None),
                ),
                "cooling_put",
            )
        except Exception as e:
            self._cooling_fail(f"cooling_put_exc:{e}", previous)
            return False
        if self._denied_body(body):
            self._log_txn("cooling_put_denied — no resume")
            return False
        self._log_txn(
            f"cooling PUT profile={profile.name} max={profile.max_fan_speed} "
            f"min={profile.min_fan_speed} target_c={profile.target_temperature_c} "
            f"hot_c={profile.hot_temperature_c} dangerous_c={profile.dangerous_temperature_c} "
            f"http={code} body={_summarize_http_body(body)} "
            f"chip_temp_f={self._chip_temp_f} power_w={self.power_w}"
        )
        self._last_cooling_result = f"http_{code}"
        if code != 200:
            self._cooling_fail(f"cooling_put_http_{code}", previous)
            return False
        if not self._confirm_cooling(profile):
            self._cooling_fail(self.last_error or "cooling_confirm_failed", previous)
            return False
        stabilize = float(self.settings.cooling_stabilize_seconds or 0)
        if stabilize > 0:
            self._sleep(stabilize)
        # Log whether the PUT temporarily changed bosminer process / ready / pause reason.
        self._readiness_snapshot("post_cooling_put", previous=before)
        self._cooling_applied = profile
        self._cooling_last_change_ts = self._now()
        self.last_error = ""
        return True

    def _wait_cooling_resume_verify(self, mode: str, profile: CoolingProfile) -> bool:
        """user_pause=false, running, boards, watts>0, TH/s recovering, cooling still requested."""
        deadline = self._now() + self._resume_wait_s()
        hist: dict[str, list] = {"power": [], "hash": []}
        while self._now() < deadline:
            self.health.touch()
            obs = self.observe_miner()
            if obs.ok and not self._is_paused(obs) and not obs.user_paused:
                if mode in BOARD_MAP and mode != "PAUSED":
                    if not self._boards_match(obs.enabled_ids, BOARD_MAP[mode]):
                        self._sleep(BOARD_POLL_S)
                        continue
                if obs.running and obs.power_w is not None and float(obs.power_w) > 0:
                    self._record_trend(obs, hist)
                    cooling_ok = profile.matches(self._cooling_applied) or profile.matches(
                        self._live_cooling_profile()
                    )
                    rising = self._trend_rising(hist) or obs.hashrate is None or float(obs.hashrate) > 0
                    if cooling_ok and rising:
                        return True
                    if cooling_ok and float(obs.power_w) > COOLING_IDLE_POWER_W:
                        return True
            self._sleep(BOARD_POLL_S)
        self.last_error = "cooling_resume_verify"
        return False

    def _readiness_from_obs(self, obs: MinerObservation) -> dict[str, Any]:
        return {
            "ok": bool(obs.ok),
            "paused": bool(obs.paused),
            "user_paused": bool(obs.user_paused),
            "running": bool(obs.running),
            "starting": bool(obs.starting),
            "phase": obs.phase or "",
            "status": obs.status_raw,
            "pause_reason": obs.pause_reason or "",
            "power_w": obs.power_w,
            "bosminer_uptime_s": obs.bosminer_uptime_s,
            "miner_ready": obs.miner_ready,
            "not_started": bool(obs.not_started),
        }

    def _log_readiness(self, label: str, snap: dict[str, Any], previous: dict | None = None) -> None:
        changed = []
        if previous:
            for key in snap:
                if previous.get(key) != snap.get(key):
                    changed.append(f"{key}:{previous.get(key)}->{snap.get(key)}")
        suffix = f" changed=[{', '.join(changed)}]" if changed else ""
        op = self._cooling_txn_id or "-"
        self.log(
            f"op={op} phase={self._cooling_phase} "
            f"cooling readiness {label} paused={snap.get('paused')} "
            f"user_paused={snap.get('user_paused')} running={snap.get('running')} "
            f"starting={snap.get('starting')} phase={snap.get('phase')} "
            f"status={snap.get('status')} pause_reason={snap.get('pause_reason')} "
            f"power_w={snap.get('power_w')} bosminer_uptime_s={snap.get('bosminer_uptime_s')} "
            f"miner_ready={snap.get('miner_ready')} not_started={snap.get('not_started')}"
            f"{suffix}"
        )

    def _readiness_snapshot(self, label: str, previous: dict | None = None) -> dict[str, Any]:
        """Poll pause / process-ready / watts / status for cooling-resume diagnosis."""
        try:
            obs = self.observe_miner()
        except Exception as e:
            self.log(f"cooling readiness {label} observe_exc={e}")
            obs = MinerObservation()
        snap = self._readiness_from_obs(obs)
        self._log_readiness(label, snap, previous)
        return snap

    def _cooling_process_ready(self, snap: dict[str, Any]) -> bool:
        """True when bosminer looks able to accept ResumeMining / Start.

        OpenAPI: bosminer_uptime_s == 0 means the process is not running.
        user_pause + ~0 W is expected after a gated cooling PUT — that is ready.
        """
        if snap.get("not_started") or snap.get("miner_ready") is False:
            return False
        uptime = snap.get("bosminer_uptime_s")
        if uptime is not None:
            try:
                if float(uptime) <= 0:
                    return False
            except (TypeError, ValueError):
                pass
        if snap.get("ok"):
            return True
        return False

    def _wait_cooling_process_ready(self, timeout_s: float) -> dict[str, Any]:
        deadline = self._now() + max(0.0, float(timeout_s))
        last = self._readiness_snapshot("process_ready_poll")
        while True:
            if self._cooling_process_ready(last):
                return last
            if self._now() >= deadline:
                return last
            self._sleep(min(float(BOARD_POLL_S), 5.0))
            last = self._readiness_snapshot("process_ready_poll")

    def _cooling_resume_accepted(self, obs: MinerObservation) -> bool:
        if not obs.ok:
            return False
        if obs.running or obs.starting or self._stage_a_cleared(obs):
            return True
        return self._transitional_operational(obs) and not self._is_paused(obs)

    def _cooling_escalate_start(self) -> bool:
        """Hard-disabled. Cooling recovery must not call Start mining."""
        self._emit("lard_cooling_escalate_blocked", action="start")
        return False

    def _cooling_escalate_restart(self) -> bool:
        """Hard-disabled. Cooling recovery must not call BOSminer Restart."""
        self._emit("lard_cooling_escalate_blocked", action="restart")
        return False

    def _resume_after_cooling(
        self, previous: CoolingProfile | None, mode: str | None = None
    ) -> CoolingResult:
        """One resume, then bounded recovery. 0 W during cooldown/APPLYING is not ERROR.

        At most one automatic resume retry per transaction. Start / BOSminer Restart
        are not used. Device reboot is never issued. operational/applying do not
        close the transaction. A hard fault before either resume aborts.
        """
        if mode not in RANK:
            mode = self.desired_mode if self.desired_mode in RANK else self._settled_mode()
        if mode == "PAUSED":
            mode = self._settled_mode() if self._settled_mode() in BOARD_MAP else "ONE_BOARD"
        profile = self._cooling_desired or self._cooling_applied or previous
        if profile is None:
            profile = CoolingProfile(str(mode), FAN_MAX_DEFAULT, None, MIN_REQUIRED_FANS)
        self.actual_mode = "APPLYING"
        self._cooling_transition_active = True
        self.last_error = ""
        settle_status = self._settle_after_cooling(profile)
        if settle_status == "hard_fault":
            return _txn_result("error")
        if settle_status == "cancelled" or self._cancel_requested or self._reload_hold:
            self._log_txn("primary resume withheld — settle cancelled")
            return CoolingResult("denied")
        gate = self._gate_resume("primary")
        if gate == "hard_fault":
            return _txn_result("error")
        if gate == "not_clean":
            self._log_txn("primary resume withheld — telemetry not clean")
        else:
            code = self._resume_once("primary")
            if code is None:
                return CoolingResult("denied")
            if code != 200 and not is_http_5xx(code):
                self._cooling_fail(f"cooling_resume_http_{code}", previous)
                return _txn_result("error")
        outcome = self._recovery_window(
            mode,
            limit_s=float(self.settings.maximum_recovery_seconds),
            expected_s=float(self.settings.expected_recovery_seconds),
            label="primary",
        )
        if outcome == "hashing":
            return self._finish_hashing(mode)
        if outcome in {"operational", "applying"}:
            # Defensive: these are not terminal. Do not report success.
            self._log_txn(f"ignored_nonterminal_outcome={outcome}")
            return self._finish_degraded(mode, f"nonterminal_{outcome}")
        if outcome == "error":
            return _txn_result("error")
        if self._cancel_requested or self._reload_hold or self._owner_txn_id != self._cooling_txn_id:
            self._log_txn("retry withheld — txn cancelled or no longer owned")
            return CoolingResult("denied")
        retries_allowed = int(self.settings.max_resume_retries_per_transaction or 0)
        if self.settings.resume_retry_enabled and self._resume_retries_used < retries_allowed:
            gate = self._gate_resume("retry")
            if gate == "hard_fault":
                return _txn_result("error")
            if gate == "not_clean":
                self._log_txn("retry resume withheld — telemetry not clean")
            else:
                self._log_txn("recovery_exhausted — one guarded resume retry")
                code = self._resume_once("retry")
                if code is None:
                    return CoolingResult("denied")
                if code != 200 and not is_http_5xx(code):
                    self._cooling_fail(f"cooling_resume_http_{code}", previous)
                    return _txn_result("error")
                post = float(self.settings.post_retry_recovery_seconds or 0)
                expected = min(float(self.settings.expected_recovery_seconds or 0), post)
                outcome = self._recovery_window(
                    mode,
                    limit_s=post,
                    expected_s=expected,
                    label="post_retry",
                )
                if outcome == "hashing":
                    return self._finish_hashing(mode)
                if outcome in {"operational", "applying"}:
                    self._log_txn(f"ignored_nonterminal_outcome={outcome}")
                    return self._finish_degraded(mode, f"nonterminal_{outcome}")
                if outcome == "error":
                    return _txn_result("error")
        return self._finish_degraded(mode, "recovery_window_exhausted")

    def _cooling_transaction_body(self, profile: CoolingProfile, resume_mode: str) -> bool:
        self._assign_txn_id()
        self._cooling_desired = profile
        obs = self.observe_miner()
        self._refresh_cooling_telemetry()
        self._log_txn(
            f"pre_state resume_mode={resume_mode} desired_max={profile.max_fan_speed} "
            f"applied={None if self._cooling_applied is None else self._cooling_applied.max_fan_speed} "
            f"live_max={self._live_max_fan_speed} power_w={obs.power_w} phase={obs.phase} "
            f"reason={obs.pause_reason} paused={obs.paused} running={obs.running} "
            f"hashrate={obs.hashrate}"
        )
        if self._cooling_already_effective(profile):
            self._last_cooling_result = "noop"
            self._log_txn("noop desired matches effective — no pause/resume")
            self._emit("lard_cooling_noop")
            self._close_cooling_terminal("noop")
            self._clear_inflight_marker()
            return _txn_result("noop")
        refuse = self._cooling_refuse_reason(obs)
        if refuse == "hard_fault":
            self._claim_cooling_owner()
            try:
                return self._fail_hard(obs)
            finally:
                self._release_cooling_owner()
        if refuse:
            self._defer_profile(profile, refuse, queue=True, explicit=True)
            self._clear_inflight_marker()
            return _txn_result("refused")
        previous = self._cooling_applied
        self._claim_cooling_owner()
        self._cooling_transition_active = True
        try:
            if not self._ensure_paused_idle_for_cooling():
                return False
            self._set_cooling_phase("COOLING_APPLYING", health="APPLYING")
            if not self._apply_cooling_while_paused(profile):
                return False
            self._last_cooling_result = "put_ok"
            if resume_mode == "PAUSED":
                self._health_class = "PAUSED"
                self._cooling_phase = "PAUSED_CONFIRMED"
                self._mark_confirmed("PAUSED")
                self._close_cooling_terminal("paused")
                return _txn_result("paused")
            return self._resume_after_cooling(previous, resume_mode)
        except Exception as e:
            self._cooling_fail(f"cooling_exc:{e}", previous)
            self.log(f"COOLING exc {traceback.format_exc()}")
            return _txn_result("error")
        finally:
            self._release_cooling_owner()
            self._cooling_transition_active = False

    def _gated_cooling_transition(
        self,
        profile: CoolingProfile,
        resume_mode: str,
        _depth: int = 0,
        _apply_gen: int | None = None,
    ) -> CoolingResult | bool:
        """Pause-first cooling transaction plus bounded recovery. Per-miner lock held.

        Recursive coalesced apply runs only after successful HASHING.
        A callback whose generation or terminal no longer matches issues no
        device command. Cooling control disabled returns before any pause
        or PUT: Braiins owns cooling.
        """
        if not self._cooling_control_enabled():
            self._log_txn(
                "cooling_control_disabled — no pause-for-cooling, no cooling PUT"
            )
            self._emit("lard_cooling_deferred", reason="cooling_control_disabled")
            return _txn_result("refused")
        with self._braiins_mutex:
            if _depth > 0:
                if _apply_gen is not None and _apply_gen != self._cooling_terminal_gen:
                    self._pending_profile = profile
                    self._pending_explicit = True
                    self._note_pending_held("stale_callback")
                    return _txn_result("refused")
                if not self._auto_apply_pending_allowed():
                    self._pending_profile = profile
                    self._pending_explicit = True
                    self._note_pending_held("stale_callback")
                    return _txn_result("refused")
            if self._cooling_txn_active:
                self._defer_profile(profile, "other_transaction", queue=True, explicit=True)
                return _txn_result("refused")
            gen_at_start = self._cooling_terminal_gen
            ok = self._cooling_transaction_body(profile, resume_mode)
            if self._cooling_terminal_kind in {"degraded", "error"} or (
                self._cooling_terminal_gen != gen_at_start
                and self._health_class in {"DEGRADED_NEEDS_ATTENTION", "ERROR"}
            ):
                self._note_pending_held(self._health_class or self._cooling_terminal_kind)
                pending = None
            elif not self._auto_apply_pending_allowed():
                self._note_pending_held(self._health_class or "not_success")
                pending = None
            else:
                pending = self._take_pending_if_terminal()
            apply_gen = self._cooling_terminal_gen
        if pending is not None and _depth < 3:
            if (
                not self._auto_apply_pending_allowed()
                or apply_gen != self._cooling_terminal_gen
            ):
                self._pending_profile = pending
                self._pending_explicit = True
                self._note_pending_held("stale_callback")
                return ok
            self._log_txn(f"apply_coalesced_pending max={pending.max_fan_speed}")
            return self._gated_cooling_transition(
                pending, resume_mode, _depth=_depth + 1, _apply_gen=apply_gen
            )
        return ok

    def _apply_live_board_health(self, obs: MinerObservation) -> None:
        """Fill board-health fields from the current read only. Never default healthy.

        A missing ``board_health`` hook, a raised hook, or a payload that does
        not verify is ``boards_healthy=False`` and ``board_health_verified=False``.
        Previous polls are not reused.
        """
        obs.boards_healthy = False
        obs.board_stale = False
        obs.board_health_verified = False
        obs.board_reports = []
        obs.safety_fault = ""
        obs.board_health_reason = "unverified"
        health_fn = getattr(self.b, "board_health", None)
        info = None
        if not callable(health_fn):
            obs.board_stale = True
            obs.board_health_reason = "board_health_hook_absent"
            self._log_txn("board_health hook absent — fail closed (not healthy)")
            return
        try:
            info = health_fn()
        except Exception as e:
            obs.board_stale = True
            obs.board_health_reason = f"board_health_exc:{e}"
            self._log_txn(f"board_health unavailable — fail closed: {e}")
            return
        if not isinstance(info, dict):
            obs.board_stale = True
            obs.board_health_reason = "board_health_missing"
            self._log_txn("board_health empty — fail closed (not healthy)")
            return
        obs.board_reports = [rec for rec in (info.get("boards") or []) if isinstance(rec, dict)]
        obs.board_health_reason = str(info.get("reason") or "")
        obs.safety_fault = str(info.get("safety_fault") or "")
        obs.board_stale = bool(info.get("stale")) or bool(info.get("malformed"))
        # A missing "healthy" key is not healthy. A missing "verified" key is not verified.
        verified = bool(info.get("verified")) and not obs.board_stale and not info.get("malformed")
        if "healthy" not in info:
            healthy = False
            if not obs.board_health_reason:
                obs.board_health_reason = "healthy_key_absent"
        else:
            healthy = bool(info.get("healthy"))
        obs.board_health_verified = verified
        obs.boards_healthy = bool(healthy and verified and not obs.safety_fault and not obs.board_stale)

    def _evidence_window_s(self) -> float:
        """Configured monotonic verification window. Not a larger fallback."""
        return max(1.0, float(self.settings.expected_recovery_seconds or EXPECTED_RECOVERY_S))

    def _transition_evidence_fresh(self) -> bool:
        """Independent lifecycle evidence still inside the monotonic window.

        A previous APPLYING label is not evidence. Zero watts is not evidence.
        The word "applying" is not evidence.
        """
        if not self._transition_evidence:
            return False
        return (self._now() - self._transition_evidence_mono) <= self._evidence_window_s()

    def _verification_deadline_expired(self) -> bool:
        """True when independent evidence is missing or past its deadline.

        A recorded WAITING deadline is also honored and is never extended by
        a later unavailable poll.
        """
        if not self._transition_evidence_fresh():
            return True
        deadline = float(self._verification_ctx.get("evidence_deadline_mono") or 0.0)
        if deadline and self._now() > deadline:
            return True
        return False

    def _in_verification_state(self) -> bool:
        """APPLYING, WAITING_FOR_BRAIINS, or an active cooling transition."""
        return (
            self.actual_mode in {"APPLYING", "WAITING_FOR_BRAIINS"}
            or self.controller_state in {"APPLYING", "WAITING_FOR_BRAIINS"}
            or self._cooling_transition_active
        )

    def _verified_physical_mode(self, obs: MinerObservation | None) -> str:
        """Physical mode from a current coherent miner read. Empty if unverified."""
        if obs is None or not getattr(obs, "ok", False):
            return ""
        if self._hard_fault(obs):
            return ""
        if self._paused_confirmed(obs):
            return "PAUSED"
        if obs.running and not self._is_paused(obs):
            boards = set(obs.enabled_ids)
            if boards == {"1", "2", "3"}:
                return "THREE_BOARD"
            if boards == {"1", "2"}:
                return "TWO_BOARD"
            if boards == {"1"}:
                return "ONE_BOARD"
        return ""

    def _sync_mode_contract(self, obs: MinerObservation | None = None) -> None:
        """Publish physical mode and controller lifecycle as different fields.

        ``actual_mode`` stays the compatibility sensor. It is a verified
        physical mode only when the value is in ``OBSERVED_MINER_MODES``.
        """
        if obs is None:
            obs = getattr(self, "_last_obs", None)
        physical = self._verified_physical_mode(obs)
        if physical:
            self.observed_miner_mode = physical
            self._last_verified_miner_mode = physical
        elif (
            self.actual_mode in OBSERVED_MINER_MODES
            and self.telemetry_class in {"VALID_PAUSED", "RUNNING_HEALTHY"}
            and not self._fault_latched
        ):
            self.observed_miner_mode = self.actual_mode
            self._last_verified_miner_mode = self.actual_mode
        else:
            self.observed_miner_mode = "UNVERIFIED"

        self.write_gate = "ARMED" if self.settings.enable_writes else "DISARMED"
        if self._fault_latched or self.actual_mode == "FAULT_LATCHED":
            self.controller_state = "FAULT_LATCHED"
        elif self.actual_mode == "WAITING_FOR_BRAIINS":
            self.controller_state = "WAITING_FOR_BRAIINS"
        elif self.actual_mode == "APPLYING" or self._cooling_transition_active:
            self.controller_state = "APPLYING"
        elif self.actual_mode == "ERROR":
            self.controller_state = "ERROR"
        elif (
            self.observed_miner_mode in {"ONE_BOARD", "TWO_BOARD", "THREE_BOARD"}
            and self.telemetry_class == "RUNNING_HEALTHY"
        ):
            self.controller_state = "RUNNING"
        elif self.observed_miner_mode in OBSERVED_MINER_MODES:
            self.controller_state = "OBSERVING"
        else:
            self.controller_state = "DISARMED"
        if self.write_permission is not None:
            self.write_permission.controller_state = self.controller_state

    def _fault_reason_for_verification(self) -> str:
        """Distinguish why verification ended in FAULT_LATCHED."""
        cls = self.telemetry_class
        err = (self.last_error or "").lower()
        had_evidence = bool(self._transition_evidence_mono)
        expired = not self._transition_evidence_fresh()
        if (
            self._board_unverified
            or "faulted_unverified" in err
            or "board_wait_timeout" in err
        ):
            reason = "board_verification_timeout_or_mismatch"
        elif cls == "BOSMINER_UNAVAILABLE":
            reason = "bosminer_unavailable_during_verification"
        elif cls in {"REQUIRED_TELEMETRY_MALFORMED", "UNKNOWN"} or "malformed" in err:
            reason = "required_telemetry_malformed"
        elif cls == "FAULT_LATCHED" or self._critical_fault:
            reason = "contradictory_telemetry"
        elif cls in {"API_UNREACHABLE", "AUTHENTICATION_FAILED"}:
            reason = "required_telemetry_unavailable"
        elif expired and had_evidence:
            reason = "valid_transition_evidence_expired"
        else:
            reason = "required_telemetry_unavailable"
        if (
            expired
            and had_evidence
            and "valid_transition_evidence_expired" not in reason
        ):
            reason = f"{reason}|valid_transition_evidence_expired"
        return reason

    def _enter_waiting_for_braiins(self) -> None:
        """Bounded wait. Deadline is the existing evidence window, not a new one."""
        now = self._now()
        window = self._evidence_window_s()
        deadline = float(self._transition_evidence_mono) + window
        ctx = self._verification_ctx
        if self.actual_mode != "WAITING_FOR_BRAIINS":
            ctx["entered_mono"] = now
            ctx["transaction_id"] = self._cooling_txn_id or ""
            ctx["expected_operation"] = self.desired_mode or self.reason or ""
            ctx["evidence_deadline_mono"] = deadline
            ctx["last_valid_telemetry_mono"] = float(self._telemetry_last_success_mono or 0.0)
            ctx["reason"] = self.telemetry_class or "telemetry_unavailable"
            ctx["evidence"] = self._lifecycle_reason or "prior_independent_lifecycle"
        elif not ctx.get("evidence_deadline_mono"):
            ctx["evidence_deadline_mono"] = deadline
        self.telemetry_class = "WAITING_FOR_BRAIINS"
        self.observed_state = "WAITING_FOR_BRAIINS"
        self.actual_mode = "WAITING_FOR_BRAIINS"
        self.controller_state = "WAITING_FOR_BRAIINS"
        self._cooling_transition_active = False

    def _enter_fault_latched(self, reason: str) -> None:
        """Latch a verification fault. Does not arm writes or issue a command."""
        self._fault_latched = True
        if reason and reason not in (self.last_error or ""):
            self.last_error = f"{self.last_error}|{reason}" if self.last_error else reason
        self.telemetry_class = "FAULT_LATCHED"
        self.observed_state = "FAULT_LATCHED"
        self.actual_mode = "FAULT_LATCHED"
        self.controller_state = "FAULT_LATCHED"
        self._health_class = "FAULT_LATCHED"
        self._cooling_transition_active = False
        self._transition_evidence = False
        self._drop_stale_write_queue("fault_latched")

    def _recovery_sample_ok(self, obs: MinerObservation) -> bool:
        """One poll toward recovery readiness. Does not arm writes."""
        if not obs.ok or not self._auth_ok or not self._bosminer_available:
            return False
        if self.telemetry_class not in {"VALID_PAUSED", "VALID_TRANSITION", "RUNNING_HEALTHY"}:
            return False
        if self._critical_fault:
            return False
        if self.ha.state(ENT_OLD_AUTO) == "on":
            return False
        if self.competing_writer_blocks_arming():
            return False
        return True

    def _settle_unavailable_applying(self, obs: MinerObservation) -> None:
        """Bound every verification state, including WAITING_FOR_BRAIINS.

        APPLYING may enter WAITING_FOR_BRAIINS only while independent miner
        lifecycle evidence is still inside the configured monotonic window.
        WAITING_FOR_BRAIINS, APPLYING, and an active cooling transition all
        re-check that window on every poll. Deadline expiry, malformed or
        contradictory telemetry, or a read that cannot establish a miner
        lifecycle leaves FAULT_LATCHED. The latch does not arm writes.

        An in-flight apply or cooling transaction keeps its own loop across a
        transient miss. It still faults once a recorded evidence deadline has
        passed and the miner read is still unavailable.
        """
        if self._fault_latched or self.actual_mode == "FAULT_LATCHED":
            return
        if self._health_class in TERMINAL_HEALTH:
            return
        if not self._in_verification_state():
            return
        # An ok hard fault already has the cooling ERROR path. Do not relabel it.
        if obs.ok and self._hard_fault(obs):
            return
        in_flight = bool(self._cooling_txn_active or self._apply_depth)
        if in_flight:
            waiting = (
                self.actual_mode == "WAITING_FOR_BRAIINS"
                or self.controller_state == "WAITING_FOR_BRAIINS"
            )
            recorded = float(self._verification_ctx.get("evidence_deadline_mono") or 0.0)
            recorded_elapsed = bool(recorded and self._now() > recorded)
            evidence_elapsed = bool(
                self._transition_evidence and not self._transition_evidence_fresh()
            )
            if not (waiting or recorded_elapsed or evidence_elapsed):
                return
        # A live running or paused report is miner state, including 0 W.
        if obs.ok and (
            obs.running
            or self._paused_confirmed(obs)
            or self.telemetry_class in {"VALID_PAUSED", "VALID_TRANSITION", "RUNNING_HEALTHY"}
        ):
            if (
                self.actual_mode == "WAITING_FOR_BRAIINS"
                and self.telemetry_class == "VALID_TRANSITION"
            ):
                self.actual_mode = "APPLYING"
                self.controller_state = "APPLYING"
            return
        applying_only = self._controller_label_only(obs)
        unexplained = obs.ok and self.telemetry_class == "UNKNOWN"
        unavailable = (not obs.ok) or self.telemetry_class in {
            "API_UNREACHABLE",
            "AUTHENTICATION_FAILED",
            "BOSMINER_UNAVAILABLE",
            "REQUIRED_TELEMETRY_MALFORMED",
        }
        if not unavailable and not applying_only and not unexplained:
            return
        malformed = (
            applying_only
            or unexplained
            or self.telemetry_class == "REQUIRED_TELEMETRY_MALFORMED"
        )
        if malformed or self._verification_deadline_expired():
            self._enter_fault_latched(self._fault_reason_for_verification())
            return
        self._enter_waiting_for_braiins()

    def _finish_observation(self, obs: MinerObservation) -> None:
        """Classify the read, update recovery readiness, settle stuck APPLYING.

        Recovery readiness never sets ``write_permission.permitted``.
        """
        if obs.ok:
            error_text = ""
            http_code = None
        else:
            error_text = f"{self.last_error or ''} {self._last_transport or ''}"
            http_code = self._last_http_code
        positive = bool(obs.ok and self._positive_lifecycle(obs))
        malformed = (not obs.ok) and ("malformed" in error_text.lower())
        self.telemetry_class = classify_miner_telemetry(
            ok=bool(obs.ok),
            http_code=http_code,
            error_text=error_text,
            paused=bool(obs.paused),
            user_paused=bool(obs.user_paused),
            running=bool(obs.running),
            positive_lifecycle=positive,
            power_w=obs.power_w,
            critical_fault=bool(obs.ok and self._hard_fault(obs)),
            malformed=malformed,
            board_unverified=False,
        )
        if self.telemetry_class == "AUTHENTICATION_FAILED":
            self._auth_ok = False
        elif obs.ok:
            self._auth_ok = True
        if self.telemetry_class == "BOSMINER_UNAVAILABLE":
            self._bosminer_available = False
        elif obs.ok:
            self._bosminer_available = True
        if obs.ok:
            self._api_reachable = True
            self._required_telemetry_fresh = True
            if self.power_w is not None:
                self._last_good_power_w = self.power_w
            if self.boards_str:
                self._last_good_boards = self.boards_str
        else:
            # A failed required read must not mark cooling-PUT freshness FRESH.
            self._required_telemetry_fresh = False
            self._api_reachable = self._last_http_code is not None
        self._critical_fault = bool(obs.ok and self._hard_fault(obs))
        if positive:
            self._transition_evidence = True
            self._transition_evidence_mono = self._now()
            if self._verification_ctx.get("evidence_deadline_mono"):
                self._verification_ctx["evidence_deadline_mono"] = (
                    self._transition_evidence_mono + self._evidence_window_s()
                )
            self._verification_ctx["last_valid_telemetry_mono"] = self._now()
        elif obs.ok:
            self._transition_evidence = False
        elif obs.ok is False and self._telemetry_last_success_mono:
            self._verification_ctx["last_valid_telemetry_mono"] = float(
                self._telemetry_last_success_mono
            )
        if self._recovery_sample_ok(obs):
            self._valid_poll_streak += 1
        else:
            self._valid_poll_streak = 0
        self.recovery_ready = self._valid_poll_streak >= RECOVERY_READY_POLLS
        self._settle_unavailable_applying(obs)
        if self.actual_mode in {"FAULT_LATCHED", "WAITING_FOR_BRAIINS"}:
            self.observed_state = self.actual_mode
        elif self._fault_latched:
            self.observed_state = "FAULT_LATCHED"
        else:
            self.observed_state = self.telemetry_class
        self._sync_mode_contract(obs)

    def observe_miner(self) -> MinerObservation:
        """Read boards + pause/mining state. Topology alone never confirms a live mode."""
        obs = MinerObservation()
        try:
            return self._observe_miner_body(obs)
        finally:
            self._finish_observation(obs)

    def _observe_miner_body(self, obs: MinerObservation) -> MinerObservation:
        try:
            ids, code, body = self.b.enabled_ids()
        except Exception as e:
            self.last_error = f"read_boards_exc:{e}"
            self._last_transport = str(e)
            self._last_http_code = None
            self._note_endpoint(
                "boards",
                ok=False,
                failure_class="API_UNREACHABLE",
                summary=str(e),
            )
            return obs
        if code != 200:
            self.last_error = f"read_boards_http_{code}"
            self._last_transport = _summarize_http_body(body)
            self._last_http_code = int(code) if code is not None else None
            self._note_endpoint(
                "boards",
                ok=False,
                failure_class=self._read_failure_class(code, body),
                summary=_summarize_http_body(body) or f"http_{code}",
            )
            return obs
        self._note_endpoint("boards", ok=True)
        obs.enabled_ids = [norm_board_id(i) for i in ids if norm_board_id(i)]
        obs.boards_ok = True
        self.boards_str = ",".join(obs.enabled_ids) if obs.enabled_ids else "none"

        try:
            parsed, dcode, dbody = self.b.mining_state()
        except Exception as e:
            self.last_error = f"read_details_exc:{e}"
            self._last_transport = str(e)
            self._last_http_code = None
            self._note_endpoint(
                "details",
                ok=False,
                failure_class="API_UNREACHABLE",
                summary=str(e),
            )
            return obs
        if dcode != 200:
            self.last_error = f"read_details_http_{dcode}"
            self._last_transport = _summarize_http_body(dbody)
            self._last_http_code = int(dcode) if dcode is not None else None
            self._note_endpoint(
                "details",
                ok=False,
                failure_class=self._read_failure_class(dcode, dbody),
                summary=_summarize_http_body(dbody) or f"http_{dcode}",
            )
            return obs
        self._note_endpoint("details", ok=True)
        if not isinstance(parsed, dict):
            parsed = parse_mining_state({})
        obs.details_ok = True
        obs.status_raw = parsed.get("status_raw")
        obs.phase = parsed.get("phase") or ""
        obs.user_paused = bool(parsed.get("user_paused"))
        obs.paused = bool(parsed.get("paused"))
        obs.running = bool(parsed.get("running"))
        obs.starting = bool(parsed.get("starting"))
        obs.preheating = bool(parsed.get("preheating"))
        obs.ramping = bool(parsed.get("ramping"))
        obs.pause_reason = str(parsed.get("pause_reason") or "")
        obs.bosminer_uptime_s = parsed.get("bosminer_uptime_s")
        obs.miner_ready = parsed.get("miner_ready")
        obs.not_started = bool(parsed.get("not_started"))
        obs.ok = True
        self._apply_live_board_health(obs)
        self.b.fail_count = 0
        self.miner_paused = self._is_paused(obs)
        self.mining_phase = obs.phase or ("paused" if obs.paused else "")

        try:
            obs.power_w = self.b.approx_power_w()
            self.power_w = obs.power_w
        except Exception as e:
            self.log(f"power_read_skip: {e}")
        try:
            obs.hashrate = self.b.approx_hashrate()
        except Exception as e:
            self.log(f"hashrate_read_skip: {e}")
        self._last_obs = obs
        return obs

    def _is_paused(self, obs: MinerObservation) -> bool:
        return bool(obs.user_paused or obs.paused)

    def _paused_confirmed(self, obs: MinerObservation) -> bool:
        if not obs.ok or obs.running:
            return False
        return self._is_paused(obs) or obs.phase == "stopped"

    def _in_resume_warmup(self) -> bool:
        if not self._resume_ts:
            return False
        return (self._now() - self._resume_ts) < WARMUP_S

    def _active_confirmed(self, mode: str, obs: MinerObservation) -> bool:
        """ONE/TWO/THREE_BOARD is confirmed only after boards + not paused + running."""
        if not obs.ok or mode not in BOARD_MAP or mode == "PAUSED":
            return False
        if not self._boards_match(obs.enabled_ids, BOARD_MAP[mode]):
            return False
        if self._is_paused(obs) or obs.user_paused:
            return False
        if not obs.running:
            return False
        # Optional check 5: after WARMUP_S, live watts/hashrate may add
        # confidence. Power=0 alone is never a failure — especially during
        # the immediate resume warmup window — and does not block confirm.
        return True

    def _needs_reconcile(self, desired: str, obs: MinerObservation) -> bool:
        if desired == "PAUSED":
            return not self._paused_confirmed(obs)
        return not self._active_confirmed(desired, obs)

    def infer_actual_mode(self, obs: MinerObservation) -> str:
        """Map observation → published actual. Never ONE/TWO/THREE while paused."""
        if self._cooling_transition_active:
            return "APPLYING"
        if not obs.ok:
            return self.actual_mode
        if self._paused_confirmed(obs):
            return "PAUSED"
        if (
            obs.starting
            or obs.preheating
            or obs.ramping
            or obs.phase in {"starting", "stopping"} | TRANSITIONAL_PHASES
        ):
            return "APPLYING"
        if obs.running:
            s = set(obs.enabled_ids)
            if s == {"1", "2", "3"}:
                return "THREE_BOARD"
            if s == {"1", "2"}:
                return "TWO_BOARD"
            if s == {"1"}:
                return "ONE_BOARD"
            return "ERROR"
        if self._is_paused(obs) or obs.phase == "stopped":
            return "PAUSED"
        return "APPLYING"

    def _mark_confirmed(self, mode: str) -> None:
        self.actual_mode = mode
        self.confirmed_operational = mode
        self.mode_entered_ts = self._wall()
        self.last_transition_ts = self._wall()
        self.last_error = ""

    def _mark_error(self, err: str) -> None:
        self.last_error = err
        self.actual_mode = "ERROR"
        self._health_class = "ERROR"
        self._cooling_phase = "ERROR"

    def _wait_until(self, predicate, fail_msg: str, timeout_s: float | None = None) -> bool:
        limit = self._board_wait_s() if timeout_s is None else int(timeout_s)
        deadline = self._now() + max(1, limit)
        while self._now() < deadline:
            self.health.touch()
            obs = self.observe_miner()
            if obs.ok and predicate(obs):
                return True
            self._sleep(BOARD_POLL_S)
        self.last_error = fail_msg
        return False

    def _stage_a_cleared(self, obs: MinerObservation) -> bool:
        """Resume HTTP accepted and miner no longer reports user_pause / paused."""
        if not obs.ok:
            return False
        return (not self._is_paused(obs)) and (not obs.user_paused)

    def _transitional_operational(self, obs: MinerObservation) -> bool:
        """running / preheat / ramping / starting — not mature hashrate."""
        if obs.running or obs.starting or obs.preheating or obs.ramping:
            return True
        phase = (obs.phase or "").strip().lower().replace("-", "_")
        return phase in TRANSITIONAL_PHASES

    def _resume_in_progress(self, desired: str, obs: MinerObservation) -> bool:
        """Stage C already met: stay APPLYING; do not re-issue resume or long-wait."""
        if desired not in BOARD_MAP or desired == "PAUSED":
            return False
        if not obs.ok or self._is_paused(obs) or obs.user_paused:
            return False
        if not self._boards_match(obs.enabled_ids, BOARD_MAP[desired]):
            return False
        if self._active_confirmed(desired, obs):
            return False
        return self._transitional_operational(obs)

    def _record_trend(self, obs: MinerObservation, hist: dict[str, list]) -> None:
        if obs.power_w is not None:
            hist["power"].append(float(obs.power_w))
        if obs.hashrate is not None:
            hist["hash"].append(float(obs.hashrate))

    def _trend_rising(self, hist: dict[str, list]) -> bool:
        return metric_trend_rising(hist.get("power") or []) or metric_trend_rising(hist.get("hash") or [])

    def _wait_stage_c(self, mode: str) -> bool:
        """Stage C: transitional operational phase AND power/hashrate begins rising."""
        deadline = self._now() + self._resume_wait_s()
        hist: dict[str, list] = {"power": [], "hash": []}
        while self._now() < deadline:
            self.health.touch()
            obs = self.observe_miner()
            if obs.ok:
                if self._active_confirmed(mode, obs):
                    return True
                if self._transitional_operational(obs) and not self._is_paused(obs):
                    self._record_trend(obs, hist)
                    if self._trend_rising(hist):
                        self.log(
                            f"resume stage C ok phase={obs.phase} "
                            f"power={obs.power_w} hashrate={obs.hashrate}"
                        )
                        return True
            self._sleep(BOARD_POLL_S)
        self.last_error = "resume_wait_timeout"
        return False

    def apply_mode(self, mode: str) -> bool:
        """Converge miner to desired mode. actual is APPLYING until confirmed.

        Cooling PUTs are owned here only while paused (maintenance). Never live
        mid-hash. Failures restore known-good cooling, pause, and ERROR — no
        legacy fan/auto handoff.
        """
        if not self.settings.enable_writes:
            self._log_write_blocked("apply_mode", "controller", "enable_writes_false")
            return False
        self.log(f"APPLY begin mode={mode}")
        self.actual_mode = "APPLYING"
        self._apply_depth += 1
        previous_cooling = self._cooling_applied
        cooling_changed = False
        abort = self._thermal_abort_needed()
        cooling_profile = self.desired_cooling_profile(mode, abort=abort)
        self._cooling_desired = cooling_profile
        try:
            with self._braiins_mutex:
                return self._apply_mode_locked(
                    mode, cooling_profile, previous_cooling, cooling_changed
                )
        except Exception as e:
            self._mark_error(f"apply_exc:{e}")
            self.log(f"APPLY exc {traceback.format_exc()}")
            return False
        finally:
            self._apply_depth = max(0, self._apply_depth - 1)
            self._cooling_transition_active = False
            if self._txn_owner == threading.get_ident():
                self._release_cooling_owner()
            else:
                self._cooling_txn_active = False

    def _apply_mode_locked(
        self,
        mode: str,
        cooling_profile: CoolingProfile,
        previous_cooling: CoolingProfile | None,
        cooling_changed: bool,
    ) -> bool:
        obs = self._observe_retrying()
        if not obs.ok:
            self._mark_error(self.last_error or "observe_failed")
            return False

        if mode == "PAUSED":
            if not self._paused_confirmed(obs):
                code, body = self._http_retry(
                    lambda: self._device_tuple("mode_pause", lambda: self.b.pause()),
                    "pause",
                )
                self.log(f"pause http={code}")
                if self._denied_body(body) or code == 0:
                    return False
                if code != 200:
                    self._mark_error(f"pause_http_{code}")
                    return False
                if not self._wait_until(self._paused_confirmed, "pause_wait_timeout"):
                    self._mark_error(self.last_error or "pause_wait_timeout")
                    return False
            if self._boards_already_satisfied("PAUSED", obs):
                self.log("pause boards already match, skip PATCH/wait")
            elif not self._ensure_boards(BOARD_MAP["PAUSED"]):
                self._mark_error(self.last_error or "board_pause_failed")
                return False
            obs = self._observe_retrying()
            if not self._paused_confirmed(obs):
                code, body = self._http_retry(
                    lambda: self._device_tuple("mode_pause", lambda: self.b.pause()),
                    "pause",
                )
                self.log(f"pause http={code}")
                if self._denied_body(body) or code == 0:
                    return False
                if code != 200:
                    self._mark_error(f"pause_http_{code}")
                    return False
                if not self._wait_until(self._paused_confirmed, "pause_wait_timeout"):
                    self._mark_error(self.last_error or "pause_wait_timeout")
                    return False
            # Mining pause above is the mode state machine. Cooling attach
            # (pause-for-cooling is already paused here, then PUT) stays off
            # unless cooling control is explicitly enabled.
            if not self._cooling_control_enabled():
                paused_cooling = False
                auto_cool = False
            else:
                paused_cooling = self._cooling_needed(cooling_profile, already_paused=True)
                auto_cool = self._legacy_auto_cool_armed()
            if paused_cooling and not auto_cool:
                self._defer_profile(cooling_profile, "auto_fan_ceiling_disabled", queue=False)
            elif paused_cooling:
                self._assign_txn_id()
                self._claim_cooling_owner()
                self._set_applying_cooling()
                if not self._ensure_paused_idle_for_cooling():
                    self._release_cooling_owner()
                    return False
                if not self._apply_cooling_while_paused(cooling_profile):
                    self._release_cooling_owner()
                    return False
                self._cooling_transition_active = False
                self._release_cooling_owner()
            self._mark_confirmed("PAUSED")
            return True

        if not self._ensure_power_target():
            self._mark_error(self.last_error or "power_target_failed")
            return False

        already_paused = self._paused_confirmed(obs) or self._is_paused(obs) or obs.user_paused
        # Board-count / power-target / resume below are the mining state
        # machine. Do not pause for a cooling PUT while Braiins owns cooling.
        if not self._cooling_control_enabled():
            cooling_needed = False
            auto_cool = False
        else:
            cooling_needed = self._cooling_needed(cooling_profile, already_paused=already_paused)
            auto_cool = self._legacy_auto_cool_armed()
        if cooling_needed and not auto_cool:
            self._defer_profile(cooling_profile, "auto_fan_ceiling_disabled", queue=False)
            cooling_needed = False
        if cooling_needed:
            self._assign_txn_id()
            self._claim_cooling_owner()
            self._set_applying_cooling()
            if not self._ensure_paused_idle_for_cooling():
                self._release_cooling_owner()
                return False
            if not self._apply_cooling_while_paused(cooling_profile):
                self._release_cooling_owner()
                return False
            cooling_changed = True
            self.actual_mode = "APPLYING"
            obs = self._observe_retrying()

        # PAUSED→ONE_BOARD shares ["1"]. If topology already matches (or
        # identical map + empty/partial read), skip PATCH and board_wait.
        if self._boards_already_satisfied(mode, obs):
            self.log(f"Stage B satisfied expect={BOARD_MAP[mode]}, skip PATCH/wait")
        elif not self._ensure_boards(BOARD_MAP[mode]):
            self._mark_error(self.last_error or "boards_failed")
            return False

        obs = self._observe_retrying()
        needs_resume = self._is_paused(obs) or obs.user_paused or obs.phase == "stopped"
        if needs_resume:
            if cooling_changed:
                ok = self._resume_after_cooling(previous_cooling, mode)
                self._release_cooling_owner()
                if self._auto_apply_pending_allowed():
                    pending = self._take_pending_if_terminal()
                    if pending is not None:
                        self._gated_cooling_transition(
                            pending,
                            mode,
                            _depth=1,
                            _apply_gen=self._cooling_terminal_gen,
                        )
                else:
                    self._note_pending_held(
                        self._health_class or self._cooling_terminal_kind or "not_success"
                    )
                return ok
            else:
                code, body = self._http_retry(
                    lambda: self._device_tuple("mode_resume", lambda: self.b.resume()),
                    "resume",
                )
                self.log(f"resume http={code}")
                if self._denied_body(body) or code == 0:
                    return False
                if code != 200:
                    self._mark_error(f"resume_http_{code}")
                    return False
                self._resume_ts = self._now()
            # Stage A: accepted resume and no longer user_pause / paused.
            if not self._wait_until(
                self._stage_a_cleared,
                "resume_wait_timeout",
                timeout_s=self._resume_wait_s(),
            ):
                if cooling_changed:
                    self._cooling_fail(self.last_error or "resume_wait_timeout", previous_cooling)
                else:
                    self._mark_error(self.last_error or "resume_wait_timeout")
                return False

        # Stage B: skip if already matched. PATCH 200 = accepted; poll topology.
        obs = self.observe_miner() if needs_resume else obs
        if self._boards_already_satisfied(mode, obs):
            self.log(f"Stage B still satisfied expect={BOARD_MAP[mode]}")
        elif not self._ensure_boards(BOARD_MAP[mode]):
            self._mark_error(self.last_error or "boards_failed")
            return False

        obs = self._observe_retrying()
        if self._active_confirmed(mode, obs):
            self._mark_confirmed(mode)
            return True

        # Stage C: running/preheat/ramping (or starting) AND watts/hashrate rising.
        # Do not require mature/full hashrate in this window.
        if needs_resume or not obs.running:
            if not self._wait_stage_c(mode):
                if cooling_changed:
                    self._cooling_fail(self.last_error or "resume_wait_timeout", previous_cooling)
                else:
                    self._mark_error(self.last_error or "resume_wait_timeout")
                return False

        obs = self.observe_miner()
        if self._active_confirmed(mode, obs):
            self._mark_confirmed(mode)
            return True
        # Resume is operationally successful; remain APPLYING until final confirm.
        self.actual_mode = "APPLYING"
        self.last_error = ""
        self.log(f"resume operational APPLYING until confirm mode={mode} phase={obs.phase}")
        return True

    def _status_dict(self, solar_avg: float, enable_on: bool) -> dict:
        return {
            "ts": self.log.now_local(),
            "last_seen": utc_iso(),
            "master_enable": enable_on,
            "enable_writes": self.settings.enable_writes,
            "writes_allowed": self.writes_allowed(enable_on),
            "mode_request": self.mode_request,
            "desired_mode": self.desired_mode,
            "requested_mode": self.desired_mode,
            "actual_mode": self.actual_mode,
            "observed_miner_mode": self.observed_miner_mode,
            "controller_state": self.controller_state,
            "observed_state": self.observed_state,
            "telemetry_class": self.telemetry_class,
            "recovery_ready": bool(self.recovery_ready),
            "writes_permitted": bool(self.write_permission.permitted),
            "confirmed_operational": self.confirmed_operational,
            "miner_paused": self.miner_paused,
            "mining_phase": self.mining_phase,
            "resume_warmup": self._in_resume_warmup(),
            "reason": self.reason,
            "last_error": self.last_error,
            "solar_avg_w": round(solar_avg, 1),
            "soc": self.fnum(ENT_SOC),
            "solar_available": self.fnum(ENT_SOLAR_AVAIL),
            "old_auto_enable": self.ha.state(ENT_OLD_AUTO),
            "power_target_w": self.settings.power_target_w,
            "power_w": self.power_w,
            "boards": self.boards_str,
            "acting": self.acting,
            "dry_reads": self.dry_reads,
            "api_fail_count": self.ha.fail_count + self.b.fail_count,
            "last_braiins_ok": self.b.last_ok_iso,
            "miner_url": self.settings.miner_url,
            "supervision": "s6-overlay + Supervisor watchdog",
            "fan_max_pct": None if self._cooling_applied is None else self._cooling_applied.max_fan_speed,
            "cooling_profile_desired": None
            if self._cooling_desired is None
            else f"{self._cooling_desired.name}:{self._cooling_desired.max_fan_speed}",
            "cooling_profile_applied": None
            if self._cooling_applied is None
            else f"{self._cooling_applied.name}:{self._cooling_applied.max_fan_speed}",
            "cooling_transition": self._cooling_transition_active,
            "cooling_dwell_s": int(self.settings.cooling_dwell_seconds or 0),
            "cooling_resume_settle_s": int(self.settings.cooling_resume_settle_seconds or 0),
            "chip_temp_f": self._chip_temp_f,
            "fan_rpm": self._fan_rpm,
            "fan_pct": self._fan_pct,
            "thermal_abort": self._thermal_abort_active,
            "cooling_control_enabled": bool(self.settings.cooling_control_enabled),
            **self._cooling_obs_fields(),
        }

    def _write_status_files(self, status: dict) -> None:
        payload = json.dumps(status, indent=2)
        paths = [
            self.settings.data_dir / "status.json",
            self.settings.share_dir / "lard_controller_status.json",
        ]
        for p in paths:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(payload)
            except Exception:
                continue

    def publish(self, solar_avg: float, enable_on: bool):
        self._sync_mode_contract(getattr(self, "_last_obs", None))
        if self._telemetry_freshness == "FRESH" and not self._telemetry_fault:
            self._sync_idle_health(getattr(self, "_last_obs", None))
        status = self._status_dict(solar_avg, enable_on)
        power_fresh = self._power_fresh()
        boards_fresh = self._boards_fresh()
        if power_fresh:
            power_state = self.power_w
        else:
            power_state = "unknown"
            status["power_w"] = None
        if boards_fresh:
            boards_state = self.boards_str or "none"
        else:
            boards_state = "unverified"
            status["boards"] = "unverified"
        status["last_power_w"] = self._last_good_power_w
        status["last_boards"] = self._last_good_boards
        status["power_fresh"] = power_fresh
        status["boards_fresh"] = boards_fresh
        self._write_status_files(status)
        self.health.set_status(status)

        last_seen = status["last_seen"]
        error = self.last_error or ""
        fails = status["api_fail_count"]
        hb = {
            "online": "on",
            "last_seen": last_seen,
            "requested_mode": self.desired_mode,
            "actual_mode": self.actual_mode,
            "error": error if error else "ok",
            "api_fail_count": fails,
            "last_braiins_ok": self.b.last_ok_iso or "",
            "power_w": power_state if power_fresh else "",
            "boards": boards_state,
        }
        mqtt_ok = self.mqtt.publish_heartbeat(hb)

        # REST entities are the guaranteed HA health path when MQTT is down.
        # Do not skip REST even if MQTT works — last_seen freshness is the
        # REST-safe watchdog signal if LWT never fires.
        attrs_base = {
            "friendly_name": "",
            "reason": self.reason,
            "master_enable": enable_on,
            "enable_writes": self.settings.enable_writes,
            "writes_allowed": self.writes_allowed(enable_on),
        }
        self.ha.set_state(
            ENT_CTRL_ONLINE,
            "on",
            {
                "friendly_name": "LARD Controller Online",
                "device_class": "connectivity",
                "reason": self.reason,
                "mqtt": mqtt_ok,
            },
        )
        self.ha.set_state(
            ENT_CTRL_LAST_SEEN,
            last_seen,
            {"friendly_name": "LARD Controller Last Seen", "device_class": "timestamp"},
        )
        self.ha.set_state(
            ENT_CTRL_REQUESTED,
            self.desired_mode,
            {**attrs_base, "friendly_name": "LARD Controller Requested Mode"},
        )
        cool = self._cooling_obs_fields()
        self.ha.set_state(
            ENT_CTRL_ACTUAL,
            self.actual_mode,
            {
                **attrs_base,
                "friendly_name": "LARD Controller Actual Mode",
                "health_class": cool["health_class"],
                "cooling_phase": cool["cooling_phase"],
                "cooling_txn_id": cool["cooling_txn_id"],
                "observed_miner_mode": self.observed_miner_mode,
                "controller_state": self.controller_state,
                "telemetry_class": self.telemetry_class,
                "health_classification": self.telemetry_class,
                "actual_mode_is_physical": self.actual_mode in OBSERVED_MINER_MODES,
                "requested_mode": self.desired_mode,
                "write_gate": self.write_gate,
                "api_reachable": bool(self._api_reachable),
                "bosminer_available": bool(self._bosminer_available),
                "recovery_ready": bool(self.recovery_ready),
                "fault_reason": self.last_error or "",
                "current_error": cool["current_error"],
                "active_fault": cool["active_fault"],
                "last_fault_reason": cool["last_fault_reason"],
                "last_fault_timestamp": cool["last_fault_timestamp"],
                "last_fault_class": cool["last_fault_class"],
                "last_fault_count": cool["last_fault_count"],
                "sustained_telemetry_recovery_polls": cool["sustained_telemetry_recovery_polls"],
                "sustained_telemetry_recovery_required": cool[
                    "sustained_telemetry_recovery_required"
                ],
            },
        )
        self.ha.set_state(
            ENT_CTRL_ERROR,
            error if error else "ok",
            {"friendly_name": "LARD Controller Error"},
        )
        self.ha.set_state(
            ENT_CTRL_FAILS,
            fails,
            {
                "friendly_name": "LARD Controller API Fail Count",
                "state_class": "measurement",
            },
        )
        self.ha.set_state(
            ENT_CTRL_BRAIINS_OK,
            self.b.last_ok_iso or "",
            {"friendly_name": "LARD Controller Last Braiins OK"},
        )
        self.ha.set_state(
            ENT_CTRL_POWER,
            power_state,
            {
                "friendly_name": "LARD Controller Power",
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
                "fresh": power_fresh,
                "last_power_w": self._last_good_power_w,
            },
        )
        self.ha.set_state(
            ENT_CTRL_BOARDS,
            boards_state,
            {
                "friendly_name": "LARD Controller Boards",
                "fresh": boards_fresh,
                "last_boards": self._last_good_boards,
            },
        )
        self.ha.set_state(
            ENT_CTRL_HEALTH,
            self._health_class,
            {
                "friendly_name": "LARD Controller Health",
                **cool,
            },
        )

        # Compatibility sensors from the uploaded controller
        self.ha.set_state(
            "sensor.lard_miner_mode_actual",
            self.actual_mode,
            {"friendly_name": "LARD Miner Mode Actual", "reason": self.reason, "last_error": self.last_error},
        )
        self.ha.set_state(
            "sensor.lard_miner_mode_desired",
            self.desired_mode,
            {"friendly_name": "LARD Miner Mode Desired", "reason": self.reason},
        )
        self.ha.set_state(
            "sensor.lard_miner_mode_reason",
            self.reason,
            {"friendly_name": "LARD Miner Mode Reason"},
        )
        self.ha.set_state(
            "sensor.lard_solar_avg_w",
            round(solar_avg, 1),
            {
                "friendly_name": "LARD Solar Avg W",
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
            },
        )
        self.ha.set_state(
            "sensor.lard_board_priority_status",
            "on" if enable_on else "idle",
            {
                "friendly_name": "LARD Board Priority Status",
                "desired_mode": self.desired_mode,
                "actual_mode": self.actual_mode,
                "reason": self.reason,
                "last_error": self.last_error,
                "solar_avg_w": round(solar_avg, 1),
                "master_enable": enable_on,
                "enable_writes": self.settings.enable_writes,
                "power_target_w": self.settings.power_target_w,
            },
        )

    def read_actual_from_miner(self):
        """Observe miner. Hashboard set {1} while user-paused is PAUSED, not ONE_BOARD.

        FAULT_LATCHED stays latched across later good reads (recovery readiness
        is advisory and does not clear it). WAITING_FOR_BRAIINS is not frozen:
        ``_finish_observation`` already applied the evidence deadline.
        """
        obs = self.observe_miner()
        if self._fault_latched or self.actual_mode == "FAULT_LATCHED":
            self._sync_mode_contract(obs)
            return obs
        if self._cooling_transition_active:
            self.actual_mode = "APPLYING"
            self._sync_mode_contract(obs)
            return obs
        if self.actual_mode == "WAITING_FOR_BRAIINS" and not obs.ok:
            self._sync_mode_contract(obs)
            return obs
        mode = self.infer_actual_mode(obs)
        self.actual_mode = mode
        if mode in RANK:
            self.confirmed_operational = mode
        self._sync_mode_contract(obs)
        return obs

    def tick(self):
        if not self.settings.enable_writes:
            self._drop_stale_write_queue("enable_writes_false")
        self.health.touch()
        enable_on = self.ha.state(ENT_ENABLE) == "on"
        solar_avg = self.update_solar_avg()
        desired, reason = self.compute_desired(solar_avg)
        self.desired_mode = desired
        self.reason = reason
        self.dry_reads += 1

        old_auto = self.ha.state(ENT_OLD_AUTO)
        if old_auto == "on":
            self.log("WARNING competing writer: switch.solar_miner_auto_enable is ON — will not turn it on; writes refused")

        self._refresh_cooling_telemetry()
        # Fan-max helper is legacy. Track it, but native policy does not schedule on it.
        # While Braiins owns cooling, helper bumps are ignored (not queued).
        helper_changed = self._note_fan_max_helper()
        if not self._cooling_control_enabled():
            helper_changed = False
            self._fan_max_seen = self.read_fan_max_pct()
        abort = self._thermal_abort_needed()
        cooling_mode = desired if desired in RANK else self._settled_mode()
        desired_cooling = self.desired_cooling_profile(cooling_mode, abort=abort)
        self._cooling_desired = desired_cooling
        native = self._native_temperature_policy()

        if not self.writes_allowed(enable_on):
            self.acting = False
            if native:
                self._absorb_temperature_policy_while_disarmed()
            if self.settings.enable_writes and not enable_on:
                self.reason = f"{reason}|master_gate_off"
            elif not self.settings.enable_writes:
                self.reason = f"{reason}|enable_writes_false"
            obs = None
            try:
                obs = self.read_actual_from_miner()
            except Exception as e:
                self.log(f"observe miner failed: {e}")
                obs = None
            self._note_tick_observation(obs, "observe_only")
            self.publish(solar_avg, enable_on)
            return

        if old_auto == "on":
            self.acting = False
            self.last_error = "refusing_writes_old_auto_enable_is_on"
            if native:
                self._absorb_temperature_policy_while_disarmed()
            self.publish(solar_avg, enable_on)
            return

        if self.competing_writer_blocks_arming():
            self.acting = False
            self.last_error = "refusing_writes_competing_writer"
            self.reason = f"{reason}|competing_writer"
            self._log_write_blocked("arm", "tick", "competing_writer")
            if native:
                self._absorb_temperature_policy_while_disarmed()
            obs = None
            try:
                obs = self.read_actual_from_miner()
            except Exception as e:
                self.log(f"observe miner failed: {e}")
                obs = None
            self._note_tick_observation(obs, "competing_writer")
            self.publish(solar_avg, enable_on)
            return

        if (
            self._cooling_txn_active
            or self._reload_hold
            or self._health_class == "INTERRUPTED_MANUAL_REVIEW"
        ):
            self.acting = False
            if self._reload_hold or self._health_class == "INTERRUPTED_MANUAL_REVIEW":
                self.reason = f"{reason}|interrupted_manual_review"
            else:
                self.reason = f"{reason}|txn_busy"
            self.publish(solar_avg, enable_on)
            return

        self.acting = True
        try:
            obs = self.observe_miner()
        except Exception as e:
            self.log(f"observe miner failed: {e}")
            obs = MinerObservation()

        self._miner_prev_ok = bool(obs.ok)
        # A missed read is UNKNOWN/STALE until the consecutive-failure threshold.
        # It must not ERROR on the first miss or issue a corrective command.
        self._note_tick_observation(obs, "tick")
        if not obs.ok:
            self.publish(solar_avg, True)
            return

        if self._health_class == "ERROR" or self._cooling_terminal_kind == "error":
            if self._pending_profile is not None:
                self._note_pending_held("ERROR")
            self.reason = f"{reason}|error_hold"
            self.publish(solar_avg, True)
            return

        if (
            self._health_class == "DEGRADED_NEEDS_ATTENTION"
            and desired != "PAUSED"
            and not abort
        ):
            if self._pending_profile is not None:
                self._note_pending_held("DEGRADED_NEEDS_ATTENTION")
            self.reason = f"{reason}|degraded_needs_attention"
            self.publish(solar_avg, True)
            return

        if (
            self._cooling_control_enabled()
            and self._pending_explicit
            and self._pending_profile is not None
            and not self._cooling_txn_active
            and self._cooling_refuse_reason(obs) is None
        ):
            blocked = self._health_class in {"DEGRADED_NEEDS_ATTENTION", "ERROR"} or (
                self._cooling_terminal_kind in {"degraded", "error"}
            )
            if blocked or self._cooling_txn_active:
                self._note_pending_held(self._health_class or self._cooling_terminal_kind)
            else:
                profile = self._pending_profile
                gen = self._cooling_terminal_gen
                self._pending_profile = None
                self._pending_explicit = False
                if (
                    gen != self._cooling_terminal_gen
                    or self._health_class in {"DEGRADED_NEEDS_ATTENTION", "ERROR"}
                    or self._cooling_terminal_kind in {"degraded", "error"}
                ):
                    self._pending_profile = profile
                    self._pending_explicit = True
                    self._note_pending_held("stale_callback")
                else:
                    resume_mode = desired if desired in RANK else self._settled_mode()
                    self._gated_cooling_transition(profile, resume_mode)
                    self.publish(solar_avg, True)
                    return

        if native and not self._cooling_control_enabled():
            # Ignore target/hot/dangerous helper edits. Absorb so turning
            # cooling control on later does not replay an ignored bump.
            self._absorb_temperature_policy_while_disarmed()
        policy_changed = (
            self._note_temperature_policy()
            if native and self._cooling_control_enabled()
            else False
        )
        if (
            self._cooling_control_enabled()
            and native
            and policy_changed
            and not abort
            and self._needs_reconcile(desired, obs)
        ):
            # Mining-mode pause stays a mode transition. Queue the setpoint
            # for the next converged tick instead of attaching it to every
            # board-count change.
            self._defer_profile(
                desired_cooling, "mode_reconcile_before_policy", queue=True, explicit=True
            )
            self._mark_temperature_policy_seen()
            policy_changed = False

        if not self._needs_reconcile(desired, obs):
            if desired == "PAUSED" and self._paused_confirmed(obs):
                self.actual_mode = "PAUSED"
                self.confirmed_operational = "PAUSED"
            elif self._active_confirmed(desired, obs):
                self.actual_mode = desired
                self.confirmed_operational = desired
            if self._cooling_should_transition(
                desired_cooling,
                abort=abort,
                helper_changed=False if native else helper_changed,
                policy_changed=policy_changed,
            ):
                # Legacy ceiling chasing stays behind auto_fan_ceiling_enabled.
                # Native target writes are operator changes, not that flag.
                legacy_blocked = (not native) and (
                    not self.settings.auto_fan_ceiling_enabled
                ) and (not abort)
                if legacy_blocked:
                    self._defer_profile(
                        desired_cooling, "auto_fan_ceiling_disabled", queue=False
                    )
                    self.publish(solar_avg, True)
                    return
                if native and policy_changed:
                    self._mark_temperature_policy_seen()
                self.actual_mode = "APPLYING"
                self.reason = f"{reason}|cooling_applying"
                self.publish(solar_avg, True)
                ok = self._gated_cooling_transition(desired_cooling, desired)
                if not ok:
                    self.log(f"COOLING failed err={self.last_error}")
                try:
                    self.observe_miner()
                except Exception as e:
                    self.log(f"post_cooling_read: {e}")
                self.publish(solar_avg, True)
                return
            self.publish(solar_avg, True)
            return

        if self._resume_in_progress(desired, obs):
            self.actual_mode = "APPLYING"
            self.last_error = ""
            self.reason = f"{reason}|applying"
            self.publish(solar_avg, True)
            return

        self.actual_mode = "APPLYING"
        self.reason = f"{reason}|applying"
        self.publish(solar_avg, True)
        ok = self.apply_mode(desired)
        if not ok:
            self.log(f"APPLY failed err={self.last_error}")
        try:
            self.observe_miner()
        except Exception as e:
            self.log(f"post_apply_read: {e}")
        self.publish(solar_avg, True)


def main() -> int:
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    log = Logger(settings)
    log("controller starting (Supervisor add-on foreground)")
    log(
        f"miner={settings.miner_url} poll={settings.poll_seconds}s "
        f"board_wait>={settings.board_wait_seconds}s resume_wait={RESUME_WAIT_S}s "
        f"enable_writes={settings.enable_writes} "
        f"writes_permitted=false "
        f"power_target={settings.power_target_w}W "
        f"version={ADDON_VERSION} chip_abort_f={CHIP_ABORT_F} "
        f"cooling_dwell={settings.cooling_dwell_seconds}s "
        f"cooling_settle={settings.cooling_settle_seconds}s "
        f"cooling_resume_settle={settings.cooling_resume_settle_seconds}s "
        f"auto_fan_ceiling={settings.auto_fan_ceiling_enabled} "
        f"cooling_control={settings.cooling_control_enabled} "
        f"cooling_policy={settings.cooling_policy} "
        f"helper_unit=F option_unit=C "
        f"target_c={settings.cooling_target_temperature_c}/"
        f"hot_c={settings.cooling_hot_temperature_c}/"
        f"dangerous_c={settings.cooling_dangerous_temperature_c} "
        f"envelope={settings.cooling_envelope_min_fan_pct}-"
        f"{settings.cooling_envelope_max_fan_pct} "
        f"expected_recovery={settings.expected_recovery_seconds}s "
        f"max_recovery={settings.maximum_recovery_seconds}s "
        f"legacy_fan_profiles=ONE:{settings.cooling_one_board_max_fan_pct}/"
        f"TWO:{settings.cooling_two_board_max_fan_pct}/"
        f"THREE:{settings.cooling_three_board_max_fan_pct}/"
        f"PAUSED:{settings.cooling_paused_max_fan_pct} (legacy, TBD/measured)"
    )
    if settings.enable_writes:
        log("WRITES ARMED — still requires input_boolean.lard_board_priority_enable=on")
    else:
        log("WRITES DISARMED — observe-only until add-on option enable_writes is true")
    if settings.cooling_control_enabled:
        log(
            "COOLING CONTROL ARMED — explicit opt-in; still pause-first and "
            "still requires enable_writes plus the HA master gate"
        )
    else:
        log(
            "COOLING CONTROL OFF — Braiins owns cooling; "
            "no PUT /api/v1/cooling/mode, no pause-for-cooling, "
            "no thermal-abort fan write. Hashboard pause/resume stays."
        )

    health = HealthState(settings)
    try:
        start_health_server(health, settings.health_port, log)
    except OSError as e:
        log(f"FATAL health bind 0.0.0.0:{settings.health_port}: {e}")
        return 1

    token = ha_token(settings)
    ha = HA(ha_bases(settings), token, log)
    braiins = Braiins(settings, log)
    ctrl = Controller(ha, braiins, settings, log, health)
    ctrl.reconcile_after_reload()
    if token:
        ctrl.mqtt.discover_broker(token)
    ctrl.mqtt.start()

    try:
        ctrl.ensure_fan_max_helper()
    except Exception as e:
        log(f"fan_max helper ensure skip: {e}")
    try:
        ctrl.ensure_cooling_target_helpers()
    except Exception as e:
        log(f"cooling target helper ensure skip: {e}")

    try:
        ctrl.read_actual_from_miner()
    except Exception as e:
        log(f"initial miner read failed (ok if offline / no password): {e}")

    try:
        ctrl._refresh_cooling_telemetry()
    except Exception as e:
        log(f"cooling telemetry startup skip: {e}")

    log("controller loop enter — both gates off means observe-only")
    while True:
        try:
            ctrl.tick()
        except Exception as e:
            log(f"tick_err {e}\n{traceback.format_exc()}")
            ctrl.last_error = str(e)[:200]
            health.touch()
            try:
                ctrl.publish(0.0, False)
            except Exception:
                pass
        time.sleep(max(1, int(settings.poll_seconds)))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
