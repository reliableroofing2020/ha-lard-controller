# Changelog

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
