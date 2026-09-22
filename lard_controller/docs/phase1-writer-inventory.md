# Phase 1 writer inventory (0.1.12)

In-repo miner write paths only. This file does not claim Home Assistant automations are fenced. `enable_writes` defaults **false**. A blocked call returns before any Braiins request body or socket.

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

## HA paths (to be filled by live inventory)

This VM does not have a complete Home Assistant inventory. The rows below are the expected disposition only. They are not a claim that the live system is fenced.

| Path | Expected disposition | Status |
| --- | --- | --- |
| `automation.solar_miner_upstairs_ac` | logbook only; must not write input_select.lard_miner_mode_request | to_be_filled_by_live_inventory |
| `switch.solar_miner_auto_enable` | remain off; LARD never turns it on; ON refuses add-on writes | to_be_filled_by_live_inventory |
| `button.lard_mining_pause / button.lard_mining_resume` | not pressed by an agent loop; still callable from HA until live fence | to_be_filled_by_live_inventory |
| `script.solar_miner_pause / resume / set-target` | not scheduled; still a bypass until live fence | to_be_filled_by_live_inventory |
| `automation.solar_miner_power_governor and other pause/verify automations` | stay off | to_be_filled_by_live_inventory |
| `binary_sensor.lard_competing_writer` | ON denies arming; missing entity is not a writer | to_be_filled_by_live_inventory |
| `direct hashboard PATCH or restore scripts outside this add-on` | do not run; no in-repo maintenance bypass | to_be_filled_by_live_inventory |

While any of those writers can still reach the miner, leave `enable_writes` false. If `binary_sensor.lard_competing_writer` is ON, LARD also refuses to arm. A missing competing-writer sensor is not itself a writer.

