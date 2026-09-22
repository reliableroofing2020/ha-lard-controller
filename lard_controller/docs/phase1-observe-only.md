# Phase 1 — observe only (0.1.12)

This add-on version classifies the miner and refuses Braiins writes unless a human later re-arms them. It does not deploy itself, does not set `enable_writes`, and does not call the miner.

`enable_writes` still defaults **false**. On process start and on reload the write gate is disarmed. Pause, resume, power-target, hashboard PATCH, and cooling PUT return before any HTTP body is built. The log line is:

```text
write blocked op=<pause|resume|set_power|patch_boards|cooling_put|arm> source=<controller|braiins|tick> reason=<...> state={...}
```

Recovery readiness does not flip that gate.

## Operator note

Leave the add-on in observe-only until bosminer has been stable on the LAN and the Home Assistant fence below is actually in place.

Published separately:

| Field | Meaning |
| --- | --- |
| `sensor.lard_controller_requested_mode` | What LARD was asked for (`desired_mode`) |
| `sensor.lard_controller_actual_mode` | Last settled observation. Not left at `APPLYING` when the API or bosminer is down |
| Health attribute `observed_state` | Phase 1 class for this poll |
| Health attribute `telemetry_class` | Same class, including `FAULT_LATCHED` or `WAITING_FOR_BRAIINS` |
| Health attribute `recovery_ready` | Five good polls. Advisory only |
| Health attribute `writes_permitted` | False except during one already-authorized call |

Do not treat `recovery_ready=true` as permission to mine. A person turns `enable_writes` on later, and only after the fence is verified. `input_boolean.lard_board_priority_enable` is still required. `switch.solar_miner_auto_enable` must stay off.

## Telemetry classes

| Class | When |
| --- | --- |
| `BOSMINER_UNAVAILABLE` | Gateway body or status says connection refused (`os error 111`), "BOSminer is not running", or HTTP 412. This is not a successful read and it is not a reason to write |
| `API_UNREACHABLE` | The read failed and the text is not auth, bosminer, or malformed |
| `AUTHENTICATION_FAILED` | HTTP 401 or an authentication-token error |
| `REQUIRED_TELEMETRY_MALFORMED` | Required board/details payload is malformed |
| `VALID_PAUSED` | Pause evidence. **0 W is normal** |
| `VALID_TRANSITION` | Only when this poll has coherent lifecycle evidence (preheat, ramp, startup, init, tuner, and the other exact tokens). Never inferred from 0 W or from a stuck `APPLYING` label |
| `RUNNING_HEALTHY` | Running, no critical fault, power above the idle threshold |
| `FAULT_LATCHED` | Critical fault, or hashboard PATCH HTTP 200 whose readback does not match. Expected `[1,2,3]` with actual `[1]` or `[]` is faulted/unverified |
| `WAITING_FOR_BRAIINS` | API/bosminer is down **and** fresh lifecycle evidence still exists. Otherwise the controller leaves `APPLYING` for `FAULT_LATCHED` |

Zero watts alone is not a fault.

## Bosminer unavailable vs telemetry failure

| What you see | What it means | What LARD does |
| --- | --- | --- |
| `cooling_state` or `read_boards` HTTP 500 with `Connection refused (os error 111)`, or HTTP 412 "BOSminer is not running" | The REST gateway answered. **bosminer** did not. Phase 0 saw this while hashrate/power collapsed and `read_boards_http_500` was the sticky error | Class `BOSMINER_UNAVAILABLE`. If actual was `APPLYING` with no fresh lifecycle evidence, publish `FAULT_LATCHED` and stop. No pause, resume, PATCH, or power write |
| `telemetry_sustained_unavailable` after repeated generic read failures | The add-on missed required reads and was not inside a cooling transaction | Existing streak still raises `ERROR` after `telemetry_failures_before_error` (default 3). That path does not issue a corrective write |
| `helper_miss entity=input_number.lard_cooling_*_f` | Optional alias is missing. The canonical `*_c` helpers (values in °F) are the source of truth | One line per backoff window. **Not** a miner failure. `api_fail_count` does not climb from these quiet misses while cooling control is off |
| `sensor.lard_controller_power_w` = 0 while class is `VALID_PAUSED` or `VALID_TRANSITION` | Idle or a named lifecycle | Not a fault |

Bosminer flap is outside this add-on. Fix bosminer on the miner host before any re-arm. LARD only classifies it.

## Cooling helper migration

| Entity | Unit | Phase 1 |
| --- | --- | --- |
| `input_number.lard_cooling_target_c` | °F (historical id) | **Canonical.** Keep. Do not rename in this change |
| `input_number.lard_cooling_hot_c` | °F | Canonical. Keep |
| `input_number.lard_cooling_dangerous_c` | °F | Canonical. Keep |
| `input_number.lard_cooling_*_f` | optional alias | **Do not have to exist.** If the canonical `*_c` helper has a number, the `*_f` id is not polled. If it 404s, the miss is cached (30s, doubling, cap 600s, monotonic clock) with one diagnostic per window |
| `input_number.lard_cooling_envelope_{min,max}_pct` | percent | Keep |
| Per-board `lard_cooling_{one,two,three,paused}_board_max_pct` | percent | Still optional. Cooling control is off, so they are not actuators |

The `_c` suffix is not metadata. A stored 158 stays 158 °F. LARD does not convert it because the id contains `_c`. Conversion to Braiins `degree_c` happens only inside a cooling PUT body, and cooling control defaults **false**, so that body is not sent.

No Home Assistant entity is deleted by this version.

## Home Assistant writer fence

This repository does not change Home Assistant automations. The live fence is parallel work. Expected state before anyone re-arms LARD:

- Add-on `enable_writes` remains **false**
- `switch.solar_miner_auto_enable` remains **off** (LARD never turns it on)
- `automation.solar_miner_upstairs_ac` must not write `input_select.lard_miner_mode_request` (mode step-down). Point that branch at a non-actuator, or turn the automation off for the miner path
- `script.solar_miner_pause`, resume, and set-target are not on a schedule
- `button.lard_mining_pause` / `button.lard_mining_resume` are not pressed by an agent loop
- `automation.solar_miner_power_governor` and the other pause/verify automations stay off
- Do not run direct hashboard PATCH or restore scripts against the miner
- While any of those writers are still live, set `binary_sensor.lard_competing_writer` to **on**. LARD treats that as an arming denial and will not command the miner. A missing sensor is not a writer and is not polled on a hot loop (same miss cache as optional helpers)

`input_boolean.lard_board_priority_enable` can stay on. With `enable_writes` false it does not authorize writes.

## Observe-only checklist

1. Install or reload 0.1.12 with `enable_writes` false and `cooling_control_enabled` false.
2. Startup log contains `WRITES DISARMED` and `writes_permitted=false`.
3. Requested mode and observed state both publish. Observed state is not stuck on `APPLYING` while bosminer is refused.
4. Add-on log has no `PATCH`, `actions/pause`, `actions/resume`, or `power-target` **from this add-on** while the gate is disarmed. A `write blocked` line is a refusal, not a command.
5. `helper_miss` for `lard_cooling_*_f` appears at most once per backoff window, not every poll.
6. `sensor.lard_controller_api_fail_count` is not driven by those helper misses.
7. After five valid polls (auth ok, bosminer available, no critical fault, competing-writer flag off), `recovery_ready` may become true. `enable_writes` is still false. `writes_permitted` is still false.
8. No cooling PUT. Braiins still owns cooling.

## Rollback

1. Keep `enable_writes` false during the rollback. Do not re-arm to "undo" a classification.
2. Redeploy the previous add-on image (**0.1.11**) from the Supervisor panel or your usual image pin. This git change does not deploy.
3. Leave the canonical `input_number.lard_cooling_*_c` helpers as they are. Do not delete them and do not invent `_f` entities as part of rollback.
4. Leave `switch.solar_miner_auto_enable` off.
5. Confirm the new container log says version `0.1.11` and `WRITES DISARMED` before walking away.

## Explicitly not in 0.1.12

Automatic board ladder, power tiers, hashrate-target automation, loft HVAC control, cooling writes, and any live Braiins command.
