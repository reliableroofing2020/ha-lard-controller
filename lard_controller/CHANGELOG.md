# Changelog

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
