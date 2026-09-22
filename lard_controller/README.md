# LARD Controller

Home Assistant Supervisor **local add-on** that packages and supervises the LARD board-priority Braiins actuator.

Supervisor owns start / stop / restart. This is not an Advanced SSH `nohup` job and there is no `pgrep` keep-alive.

**Writes default off.** First boot is observe-only.

| | |
| --- | --- |
| Slug | `lard_controller` |
| Version | `0.1.11` |
| Miner | Braiins OS+ REST @ `http://192.168.1.113` (API ~1.8.0) |
| Network | `host_network: true` |
| Health | `http://<ha-host>:8099/health` (Supervisor watchdog) |
| Supervision | s6-overlay longrun, `exec` python in the foreground; crash → container exit → Supervisor restart |

## Install as a local add-on

1. Copy this folder to the HA host:

   ```bash
   # from a machine that can write the HA add-ons share
   cp -a lard_controller /addons/lard_controller
   ```

   On HAOS the local add-on path is `/addons` (the `addons` share). The folder name should be `lard_controller` and must contain `config.yaml` + `Dockerfile`.

2. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Check for updates**.
3. Open **Local add-ons → LARD Controller**.
4. **Install**. Do not start yet.
5. Set options (password via the UI secret field **or** `/data/secrets.json` — see below). Leave **`enable_writes: false`**.
6. Start the add-on. Confirm `/health` is 200 and HA entities update.
7. Run the recovery checklist in [DOCS.md](DOCS.md).
8. Only then set `enable_writes: true` **and** turn on `input_boolean.lard_board_priority_enable`.

## Secrets (never hardcode)

Resolution order for the Braiins password:

1. Add-on option `braiins_password` (schema type `password`)
2. `/data/secrets.json` key `braiins_password`
3. Environment `BRAIINS_PASSWORD` / `LARD_BRAIINS_PASSWORD`

Example `/data/secrets.json` (add-on persistent volume; create via the add-on file browser or a one-shot copy):

```json
{
  "braiins_password": "your-miner-root-password"
}
```

Home Assistant API:

- **As an add-on:** `SUPERVISOR_TOKEN` + `http://supervisor/core` (`homeassistant_api: true`).
- **Local / dev:** option `ha_token` (long-lived token) + optional `ha_base_url`.

## Run locally (dev)

```bash
export LARD_DATA_DIR=/tmp/lard-data
export LARD_SHARE_DIR=/tmp/lard-share
export LARD_ENABLE_WRITES=false
export LARD_HEALTH_PORT=8099
export LARD_HA_BASE_URL=http://127.0.0.1:18123
export LARD_HA_TOKEN=dev
python3 -u lard_controller/app/controller.py
```

Then `curl -i http://127.0.0.1:8099/health`.

## Single-writer cutover

Before `enable_writes: true`, turn **off**:

- `switch.solar_miner_auto_enable` (must stay off — this add-on will never turn it on)
- `automation.solar_miner_power_governor`
- `automation.solar_miner_soc_hard_pause`
- `automation.solar_miner_command_verify`
- `automation.solar_miner_night_budget`
- `automation.solar_miner_thermal_protect` (Braiins pause/derate path)
- `automation.solar_miner_fan_watchdog` (Braiins pause path)

HVAC-only automations (`solar_miner_lower_ac`, `solar_miner_upstairs_ac`, presence comfort) are not Braiins writers.

Drop [`../package/lard_controller_watchdog.yaml`](../package/lard_controller_watchdog.yaml) into `config/packages/` (watchdog) and [`ha_packages/lard_cooling_target.yaml`](ha_packages/lard_cooling_target.yaml) (target / hot / dangerous in °F). [`ha_packages/lard_fan_max.yaml`](ha_packages/lard_fan_max.yaml) and [`ha_packages/lard_cooling_profiles.yaml`](ha_packages/lard_cooling_profiles.yaml) are legacy fan-ceiling helpers only.

## Cooling (0.1.11 — Braiins owns cooling)

Design note for the inert opt-in path: [docs/cooling-temperature-target.md](docs/cooling-temperature-target.md).

**Braiins OS owns cooling.** `cooling_control_enabled` defaults **false**. LARD does not `PUT /api/v1/cooling/mode` (no temperature target, no fan ceiling, no thermal-abort fan write), does not pause for cooling, and ignores target / hot / dangerous / fan-max helper changes. Hashboard pause, resume, power target, and board priority still run when writes are armed. Chip temperature and fan RPM/PWM stay observe-only. HA helper entities are left in place; removing them is a separate Home Assistant cleanup.

`native_auto_target` and `legacy_fan_ceiling` schedule nothing unless `cooling_control_enabled` is explicitly true. `enable_writes` still defaults **false**. Arming writes does not push a cooling setpoint.

The only cooling write on this Braiins OS+ build, and only on that explicit opt-in, is `PUT /api/v1/cooling/mode`. `native_auto_target` would put **target chip °C** on `CoolingAutoMode.target_temperature`. The operator sets that target in **°F**. LARD converts with `round((f - 32) * 5 / 9)` only when it builds the PUT body. Braiins modulates PWM. Board-count fan-max profiles are not the control knob. `auto_fan_ceiling_enabled` stays **false**.

A live cooling PUT while hashing still stalls this site's miner, so any setpoint write remains a pause-first maintenance transition. After two confirming pause polls, the PUT, and the settle window, ResumeMining runs **once**, then bounded recovery. Start, BOSminer Restart, and device reboot are not used. `enable_writes` still defaults **false**. Arming it does not itself push a target.

Add-on option defaults stay internal Celsius (Toolbox examples, OpenAPI range 0–200 °C, not a claimed firmware default): target **70**, hot **85**, dangerous **95**. They apply only when no helper state exists. Operator helpers are Fahrenheit: package initials **158 / 185 / 203** (those same Toolbox points). A site that was on 70 / 79 / 95 °C should set the helpers to **158 / 174 / 203**. Wide fan envelope **0–100**.

Legacy `cooling_policy: legacy_fan_ceiling` keeps one placeholder envelope per board-count state (ONE 70 / TWO 85 / THREE 100 / PAUSED 100, TBD/measured). Those PUTs still require `auto_fan_ceiling_enabled`. `input_number.lard_fan_max_pct` only caps that legacy profile. It is not a live fan slider.

| | |
| --- | --- |
| Dwell | `cooling_dwell_seconds` (default 600) — blocks short solar/SOC/slider flaps |
| Stabilize | `cooling_stabilize_seconds` (default 5) after a confirmed PUT |
| Post-write settle | `cooling_settle_seconds` (default 45). Poll `transition_poll_interval_seconds` (default 10). Do not resume just because the PUT returned. `cooling_resume_settle_seconds` is used only when the new settle is 0 |
| Expected recovery | `expected_recovery_seconds` (default 240). 0 W is fine while lifecycle is positive |
| Maximum recovery | `maximum_recovery_seconds` (default 600). Then at most one resume retry (`max_resume_retries_per_transaction` 1) and `post_retry_recovery_seconds` (default 180) |
| Stable hash | `stable_hash_poll_count` (default 3) before health is `HASHING` |
| Telemetry | `telemetry_failures_before_error` (default 3). First misses are UNKNOWN/STALE, not ERROR |
| Cooling control | `cooling_control_enabled` (default **false**). Braiins owns cooling. Leave off |
| Cooling policy | `cooling_policy` (default `native_auto_target`). Inert unless cooling control is explicitly enabled |
| Target / hot / dangerous | Helpers °F on `lard_cooling_target_c` / `_hot_c` / `_dangerous_c` (awkward historical IDs). Optional `_f` IDs win when present. Options stay 70 / 85 / 95 °C internal. PUT body is `degree_c`. |
| Fan envelope | min 0, max 100 (safety band, not PWM) |
| Auto fan ceiling | `auto_fan_ceiling_enabled` (default **false**). Legacy only. Leave off |
| Cooling only while paused | `cooling_writes_only_when_paused` (default true) |
| Thermal abort | `CHIP_ABORT_F=180` is observed only. It does not PUT cooling or pause for fans while `cooling_control_enabled` is false |

The add-on does **not** create real HA helpers (REST cannot). On startup it POSTs state stubs for the compatibility `_c` target helpers (values in °F, not a Celsius number) and `lard_fan_max_pct` if missing and, if `config/packages/` already exists, copies the YAML once. It does not stub the preferred `_f` entities, so a phantom `_f` state cannot hide a real `_c` helper. Prefer installing the packages and restarting Core:

```yaml
# configuration.yaml
homeassistant:
  packages: !include_dir_named packages
```

```bash
cp lard_controller/ha_packages/lard_cooling_target.yaml /config/packages/lard_cooling_target.yaml
# Legacy fan-ceiling helpers only (not used by native_auto_target):
cp lard_controller/ha_packages/lard_fan_max.yaml /config/packages/lard_fan_max.yaml
cp lard_controller/ha_packages/lard_cooling_profiles.yaml /config/packages/lard_cooling_profiles.yaml
```

Keep `automation.solar_miner_fan_watchdog` off. Do not add a separate HA automation that writes Braiins fans.

## What this add-on will not do

- Reboot the miner
- Write SRNE / BMS / grid entities
- Turn on `switch.solar_miner_auto_enable`
- Invent a new energy policy (board-priority thresholds stay as in the uploaded actuator)
