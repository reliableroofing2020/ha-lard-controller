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
| `sensor.lard_controller_requested_mode` | What LARD was asked for (`desired_mode`). Not the miner |
| `observed_miner_mode` | Verified physical mode only: `PAUSED`, `ONE_BOARD`, `TWO_BOARD`, `THREE_BOARD`. Otherwise `UNVERIFIED` |
| `controller_state` | Controller lifecycle: `DISARMED`, `OBSERVING`, `APPLYING`, `WAITING_FOR_BRAIINS`, `RUNNING`, `FAULT_LATCHED`, `ERROR` |
| `sensor.lard_controller_actual_mode` | Compatibility sensor. See the migration note below. Not a physical mode when the value is `APPLYING`, `WAITING_FOR_BRAIINS`, `FAULT_LATCHED`, or `ERROR` |
| Health attribute `telemetry_class` | This poll's miner-plane class |
| Health attribute `recovery_ready` | Five good polls. Advisory only. Does not clear `FAULT_LATCHED` and does not arm writes |
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
| `VALID_TRANSITION` | This poll has current independent miner-side lifecycle evidence. Never inferred from 0 W, from the word `applying`, or from LARD's own requested/actual/controller state |
| `RUNNING_HEALTHY` | Running, no critical fault, power above the idle threshold |
| `FAULT_LATCHED` | Verification could not be completed, a critical fault, or hashboard PATCH HTTP 200 whose readback does not match. The reason string distinguishes bosminer loss, missing telemetry, malformed/contradictory telemetry, board readback mismatch, and expired transition evidence |
| `WAITING_FOR_BRAIINS` | Bounded wait. API or bosminer is down **and** independent lifecycle evidence is still inside `expected_recovery_seconds` (default 240, monotonic). It is not a resting state |

## WAITING_FOR_BRAIINS is bounded

`APPLYING` may enter `WAITING_FOR_BRAIINS` only when a previous poll recorded independent miner lifecycle evidence and that evidence is still inside the configured window. The wait stores a transaction id when one exists, the monotonic time it was entered, the expected operation (`desired_mode`), the evidence deadline (evidence time plus `expected_recovery_seconds`, not a larger timeout), the last valid required-telemetry monotonic time, and the reason.

Every later poll re-checks required telemetry, bosminer/API availability, independent miner lifecycle, and the deadline. When the deadline passes, or the read is malformed, contradictory, or only the controller word `applying`, the controller enters `FAULT_LATCHED`. It does not stay in `WAITING_FOR_BRAIINS`. It does not arm writes and it does not retry a Braiins command.

`FAULT_LATCHED` reasons:

| Token | Meaning |
| --- | --- |
| `bosminer_unavailable_during_verification` | Connection refused, bosminer not running, or the same class of gateway failure while a transition was being verified |
| `required_telemetry_unavailable` | API unreachable or authentication failed during verification |
| `required_telemetry_malformed` | Malformed payload, or an ok read that cannot establish a miner lifecycle (including 0 W / 0 TH/s with only `applying`) |
| `contradictory_telemetry` | The miner report is a hard fault or contradicts itself |
| `board_verification_timeout_or_mismatch` | Hashboard PATCH readback did not match, including HTTP 200 |
| `valid_transition_evidence_expired` | The independent lifecycle evidence aged out of the monotonic window |

## What valid transition evidence means

`VALID_TRANSITION` requires a current ok read from `GET /api/v1/miner/details`, parsed by `parse_mining_state`. These miner fields count:

- `detailed_status` phase equal to `starting`, `preheating` / `preheat` / `warming` / `warmup`, or `ramping` / `ramp` / `quick_ramping` (parser flags `starting`, `preheating`, `ramping`)
- exact tokens on miner `phase`, `pause_reason`, or `status`: `cooldown`, `cooling_down`, `startup`, `init`, `initializing`, `autotune` / `tuning` / `tuner`, `booting`, and the phase names above

Those fields are the miner's report. They are not `desired_mode`, not `sensor.lard_controller_requested_mode`, not `actual_mode`, and not `controller_state`.

The word `applying` is a LARD request / cooling-interim label. It is not in the miner token set. 0 W and 0 TH/s plus only `applying` is not `VALID_TRANSITION`. If the controller was verifying a change, that read fails closed to `FAULT_LATCHED`.

A coherent miner pause at 0 W / 0 TH/s is `VALID_PAUSED`, not a fault.

## What BOSMINER_UNAVAILABLE means

The REST gateway answered and the body or status says bosminer is not there: connection refused (`os error 111`), "BOSminer is not running", or HTTP 412. That is not a successful read, not a pause, and not a transition. During verification it starts a bounded `WAITING_FOR_BRAIINS` only when independent evidence is still fresh. Otherwise, and always after the deadline, it is `FAULT_LATCHED` with `bosminer_unavailable_during_verification`.

## actual_mode migration

`ACTUAL_MODES` used to be `PAUSED`, `APPLYING`, `ONE_BOARD`, `TWO_BOARD`, `THREE_BOARD`, `ERROR`. Runtime also published `WAITING_FOR_BRAIINS` and `FAULT_LATCHED`, which were missing from that tuple.

Those two values are now in `ACTUAL_MODES` so the compatibility sensor `sensor.lard_controller_actual_mode` (and `sensor.lard_miner_mode_actual`) can still show the latch. Treat that sensor as a compatibility union:

- physical only when `actual_mode_is_physical` is true, or when the value is in `PAUSED | ONE_BOARD | TWO_BOARD | THREE_BOARD`
- otherwise it is controller lifecycle, also published as `controller_state`

Dashboards that need the miner, not the controller, should read attribute `observed_miner_mode`. `UNVERIFIED` means this poll did not prove a physical mode. Do not chart `WAITING_FOR_BRAIINS` or `FAULT_LATCHED` as a hashboard mode. Requested intent stays on `desired_mode` / `sensor.lard_controller_requested_mode`.

Zero watts alone is not a fault.

## Bosminer unavailable vs telemetry failure

| What you see | What it means | What LARD does |
| --- | --- | --- |
| `cooling_state` or `read_boards` HTTP 500 with `Connection refused (os error 111)`, or HTTP 412 "BOSminer is not running" | The REST gateway answered. **bosminer** did not. Phase 0 saw this while hashrate/power collapsed and `read_boards_http_500` was the sticky error | Class `BOSMINER_UNAVAILABLE`. If the controller is verifying (`APPLYING` or `WAITING_FOR_BRAIINS`) and independent evidence is still inside the window, publish bounded `WAITING_FOR_BRAIINS`. When that window ends, or there was no independent evidence, publish `FAULT_LATCHED`. No pause, resume, PATCH, or power write |
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
3. Requested mode, `observed_miner_mode`, and `controller_state` all publish. `controller_state` is not left on `WAITING_FOR_BRAIINS` after the evidence deadline. `observed_miner_mode` is never `APPLYING`, `WAITING_FOR_BRAIINS`, or `FAULT_LATCHED`.
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
