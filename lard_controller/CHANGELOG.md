# Changelog

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
