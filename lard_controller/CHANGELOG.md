# Changelog

## 0.1.0

- First Supervisor-owned local add-on for the LARD board-priority Braiins actuator.
- Dual write gate: `enable_writes` (default false) and `input_boolean.lard_board_priority_enable`.
- s6-overlay foreground exec; container exits on crash so Supervisor restarts it.
- HTTP health on :8099 wired to the add-on watchdog.
- HA REST heartbeat entities; optional MQTT discovery + LWT.
