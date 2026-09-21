# LARD Controller 0.1.7 notes

## Files

- `app/controller.py` — recovery state machine, cooling transaction, telemetry freshness, health sensor, events.
- `app/test_controller.py` — fake clock coverage for the 12 recovery cases plus existing reconciliation/cooling tests.
- `config.yaml`, `Dockerfile`, `CHANGELOG.md`, `README.md`, `DOCS.md`, `translations/en.yaml` — version 0.1.7 and the new options.

`actual_mode` is still `PAUSED | APPLYING | ONE_BOARD | TWO_BOARD | THREE_BOARD | ERROR`. The finer health class is `sensor.lard_controller_health`. Existing entity IDs, `enable_writes`, and the master gate are unchanged. `switch.solar_miner_auto_enable` is never turned on.

## State machine

Per miner, under the existing Braiins mutex:

`HASHING → PAUSE_REQUESTED → PAUSED_CONFIRMED → COOLING_APPLYING → COOLING_SETTLING → RESUME_REQUESTED → RECOVERING → HASHING`

Failure path: `RECOVERING → RETRY_RESUME_ONCE → RECOVERING → DEGRADED_NEEDS_ATTENTION`. `ERROR` is only for an explicit hard fault, a cooling PUT failure, or a non-5xx resume rejection.

Same-value ceiling is a no-op (no pause, no PUT, no resume). A ceiling requested while a transaction is active is coalesced to the newest value. It auto-applies only after `HASHING` or an intentional confirmed `PAUSED`. `DEGRADED_NEEDS_ATTENTION` and `ERROR` keep that pending ceiling visible, log `pending_held`, and do not start another cooling cycle. A fresh explicit operator request is required. Automatic fan-ceiling ticks do not queue when `auto_fan_ceiling_enabled` is false.

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

## B1

Pending coalesced ceilings no longer start a second cooling transaction after `DEGRADED_NEEDS_ATTENTION` or `ERROR`. `_take_pending_if_terminal` consumes pending only when the transaction is inactive, health is `HASHING` or an intentional confirmed `PAUSED`, and the closed outcome is not `degraded` or `error`. Otherwise it logs `pending_held` (once per reason and ceiling) and leaves `_pending_profile` visible. `_gated_cooling_transition` records a terminal generation. A nested or deferred apply re-checks that generation and the allow rule before any pause, PUT, or resume. A stale callback restores the pending ceiling and returns `refused` with no device commands. Tick does not auto-start a held ceiling from `DEGRADED` or `ERROR`. A fresh explicit `request_cooling_ceiling` is still an operator action.

Tests: `test_b1_pending_held_after_degraded_no_second_cycle` (fake time through the 600s window, one retry, and the 180s post-retry window; exactly the original pause/PUT/two resumes; pending stays; a later tick adds no writes). `test_b1_pending_held_after_cooling_put_error` (HTTP 400 on the cooling PUT; resume count 0; the coalesced ceiling is not PUT; only the original attempt plus the existing fail-safe restore). `test_b1_stale_callback_does_not_apply_after_error` (depth-1 apply after the terminal generation changes issues no commands). `test_b1_pending_still_applies_after_clean_hashing` (clean `HASHING` still applies the newest ceiling; cooling PUTs are the original target then the pending target).

## B2

`operational` (including running at 0 W) and `applying` (ramp) are interim inside the recovery window. `_finish_operational` and `_finish_applying` return `CoolingResult("interim")`, which is falsy and not closed. They do not clear `_cooling_txn_active`, release ownership, or count as success. `_recovery_window` returns only `hashing`, `exhausted`, or `error`. The primary 600s deadline is stamped once from the first recovery observation and is not moved when APPLYING or running is seen again. The post-retry window is a separate 180s deadline. Only stable full `HASHING`, bounded exhaustion (`DEGRADED_NEEDS_ATTENTION`), or a hard-fault/`ERROR` close the transaction. `CoolingResult` is truthy only for closed success outcomes `hashing`, `paused`, and `noop`.

Tests: `test_b2_running_zero_watts_reaches_degraded_not_success` and `test_b2_persistent_applying_reaches_degraded` (exactly 2 resumes, span from the first resume at least 780s and under 820s, primary deadline delta exactly 600s, one primary `recovery_begin`, `primary_deadline_kept` once, transaction still active in the interim logs, final `DEGRADED_NEEDS_ATTENTION`, outcome `degraded`, no Start/Restart). `test_b2_unhealthy_board_with_watts_degrades` (nonzero watts and hashrate with an unhealthy board stay in recovery, then `DEGRADED`, not `HASHING`). `test_b2_later_three_healthy_polls_still_hash` (a legitimate later 3-poll healthy sample still reaches `HASHING` inside the 240s window, one resume).

## B3

Every settle poll uses `_classify_settle_obs` and the same `_hard_fault` predicate. A hard fault calls `_fail_hard`, aborts the settle, and does not confirm, resume, retry, or apply pending. `_gate_resume` observes again immediately before the first resume and before the retry resume. Stale or missing telemetry is `not_clean`: it is not `ERROR` by itself, and it does not authorize ResumeMining. The configured settle remains 45s (`cooling_settle_seconds`); a zero option still falls back to the older settle setting rather than skipping the observation.

Tests: `test_b3_hard_fault_during_settle_aborts_before_resume` and `test_b3_thermal_fault_during_settle_aborts_before_resume` (fault injected in `COOLING_SETTLING` before `_settle_complete`; resume 0, retry 0, health `ERROR`, cooling PUTs are the original ceiling only, a later tick adds no writes; the hardware case also holds the pending ceiling). `test_b3_fault_before_put_issues_no_cooling_command` (no pause, PUT, or resume). `test_b3_fault_immediately_before_first_resume` (full 45s settle, then fault on the primary gate; resume 0). `test_b3_fault_during_recovery_before_retry` (one resume already sent, no retry). `test_b3_fault_immediately_before_retry_resume` (one resume, retries used 0). `test_b3_stale_telemetry_is_not_a_hard_fault_and_not_blind_resume` (mining-state HTTP 500 through settle, recovery, and both gates; resume 0, not a hard fault, ends `DEGRADED`).

## B4

Option A is implemented on the existing Braiins reads. `Braiins.board_health()` calls `GET /api/v1/miner/hw/hashboards` and `GET /api/v1/miner/errors`. Public `Hashboard` has no health enum, so a board is proven only when the current payload has `id`, enabled, `chips_count > 0` (int or `{"value": n}`), is not stale, and carries no safety-fault token. Miner errors (`message`, `error_codes[].code`/`reason`, `components[].name`) map onto the same hard-fault tokens. HTTP failure, malformed JSON, a missing list, incomplete entries, stale/partial flags, or an unknown value are not healthy. Option B: if the client has no `board_health` hook, `_apply_live_board_health` sets `boards_healthy=False` and `board_health_verified=False`. It does not trust a raw hashboard list and it does not reuse a previous healthy read. `_hashing_sample_ok` also requires every id in the configured `BOARD_MAP` for that mode to be `proven_healthy` on this read. Expected count is that configured set, not `len(hashboards)`. A partial list can report its present boards as structurally healthy and still fail the mode check.

Tests: `test_b4_live_shape_without_board_health_does_not_hash_on_watts` (hook removed; watts and TH/s do not verify boards or reach `HASHING`; ends `DEGRADED` after two resumes). `test_b4_three_healthy_boards_and_three_polls_hash`. `test_b4_two_of_three_boards_never_hash`. `test_b4_present_unhealthy_board_never_hashes` (`DEGRADED`, not `ERROR`, when the board is explicitly unhealthy without a hard-fault token). `test_b4_missing_malformed_stale_incomplete_do_not_hash`. `test_b4_invalid_sample_resets_stable_count` (a bad sample inside the 3-poll window returns the stable count to 1). `test_b4_cached_healthy_then_unavailable_is_not_hashing`. `test_b4_live_client_parses_hashboards_and_errors_fail_closed` (HTTP 500 fail closed; three complete boards plus empty errors verify healthy; a PSU error sets `safety_fault` and not healthy; a two-board payload does not prove `THREE_BOARD`).

## Existing Safeguards Regression Check

The original recovery cases still pass on the fake clock. Pause still needs two confirming polls. A live cooling PUT while hashing is still refused. Same-value ceilings are still a no-op. A 5xx resume is still bounded unreadiness, not Start/Restart. A non-5xx resume rejection is still `ERROR`. The first telemetry miss is still `UNKNOWN` or `STALE` and does not cancel an active transaction. Device reboot stays on the deny list. Defaults remain `enable_writes` false, `auto_fan_ceiling_enabled` false, and `cooling_writes_only_when_paused` true.

Three older tests had encoded the unsafe early success (running at 0 W or a watt ramp closing the transaction as the target board mode). They now expect `DEGRADED_NEEDS_ATTENTION` after the bounded window, two resumes, and no Start/Restart: `test_power_zero_during_warmup_is_not_failure`, `test_stage_abc_low_rising_watts_no_mature_th`, and `test_resume_does_not_timeout_at_30s_if_stage_a_clears_later`. Case 12 now ends `DEGRADED` rather than leaving health in `RECOVERING` after a false success.

## Remaining Follow-ups

Not changed in this pass:

- `_positive_lifecycle` is still broad (`init` substring, and any `running and not paused` counts as positive), so running at 0 W waits until 600s rather than stopping at 240s.
- `_cooling_escalate_start` and `_cooling_escalate_restart` are still defined and unused on the live resume path.
- Recovery deadlines still use `time.time()` (wall clock), not a monotonic clock.
- Reload and orphan cooling-transaction policy is unchanged.
- There is no concurrency test around overlapping operator requests and the recovery loop.
- Exact 239/240/599/600/780 second boundaries are covered by the window span asserts above, not by one-second edge fixtures.
- Startup and cooldown refusal reasons are not separately documented in `_cooling_refuse_reason`.
- Comments near the Braiins command policy still describe Restart as a last resort even though this recovery path does not call it.

## Test Results

`python3 -m unittest test_controller` from `lard_controller/app`: 98 tests, OK. 23 new tests in `BlockerFixTests` (`test_b1_*` through `test_b4_*`). The suite uses `FakeClock` (`Controller._now` and `_sleep`); there are no real multi-minute sleeps. Command-count asserts check `write_names()`, `cooling_puts()`, and `resume_calls`, and assert that `start` and `restart` are absent after the terminal under test.

## Merge Readiness: READY FOR RE-REVIEW

Do not merge from this change. No deploy, no Home Assistant config change, and no live Braiins command.
