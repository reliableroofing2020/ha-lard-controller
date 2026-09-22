# Phase 1 writer inventory (0.1.12)

In-repo miner write paths are fenced. Live Home Assistant `braiins_os_plus` entities are not. `enable_writes` defaults **false** and does not cover those entities. A blocked LARD call returns before any Braiins request body or socket. Observe-only deployment stays blocked while the hard HA bypasses below can still reach the miner.

Machine-readable copy: [phase1-writer-inventory.json](phase1-writer-inventory.json). The test `test_writer_inventory_matches_code_and_gates_network_paths` requires the JSON `writers` array to equal `WRITER_INVENTORY` in `app/controller.py`.

## WRITE_BLOCKED

When `enable_writes` is false, every gated path returns this record (and logs `result=WRITE_BLOCKED`) before the HTTP client builds a request:

```json
{
  "result": "WRITE_BLOCKED",
  "requested_operation": "pause|resume|set_power|patch_boards|cooling_put|apply_mode|...",
  "source": "braiins|controller",
  "controller_state": "DISARMED",
  "enable_writes": false,
  "reason": "enable_writes_false",
  "network_write_sent": false,
  "denied": true,
  "write_blocked": true,
  "op": "<same as requested_operation>"
}
```

`network_write_sent` is always false on this record. HTTP 200 from a later armed call is still only an acknowledgement.

Start, restart, reboot, and factory reset do not return this record. They raise `RuntimeError` inside `_call` before authentication and before any socket. There is no maintenance hashboard tool. `MAINTENANCE_LOCKOUT` is a reserved controller state and no path assigns it. A raw board PATCH is `Braiins.patch_boards`, which is the same gate.

## In-repo paths

| Path | Location | Direct/indirect | User reachable | Gate before network write | Disposition | Status |
| --- | --- | --- | --- | --- | --- | --- |
| `Braiins.pause` | app/controller.py Braiins.pause | direct | only after enable_writes and the HA master gate, via apply_mode | _blocked_write then Controller._authorize_device_write | gated | active |
| `Braiins.resume` | app/controller.py Braiins.resume | direct | only after enable_writes and the HA master gate, via apply_mode | _blocked_write then Controller._authorize_device_write | gated | active |
| `Braiins.set_power` | app/controller.py Braiins.set_power | direct | only after enable_writes and the HA master gate, via apply_mode | _blocked_write before the watt body is built | gated | active |
| `Braiins.patch_boards` | app/controller.py Braiins.patch_boards | direct | only after enable_writes and the HA master gate, via apply_mode | _blocked_write before the hashboard body is built | gated | active |
| `Braiins.set_cooling_auto` | app/controller.py Braiins.set_cooling_auto | direct | cooling_control_enabled and enable_writes, via a cooling transaction | cooling_control_enabled check, then _blocked_write, before the auto body | gated | active |
| `Braiins.start` | app/controller.py Braiins.start | direct | no | BRAIINS_DENY_PATHS inside _call before ensure_auth and before any socket | structurally_denied | retired |
| `Braiins.restart` | app/controller.py Braiins.restart | direct | no | BRAIINS_DENY_PATHS inside _call before ensure_auth and before any socket | structurally_denied | retired |
| `Braiins._call reboot/factory-reset` | app/controller.py BRAIINS_DENY_PATHS | direct | no | BRAIINS_DENY_PATHS before ensure_auth and before any socket | structurally_denied | retired |
| `Controller._cooling_escalate_start` | app/controller.py | indirect | no | helper returns false and does not call the client | retired | retired |
| `Controller._cooling_escalate_restart` | app/controller.py | indirect | no | helper returns false and does not call the client | retired | retired |
| `Controller.apply_mode` | app/controller.py Controller.tick / apply_mode | indirect | tick when enable_writes and the HA master gate are both on | tick returns before apply_mode when writes are disallowed; each command re-checks _authorize_device_write | gated | active |
| `Controller.request_cooling_ceiling` | app/controller.py | indirect | explicit call only; refused while cooling control is off | _cooling_control_enabled, _policy_denial, then _call_device | gated | active |
| `Controller.request_temperature_policy` | app/controller.py | indirect | explicit call only; refused while cooling control is off | _cooling_control_enabled, _policy_denial, then _call_device | gated | active |
| `health HTTP server` | app/controller.py start_health_server | indirect | read-only | no POST/PUT/PATCH handler | read_only | active |

## Hard HA bypasses (observe-only deploy blockers)

Read-only Home Assistant inventory, 2026-09-22 ~16:55 CT. This repository did not call HA services, did not edit HA config, and did not contact the miner. `enable_writes=false` on the LARD add-on does **not** cover these paths. They talk to miner `192.168.1.113` through the `braiins_os_plus` integration. The `lard_` entity-id prefix is historical naming, not a LARD gate.

Observe-only deployment stays blocked until these are disabled, hidden, or wrapped so the only miner commands go through LARD.

| Path | Where | Write | Gate before network | Disposition (not applied) | Live status |
| --- | --- | --- | --- | --- | --- |
| `button.lard_mining_pause` / `resume` | braiins_os_plus | direct miner | none | disable or hide | pressed ~15:48 and ~15:51 CT |
| `button.lard_bosminer_start` / `stop` / `restart` | braiins_os_plus | direct miner | none | disable or hide | start ~15:41 CT, restart ~15:31 CT; stop unknown |
| `button.lard_device_reboot` | braiins_os_plus | direct miner | none | disable or hide | pressed ~15:32 CT |
| `button.lard_tuner_increase_*` / `decrease_*` | braiins_os_plus | direct miner | none | disable or hide | mixed unknown/unavailable |
| `number.lard_power_target` / `power_adjustment_step` | braiins_os_plus | direct miner | none | disable or hide | 1850 W / step 250 |
| `number.lard_hashrate_target` / `hashrate_adjustment_step` | braiins_os_plus | direct miner | none | disable or hide | target unavailable / step 10 |
| `select.lard_performance_mode` | braiins_os_plus | direct miner | none | disable or hide | Power Target |
| `switch.lard_device_locate_led_blinking` | braiins_os_plus | direct miner | none | disable or hide | off |
| `script.solar_miner_pause` / `resume_min` / `set_target` / `evaluate` | scripts | call those entities | no `enable_writes` check | disable, or rewrite to a no-op | callable, state off |
| Lovelace `solar-miner` | dashboard | exposes `number.lard_power_target` | none | remove that card or make it read-only | card present |
| `braiins_os_plus` config entry | integration | owns the entities above | none from LARD | disable or hide its write entities | loaded, Antminer S19j Pro |
| HA REST/WS | API | can press the same entities | auth only | goes away when the entities are disabled | reachable; no service call from this repo |

The LARD add-on does not create these buttons, numbers, or the performance select. Its own pause, resume, and power-target client methods return `WRITE_BLOCKED` before HTTP when `enable_writes` is false. `HA.set_state` refuses `number.lard_power_target` before any Home Assistant POST. That refusal does not stop the integration entity.

## Latent automations (currently off)

These call the solar_miner scripts if someone turns them on. Keep them off. Off is not a substitute for disabling the buttons.

`automation.solar_miner_command_verify`, `fan_watchdog`, `night_budget`, `power_governor`, `soc_hard_pause`, `thermal_protect`.

## Already OK on that inventory

- `automation.solar_miner_upstairs_ac` is on and writes `climate.loft` plus logbook only.
- `switch.solar_miner_auto_enable` is off. Off does not block a manual button, script, or API call.
- Deployed add-on `762409a4_lard_controller` is 0.1.11 with `enable_writes` false, cooling control false, and auto fan ceiling false.
- No `rest_command` or `shell_command` service is registered.

## Recommended HA disposition (runbook only — do not apply)

Do not run these steps from this repository, a cloud agent, or an API token. They change live Home Assistant.

1. Disable or hide every `braiins_os_plus` write entity in the table above. Read-only miner sensors can stay if they do not command the miner.
2. Disable `script.solar_miner_pause`, `script.solar_miner_resume_min`, `script.solar_miner_set_target`, and `script.solar_miner_evaluate`, or rewrite them so they do not press or set `braiins_os_plus` entities.
3. Keep `switch.solar_miner_auto_enable` off.
4. Remove `number.lard_power_target` from the Lovelace `solar-miner` dashboard, or replace that card with a read-only sensor.
5. Leave the latent solar automations off.
6. Leave upstairs AC as HVAC plus logbook only.

`binary_sensor.lard_competing_writer`, when ON, only stops the LARD add-on from arming. It does not stop `braiins_os_plus`. A missing competing-writer sensor is not itself a writer.

