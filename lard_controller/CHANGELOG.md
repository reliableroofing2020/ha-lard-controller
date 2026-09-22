# Changelog

## 0.1.8

- Write-enable hardening only. Does not turn on `enable_writes`, `auto_fan_ceiling_enabled`, or `switch.solar_miner_auto_enable`. The default posture stays observe-only.
- Immediate pre-emit authorization on every device-changing call (`_authorize_device_write` / `_call_device`). A denial fails closed, logs `lard_write_denied`, and sends no command. Mid-transaction denial holds the miner for manual review.
- `/actions/start` and `/actions/restart` join reboot and factory reset on the structural deny-list, checked before HTTP. Start and BOSminer Restart escalation helpers stay permanently disabled.
- Recovery extends past the expected window only for named lifecycle tokens (APPLYING, cooldown, preheat, startup, init, tuner, ramping, and the other exact names). Running at 0 W with no named token exhausts at the expected window, then one resume retry and the post-retry window. `init` does not match `reinitializing`.
- Transaction deadlines, settle, retry, and budgets use `time.monotonic()`. Wall clock is for human-facing stamps only. A process reload does not reuse a previous monotonic deadline.
- Reload and cancel publish `INTERRUPTED_MANUAL_REVIEW`, drop pending auto-apply, and do not Resume, Start, Restart, reboot, or PUT.
- One cooling-transaction owner. Concurrent ceiling requests coalesce to the newest pending value. Device emits are serialized and cannot overlap.
- Absent, null, or malformed options cannot arm writes or automatic fan ceiling. `cooling_writes_only_when_paused` stays true. B1–B4 protections from 0.1.7 are unchanged.

## 0.1.7

- Bounded cooling recovery state machine. After a gated pause → cooling PUT → settle, ResumeMining is issued **once**. 0 W / 0 TH/s during cooldown, preheat, startup, init, or APPLYING stays `RECOVERING` and is not `ERROR`.
- Windows: post-write settle 45s (`cooling_settle_seconds`), poll 10s, expected recovery 240s, maximum 600s. Between expected and maximum the controller keeps waiting only while Braiins reports a positive lifecycle. At the maximum, exactly one guarded resume retry, then a 180s post-retry window. If that still does not hash: `DEGRADED_NEEDS_ATTENTION`, not `ERROR`.
- Full `HASHING` health needs 3 consecutive polls with plausible watts, hashrate above the startup threshold, every configured board proven healthy on the current read, and no hard fault. Live board health comes from `GET /api/v1/miner/hw/hashboards` plus `GET /api/v1/miner/errors`. A missing hook, stale/malformed/incomplete payload, or unhealthy board is not healthy. Watts and TH/s alone are not `HASHING`.
- Hard faults (overheat, hardware/board/PSU/fan failure, unrecoverable), including a fault seen on a settle poll or immediately before ResumeMining, go straight to `ERROR`. No ResumeMining after that fault, no Start, no BOSminer Restart, no device reboot, and no extra cooling PUT on that path.
- Telemetry timeouts publish `UNKNOWN` / `STALE` and keep the last good reading. They do not ERROR or issue corrective commands on the first miss. `ERROR` only after `telemetry_failures_before_error` (default 3). Timeouts during a cooling transaction do not cancel or overwrite that transaction.
- Per-miner lock. Same-value cooling is a no-op (no pause/resume). Requests during a transaction coalesce to the newest ceiling and auto-apply only after successful `HASHING`. Confirmed `PAUSED`, `DEGRADED_NEEDS_ATTENTION`, and `ERROR` keep that pending ceiling visible and do not start another cooling cycle. Running or APPLYING inside the recovery window does not close the transaction or reset the 600s deadline. Start and BOSminer Restart escalation helpers are hard-disabled and issue no device command. No live cooling PUT while hashing.
- `auto_fan_ceiling_enabled` stays **false**. Automatic fan-ceiling changes stay off. `cooling_writes_only_when_paused` stays true. `enable_writes` still defaults false. `switch.solar_miner_auto_enable` is still never turned on.
- Observability: `sensor.lard_controller_health` plus cooling transaction attributes (txn id, phase, ceilings, resume result, recovery elapsed/remaining, lifecycle reason, telemetry freshness). Structured `lard_cooling_*` / `lard_telemetry_*` events.

## 0.1.6

- After a gated cooling PUT, do **not** treat the first `ResumeMining` HTTP 500 as a hard fail. Cooling apply is a disruptive config transition: BOSminer may not accept resume until the process settles (`bosminer_uptime_s == 0` means the process is not running).
- Sequence after PUT + confirm: stay `APPLYING` → poll pause / process-ready / watts / status → wait `cooling_resume_settle_seconds` (default 20) → bounded ResumeMining backoff (5s / 10s / 20s) → escalate `PUT /api/v1/actions/start` if still 500 → then BOSminer `PUT /api/v1/actions/restart`. Full device reboot (`/actions/reboot`) is still denied and is never used.
- `ERROR` only after that bounded recovery is exhausted. Intentional pause during the window is still not `ERROR`. No legacy handoff.
- Logs whether the cooling PUT temporarily changes bosminer uptime, miner-ready, or pause reason (distinguishes “not ready yet” vs hard failure). Existing 0.1.5 pause-first cooling rules are unchanged. `enable_writes` still defaults false. No hard-coded final fan %.

## 0.1.5

- Cooling / fan-profile writes are **maintenance-gated**. A live `PUT /api/v1/cooling/mode` while hashing stalls this site's BOS+ (~26.09) miner (PAUSED/0W → APPLYING/0W / `read_boards_http_500`). The add-on now never writes cooling mid-hash.
- When the desired profile differs from the applied profile: APPLYING → serialize Braiins writes → pause (`user_pause`) → verify pause + ~0 W → tagged `{"auto":{"max_fan_speed": N, ...}}` PUT → read back / confirm → short stabilize → resume → verify run / boards / watts / TH recovering → only then publish the operating mode as actual.
- Intentional pause during this sequence is not `ERROR`. The controller stays `APPLYING` for the whole transition.
- Cooling PUT failure or resume failure: restore the known-good profile if possible, pause the miner safely, enter `ERROR`. No hand-off to legacy fan automations or `switch.solar_miner_auto_enable`.
- Idempotent: desired == applied skips pause and cooling PUT. Dwell (`cooling_dwell_seconds`, default 600) blocks rapid re-transitions from solar/SOC/slider flaps. Thermal abort (`CHIP_ABORT_F=180`) still restores unconstrained 100, but through the same gated sequence (dwell bypassed).
- One configurable envelope per major board-count state (ONE_BOARD / TWO_BOARD / THREE_BOARD / PAUSED). Values are **TBD/measured placeholders**, not hard finals. `input_number.lard_fan_max_pct` is only an envelope cap on the desired profile — it never live-PUTs while hashing.
- Auth unchanged: raw `Authorization` token (no Bearer). Cooling API remains `PUT /api/v1/cooling/mode`. The old `PUT /api/v1/cooling {"mode":"automatic"}` wipe path stays gone. Only this add-on writes cooling; keep `automation.solar_miner_fan_watchdog` off.

## 0.1.4

- Own Braiins OS+ fan max ceiling over the LAN API. `PUT /api/v1/cooling/mode` with tagged-union `{"auto":{"max_fan_speed": N}}` (integer percent 0–100). The old wipe path `PUT /api/v1/cooling {"mode":"automatic"}` is gone.
- HA helper `input_number.lard_fan_max_pct` (0–100, step 1, default 100) is required. Package: `ha_packages/lard_fan_max.yaml`. The add-on ensures a state stub on startup if the helper is missing; install the package for a real slider.
- Ceiling is applied on helper change, add-on start, Braiins login/reconnect, and after `apply_mode` completes so a control cycle cannot wipe it. Cool path only: does not set `APPLYING`, pause, write 0 W, or restart boards.
- Cooling API failures are logged independently and never fail the mining-control / `apply_mode` loop.
- `CHIP_ABORT_F=180`: if chip temp or a thermal/cooling fault crosses that threshold, restore unconstrained auto (`max_fan_speed=100`, `minimum_required_fans=2`) immediately. No new mining-pause rule.
- Fan-ceiling writes require `enable_writes` (same add-on gate as other Braiins writers). They still refuse when `switch.solar_miner_auto_enable` is on. They do not force AUTO or enable legacy writers.

## 0.1.3

- If requested hashboard topology already matches, skip PATCH and `board_wait` (Stage B satisfied). PAUSED→ONE_BOARD shares `["1"]` and goes straight to staged resume.
- Board id compares are normalized (`1` / `"1"` / `1.0`). `board_wait_timeout` cannot fire when the poll already returns the expected set.
- One empty or partial board read during pause/ramp retries with 2s / 5s / 10s / 20s backoff; it is not a hard fail.
- `enable_writes` still defaults false. Resume A→C criteria from 0.1.2 are unchanged.

## 0.1.2

- Resume confirmation is staged (A: accepted + no longer paused, B: boards match via async poll, C: running/preheat/ramping and watts or hashrate begin rising). Mature/full hashrate is not required in the resume window.
- Resume wait is 120s (`RESUME_WAIT_S`). A miner that leaves `user_pause` after 30s no longer false-fails.
- After Stage C, resume is operationally successful and actual stays `APPLYING` until running+boards confirm the live mode.
- Transient Braiins HTTP 5xx during board/ramp transitions retry with 2s / 5s / 10s / 20s backoff. A single 500 does not ERROR; sustained 5xx after that window does.
- `api_fail_count` (Braiins `fail_count`) resets after a successful authenticated read so a transient error does not poison the next state.
- Hashboard PATCH HTTP 200 remains accepted-not-applied; topology is still polled. `enable_writes` still defaults false.

## 0.1.1

- Mode reconciliation no longer treats hashboard topology as a confirmed live mode.
- Desired mode stays separate from confirmed operational/actual (`PAUSED`, `APPLYING`, `ONE_BOARD`, `TWO_BOARD`, `THREE_BOARD`, `ERROR`).
- `ONE_BOARD` / `TWO_BOARD` / `THREE_BOARD` confirm only when expected boards are set, the miner is not user-paused, mining is running, and Braiins reports resumed/operational state. Power=0 during resume warmup is not a failure.
- Restart/idempotent ticks resume a paused miner even when boards already match; they skip pause/resume when already running and skip all writes when already converged.
- Unit tests cover the paused/running/wrong-board/PAUSED and add-on restart cases.

## 0.1.0

- First Supervisor-owned local add-on for the LARD board-priority Braiins actuator.
- Dual write gate: `enable_writes` (default false) and `input_boolean.lard_board_priority_enable`.
- s6-overlay foreground exec; container exits on crash so Supervisor restarts it.
- HTTP health on :8099 wired to the add-on watchdog.
- HA REST heartbeat entities; optional MQTT discovery + LWT.
