# Operator Fahrenheit helpers (0.1.10)

Addon version **0.1.10**. Base is `main` at 0.1.9 (PR #9). Operator cooling helpers are Fahrenheit. Braiins `PUT /api/v1/cooling/mode` bodies stay `degree_c`, converted with `round((f - 32) * 5 / 9)` only at that boundary. This does not enable writes, AUTO, or any live control. Do not merge from this note, do not deploy, do not set `enable_writes`, and do not turn on `switch.solar_miner_auto_enable`. The miner stays PAUSED.

## Unit scheme

Two layers, on purpose:

- HA helpers are the operator unit, °F. The package keeps historical IDs `lard_cooling_target_c` / `_hot_c` / `_dangerous_c`; the numeric state is still °F. Optional IDs `lard_cooling_target_f` / `_hot_f` / `_dangerous_f` win when they have a number. The package does not create those aliases, so a new slider cannot hide the helpers the site is about to set to 158 / 174 / 203.
- Add-on options `cooling_*_temperature_c` stay internal °C (defaults 70 / 85 / 95, schema 0–200). They are the fallback when no helper state exists. A stored 70 is not reread as 70 °F.

Order is `target < hot < dangerous` in °F before convert. After convert, each integer must be in 0–200 and still strictly ordered. Package slider is 100–250 °F, mode slider, initials 158 / 185 / 203. A site on 70 / 79 / 95 °C sets helpers to 158 / 174 / 203 (174 °F → 79 °C). `enable_writes` stays false.

## Tests

`python3 -m unittest test_controller` from `lard_controller/app`.

Covered: 158 °F helper → `degree_c` 70 in the PUT body; `_f` preferred over the `_c` id; `_c` id alone is still °F (158 / 174 / 203 → 70 / 79 / 95); order checked in °F; rounding collapse and OpenAPI range refuse; absent helpers use internal 70 / 85 / 95 °C rather than 70 °F; missing stubs seed 158 / 185 / 203 and do not invent `_f`; disarmed changes are not replayed; `enable_writes` default remains false.

---

# Cooling temperature-target ownership (0.1.9)

Addon version **0.1.9**. Base is `main` at 0.1.8 (PR #8). This change flips cooling ownership to Braiins Automatic `target_temperature`. It does not enable writes, AUTO, or any live control. Do not merge from this note, do not deploy, do not set `enable_writes`, and do not turn on `switch.solar_miner_auto_enable`. The miner stays PAUSED.

## Ownership

Inventory conclusion, carried into the design note `docs/cooling-temperature-target.md`: the only cooling mutate path is `PUT /api/v1/cooling/mode`. `GET /cooling/state` is RPM/PWM telemetry with no ceiling. `GET /cooling/mode` is 405. Setpoints are read from `GET /configuration/miner`. There is no separate live fan-max primitive.

Default `cooling_policy` is `native_auto_target`. The desired body is Automatic mode with target/hot/dangerous °C and a wide fan envelope (min 0, max 100). Board-count `max_fan_speed` profiles are not consulted. `auto_fan_ceiling_enabled` stays false and is not the efficient-mining path. `legacy_fan_ceiling` keeps the old per-board ceilings and still requires that flag.

Operator defaults 70 / 85 / 95 °C are Braiins Toolbox examples inside the OpenAPI 0–200 range. They are not a claimed 26.09 firmware default.

## What still holds

H1–H7 and B1–B4 are unchanged. A policy write still re-checks authorization immediately before emit, pauses until two idle polls, issues one PUT, settles, resumes once, and allows at most one retry. Deadlines stay monotonic. Cancel and reload still publish `INTERRUPTED_MANUAL_REVIEW` with no Resume. Start, Restart, and reboot stay on the deny-list. Pending still auto-applies only after `HASHING`.

The first setpoint sample is a seed. Changes while writes are disarmed are absorbed, so arming `enable_writes` does not itself pause the miner or PUT. An operator change after writes are armed schedules one gated transaction. The same value is a no-op. Unordered temperatures refuse the PUT. Thermal abort opens the envelope to 100% and keeps the target fields in the body.

## Tests

`python3 -m unittest test_controller` from `lard_controller/app`.

New `TemperatureTargetPolicyTests` cover: native defaults and fail-closed writes; target_temperature preferred across board modes; fan-max and board-mode changes do not schedule a cooling PUT; legacy ceiling still requires `auto_fan_ceiling_enabled`; one operator target change PUTs the auto body then no-ops; a disarmed change is not replayed when writes arm; invalid order, a disarmed explicit request, and an explicit fan-ceiling request under the native policy do not PUT; thermal abort keeps `target_temperature`; configuration parse and the real client PUT path; unknown policy strings stay native and disarmed. No Start/Restart on those paths. Full suite: 122 tests OK.

## Remaining risks

- Defaults stay observe-only. This change was not deployed and did not call the miner.
- A future armed window will pause the miner once per real setpoint change, because that PUT is still disruptive on 26.09. Dwell (600s) limits repeats. It will not pause for PWM or board-count fan profiles.
- 70/85/95 are operator defaults, not read from this miner's `configuration/constraints`. A later observe-only read should confirm the live constraints before anyone arms writes.
- GUI Apply is still not HAR-proven. The schema allows only the cooling-mode PUT.
- Partial auto PUTs on this firmware have nulled omitted temperatures. The native body sends target, hot, and dangerous together so a target change does not clear the others. A legacy fan-only PUT still omits them, which matches 0.1.8 and can clear setpoints if that legacy path is ever armed.

---

# Write-Enable Hardening PR Report

Addon version **0.1.8**. Base is `main` at 0.1.7 (PR #7, `2d3edfc`). This change hardens write paths only. It does not enable writes, AUTO, or any live control. Do not merge, do not deploy, do not set `enable_writes`, and do not turn on `switch.solar_miner_auto_enable`.

## Scope

- Files: `app/controller.py`, `app/test_controller.py`, `config.yaml`, `Dockerfile`, `CHANGELOG.md`, `README.md`, `NOTES.md`.
- Version bump: `ADDON_VERSION`, `config.yaml` `version`, and `io.hass.version` are `0.1.8`.
- Write authorization, Start/Restart deny-list, named lifecycle tokens, monotonic deadlines, reload/cancel, single-owner cooling transactions, and fail-closed config migration.
- Preserved from 0.1.7: pending auto-applies only after `HASHING` (B1); `operational` / `applying` are interim (B2); a settle or pre-resume hard fault does not ResumeMining (B3); board health fails closed (B4); two-poll pause confirmation; same-value cooling no-op; bounded 5xx resume; first telemetry miss is `UNKNOWN` / `STALE`; device reboot stays denied.
- Out of scope: enabling writes or AUTO, cooling PUT against a live miner, Fan Max, Pause/Resume/Start/Restart/reboot, Home Assistant changes, and any live Braiins call.

## H1

Every device-changing call goes through `_call_device`, which runs `_authorize_device_write` and then runs it again under `_emit_lock` immediately before the client function. A denial returns `None`, does not call the client, logs `write_denied`, and emits `lard_write_denied`.

The check requires `enable_writes`, the HA master gate, and `switch.solar_miner_auto_enable` off. Cooling actions (`pause`, `cooling_put`, `resume`, failsafe pause/restore) also require an active transaction owned by this thread, `_owner_txn_id == _cooling_txn_id`, a phase that allows that command, no cancel, and a non-terminal health class. A cooling PUT also requires `FRESH` telemetry, a paused-idle observation, and no hard fault. A retry resume is refused when retries already exceed `max_resume_retries_per_transaction`. The retry counter increments only after a non-denied emit. `start`, `restart`, and `reboot` return `structural_deny` before any other check.

A denial while a transaction is active sets cancel and `reload_hold`. Health becomes `INTERRUPTED_MANUAL_REVIEW` unless it is already `DEGRADED_NEEDS_ATTENTION` or `ERROR`. Covered emit sites include cooling pause, cooling PUT, initial and retry resume, failsafe pause/restore, mode pause/resume, power target, and board enable/disable.

Tests: `test_h1_disarm_before_pause_leaves_write_log_empty` (disarm between tick and pause; `write_names()` empty, zero cooling PUTs, zero resumes). `test_h1_disarm_before_put_and_resume_and_retry` (disarm before PUT: no `set_cooling_auto`, zero resumes; disarm on the primary gate: one PUT and zero resumes; disarm on the retry gate: one resume already sent, `_resume_retries_used == 0`). `test_h1_txn_id_terminal_and_stale_block_put_or_resume` (a changed txn id and stale telemetry leave cooling PUTs and resumes at 0; terminal health before the primary resume leaves the one PUT already sent and zero resumes).

## H2

`BRAIINS_DENY_PATHS` includes `/actions/start`, `/actions/restart`, reboot, and factory reset. `Braiins._call` raises `refused Braiins path … (structurally denied before HTTP)` before `ensure_auth` and before any socket. `_cooling_escalate_start` and `_cooling_escalate_restart` stay permanently disabled: they emit `lard_cooling_escalate_blocked` and return false with no device call. Recovery comments do not describe Restart as a last resort.

Tests: `test_device_reboot_start_and_restart_denied_before_http` patches `urllib.request.urlopen` and asserts start, restart, reboot, and factory reset raise with `urlopen` not called. `test_h2_recovery_and_reload_never_call_start_or_restart` runs recovery and reload and asserts those commands are absent. `test_escalate_helpers_cannot_command` still covers the disabled helpers.

## H3

`_positive_lifecycle` is an exact-token match against `LEGITIMATE_LIFECYCLE_TOKENS` (applying, cooldown, cooling_down, preheat/preheating, startup, starting, init, initializing, autotune/tuning/tuner, ramping/ramp/quick_ramping, warming/warmup, booting) plus adjacent bigrams (`cooling down` → `cooling_down`). Parser flags `starting`, `preheating`, and `ramping` are equality checks. Hard fault is checked first and is not positive. Blanket `running`, not-paused, unqualified watts, unknown strings, and `miner_ready is False` do not extend the window. `init` does not match `reinitializing`.

The primary deadline is still stamped at start + maximum (default 600s). The recovery loop, including the `_active_confirmed` branch, returns exhausted once elapsed reaches the expected window (default 240s) unless the observation is a named positive lifecycle. A non-positive path is then one resume retry and the 180s post-retry window (about 420s). A named token still runs to the 600s maximum plus 180s (about 780s).

Tests: `test_h3_named_tokens_only` (running at 0 W, an unknown string, and `reinitializing` are not positive; `init`, cooldown, preheat, applying, and ramping are; a hard fault wins). An unknown lifecycle then ends `DEGRADED_NEEDS_ATTENTION` near 420s with two resumes. `test_b2_running_zero_watts_reaches_degraded_not_success` expects that same non-positive span. Persistent applying still uses the 780s window.

## H4

`Controller._now` is `time.monotonic()` and is the only clock for transaction deadlines, settle, retry, recovery budgets, and cooling dwell. `Controller._wall` is `time.time()` for human stamps, anti-flap, `mode_entered_ts`, `last_board_change_ts`, and telemetry success timestamps. Those two clocks are not mixed into one deadline. The inflight marker stores `monotonic_deadline: null` and `resume_on_load: false`. `reconcile_after_reload` zeroes in-memory deadline fields and ignores any numeric deadline a previous process wrote.

Tests use `FakeClock` via `_now` and `_sleep`. `test_h4_wall_jump_does_not_move_monotonic_window` advances wall clock by 50000s during sleep while the monotonic fake clock advances normally; a cooldown lifecycle still spans the monotonic 600s window. `test_h4_boundaries_239_240_599_600_780` checks command counts at 239/240 (non-positive: one resume still in `RECOVERING`, then two resumes and about 420s) and 599/600/780 (cooldown: one resume at 599s, span about 780s, primary deadline delta 600s).

## H5

`cancel_cooling_transaction` clears `_cooling_txn_id`, bumps `_emit_generation`, sets cancel and `reload_hold`, publishes `INTERRUPTED_MANUAL_REVIEW`, emits `lard_cooling_interrupted`, and does not Resume, Start, Restart, or PUT. Recovery and settle loops return exhausted or cancelled on cancel or owner-id mismatch and do not retry. `_note_recovery_interim` returns immediately when health is already terminal or cancel is set, so a later poll cannot overwrite `INTERRUPTED_MANUAL_REVIEW` with `RECOVERING`.

`reconcile_after_reload` runs from `main()` immediately after `Controller()` is constructed. A missing marker is a clean start. A present, unreadable, or deadline-bearing marker clears pending, discards monotonic deadlines, sets `INTERRUPTED_MANUAL_REVIEW`, and issues no command. `_auto_apply_pending_allowed` is false while `reload_hold`, cancel, terminal health, or terminal kind `interrupted` is set, so a paused interrupted transaction is not auto-resumed. A tick in that state publishes and returns without a miner read and without writes.

Tests: `test_h5_cancel_reload_and_stale_txn_issue_no_command` cancels during recovery (one resume already sent, zero cooling PUTs, health `INTERRUPTED_MANUAL_REVIEW`). A marker with `monotonic_deadline: 999999` and `resume_on_load: true` reconciles to `interrupted`, zeroes the deadline, drops pending, and the following tick has an empty write log. A resume `_call_device` after that reconcile returns `None` and adds no command.

## H6

One active cooling transaction. `request_cooling_ceiling` takes `_txn_lock` and, if a transaction is active, coalesces the newest ceiling and returns without starting another cycle. `_emit_lock` serializes device HTTP. `_emit_inflight` counts an overlap and denies the second emit. Cooling commands require the same-thread owner and the live txn id. `tick` returns observe-only when a transaction is active, `reload_hold` is set, or health is `INTERRUPTED_MANUAL_REVIEW`, so a second tick cannot open another cycle or perturb scripted miner state.

Tests (thread barriers, no sleeps): `test_h6_one_owner_under_concurrent_pressure` requests newer ceilings and a tick during settle; one cooling PUT of the original ceiling, pending is the newest value, overlap violations stay 0, and no Start/Restart/reboot. `test_h6_cancel_at_retry_and_telemetry_withhold_resume` cancels at the retry gate while telemetry is withheld; resume count stays at the one primary resume.

## H7

`Settings` defaults remain `enable_writes=False`, `auto_fan_ceiling_enabled=False`, and `cooling_writes_only_when_paused=True`. `load_settings` skips `None` and empty strings. `_truthy` is true only for boolean `True` or the strings `1`, `true`, `yes`, and `on`. A dict, list, or other malformed value is not true. Malformed JSON loads as `{}`. Nothing in this process sets `switch.solar_miner_auto_enable` to on. After reload, pending is cleared and cannot auto-run. Invalid config fails closed to the defaults above.

Tests: `test_h7_migration_cannot_arm_writes_or_pending` covers absent, null, and malformed options and bad JSON; reconcile drops pending and any monotonic deadline; the following tick has an empty write log; `ENT_OLD_AUTO` is never set on.

## B1–B4 Regression Check

- **B1.** `_auto_apply_pending_allowed` is still `HASHING` only. `DEGRADED_NEEDS_ATTENTION`, `ERROR`, and `INTERRUPTED_MANUAL_REVIEW` hold pending. `reload_hold` and cancel also block auto-apply. `BlockerFixTests.test_b1_*` still pass, including stale callback after `ERROR`.
- **B2.** `operational` and `applying` still return a falsy interim `CoolingResult` and do not close the transaction. Running at 0 W with no named token now exhausts at the expected window (H3) and still ends `DEGRADED_NEEDS_ATTENTION`, not success. Persistent applying still waits the maximum window.
- **B3.** Settle and pre-resume hard faults still abort before ResumeMining. No Start, Restart, or extra cooling PUT on that path. `test_b3_*` still pass.
- **B4.** Board health still fails closed: missing hook, stale, malformed, incomplete, or unhealthy boards are not `HASHING`. Watts and TH/s alone are not success. `test_b4_*` still pass.
- Also unchanged: two consecutive pause polls, same-value cooling no-op, bounded 5xx resume (not Start/Restart), first telemetry miss is `UNKNOWN` or `STALE` and does not cancel an active transaction, device reboot denied.

## Test Results

Command: `python3 -m unittest test_controller` from `lard_controller/app`.

Result: **Ran 110 tests in 0.336s — OK.**

The 0.1.7 suite was 99 tests. This PR adds 11 tests in `WriteEnableHardeningTests` (`test_h1_*` through `test_h7_*`, including the boundary, wall-clock, cancel/reload, and concurrency cases) and replaces the old reboot test with `test_device_reboot_start_and_restart_denied_before_http`. The clock is `FakeClock` on `_now` and `_sleep`. There are no real multi-minute sleeps and no live Braiins calls.

Denied paths assert command counts: `write_names()` empty or missing the denied verb, `cooling_puts()` empty where the PUT must not happen, `resume_calls` at the expected count (often 0), and `_forbid_control` (no start, restart, or reboot). Overlap violations stay 0. Pending coalescing asserts the newest ceiling and a single owner.

## Remaining Risks

- Defaults stay observe-only. This PR does not arm `enable_writes`, `auto_fan_ceiling_enabled`, or solar auto, and it was not deployed.
- A mid-transaction auth denial or cancel latches `reload_hold` and `INTERRUPTED_MANUAL_REVIEW` until the next process start reconciles. That is fail-closed. It does not auto-resume a paused miner.
- H3 changes the 0.1.7 window for running at 0 W with no named lifecycle token: expected 240s, one retry, then 180s (about 420s), instead of waiting until 600s. Named tokens still use the 600s maximum plus 180s.
- Anti-flap and human timestamps still use wall clock. Transaction deadlines do not.
- Concurrency coverage is deterministic thread barriers in unit tests, not a live miner. The gated transition still holds the Braiins mutex for its own body; a second ceiling request coalesces under `_txn_lock` without taking that mutex.
- No live Braiins, Home Assistant, Pause, Resume, Start, Restart, reboot, Fan Max, or cooling PUT was performed.

## Merge Readiness: READY FOR SAFETY REVIEW

Full suite is green (110 tests, OK). H1–H7 are implemented. B1–B4 tests still pass. Denied paths assert empty or exact command counts. Do not merge from this review. Do not deploy. Do not enable writes or AUTO.

---

# LARD Controller 0.1.7 notes (historical)

The sections below are the 0.1.7 review notes. The follow-ups they list (broad lifecycle match, wall-clock deadlines, reload policy, concurrency) are closed by the 0.1.8 report above.

## Files

- `app/controller.py` — recovery state machine, cooling transaction, telemetry freshness, health sensor, events.
- `app/test_controller.py` — fake clock coverage for the 12 recovery cases plus existing reconciliation/cooling tests.
- `config.yaml`, `Dockerfile`, `CHANGELOG.md`, `README.md`, `DOCS.md`, `translations/en.yaml` — version 0.1.7 and the new options.

`actual_mode` is still `PAUSED | APPLYING | ONE_BOARD | TWO_BOARD | THREE_BOARD | ERROR`. The finer health class is `sensor.lard_controller_health`. Existing entity IDs, `enable_writes`, and the master gate are unchanged. `switch.solar_miner_auto_enable` is never turned on.

## State machine

Per miner, under the existing Braiins mutex:

`HASHING → PAUSE_REQUESTED → PAUSED_CONFIRMED → COOLING_APPLYING → COOLING_SETTLING → RESUME_REQUESTED → RECOVERING → HASHING`

Failure path: `RECOVERING → RETRY_RESUME_ONCE → RECOVERING → DEGRADED_NEEDS_ATTENTION`. `ERROR` is only for an explicit hard fault, a cooling PUT failure, or a non-5xx resume rejection.

Same-value ceiling is a no-op (no pause, no PUT, no resume). A ceiling requested while a transaction is active is coalesced to the newest value. It auto-applies only after successful `HASHING`. Confirmed `PAUSED`, `DEGRADED_NEEDS_ATTENTION`, and `ERROR` keep that pending ceiling visible, log `pending_held`, and do not start another cooling cycle. A fresh explicit operator request is required. Automatic fan-ceiling ticks do not queue when `auto_fan_ceiling_enabled` is false. `_cooling_escalate_start` and `_cooling_escalate_restart` are hard-disabled: they log `escalate_blocked` and return false without Start, Restart, or Resume.

`operational` (running, including 0 W) and `applying` (ramp) are non-terminal inside the recovery window. They may publish `RECOVERING` / `APPLYING` diagnostics. They do not clear the cooling transaction, count as success, skip the 600s deadline, or skip the one resume retry. Re-observing them does not move the original maximum deadline. The post-retry window is a separate 180s deadline.

Pause is confirmed by two consecutive miner-state polls, not by HTTP 200 alone. The cooling PUT runs only after that confirmation. Every settle poll uses the same hard-fault predicate. A hard fault aborts the settle and does not ResumeMining. The predicate is checked again immediately before the first resume and before the one retry resume. A stale or missing read is not a hard fault and is not permission to resume blindly. HTTP 500 enters recovery; it does not call Start or BOSminer Restart.

## Timings (defaults)

| Knob | Default | Role |
| --- | --- | --- |
| `cooling_settle_seconds` | 45 | Poll after PUT before the first resume. A zero value falls back to `cooling_resume_settle_seconds`. |
| `transition_poll_interval_seconds` | 10 | Pause, settle, and recovery poll. |
| `expected_recovery_seconds` | 240 | 0 W is normal while lifecycle is positive. |
| `maximum_recovery_seconds` | 600 | Stop waiting unless lifecycle is still positive; then one retry. |
| `post_retry_recovery_seconds` | 180 | Window after that single retry. |
| `stable_hash_poll_count` | 3 | Consecutive good samples for full `HASHING`. |
| `telemetry_failures_before_error` | 3 | Consecutive read failures before telemetry `ERROR`. |
| `max_resume_retries_per_transaction` | 1 | Automatic resume retries per transaction. |

`auto_fan_ceiling_enabled` is false. `cooling_writes_only_when_paused` is true.

## UNKNOWN vs ERROR

A failed miner read sets freshness to `UNKNOWN` (no prior success) or `STALE` (last good reading kept, with its timestamp). It does not pause, resume, or write cooling. During a cooling transaction the phase is left alone. `ERROR` / `telemetry_sustained_unavailable` happens only after the consecutive-failure threshold, and only when no cooling transaction is active.

## HASHING vs DEGRADED vs ERROR

- **HASHING:** three consecutive current polls, power above the idle floor, hashrate at or above the startup threshold, every board in the configured `BOARD_MAP` for that mode proven healthy on this read, not stale, and no hard fault. Expected board count is that configured set, not the length of a partial `hashboards` array. A previous healthy read is not reused.
- **RECOVERING:** reachable, legitimate lifecycle (cooldown, cooling down, preheat, startup, init, autotune, APPLYING, or running but not yet a full hash sample). 0 W and 0 TH/s are allowed here. This does not close the cooling transaction.
- **DEGRADED_NEEDS_ATTENTION:** recovery hit the maximum, the one resume retry and the post-retry window did not reach hashing, and there is no hard fault. The miner is left for a person. The controller does not Start, Restart, or reboot. A coalesced pending ceiling stays visible and is not applied.
- **ERROR:** hard fault (overheat, thermal, hardware, board, ASIC, PSU, fan failure, unrecoverable), including a fault during settle or immediately before either resume, a cooling PUT failure, or a non-5xx resume rejection. PUT/4xx paths still pause and restore the last known-good ceiling. A hard fault does not. Pending is held. Tick does not issue further device commands.

A missing, unhealthy, stale, malformed, or incomplete board reading is not `HASHING`, even with nonzero watts and hashrate. Missing telemetry during recovery may stay `RECOVERING` inside the window. It never becomes `HASHING`.

## API uncertainties

- `GET /api/v1/cooling/mode` is 405 on this BOS+. Effective ceiling is read from `GET /api/v1/cooling/state` when that payload includes `max_fan_speed`. If the field is absent, PUT 200 + GET 200 is accepted and logged.
- Resume HTTP 500 vs body is not a stable "not ready" schema. 0.1.7 treats 5xx as unreadiness and decides from later miner state, not from the status code alone.
- Lifecycle text is taken from the phase / pause reason / status strings already parsed from `GET /api/v1/miner/details`. There is no new endpoint. Site wording that does not match the known cooldown/preheat/startup tokens will not count as positive lifecycle, so recovery ends at the expected window and uses the one retry.
- `bosminer-experimental.toml` fan min/max is unsupported on BOS+ 26.09 and is not written.
- Board health uses existing Braiins reads, not a new endpoint. `Braiins.board_health()` calls `GET /api/v1/miner/hw/hashboards` (`id`, `enabled`/`is_enabled`, `chips_count` as an int or `{"value": n}`, `stats`, plus any `healthy`/`health`/`status`/`fault`/`stale` field) and `GET /api/v1/miner/errors` (`message`, `error_codes[].code`/`reason`, `components[].name`). A board is proven only when that response is current and complete: id, enabled, `chips_count > 0`, not stale, and no safety-fault token. HTTP failure, malformed JSON, a missing list, incomplete entries, stale/partial flags, or an unknown health value are not healthy. If the `board_health` hook is absent, the controller fails closed (`boards_healthy=False`, `board_health_verified=False`). It does not default missing live board health to healthy.

## Manual prove checklist

Do not arm writes until the observe-only rows pass. Leave `auto_fan_ceiling_enabled` false. Leave `switch.solar_miner_auto_enable` off.

1. Install 0.1.7 with `enable_writes: false`. `/health` is 200. `sensor.lard_controller_health` publishes. No Braiins writes in the log.
2. Unplug or black-hole the miner for one poll. Freshness goes `UNKNOWN` or `STALE`. Actual mode does not become `ERROR` on that first miss. No pause, resume, or cooling PUT.
3. Arm writes only in a planned window (`enable_writes` and the master boolean). Confirm auto fan ceiling is still false: moving `input_number.lard_fan_max_pct` while hashing does **not** PUT cooling.
4. Explicit paused-window proof only: pause, confirm ~0 W, PUT a new ceiling, wait the settle, resume once. Expect `RECOVERING` at 0 W for cooldown/preheat (often ~3 minutes; a cooling change has been seen near 8 minutes). Do not call that `ERROR`.
5. When watts and hashrate are stable across three polls and boards look right, health becomes `HASHING` and actual returns to the board mode.
6. If it is still not hashing at the maximum, expect exactly one more ResumeMining, then either `HASHING` or `DEGRADED_NEEDS_ATTENTION`. No Start, no BOSminer Restart, no device reboot.
7. A real thermal/hardware fault should be `ERROR` without a second automatic resume.

# PR #7 Blocker Fix Report

## B1 — Pending After Terminal Failure

Pending coalesced ceilings no longer start a second cooling transaction after `DEGRADED_NEEDS_ATTENTION` or `ERROR`. `_take_pending_if_terminal` consumes pending only when the transaction is inactive and health is `HASHING`. Confirmed `PAUSED` is not an automatic apply. A closed outcome of `degraded` or `error` also blocks apply. Otherwise it logs `pending_held` (once per reason and ceiling) and leaves `_pending_profile` visible. `_gated_cooling_transition` records a terminal generation. A nested or deferred apply re-checks that generation and the allow rule before any pause, PUT, or resume. A stale callback restores the pending ceiling and returns `refused` with no device commands. Tick does not auto-start a held ceiling from `DEGRADED` or `ERROR`. A fresh explicit `request_cooling_ceiling` is still an operator action.

Tests: `test_b1_pending_held_after_degraded_no_second_cycle` (fake time through the 600s window, one retry, and the 180s post-retry window; exactly the original pause/PUT/two resumes; pending stays; a later tick adds no writes). `test_b1_pending_held_after_cooling_put_error` (HTTP 400 on the cooling PUT; resume count 0; the coalesced ceiling is not PUT; only the original attempt plus the existing fail-safe restore). `test_b1_stale_callback_does_not_apply_after_error` (depth-1 apply after the terminal generation changes issues no commands). `test_b1_pending_still_applies_after_clean_hashing` (clean `HASHING` still applies the newest ceiling; cooling PUTs are the original target then the pending target).

## B2 — Bounded Recovery Completion

`operational` (including running at 0 W) and `applying` (ramp) are interim inside the recovery window. `_finish_operational` and `_finish_applying` return `CoolingResult("interim")`, which is falsy and not closed. They do not clear `_cooling_txn_active`, release ownership, or count as success. `_recovery_window` returns only `hashing`, `exhausted`, or `error`. The primary 600s deadline is stamped once from the first recovery observation and is not moved when APPLYING or running is seen again. The post-retry window is a separate 180s deadline. Only stable full `HASHING`, bounded exhaustion (`DEGRADED_NEEDS_ATTENTION`), or a hard-fault/`ERROR` close the transaction. `CoolingResult` is truthy only for closed success outcomes `hashing`, `paused`, and `noop`.

Tests: `test_b2_running_zero_watts_reaches_degraded_not_success` and `test_b2_persistent_applying_reaches_degraded` (exactly 2 resumes, span from the first resume at least 780s and under 820s, primary deadline delta exactly 600s, one primary `recovery_begin`, `primary_deadline_kept` once, transaction still active in the interim logs, final `DEGRADED_NEEDS_ATTENTION`, outcome `degraded`, no Start/Restart). `test_b2_unhealthy_board_with_watts_degrades` (nonzero watts and hashrate with an unhealthy board stay in recovery, then `DEGRADED`, not `HASHING`). `test_b2_later_three_healthy_polls_still_hash` (a legitimate later 3-poll healthy sample still reaches `HASHING` inside the 240s window, one resume).

## B3 — Hard Fault During Settle

Every settle poll uses `_classify_settle_obs` and the same `_hard_fault` predicate. A hard fault calls `_fail_hard`, aborts the settle, and does not confirm, resume, retry, or apply pending. `_gate_resume` observes again immediately before the first resume and before the retry resume. Stale or missing telemetry is `not_clean`: it is not `ERROR` by itself, and it does not authorize ResumeMining. The configured settle remains 45s (`cooling_settle_seconds`); a zero option still falls back to the older settle setting rather than skipping the observation.

Tests: `test_b3_hard_fault_during_settle_aborts_before_resume` and `test_b3_thermal_fault_during_settle_aborts_before_resume` (fault injected in `COOLING_SETTLING` before `_settle_complete`; resume 0, retry 0, health `ERROR`, cooling PUTs are the original ceiling only, a later tick adds no writes; the hardware case also holds the pending ceiling). `test_b3_fault_before_put_issues_no_cooling_command` (no pause, PUT, or resume). `test_b3_fault_immediately_before_first_resume` (full 45s settle, then fault on the primary gate; resume 0). `test_b3_fault_during_recovery_before_retry` (one resume already sent, no retry). `test_b3_fault_immediately_before_retry_resume` (one resume, retries used 0). `test_b3_stale_telemetry_is_not_a_hard_fault_and_not_blind_resume` (mining-state HTTP 500 through settle, recovery, and both gates; resume 0, not a hard fault, ends `DEGRADED`).

## B4 — Live Board Health

Option A is implemented on the existing Braiins reads. `Braiins.board_health()` calls `GET /api/v1/miner/hw/hashboards` and `GET /api/v1/miner/errors`. Public `Hashboard` has no health enum, so a board is proven only when the current payload has `id`, enabled, `chips_count > 0` (int or `{"value": n}`), is not stale, and carries no safety-fault token. Miner errors (`message`, `error_codes[].code`/`reason`, `components[].name`) map onto the same hard-fault tokens. HTTP failure, malformed JSON, a missing list, incomplete entries, stale/partial flags, or an unknown value are not healthy. Option B: if the client has no `board_health` hook, `_apply_live_board_health` sets `boards_healthy=False` and `board_health_verified=False`. It does not trust a raw hashboard list and it does not reuse a previous healthy read. `_hashing_sample_ok` also requires every id in the configured `BOARD_MAP` for that mode to be `proven_healthy` on this read. Expected count is that configured set, not `len(hashboards)`. A partial list can report its present boards as structurally healthy and still fail the mode check.

Tests: `test_b4_live_shape_without_board_health_does_not_hash_on_watts` (hook removed; watts and TH/s do not verify boards or reach `HASHING`; ends `DEGRADED` after two resumes). `test_b4_three_healthy_boards_and_three_polls_hash`. `test_b4_two_of_three_boards_never_hash`. `test_b4_present_unhealthy_board_never_hashes` (`DEGRADED`, not `ERROR`, when the board is explicitly unhealthy without a hard-fault token). `test_b4_missing_malformed_stale_incomplete_do_not_hash`. `test_b4_invalid_sample_resets_stable_count` (a bad sample inside the 3-poll window returns the stable count to 1). `test_b4_cached_healthy_then_unavailable_is_not_hashing`. `test_b4_live_client_parses_hashboards_and_errors_fail_closed` (HTTP 500 fail closed; three complete boards plus empty errors verify healthy; a PSU error sets `safety_fault` and not healthy; a two-board payload does not prove `THREE_BOARD`).

## Existing Safeguards Regression Check

The original recovery cases still pass on the fake clock. Pause still needs two confirming polls. A live cooling PUT while hashing is still refused. Same-value ceilings are still a no-op. A 5xx resume is still bounded unreadiness, not Start/Restart. A non-5xx resume rejection is still `ERROR`. The first telemetry miss is still `UNKNOWN` or `STALE` and does not cancel an active transaction. Device reboot stays on the deny list. Defaults remain `enable_writes` false, `auto_fan_ceiling_enabled` false, and `cooling_writes_only_when_paused` true.

Three older tests had encoded the unsafe early success (running at 0 W or a watt ramp closing the transaction as the target board mode). They now expect `DEGRADED_NEEDS_ATTENTION` after the bounded window, two resumes, and no Start/Restart: `test_power_zero_during_warmup_is_not_failure`, `test_stage_abc_low_rising_watts_no_mature_th`, and `test_resume_does_not_timeout_at_30s_if_stage_a_clears_later`. Case 12 now ends `DEGRADED` rather than leaving health in `RECOVERING` after a false success.

## Remaining Follow-ups

Completed in this pass (trivial, and required so the recovery path cannot call them):

- `_cooling_escalate_start` and `_cooling_escalate_restart` are hard-disabled. Both log `escalate_blocked`, emit `lard_cooling_escalate_blocked`, and return false. They do not call Start, BOSminer Restart, or Resume. Covered by `test_escalate_helpers_cannot_command`.
- The Braiins deny-list comment no longer describes Restart as a last-resort cooling-resume escalation. Device reboot stays denied. 0.1.7 recovery does not call Start or BOSminer Restart.

Still open (not changed; narrowing them would change the 240/600 contract or add policy outside B1–B4):

- `_positive_lifecycle` is still broad (`init` substring, and any `running and not paused` counts as positive), so running at 0 W waits until 600s rather than stopping at 240s. Narrowing it would break the B2 window.
- Recovery deadlines still use `time.time()` via `_now` (wall clock), not a monotonic clock. Tests replace `_now`. Anti-flap still calls `time.time()` directly.
- Reload and orphan cooling-transaction policy is unchanged. The transaction is in memory. A process reload can leave the miner paused; there is no automatic resume-on-restart.
- There is no concurrency stress test around overlapping operator requests and the recovery loop. The cooling path remains under `_braiins_mutex`.
- Exact 239/240/599/600/780 second boundaries are covered by the window span asserts (first-resume span at least 780s and under 820s, primary deadline delta exactly 600s), not by one-second edge fixtures.
- Startup and cooldown are positive lifecycle, not extra `_cooling_refuse_reason` tokens. Pause-first still applies. They were not added to the refuse list.

## Test Results (total, new, full suite, fake-clock, command-count assertions)

`python3 -m unittest test_controller` from `lard_controller/app`: see the recorded run below. New tests are `BlockerFixTests` (`test_b1_*` through `test_b4_*` plus `test_escalate_helpers_cannot_command`). The suite uses `FakeClock` (`Controller._now` and `_sleep`); there are no real multi-minute sleeps. Command-count asserts check `write_names()`, `cooling_puts()`, and `resume_calls`, and assert that `start` and `restart` are absent after the terminal under test and after the hard-disabled escalation helpers.

Recorded result: 99 tests, OK. 24 of those are in `BlockerFixTests` (23 `test_b1_*`–`test_b4_*` cases plus `test_escalate_helpers_cannot_command`). Full suite `python3 -m unittest test_controller` from `lard_controller/app` finished in well under a second on the fake clock.

## Merge Readiness: READY FOR RE-REVIEW

Do not merge from this change. No deploy, no Home Assistant config change, and no live Braiins command.
