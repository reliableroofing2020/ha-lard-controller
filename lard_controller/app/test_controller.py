#!/usr/bin/env python3
"""Unit tests for LARD mode reconciliation (desired vs confirmed operational)."""
from __future__ import annotations

import tempfile
import time
import unittest
import unittest.mock
from collections import deque
from pathlib import Path

from controller import (
    API_5XX_BACKOFF_S,
    CHIP_ABORT_F,
    COOLING_IDLE_POWER_W,
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
    CoolingProfile,
    HealthState,
    Logger,
    Settings,
    clamp_fan_max_pct,
    metric_trend_rising,
    norm_board_id,
    parse_board_health_payload,
    parse_cooling_telemetry,
    parse_mining_state,
)


class FakeHA:
    def __init__(self, states: dict):
        self._states = dict(states)
        self.fail_count = 0
        self.writes: list[tuple[str, object]] = []
        self.events: list[tuple[str, dict]] = []

    def fire_event(self, event_type, data=None):
        self.events.append((event_type, dict(data or {})))

    def state(self, entity_id: str):
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
            return [], code, {}
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

    def test_device_reboot_denied_bosminer_restart_allowed(self):
        tmp = Path(tempfile.mkdtemp(prefix="lard-braiins-deny-"))
        settings = Settings(braiins_password="x", data_dir=tmp, share_dir=tmp / "share")
        client = Braiins(settings, Logger(settings))
        client.token = "tok"
        client.token_ts = time.time()
        with self.assertRaises(RuntimeError) as reboot_err:
            client._call("PUT", "/api/v1/actions/reboot")
        self.assertIn("reboot", str(reboot_err.exception).lower())
        with self.assertRaises(RuntimeError):
            client._call("PUT", "/api/v1/system/reboot")
        with self.assertRaises(RuntimeError):
            client._call("PUT", "/api/v1/actions/factory-reset")

        class Resp:
            status = 200

            def read(self):
                return b"true"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        with unittest.mock.patch("urllib.request.urlopen", return_value=Resp()):
            code, body = client.restart()
        self.assertEqual(code, 200)
        self.assertTrue(hasattr(Braiins, "start"))


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
        self._assert_recovery_deadline(ctrl, b)
        log = (ctrl.settings.data_dir / "controller.log").read_text()
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


if __name__ == "__main__":
    unittest.main()
