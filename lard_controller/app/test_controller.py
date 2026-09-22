#!/usr/bin/env python3
"""Unit tests for LARD mode reconciliation (desired vs confirmed operational)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import unittest.mock
from collections import deque
from pathlib import Path

from controller import (
    ACTUAL_MODES,
    ADDON_VERSION,
    API_5XX_BACKOFF_S,
    CONTROLLER_STATES,
    LEGITIMATE_LIFECYCLE_TOKENS,
    OBSERVED_MINER_MODES,
    CHIP_ABORT_F,
    COOLING_DANGEROUS_C,
    COOLING_DANGEROUS_F,
    COOLING_HOT_C,
    COOLING_HOT_F,
    COOLING_IDLE_POWER_W,
    COOLING_POLICY_LEGACY,
    COOLING_POLICY_NATIVE,
    COOLING_TARGET_C,
    COOLING_TARGET_F,
    TEMP_F_OPENAPI_MAX,
    TEMP_F_OPENAPI_MIN,
    ENT_COOLING_DANGEROUS_C,
    ENT_COOLING_DANGEROUS_F,
    ENT_COOLING_HOT_C,
    ENT_COOLING_HOT_F,
    ENT_COMPETING_WRITER,
    ENT_COOLING_TARGET_C,
    ENT_COOLING_TARGET_F,
    ENT_ENABLE,
    ENT_FAN_MAX,
    ENT_FAULT,
    ENT_HB,
    ENT_MODE_REQ,
    ENT_OLD_AUTO,
    ENT_SOC,
    ENT_STALE,
    RESUME_WAIT_S,
    Braiins,
    Controller,
    WritePermission,
    board_patch_readback,
    classify_miner_telemetry,
    CoolingProfile,
    HealthState,
    Logger,
    MinerObservation,
    Settings,
    load_settings,
    clamp_fan_max_pct,
    metric_trend_rising,
    norm_board_id,
    parse_board_health_payload,
    fahrenheit_to_celsius_int,
    operator_setpoints_to_degree_c,
    parse_configured_cooling,
    parse_cooling_telemetry,
    parse_mining_state,
)


class FakeHA:
    def __init__(self, states: dict):
        self._states = dict(states)
        self.fail_count = 0
        self.writes: list[tuple[str, object]] = []
        self.events: list[tuple[str, dict]] = []
        self.state_reads: list[tuple[str, bool]] = []
        self.last_read_absent = False

    def fire_event(self, event_type, data=None):
        self.events.append((event_type, dict(data or {})))

    def state(self, entity_id: str, *, quiet: bool = False):
        self.state_reads.append((entity_id, quiet))
        return self._states.get(entity_id)

    def set_state(self, entity_id: str, state, attributes=None):
        self.writes.append((entity_id, state))
        self._states[entity_id] = state


class FakeBraiins:
    """In-memory Braiins OS+ client. Tracks writes; no HTTP."""

    def __init__(
        self,
        enabled=None,
        paused=True,
        running=False,
        user_paused=True,
        phase="stopped",
        status="paused",
        power_w=0.0,
        hashrate=0.0,
        power_target=944,
    ):
        self.enabled = [str(x) for x in (enabled if enabled is not None else ["1"])]
        self.paused = paused
        self.running = running
        self.user_paused = user_paused
        self.phase = phase
        self.status = status
        self.power_w = power_w
        self.hashrate = hashrate
        self.power_target = power_target
        self.fail_count = 0
        self.last_ok_iso = ""
        self.calls: list[tuple] = []
        self.resume_sets_running = True
        self.pause_sets_paused = True
        self.details_http = 200
        self.boards_http = 200
        self.resume_http = 200
        self.pause_http = 200
        self.clock = None
        self.unpause_after_elapsed = None
        self._resume_clock_at = None
        self.delay_board_apply_polls = 0
        self._pending_enabled = None
        self._board_delay_left = 0
        self.power_step = 0.0
        self.resume_sets_starting = False
        self.board_reads = None
        self.token_ts = 1.0
        self.cooling_http = 200
        self.cooling_state = {
            "fans": [{"position": 0, "rpm": 2400, "target_speed_ratio": 0.55}],
            "highest_temperature": {"location": 1, "temperature": {"degree_c": 70}},
        }
        self.cooling_exc = None
        self.last_cooling_body = None
        self.applied_max_fan = None
        self.cooling_confirm_max = None
        self._power_before_pause = power_w if power_w and power_w > 0 else 400.0
        self._hash_before_pause = hashrate if hashrate and hashrate > 0 else 20.0
        self.bosminer_uptime_s = 120.0
        self.pause_reason = "user_pause" if user_paused else ""
        self.start_http = 200
        self.restart_http = 200
        self.start_sets_running = False
        self.restart_sets_running = False
        self.resume_ok_after_elapsed = None
        self.cooling_resets_bosminer = False
        self._cooling_put_clock_at = None
        # 0.1.7 scripted lifecycle. Defaults keep the old resume_sets_running path.
        self.lifecycle_after_resume = None
        self.become_running_after_s = None
        self.run_on_resume_number = None
        self.resume_calls = 0
        self.resume_500_enters_lifecycle = False
        self.hard_fault_after_resume = False
        self.boards_healthy = True
        self.board_stale = False
        self.lifecycle_power_w = 0.0
        self.lifecycle_hashrate = 0.0
        self.running_zero_on_resume = False
        self.fault_when = None
        self.miner_errors: list = []
        self.board_health_script: list = []
        self.board_health_when_running: list = []
        self.hashboards_malformed = False
        self.omit_board_telemetry = False
        self.incomplete_boards = False
        self.extra_unhealthy_ids: list = []
        self.unhealthy_ids: list = []

    def _next_http(self, name: str, default: int = 200) -> int:
        val = getattr(self, name, default)
        if isinstance(val, deque):
            if not val:
                return default
            if len(val) == 1:
                return int(val[0])
            return int(val.popleft())
        return int(val)

    def _note_http(self, code: int) -> None:
        if code == 200:
            self.fail_count = 0
        else:
            self.fail_count += 1

    def write_names(self) -> list[str]:
        names = []
        for call in self.calls:
            name = call[0]
            if name in {
                "pause",
                "resume",
                "start",
                "restart",
                "patch_boards",
                "set_power",
                "set_cooling",
                "set_cooling_auto",
            }:
                names.append(name)
        return names

    def mining_write_names(self) -> list[str]:
        return [n for n in self.write_names() if n not in {"set_cooling", "set_cooling_auto"}]

    def cooling_puts(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "set_cooling_auto"]

    def _set_paused(self) -> None:
        if self.power_w and float(self.power_w) > 0:
            self._power_before_pause = float(self.power_w)
        if self.hashrate and float(self.hashrate) > 0:
            self._hash_before_pause = float(self.hashrate)
        self.paused = True
        self.running = False
        self.user_paused = True
        self.phase = "stopped"
        self.status = "paused"
        self.power_w = 0.0
        self.pause_reason = "user_pause"

    def _set_running(self) -> None:
        self.paused = False
        self.running = True
        self.user_paused = False
        self.phase = "running"
        self.status = "normal"
        self.pause_reason = ""
        if self.power_w is None or float(self.power_w) <= 0:
            self.power_w = float(getattr(self, "_power_before_pause", None) or 400.0)
        if self.hashrate is None or float(self.hashrate) <= 0:
            self.hashrate = float(getattr(self, "_hash_before_pause", None) or 20.0)

    def _set_starting(self) -> None:
        self.paused = False
        self.running = False
        self.user_paused = False
        self.phase = "starting"
        self.status = "starting"

    def _enter_lifecycle(self, phase: str) -> None:
        self.paused = False
        self.running = False
        self.user_paused = False
        self.phase = phase
        self.status = phase
        self.pause_reason = phase
        self.power_w = float(self.lifecycle_power_w or 0.0)
        self.hashrate = float(self.lifecycle_hashrate or 0.0)

    def _enter_hard_fault(self) -> None:
        self.paused = True
        self.running = False
        self.user_paused = False
        self.phase = "stopped"
        self.status = "hardware_fault"
        self.pause_reason = "hardware_fault"
        self.power_w = 0.0
        self.hashrate = 0.0

    def _maybe_become_running(self) -> None:
        if self.become_running_after_s is None or self.clock is None:
            return
        if self._resume_clock_at is None or self.running:
            return
        if self.clock.t >= self._resume_clock_at + float(self.become_running_after_s):
            self._set_running()

    def _apply_scripted_state(self) -> None:
        self._maybe_delayed_unpause()
        self._maybe_become_running()

    def _consume_board_script(self, script: list):
        if not script:
            return None
        item = script.pop(0)
        if item is None:
            raise RuntimeError("board_health unavailable")
        if item == "bad":
            saved = self.boards_healthy
            self.boards_healthy = False
            try:
                return self._board_health_payload()
            finally:
                self.boards_healthy = saved
        if isinstance(item, dict):
            return item
        return self._board_health_payload()

    def _board_health_payload(self):
        """Same shape ``Braiins.board_health`` returns from hashboards + errors."""
        if self.omit_board_telemetry:
            return parse_board_health_payload(None, [], errors_checked=True)
        if self.hashboards_malformed:
            return parse_board_health_payload({"hashboards": "bad"}, [], errors_checked=True)
        boards = []
        for bid in self.enabled:
            entry = {
                "id": str(bid),
                "enabled": True,
                "chips_count": None if self.incomplete_boards else 126,
            }
            if (not self.boards_healthy) or str(bid) in {str(x) for x in self.unhealthy_ids}:
                entry["healthy"] = False
            if self.board_stale:
                entry["stale"] = True
            boards.append(entry)
        for bid in self.extra_unhealthy_ids:
            boards.append(
                {
                    "id": str(bid),
                    "enabled": True,
                    "chips_count": 126,
                    "healthy": False,
                }
            )
        return parse_board_health_payload(
            {"hashboards": boards},
            list(self.miner_errors or []),
            errors_checked=True,
        )

    def board_health(self):
        if self.board_health_script:
            return self._consume_board_script(self.board_health_script)
        if self.resume_calls > 0 and self.running and self.board_health_when_running:
            return self._consume_board_script(self.board_health_when_running)
        return self._board_health_payload()

    def _maybe_delayed_unpause(self) -> None:
        if self.unpause_after_elapsed is None or self.clock is None:
            return
        if self._resume_clock_at is None:
            return
        if self.clock.t >= self._resume_clock_at + self.unpause_after_elapsed:
            self._set_starting()
            if self.power_w is None or self.power_w <= 0:
                self.power_w = 25.0
            if not self.power_step:
                self.power_step = 20.0

    def pause(self):
        self.calls.append(("pause",))
        code = self._next_http("pause_http")
        self._note_http(code)
        if code != 200:
            return code, {}
        if self.pause_sets_paused:
            self._set_paused()
        return 200, {"already_paused": False}

    def resume(self):
        self.calls.append(("resume",))
        self.resume_calls += 1
        if self.clock is not None:
            self._resume_clock_at = self.clock.t
            if self.resume_calls == 1:
                self._first_resume_clock_at = self.clock.t
        if self.resume_ok_after_elapsed is not None and self.clock is not None:
            origin = self._cooling_put_clock_at
            if origin is None:
                origin = self.clock.t
            if self.clock.t < origin + float(self.resume_ok_after_elapsed):
                self._note_http(500)
                return 500, {"error": "bosminer_not_ready"}
        code = self._next_http("resume_http")
        self._note_http(code)
        if code != 200:
            if self.resume_500_enters_lifecycle and self.lifecycle_after_resume:
                self._enter_lifecycle(self.lifecycle_after_resume)
            return code, {}
        if self.hard_fault_after_resume:
            self._enter_hard_fault()
            return 200, {"already_mining": False}
        if (
            self.run_on_resume_number is not None
            and self.resume_calls >= int(self.run_on_resume_number)
        ):
            self._set_running()
        elif self.running_zero_on_resume:
            self.paused = False
            self.running = True
            self.user_paused = False
            self.phase = "running"
            self.status = "normal"
            self.pause_reason = ""
            self.power_w = 0.0
            self.hashrate = 0.0
        elif self.lifecycle_after_resume:
            self._enter_lifecycle(self.lifecycle_after_resume)
        elif self.resume_sets_running:
            self._set_running()
        elif self.resume_sets_starting:
            self._set_starting()
        return 200, {"already_mining": False}

    def start(self):
        self.calls.append(("start",))
        code = self._next_http("start_http")
        self._note_http(code)
        if code != 200:
            return code, {}
        if self.start_sets_running:
            self._set_running()
        return 200, {"already_running": False}

    def restart(self):
        self.calls.append(("restart",))
        code = self._next_http("restart_http")
        self._note_http(code)
        if code != 200:
            return code, {}
        if self.restart_sets_running:
            self._set_running()
        elif self.bosminer_uptime_s is not None and float(self.bosminer_uptime_s) <= 0:
            self.bosminer_uptime_s = 1.0
        return 200, {"already_running": False}

    def set_power(self, watt: int):
        self.calls.append(("set_power", int(watt)))
        self.power_target = int(watt)
        return 200, {"watt": int(watt)}

    def get_power_target(self):
        self.calls.append(("get_power_target",))
        return 200, {"watt": int(self.power_target)}

    def patch_boards(self, enable: bool, ids: list[str]):
        ids = [str(i) for i in ids]
        self.calls.append(("patch_boards", bool(enable), ids))
        if enable:
            new = sorted(set(self.enabled) | set(ids))
        else:
            new = sorted(set(self.enabled) - set(ids))
        if self.delay_board_apply_polls > 0:
            self._pending_enabled = new
            self._board_delay_left = int(self.delay_board_apply_polls)
        else:
            self.enabled = new
        return 200, {}

    def enabled_ids(self):
        self.calls.append(("enabled_ids",))
        code = self._next_http("boards_http")
        self._note_http(code)
        if code != 200:
            body = getattr(self, "boards_error_body", None) or {}
            return [], code, body
        if self._pending_enabled is not None:
            if self._board_delay_left <= 0:
                self.enabled = self._pending_enabled
                self._pending_enabled = None
            else:
                self._board_delay_left -= 1
        if self.board_reads is not None:
            if len(self.board_reads) > 1:
                cur = self.board_reads.popleft()
            else:
                cur = self.board_reads[0] if self.board_reads else list(self.enabled)
            return list(cur), 200, {"hashboards": []}
        return list(self.enabled), 200, {"hashboards": []}

    def set_cooling_auto(self, max_fan_speed, extra_auto=None):
        n = int(max_fan_speed)
        extra = dict(extra_auto or {})
        body = {"auto": {"max_fan_speed": n, **extra}}
        self.last_cooling_body = body
        self.calls.append(("set_cooling_auto", n, extra or None))
        if self.clock is not None:
            self._cooling_put_clock_at = self.clock.t
        if self.cooling_resets_bosminer:
            self.bosminer_uptime_s = 0.0
            self.pause_reason = "application_unavailable"
        if self.cooling_exc:
            raise self.cooling_exc
        code = self._next_http("cooling_http")
        self._note_http(code)
        if code == 200:
            self.applied_max_fan = n
            state = dict(self.cooling_state or {})
            state["max_fan_speed"] = n
            if extra.get("min_fan_speed") is not None:
                state["min_fan_speed"] = extra["min_fan_speed"]
            state["auto"] = {"max_fan_speed": n, **extra}
            self.cooling_state = state
        return code, body

    def get_cooling_state(self):
        self.calls.append(("get_cooling_state",))
        if self.cooling_exc:
            raise self.cooling_exc
        state = dict(self.cooling_state or {"fans": []})
        if self.cooling_confirm_max is not None:
            state["max_fan_speed"] = int(self.cooling_confirm_max)
            auto = dict(state.get("auto") or {})
            auto["max_fan_speed"] = int(self.cooling_confirm_max)
            state["auto"] = auto
        return 200, state

    def approx_power_w(self):
        self.calls.append(("approx_power_w",))
        if self.power_step and not self.paused and not self.user_paused:
            self.power_w = (self.power_w or 0.0) + float(self.power_step)
        return self.power_w

    def approx_hashrate(self):
        self.calls.append(("approx_hashrate",))
        return self.hashrate

    def mining_state(self, details=None):
        self.calls.append(("mining_state",))
        code = self._next_http("details_http")
        self._note_http(code)
        if code != 200:
            return {}, code, {}
        if callable(self.fault_when):
            self.fault_when(self)
        self._apply_scripted_state()
        uptime = self.bosminer_uptime_s
        miner_ready = None if uptime is None else float(uptime) > 0
        not_started = self.status in (1, "1", "not_started", "miner_status_not_started") or (
            miner_ready is False
        )
        parsed = {
            "status_raw": self.status,
            "phase": self.phase,
            "user_paused": self.user_paused,
            "paused": self.paused,
            "running": self.running,
            "starting": self.phase == "starting",
            "preheating": self.phase in {"preheating", "preheat"},
            "ramping": self.phase in {"ramping", "ramp", "quick_ramping"},
            "pause_reason": self.pause_reason,
            "bosminer_uptime_s": uptime,
            "miner_ready": miner_ready,
            "not_started": bool(not_started),
        }
        return parsed, 200, {}


class FakeClock:
    def __init__(self, start: float = 1_000.0):
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += float(seconds)


def ha_states(mode_req: str) -> dict:
    return {
        ENT_ENABLE: "on",
        ENT_MODE_REQ: mode_req,
        ENT_OLD_AUTO: "off",
        ENT_HB: "on",
        ENT_STALE: "off",
        ENT_FAULT: "ok",
        ENT_SOC: "80",
        ENT_FAN_MAX: "100",
    }


def make_controller(braiins: FakeBraiins, mode_req: str, enable_writes: bool = True) -> Controller:
    tmp = Path(tempfile.mkdtemp(prefix="lard-test-"))
    settings = Settings(
        enable_writes=enable_writes,
        data_dir=tmp,
        share_dir=tmp / "share",
        poll_seconds=10,
        board_wait_seconds=60,
        power_target_w=944,
        # Tests keep envelopes equal so board-only paths stay pause-free unless
        # a cooling test sets a distinct desired profile / helper cap.
        cooling_dwell_seconds=600,
        cooling_stabilize_seconds=0,
        cooling_resume_settle_seconds=0,
        cooling_one_board_max_fan_pct=100,
        cooling_two_board_max_fan_pct=100,
        cooling_three_board_max_fan_pct=100,
        cooling_paused_max_fan_pct=100,
        # Production default is false. These tests opt into the gated path.
        auto_fan_ceiling_enabled=True,
        # Production default is false: Braiins owns cooling and LARD schedules
        # zero cooling transactions. Historical pause→PUT→resume cases opt in.
        cooling_control_enabled=True,
        # Production default is native_auto_target. Legacy tests keep the
        # fan-ceiling owner so existing pause→PUT→resume cases stay intact.
        cooling_policy="legacy_fan_ceiling",
        cooling_settle_seconds=0,
        cooling_writes_only_when_paused=True,
    )
    log = Logger(settings)
    health = HealthState(settings)
    ha = FakeHA(ha_states(mode_req))
    ctrl = Controller(ha, braiins, settings, log, health)
    clock = FakeClock()
    ctrl._now = clock.now  # type: ignore[method-assign]
    ctrl._sleep = clock.sleep  # type: ignore[method-assign]
    ctrl._clock = clock
    braiins.clock = clock
    return ctrl


def paused_one_board(**kwargs) -> FakeBraiins:
    return FakeBraiins(
        enabled=["1"],
        paused=True,
        running=False,
        user_paused=True,
        phase="stopped",
        status="paused",
        power_w=0.0,
        **kwargs,
    )


def running_boards(ids, **kwargs) -> FakeBraiins:
    return FakeBraiins(
        enabled=ids,
        paused=False,
        running=True,
        user_paused=False,
        phase="running",
        status="normal",
        power_w=400.0,
        hashrate=20.0,
        **kwargs,
    )


class ParseMiningStateTests(unittest.TestCase):
    def test_legacy_status_paused_int(self):
        parsed = parse_mining_state({"status": 3})
        self.assertTrue(parsed["paused"])
        self.assertTrue(parsed["user_paused"])
        self.assertFalse(parsed["running"])

    def test_legacy_status_normal_name(self):
        parsed = parse_mining_state({"status": "MINER_STATUS_NORMAL"})
        self.assertTrue(parsed["running"])
        self.assertFalse(parsed["paused"])
        self.assertFalse(parsed["user_paused"])

    def test_detailed_user_pause(self):
        parsed = parse_mining_state(
            {
                "status": "paused",
                "detailed_status": {"stopped": {"reason": {"user_pause": {}}}},
            }
        )
        self.assertEqual(parsed["phase"], "stopped")
        self.assertTrue(parsed["user_paused"])
        self.assertTrue(parsed["paused"])
        self.assertFalse(parsed["running"])

    def test_detailed_running_nested_status(self):
        parsed = parse_mining_state(
            {
                "status": 2,
                "detailed_status": {
                    "status": {"running": {"reason": {"normal": {}}}},
                },
            }
        )
        self.assertEqual(parsed["phase"], "running")
        self.assertTrue(parsed["running"])
        self.assertFalse(parsed["paused"])

    def test_starting_is_not_running(self):
        parsed = parse_mining_state({"detailed_status": {"starting": {"reason": {"none": {}}}}})
        self.assertTrue(parsed["starting"])
        self.assertFalse(parsed["running"])
        self.assertFalse(parsed["user_paused"])

    def test_preheating_and_ramping_are_transitional(self):
        pre = parse_mining_state({"detailed_status": {"preheating": {}}})
        self.assertTrue(pre["preheating"])
        self.assertFalse(pre["running"])
        self.assertFalse(pre["paused"])
        ramp = parse_mining_state({"status": "ramping"})
        self.assertTrue(ramp["ramping"])
        self.assertEqual(ramp["phase"], "ramping")
        self.assertFalse(ramp["running"])

    def test_bosminer_uptime_zero_means_process_not_ready(self):
        parsed = parse_mining_state(
            {
                "status": 1,
                "bosminer_uptime_s": 0,
                "detailed_status": {
                    "stopped": {"reason": {"application_unavailable": {}}}
                },
            }
        )
        self.assertEqual(parsed["pause_reason"], "application_unavailable")
        self.assertEqual(parsed["bosminer_uptime_s"], 0.0)
        self.assertFalse(parsed["miner_ready"])
        self.assertTrue(parsed["not_started"])

    def test_user_pause_with_bosminer_up_is_ready(self):
        parsed = parse_mining_state(
            {
                "status": 3,
                "bosminer_uptime_s": 44,
                "detailed_status": {"stopped": {"reason": {"user_pause": {}}}},
            }
        )
        self.assertEqual(parsed["pause_reason"], "user_pause")
        self.assertTrue(parsed["user_paused"])
        self.assertTrue(parsed["miner_ready"])
        self.assertFalse(parsed["not_started"])


class ReconciliationTests(unittest.TestCase):
    def test_1_paused_correct_boards_must_resume_not_applied_until_running(self):
        b = paused_one_board()
        ctrl = make_controller(b, "ONE_BOARD")
        obs = ctrl.read_actual_from_miner()
        self.assertEqual(ctrl.actual_mode, "PAUSED")
        self.assertTrue(ctrl._needs_reconcile("ONE_BOARD", obs))
        self.assertNotEqual(ctrl.actual_mode, "ONE_BOARD")

        ctrl.tick()
        self.assertIn("resume", b.write_names())
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertTrue(b.running)
        self.assertFalse(b.paused)

    def test_1b_resume_without_running_does_not_confirm_one_board(self):
        """Resume accepted but miner stays paused: not ONE_BOARD, and not ERROR."""
        b = paused_one_board()
        b.resume_sets_running = False
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.tick()
        self.assertIn("resume", b.write_names())
        self.assertNotEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertIn("degraded", ctrl.last_error)

    def test_2_running_wrong_boards_fixes_boards_no_pause_resume(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "TWO_BOARD")
        ctrl.read_actual_from_miner()
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")

        ctrl.tick()
        self.assertIn("patch_boards", b.write_names())
        self.assertNotIn("pause", b.write_names())
        self.assertNotIn("resume", b.write_names())
        self.assertEqual(sorted(b.enabled), ["1", "2"])
        self.assertEqual(ctrl.actual_mode, "TWO_BOARD")
        self.assertTrue(b.running)

    def test_3_running_correct_boards_no_unnecessary_writes(self):
        b = running_boards(["1", "2"])
        ctrl = make_controller(b, "TWO_BOARD")
        ctrl.read_actual_from_miner()
        self.assertEqual(ctrl.actual_mode, "TWO_BOARD")
        before = list(b.mining_write_names())

        ctrl.tick()
        self.assertEqual(b.mining_write_names(), before)
        self.assertEqual(ctrl.actual_mode, "TWO_BOARD")
        self.assertNotIn("pause", b.write_names())
        self.assertNotIn("resume", b.write_names())
        self.assertNotIn("patch_boards", b.write_names())
        self.assertNotIn("set_power", b.write_names())

    def test_4_paused_desired_running_miner_must_pause(self):
        b = running_boards(["1", "2", "3"])
        ctrl = make_controller(b, "PAUSED")
        ctrl.read_actual_from_miner()
        self.assertEqual(ctrl.actual_mode, "THREE_BOARD")

        ctrl.tick()
        self.assertIn("pause", b.write_names())
        self.assertEqual(ctrl.actual_mode, "PAUSED")
        self.assertTrue(b.paused)
        self.assertFalse(b.running)

    def test_5_restart_paused_matching_boards_does_not_skip_resume(self):
        """Add-on restart: read_actual must not short-circuit resume."""
        cases = [
            ("ONE_BOARD", ["1"], "resume"),
            ("TWO_BOARD", ["1", "2"], "resume"),
            ("THREE_BOARD", ["1", "2", "3"], "resume"),
        ]
        for desired, boards, expect_write in cases:
            with self.subTest(desired=desired, boards=boards):
                b = FakeBraiins(
                    enabled=boards,
                    paused=True,
                    running=False,
                    user_paused=True,
                    phase="stopped",
                    status="paused",
                    power_w=0.0,
                )
                boot = make_controller(b, desired)
                boot.read_actual_from_miner()
                self.assertEqual(boot.actual_mode, "PAUSED", desired)
                self.assertNotEqual(boot.actual_mode, desired)

                restarted = make_controller(b, desired)
                restarted.read_actual_from_miner()
                self.assertEqual(restarted.actual_mode, "PAUSED")
                restarted.tick()
                self.assertIn(expect_write, b.write_names())
                self.assertEqual(restarted.actual_mode, desired)
                self.assertTrue(b.running)

    def test_5_restart_running_wrong_boards_fixes_boards_only(self):
        b = running_boards(["1"])
        boot = make_controller(b, "TWO_BOARD")
        boot.read_actual_from_miner()
        self.assertEqual(boot.actual_mode, "ONE_BOARD")

        restarted = make_controller(b, "TWO_BOARD")
        restarted.read_actual_from_miner()
        self.assertEqual(restarted.actual_mode, "ONE_BOARD")
        restarted.tick()
        self.assertIn("patch_boards", b.write_names())
        self.assertNotIn("pause", b.write_names())
        self.assertNotIn("resume", b.write_names())
        self.assertEqual(restarted.actual_mode, "TWO_BOARD")

    def test_5_restart_running_correct_boards_no_writes(self):
        b = running_boards(["1", "2", "3"])
        boot = make_controller(b, "THREE_BOARD")
        boot.read_actual_from_miner()
        self.assertEqual(boot.actual_mode, "THREE_BOARD")

        restarted = make_controller(b, "THREE_BOARD")
        restarted.read_actual_from_miner()
        self.assertEqual(restarted.actual_mode, "THREE_BOARD")
        before = list(b.mining_write_names())
        restarted.tick()
        self.assertEqual(b.mining_write_names(), before)
        self.assertEqual(restarted.actual_mode, "THREE_BOARD")

    def test_5_restart_running_then_desired_pause(self):
        b = running_boards(["1"])
        boot = make_controller(b, "PAUSED")
        boot.read_actual_from_miner()
        self.assertEqual(boot.actual_mode, "ONE_BOARD")

        restarted = make_controller(b, "PAUSED")
        restarted.read_actual_from_miner()
        self.assertEqual(restarted.actual_mode, "ONE_BOARD")
        restarted.tick()
        self.assertIn("pause", b.write_names())
        self.assertEqual(restarted.actual_mode, "PAUSED")

    def test_stale_actual_one_board_does_not_skip_resume(self):
        """Even a leftover actual=ONE_BOARD must not skip when miner is paused."""
        b = paused_one_board()
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.actual_mode = "ONE_BOARD"
        ctrl.confirmed_operational = "ONE_BOARD"
        ctrl.tick()
        self.assertIn("resume", b.write_names())
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertTrue(b.running)

    def test_power_zero_during_warmup_is_not_failure(self):
        """0 W while running stays inside recovery. It is not ERROR and not HASHING."""
        b = paused_one_board()
        b.power_w = 0.0
        ctrl = make_controller(b, "ONE_BOARD")

        def resume_still_zero():
            b._set_running()
            b.power_w = 0.0
            b.hashrate = 0.0
            return 200, {}

        b.resume = lambda: (b.calls.append(("resume",)) or resume_still_zero())  # type: ignore
        ctrl.tick()
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertNotEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertNotEqual(ctrl._health_class, "HASHING")
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertEqual(b.write_names().count("resume"), 2)
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertEqual(b.power_w, 0.0)

    def test_writes_gate_still_blocks(self):
        b = paused_one_board()
        ctrl = make_controller(b, "ONE_BOARD", enable_writes=False)
        ctrl.tick()
        self.assertEqual(b.write_names(), [])
        self.assertEqual(ctrl.actual_mode, "PAUSED")
        self.assertIn("enable_writes_false", ctrl.reason)

    def test_master_gate_still_blocks(self):
        b = paused_one_board()
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.ha._states[ENT_ENABLE] = "off"
        ctrl.tick()
        self.assertEqual(b.mining_write_names(), [])
        self.assertIn("master_gate_off", ctrl.reason)

    def test_old_auto_still_refuses_writes(self):
        b = paused_one_board()
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.ha._states[ENT_OLD_AUTO] = "on"
        ctrl.tick()
        self.assertEqual(b.write_names(), [])
        self.assertEqual(ctrl.last_error, "refusing_writes_old_auto_enable_is_on")

    def test_idempotent_two_board_paused_already_correct_boards(self):
        b = FakeBraiins(
            enabled=["1", "2"],
            paused=True,
            running=False,
            user_paused=True,
            phase="stopped",
            status="paused",
            power_w=0.0,
        )
        ctrl = make_controller(b, "TWO_BOARD")
        ctrl.tick()
        self.assertIn("resume", b.write_names())
        self.assertNotIn("patch_boards", b.write_names())
        self.assertEqual(ctrl.actual_mode, "TWO_BOARD")

    def test_three_board_running_only_one_on(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "THREE_BOARD")
        ctrl.tick()
        self.assertIn("patch_boards", b.write_names())
        self.assertNotIn("pause", b.write_names())
        self.assertNotIn("resume", b.write_names())
        self.assertEqual(sorted(b.enabled), ["1", "2", "3"])
        self.assertEqual(ctrl.actual_mode, "THREE_BOARD")


class ResumeConvergenceTests(unittest.TestCase):
    def test_resume_wait_constant_is_90_to_120(self):
        self.assertGreaterEqual(RESUME_WAIT_S, 90)
        self.assertLessEqual(RESUME_WAIT_S, 120)
        self.assertEqual(API_5XX_BACKOFF_S, (2, 5, 10, 20))
        self.assertFalse(Settings().enable_writes)

    def test_stage_abc_low_rising_watts_no_mature_th(self):
        b = paused_one_board()
        b.resume_sets_running = False
        b.resume_sets_starting = True
        b.power_w = 40.0
        b.hashrate = 0.002
        b.power_step = 15.0
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.tick()
        self.assertIn("resume", b.write_names())
        self.assertEqual(b.write_names().count("resume"), 2)
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertNotEqual(ctrl._health_class, "HASHING")
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertIn("degraded_needs_attention", ctrl.last_error)
        self.assertFalse(b.running)
        self.assertLess(b.hashrate, 1.0)
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())

    def test_resume_does_not_timeout_at_30s_if_stage_a_clears_later(self):
        b = paused_one_board()
        b.resume_sets_running = False
        b.unpause_after_elapsed = 90
        ctrl = make_controller(b, "ONE_BOARD")
        started = ctrl._clock.t
        ctrl.tick()
        self.assertGreaterEqual(ctrl._clock.t - started, 90)
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertNotIn("resume_wait_timeout", ctrl.last_error or "")
        self.assertNotEqual(ctrl._health_class, "HASHING")
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertEqual(b.write_names().count("resume"), 2)
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())

    def test_single_http_500_then_success_does_not_error(self):
        b = paused_one_board()
        b.boards_http = deque([500, 200])
        ctrl = make_controller(b, "ONE_BOARD")
        ok = ctrl.apply_mode("ONE_BOARD")
        self.assertTrue(ok)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertEqual(ctrl.last_error, "")
        self.assertEqual(b.fail_count, 0)

    def test_single_resume_500_then_success_does_not_error(self):
        b = paused_one_board()
        b.resume_http = deque([500, 200])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertEqual(ctrl.last_error, "")

    def test_sustained_500s_after_backoff_window_errors(self):
        b = paused_one_board()
        b.boards_http = 500
        ctrl = make_controller(b, "ONE_BOARD")
        started = ctrl._clock.t
        ok = ctrl.apply_mode("ONE_BOARD")
        self.assertFalse(ok)
        self.assertEqual(ctrl.actual_mode, "ERROR")
        self.assertIn("500", ctrl.last_error)
        self.assertGreaterEqual(ctrl._clock.t - started, sum(API_5XX_BACKOFF_S))

    def test_api_fail_count_resets_after_successful_read(self):
        b = running_boards(["1"])
        b.fail_count = 6
        ctrl = make_controller(b, "ONE_BOARD")
        obs = ctrl.observe_miner()
        self.assertTrue(obs.ok)
        self.assertEqual(b.fail_count, 0)

    def test_braiins_fail_count_resets_on_authenticated_http_200(self):
        tmp = Path(tempfile.mkdtemp(prefix="lard-braiins-"))
        settings = Settings(braiins_password="x", data_dir=tmp, share_dir=tmp / "share")
        client = Braiins(settings, Logger(settings))
        client.token = "tok"
        client.token_ts = time.time()
        client.fail_count = 9

        class Resp:
            status = 200

            def read(self):
                return b'{"ok":true}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with unittest.mock.patch("controller.urllib.request.urlopen", return_value=Resp()):
            code, payload = client._call("GET", "/api/v1/miner/details")
        self.assertEqual(code, 200)
        self.assertEqual(payload.get("ok"), True)
        self.assertEqual(client.fail_count, 0)

    def test_board_http_200_delayed_topology_still_converges(self):
        b = running_boards(["1"])
        b.delay_board_apply_polls = 3
        ctrl = make_controller(b, "TWO_BOARD")
        ctrl.tick()
        self.assertIn("patch_boards", b.write_names())
        self.assertEqual(sorted(b.enabled), ["1", "2"])
        self.assertEqual(ctrl.actual_mode, "TWO_BOARD")
        self.assertEqual(ctrl.last_error, "")

    def test_paused_correct_boards_still_resumes(self):
        """0.1.1 regression: boards {1} + user_pause is PAUSED and must resume."""
        b = paused_one_board()
        ctrl = make_controller(b, "ONE_BOARD")
        obs = ctrl.read_actual_from_miner()
        self.assertEqual(ctrl.actual_mode, "PAUSED")
        self.assertTrue(ctrl._needs_reconcile("ONE_BOARD", obs))
        ctrl.tick()
        self.assertIn("resume", b.write_names())
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertTrue(b.running)

    def test_trend_helper_requires_rise_not_absolute(self):
        self.assertFalse(metric_trend_rising([40]))
        self.assertTrue(metric_trend_rising([40, 55, 80]))
        self.assertFalse(metric_trend_rising([80, 80]))


class BoardMatchSkipTests(unittest.TestCase):
    def test_paused_to_one_board_same_topology_resume_only(self):
        """PAUSED→ONE_BOARD with boards already [1]: resume only, no PATCH/wait."""
        b = paused_one_board()
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.actual_mode = "PAUSED"
        ctrl.confirmed_operational = "PAUSED"
        ctrl.tick()
        self.assertIn("resume", b.write_names())
        self.assertNotIn("patch_boards", b.write_names())
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertNotIn("board_wait_timeout", ctrl.last_error or "")
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertTrue(b.running)

    def test_enabled_ids_int_vs_str_still_matches(self):
        self.assertEqual(norm_board_id(1), "1")
        self.assertEqual(norm_board_id("1"), "1")
        self.assertEqual(norm_board_id(1.0), "1")
        b = paused_one_board()
        b.enabled = [1]
        ctrl = make_controller(b, "ONE_BOARD")
        self.assertTrue(ctrl._boards_match([1], ["1"]))
        self.assertTrue(ctrl._boards_match([1, 2], ["2", "1"]))
        obs = ctrl.observe_miner()
        self.assertTrue(ctrl._boards_already_satisfied("ONE_BOARD", obs))
        ctrl.tick()
        self.assertIn("resume", b.write_names())
        self.assertNotIn("patch_boards", b.write_names())
        self.assertNotIn("board_wait_timeout", ctrl.last_error or "")
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")

    def test_one_empty_board_poll_then_correct_set_succeeds(self):
        b = paused_one_board()
        b.board_reads = deque([[], ["1"]])
        ctrl = make_controller(b, "ONE_BOARD")
        ok = ctrl._ensure_boards(["1"])
        self.assertTrue(ok)
        self.assertNotIn("board_wait_timeout", ctrl.last_error or "")
        self.assertNotIn("patch_boards", b.write_names())
        self.assertTrue(ctrl._boards_match(["1"], ["1"]))

    def test_set_boards_poll_int_ids_do_not_timeout(self):
        b = running_boards(["1"])
        b.enabled = [1]
        ctrl = make_controller(b, "ONE_BOARD")
        ok = ctrl._set_boards(["1"])
        self.assertTrue(ok)
        self.assertNotIn("board_wait_timeout", ctrl.last_error or "")
        self.assertNotIn("patch_boards", b.write_names())


def _applied(max_fan: int, name: str = "ONE_BOARD") -> CoolingProfile:
    fans = 2 if max_fan >= 100 else None
    return CoolingProfile(name, max_fan, None, fans)


class FanCeilingTests(unittest.TestCase):
    def test_old_automatic_wipe_path_is_gone(self):
        self.assertFalse(hasattr(Braiins, "set_cooling_profile_auto"))
        self.assertTrue(hasattr(Braiins, "set_cooling_auto"))
        src = Path(__file__).resolve().parent.joinpath("controller.py").read_text()
        self.assertNotIn('{"mode": "automatic"}', src)
        self.assertNotIn('{"mode":"automatic"}', src)
        self.assertIn('"/api/v1/cooling/mode"', src)
        self.assertNotIn('"/api/v1/cooling",', src)
        self.assertNotIn("set_cooling_profile_auto", src)
        self.assertNotIn("def _sync_fan_ceiling", src)

    def test_clamp_fan_max_pct(self):
        self.assertEqual(clamp_fan_max_pct(-5), 0)
        self.assertEqual(clamp_fan_max_pct(160), 100)
        self.assertEqual(clamp_fan_max_pct("60"), 60)
        self.assertEqual(clamp_fan_max_pct("60.4"), 60)
        self.assertEqual(clamp_fan_max_pct("bogus"), 100)
        self.assertGreaterEqual(CHIP_ABORT_F, 180)

    def test_parse_cooling_telemetry_ratio_and_temp(self):
        tel = parse_cooling_telemetry(
            {
                "fans": [{"position": 0, "rpm": 3100, "target_speed_ratio": 0.6}],
                "highest_temperature": {"location": 1, "temperature": {"degree_c": 80}},
                "max_fan_speed": 70,
            }
        )
        self.assertEqual(tel["fan_rpm"], 3100)
        self.assertEqual(tel["fan_pct"], 60.0)
        self.assertAlmostEqual(tel["chip_temp_f"], 176.0)
        self.assertEqual(tel["max_fan_speed"], 70)

    def test_apply_mode_put_body_uses_helper_max_fan_speed(self):
        b = paused_one_board()
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ok = ctrl.apply_mode("ONE_BOARD")
        self.assertTrue(ok)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        puts = b.cooling_puts()
        self.assertTrue(puts)
        self.assertTrue(any(c[1] == 60 for c in puts))
        self.assertEqual((b.last_cooling_body or {}).get("auto", {}).get("max_fan_speed"), 60)
        self.assertNotIn("mode", b.last_cooling_body or {})
        self.assertNotIn("pause", b.mining_write_names())
        self.assertIn("resume", b.mining_write_names())
        self.assertLess(b.write_names().index("set_cooling_auto"), b.write_names().index("resume"))

    def test_desired_equals_applied_skips_pause_and_cooling_put(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.read_actual_from_miner()
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("pause", b.write_names())
        self.assertNotIn("resume", b.write_names())

    def test_desired_change_order_pause_put_resume_then_actual(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.read_actual_from_miner()
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        ctrl.ha._states[ENT_FAN_MAX] = "60"

        seen = []
        orig_pause = b.pause
        orig_put = b.set_cooling_auto
        orig_resume = b.resume

        def wrap_pause():
            seen.append(("pause", ctrl.actual_mode, ctrl.last_error, b.power_w, b.user_paused))
            self.assertEqual(ctrl.actual_mode, "APPLYING")
            self.assertEqual(ctrl.last_error, "")
            return orig_pause()

        def wrap_put(max_fan_speed, extra_auto=None):
            seen.append(
                (
                    "set_cooling_auto",
                    ctrl.actual_mode,
                    ctrl.last_error,
                    b.power_w,
                    b.user_paused,
                    int(max_fan_speed),
                )
            )
            self.assertEqual(ctrl.actual_mode, "APPLYING")
            self.assertEqual(ctrl.last_error, "")
            self.assertTrue(b.user_paused)
            self.assertLessEqual(float(b.power_w or 0), COOLING_IDLE_POWER_W)
            self.assertNotEqual(ctrl.actual_mode, "ERROR")
            return orig_put(max_fan_speed, extra_auto)

        def wrap_resume():
            seen.append(("resume", ctrl.actual_mode, ctrl.last_error, b.user_paused))
            self.assertEqual(ctrl.actual_mode, "APPLYING")
            self.assertEqual(ctrl.last_error, "")
            return orig_resume()

        b.pause = wrap_pause  # type: ignore[method-assign]
        b.set_cooling_auto = wrap_put  # type: ignore[method-assign]
        b.resume = wrap_resume  # type: ignore[method-assign]

        ctrl.tick()
        names = [row[0] for row in seen]
        self.assertEqual(names, ["pause", "set_cooling_auto", "resume"])
        self.assertEqual(seen[1][5], 60)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertEqual(ctrl.last_error, "")
        self.assertTrue(b.running)
        self.assertFalse(b.user_paused)
        self.assertGreater(float(b.power_w or 0), 0)
        self.assertEqual(ctrl._cooling_applied.max_fan_speed, 60)
        self.assertNotIn("mode", b.last_cooling_body or {})

    def test_cooling_put_failure_restores_pauses_errors_no_legacy(self):
        b = running_boards(["1"])
        b.cooling_http = deque([400, 200])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "ERROR")
        self.assertIn("cooling_put", ctrl.last_error)
        self.assertNotIn("resume", b.write_names())
        puts = b.cooling_puts()
        self.assertGreaterEqual(len(puts), 2)
        self.assertEqual(puts[0][1], 60)
        self.assertEqual(puts[-1][1], 100)
        self.assertIn("pause", b.write_names())
        self.assertTrue(b.paused)
        self.assertNotEqual(ctrl.ha._states.get(ENT_OLD_AUTO), "on")

    def test_apply_mode_cooling_failure_errors_no_resume(self):
        b = paused_one_board()
        b.cooling_exc = RuntimeError("cooling down")
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ok = ctrl.apply_mode("ONE_BOARD")
        self.assertFalse(ok)
        self.assertEqual(ctrl.actual_mode, "ERROR")
        self.assertIn("cooling_put", ctrl.last_error)
        self.assertNotIn("resume", b.mining_write_names())
        self.assertNotEqual(ctrl.ha._states.get(ENT_OLD_AUTO), "on")

    def test_apply_mode_cooling_http_error_errors(self):
        b = paused_one_board()
        b.cooling_http = 400
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.ha._states[ENT_FAN_MAX] = "45"
        ok = ctrl.apply_mode("ONE_BOARD")
        self.assertFalse(ok)
        self.assertEqual(ctrl.actual_mode, "ERROR")
        self.assertIn("cooling_put_http_400", ctrl.last_error)
        self.assertNotIn("resume", b.mining_write_names())

    def test_resume_failure_errors_and_restores(self):
        b = running_boards(["1"])
        b.resume_http = 400
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "ERROR")
        self.assertIn("cooling_resume_http_400", ctrl.last_error)
        puts = [c[1] for c in b.cooling_puts()]
        self.assertIn(60, puts)
        self.assertEqual(puts[-1], 100)
        self.assertTrue(b.paused)
        self.assertNotEqual(ctrl.ha._states.get(ENT_OLD_AUTO), "on")

    def test_intentional_pause_during_transition_is_not_error(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.ha._states[ENT_FAN_MAX] = "55"
        errors_while_paused = []

        orig_put = b.set_cooling_auto

        def wrap_put(max_fan_speed, extra_auto=None):
            errors_while_paused.append((ctrl.actual_mode, ctrl.last_error, b.user_paused, b.power_w))
            self.assertEqual(ctrl.actual_mode, "APPLYING")
            self.assertEqual(ctrl.last_error, "")
            self.assertTrue(b.user_paused)
            return orig_put(max_fan_speed, extra_auto)

        b.set_cooling_auto = wrap_put  # type: ignore[method-assign]
        ctrl.tick()
        self.assertTrue(errors_while_paused)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertEqual(ctrl.last_error, "")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")

    def test_dwell_prevents_rapid_retransitions(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = _applied(100)
        ctrl._cooling_last_change_ts = ctrl._now()
        ctrl._fan_max_seen = 100
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("pause", b.write_names())
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        ctrl._clock.t += 601
        ctrl.tick()
        self.assertTrue(b.cooling_puts())
        self.assertEqual(b.cooling_puts()[-1][1], 60)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")

    def test_helper_change_is_gated_not_live(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.read_actual_from_miner()
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertIn("pause", b.write_names())
        self.assertIn("resume", b.write_names())
        self.assertTrue(any(c[1] == 60 for c in b.cooling_puts()))
        pause_i = b.write_names().index("pause")
        put_i = b.write_names().index("set_cooling_auto")
        resume_i = b.write_names().index("resume")
        self.assertLess(pause_i, put_i)
        self.assertLess(put_i, resume_i)

    def test_helper_100_restores_unconstrained_gated(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = _applied(60)
        ctrl._fan_max_seen = 60
        ctrl.ha._states[ENT_FAN_MAX] = "100"
        ctrl.tick()
        self.assertTrue(b.cooling_puts())
        self.assertEqual(b.cooling_puts()[-1][1], 100)
        extra = b.cooling_puts()[-1][2] or {}
        self.assertEqual(extra.get("minimum_required_fans"), 2)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertIn("pause", b.write_names())
        self.assertIn("resume", b.write_names())

    def test_chip_abort_uses_gated_transition(self):
        b = running_boards(["1"])
        b.cooling_state = {
            "fans": [{"position": 0, "rpm": 4200, "target_speed_ratio": 0.6}],
            "highest_temperature": {"location": 1, "temperature": {"degree_c": 85}},
            "max_fan_speed": 60,
        }
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = _applied(60)
        ctrl._fan_max_seen = 60
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.tick()
        self.assertGreaterEqual(ctrl._chip_temp_f, CHIP_ABORT_F)
        self.assertEqual(b.cooling_puts()[-1][1], 100)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertIn("pause", b.mining_write_names())
        self.assertIn("resume", b.mining_write_names())
        self.assertTrue(ctrl._thermal_abort_active)

    def test_chip_abort_bypasses_dwell(self):
        b = running_boards(["1"])
        b.cooling_state = {
            "fans": [{"position": 0, "rpm": 4200, "target_speed_ratio": 1.0}],
            "highest_temperature": {"location": 1, "temperature": {"degree_c": 85}},
            "max_fan_speed": 60,
        }
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = _applied(60)
        ctrl._cooling_last_change_ts = ctrl._now()
        ctrl._fan_max_seen = 60
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.tick()
        self.assertTrue(b.cooling_puts())
        self.assertEqual(b.cooling_puts()[-1][1], 100)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")

    def test_enable_writes_false_skips_fan_ceiling(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD", enable_writes=False)
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        self.assertEqual(b.write_names(), [])

    def test_board_count_profile_change_pauses_before_cooling_put(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "TWO_BOARD")
        ctrl.settings.cooling_one_board_max_fan_pct = 70
        ctrl.settings.cooling_two_board_max_fan_pct = 85
        ctrl._cooling_applied = _applied(70)
        ctrl._fan_max_seen = 100
        ctrl.tick()
        self.assertIn("pause", b.write_names())
        self.assertIn("set_cooling_auto", b.write_names())
        self.assertIn("resume", b.write_names())
        self.assertTrue(any(c[1] == 85 for c in b.cooling_puts()))
        self.assertEqual(sorted(b.enabled), ["1", "2"])
        self.assertEqual(ctrl.actual_mode, "TWO_BOARD")
        pause_i = b.write_names().index("pause")
        put_i = b.write_names().index("set_cooling_auto")
        self.assertLess(pause_i, put_i)

    def test_cooling_resume_500_then_200_after_settle_succeeds(self):
        """0.1.6: first post-cooling ResumeMining 500 is unreadiness, not ERROR."""
        b = running_boards(["1"])
        b.resume_http = deque([500, 200])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.settings.cooling_resume_settle_seconds = 15
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        started = ctrl._clock.t
        modes_on_resume = []
        errors_on_resume = []
        resume_times = []
        orig_resume = b.resume

        def wrap_resume():
            modes_on_resume.append(ctrl.actual_mode)
            errors_on_resume.append(ctrl.last_error)
            resume_times.append(ctrl._clock.t)
            self.assertEqual(ctrl.actual_mode, "APPLYING")
            self.assertNotEqual(ctrl.actual_mode, "ERROR")
            return orig_resume()

        b.resume = wrap_resume  # type: ignore[method-assign]
        ctrl.tick()
        self.assertTrue(resume_times)
        self.assertGreaterEqual(resume_times[0] - started, 15)
        self.assertGreaterEqual(len(resume_times), 2)
        self.assertTrue(all(m == "APPLYING" for m in modes_on_resume))
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertEqual(ctrl.last_error, "")
        self.assertTrue(b.running)
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertGreaterEqual(b.write_names().count("resume"), 2)

    def test_cooling_resume_always_500_is_degraded_not_error(self):
        """Sustained ResumeMining 500 with no lifecycle progress is DEGRADED.

        0.1.7 does not escalate to Start or BOSminer Restart, and does not ERROR
        solely because resume stays HTTP 500 / 0 W.
        """
        b = running_boards(["1"])
        b.resume_http = 500
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.settings.cooling_resume_settle_seconds = 8
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        started = ctrl._clock.t
        saw_applying = []
        orig_resume = b.resume

        def wrap_resume():
            saw_applying.append(ctrl.actual_mode)
            self.assertEqual(ctrl.actual_mode, "APPLYING")
            self.assertNotEqual(ctrl.actual_mode, "ERROR")
            return orig_resume()

        b.resume = wrap_resume  # type: ignore[method-assign]
        ctrl.tick()
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertIn("degraded_needs_attention", ctrl.last_error)
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertEqual(b.resume_calls, 2)
        self.assertTrue(saw_applying)
        self.assertTrue(all(m == "APPLYING" for m in saw_applying))
        self.assertGreaterEqual(ctrl._clock.t - started, 8)
        self.assertTrue(b.paused)
        self.assertNotEqual(ctrl.ha._states.get(ENT_OLD_AUTO), "on")
        puts = [c[1] for c in b.cooling_puts()]
        self.assertEqual(puts, [60])

    def test_cooling_resume_200_first_try_still_works(self):
        b = running_boards(["1"])
        b.resume_http = 200
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.settings.cooling_resume_settle_seconds = 12
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.ha._states[ENT_FAN_MAX] = "55"
        started = ctrl._clock.t
        resume_times = []
        orig_resume = b.resume

        def wrap_resume():
            resume_times.append(ctrl._clock.t)
            self.assertEqual(ctrl.actual_mode, "APPLYING")
            return orig_resume()

        b.resume = wrap_resume  # type: ignore[method-assign]
        ctrl.tick()
        self.assertTrue(resume_times)
        self.assertGreaterEqual(resume_times[0] - started, 12)
        self.assertEqual(b.write_names().count("resume"), 1)
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertEqual(ctrl.last_error, "")
        self.assertTrue(b.running)

    def test_cooling_put_process_state_change_is_logged(self):
        b = running_boards(["1"])
        b.cooling_resets_bosminer = True
        b.resume_ok_after_elapsed = 10
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.settings.cooling_resume_settle_seconds = 10
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.tick()
        log = (ctrl.settings.data_dir / "controller.log").read_text()
        self.assertIn("cooling readiness post_cooling_put", log)
        self.assertIn("bosminer_uptime_s", log)
        self.assertIn("application_unavailable", log)
        self.assertIn("changed=", log)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")

    def test_device_reboot_start_and_restart_denied_before_http(self):
        tmp = Path(tempfile.mkdtemp(prefix="lard-braiins-deny-"))
        settings = Settings(braiins_password="x", data_dir=tmp, share_dir=tmp / "share")
        client = Braiins(settings, Logger(settings))
        client.token = "tok"
        client.token_ts = time.time()
        with unittest.mock.patch("urllib.request.urlopen") as opened:
            for path in (
                "/api/v1/actions/reboot",
                "/api/v1/system/reboot",
                "/api/v1/actions/factory-reset",
                "/api/v1/actions/start",
                "/api/v1/actions/restart",
            ):
                with self.assertRaises(RuntimeError) as err:
                    client._call("PUT", path)
                self.assertIn("denied", str(err.exception).lower())
            with self.assertRaises(RuntimeError):
                client.start()
            with self.assertRaises(RuntimeError):
                client.restart()
            opened.assert_not_called()


class RecoveryStateMachineTests(unittest.TestCase):
    """0.1.7 bounded recovery. Fake clock only — no real multi-minute sleeps."""

    def test_defaults_keep_auto_fan_ceiling_off(self):
        s = Settings()
        self.assertFalse(s.auto_fan_ceiling_enabled)
        self.assertTrue(s.cooling_writes_only_when_paused)
        self.assertEqual(s.cooling_settle_seconds, 45)
        self.assertEqual(s.transition_poll_interval_seconds, 10)
        self.assertEqual(s.expected_recovery_seconds, 240)
        self.assertEqual(s.maximum_recovery_seconds, 600)
        self.assertEqual(s.post_retry_recovery_seconds, 180)
        self.assertEqual(s.stable_hash_poll_count, 3)
        self.assertEqual(s.telemetry_failures_before_error, 3)
        self.assertTrue(s.resume_retry_enabled)
        self.assertEqual(s.max_resume_retries_per_transaction, 1)
        self.assertTrue(s.coalesce_pending_cooling_requests)
        self.assertFalse(s.enable_writes)

    def test_1_plain_pause_resume_zero_watts_then_hashing(self):
        b = running_boards(["1"])
        b.lifecycle_after_resume = "cooldown"
        b.become_running_after_s = 185
        ctrl = make_controller(b, "ONE_BOARD")
        ok = ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertTrue(ok)
        self.assertGreaterEqual(ctrl._clock.t - b._resume_clock_at, 185)
        self.assertLess(ctrl._clock.t - b._resume_clock_at, 240)
        self.assertEqual(b.resume_calls, 1)
        self.assertNotIn("set_cooling_auto", b.write_names())
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertEqual(ctrl._health_class, "HASHING")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertEqual(ctrl.last_error, "")
        log = (ctrl.settings.data_dir / "controller.log").read_text()
        self.assertIn("power_w=0.0", log)
        self.assertIn("recovery_hashing", log)

    def test_2_cooling_while_paused_delayed_success(self):
        b = paused_one_board()
        b.lifecycle_after_resume = "cooldown"
        b.become_running_after_s = 480
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = _applied(100)
        ok = ctrl.request_cooling_ceiling(60, resume_mode="ONE_BOARD")
        self.assertTrue(ok)
        self.assertEqual(b.resume_calls, 1)
        self.assertGreaterEqual(ctrl._clock.t - b._resume_clock_at, 480)
        self.assertLess(ctrl._clock.t - b._resume_clock_at, 600)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertEqual(ctrl._health_class, "HASHING")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        puts = [c[1] for c in b.cooling_puts()]
        self.assertEqual(puts, [60])
        names = b.write_names()
        self.assertLess(names.index("set_cooling_auto"), names.index("resume"))

    def test_3_resume_http_500_then_legitimate_recovery_is_not_error(self):
        b = running_boards(["1"])
        b.resume_http = 500
        b.resume_500_enters_lifecycle = True
        b.lifecycle_after_resume = "cooldown"
        b.become_running_after_s = 185
        ctrl = make_controller(b, "ONE_BOARD")
        ok = ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertTrue(ok)
        self.assertEqual(b.resume_calls, 1)
        self.assertFalse(ctrl._resume_retry_used)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertEqual(ctrl._health_class, "HASHING")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertEqual(ctrl.last_error, "")

    def test_4_max_recovery_one_retry_then_hashing(self):
        b = running_boards(["1"])
        b.lifecycle_after_resume = "cooldown"
        b.run_on_resume_number = 2
        ctrl = make_controller(b, "ONE_BOARD")
        ok = ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertTrue(ok)
        self.assertEqual(b.resume_calls, 2)
        self.assertTrue(ctrl._resume_retry_used)
        self.assertGreaterEqual(ctrl._clock.t - b._resume_clock_at, 0)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertEqual(ctrl._health_class, "HASHING")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        log = (ctrl.settings.data_dir / "controller.log").read_text()
        self.assertIn("recovery_limit", log)
        self.assertIn("one guarded resume retry", log)

    def test_5_retry_fails_degraded_not_error(self):
        b = running_boards(["1"])
        b.lifecycle_after_resume = "cooldown"
        ctrl = make_controller(b, "ONE_BOARD")
        ok = ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertFalse(ok)
        self.assertEqual(b.resume_calls, 2)
        self.assertTrue(ctrl._resume_retry_used)
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertIn("degraded_needs_attention", ctrl.last_error)
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertNotIn("set_cooling_auto", b.write_names())

    def test_6_hard_fault_is_error_without_resume_retry(self):
        b = running_boards(["1"])
        b.hard_fault_after_resume = True
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ok = ctrl.tick()
        self.assertFalse(ok if ok is not None else False)
        self.assertEqual(b.resume_calls, 1)
        self.assertFalse(ctrl._resume_retry_used)
        self.assertEqual(ctrl.actual_mode, "ERROR")
        self.assertEqual(ctrl._health_class, "ERROR")
        self.assertIn("hard_fault", ctrl.last_error)
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        puts = [c[1] for c in b.cooling_puts()]
        self.assertEqual(puts, [60])

    def test_7_one_or_two_telemetry_timeouts_are_stale_not_error(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_txn_active = True
        ctrl._cooling_phase = "RECOVERING"
        ctrl._health_class = "RECOVERING"
        ctrl.actual_mode = "APPLYING"
        ctrl._note_telemetry_success()
        ctrl._note_telemetry_failure("recovery_primary")
        self.assertEqual(ctrl._telemetry_freshness, "STALE")
        self.assertEqual(ctrl._cooling_phase, "RECOVERING")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        ctrl._note_telemetry_failure("recovery_primary")
        self.assertEqual(ctrl._telemetry_fail_streak, 2)
        self.assertEqual(ctrl._telemetry_freshness, "STALE")
        self.assertEqual(ctrl._cooling_phase, "RECOVERING")
        self.assertEqual(ctrl._health_class, "RECOVERING")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertEqual(b.write_names(), [])

        b2 = running_boards(["1"])
        b2.boards_http = 500
        ctrl2 = make_controller(b2, "ONE_BOARD")
        ctrl2.tick()
        self.assertEqual(ctrl2._telemetry_freshness, "UNKNOWN")
        self.assertNotEqual(ctrl2.actual_mode, "ERROR")
        ctrl2.tick()
        self.assertEqual(ctrl2._telemetry_fail_streak, 2)
        self.assertNotEqual(ctrl2.actual_mode, "ERROR")
        self.assertEqual(ctrl2._health_class, "UNKNOWN")
        self.assertEqual(b2.write_names(), [])

    def test_8_sustained_telemetry_failure_errors_without_corrective_writes(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_txn_active = True
        ctrl._cooling_phase = "RECOVERING"
        ctrl._health_class = "RECOVERING"
        ctrl.actual_mode = "APPLYING"
        for _ in range(3):
            ctrl._note_telemetry_failure("recovery_primary")
        self.assertEqual(ctrl._cooling_phase, "RECOVERING")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertTrue(ctrl._telemetry_fault)
        self.assertEqual(b.write_names(), [])

        b2 = running_boards(["1"])
        b2.boards_http = 500
        ctrl2 = make_controller(b2, "ONE_BOARD")
        ctrl2.tick()
        ctrl2.tick()
        ctrl2.tick()
        self.assertEqual(ctrl2.actual_mode, "ERROR")
        self.assertEqual(ctrl2._health_class, "ERROR")
        self.assertEqual(ctrl2.last_error, "telemetry_sustained_unavailable")
        self.assertEqual(b2.write_names(), [])

    def test_9_same_value_cooling_is_noop(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = CoolingProfile("EXPLICIT", 70, None, None)
        ok = ctrl.request_cooling_ceiling(70)
        self.assertTrue(ok)
        self.assertNotIn("pause", b.write_names())
        self.assertNotIn("resume", b.write_names())
        self.assertNotIn("set_cooling_auto", b.write_names())
        self.assertEqual(ctrl._last_cooling_result, "noop")
        self.assertIn("lard_cooling_noop", [name for name, _payload in ctrl.ha.events])

    def test_10_multiple_ceilings_during_txn_keep_newest_only(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        orig_pause = b.pause

        def wrap_pause():
            if b.resume_calls == 0 and b.write_names().count("pause") == 0:
                ctrl.request_cooling_ceiling(70)
                ctrl.request_cooling_ceiling(80)
            return orig_pause()

        b.pause = wrap_pause  # type: ignore[method-assign]
        ctrl.tick()
        puts = [c[1] for c in b.cooling_puts()]
        self.assertEqual(puts, [60, 80])
        self.assertNotIn(70, puts)
        self.assertEqual(ctrl._health_class, "HASHING")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertIn("lard_cooling_coalesced", [name for name, _payload in ctrl.ha.events])

    def test_11_live_cooling_while_hashing_is_rejected(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.settings.auto_fan_ceiling_enabled = False
        ctrl._cooling_applied = _applied(100)
        ctrl.tick()
        self.assertNotIn("set_cooling_auto", b.write_names())
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        before = list(b.write_names())
        ctrl.tick()
        self.assertEqual(b.write_names(), before)
        self.assertIn("lard_cooling_deferred", [name for name, _payload in ctrl.ha.events])

        b2 = running_boards(["1"])
        ctrl2 = make_controller(b2, "ONE_BOARD")
        ctrl2.settings.auto_fan_ceiling_enabled = False
        ctrl2._cooling_applied = _applied(100)
        ok = ctrl2.request_cooling_ceiling(60, resume_mode="ONE_BOARD")
        self.assertTrue(ok)
        names = b2.write_names()
        self.assertLess(names.index("pause"), names.index("set_cooling_auto"))
        self.assertEqual(ctrl2._health_class, "HASHING")
        self.assertNotEqual(ctrl2.actual_mode, "ERROR")

        b3 = running_boards(["1"])
        ctrl3 = make_controller(b3, "ONE_BOARD")
        refused = ctrl3._apply_cooling_while_paused(CoolingProfile("EXPLICIT", 50, None, None))
        self.assertFalse(refused)
        self.assertNotIn("set_cooling_auto", b3.write_names())
        self.assertNotEqual(ctrl3.actual_mode, "ERROR")

    def test_12_unhealthy_board_is_not_full_hashing(self):
        b = running_boards(["1"])
        b.boards_healthy = False
        ctrl = make_controller(b, "ONE_BOARD")
        ok = ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertFalse(bool(ok))
        self.assertNotEqual(ctrl._health_class, "HASHING")
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertNotEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertFalse(ctrl._cooling_txn_active)
        self.assertEqual(b.resume_calls, 2)
        self.assertGreater(b.power_w, 10)
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertEqual(getattr(ok, "outcome", None), "degraded")
        self.assertTrue(getattr(ok, "closed", False))


def _forbid_control(test, braiins):
    names = braiins.write_names()
    test.assertNotIn("start", names)
    test.assertNotIn("restart", names)


class BlockerFixTests(unittest.TestCase):
    """B1–B4 regression tests. Fake clock only."""

    def _arm_ceiling(self, braiins, mode="ONE_BOARD", settle=0):
        ctrl = make_controller(braiins, mode)
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.settings.cooling_settle_seconds = settle
        return ctrl

    def _queue_pending_on_first_pause(self, ctrl, braiins, pct=80):
        orig = braiins.pause

        def wrap():
            if braiins.write_names().count("pause") == 0:
                ctrl.request_cooling_ceiling(pct)
            return orig()

        braiins.pause = wrap  # type: ignore[method-assign]

    def test_b1_pending_held_after_degraded_no_second_cycle(self):
        b = running_boards(["1"])
        b.lifecycle_after_resume = "cooldown"
        b.resume_sets_running = False
        ctrl = self._arm_ceiling(b)
        self._queue_pending_on_first_pause(ctrl, b, 80)
        ctrl.tick()
        puts = [c[1] for c in b.cooling_puts()]
        self.assertEqual(puts, [60])
        self.assertEqual(b.resume_calls, 2)
        self.assertEqual(ctrl._resume_retries_used, 1)
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertFalse(ctrl._cooling_txn_active)
        self.assertIsNotNone(ctrl._pending_profile)
        self.assertEqual(ctrl._pending_profile.max_fan_speed, 80)
        self.assertIn("pending_held", (ctrl.settings.data_dir / "controller.log").read_text())
        _forbid_control(self, b)
        before = list(b.write_names())
        ctrl._clock.sleep(600)
        ctrl.tick()
        self.assertEqual(b.write_names(), before)
        self.assertEqual(b.resume_calls, 2)
        self.assertEqual([c[1] for c in b.cooling_puts()], [60])
        self.assertEqual(ctrl._pending_profile.max_fan_speed, 80)
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")

    def test_b1_pending_held_after_cooling_put_error(self):
        b = running_boards(["1"])
        b.cooling_http = 400
        ctrl = self._arm_ceiling(b)
        self._queue_pending_on_first_pause(ctrl, b, 80)
        ctrl.tick()
        puts = [c[1] for c in b.cooling_puts()]
        self.assertIn(60, puts)
        self.assertNotIn(80, puts)
        self.assertEqual(b.resume_calls, 0)
        self.assertEqual(ctrl._health_class, "ERROR")
        self.assertEqual(ctrl.actual_mode, "ERROR")
        self.assertFalse(ctrl._cooling_txn_active)
        self.assertEqual(ctrl._pending_profile.max_fan_speed, 80)
        self.assertIn("pending_held", (ctrl.settings.data_dir / "controller.log").read_text())
        _forbid_control(self, b)
        before = list(b.write_names())
        resume_before = b.resume_calls
        ctrl._clock.sleep(300)
        ctrl.tick()
        self.assertEqual(b.write_names(), before)
        self.assertEqual(b.resume_calls, resume_before)
        self.assertNotIn(80, [c[1] for c in b.cooling_puts()])
        self.assertEqual(ctrl._pending_profile.max_fan_speed, 80)

    def test_b1_stale_callback_does_not_apply_after_error(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl._health_class = "ERROR"
        ctrl._cooling_terminal_kind = "error"
        ctrl._cooling_terminal_gen = 4
        ctrl._pending_profile = CoolingProfile("EXPLICIT", 80, None, None)
        ctrl._pending_explicit = True
        before = list(b.write_names())
        result = ctrl._gated_cooling_transition(
            ctrl._pending_profile, "ONE_BOARD", _depth=1, _apply_gen=4
        )
        self.assertFalse(bool(result))
        self.assertEqual(getattr(result, "outcome", None), "refused")
        self.assertEqual(b.write_names(), before)
        self.assertEqual(b.resume_calls, 0)
        self.assertEqual(b.cooling_puts(), [])
        self.assertEqual(ctrl._pending_profile.max_fan_speed, 80)

    def test_b1_pending_still_applies_after_clean_hashing(self):
        b = running_boards(["1"])
        ctrl = self._arm_ceiling(b)
        self._queue_pending_on_first_pause(ctrl, b, 80)
        ctrl.tick()
        puts = [c[1] for c in b.cooling_puts()]
        self.assertEqual(puts, [60, 80])
        self.assertEqual(ctrl._health_class, "HASHING")
        self.assertIsNone(ctrl._pending_profile)
        _forbid_control(self, b)

    def _assert_recovery_deadline(self, ctrl, braiins):
        origin = getattr(braiins, "_first_resume_clock_at", None) or braiins._resume_clock_at
        span = ctrl._clock.t - origin
        self.assertGreaterEqual(span, 600 + 180)
        self.assertLess(span, 600 + 180 + 40)
        self.assertEqual(
            ctrl._primary_recovery_deadline_ts - ctrl._primary_recovery_started_ts,
            600,
        )
        log = (ctrl.settings.data_dir / "controller.log").read_text()
        self.assertEqual(log.count("recovery_begin label=primary"), 1)
        self.assertEqual(log.count("recovery_limit label=primary"), 1)
        self.assertEqual(log.count("primary_deadline_kept"), 1)
        self.assertIn("txn_active=True", log)
        self.assertNotIn("recovery_hashing", log)
        return log

    def test_b2_running_zero_watts_reaches_degraded_not_success(self):
        b = running_boards(["1"])
        b.resume_sets_running = False
        b.running_zero_on_resume = True
        ctrl = make_controller(b, "ONE_BOARD")
        ok = ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertFalse(bool(ok))
        self.assertEqual(ok.outcome, "degraded")
        self.assertTrue(ok.closed)
        self.assertEqual(b.resume_calls, 2)
        self.assertEqual(ctrl._resume_retries_used, 1)
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertNotEqual(ctrl._health_class, "HASHING")
        self.assertFalse(ctrl._cooling_txn_active)
        self.assertEqual(b.power_w, 0.0)
        origin = getattr(b, "_first_resume_clock_at", None) or b._resume_clock_at
        span = ctrl._clock.t - origin
        # Running at 0 W has no named lifecycle token, so the window stops at
        # the 240s expected bound, then one retry and the 180s post-retry bound.
        self.assertGreaterEqual(span, 240 + 180)
        self.assertLess(span, 240 + 180 + 40)
        self.assertEqual(
            ctrl._primary_recovery_deadline_ts - ctrl._primary_recovery_started_ts,
            600,
        )
        log = (ctrl.settings.data_dir / "controller.log").read_text()
        self.assertIn("recovery_no_progress", log)
        self.assertNotIn("recovery_hashing", log)
        self.assertIn("recovery_interim", log)
        self.assertIn("interim=operational", log)
        _forbid_control(self, b)
        self.assertNotIn("set_cooling_auto", b.write_names())

    def test_b2_persistent_applying_reaches_degraded(self):
        b = running_boards(["1"])
        b.resume_sets_running = False
        b.lifecycle_after_resume = "ramping"
        b.lifecycle_power_w = 5.0
        b.lifecycle_hashrate = 0.0
        b.power_step = 5.0
        ctrl = make_controller(b, "ONE_BOARD")
        ok = ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertFalse(bool(ok))
        self.assertEqual(ok.outcome, "degraded")
        self.assertEqual(b.resume_calls, 2)
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertNotEqual(ctrl._health_class, "HASHING")
        self.assertFalse(ctrl._cooling_txn_active)
        log = self._assert_recovery_deadline(ctrl, b)
        self.assertIn("interim=applying", log)
        _forbid_control(self, b)

    def test_b2_unhealthy_board_with_watts_degrades(self):
        b = running_boards(["1"])
        b.boards_healthy = False
        b.power_w = 500.0
        b.hashrate = 25.0
        ctrl = make_controller(b, "ONE_BOARD")
        ok = ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertFalse(bool(ok))
        self.assertEqual(ok.outcome, "degraded")
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertGreater(b.power_w, 10)
        self.assertGreater(b.hashrate, 1)
        self.assertEqual(b.resume_calls, 2)
        self.assertFalse(ctrl._cooling_txn_active)
        _forbid_control(self, b)

    def test_b2_later_three_healthy_polls_still_hash(self):
        b = running_boards(["1"])
        b.lifecycle_after_resume = "cooldown"
        b.become_running_after_s = 185
        ctrl = make_controller(b, "ONE_BOARD")
        ok = ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertTrue(bool(ok))
        self.assertEqual(ok.outcome, "hashing")
        self.assertEqual(ctrl._health_class, "HASHING")
        self.assertEqual(b.resume_calls, 1)
        self.assertGreaterEqual(ctrl._clock.t - b._resume_clock_at, 185)
        self.assertLess(ctrl._clock.t - b._resume_clock_at, 240)

    def _after_terminal_is_quiet(self, ctrl, braiins):
        before = list(braiins.write_names())
        resumes = braiins.resume_calls
        puts = list(braiins.cooling_puts())
        ctrl._clock.sleep(600)
        ctrl.tick()
        self.assertEqual(braiins.write_names(), before)
        self.assertEqual(braiins.resume_calls, resumes)
        self.assertEqual(braiins.cooling_puts(), puts)
        _forbid_control(self, braiins)

    def test_b3_hard_fault_during_settle_aborts_before_resume(self):
        b = running_boards(["1"])
        ctrl = self._arm_ceiling(b, settle=45)
        self.assertEqual(ctrl._settle_seconds(), 45)

        def fault_when(fake):
            if ctrl._cooling_phase == "COOLING_SETTLING" and not ctrl._settle_complete:
                fake._enter_hard_fault()

        b.fault_when = fault_when
        self._queue_pending_on_first_pause(ctrl, b, 80)
        ctrl.tick()
        self.assertEqual(b.resume_calls, 0)
        self.assertEqual(ctrl._resume_retries_used, 0)
        self.assertFalse(ctrl._resume_retry_used)
        self.assertEqual(ctrl._health_class, "ERROR")
        self.assertEqual(ctrl.actual_mode, "ERROR")
        self.assertIn("hard_fault", ctrl.last_error)
        self.assertEqual([c[1] for c in b.cooling_puts()], [60])
        self.assertEqual(ctrl._pending_profile.max_fan_speed, 80)
        self.assertFalse(ctrl._cooling_txn_active)
        _forbid_control(self, b)
        self._after_terminal_is_quiet(ctrl, b)

    def test_b3_thermal_fault_during_settle_aborts_before_resume(self):
        b = running_boards(["1"])
        ctrl = self._arm_ceiling(b, settle=45)

        def fault_when(fake):
            if ctrl._cooling_phase == "COOLING_SETTLING" and not ctrl._settle_complete:
                fake.paused = True
                fake.running = False
                fake.user_paused = False
                fake.phase = "stopped"
                fake.status = "thermal_fault"
                fake.pause_reason = "thermal_fault"
                fake.power_w = 0.0

        b.fault_when = fault_when
        ctrl.tick()
        self.assertEqual(b.resume_calls, 0)
        self.assertEqual(ctrl._health_class, "ERROR")
        self.assertIn("hard_fault", ctrl.last_error)
        self.assertEqual([c[1] for c in b.cooling_puts()], [60])
        _forbid_control(self, b)
        self._after_terminal_is_quiet(ctrl, b)

    def test_b3_fault_before_put_issues_no_cooling_command(self):
        b = running_boards(["1"])
        b._enter_hard_fault()
        ctrl = self._arm_ceiling(b, settle=45)
        ctrl.tick()
        self.assertEqual(b.resume_calls, 0)
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("pause", b.write_names())
        self.assertEqual(ctrl._health_class, "ERROR")
        _forbid_control(self, b)
        self._after_terminal_is_quiet(ctrl, b)

    def test_b3_fault_immediately_before_first_resume(self):
        b = running_boards(["1"])
        ctrl = self._arm_ceiling(b, settle=45)

        def fault_when(fake):
            if ctrl._resume_gate == "primary":
                fake._enter_hard_fault()

        b.fault_when = fault_when
        ctrl.tick()
        self.assertEqual(b.resume_calls, 0)
        self.assertEqual(ctrl._resume_retries_used, 0)
        self.assertEqual(ctrl._health_class, "ERROR")
        self.assertEqual([c[1] for c in b.cooling_puts()], [60])
        self.assertGreaterEqual(ctrl._clock.t - b._cooling_put_clock_at, 45)
        _forbid_control(self, b)
        self._after_terminal_is_quiet(ctrl, b)

    def test_b3_fault_during_recovery_before_retry(self):
        b = running_boards(["1"])
        b.lifecycle_after_resume = "cooldown"
        b.resume_sets_running = False
        ctrl = self._arm_ceiling(b, settle=45)

        def fault_when(fake):
            if (
                ctrl._cooling_phase == "RECOVERING"
                and ctrl._recovery_elapsed_s >= 30
                and fake.resume_calls == 1
                and not ctrl._resume_retry_used
            ):
                fake._enter_hard_fault()

        b.fault_when = fault_when
        ctrl.tick()
        self.assertEqual(b.resume_calls, 1)
        self.assertEqual(ctrl._resume_retries_used, 0)
        self.assertEqual(ctrl._health_class, "ERROR")
        self.assertEqual([c[1] for c in b.cooling_puts()], [60])
        _forbid_control(self, b)
        self._after_terminal_is_quiet(ctrl, b)

    def test_b3_fault_immediately_before_retry_resume(self):
        b = running_boards(["1"])
        b.lifecycle_after_resume = "cooldown"
        b.resume_sets_running = False
        ctrl = self._arm_ceiling(b, settle=45)

        def fault_when(fake):
            if ctrl._resume_gate == "retry":
                fake._enter_hard_fault()

        b.fault_when = fault_when
        ctrl.tick()
        self.assertEqual(b.resume_calls, 1)
        self.assertEqual(ctrl._resume_retries_used, 0)
        self.assertFalse(ctrl._resume_retry_used)
        self.assertEqual(ctrl._health_class, "ERROR")
        self.assertEqual([c[1] for c in b.cooling_puts()], [60])
        _forbid_control(self, b)
        self._after_terminal_is_quiet(ctrl, b)

    def test_b3_stale_telemetry_is_not_a_hard_fault_and_not_blind_resume(self):
        b = running_boards(["1"])
        ctrl = self._arm_ceiling(b, settle=45)
        orig = b.mining_state

        def mining_state(details=None):
            if ctrl._cooling_phase in {"COOLING_SETTLING", "RECOVERING", "RETRY_RESUME_ONCE"} or ctrl._resume_gate in {
                "primary",
                "retry",
            }:
                b.calls.append(("mining_state",))
                return {}, 500, {}
            return orig(details)

        b.mining_state = mining_state  # type: ignore[method-assign]
        ctrl.tick()
        self.assertEqual(b.resume_calls, 0)
        self.assertNotEqual(ctrl._health_class, "HASHING")
        self.assertNotIn("hard_fault", ctrl.last_error)
        _forbid_control(self, b)

    def test_b4_live_shape_without_board_health_does_not_hash_on_watts(self):
        b = running_boards(["1"])
        b.power_w = 1200.0
        b.hashrate = 50.0
        b.board_health = None
        ctrl = make_controller(b, "ONE_BOARD")
        obs = ctrl.observe_miner()
        self.assertFalse(obs.boards_healthy)
        self.assertFalse(obs.board_health_verified)
        self.assertFalse(ctrl._hashing_sample_ok(obs, "ONE_BOARD"))
        ok = ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertFalse(bool(ok))
        self.assertNotEqual(ctrl._health_class, "HASHING")
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertEqual(b.resume_calls, 2)
        self.assertGreater(b.power_w, 10)
        _forbid_control(self, b)

    def test_b4_three_healthy_boards_and_three_polls_hash(self):
        b = running_boards(["1", "2", "3"])
        ctrl = make_controller(b, "THREE_BOARD")
        obs = ctrl.observe_miner()
        self.assertTrue(obs.board_health_verified)
        self.assertTrue(obs.boards_healthy)
        self.assertEqual(len(obs.board_reports), 3)
        self.assertTrue(ctrl._hashing_sample_ok(obs, "THREE_BOARD"))
        ok = ctrl.run_plain_pause_resume("THREE_BOARD")
        self.assertTrue(bool(ok))
        self.assertEqual(ok.outcome, "hashing")
        self.assertEqual(ctrl._health_class, "HASHING")
        self.assertEqual(ctrl.actual_mode, "THREE_BOARD")
        self.assertEqual(b.resume_calls, 1)

    def test_b4_two_of_three_boards_never_hash(self):
        b = running_boards(["1", "2"])
        b.power_w = 1600.0
        b.hashrate = 70.0
        ctrl = make_controller(b, "THREE_BOARD")
        obs = ctrl.observe_miner()
        self.assertFalse(ctrl._hashing_sample_ok(obs, "THREE_BOARD"))
        self.assertEqual(len(obs.board_reports), 2)
        ok = ctrl.run_plain_pause_resume("THREE_BOARD")
        self.assertFalse(bool(ok))
        self.assertNotEqual(ctrl._health_class, "HASHING")
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertEqual(b.resume_calls, 2)
        self.assertGreater(b.power_w, 10)
        _forbid_control(self, b)

    def test_b4_present_unhealthy_board_never_hashes(self):
        b = running_boards(["1", "2", "3"])
        b.unhealthy_ids = ["2"]
        b.power_w = 1500.0
        b.hashrate = 60.0
        ctrl = make_controller(b, "THREE_BOARD")
        obs = ctrl.observe_miner()
        self.assertFalse(obs.boards_healthy)
        self.assertFalse(ctrl._hashing_sample_ok(obs, "THREE_BOARD"))
        ok = ctrl.run_plain_pause_resume("THREE_BOARD")
        self.assertFalse(bool(ok))
        self.assertNotEqual(ctrl._health_class, "HASHING")
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        _forbid_control(self, b)

    def test_b4_missing_malformed_stale_incomplete_do_not_hash(self):
        cases = []
        missing = running_boards(["1"])
        missing.omit_board_telemetry = True
        cases.append(missing)
        malformed = running_boards(["1"])
        malformed.hashboards_malformed = True
        cases.append(malformed)
        stale = running_boards(["1"])
        stale.board_stale = True
        cases.append(stale)
        incomplete = running_boards(["1"])
        incomplete.incomplete_boards = True
        cases.append(incomplete)
        for braiins in cases:
            ctrl = make_controller(braiins, "ONE_BOARD")
            obs = ctrl.observe_miner()
            self.assertFalse(ctrl._hashing_sample_ok(obs, "ONE_BOARD"), braiins.board_health_reason if False else obs.board_health_reason)
            self.assertFalse(obs.boards_healthy)

    def test_b4_invalid_sample_resets_stable_count(self):
        b = running_boards(["1"])
        b.board_health_when_running = ["ok", "ok", "bad", "ok", "ok", "ok"]
        ctrl = make_controller(b, "ONE_BOARD")
        ok = ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertTrue(bool(ok))
        self.assertEqual(ok.outcome, "hashing")
        log = (ctrl.settings.data_dir / "controller.log").read_text()
        self.assertGreaterEqual(log.count("stable=1/3"), 2)
        self.assertEqual(log.count("stable=3/3"), 1)
        bad_at = log.find("boards_healthy=False")
        self.assertGreater(bad_at, log.find("stable=2/3"))
        self.assertGreater(log.find("stable=3/3"), bad_at)

    def test_b4_cached_healthy_then_unavailable_is_not_hashing(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        first = ctrl.observe_miner()
        self.assertTrue(ctrl._hashing_sample_ok(first, "ONE_BOARD"))
        b.board_health_script = [
            {
                "verified": False,
                "healthy": False,
                "stale": True,
                "malformed": False,
                "boards": [{"id": "1", "proven_healthy": True}],
                "reason": "unavailable",
                "safety_fault": "",
            }
        ]
        second = ctrl.observe_miner()
        self.assertFalse(second.board_health_verified)
        self.assertFalse(second.boards_healthy)
        self.assertFalse(ctrl._hashing_sample_ok(second, "ONE_BOARD"))
        self.assertTrue(first.boards_healthy)

    def test_b4_live_client_parses_hashboards_and_errors_fail_closed(self):
        tmp = Path(tempfile.mkdtemp(prefix="lard-board-health-"))
        settings = Settings(braiins_password="x", data_dir=tmp, share_dir=tmp / "share")
        client = Braiins(settings, Logger(settings))

        def fail_call(method, path, body=None, timeout=30):
            return 500, {}

        client._call = fail_call  # type: ignore[method-assign]
        failed = client.board_health()
        self.assertFalse(failed["verified"])
        self.assertFalse(failed["healthy"])
        self.assertTrue(failed["stale"])

        payload = {
            "hashboards": [
                {"id": "1", "enabled": True, "chips_count": 126},
                {"id": "2", "enabled": True, "chips_count": {"value": 110}},
                {"id": "3", "is_enabled": True, "chips_count": 98, "healthy": True},
            ]
        }

        def ok_call(method, path, body=None, timeout=30):
            if str(path).endswith("/hashboards"):
                return 200, payload
            if str(path).endswith("/errors"):
                return 200, {"errors": []}
            return 404, {}

        client._call = ok_call  # type: ignore[method-assign]
        info = client.board_health()
        self.assertTrue(info["verified"])
        self.assertTrue(info["healthy"])
        self.assertEqual(info["reported_ids"], ["1", "2", "3"])
        self.assertNotEqual(len(info["reported_ids"]), 0)

        def fault_call(method, path, body=None, timeout=30):
            if str(path).endswith("/hashboards"):
                return 200, payload
            if str(path).endswith("/errors"):
                return 200, {
                    "errors": [
                        {
                            "message": "PSU fault",
                            "error_codes": [{"code": "psu_fault", "reason": "psu_fault"}],
                            "components": [{"name": "psu", "index": 0}],
                        }
                    ]
                }
            return 404, {}

        client._call = fault_call  # type: ignore[method-assign]
        faulted = client.board_health()
        self.assertFalse(faulted["healthy"])
        self.assertIn("psu_fault", faulted["safety_fault"])
        partial = parse_board_health_payload(
            {"hashboards": payload["hashboards"][:2]},
            [],
            errors_checked=True,
        )
        self.assertEqual(len(partial["reported_ids"]), 2)
        self.assertTrue(partial["healthy"])
        ctrl = make_controller(running_boards(["1", "2"]), "THREE_BOARD")
        obs = ctrl.observe_miner()
        self.assertFalse(ctrl._expected_boards_proven(obs, "THREE_BOARD"))
        self.assertFalse(ctrl._hashing_sample_ok(obs, "THREE_BOARD"))

    def test_escalate_helpers_cannot_command(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        before = list(b.write_names())
        self.assertFalse(ctrl._cooling_escalate_start())
        self.assertFalse(ctrl._cooling_escalate_restart())
        self.assertEqual(b.write_names(), before)
        self.assertEqual(b.resume_calls, 0)
        _forbid_control(self, b)
        log = (ctrl.settings.data_dir / "controller.log").read_text()
        self.assertEqual(log.count("escalate_blocked"), 2)
        self.assertNotIn("actions/start", log)
        self.assertNotIn("actions/restart", log)


class WriteEnableHardeningTests(unittest.TestCase):
    """H1–H7. Fake clock and barriers only — no real sleeps, no live Braiins."""

    def _arm(self, braiins, settle=0):
        ctrl = make_controller(braiins, "ONE_BOARD")
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.settings.cooling_settle_seconds = settle
        return ctrl

    def _events(self, ctrl):
        return [name for name, _payload in ctrl.ha.events]

    def test_h1_disarm_before_pause_leaves_write_log_empty(self):
        b = running_boards(["1"])
        ctrl = self._arm(b)
        orig = ctrl.observe_miner

        def observe():
            obs = orig()
            if ctrl._cooling_phase == "PAUSE_REQUESTED":
                ctrl.settings.enable_writes = False
            return obs

        ctrl.observe_miner = observe  # type: ignore[method-assign]
        ctrl.tick()
        self.assertEqual(b.write_names(), [])
        self.assertEqual(b.cooling_puts(), [])
        self.assertEqual(b.resume_calls, 0)
        self.assertIn("lard_write_denied", self._events(ctrl))
        _forbid_control(self, b)

    def test_h1_disarm_before_put_and_resume_and_retry(self):
        b = running_boards(["1"])
        b.lifecycle_after_resume = "cooldown"
        b.resume_sets_running = False
        ctrl = self._arm(b)
        orig = ctrl.observe_miner

        def observe():
            obs = orig()
            if ctrl._cooling_phase == "COOLING_APPLYING":
                ctrl.settings.enable_writes = False
            return obs

        ctrl.observe_miner = observe  # type: ignore[method-assign]
        ctrl.tick()
        self.assertNotIn("set_cooling_auto", b.write_names())
        self.assertEqual(b.cooling_puts(), [])
        self.assertEqual(b.resume_calls, 0)
        self.assertIn("lard_write_denied", self._events(ctrl))
        _forbid_control(self, b)

        b2 = running_boards(["1"])
        b2.lifecycle_after_resume = "cooldown"
        b2.resume_sets_running = False
        ctrl2 = self._arm(b2)
        orig2 = ctrl2.observe_miner

        def observe_resume():
            obs = orig2()
            if ctrl2._resume_gate == "primary":
                ctrl2.settings.enable_writes = False
            return obs

        ctrl2.observe_miner = observe_resume  # type: ignore[method-assign]
        before_puts = 0
        ctrl2.tick()
        self.assertEqual(b2.resume_calls, 0)
        self.assertEqual(len(b2.cooling_puts()), 1)
        self.assertGreater(len(b2.write_names()), before_puts)
        self.assertNotIn("resume", b2.write_names())
        _forbid_control(self, b2)

        b3 = running_boards(["1"])
        b3.running_zero_on_resume = True
        b3.resume_sets_running = False
        ctrl3 = self._arm(b3)
        orig3 = ctrl3.observe_miner

        def observe_retry():
            obs = orig3()
            if ctrl3._resume_gate == "retry":
                ctrl3.settings.enable_writes = False
            return obs

        ctrl3.observe_miner = observe_retry  # type: ignore[method-assign]
        ctrl3.tick()
        self.assertEqual(b3.resume_calls, 1)
        self.assertEqual(ctrl3._resume_retries_used, 0)
        self.assertNotIn("start", b3.write_names())
        self.assertNotIn("restart", b3.write_names())

    def test_h1_txn_id_terminal_and_stale_block_put_or_resume(self):
        b = running_boards(["1"])
        ctrl = self._arm(b)
        orig = ctrl.observe_miner

        def tamper():
            obs = orig()
            if ctrl._cooling_phase == "COOLING_APPLYING":
                ctrl._cooling_txn_id = "cool-tampered"
            return obs

        ctrl.observe_miner = tamper  # type: ignore[method-assign]
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        self.assertEqual(b.resume_calls, 0)
        self.assertIn("lard_write_denied", self._events(ctrl))
        _forbid_control(self, b)

        b2 = running_boards(["1"])
        ctrl2 = self._arm(b2)
        orig2 = ctrl2.observe_miner

        def terminal():
            obs = orig2()
            if ctrl2._resume_gate == "primary":
                ctrl2._health_class = "ERROR"
                ctrl2._cooling_terminal_kind = "error"
            return obs

        ctrl2.observe_miner = terminal  # type: ignore[method-assign]
        ctrl2.tick()
        self.assertEqual(b2.resume_calls, 0)
        self.assertEqual(len(b2.cooling_puts()), 1)
        self.assertNotIn("resume", b2.write_names())
        _forbid_control(self, b2)

        b3 = running_boards(["1"])
        ctrl3 = self._arm(b3)
        orig3 = ctrl3.observe_miner

        def stale():
            obs = orig3()
            if ctrl3._cooling_phase == "COOLING_APPLYING":
                ctrl3._telemetry_freshness = "STALE"
            return obs

        ctrl3.observe_miner = stale  # type: ignore[method-assign]
        ctrl3.tick()
        self.assertEqual(b3.cooling_puts(), [])
        self.assertEqual(b3.resume_calls, 0)
        self.assertNotIn("set_cooling_auto", b3.write_names())
        _forbid_control(self, b3)

    def test_h2_recovery_and_reload_never_call_start_or_restart(self):
        b = running_boards(["1"])
        b.lifecycle_after_resume = "cooldown"
        b.resume_sets_running = False
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertFalse(ctrl._cooling_escalate_start())
        self.assertFalse(ctrl._cooling_escalate_restart())
        ctrl.reconcile_after_reload()
        ctrl.tick()
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())

    def test_h3_named_tokens_only(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")

        def obs(**kwargs):
            base = dict(ok=True, details_ok=True, enabled_ids=["1"], boards_healthy=True)
            base.update(kwargs)
            return MinerObservation(**base)

        self.assertFalse(ctrl._positive_lifecycle(obs(running=True, phase="running", power_w=0)))
        self.assertFalse(ctrl._positive_lifecycle(obs(phase="mystery", paused=False, running=False)))
        self.assertFalse(ctrl._positive_lifecycle(obs(phase="reinitializing")))
        self.assertTrue(ctrl._positive_lifecycle(obs(phase="init")))
        self.assertTrue(ctrl._positive_lifecycle(obs(phase="cooldown")))
        self.assertTrue(ctrl._positive_lifecycle(obs(phase="preheat", preheating=True)))
        # "applying" is a controller label, not miner-side lifecycle evidence.
        self.assertFalse(ctrl._positive_lifecycle(obs(phase="applying", power_w=0.0, hashrate=0.0)))
        self.assertTrue(ctrl._positive_lifecycle(obs(phase="ramping", ramping=True)))
        self.assertFalse(
            ctrl._positive_lifecycle(obs(phase="preheat", pause_reason="overheat", preheating=True))
        )
        self.assertTrue(ctrl._hard_fault(obs(phase="preheat", pause_reason="overheat")))

        unknown = running_boards(["1"])
        unknown.resume_sets_running = False
        unknown.lifecycle_after_resume = "mystery"
        ctrl_u = make_controller(unknown, "ONE_BOARD")
        ok = ctrl_u.run_plain_pause_resume("ONE_BOARD")
        self.assertFalse(bool(ok))
        self.assertEqual(ok.outcome, "degraded")
        self.assertEqual(unknown.resume_calls, 2)
        origin = unknown._first_resume_clock_at
        span = ctrl_u._clock.t - origin
        self.assertGreaterEqual(span, 240 + 180)
        self.assertLess(span, 240 + 180 + 40)
        self.assertEqual(ctrl_u._health_class, "DEGRADED_NEEDS_ATTENTION")
        _forbid_control(self, unknown)

    def test_h4_wall_jump_does_not_move_monotonic_window(self):
        b = running_boards(["1"])
        b.lifecycle_after_resume = "cooldown"
        b.run_on_resume_number = 2
        ctrl = make_controller(b, "ONE_BOARD")
        wall = {"t": 1_700_000_000.0}
        orig_sleep = ctrl._sleep

        def sleep(seconds):
            wall["t"] += 50_000.0
            return orig_sleep(seconds)

        ctrl._sleep = sleep  # type: ignore[method-assign]
        with unittest.mock.patch("time.time", lambda: wall["t"]):
            ok = ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertTrue(bool(ok))
        self.assertEqual(b.resume_calls, 2)
        self.assertGreaterEqual(ctrl._clock.t - b._first_resume_clock_at, 600)
        self.assertLess(ctrl._clock.t - b._first_resume_clock_at, 640)
        _forbid_control(self, b)

    def test_h4_boundaries_239_240_599_600_780(self):
        b = running_boards(["1"])
        b.resume_sets_running = False
        b.running_zero_on_resume = True
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.settings.transition_poll_interval_seconds = 1
        snaps = {}
        orig = b.mining_state

        def mining_state(details=None):
            parsed = orig(details)
            if ctrl._cooling_phase == "RECOVERING":
                snaps[int(ctrl._recovery_elapsed_s)] = (
                    b.resume_calls,
                    ctrl._resume_retries_used,
                    ctrl._health_class,
                )
            return parsed

        b.mining_state = mining_state  # type: ignore[method-assign]
        ok = ctrl.run_plain_pause_resume("ONE_BOARD")
        self.assertFalse(bool(ok))
        self.assertEqual(ok.outcome, "degraded")
        self.assertIn(239, snaps)
        self.assertEqual(snaps[239][0], 1)
        self.assertEqual(snaps[239][1], 0)
        self.assertEqual(snaps[239][2], "RECOVERING")
        self.assertEqual(b.resume_calls, 2)
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")
        span = ctrl._clock.t - b._first_resume_clock_at
        self.assertGreaterEqual(span, 240 + 180)
        self.assertLess(span, 240 + 180 + 5)
        _forbid_control(self, b)

        b2 = running_boards(["1"])
        b2.resume_sets_running = False
        b2.lifecycle_after_resume = "cooldown"
        ctrl2 = make_controller(b2, "ONE_BOARD")
        ctrl2.settings.transition_poll_interval_seconds = 1
        snaps2 = {}
        orig2 = b2.mining_state

        def mining_state2(details=None):
            parsed = orig2(details)
            if ctrl2._cooling_phase == "RECOVERING" and not ctrl2._resume_retry_used:
                snaps2[int(ctrl2._recovery_elapsed_s)] = b2.resume_calls
            return parsed

        b2.mining_state = mining_state2  # type: ignore[method-assign]
        ok2 = ctrl2.run_plain_pause_resume("ONE_BOARD")
        self.assertFalse(bool(ok2))
        self.assertEqual(snaps2.get(599), 1)
        self.assertEqual(b2.resume_calls, 2)
        self.assertEqual(ctrl2._health_class, "DEGRADED_NEEDS_ATTENTION")
        span2 = ctrl2._clock.t - b2._first_resume_clock_at
        self.assertGreaterEqual(span2, 600 + 180)
        self.assertLess(span2, 600 + 180 + 5)
        self.assertEqual(
            ctrl2._primary_recovery_deadline_ts - ctrl2._primary_recovery_started_ts,
            600,
        )
        _forbid_control(self, b2)

    def test_h5_cancel_reload_and_stale_txn_issue_no_command(self):
        b = running_boards(["1"])
        b.resume_sets_running = False
        b.running_zero_on_resume = True
        ctrl = make_controller(b, "ONE_BOARD")
        ready = threading.Barrier(2)
        released = threading.Event()
        orig = b.mining_state

        def mining_state(details=None):
            parsed = orig(details)
            if (
                ctrl._cooling_phase == "RECOVERING"
                and b.resume_calls == 1
                and ctrl._recovery_elapsed_s >= 230
                and not getattr(ctrl, "_cancel_hooked", False)
            ):
                ctrl._cancel_hooked = True
                ready.wait(timeout=5)
                self.assertTrue(released.wait(timeout=5))
            return parsed

        b.mining_state = mining_state  # type: ignore[method-assign]

        def other():
            ready.wait(timeout=5)
            ctrl.cancel_cooling_transaction("test-cancel")
            released.set()

        thread = threading.Thread(target=other)
        thread.start()
        ctrl.run_plain_pause_resume("ONE_BOARD")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(b.resume_calls, 1)
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertEqual(ctrl._health_class, "INTERRUPTED_MANUAL_REVIEW")

        b2 = paused_one_board()
        ctrl2 = make_controller(b2, "ONE_BOARD", enable_writes=True)
        marker = {
            "txn_id": "cool-old",
            "phase": "COOLING_SETTLING",
            "monotonic_deadline": 999999,
            "resume_on_load": True,
        }
        (ctrl2.settings.data_dir / "cooling_txn_inflight.json").write_text(json.dumps(marker))
        self.assertEqual(ctrl2.reconcile_after_reload(), "interrupted")
        self.assertEqual(ctrl2._primary_recovery_deadline_ts, 0.0)
        self.assertIsNone(ctrl2._pending_profile)
        self.assertEqual(b2.write_names(), [])
        ctrl2.tick()
        self.assertEqual(b2.write_names(), [])
        self.assertEqual(ctrl2._health_class, "INTERRUPTED_MANUAL_REVIEW")
        denied = ctrl2._call_device("resume", lambda: b2.resume())
        self.assertIsNone(denied)
        self.assertEqual(b2.write_names(), [])
        self.assertEqual(b2.resume_calls, 0)

    def test_h6_one_owner_under_concurrent_pressure(self):
        b = running_boards(["1"])
        b.resume_sets_running = False
        b.lifecycle_after_resume = "cooldown"
        ctrl = self._arm(b, settle=45)
        ready = threading.Barrier(2)
        released = threading.Event()
        orig_sleep = ctrl._sleep
        depths = {"cur": 0, "max": 0}
        depth_lock = threading.Lock()

        def wrap(name, fn):
            def wrapped(*args, **kwargs):
                with depth_lock:
                    depths["cur"] += 1
                    depths["max"] = max(depths["max"], depths["cur"])
                try:
                    return fn(*args, **kwargs)
                finally:
                    with depth_lock:
                        depths["cur"] -= 1

            return wrapped

        b.pause = wrap("pause", b.pause)  # type: ignore[method-assign]
        b.resume = wrap("resume", b.resume)  # type: ignore[method-assign]
        b.set_cooling_auto = wrap("put", b.set_cooling_auto)  # type: ignore[method-assign]

        def sleep(seconds):
            if ctrl._cooling_phase == "COOLING_SETTLING" and not getattr(ctrl, "_settle_hooked", False):
                ctrl._settle_hooked = True
                ready.wait(timeout=5)
                self.assertTrue(released.wait(timeout=5))
            return orig_sleep(seconds)

        ctrl._sleep = sleep  # type: ignore[method-assign]

        def other():
            ready.wait(timeout=5)
            ctrl.request_cooling_ceiling(70)
            ctrl.request_cooling_ceiling(80)
            ctrl.tick()
            released.set()

        thread = threading.Thread(target=other)
        thread.start()
        ctrl.tick()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([c[1] for c in b.cooling_puts()], [60])
        self.assertIsNotNone(ctrl._pending_profile)
        self.assertEqual(ctrl._pending_profile.max_fan_speed, 80)
        self.assertEqual(depths["max"], 1)
        self.assertEqual(ctrl._emit_overlap_violations, 0)
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertEqual(ctrl._health_class, "DEGRADED_NEEDS_ATTENTION")

    def test_h6_cancel_at_retry_and_telemetry_withhold_resume(self):
        b = running_boards(["1"])
        b.resume_sets_running = False
        b.running_zero_on_resume = True
        ctrl = make_controller(b, "ONE_BOARD")
        ready = threading.Barrier(2)
        released = threading.Event()
        orig_gate = ctrl._gate_resume

        def gate(label):
            if label == "retry":
                ready.wait(timeout=5)
                self.assertTrue(released.wait(timeout=5))
            return orig_gate(label)

        ctrl._gate_resume = gate  # type: ignore[method-assign]

        def other():
            ready.wait(timeout=5)
            b.details_http = 500
            ctrl._note_telemetry_failure("retry_boundary")
            released.set()

        thread = threading.Thread(target=other)
        thread.start()
        ctrl.run_plain_pause_resume("ONE_BOARD")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(b.resume_calls, 1)
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())

    def test_h7_migration_cannot_arm_writes_or_pending(self):
        cases = [
            {},
            {"enable_writes": None, "auto_fan_ceiling_enabled": None},
            {"enable_writes": "maybe", "auto_fan_ceiling_enabled": "not-a-bool"},
            {"enable_writes": False, "auto_fan_ceiling_enabled": "false"},
            {"enable_writes": "", "cooling_writes_only_when_paused": None},
            {"enable_writes": {"bad": True}, "auto_fan_ceiling_enabled": ["x"]},
        ]
        old_opt = os.environ.get("LARD_OPTIONS")
        old_sec = os.environ.get("LARD_SECRETS")
        try:
            for options in cases:
                folder = Path(tempfile.mkdtemp(prefix="lard-migrate-"))
                (folder / "options.json").write_text(json.dumps(options))
                (folder / "secrets.json").write_text("{}")
                os.environ["LARD_OPTIONS"] = str(folder / "options.json")
                os.environ["LARD_SECRETS"] = str(folder / "secrets.json")
                loaded = load_settings()
                self.assertFalse(loaded.enable_writes, options)
                self.assertFalse(loaded.auto_fan_ceiling_enabled, options)
                self.assertTrue(loaded.cooling_writes_only_when_paused, options)
            folder = Path(tempfile.mkdtemp(prefix="lard-badjson-"))
            (folder / "options.json").write_text("{not json")
            (folder / "secrets.json").write_text("{}")
            os.environ["LARD_OPTIONS"] = str(folder / "options.json")
            os.environ["LARD_SECRETS"] = str(folder / "secrets.json")
            broken = load_settings()
            self.assertFalse(broken.enable_writes)
            self.assertFalse(broken.auto_fan_ceiling_enabled)
        finally:
            if old_opt is None:
                os.environ.pop("LARD_OPTIONS", None)
            else:
                os.environ["LARD_OPTIONS"] = old_opt
            if old_sec is None:
                os.environ.pop("LARD_SECRETS", None)
            else:
                os.environ["LARD_SECRETS"] = old_sec

        fresh = Settings()
        self.assertFalse(fresh.enable_writes)
        self.assertFalse(fresh.auto_fan_ceiling_enabled)
        self.assertTrue(fresh.cooling_writes_only_when_paused)

        b = paused_one_board()
        ctrl = make_controller(b, "ONE_BOARD", enable_writes=True)
        (ctrl.settings.data_dir / "cooling_txn_inflight.json").write_text(
            json.dumps(
                {
                    "txn_id": "cool-migrate",
                    "phase": "RECOVERING",
                    "pending_ceiling": 80,
                    "monotonic_deadline": 12345,
                }
            )
        )
        ctrl._pending_profile = CoolingProfile("EXPLICIT", 80, None, None)
        ctrl._pending_explicit = True
        self.assertEqual(ctrl.reconcile_after_reload(), "interrupted")
        self.assertIsNone(ctrl._pending_profile)
        ctrl.tick()
        self.assertEqual(b.write_names(), [])
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn((ENT_OLD_AUTO, "on"), ctrl.ha.writes)


class TemperatureTargetPolicyTests(unittest.TestCase):
    """0.1.10: operator helpers are °F. Braiins PUT bodies stay degree_c.

    Fan-ceiling chasing stays legacy. Add-on temperature options stay internal °C.
    """

    def _native(self, braiins, mode="ONE_BOARD", enable_writes=True):
        ctrl = make_controller(braiins, mode, enable_writes=enable_writes)
        ctrl.settings.cooling_policy = COOLING_POLICY_NATIVE
        # Even if the legacy flag is on, native mode must not chase fan max.
        ctrl.settings.auto_fan_ceiling_enabled = True
        return ctrl

    def test_defaults_keep_native_policy_and_writes_off(self):
        fresh = Settings()
        self.assertEqual(fresh.cooling_policy, COOLING_POLICY_NATIVE)
        self.assertFalse(fresh.enable_writes)
        self.assertFalse(fresh.auto_fan_ceiling_enabled)
        self.assertTrue(fresh.cooling_writes_only_when_paused)
        self.assertEqual(fresh.cooling_target_temperature_c, COOLING_TARGET_C)
        self.assertEqual(fresh.cooling_hot_temperature_c, COOLING_HOT_C)
        self.assertEqual(fresh.cooling_dangerous_temperature_c, COOLING_DANGEROUS_C)
        self.assertEqual(fresh.cooling_envelope_min_fan_pct, 0)
        self.assertEqual(fresh.cooling_envelope_max_fan_pct, 100)
        self.assertEqual(ADDON_VERSION, "0.1.12")
        self.assertFalse(fresh.cooling_control_enabled)
        self.assertEqual((COOLING_TARGET_C, COOLING_HOT_C, COOLING_DANGEROUS_C), (70, 85, 95))
        self.assertEqual((COOLING_TARGET_F, COOLING_HOT_F, COOLING_DANGEROUS_F), (158, 185, 203))
        self.assertEqual(fahrenheit_to_celsius_int(158), 70)
        self.assertEqual(fahrenheit_to_celsius_int(185), 85)
        self.assertEqual(fahrenheit_to_celsius_int(203), 95)
        self.assertEqual(fahrenheit_to_celsius_int(174), 79)
        self.assertEqual(fahrenheit_to_celsius_int(TEMP_F_OPENAPI_MIN), 0)
        self.assertEqual(fahrenheit_to_celsius_int(TEMP_F_OPENAPI_MAX), 200)

    def test_absent_helpers_use_internal_celsius_not_fahrenheit(self):
        """Fail closed: a stored option of 70 °C must not be read as 70 °F."""
        b = paused_one_board()
        ctrl = self._native(b, enable_writes=False)
        self.assertFalse(ctrl.settings.enable_writes)
        self.assertEqual(ctrl.temperature_setpoints(), (70, 85, 95))
        profile = ctrl.desired_temperature_policy()
        self.assertIsNotNone(profile)
        body = {"auto": {"max_fan_speed": profile.max_fan_speed, **profile.extra_auto()}}
        self.assertEqual(body["auto"]["target_temperature"], {"degree_c": 70})
        self.assertEqual(body["auto"]["hot_temperature"], {"degree_c": 85})
        self.assertEqual(body["auto"]["dangerous_temperature"], {"degree_c": 95})
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        self.assertEqual(b.write_names(), [])

    def test_fahrenheit_helper_158_puts_degree_c_70(self):
        b = running_boards(["1"])
        ctrl = self._native(b)
        ctrl.settings.auto_fan_ceiling_enabled = False
        ctrl.ha._states[ENT_COOLING_TARGET_F] = "149"
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        # Canonical _c wins. The _f value is a decoy and must not override it.
        # 158/185/203 °F are the historical helper numbers (not converted from the _c suffix).
        ctrl.ha._states[ENT_COOLING_TARGET_C] = "158"
        ctrl.ha._states[ENT_COOLING_HOT_C] = "185"
        ctrl.ha._states[ENT_COOLING_DANGEROUS_C] = "203"
        ctrl.ha._states[ENT_COOLING_TARGET_F] = "100"
        ctrl.tick()
        self.assertEqual(len(b.cooling_puts()), 1)
        auto = (b.last_cooling_body or {}).get("auto") or {}
        self.assertEqual(auto.get("target_temperature"), {"degree_c": 70})
        self.assertEqual(auto.get("hot_temperature"), {"degree_c": 85})
        self.assertEqual(auto.get("dangerous_temperature"), {"degree_c": 95})
        self.assertNotIn("manual", b.last_cooling_body or {})
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertFalse(Settings().enable_writes)

    def test_compatibility_c_id_is_fahrenheit_when_f_entity_absent(self):
        b = paused_one_board()
        ctrl = self._native(b, enable_writes=False)
        ctrl.ha._states[ENT_COOLING_TARGET_C] = "158"
        ctrl.ha._states[ENT_COOLING_HOT_C] = "174"
        ctrl.ha._states[ENT_COOLING_DANGEROUS_C] = "203"
        self.assertEqual(ctrl.temperature_setpoints(), (70, 79, 95))
        profile = ctrl.desired_temperature_policy()
        body = {"auto": {"max_fan_speed": profile.max_fan_speed, **profile.extra_auto()}}
        self.assertEqual(body["auto"]["target_temperature"], {"degree_c": 70})
        self.assertEqual(body["auto"]["hot_temperature"], {"degree_c": 79})
        self.assertEqual(body["auto"]["dangerous_temperature"], {"degree_c": 95})
        self.assertFalse(ctrl.settings.enable_writes)

    def test_ordering_checked_in_fahrenheit_before_convert(self):
        b = running_boards(["1"])
        ctrl = self._native(b)
        ctrl.tick()
        ctrl.ha._states[ENT_COOLING_TARGET_F] = "190"
        ctrl.ha._states[ENT_COOLING_HOT_F] = "180"
        ctrl.ha._states[ENT_COOLING_DANGEROUS_F] = "203"
        self.assertIsNone(ctrl.temperature_setpoints())
        self.assertIsNone(operator_setpoints_to_degree_c(190, 180, 203))
        self.assertFalse(ctrl.request_temperature_policy())
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())

    def test_rounding_collapse_and_openapi_range_refuse(self):
        b = paused_one_board()
        ctrl = self._native(b, enable_writes=False)
        ctrl.ha._states[ENT_COOLING_TARGET_F] = "159"
        ctrl.ha._states[ENT_COOLING_HOT_F] = "160"
        ctrl.ha._states[ENT_COOLING_DANGEROUS_F] = "203"
        self.assertEqual(fahrenheit_to_celsius_int(159), fahrenheit_to_celsius_int(160))
        self.assertIsNone(ctrl.temperature_setpoints())
        ctrl.ha._states[ENT_COOLING_TARGET_F] = "31"
        ctrl.ha._states[ENT_COOLING_HOT_F] = "185"
        ctrl.ha._states[ENT_COOLING_DANGEROUS_F] = "203"
        self.assertLess(fahrenheit_to_celsius_int(31), 0)
        self.assertIsNone(ctrl.temperature_setpoints())
        ctrl.ha._states[ENT_COOLING_TARGET_F] = "158"
        ctrl.ha._states[ENT_COOLING_HOT_F] = "185"
        ctrl.ha._states[ENT_COOLING_DANGEROUS_F] = "400"
        self.assertGreater(fahrenheit_to_celsius_int(400), 200)
        self.assertIsNone(ctrl.temperature_setpoints())
        self.assertFalse(ctrl.request_temperature_policy())
        self.assertEqual(b.cooling_puts(), [])
        self.assertFalse(ctrl.settings.enable_writes)

    def test_missing_helper_stub_is_fahrenheit(self):
        b = paused_one_board()
        ctrl = self._native(b, enable_writes=False)
        ctrl.ensure_cooling_target_helpers()
        self.assertEqual(ctrl.ha.state(ENT_COOLING_TARGET_C), 158)
        self.assertEqual(ctrl.ha.state(ENT_COOLING_HOT_C), 185)
        self.assertEqual(ctrl.ha.state(ENT_COOLING_DANGEROUS_C), 203)
        self.assertIsNone(ctrl.ha.state(ENT_COOLING_TARGET_F))
        self.assertEqual(ctrl.temperature_setpoints(), (70, 85, 95))
        ctrl.ha._states[ENT_COOLING_TARGET_C] = "174"
        ctrl.ensure_cooling_target_helpers()
        self.assertEqual(ctrl.ha.state(ENT_COOLING_TARGET_C), "174")
        self.assertFalse(ctrl.settings.enable_writes)

    def test_policy_prefers_target_temperature_across_board_modes(self):
        b = paused_one_board()
        ctrl = self._native(b)
        one = ctrl.desired_cooling_profile("ONE_BOARD")
        three = ctrl.desired_cooling_profile("THREE_BOARD")
        self.assertEqual(one, three)
        self.assertEqual(one.target_temperature_c, 70)
        self.assertEqual(one.hot_temperature_c, 85)
        self.assertEqual(one.dangerous_temperature_c, 95)
        self.assertEqual(one.max_fan_speed, 100)
        body = {"auto": {"max_fan_speed": one.max_fan_speed, **one.extra_auto()}}
        self.assertEqual(body["auto"]["target_temperature"], {"degree_c": 70})
        self.assertEqual(body["auto"]["hot_temperature"], {"degree_c": 85})
        self.assertEqual(body["auto"]["dangerous_temperature"], {"degree_c": 95})
        self.assertNotIn("manual", body)
        self.assertNotIn("fan_speed_ratio", body["auto"])
        # Per-board fan profiles are not the knob in this mode.
        ctrl.settings.cooling_one_board_max_fan_pct = 40
        ctrl.settings.cooling_three_board_max_fan_pct = 90
        again = ctrl.desired_cooling_profile("THREE_BOARD")
        self.assertEqual(again.max_fan_speed, 100)
        self.assertEqual(again.target_temperature_c, 70)

    def test_high_frequency_max_fan_updates_are_not_scheduled(self):
        b = running_boards(["1"])
        ctrl = self._native(b)
        ctrl.settings.cooling_one_board_max_fan_pct = 70
        ctrl.settings.cooling_two_board_max_fan_pct = 85
        ctrl.tick()
        ctrl.ha._states[ENT_FAN_MAX] = "40"
        ctrl.tick()
        ctrl.ha._states[ENT_FAN_MAX] = "55"
        ctrl.tick()
        ctrl.ha._states[ENT_MODE_REQ] = "TWO_BOARD"
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("set_cooling_auto", b.write_names())
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())

    def test_legacy_ceiling_path_stays_gated(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        self.assertEqual(ctrl.settings.cooling_policy, COOLING_POLICY_LEGACY)
        ctrl.settings.auto_fan_ceiling_enabled = False
        ctrl._cooling_applied = _applied(100)
        ctrl._fan_max_seen = 100
        ctrl.tick()
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        ctrl.settings.auto_fan_ceiling_enabled = True
        ctrl.tick()
        self.assertTrue(b.cooling_puts())
        self.assertEqual(b.cooling_puts()[-1][1], 60)
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        auto = (b.last_cooling_body or {}).get("auto") or {}
        self.assertNotIn("target_temperature", auto)

    def test_operator_target_change_writes_auto_once_then_noops(self):
        b = running_boards(["1"])
        ctrl = self._native(b)
        ctrl.settings.auto_fan_ceiling_enabled = False
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        ctrl.ha._states[ENT_COOLING_TARGET_C] = "149"
        ctrl.tick()
        self.assertEqual(len(b.cooling_puts()), 1)
        auto = (b.last_cooling_body or {}).get("auto") or {}
        self.assertEqual(auto.get("target_temperature"), {"degree_c": 65})
        self.assertEqual(auto.get("hot_temperature"), {"degree_c": 85})
        self.assertEqual(auto.get("dangerous_temperature"), {"degree_c": 95})
        self.assertEqual(auto.get("max_fan_speed"), 100)
        self.assertNotIn("manual", b.last_cooling_body or {})
        names = b.write_names()
        self.assertLess(names.index("pause"), names.index("set_cooling_auto"))
        self.assertLess(names.index("set_cooling_auto"), names.index("resume"))
        self.assertNotIn("start", names)
        self.assertNotIn("restart", names)
        puts_after = len(b.cooling_puts())
        ctrl.tick()
        self.assertEqual(len(b.cooling_puts()), puts_after)

    def test_disarmed_change_is_not_replayed_when_writes_arm(self):
        b = running_boards(["1"])
        ctrl = self._native(b, enable_writes=False)
        ctrl.tick()
        ctrl.ha._states[ENT_COOLING_TARGET_C] = "149"
        ctrl.tick()
        self.assertEqual(b.write_names(), [])
        ctrl.settings.enable_writes = True
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        ctrl.ha._states[ENT_COOLING_TARGET_C] = "151"
        ctrl.tick()
        self.assertEqual(len(b.cooling_puts()), 1)
        auto = (b.last_cooling_body or {}).get("auto") or {}
        self.assertEqual(auto.get("target_temperature"), {"degree_c": 66})
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())

    def test_invalid_temperature_order_does_not_put(self):
        b = running_boards(["1"])
        ctrl = self._native(b)
        ctrl.tick()
        ctrl.ha._states[ENT_COOLING_TARGET_F] = "200"
        ctrl.ha._states[ENT_COOLING_HOT_F] = "180"
        ctrl.tick()
        self.assertFalse(ctrl.request_temperature_policy())
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())

    def test_native_refuses_explicit_fan_ceiling(self):
        b = paused_one_board()
        ctrl = self._native(b)
        self.assertFalse(ctrl.request_cooling_ceiling(60))
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertIn(
            "lard_cooling_deferred",
            [name for name, _payload in ctrl.ha.events],
        )

    def test_request_temperature_policy_refuses_when_writes_disarmed(self):
        b = paused_one_board()
        ctrl = self._native(b, enable_writes=False)
        self.assertFalse(ctrl.request_temperature_policy())
        self.assertEqual(b.write_names(), [])
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())

    def test_thermal_abort_keeps_target_temperature(self):
        b = running_boards(["1"])
        b.cooling_state = {
            "fans": [{"position": 0, "rpm": 4200, "target_speed_ratio": 0.4}],
            "highest_temperature": {"location": 1, "temperature": {"degree_c": 85}},
            "max_fan_speed": 40,
        }
        ctrl = self._native(b)
        ctrl.settings.cooling_envelope_max_fan_pct = 40
        applied = ctrl.desired_temperature_policy()
        self.assertIsNotNone(applied)
        self.assertEqual(applied.max_fan_speed, 40)
        ctrl._cooling_applied = applied
        ctrl.tick()
        self.assertTrue(b.cooling_puts())
        auto = (b.last_cooling_body or {}).get("auto") or {}
        self.assertEqual(auto.get("max_fan_speed"), 100)
        self.assertEqual(auto.get("target_temperature"), {"degree_c": 70})
        self.assertEqual(auto.get("hot_temperature"), {"degree_c": 85})
        self.assertEqual(auto.get("dangerous_temperature"), {"degree_c": 95})
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())

    def test_parse_configured_cooling_and_client_put_shape(self):
        parsed = parse_configured_cooling(
            {
                "pool_groups": [],
                "temperature": {
                    "mode": {
                        "auto": {
                            "target_temperature": {"degree_c": 70},
                            "hot_temperature": {"degree_c": 85},
                            "dangerous_temperature": {"degree_c": 95},
                            "min_fan_speed": None,
                            "max_fan_speed": 100,
                            "minimum_required_fans": 2,
                        }
                    }
                },
            }
        )
        self.assertEqual(parsed["mode"], "auto")
        self.assertEqual(parsed["target_temperature_c"], 70)
        self.assertEqual(parsed["max_fan_speed"], 100)
        self.assertIsNone(parse_configured_cooling({"temperature": {}})["mode"])
        tel = parse_cooling_telemetry(
            {
                "fans": [{"position": 0, "rpm": 1000, "target_speed_ratio": 0.4}],
                "highest_temperature": {"temperature": {"degree_c": 60}},
            }
        )
        self.assertIsNone(tel["max_fan_speed"])
        self.assertEqual(tel["fan_pct"], 40.0)

        tmp = Path(tempfile.mkdtemp(prefix="lard-cooling-put-"))
        settings = Settings(
            braiins_password="x",
            data_dir=tmp,
            share_dir=tmp / "share",
            cooling_control_enabled=True,
        )
        client = Braiins(settings, Logger(settings))
        captured = {}

        def _call(method, path, body=None, timeout=30):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = body
            return 200, body

        client._call = _call  # type: ignore[method-assign]
        profile = CoolingProfile("TEMPERATURE_TARGET", 100, None, 2, 70, 85, 95)
        client.set_cooling_auto(profile.max_fan_speed, profile.extra_auto())
        self.assertEqual(captured["method"], "PUT")
        self.assertEqual(captured["path"], "/api/v1/cooling/mode")
        self.assertEqual(captured["body"]["auto"]["target_temperature"], {"degree_c": 70})
        self.assertNotIn("manual", captured["body"])

    def test_unknown_policy_and_absent_options_stay_disarmed(self):
        old_opt = os.environ.get("LARD_OPTIONS")
        old_sec = os.environ.get("LARD_SECRETS")
        try:
            for options in (
                {},
                {"cooling_policy": None},
                {"cooling_policy": "chase_pwm"},
                {"cooling_policy": {"bad": True}, "enable_writes": None},
                {"cooling_policy": "legacy_fan_ceiling", "auto_fan_ceiling_enabled": False},
            ):
                folder = Path(tempfile.mkdtemp(prefix="lard-policy-"))
                (folder / "options.json").write_text(json.dumps(options))
                (folder / "secrets.json").write_text("{}")
                os.environ["LARD_OPTIONS"] = str(folder / "options.json")
                os.environ["LARD_SECRETS"] = str(folder / "secrets.json")
                loaded = load_settings()
                self.assertFalse(loaded.enable_writes, options)
                self.assertFalse(loaded.auto_fan_ceiling_enabled, options)
                self.assertTrue(loaded.cooling_writes_only_when_paused, options)
                if options.get("cooling_policy") == "legacy_fan_ceiling":
                    self.assertEqual(loaded.cooling_policy, COOLING_POLICY_LEGACY)
                else:
                    self.assertEqual(loaded.cooling_policy, COOLING_POLICY_NATIVE, options)
        finally:
            if old_opt is None:
                os.environ.pop("LARD_OPTIONS", None)
            else:
                os.environ["LARD_OPTIONS"] = old_opt
            if old_sec is None:
                os.environ.pop("LARD_SECRETS", None)
            else:
                os.environ["LARD_SECRETS"] = old_sec


class BraiinsOwnsCoolingTests(unittest.TestCase):
    """0.1.11: Braiins owns cooling. Default cooling_control_enabled is false.

    Helper bumps and mode changes must not PUT /api/v1/cooling/mode or pause
    for cooling. Pause, resume, and hashboard mode still run. enable_writes
    false and switch.solar_miner_auto_enable on still fail closed.
    """

    def _owned(self, braiins, mode="ONE_BOARD", enable_writes=True, policy=None):
        ctrl = make_controller(braiins, mode, enable_writes=enable_writes)
        ctrl.settings.cooling_control_enabled = False
        ctrl.settings.auto_fan_ceiling_enabled = True
        if policy is not None:
            ctrl.settings.cooling_policy = policy
        else:
            ctrl.settings.cooling_policy = COOLING_POLICY_NATIVE
        return ctrl

    def _bump_cooling_helpers(self, ctrl):
        ctrl.ha._states[ENT_COOLING_TARGET_F] = "170"
        ctrl.ha._states[ENT_COOLING_HOT_F] = "190"
        ctrl.ha._states[ENT_COOLING_DANGEROUS_F] = "210"
        ctrl.ha._states[ENT_COOLING_TARGET_C] = "140"
        ctrl.ha._states[ENT_FAN_MAX] = "40"

    def test_default_is_off_and_malformed_stays_off(self):
        fresh = Settings()
        self.assertFalse(fresh.cooling_control_enabled)
        self.assertEqual(fresh.cooling_policy, COOLING_POLICY_NATIVE)
        self.assertFalse(fresh.enable_writes)
        self.assertEqual(ADDON_VERSION, "0.1.12")
        old_opt = os.environ.get("LARD_OPTIONS")
        old_sec = os.environ.get("LARD_SECRETS")
        try:
            cases = (
                ({}, False),
                ({"cooling_control_enabled": None}, False),
                ({"cooling_control_enabled": ""}, False),
                ({"cooling_control_enabled": "maybe"}, False),
                ({"cooling_control_enabled": {"bad": True}}, False),
                ({"cooling_control_enabled": False}, False),
                ({"cooling_control_enabled": "false"}, False),
                ({"cooling_control_enabled": True}, True),
                ({"cooling_control_enabled": "true"}, True),
                ({"cooling_control_enabled": "on"}, True),
                (
                    {
                        "cooling_policy": "legacy_fan_ceiling",
                        "auto_fan_ceiling_enabled": True,
                        "enable_writes": True,
                    },
                    False,
                ),
            )
            for options, expect in cases:
                folder = Path(tempfile.mkdtemp(prefix="lard-cool-own-"))
                (folder / "options.json").write_text(json.dumps(options))
                (folder / "secrets.json").write_text("{}")
                os.environ["LARD_OPTIONS"] = str(folder / "options.json")
                os.environ["LARD_SECRETS"] = str(folder / "secrets.json")
                loaded = load_settings()
                self.assertEqual(loaded.cooling_control_enabled, expect, options)
                if "enable_writes" not in options:
                    self.assertFalse(loaded.enable_writes, options)
        finally:
            if old_opt is None:
                os.environ.pop("LARD_OPTIONS", None)
            else:
                os.environ["LARD_OPTIONS"] = old_opt
            if old_sec is None:
                os.environ.pop("LARD_SECRETS", None)
            else:
                os.environ["LARD_SECRETS"] = old_sec

    def test_helper_bumps_never_emit_cooling_put(self):
        b = running_boards(["1"])
        ctrl = self._owned(b)
        ctrl.tick()
        self.assertIsNotNone(ctrl._chip_temp_f)
        self.assertEqual(b.cooling_puts(), [])
        self._bump_cooling_helpers(ctrl)
        ctrl.tick()
        ctrl.ha._states[ENT_COOLING_TARGET_F] = "165"
        ctrl.ha._states[ENT_FAN_MAX] = "55"
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("set_cooling_auto", b.write_names())
        self.assertNotIn("pause", b.write_names())
        self.assertNotIn("resume", b.write_names())
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())
        self.assertFalse(ctrl.request_temperature_policy())
        self.assertFalse(ctrl.request_cooling_ceiling(60))
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("pause", b.write_names())
        health_states = [state for ent, state in ctrl.ha.writes if ent == "sensor.lard_controller_health"]
        self.assertTrue(health_states)

    def test_legacy_helper_and_mode_change_never_put(self):
        b = running_boards(["1"])
        ctrl = self._owned(b, mode="TWO_BOARD", policy=COOLING_POLICY_LEGACY)
        ctrl.settings.cooling_one_board_max_fan_pct = 40
        ctrl.settings.cooling_two_board_max_fan_pct = 70
        ctrl.settings.auto_fan_ceiling_enabled = True
        ctrl._cooling_applied = _applied(40)
        ctrl._fan_max_seen = 100
        self._bump_cooling_helpers(ctrl)
        ctrl.tick()
        self.assertIn("patch_boards", b.write_names())
        self.assertEqual(sorted(b.enabled), ["1", "2"])
        self.assertEqual(ctrl.actual_mode, "TWO_BOARD")
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("pause", b.write_names())
        self.assertNotIn("set_cooling_auto", b.write_names())
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())

    def test_pause_resume_and_boards_still_work(self):
        paused = paused_one_board()
        paused.power_target = 500
        ctrl = self._owned(paused, mode="ONE_BOARD", policy=COOLING_POLICY_LEGACY)
        ctrl.settings.cooling_one_board_max_fan_pct = 40
        ctrl.ha._states[ENT_FAN_MAX] = "40"
        ctrl.tick()
        self.assertIn("resume", paused.write_names())
        self.assertIn("set_power", paused.write_names())
        self.assertEqual(paused.cooling_puts(), [])
        self.assertNotIn("set_cooling_auto", paused.write_names())
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertTrue(paused.running)

        running = running_boards(["1", "2", "3"])
        ctrl_pause = self._owned(running, mode="PAUSED", policy=COOLING_POLICY_NATIVE)
        self._bump_cooling_helpers(ctrl_pause)
        ctrl_pause.tick()
        self.assertIn("pause", running.write_names())
        self.assertEqual(running.cooling_puts(), [])
        self.assertNotIn("set_cooling_auto", running.write_names())
        self.assertEqual(ctrl_pause.actual_mode, "PAUSED")
        self.assertTrue(running.paused)
        self.assertFalse(running.running)

    def test_thermal_abort_does_not_write_or_pause_for_cooling(self):
        b = running_boards(["1"])
        b.cooling_state = {
            "fans": [{"position": 0, "rpm": 4200, "target_speed_ratio": 0.4}],
            "highest_temperature": {"location": 1, "temperature": {"degree_c": 85}},
            "max_fan_speed": 40,
        }
        ctrl = self._owned(b, policy=COOLING_POLICY_LEGACY)
        ctrl.settings.auto_fan_ceiling_enabled = True
        ctrl._cooling_applied = _applied(40)
        ctrl._fan_max_seen = 40
        ctrl.ha._states[ENT_FAN_MAX] = "40"
        ctrl.tick()
        self.assertGreaterEqual(ctrl._chip_temp_f, CHIP_ABORT_F)
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("pause", b.write_names())
        self.assertNotIn("resume", b.write_names())
        self.assertNotIn("set_cooling_auto", b.write_names())
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertTrue(b.running)

    def test_enable_writes_false_still_fail_closed(self):
        b = paused_one_board()
        ctrl = self._owned(b, enable_writes=False)
        self._bump_cooling_helpers(ctrl)
        ctrl.tick()
        self.assertEqual(b.write_names(), [])
        self.assertEqual(b.cooling_puts(), [])
        self.assertIn("enable_writes_false", ctrl.reason)
        self.assertFalse(ctrl.request_temperature_policy())
        self.assertFalse(ctrl.request_cooling_ceiling(80))
        self.assertEqual(b.write_names(), [])

    def test_old_auto_switch_still_refuses_writes(self):
        b = paused_one_board()
        ctrl = self._owned(b, policy=COOLING_POLICY_LEGACY)
        ctrl.settings.auto_fan_ceiling_enabled = True
        ctrl.ha._states[ENT_OLD_AUTO] = "on"
        self._bump_cooling_helpers(ctrl)
        ctrl.tick()
        self.assertEqual(b.write_names(), [])
        self.assertEqual(b.cooling_puts(), [])
        self.assertEqual(ctrl.last_error, "refusing_writes_old_auto_enable_is_on")
        self.assertNotIn((ENT_OLD_AUTO, "on"), ctrl.ha.writes)
        self.assertFalse(ctrl.request_temperature_policy())
        self.assertEqual(b.write_names(), [])

    def test_client_refuses_cooling_put_before_http(self):
        tmp = Path(tempfile.mkdtemp(prefix="lard-cool-refuse-"))
        settings = Settings(braiins_password="x", data_dir=tmp, share_dir=tmp / "share")
        self.assertFalse(settings.cooling_control_enabled)
        client = Braiins(settings, Logger(settings))
        called = []

        def _call(method, path, body=None, timeout=30):
            called.append((method, path, body))
            return 200, body

        client._call = _call  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError) as raised:
            client.set_cooling_auto(100, {"target_temperature": {"degree_c": 70}})
        self.assertIn("cooling_control_disabled", str(raised.exception))
        self.assertEqual(called, [])
        self.assertIn("/api/v1/cooling/mode", str(raised.exception))

        settings.cooling_control_enabled = True
        client.set_cooling_auto(100, {"target_temperature": {"degree_c": 70}})
        self.assertEqual(called[0][0], "PUT")
        self.assertEqual(called[0][1], "/api/v1/cooling/mode")
        self.assertEqual(
            called[0][2]["auto"]["target_temperature"],
            {"degree_c": 70},
        )

    def test_ignored_bump_is_not_replayed_when_cooling_control_is_enabled(self):
        b = running_boards(["1"])
        ctrl = self._owned(b)
        ctrl.tick()
        self._bump_cooling_helpers(ctrl)
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        ctrl.settings.cooling_control_enabled = True
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        self.assertNotIn("pause", b.write_names())
        ctrl.ha._states[ENT_COOLING_TARGET_C] = "176"
        ctrl.tick()
        self.assertEqual(len(b.cooling_puts()), 1)
        self.assertNotIn("start", b.write_names())
        self.assertNotIn("restart", b.write_names())


class _AbsentOptionalHA(FakeHA):
    """Transport-absent optional _f helpers. Canonical _c still comes from _states."""

    _F_IDS = {
        ENT_COOLING_TARGET_F,
        ENT_COOLING_HOT_F,
        ENT_COOLING_DANGEROUS_F,
    }

    def state(self, entity_id: str, *, quiet: bool = False):
        self.state_reads.append((entity_id, quiet))
        if entity_id in self._F_IDS and entity_id not in self._states:
            self.last_read_absent = True
            if not quiet:
                self.fail_count += 1
            return None
        self.last_read_absent = False
        return self._states.get(entity_id)


class Phase1ObserveGateTests(unittest.TestCase):
    """Phase 1: disarmed writes, helper resolution, telemetry classes, no live Braiins."""

    def test_startup_and_reload_are_disarmed(self):
        fresh = Settings()
        self.assertFalse(fresh.enable_writes)
        self.assertFalse(fresh.cooling_control_enabled)
        b = paused_one_board()
        ctrl = make_controller(b, "THREE_BOARD", enable_writes=False)
        self.assertFalse(ctrl.write_permission.permitted)
        self.assertEqual(ctrl.write_permission.reason, "startup_disarmed")
        self.assertEqual(ctrl.write_permission.source, "startup")
        ctrl.reconcile_after_reload()
        self.assertFalse(ctrl.write_permission.permitted)
        self.assertEqual(ctrl.write_permission.reason, "reload_disarmed")
        self.assertEqual(ctrl.write_permission.source, "reload")
        self.assertFalse(ctrl.recovery_ready)

    def test_write_gate_blocks_before_http_and_observe_only_sends_nothing(self):
        b = paused_one_board()
        b.power_target = 500
        ctrl = make_controller(b, "THREE_BOARD", enable_writes=False)
        ctrl.settings.cooling_control_enabled = True
        ctrl.tick()
        self.assertEqual(b.write_names(), [])
        self.assertEqual(b.cooling_puts(), [])
        self.assertIn("enable_writes_false", ctrl.reason)
        self.assertFalse(ctrl.write_permission.permitted)
        for op, fn in (
            ("mode_pause", lambda: b.pause()),
            ("mode_resume", lambda: b.resume()),
            ("set_power", lambda: b.set_power(944)),
            ("patch_boards", lambda: b.patch_boards(True, ["2", "3"])),
            ("cooling_put", lambda: b.set_cooling_auto(100, {"target_temperature": {"degree_c": 70}})),
        ):
            code, body = ctrl._device_tuple(op, fn)
            self.assertEqual(code, 0, op)
            self.assertTrue(body.get("denied"), op)
        self.assertEqual(b.write_names(), [])
        self.assertEqual(b.cooling_puts(), [])
        log = (ctrl.settings.data_dir / "controller.log").read_text()
        self.assertIn("write blocked", log)
        self.assertIn("op=mode_pause", log)
        self.assertIn("source=controller", log)
        self.assertIn("reason=enable_writes_false", log)
        self.assertIn("state=", log)

        tmp = Path(tempfile.mkdtemp(prefix="lard-gate-"))
        settings = Settings(
            braiins_password="x",
            data_dir=tmp,
            share_dir=tmp / "share",
            cooling_control_enabled=True,
            enable_writes=False,
        )
        client = Braiins(settings, Logger(settings))
        client.write_permission = WritePermission()
        called = []

        def _call(method, path, body=None, timeout=30):
            called.append((method, path, body))
            return 200, body or {}

        client._call = _call  # type: ignore[method-assign]
        for op, result in (
            ("pause", client.pause()),
            ("resume", client.resume()),
            ("set_power", client.set_power(944)),
            ("patch_boards", client.patch_boards(True, ["1", "2", "3"])),
            ("cooling_put", client.set_cooling_auto(80, None)),
        ):
            self.assertEqual(result[0], 0, op)
            self.assertTrue(result[1].get("write_blocked"), op)
        self.assertEqual(called, [])
        blog = (tmp / "controller.log").read_text()
        self.assertIn("write blocked", blog)
        self.assertIn("source=braiins", blog)
        self.assertIn("op=pause", blog)
        self.assertIn("op=set_power", blog)
        self.assertIn("op=patch_boards", blog)
        self.assertIn("op=cooling_put", blog)
        client.write_permission.permitted = True
        client.pause()
        self.assertEqual(called[0][0], "PUT")
        self.assertIn("/api/v1/actions/pause", called[0][1])

    def test_competing_writer_denies_arming(self):
        b = paused_one_board()
        b.power_target = 100
        ctrl = make_controller(b, "ONE_BOARD", enable_writes=True)
        ctrl.ha._states[ENT_COMPETING_WRITER] = "on"
        ctrl.tick()
        self.assertEqual(b.write_names(), [])
        self.assertEqual(b.cooling_puts(), [])
        self.assertEqual(ctrl.last_error, "refusing_writes_competing_writer")
        self.assertIn("competing_writer", ctrl.reason)
        self.assertFalse(ctrl.write_permission.permitted)
        self.assertEqual(ctrl.competing_writer_blocks_arming(), "competing_writer")
        log = (ctrl.settings.data_dir / "controller.log").read_text()
        self.assertIn("write blocked", log)
        self.assertIn("op=arm", log)
        self.assertIn("reason=competing_writer", log)

    def test_canonical_c_helper_skips_missing_f_and_does_not_convert(self):
        b = paused_one_board()
        ctrl = make_controller(b, "PAUSED", enable_writes=False)
        ctrl.settings.cooling_control_enabled = False
        ctrl.ha = _AbsentOptionalHA(ha_states("PAUSED"))
        ctrl.ha._states[ENT_COOLING_TARGET_C] = "158"
        ctrl.ha._states[ENT_COOLING_HOT_C] = "185"
        ctrl.ha._states[ENT_COOLING_DANGEROUS_C] = "203"
        self.assertEqual(
            ctrl._operator_temp_f(ENT_COOLING_TARGET_F, ENT_COOLING_TARGET_C, 70),
            158.0,
        )
        self.assertEqual(ctrl.temperature_setpoints(), (70, 85, 95))
        f_reads = [ent for ent, _quiet in ctrl.ha.state_reads if ent.endswith("_f")]
        self.assertEqual(f_reads, [])
        before = ctrl.ha.fail_count
        streak = ctrl._telemetry_fail_streak
        for _ in range(4):
            ctrl.temperature_setpoints()
        self.assertEqual(ctrl.ha.fail_count, before)
        self.assertEqual(ctrl._telemetry_fail_streak, streak)
        self.assertEqual(
            [ent for ent, _quiet in ctrl.ha.state_reads if ent.endswith("_f")],
            [],
        )

    def test_missing_f_helper_backs_off_on_monotonic_clock(self):
        b = paused_one_board()
        ctrl = make_controller(b, "PAUSED", enable_writes=False)
        ctrl.settings.cooling_control_enabled = False
        ctrl.ha = _AbsentOptionalHA(ha_states("PAUSED"))
        started = ctrl._now()
        self.assertIsNotNone(ctrl.temperature_setpoints())
        self.assertEqual(ctrl._telemetry_fail_streak, 0)
        self.assertEqual(ctrl.ha.fail_count, 0)
        target_until = ctrl._helper_miss_until[ENT_COOLING_TARGET_F]
        self.assertEqual(target_until, started + 30.0)
        first_f = [ent for ent, _q in ctrl.ha.state_reads if ent.endswith("_f")]
        self.assertEqual(len(first_f), 3)
        ctrl.ha.state_reads.clear()
        ctrl._clock.t = started + 10
        ctrl.temperature_setpoints()
        self.assertEqual(
            [ent for ent, _q in ctrl.ha.state_reads if ent.endswith("_f")],
            [],
        )
        log = (ctrl.settings.data_dir / "controller.log").read_text()
        self.assertEqual(log.count(f"helper_miss entity={ENT_COOLING_TARGET_F}"), 1)
        self.assertIn("not miner telemetry", log)
        ctrl._clock.t = target_until
        ctrl.temperature_setpoints()
        self.assertIn(
            ENT_COOLING_TARGET_F,
            [ent for ent, _q in ctrl.ha.state_reads if ent.endswith("_f")],
        )
        log = (ctrl.settings.data_dir / "controller.log").read_text()
        self.assertEqual(log.count(f"helper_miss entity={ENT_COOLING_TARGET_F}"), 2)
        self.assertEqual(ctrl._telemetry_fail_streak, 0)
        self.assertEqual(ctrl.ha.fail_count, 0)

    def test_telemetry_classes_are_deterministic(self):
        refused = "BOSminer API connection error: Connection refused (os error 111)"
        self.assertEqual(
            classify_miner_telemetry(ok=False, http_code=500, error_text=refused),
            "BOSMINER_UNAVAILABLE",
        )
        self.assertEqual(
            classify_miner_telemetry(
                ok=False, http_code=412, error_text="BOSminer is not running"
            ),
            "BOSMINER_UNAVAILABLE",
        )
        self.assertEqual(
            classify_miner_telemetry(
                ok=False,
                http_code=401,
                error_text="Missing or invalid authentication token",
            ),
            "AUTHENTICATION_FAILED",
        )
        self.assertEqual(
            classify_miner_telemetry(ok=False, http_code=500, error_text="timeout"),
            "API_UNREACHABLE",
        )
        self.assertEqual(
            classify_miner_telemetry(ok=False, error_text="hashboards malformed"),
            "REQUIRED_TELEMETRY_MALFORMED",
        )
        self.assertEqual(
            classify_miner_telemetry(
                ok=True, paused=True, user_paused=True, power_w=0.0
            ),
            "VALID_PAUSED",
        )
        self.assertEqual(
            classify_miner_telemetry(
                ok=True, positive_lifecycle=True, power_w=0.0, running=False
            ),
            "VALID_TRANSITION",
        )
        self.assertNotEqual(
            classify_miner_telemetry(
                ok=True,
                running=True,
                power_w=0.0,
                positive_lifecycle=False,
                paused=False,
            ),
            "FAULT_LATCHED",
        )
        self.assertNotEqual(
            classify_miner_telemetry(
                ok=True,
                running=True,
                power_w=0.0,
                positive_lifecycle=False,
                paused=False,
            ),
            "VALID_TRANSITION",
        )
        self.assertEqual(
            classify_miner_telemetry(ok=True, running=True, power_w=400.0),
            "RUNNING_HEALTHY",
        )
        self.assertEqual(
            classify_miner_telemetry(ok=True, critical_fault=True, running=True, power_w=400),
            "FAULT_LATCHED",
        )
        self.assertEqual(board_patch_readback(["1", "2", "3"], ["1"], 200), "faulted_unverified")
        self.assertEqual(board_patch_readback(["1", "2", "3"], [], 200), "faulted_unverified")
        self.assertEqual(
            board_patch_readback(["1", "2", "3"], ["1", "2", "3"], 200),
            "verified",
        )
        self.assertNotEqual(board_patch_readback(["1", "2", "3"], ["1", "2", "3"], 500), "verified")

    def test_board_patch_200_without_readback_is_not_success(self):
        b = paused_one_board()
        b.board_reads = deque([["1"]])
        ctrl = make_controller(b, "THREE_BOARD", enable_writes=True)
        ok = ctrl.apply_mode("THREE_BOARD")
        self.assertFalse(ok)
        self.assertNotEqual(ctrl.actual_mode, "THREE_BOARD")
        self.assertIn("faulted_unverified", ctrl.last_error)
        self.assertEqual(ctrl.telemetry_class, "FAULT_LATCHED")
        self.assertIn("patch_boards", b.write_names())
        log = (ctrl.settings.data_dir / "controller.log").read_text()
        self.assertIn("board_readback faulted_unverified", log)
        self.assertIn("patch_http=200", log)

        empty = paused_one_board()
        empty.board_reads = deque([[]])
        ctrl_empty = make_controller(empty, "THREE_BOARD", enable_writes=True)
        self.assertFalse(ctrl_empty.apply_mode("THREE_BOARD"))
        self.assertIn("faulted_unverified", ctrl_empty.last_error)
        self.assertNotEqual(ctrl_empty.actual_mode, "THREE_BOARD")

    def test_applying_clears_when_bosminer_unavailable(self):
        b = paused_one_board()
        b.boards_http = 500
        b.boards_error_body = {
            "error": "Internal error",
            "message": "BOSminer API connection error: Connection refused (os error 111)",
        }
        ctrl = make_controller(b, "THREE_BOARD", enable_writes=False)
        ctrl.actual_mode = "APPLYING"
        ctrl._cooling_transition_active = True
        ctrl.tick()
        self.assertEqual(ctrl.telemetry_class, "FAULT_LATCHED")
        self.assertEqual(ctrl.actual_mode, "FAULT_LATCHED")
        self.assertEqual(ctrl.observed_state, "FAULT_LATCHED")
        self.assertEqual(ctrl.desired_mode, "THREE_BOARD")
        self.assertNotEqual(ctrl.observed_state, ctrl.desired_mode)
        self.assertFalse(ctrl._cooling_transition_active)
        self.assertEqual(b.write_names(), [])
        self.assertFalse(ctrl.write_permission.permitted)

        b2 = paused_one_board()
        b2.boards_http = 500
        b2.boards_error_body = b.boards_error_body
        ctrl2 = make_controller(b2, "THREE_BOARD", enable_writes=False)
        ctrl2.actual_mode = "APPLYING"
        ctrl2._transition_evidence = True
        ctrl2._transition_evidence_mono = ctrl2._now()
        ctrl2.tick()
        self.assertEqual(ctrl2.actual_mode, "WAITING_FOR_BRAIINS")
        self.assertEqual(ctrl2.telemetry_class, "WAITING_FOR_BRAIINS")
        self.assertEqual(b2.write_names(), [])

        ctrl2._transition_evidence_mono = ctrl2._now() - 10_000
        ctrl2.tick()
        self.assertEqual(ctrl2.actual_mode, "FAULT_LATCHED")
        self.assertEqual(ctrl2.controller_state, "FAULT_LATCHED")
        self.assertNotEqual(ctrl2.observed_miner_mode, "WAITING_FOR_BRAIINS")
        self.assertNotEqual(ctrl2.observed_miner_mode, "APPLYING")
        self.assertEqual(b2.write_names(), [])

    def test_recovery_ready_does_not_rearm_writes(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD", enable_writes=False)
        for _ in range(4):
            ctrl.tick()
            self.assertFalse(ctrl.recovery_ready)
        ctrl.tick()
        self.assertTrue(ctrl.recovery_ready)
        self.assertGreaterEqual(ctrl._valid_poll_streak, 5)
        self.assertTrue(ctrl._auth_ok)
        self.assertTrue(ctrl._bosminer_available)
        self.assertFalse(ctrl._critical_fault)
        self.assertFalse(ctrl.settings.enable_writes)
        self.assertFalse(ctrl.write_permission.permitted)
        self.assertEqual(b.write_names(), [])
        self.assertEqual(b.cooling_puts(), [])

        blocked = running_boards(["1"])
        ctrl_b = make_controller(blocked, "ONE_BOARD", enable_writes=False)
        ctrl_b.ha._states[ENT_COMPETING_WRITER] = "on"
        for _ in range(5):
            ctrl_b.tick()
        self.assertFalse(ctrl_b.recovery_ready)
        self.assertEqual(blocked.write_names(), [])


_BOSMINER_REFUSED = {
    "error": "Internal error",
    "message": "BOSminer API connection error: Connection refused (os error 111)",
}
_BOSMINER_NOT_RUNNING = {
    "error": "Precondition Failed",
    "message": "BOSminer is not running",
}


class VerificationSafetyTests(unittest.TestCase):
    """Adversarial checks for the three observe-only blockers.

    Fake monotonic clock only. These fail on the previous PR #12 implementation:
    settlement ran only in APPLYING, and the token "applying" was positive evidence.
    """

    def _refused(self, braiins, body=None):
        braiins.boards_http = 500
        braiins.boards_error_body = body or _BOSMINER_REFUSED

    def test_applying_waiting_then_fault_on_bosminer_loss(self):
        b = running_boards(["1"])
        b.phase = "preheating"
        b.running = False
        b.paused = False
        b.user_paused = False
        b.power_w = 0.0
        b.hashrate = 0.0
        ctrl = make_controller(b, "ONE_BOARD", enable_writes=False)
        ctrl.actual_mode = "APPLYING"
        ctrl.tick()
        self.assertEqual(ctrl.telemetry_class, "VALID_TRANSITION")
        self.assertTrue(ctrl._transition_evidence)
        self._refused(b)
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "WAITING_FOR_BRAIINS")
        self.assertEqual(ctrl.controller_state, "WAITING_FOR_BRAIINS")
        ctx = ctrl._verification_ctx
        self.assertIn("transaction_id", ctx)
        self.assertGreater(ctx["entered_mono"], 0)
        self.assertEqual(ctx["expected_operation"], ctrl.desired_mode)
        self.assertGreater(ctx["evidence_deadline_mono"], ctrl._now())
        self.assertEqual(ctx["reason"], "BOSMINER_UNAVAILABLE")
        self.assertNotEqual(ctrl.observed_miner_mode, "WAITING_FOR_BRAIINS")
        self.assertNotEqual(ctrl.observed_miner_mode, "APPLYING")
        deadline = ctx["evidence_deadline_mono"]
        ctrl._clock.t = deadline + 1
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "FAULT_LATCHED")
        self.assertEqual(ctrl.controller_state, "FAULT_LATCHED")
        self.assertNotEqual(ctrl.actual_mode, "WAITING_FOR_BRAIINS")
        self.assertIn("bosminer_unavailable_during_verification", ctrl.last_error)
        self.assertIn("valid_transition_evidence_expired", ctrl.last_error)
        self.assertEqual(b.write_names(), [])
        self.assertFalse(ctrl.settings.enable_writes)
        self.assertFalse(ctrl.write_permission.permitted)
        self.assertEqual(ctrl.desired_mode, "ONE_BOARD")

    def test_waiting_expires_without_being_applying(self):
        b = paused_one_board()
        self._refused(b, _BOSMINER_NOT_RUNNING)
        ctrl = make_controller(b, "THREE_BOARD", enable_writes=False)
        now = ctrl._now()
        ctrl.actual_mode = "WAITING_FOR_BRAIINS"
        ctrl.controller_state = "WAITING_FOR_BRAIINS"
        ctrl._transition_evidence = True
        ctrl._transition_evidence_mono = now
        window = float(ctrl.settings.expected_recovery_seconds)
        ctrl._verification_ctx = {
            "transaction_id": "txn-observe",
            "entered_mono": now,
            "expected_operation": "THREE_BOARD",
            "evidence_deadline_mono": now + window,
            "last_valid_telemetry_mono": now,
            "reason": "BOSMINER_UNAVAILABLE",
            "evidence": "preheating",
        }
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "WAITING_FOR_BRAIINS")
        self.assertNotEqual(ctrl.actual_mode, "FAULT_LATCHED")
        ctrl._clock.t = now + window + 1
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "FAULT_LATCHED")
        self.assertEqual(ctrl.controller_state, "FAULT_LATCHED")
        self.assertIn("bosminer_unavailable_during_verification", ctrl.last_error)
        self.assertEqual(b.write_names(), [])
        self.assertFalse(ctrl.write_permission.permitted)
        self.assertEqual(ctrl.desired_mode, "THREE_BOARD")

    def test_applying_token_at_zero_power_is_not_a_transition(self):
        self.assertNotIn("applying", LEGITIMATE_LIFECYCLE_TOKENS)
        b = FakeBraiins(
            enabled=["1"],
            paused=False,
            running=False,
            user_paused=False,
            phase="applying",
            status="applying",
            power_w=0.0,
            hashrate=0.0,
        )
        b.pause_reason = ""
        ctrl = make_controller(b, "THREE_BOARD", enable_writes=False)
        ctrl.actual_mode = "APPLYING"
        obs = MinerObservation(
            ok=True,
            phase="applying",
            power_w=0.0,
            hashrate=0.0,
            enabled_ids=["1"],
        )
        self.assertFalse(ctrl._positive_lifecycle(obs))
        self.assertNotEqual(
            classify_miner_telemetry(
                ok=True,
                positive_lifecycle=ctrl._positive_lifecycle(obs),
                power_w=0.0,
                running=False,
                paused=False,
            ),
            "VALID_TRANSITION",
        )
        ctrl.tick()
        self.assertNotEqual(ctrl.telemetry_class, "VALID_TRANSITION")
        self.assertNotEqual(ctrl.actual_mode, "WAITING_FOR_BRAIINS")
        self.assertEqual(ctrl.actual_mode, "FAULT_LATCHED")
        self.assertEqual(ctrl.controller_state, "FAULT_LATCHED")
        self.assertIn("required_telemetry_malformed", ctrl.last_error)
        self.assertEqual(ctrl.observed_miner_mode, "UNVERIFIED")
        self.assertEqual(b.write_names(), [])
        self.assertFalse(ctrl.settings.enable_writes)

    def test_independent_preheat_is_valid_and_removal_fails_closed(self):
        b = running_boards(["1"])
        b.phase = "preheating"
        b.running = False
        b.paused = False
        b.user_paused = False
        b.power_w = 0.0
        b.hashrate = 0.0
        ctrl = make_controller(b, "ONE_BOARD", enable_writes=False)
        ctrl.tick()
        self.assertEqual(ctrl.telemetry_class, "VALID_TRANSITION")
        self.assertTrue(ctrl._positive_lifecycle(ctrl._last_obs))
        self.assertEqual(ctrl.actual_mode, "APPLYING")
        self.assertEqual(b.write_names(), [])
        b.phase = "applying"
        b.status = "applying"
        b.pause_reason = ""
        b.power_w = 0.0
        b.hashrate = 0.0
        ctrl.tick()
        self.assertNotEqual(ctrl.telemetry_class, "VALID_TRANSITION")
        self.assertEqual(ctrl.actual_mode, "FAULT_LATCHED")
        self.assertEqual(ctrl.controller_state, "FAULT_LATCHED")
        self.assertFalse(ctrl._transition_evidence)
        self.assertEqual(b.write_names(), [])

    def test_valid_paused_zero_power_is_not_a_fault(self):
        b = paused_one_board()
        ctrl = make_controller(b, "PAUSED", enable_writes=False)
        ctrl.tick()
        self.assertEqual(ctrl.telemetry_class, "VALID_PAUSED")
        self.assertEqual(ctrl.actual_mode, "PAUSED")
        self.assertEqual(ctrl.observed_miner_mode, "PAUSED")
        self.assertEqual(ctrl.controller_state, "OBSERVING")
        self.assertNotEqual(ctrl.actual_mode, "FAULT_LATCHED")
        self.assertNotEqual(ctrl.controller_state, "FAULT_LATCHED")
        self.assertEqual(b.power_w, 0.0)
        self.assertEqual(b.hashrate, 0.0)
        self.assertEqual(b.write_names(), [])

    def test_actual_modes_split_physical_from_controller(self):
        for physical in ("PAUSED", "ONE_BOARD", "TWO_BOARD", "THREE_BOARD"):
            self.assertIn(physical, OBSERVED_MINER_MODES)
            self.assertIn(physical, ACTUAL_MODES)
        for controller_only in ("APPLYING", "WAITING_FOR_BRAIINS", "FAULT_LATCHED", "ERROR"):
            self.assertIn(controller_only, ACTUAL_MODES)
            self.assertNotIn(controller_only, OBSERVED_MINER_MODES)
            self.assertIn(controller_only, CONTROLLER_STATES)
        self.assertIn("OBSERVING", CONTROLLER_STATES)
        self.assertIn("DISARMED", CONTROLLER_STATES)
        self.assertIn("RUNNING", CONTROLLER_STATES)
        b = running_boards(["1", "2", "3"])
        ctrl = make_controller(b, "THREE_BOARD", enable_writes=False)
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "THREE_BOARD")
        self.assertEqual(ctrl.observed_miner_mode, "THREE_BOARD")
        self.assertTrue(ctrl.actual_mode in OBSERVED_MINER_MODES)
        self.assertEqual(ctrl.controller_state, "RUNNING")
        self.assertEqual(ctrl.telemetry_class, "RUNNING_HEALTHY")
        self.assertEqual(ctrl.ha._states["sensor.lard_controller_actual_mode"], "THREE_BOARD")
        self.assertEqual(ctrl.ha._states["sensor.lard_miner_mode_desired"], "THREE_BOARD")

    def test_poll_loop_fault_then_recovery_ready_without_rearm(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD", enable_writes=False)
        ctrl.tick()
        self.assertEqual(ctrl.telemetry_class, "RUNNING_HEALTHY")
        self.assertEqual(ctrl.observed_miner_mode, "ONE_BOARD")
        self.assertEqual(ctrl.controller_state, "RUNNING")
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")

        b.phase = "preheating"
        b.running = False
        b.paused = False
        b.user_paused = False
        b.power_w = 0.0
        b.hashrate = 0.0
        ctrl.tick()
        self.assertEqual(ctrl.telemetry_class, "VALID_TRANSITION")
        self.assertEqual(ctrl.actual_mode, "APPLYING")
        self.assertEqual(ctrl.controller_state, "APPLYING")

        self._refused(b)
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "WAITING_FOR_BRAIINS")
        self.assertEqual(ctrl.controller_state, "WAITING_FOR_BRAIINS")
        self.assertNotEqual(ctrl.observed_miner_mode, "WAITING_FOR_BRAIINS")
        self.assertEqual(b.write_names(), [])

        ctrl._clock.t = ctrl._verification_ctx["evidence_deadline_mono"] + 1
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "FAULT_LATCHED")
        self.assertEqual(ctrl.controller_state, "FAULT_LATCHED")
        self.assertNotEqual(ctrl.actual_mode, "WAITING_FOR_BRAIINS")
        self.assertIn("bosminer_unavailable_during_verification", ctrl.last_error)
        self.assertEqual(b.write_names(), [])

        b.boards_http = 200
        b.phase = "running"
        b.status = "normal"
        b.running = True
        b.paused = False
        b.user_paused = False
        b.power_w = 400.0
        b.hashrate = 20.0
        b.enabled = ["1"]
        for _ in range(5):
            ctrl.tick()
            self.assertEqual(b.write_names(), [])
            self.assertFalse(ctrl.settings.enable_writes)
            self.assertFalse(ctrl.write_permission.permitted)
        self.assertTrue(ctrl.recovery_ready)
        self.assertGreaterEqual(ctrl._valid_poll_streak, 5)
        self.assertEqual(ctrl.controller_state, "FAULT_LATCHED")
        self.assertEqual(ctrl.actual_mode, "FAULT_LATCHED")
        self.assertEqual(ctrl.observed_miner_mode, "ONE_BOARD")
        self.assertFalse(ctrl.settings.enable_writes)
        self.assertFalse(ctrl.write_permission.permitted)
        self.assertNotIn("pause", b.write_names())
        self.assertNotIn("resume", b.write_names())
        self.assertNotIn("patch_boards", b.write_names())
        self.assertNotIn("set_power", b.write_names())


if __name__ == "__main__":
    unittest.main()
