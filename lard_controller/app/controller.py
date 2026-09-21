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
# Optional per-mode envelope helpers. Missing → add-on options (TBD/measured).
ENT_COOLING_ONE_MAX = "input_number.lard_cooling_one_board_max_pct"
ENT_COOLING_TWO_MAX = "input_number.lard_cooling_two_board_max_pct"
ENT_COOLING_THREE_MAX = "input_number.lard_cooling_three_board_max_pct"
ENT_COOLING_PAUSED_MAX = "input_number.lard_cooling_paused_max_pct"

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
ACTUAL_MODES = ("PAUSED", "APPLYING", "ONE_BOARD", "TWO_BOARD", "THREE_BOARD", "ERROR")
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
ADDON_VERSION = "0.1.5"

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

BRAIINS_DENY_PATHS = (
    "/reboot",
    "/restart",
    "/factory",
    "/reset",
    "/system/reboot",
    "/actions/reboot",
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
    cooling_one_board_max_fan_pct: int = COOLING_ONE_BOARD_MAX_PCT
    cooling_two_board_max_fan_pct: int = COOLING_TWO_BOARD_MAX_PCT
    cooling_three_board_max_fan_pct: int = COOLING_THREE_BOARD_MAX_PCT
    cooling_paused_max_fan_pct: int = COOLING_PAUSED_MAX_PCT
    cooling_one_board_min_fan_pct: int = 0
    cooling_two_board_min_fan_pct: int = 0
    cooling_three_board_min_fan_pct: int = 0
    cooling_paused_min_fan_pct: int = 0

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


# ---------------------------------------------------------------------------
# Home Assistant client
# ---------------------------------------------------------------------------
class HA:
    def __init__(self, bases: list[str], token: str, log: Logger):
        self.bases = [b.rstrip("/") for b in bases if b]
        self.token = token
        self.log = log
        self.fail_count = 0

    def _req(self, method: str, path: str, body=None, timeout=30):
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
        self.fail_count += 1
        raise RuntimeError(f"HA {method} {path} failed: {last}")

    def state(self, entity_id: str):
        try:
            code, data = self._req("GET", f"/api/states/{entity_id}")
            if code == 200 and isinstance(data, dict):
                return data.get("state")
        except Exception as e:
            self.log(f"ha_state_err {entity_id}: {e}")
            self.fail_count += 1
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
    """Measured-safe auto envelope. Values are configurable placeholders, not finals."""

    name: str
    max_fan_speed: int
    min_fan_speed: int | None = None
    minimum_required_fans: int | None = None

    def matches(self, other: CoolingProfile | None) -> bool:
        if other is None:
            return False
        return self.max_fan_speed == other.max_fan_speed and (self.min_fan_speed or 0) == (
            other.min_fan_speed or 0
        )

    def extra_auto(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if self.min_fan_speed:
            extra["min_fan_speed"] = int(self.min_fan_speed)
        if self.minimum_required_fans is not None:
            extra["minimum_required_fans"] = int(self.minimum_required_fans)
        elif self.max_fan_speed >= FAN_MAX_DEFAULT:
            extra["minimum_required_fans"] = MIN_REQUIRED_FANS
        return extra


# ---------------------------------------------------------------------------
# Braiins OS+ REST (API ~1.8.0)
# ---------------------------------------------------------------------------
class Braiins:
    def __init__(self, settings: Settings, log: Logger):
        self.settings = settings
        self.log = log
        self.token = None
        self.token_ts = 0.0
        self.fail_count = 0
        self.last_ok_iso = ""
        self._io_lock = threading.RLock()

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
        lowered = path.lower()
        if any(deny in lowered for deny in BRAIINS_DENY_PATHS):
            raise RuntimeError(f"refused Braiins path {path} (reboot/reset denied)")
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
        return self._call("PUT", "/api/v1/actions/pause")

    def resume(self):
        return self._call("PUT", "/api/v1/actions/resume")

    def set_power(self, watt: int):
        return self._call("PUT", "/api/v1/performance/power-target", {"watt": int(watt)})

    def get_power_target(self):
        return self._call("GET", "/api/v1/performance/power-target")

    def patch_boards(self, enable: bool, ids: list[str]):
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
        """PUT /api/v1/cooling/mode tagged union. Integer percent 0–100, not a 0.6 ratio."""
        n = clamp_fan_max_pct(max_fan_speed)
        auto: dict[str, Any] = dict(extra_auto or {})
        auto["max_fan_speed"] = n
        if n >= FAN_MAX_DEFAULT:
            auto.setdefault("minimum_required_fans", MIN_REQUIRED_FANS)
        return self._call("PUT", "/api/v1/cooling/mode", {"auto": auto})

    def get_cooling_state(self):
        """GET /api/v1/cooling/state — fans rpm/target_speed_ratio + highest temp. Not /mode (405)."""
        return self._call("GET", "/api/v1/cooling/state")

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


def celsius_to_fahrenheit(c) -> float:
    return float(c) * 9.0 / 5.0 + 32.0


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


def parse_mining_state(details) -> dict[str, Any]:
    """Parse pause / mining phase from GET /api/v1/miner/details JSON.

    Handles legacy `status` (MINER_STATUS_PAUSED / NORMAL or REST ints/names)
    and `detailed_status` oneof (stopped.user_pause / running / starting).
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
    user_paused = _has_named_key(details, ("user_pause", "userPause")) or status_paused

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

    return {
        "status_raw": status_raw,
        "phase": phase,
        "user_paused": bool(user_paused and not running),
        "paused": bool(paused),
        "running": bool(running),
        "starting": bool(starting),
        "preheating": bool(preheating),
        "ramping": bool(ramping),
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
        self._miner_prev_ok = False
        self._fan_max_missing_logged = False
        self._chip_temp_f = None
        self._fan_rpm = None
        self._fan_pct = None
        self._thermal_abort_active = False
        self._live_max_fan_speed = None
        self._live_min_fan_speed = None

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
        now = time.time()
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
            code, _ = self._http_retry(lambda: self.b.patch_boards(False, to_disable), "PATCH disable")
            self.log(f"PATCH disable {to_disable} http={code}")
            if code != 200:
                self.last_error = f"board_disable_http_{code}"
                return False
        if to_enable:
            code, _ = self._http_retry(lambda: self.b.patch_boards(True, to_enable), "PATCH enable")
            self.log(f"PATCH enable {to_enable} http={code}")
            if code != 200:
                self.last_error = f"board_enable_http_{code}"
                return False

        # HTTP 200 on hashboard PATCH = accepted, not applied.
        # Poll GET; empty/partial/5xx are transient. Compare with normalized ids.
        expect = sorted(to_enable)
        deadline = self._now() + self._board_wait_s()
        empty_attempt = 0
        while self._now() < deadline:
            self.health.touch()
            try:
                actual, code, _ = self.b.enabled_ids()
            except Exception as e:
                self.log(f"board_poll exc: {e}")
                actual, code = [], 0
            self.log(f"board_poll expect={expect} actual={actual} http={code}")
            if code == 200 and self._boards_match(actual, expect):
                self.last_board_change_ts = self._now()
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
        self.last_error = f"board_wait_timeout expect={expect}"
        return False

    def _ensure_power_target(self) -> bool:
        try:
            code, payload = self.b.get_power_target()
            current = _find_number(payload, ("watt", "wattage", "power")) if isinstance(payload, dict) else None
            if code == 200 and current is not None and int(round(current)) == int(self.settings.power_target_w):
                return True
        except Exception as e:
            self.log(f"power_target_read_skip: {e}")
        code, _ = self._http_retry(
            lambda: self.b.set_power(self.settings.power_target_w),
            "power_target",
        )
        self.log(f"power_target http={code}")
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

    def _install_fan_max_package(self) -> None:
        """Copy shipped YAML packages into /config/packages when that dir already exists."""
        dest_dir = Path("/config/packages")
        if not dest_dir.is_dir():
            return
        for name in ("lard_fan_max.yaml", "lard_cooling_profiles.yaml"):
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
            self.log(f"cooling_state skip: {e}")
            return
        if code != 200:
            self.log(f"cooling_state http={code} body={_summarize_http_body(state)}")
            return
        tel = parse_cooling_telemetry(state if isinstance(state, dict) else {})
        self._chip_temp_f = tel.get("chip_temp_f")
        self._fan_rpm = tel.get("fan_rpm")
        self._fan_pct = tel.get("fan_pct")
        self._live_max_fan_speed = tel.get("max_fan_speed")
        self._live_min_fan_speed = tel.get("min_fan_speed")

    def _thermal_abort_needed(self) -> bool:
        if thermal_fault_name(self.ha.state(ENT_FAULT)):
            return True
        if self._chip_temp_f is not None and float(self._chip_temp_f) >= CHIP_ABORT_F:
            return True
        return False

    def _unconstrained_cooling(self, name: str = "ABORT") -> CoolingProfile:
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

    def desired_cooling_profile(self, mode: str, *, abort: bool = False) -> CoolingProfile:
        """Resolve one cooling envelope for the major board-count / pause state."""
        if abort:
            self._thermal_abort_active = True
            return self._unconstrained_cooling("ABORT")
        self._thermal_abort_active = False
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
    ) -> bool:
        """Cooling-only transition while the operating mode is already confirmed."""
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
            code, body = self.b.set_cooling_auto(target.max_fan_speed, extra or None)
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
            code, _ = self.b.pause()
            self.log(f"cooling fail-safe pause http={code}")
        except Exception as e:
            self.log(f"cooling fail-safe pause: {e}")

    def _cooling_fail(self, err: str, previous: CoolingProfile | None) -> None:
        """Pause first, restore known-good if possible, stay paused, ERROR.

        Never hand off to legacy writers. Restore is still a cooling PUT, so
        it must not run live while hashing — pause/idle before rewrite.
        """
        self.log(f"cooling fail err={err} — pause + restore + ERROR (no legacy handoff)")
        self._pause_safely()
        try:
            self._wait_until(self._cooling_paused_idle, "cooling_fail_idle_wait", timeout_s=30)
        except Exception:
            pass
        self._restore_known_good_cooling(previous)
        self._pause_safely()
        self.last_error = err
        self.actual_mode = "ERROR"
        self._cooling_transition_active = False

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
        return True

    def _ensure_paused_idle_for_cooling(self) -> bool:
        """Pause mining and verify user_pause + ~0 W before any cooling PUT."""
        obs = self._observe_retrying()
        if self._cooling_paused_idle(obs):
            self.last_error = ""
            self.actual_mode = "APPLYING"
            return True
        code, _ = self._http_retry(self.b.pause, "cooling_pause")
        self.log(f"cooling pause http={code}")
        if code != 200:
            self.last_error = f"cooling_pause_http_{code}"
            return False
        if not self._wait_until(self._cooling_paused_idle, "cooling_pause_wait"):
            return False
        # Intentional paused period is not ERROR.
        self.last_error = ""
        self.actual_mode = "APPLYING"
        return True

    def _apply_cooling_while_paused(self, profile: CoolingProfile) -> bool:
        """PUT tagged auto envelope, confirm it stuck. Caller must already be paused/idle."""
        previous = self._cooling_applied
        extra = profile.extra_auto()
        try:
            code, body = self._http_retry(
                lambda: self.b.set_cooling_auto(profile.max_fan_speed, extra or None),
                "cooling_put",
            )
        except Exception as e:
            self._cooling_fail(f"cooling_put_exc:{e}", previous)
            return False
        self.log(
            f"cooling PUT profile={profile.name} max={profile.max_fan_speed} "
            f"min={profile.min_fan_speed} http={code} body={_summarize_http_body(body)} "
            f"chip_temp_f={self._chip_temp_f} power_w={self.power_w}"
        )
        if code != 200:
            self._cooling_fail(f"cooling_put_http_{code}", previous)
            return False
        if not self._confirm_cooling(profile):
            self._cooling_fail(self.last_error or "cooling_confirm_failed", previous)
            return False
        stabilize = float(self.settings.cooling_stabilize_seconds or 0)
        if stabilize > 0:
            self._sleep(stabilize)
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

    def _gated_cooling_transition(self, profile: CoolingProfile, resume_mode: str) -> bool:
        """Full maintenance cooling sequence. Stays APPLYING until the operating mode is confirmed."""
        previous = self._cooling_applied
        with self._braiins_mutex:
            self._set_applying_cooling()
            self.log(
                f"COOLING begin profile={profile.name} max={profile.max_fan_speed} "
                f"resume_mode={resume_mode}"
            )
            try:
                if not self._ensure_paused_idle_for_cooling():
                    self._cooling_fail(self.last_error or "cooling_pause_wait", previous)
                    return False
                if not self._apply_cooling_while_paused(profile):
                    return False
                if resume_mode == "PAUSED":
                    self._cooling_transition_active = False
                    self._mark_confirmed("PAUSED")
                    return True
                code, _ = self._http_retry(self.b.resume, "cooling_resume")
                self.log(f"cooling resume http={code}")
                if code != 200:
                    self._cooling_fail(f"cooling_resume_http_{code}", previous)
                    return False
                self._resume_ts = self._now()
                if not self._wait_until(
                    self._stage_a_cleared,
                    "cooling_resume_wait",
                    timeout_s=self._resume_wait_s(),
                ):
                    self._cooling_fail(self.last_error or "cooling_resume_wait", previous)
                    return False
                if not self._wait_cooling_resume_verify(resume_mode, profile):
                    self._cooling_fail(self.last_error or "cooling_resume_verify", previous)
                    return False
                self._cooling_transition_active = False
                self.last_error = ""
                if resume_mode in RANK:
                    self._mark_confirmed(resume_mode)
                else:
                    self.actual_mode = resume_mode
                return True
            except Exception as e:
                self._cooling_fail(f"cooling_exc:{e}", previous)
                self.log(f"COOLING exc {traceback.format_exc()}")
                return False
            finally:
                self._cooling_transition_active = False

    def observe_miner(self) -> MinerObservation:
        """Read boards + pause/mining state. Topology alone never confirms a live mode."""
        obs = MinerObservation()
        try:
            ids, code, _ = self.b.enabled_ids()
        except Exception as e:
            self.last_error = f"read_boards_exc:{e}"
            return obs
        if code != 200:
            self.last_error = f"read_boards_http_{code}"
            return obs
        obs.enabled_ids = [norm_board_id(i) for i in ids if norm_board_id(i)]
        obs.boards_ok = True
        self.boards_str = ",".join(obs.enabled_ids) if obs.enabled_ids else "none"

        try:
            parsed, dcode, _ = self.b.mining_state()
        except Exception as e:
            self.last_error = f"read_details_exc:{e}"
            return obs
        if dcode != 200:
            self.last_error = f"read_details_http_{dcode}"
            return obs
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
        obs.ok = True
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
        self.mode_entered_ts = self._now()
        self.last_transition_ts = self._now()
        self.last_error = ""

    def _mark_error(self, err: str) -> None:
        self.last_error = err
        self.actual_mode = "ERROR"

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
        self.log(f"APPLY begin mode={mode}")
        self.actual_mode = "APPLYING"
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
            self._cooling_transition_active = False

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
                code, _ = self._http_retry(self.b.pause, "pause")
                self.log(f"pause http={code}")
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
                code, _ = self._http_retry(self.b.pause, "pause")
                self.log(f"pause http={code}")
                if code != 200:
                    self._mark_error(f"pause_http_{code}")
                    return False
                if not self._wait_until(self._paused_confirmed, "pause_wait_timeout"):
                    self._mark_error(self.last_error or "pause_wait_timeout")
                    return False
            if self._cooling_needed(cooling_profile, already_paused=True):
                self._set_applying_cooling()
                if not self._ensure_paused_idle_for_cooling():
                    self._cooling_fail(self.last_error or "cooling_pause_wait", previous_cooling)
                    return False
                if not self._apply_cooling_while_paused(cooling_profile):
                    return False
                self._cooling_transition_active = False
            self._mark_confirmed("PAUSED")
            return True

        if not self._ensure_power_target():
            self._mark_error(self.last_error or "power_target_failed")
            return False

        already_paused = self._paused_confirmed(obs) or self._is_paused(obs) or obs.user_paused
        cooling_needed = self._cooling_needed(cooling_profile, already_paused=already_paused)
        if cooling_needed:
            self._set_applying_cooling()
            if not self._ensure_paused_idle_for_cooling():
                self._cooling_fail(self.last_error or "cooling_pause_wait", previous_cooling)
                return False
            if not self._apply_cooling_while_paused(cooling_profile):
                return False
            cooling_changed = True
            self._cooling_transition_active = False
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
            code, _ = self._http_retry(self.b.resume, "resume")
            self.log(f"resume http={code}")
            if code != 200:
                if cooling_changed:
                    self._cooling_fail(f"resume_http_{code}", previous_cooling)
                else:
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
            "actual_mode": self.actual_mode,
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
            "chip_temp_f": self._chip_temp_f,
            "fan_rpm": self._fan_rpm,
            "fan_pct": self._fan_pct,
            "thermal_abort": self._thermal_abort_active,
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
        status = self._status_dict(solar_avg, enable_on)
        self._write_status_files(status)
        self.health.set_status(status)

        last_seen = status["last_seen"]
        error = self.last_error or ""
        fails = status["api_fail_count"]
        boards = self.boards_str or "unknown"
        power = "" if self.power_w is None else self.power_w
        hb = {
            "online": "on",
            "last_seen": last_seen,
            "requested_mode": self.desired_mode,
            "actual_mode": self.actual_mode,
            "error": error if error else "ok",
            "api_fail_count": fails,
            "last_braiins_ok": self.b.last_ok_iso or "",
            "power_w": power if power != "" else "",
            "boards": boards,
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
        self.ha.set_state(
            ENT_CTRL_ACTUAL,
            self.actual_mode,
            {**attrs_base, "friendly_name": "LARD Controller Actual Mode"},
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
            power if power != "" else "unknown",
            {
                "friendly_name": "LARD Controller Power",
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
            },
        )
        self.ha.set_state(
            ENT_CTRL_BOARDS,
            boards,
            {"friendly_name": "LARD Controller Boards"},
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
        """Observe miner. Hashboard set {1} while user-paused is PAUSED, not ONE_BOARD."""
        obs = self.observe_miner()
        if self._cooling_transition_active:
            self.actual_mode = "APPLYING"
            return obs
        mode = self.infer_actual_mode(obs)
        self.actual_mode = mode
        if mode in RANK:
            self.confirmed_operational = mode
        return obs

    def tick(self):
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
        helper_changed = self._note_fan_max_helper()
        abort = self._thermal_abort_needed()
        cooling_mode = desired if desired in RANK else self._settled_mode()
        desired_cooling = self.desired_cooling_profile(cooling_mode, abort=abort)
        self._cooling_desired = desired_cooling

        if not self.writes_allowed(enable_on):
            self.acting = False
            if self.settings.enable_writes and not enable_on:
                self.reason = f"{reason}|master_gate_off"
            elif not self.settings.enable_writes:
                self.reason = f"{reason}|enable_writes_false"
            try:
                self.read_actual_from_miner()
            except Exception as e:
                self.log(f"observe miner failed: {e}")
            self.publish(solar_avg, enable_on)
            return

        if old_auto == "on":
            self.acting = False
            self.last_error = "refusing_writes_old_auto_enable_is_on"
            self.publish(solar_avg, enable_on)
            return

        self.acting = True
        try:
            obs = self.observe_miner()
        except Exception as e:
            self.log(f"observe miner failed: {e}")
            obs = MinerObservation()

        self._miner_prev_ok = bool(obs.ok)

        if not obs.ok:
            # Do not infer a live board mode from topology when pause state is unknown.
            self.publish(solar_avg, True)
            return

        if not self._needs_reconcile(desired, obs):
            if desired == "PAUSED" and self._paused_confirmed(obs):
                self.actual_mode = "PAUSED"
                self.confirmed_operational = "PAUSED"
            elif self._active_confirmed(desired, obs):
                self.actual_mode = desired
                self.confirmed_operational = desired
            if self._cooling_should_transition(
                desired_cooling, abort=abort, helper_changed=helper_changed
            ):
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
        f"power_target={settings.power_target_w}W "
        f"version={ADDON_VERSION} chip_abort_f={CHIP_ABORT_F} "
        f"cooling_dwell={settings.cooling_dwell_seconds}s "
        f"cooling_profiles=ONE:{settings.cooling_one_board_max_fan_pct}/"
        f"TWO:{settings.cooling_two_board_max_fan_pct}/"
        f"THREE:{settings.cooling_three_board_max_fan_pct}/"
        f"PAUSED:{settings.cooling_paused_max_fan_pct} (TBD/measured)"
    )
    if settings.enable_writes:
        log("WRITES ARMED — still requires input_boolean.lard_board_priority_enable=on")
    else:
        log("WRITES DISARMED — observe-only until add-on option enable_writes is true")

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
    if token:
        ctrl.mqtt.discover_broker(token)
    ctrl.mqtt.start()

    try:
        ctrl.ensure_fan_max_helper()
    except Exception as e:
        log(f"fan_max helper ensure skip: {e}")

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
