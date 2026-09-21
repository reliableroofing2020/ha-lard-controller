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

Same-value ceiling is a no-op (no pause, no PUT, no resume). A ceiling requested while a transaction is active is coalesced to the newest value and applied after `HASHING` or a terminal degraded/error state. Automatic fan-ceiling ticks do not queue when `auto_fan_ceiling_enabled` is false.

Pause is confirmed by two consecutive miner-state polls, not by HTTP 200 alone. The cooling PUT runs only after that confirmation. Resume runs once after settle. HTTP 500 enters recovery; it does not call Start or BOSminer Restart.

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

- **HASHING:** three consecutive polls, power above the idle floor, hashrate at or above the startup threshold, expected boards present, boards healthy and not stale, no hard fault.
- **RECOVERING:** reachable, legitimate lifecycle (cooldown, cooling down, preheat, startup, init, autotune, APPLYING, or running but not yet a full hash sample). 0 W and 0 TH/s are allowed here.
- **DEGRADED_NEEDS_ATTENTION:** recovery hit the maximum, the one resume retry and the post-retry window did not reach hashing, and there is no hard fault. The miner is left for a person. The controller does not Start, Restart, or reboot.
- **ERROR:** hard fault (overheat, thermal, hardware, board, ASIC, PSU, fan failure, unrecoverable), cooling PUT failed, or resume returned a non-5xx rejection. Those PUT/4xx paths still pause and restore the last known-good ceiling.

A missing or unhealthy board with nonzero watts is not `HASHING`.

## API uncertainties

- `GET /api/v1/cooling/mode` is 405 on this BOS+. Effective ceiling is read from `GET /api/v1/cooling/state` when that payload includes `max_fan_speed`. If the field is absent, PUT 200 + GET 200 is accepted and logged.
- Resume HTTP 500 vs body is not a stable "not ready" schema. 0.1.7 treats 5xx as unreadiness and decides from later miner state, not from the status code alone.
- Lifecycle text is taken from the phase / pause reason / status strings already parsed from `GET /api/v1/miner/details`. There is no new endpoint. Site wording that does not match the known cooldown/preheat/startup tokens will not count as positive lifecycle, so recovery ends at the expected window and uses the one retry.
- `bosminer-experimental.toml` fan min/max is unsupported on BOS+ 26.09 and is not written.
- Board health/staleness is read only when the client exposes `board_health()`. The live Braiins client does not invent a new boards API; without that hook, boards are treated as healthy so existing topology checks still apply.

## Manual prove checklist

Do not arm writes until the observe-only rows pass. Leave `auto_fan_ceiling_enabled` false. Leave `switch.solar_miner_auto_enable` off.

1. Install 0.1.7 with `enable_writes: false`. `/health` is 200. `sensor.lard_controller_health` publishes. No Braiins writes in the log.
2. Unplug or black-hole the miner for one poll. Freshness goes `UNKNOWN` or `STALE`. Actual mode does not become `ERROR` on that first miss. No pause, resume, or cooling PUT.
3. Arm writes only in a planned window (`enable_writes` and the master boolean). Confirm auto fan ceiling is still false: moving `input_number.lard_fan_max_pct` while hashing does **not** PUT cooling.
4. Explicit paused-window proof only: pause, confirm ~0 W, PUT a new ceiling, wait the settle, resume once. Expect `RECOVERING` at 0 W for cooldown/preheat (often ~3 minutes; a cooling change has been seen near 8 minutes). Do not call that `ERROR`.
5. When watts and hashrate are stable across three polls and boards look right, health becomes `HASHING` and actual returns to the board mode.
6. If it is still not hashing at the maximum, expect exactly one more ResumeMining, then either `HASHING` or `DEGRADED_NEEDS_ATTENTION`. No Start, no BOSminer Restart, no device reboot.
7. A real thermal/hardware fault should be `ERROR` without a second automatic resume.
