# LARD Controller

Home Assistant Supervisor **local add-on** that packages and supervises the LARD board-priority Braiins actuator.

Supervisor owns start / stop / restart. This is not an Advanced SSH `nohup` job and there is no `pgrep` keep-alive.

**Writes default off.** First boot is observe-only.

| | |
| --- | --- |
| Slug | `lard_controller` |
| Version | `0.1.1` |
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

Drop [`../package/lard_controller_watchdog.yaml`](../package/lard_controller_watchdog.yaml) into `config/packages/`.

## What this add-on will not do

- Reboot the miner
- Write SRNE / BMS / grid entities
- Turn on `switch.solar_miner_auto_enable`
- Invent a new energy policy (board-priority thresholds stay as in the uploaded actuator)
