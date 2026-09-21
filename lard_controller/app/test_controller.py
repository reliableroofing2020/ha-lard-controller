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
    HealthState,
    Logger,
    Settings,
    clamp_fan_max_pct,
    metric_trend_rising,
    norm_board_id,
    parse_cooling_telemetry,
    parse_mining_state,
)


class FakeHA:
    def __init__(self, states: dict):
        self._states = dict(states)
        self.fail_count = 0
        self.writes: list[tuple[str, object]] = []

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
        self.paused = True
        self.running = False
        self.user_paused = True
        self.phase = "stopped"
        self.status = "paused"
        self.power_w = 0.0

    def _set_running(self) -> None:
        self.paused = False
        self.running = True
        self.user_paused = False
        self.phase = "running"
        self.status = "normal"

    def _set_starting(self) -> None:
        self.paused = False
        self.running = False
        self.user_paused = False
        self.phase = "starting"
        self.status = "starting"

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
        if self.clock is not None:
            self._resume_clock_at = self.clock.t
        code = self._next_http("resume_http")
        self._note_http(code)
        if code != 200:
            return code, {}
        if self.resume_sets_running:
            self._set_running()
        elif self.resume_sets_starting:
            self._set_starting()
        return 200, {"already_mining": False}

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
        if self.cooling_exc:
            raise self.cooling_exc
        code = self._next_http("cooling_http")
        self._note_http(code)
        return code, body

    def get_cooling_state(self):
        self.calls.append(("get_cooling_state",))
        if self.cooling_exc:
            raise self.cooling_exc
        return 200, dict(self.cooling_state or {"fans": []})

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
        self._maybe_delayed_unpause()
        parsed = {
            "status_raw": self.status,
            "phase": self.phase,
            "user_paused": self.user_paused,
            "paused": self.paused,
            "running": self.running,
            "starting": self.phase == "starting",
            "preheating": self.phase in {"preheating", "preheat"},
            "ramping": self.phase in {"ramping", "ramp", "quick_ramping"},
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
        b = paused_one_board()
        b.resume_sets_running = False
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.tick()
        self.assertIn("resume", b.write_names())
        self.assertNotEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertEqual(ctrl.actual_mode, "ERROR")
        self.assertIn("resume_wait_timeout", ctrl.last_error)

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
        b = paused_one_board()
        b.power_w = 0.0
        ctrl = make_controller(b, "ONE_BOARD")

        def resume_still_zero():
            b._set_running()
            b.power_w = 0.0
            return 200, {}

        b.resume = lambda: (b.calls.append(("resume",)) or resume_still_zero())  # type: ignore
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
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
        self.assertEqual(ctrl.actual_mode, "APPLYING")
        self.assertEqual(ctrl.last_error, "")
        self.assertFalse(b.running)
        self.assertLess(b.power_w, 300)
        self.assertLess(b.hashrate, 1.0)

    def test_resume_does_not_timeout_at_30s_if_stage_a_clears_later(self):
        b = paused_one_board()
        b.resume_sets_running = False
        b.unpause_after_elapsed = 90
        ctrl = make_controller(b, "ONE_BOARD")
        started = ctrl._clock.t
        ctrl.tick()
        self.assertGreaterEqual(ctrl._clock.t - started, 90)
        self.assertLess(ctrl._clock.t - started, 30 + RESUME_WAIT_S)
        self.assertNotEqual(ctrl.actual_mode, "ERROR")
        self.assertNotIn("resume_wait_timeout", ctrl.last_error or "")
        self.assertIn(ctrl.actual_mode, ("APPLYING", "ONE_BOARD"))
        self.assertIn("resume", b.write_names())

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
            }
        )
        self.assertEqual(tel["fan_rpm"], 3100)
        self.assertEqual(tel["fan_pct"], 60.0)
        self.assertAlmostEqual(tel["chip_temp_f"], 176.0)

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

    def test_apply_mode_cooling_failure_does_not_fail_apply(self):
        b = paused_one_board()
        b.cooling_exc = RuntimeError("cooling down")
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ok = ctrl.apply_mode("ONE_BOARD")
        self.assertTrue(ok)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertEqual(ctrl.last_error, "")
        self.assertIn("resume", b.mining_write_names())

    def test_apply_mode_cooling_http_500_does_not_fail_apply(self):
        b = paused_one_board()
        b.cooling_http = 500
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.ha._states[ENT_FAN_MAX] = "45"
        ok = ctrl.apply_mode("ONE_BOARD")
        self.assertTrue(ok)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertNotEqual(ctrl.actual_mode, "ERROR")

    def test_helper_change_is_cool_path_only(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.read_actual_from_miner()
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        mining_before = list(b.mining_write_names())
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.tick()
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertNotEqual(ctrl.actual_mode, "APPLYING")
        self.assertEqual(b.mining_write_names(), mining_before)
        self.assertTrue(any(c[1] == 60 for c in b.cooling_puts()))
        self.assertEqual((b.last_cooling_body or {}).get("auto", {}).get("max_fan_speed"), 60)

    def test_helper_100_restores_unconstrained(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.ha._states[ENT_FAN_MAX] = "100"
        ctrl.tick()
        self.assertTrue(b.cooling_puts())
        self.assertEqual(b.cooling_puts()[-1][1], 100)
        extra = b.cooling_puts()[-1][2] or {}
        self.assertEqual(extra.get("minimum_required_fans"), 2)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")

    def test_chip_abort_restores_100_without_pausing(self):
        b = running_boards(["1"])
        b.cooling_state = {
            "fans": [{"position": 0, "rpm": 4200, "target_speed_ratio": 0.6}],
            "highest_temperature": {"location": 1, "temperature": {"degree_c": 85}},
        }
        ctrl = make_controller(b, "ONE_BOARD")
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.tick()
        self.assertGreaterEqual(ctrl._chip_temp_f, CHIP_ABORT_F)
        self.assertEqual(b.cooling_puts()[-1][1], 100)
        self.assertEqual(ctrl.actual_mode, "ONE_BOARD")
        self.assertNotIn("pause", b.mining_write_names())
        self.assertTrue(ctrl._thermal_abort_active)

    def test_enable_writes_false_skips_fan_ceiling(self):
        b = running_boards(["1"])
        ctrl = make_controller(b, "ONE_BOARD", enable_writes=False)
        ctrl.ha._states[ENT_FAN_MAX] = "60"
        ctrl.tick()
        self.assertEqual(b.cooling_puts(), [])
        self.assertEqual(b.write_names(), [])


if __name__ == "__main__":
    unittest.main()
