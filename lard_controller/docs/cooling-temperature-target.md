# Cooling temperature target (0.1.11)

**0.1.11:** `cooling_control_enabled` defaults **false**. This note's PUT contract is inert. Braiins OS owns cooling entirely. LARD does not schedule cooling transactions unless that option is explicitly true. Hashboard switching stays. The text below is the opt-in contract from 0.1.10 and does not arm writes, AUTO, or any live miner command.

Operator setpoints are Fahrenheit, the same unit as the chip temperature sensors. Braiins REST still requires `{"degree_c": number}`. LARD converts only when it builds the cooling-mode PUT.

## What the miner actually accepts

Firmware context for this design: `2026-09-11-0-7a758742-26.09-plus`, Braiins OS+ 26.09, REST API 1.8.0. Inventory (read-only) conclusion:

- The **only** cooling mutate path is `PUT /api/v1/cooling/mode` (`SetCoolingMode` / `CoolingAutoMode`).
- gRPC has the same shape: `SetCoolingMode`. There is no standalone fan-max RPC.
- `GET /api/v1/cooling/state` is telemetry (fan RPM and `target_speed_ratio` PWM, plus highest temperature). It has **no** ceiling and no setpoint.
- `GET /api/v1/cooling/mode` is **405** on this build.
- Configured mode and setpoints are read from `GET /api/v1/configuration/miner` → `temperature.mode`.
- `experimental.toml` fan range is unsupported on 26.09.
- The GUI almost certainly saves through the same cooling-mode PUT. That is schema-forced, not HAR-proven.

`CoolingAutoMode` fields used here (OpenAPI 1.8.0):

| Field | Shape | Meaning |
| --- | --- | --- |
| `target_temperature` | `{"degree_c": number}` | Temperature the miner tries to hold by regulating fans. Range 0–200 °C. |
| `hot_temperature` | same | Fans run at 100% at this threshold. |
| `dangerous_temperature` | same | BOSMiner shuts down to avoid overheating. |
| `min_fan_speed` / `max_fan_speed` | int 0–100, max > min | Allowed fan band. Not a live PWM slider. |
| `minimum_required_fans` | int | Fans required for bosminer to run. |

A partial auto PUT on this site has been observed to null fields that were omitted. A policy write therefore sends the temperatures it owns, plus the wide fan band, so a target update does not wipe the other setpoints.

## Who owns the loop

**Braiins Automatic cooling** is the high-frequency controller. Once mode is `auto` and `target_temperature` is set, the miner modulates PWM toward that chip temperature. Hot and dangerous are thresholds, not HA control ticks.

**Home Assistant / LARD** is a rare policy writer:

1. Observe chip temperature and fan RPM/PWM from `GET /cooling/state`.
2. Observe configured mode and setpoints from `GET /configuration/miner`.
3. When the operator changes the target (or hot, dangerous, or the wide envelope), and only then, run one gated cooling transaction.
4. Do not treat `ONE_BOARD` / `TWO_BOARD` / `THREE_BOARD` fan-max profiles as the control knob.
5. Do not ramp `input_number.lard_fan_max_pct` on a timer or on every mode change.

`cooling_policy` selects the owner:

| Value | Behavior |
| --- | --- |
| `native_auto_target` (default) | Desired body is Automatic mode with target/hot/dangerous and a wide fan envelope. Board count does not change it. `auto_fan_ceiling_enabled` is ignored. |
| `legacy_fan_ceiling` | Old per-board `max_fan_speed` profiles. Still requires `auto_fan_ceiling_enabled` (default **false**). |

`auto_fan_ceiling_enabled` is not the path to efficient mining. Leave it false.

## Sunrise → overnight

Example, after a future window where writes are intentionally armed. This repository change does not arm them.

1. Sunrise automation sets `input_number.lard_cooling_target_c` (for example 158 °F). The `_c` suffix is historical; the number is Fahrenheit. An optional `input_number.lard_cooling_target_f` wins when it has a numeric state. LARD sees one operator change.
2. LARD runs the existing pause → confirm (two idle polls) → `PUT /api/v1/cooling/mode` → settle → one ResumeMining → bounded recovery. 158 °F converts with `round((158 - 32) * 5 / 9)` = 70. The body is `{"auto":{"target_temperature":{"degree_c":70},"hot_temperature":{"degree_c":85},"dangerous_temperature":{"degree_c":95},"max_fan_speed":100,...}}` when hot and dangerous are 185 °F and 203 °F.
3. For the rest of the day Braiins fans toward 70 °C. HA only watches chip temp, RPM, and PWM. It does not PUT because the sun moved or a board-count mode changed.
4. Overnight the same helper moves to a different target. That is another single gated write, subject to `cooling_dwell_seconds` (default 600) so a flapping helper cannot thrash pause/resume.
5. Mining mode and reserve pauses stay on the board-priority state machine. They are not PWM tweaks and they do not carry a cooling PUT just because the board count changed.
6. A same-value target is a no-op: no pause, no PUT, no resume.

The first sample after start is a seed. It does not write. Changing the helper while `enable_writes` is false is absorbed. Turning writes on later does not replay that change. The operator changes the helper again, or calls the explicit policy request, after writes are armed.

## Write path (unchanged discipline)

Every cooling-mode PUT still goes through:

- `enable_writes` and `input_boolean.lard_board_priority_enable`, with `switch.solar_miner_auto_enable` off
- immediate re-authorization before the emit (0.1.8)
- pause-only gate when `cooling_writes_only_when_paused` is true (default)
- one transaction owner, coalesced pending, monotonic deadlines
- cancel or reload → `INTERRUPTED_MANUAL_REVIEW`, no orphan Resume
- at most one resume retry; Start, Restart, and reboot stay denied
- pending auto-apply only after `HASHING` (not after `DEGRADED` / `ERROR`)

Unordered setpoints refuse the PUT. The check is `target < hot < dangerous` in °F **before** conversion. After `c = round((f - 32) * 5 / 9)`, each integer must sit in OpenAPI 0–200 °C and stay strictly ordered (159 °F and 160 °F both round to 71 °C, so that pair is refused). `max_fan_speed <= min_fan_speed` also refuses the PUT.

Thermal abort (`CHIP_ABORT_F` 180) still opens the fan envelope to 100% through that same gated sequence. On the native path it keeps the temperature fields in the body so the abort does not clear the target.

## Defaults (observe-safe)

| Option | Default |
| --- | --- |
| `enable_writes` | false |
| `auto_fan_ceiling_enabled` | false |
| `cooling_writes_only_when_paused` | true |
| `cooling_policy` | `native_auto_target` |
| `cooling_target_temperature_c` | 70 °C internal |
| `cooling_hot_temperature_c` | 85 °C internal |
| `cooling_dangerous_temperature_c` | 95 °C internal |
| `cooling_envelope_min_fan_pct` | 0 (omitted) |
| `cooling_envelope_max_fan_pct` | 100 |

70 / 85 / 95 are the published Braiins Toolbox examples for `--target-temp`, `--hot-temp`, and `--dangerous-temp` (BOS ≥ 25.01, range 0–200). Academy also suggests about 10 °C between the three levels. They are **internal add-on option defaults**, not the operator unit, and not a claim that 26.09 on this S19j Pro ships those numbers. Model min/max/default belong to `GET /api/v1/configuration/constraints` and are observed, not overwritten by a guessed firmware constant.

The options stay Celsius on purpose. An installed add-on already has 70 / 85 / 95 stored. Reading those numbers as Fahrenheit would PUT about 21 °C. When no helper has a numeric state, LARD converts the option to °F only for the order check, then back to the same integer °C for the body.

Helpers (package `ha_packages/lard_cooling_target.yaml`) are Fahrenheit, mode slider, min 100, max 250, step 1. The package keeps the historical entity IDs so existing automations still resolve. The `_c` suffix is not the unit.

- `input_number.lard_cooling_target_c` — initial 158 °F (Toolbox 70 °C). State is °F.
- `input_number.lard_cooling_hot_c` — initial 185 °F (Toolbox 85 °C). State is °F.
- `input_number.lard_cooling_dangerous_c` — initial 203 °F (Toolbox 95 °C). State is °F.
- Optional aliases `input_number.lard_cooling_target_f` / `_hot_f` / `_dangerous_f` — not created by the package. If one of them has a numeric state, it wins over the matching `_c` id. Create them only when they should be the source of truth.
- `input_number.lard_cooling_envelope_min_pct` / `lard_cooling_envelope_max_pct` — optional band, initial 0 and 100

100–250 °F is the mining slider (about 38–121 °C). Values outside that slider still convert if they land in 32–392 °F (OpenAPI 0–200 °C) and keep strict order.

A site that was running helper values 70 / 79 / 95 °C should set the Fahrenheit helpers to **158 / 174 / 203** before any armed write. 174 °F converts back to 79 °C. Do not leave 70 in the helper: that state is now 70 °F.

`input_number.lard_fan_max_pct` and `ha_packages/lard_cooling_profiles.yaml` remain for the legacy policy only. They are envelope leftovers, not the native actuator.

## Out of scope

No live prove, no Home Assistant deploy, no Braiins PUT/POST/PATCH, no Resume, no Fan Max change on the device, no `enable_writes`, no `solar_miner_auto_enable`. The miner stays PAUSED / 0 W until a separate, explicit window.
