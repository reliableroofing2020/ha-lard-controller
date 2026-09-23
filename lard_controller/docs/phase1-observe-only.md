# Phase 1 — observe only (0.1.12)

This add-on version classifies the miner and refuses Braiins writes unless a human later re-arms them. It does not deploy itself, does not set `enable_writes`, and does not call the miner.

`enable_writes` still defaults **false**. On process start and on reload the controller state is `DISARMED` and `writes_permitted` is false. `write_gate` is the option posture: `DISARMED` while `enable_writes` is false, `ARMED` only when that option is true. `write_gate=ARMED` is not authorization. The master boolean, the old auto switch, and a competing writer can still deny the call. No code path sets `controller_state` to `ARMED`. `MAINTENANCE_LOCKOUT` is reserved and unused: there is no maintenance PATCH tool.

Pause, resume, power-target, hashboard PATCH, cooling PUT, `apply_mode`, and the legacy cooling requests refuse **before** a request object is built when `enable_writes` is false. An unbound Braiins client is included: `enable_writes` false blocks even if no controller has bound a gate. The log line and the return value are:

```text
write blocked result=WRITE_BLOCKED op=<...> requested_operation=<...> source=<controller|braiins> reason=<...> controller_state=<...> enable_writes=false network_write_sent=false state={...}
```

```json
{"result":"WRITE_BLOCKED","requested_operation":"...","source":"...","controller_state":"DISARMED","enable_writes":false,"reason":"enable_writes_false","network_write_sent":false,"denied":true,"write_blocked":true,"op":"..."}
```

`network_write_sent` is false. Start, restart, reboot, and factory reset raise before any socket instead of returning this record. The full path list is [phase1-writer-inventory.md](phase1-writer-inventory.md).

Recovery readiness does not flip the gate and does not mean armed.

## Operator note

Leave the add-on in observe-only until bosminer has been stable on the LAN and the Home Assistant fence below is actually in place.

Published separately:

| Field | Meaning |
| --- | --- |
| `sensor.lard_controller_requested_mode` | What LARD was asked for (`desired_mode`). Not the miner |
| `observed_miner_mode` | Verified physical mode only: `PAUSED`, `ONE_BOARD`, `TWO_BOARD`, `THREE_BOARD`. Otherwise `UNVERIFIED` |
| `controller_state` | Controller lifecycle actually published: `DISARMED`, `OBSERVING`, `APPLYING`, `WAITING_FOR_BRAIINS`, `RUNNING`, `FAULT_LATCHED`, `ERROR`. `ARMED` and `MAINTENANCE_LOCKOUT` are in the typed set and are not assigned. Option posture is `write_gate` |
| `write_gate` | `DISARMED` or `ARMED` from `enable_writes` only. Not a physical mode and not permission to write |
| `health_classification` | Same value as `telemetry_class` for this poll |
| `api_reachable` / `bosminer_available` | Separate. A gateway HTTP 500 can be reachable and still bosminer-down |
| `sensor.lard_controller_power_w` / `sensor.lard_controller_boards` | Fresh only after a required boards+details read. Otherwise state `unknown` / `unverified`, with `last_power_w` / `last_boards` kept as attributes |
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

## Freshness

Required telemetry is `GET /api/v1/miner/hw/hashboards` plus `GET /api/v1/miner/details`. A failed required read publishes power as `unknown` and boards as `unverified`. The last good watts and board list stay in attributes. They are not the sensor state. Cooling `GET /api/v1/cooling/state` is optional: its failure class is recorded on the `cooling` endpoint and does not by itself become a board or API fault. Each of `boards`, `details`, and `cooling` keeps `last_success_mono`, `last_failure_mono`, `last_failure_class`, and a redacted `last_failure_summary`.

There is no second 5-second poller. These notes are taken from the existing tick. Optional `lard_cooling_*_f` misses keep the existing negative cache (30s, doubling, cap 600s). Canonical `input_number.lard_cooling_*_c` values are °F. This version does not convert them and does not add another `_c` / `_f` helper layer.

A disarmed tick, a fault latch, and a reload drop any coalesced cooling profile so a later arm cannot replay it. A failed board readback does not re-PATCH. `APPLYING` and `WAITING_FOR_BRAIINS` both end in `FAULT_LATCHED` when the monotonic evidence deadline passes.

## Aleixps

Concepts borrowed, not code: separate API reachability from bosminer availability, per-endpoint freshness, and do not present a failed required read as fresh. Do not install Aleixps. Do not copy or vendor that source. The license is unclear and LARD's implementation is independent.

## Bosminer unavailable vs telemetry failure

| What you see | What it means | What LARD does |
| --- | --- | --- |
| `cooling_state` or `read_boards` HTTP 500 with `Connection refused (os error 111)`, or HTTP 412 "BOSminer is not running" | The REST gateway answered. **bosminer** did not. Phase 0 saw this while hashrate/power collapsed and `read_boards_http_500` was the sticky error | Class `BOSMINER_UNAVAILABLE`. If the controller is verifying (`APPLYING` or `WAITING_FOR_BRAIINS`) and independent evidence is still inside the window, publish bounded `WAITING_FOR_BRAIINS`. When that window ends, or there was no independent evidence, publish `FAULT_LATCHED`. No pause, resume, PATCH, or power write |
| `telemetry_sustained_unavailable` after repeated generic read failures | The add-on missed required reads and was not inside a cooling transaction | The streak still raises `ERROR` after `telemetry_failures_before_error` (default 3). No corrective write. The active error stays until five consecutive coherent boards+details polls (`SUSTAINED_TELEMETRY_RECOVERY_POLLS`, equal to `RECOVERY_READY_POLLS`). One good read does not clear it. See the 0.1.14 section below |
| `helper_miss entity=input_number.lard_cooling_*_f` | Optional alias is missing. The canonical `*_c` helpers (values in °F) are the source of truth | One line per backoff window. **Not** a miner failure. `api_fail_count` does not climb from these quiet misses while cooling control is off |
| `sensor.lard_controller_power_w` = 0 while class is `VALID_PAUSED` or `VALID_TRANSITION` | Idle or a named lifecycle | Not a fault |

Bosminer flap is outside this add-on. Fix bosminer on the miner host before any re-arm. LARD only classifies it.

## Sustained telemetry outage (0.1.14)

`telemetry_sustained_unavailable` means required `GET /api/v1/miner/hw/hashboards` and `GET /api/v1/miner/details` missed `telemetry_failures_before_error` times (default 3) while no cooling transaction was open. The controller sets `sensor.lard_controller_health` to `ERROR`, `cooling_phase` to `ERROR`, and `last_error` to that string. It does not pause, resume, PATCH, or PUT.

That outage is also recorded once per episode, and the record is kept after the miner is readable again:

| Attribute | Meaning |
| --- | --- |
| `last_fault_reason` | `telemetry_sustained_unavailable` for this outage. History, not the current health |
| `last_fault_class` | `ERROR`, the health class the outage forced |
| `last_fault_timestamp` | Wall-clock time when this episode was first latched. Not a monotonic deadline |
| `last_fault_count` | How many such episodes have been recorded. Repeat misses inside one episode do not increment it |
| `current_error` | Same as `last_error`. Empty when nothing is active |
| `active_fault` | True only while `last_error` is non-empty |
| `sustained_telemetry_recovery_polls` | Consecutive coherent polls counted toward clearing this latch. 0 after a bad required read |
| `sustained_telemetry_recovery_required` | Clear threshold. `SUSTAINED_TELEMETRY_RECOVERY_POLLS`, which is `RECOVERY_READY_POLLS` (5). Not `recovery_ready` |

While the outage is active, health is `ERROR`, `current_error` equals `last_fault_reason`, and `sensor.lard_controller_error` is `telemetry_sustained_unavailable`. That stays true through polls 1–4 even when `telemetry_freshness` is `FRESH` and `telemetry_class` is already `RUNNING_HEALTHY`, `VALID_PAUSED`, or `VALID_TRANSITION`.

`SUSTAINED_TELEMETRY_RECOVERY_POLLS` is the sustained-telemetry ERROR clear threshold. It reuses the number 5 from `RECOVERY_READY_POLLS`. It is not the recovery-ready streak and it does not arm writes. A sample counts only when all of these are true, and at most once per `poll_seconds`:

1. The active fault is exactly `telemetry_sustained_unavailable` and health is `ERROR`.
2. Boards and details both succeeded, are not malformed, and the board set is a verified healthy `ONE_BOARD`, `TWO_BOARD`, or `THREE_BOARD` topology. Freshness is `FRESH` and the fail streak is 0.
3. `telemetry_class` is `RUNNING_HEALTHY`, `VALID_PAUSED`, or `VALID_TRANSITION`. `VALID_TRANSITION` still requires miner lifecycle evidence (preheat, ramping, tuning, and the other documented tokens). The word `applying` does not qualify. Zero watts with only `applying` stays failed closed.
4. Nothing else is wrong: not `FAULT_LATCHED`, not a hard fault, not an unverified or partial board read, not an auth / API / bosminer failure, not a write failure, not a cooling-transaction terminal (`degraded`, `error`, `interrupted`).
5. No cooling transaction and no cooling transition is active.
6. At least `poll_seconds` have passed since the previous counted sample (or since the last reset). A duplicate read inside that interval does not increment and does not reset.

A missing, malformed, stale, or contradictory required observation resets `sustained_telemetry_recovery_polls` to 0. The next count waits another full poll interval. Structural blockers (fault latch, cooling terminal, active cooling transaction) do not increment and do not clear.

The active error clears only on poll 5, when the count reaches `SUSTAINED_TELEMETRY_RECOVERY_POLLS`. Then `last_error` becomes empty (it is the current error only). Health leaves `ERROR` for `HASHING`, `PAUSED`, or `RECOVERING` using the existing evidence rules. A one-board miner with watts above the idle threshold and hashrate above the startup threshold can be `HASHING` without a three-board minimum. A coherent pause at 0 W is `PAUSED` / `VALID_PAUSED`, not `HASHING`. An evidenced preheat, ramp, or tune is `RECOVERING` / `VALID_TRANSITION`, not `HASHING`. If `cooling_phase` was `ERROR` only because of this latch, it returns to `IDLE`. Polls 1–4 keep health at `ERROR`.

`last_fault_*` does not change on recovery. Read health from `sensor.lard_controller_health` and the active error from `last_error` / `current_error` / `active_fault` / `sensor.lard_controller_error`. Do not treat `last_fault_reason` as a live fault. A dashboard line is: current telemetry healthy; prior telemetry outage recorded at `last_fault_timestamp`.

This recovery does not set `enable_writes`, does not turn on `switch.solar_miner_auto_enable`, and does not issue a miner command. `write_gate` stays `DISARMED` while `enable_writes` is false. Reload still starts `DISARMED`. `recovery_ready` still does not arm writes.

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

This repository fences every in-repo write path. A read-only inventory on 2026-09-22 ~16:55 CT found paths that never enter that fence. `button.lard_*`, `number.lard_power_target`, `number.lard_hashrate_target`, `select.lard_performance_mode`, and `switch.lard_device_locate_led_blinking` are `braiins_os_plus` entities aimed at miner `192.168.1.113`. `script.solar_miner_pause`, `resume_min`, `set_target`, and `evaluate` call those entities. The Lovelace `solar-miner` dashboard exposes `number.lard_power_target`. Several of those buttons were pressed about 15:31–15:51 CT the same day. LARD `enable_writes=false` does not stop them.

That is an observe-only **deploy blocker**. The recommended disposition is a runbook in [phase1-writer-inventory.md](phase1-writer-inventory.md). Do not apply it from this repository.

Already confirmed on that inventory, and left unchanged:

- `automation.solar_miner_upstairs_ac` writes `climate.loft` and logbook only
- `switch.solar_miner_auto_enable` is off (that does not block a manual button or script)
- latent solar automations that call the scripts are off
- deployed add-on 0.1.11 has `enable_writes` false
- no `rest_command` or `shell_command` is registered

`binary_sensor.lard_competing_writer` ON only denies LARD arming. It does not stop `braiins_os_plus`. A missing sensor is not a writer.

`input_boolean.lard_board_priority_enable` can stay on. With `enable_writes` false it does not authorize LARD writes.

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

## Still outside this add-on

The deployed supervisor image seen in the prior live review was **0.1.11**, with bosminer instability (`read_boards_http_500`, health `UNKNOWN`). This git change does not deploy 0.1.12 and does not repair or restart BOSminer. Live miner HTTP from this task is not required and was not used.

Automatic board ladder, power tiers, hashrate-target automation, loft HVAC control, cooling writes, and any live Braiins command stay out. Observe-only deployment, merge, and write arming are not authorized.
