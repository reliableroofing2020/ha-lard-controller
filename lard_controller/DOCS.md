# LARD Controller architecture

Supervised local add-on. Supervisor starts and restarts the container. The process inside is the uploaded board-priority Braiins actuator, adapted for add-on options, dual write gates, HA heartbeat entities, and an HTTP health endpoint.

## Process model

```
Supervisor
  └── Docker container (host network)
        └── s6-overlay /init   (PID 1)
              └── lard-controller longrun
                    └── exec python -u /app/controller.py
                          ├── main loop (foreground)
                          └── health thread  0.0.0.0:8099
```

- `init: false` so Docker does not inject its own init in front of s6.
- The run script **`exec`s** Python. No `nohup`, no `&`, no `pgrep` keep-alive, no detached shell.
- If the controller exits, `finish` calls s6 `halt`. The **container exits non-zero**. Supervisor recreates it (`boot: auto`) and/or the **watchdog** (`http://[HOST]:8099/health`) restarts it when `/health` is not 200.
- A hung loop (no tick for `2 × poll_seconds`) makes `/health` return **503** so Supervisor restarts a wedged process.

This replaces Advanced SSH + `nohup` + `pgrep` supervision.

## Networking

`host_network: true` so the add-on shares the HA host LAN stack and can reach `http://192.168.1.113` without a Docker NAT hairpin.

HA Core is reached at `http://supervisor/core` with `Authorization: Bearer $SUPERVISOR_TOKEN` (`homeassistant_api: true`). Optional `ha_base_url` + `ha_token` are for running the same script outside Supervisor.

MQTT is `want`, not `need`. If Mosquitto is installed, the add-on asks Supervisor `GET /services/mqtt` and publishes discovery + LWT. If MQTT is absent, heartbeat entities are written with HA REST `POST /api/states/...`.

Diagnostic JSON is **not** the health path:

| Path | Role |
| --- | --- |
| `/data/status.json` | Persistent add-on volume |
| `/share/lard_controller_status.json` | Optional host-visible copy |
| HA entities listed below | **Operator health** — use these, not `/local/*.json` |

## Write gates (both required)

Braiins pause / resume / hashboard PATCH / power-target are issued only when:

1. Add-on option `enable_writes` is **true**, and
2. `input_boolean.lard_board_priority_enable` is **on**

Cooling / fan-profile writes (`PUT /api/v1/cooling/mode` with tagged `{"auto":{"max_fan_speed": N, ...}}`) use the **same dual write gates** as pause/resume/boards. They are never issued live while hashing. A live cooling PUT on this site's BOS+ (~26.09 / Antminer) stalls mining (PAUSED/0W then APPLYING/0W / `read_boards_http_500`).

Default `enable_writes` is **false**. First boot cannot write the miner.

Additional refuse:

- `switch.solar_miner_auto_enable` is **on** → log + `refusing_writes_old_auto_enable_is_on`. This process never turns that switch on.
- Braiins paths containing reboot / restart / factory / reset are denied in the client.

Never written: SRNE, BMS, grid, `number.lard*`, `script.solar_miner*`.

## Mode resolution

| `input_select.lard_miner_mode_request` | Desired mode |
| --- | --- |
| `PAUSED` / `ONE_BOARD` / `TWO_BOARD` / `THREE_BOARD` | Honored as a manual override |
| `AUTO` | `sensor.lard_miner_mode_desired_ha` when it holds a valid mode |
| `AUTO` and that sensor unknown/unavailable | Uploaded local policy (same SOC / solar / hold / night-cap numbers) |

Hard safety still applied on AUTO: SOC ≤ 30 → `PAUSED`; lost `binary_sensor.lard_api_heartbeat`, critical stale, or a blocking fault → `PAUSED`.

Board-priority / anti-flap / async PATCH semantics are unchanged:

- Prefer board 1. Never enable 2 or 3 without 1.
- HTTP 200 on hashboard PATCH = **accepted, not applied**.
- Poll `GET /api/v1/miner/hw/hashboards` every **5 s**.
- Wait **≥ 60 s** (`board_wait_seconds`, minimum 60) before declaring a board-topology failure.
- If the requested topology **already matches**, skip PATCH and board-wait entirely. `PAUSED` and `ONE_BOARD` both use `["1"]` — that transition goes straight to staged resume.
- Board ids are normalized (`1` vs `"1"`). An empty/partial poll is retried (2s / 5s / 10s / 20s), not treated as a hard miss.
- Resume confirmation waits **120 s** (`RESUME_WAIT_S`) and does not require mature hashrate.
- Transient Braiins HTTP 5xx retry with 2s / 5s / 10s / 20s backoff. A single 500 is not `ERROR`.
- Anti-flap: 10 min up, 5 min down; 15 min settle after a board change. Holds use the last **confirmed** operational mode, never `APPLYING` / `ERROR`.

Power target stays **944 W** until someone measures a higher floor.

## Cooling owner (maintenance-gated — never live while hashing)

The add-on is the **only** Braiins cooling writer. Not Adv SSH. Not a separate HA fan automation. Keep `automation.solar_miner_fan_watchdog` **off**.

Hypothesis verified in code: a live `PUT /api/v1/cooling/mode` while hashing is unsafe on this BOS+ build. Cooling changes are a pause-first maintenance transition.

| | |
| --- | --- |
| Write | `PUT /api/v1/cooling/mode` body `{"auto":{"max_fan_speed": N, ...}}` (`N` integer u32 percent, **not** a 0.6 float) |
| Restore 100 | `{"auto":{"max_fan_speed": 100, "minimum_required_fans": 2}}` |
| Read | `GET /api/v1/cooling/state` (fans rpm / `target_speed_ratio` / optional max). `GET /api/v1/cooling/mode` is **405** |
| Auth | `Authorization: <raw token>` (no Bearer) |
| Envelope cap | `input_number.lard_fan_max_pct` — caps desired max; **never** a live mid-hash PUT |
| Profiles | One envelope per `ONE_BOARD` / `TWO_BOARD` / `THREE_BOARD` / `PAUSED` (add-on options or optional HA helpers) |

Profile placeholders (TBD/measured — do not treat as final site values):

| State | Default max % | Intent |
| --- | --- | --- |
| `ONE_BOARD` | 70 | Lower envelope |
| `TWO_BOARD` | 85 | Medium envelope |
| `THREE_BOARD` | 100 | Full / normal |
| `PAUSED` | 100 | Unconstrained / known-good restore |

Optional mins default to 0 (omitted from the PUT). Override via add-on options `cooling_*_max_fan_pct` / `cooling_*_min_fan_pct`, or helpers in `ha_packages/lard_cooling_profiles.yaml`. Effective max is `min(mode_profile, lard_fan_max_pct)`.

When **desired profile ≠ applied profile** (and dwell has elapsed, unless thermal abort):

1. Enter `APPLYING` (intentional — not `ERROR`).
2. Hold the Braiins write mutex for the whole transition.
3. Pause mining (`user_pause` / Braiins pause).
4. Verify `user_pause=true` **and** actual power ≈ 0 W.
5. `PUT /api/v1/cooling/mode` with the tagged auto body.
6. Read cooling state back; confirm requested values stuck (or PUT 200 + GET 200 when the state payload has no `max_fan_speed`).
7. Short stabilize (`cooling_stabilize_seconds`, default 5).
8. Poll miner readiness (`GET /api/v1/miner/details`: pause flag, `bosminer_uptime_s`, status / `detailed_status` reason, watts). `bosminer_uptime_s == 0` means the bosminer process is not running — that is unreadiness, not a confirmed hard fail.
9. Wait `cooling_resume_settle_seconds` (default 20). Do **not** assume the first `ResumeMining` is accepted.
10. Resume mining with bounded backoff (5s / 10s / 20s). Stay `APPLYING`. A 500 is retried.
11. If ResumeMining still 500: escalate `PUT /api/v1/actions/start`, then BOSminer `PUT /api/v1/actions/restart`. Device reboot (`/actions/reboot`) is never used.
12. Verify `user_pause=false`, mining running, expected hashboards, watts > 0, TH/s recovering, cooling still the requested profile.
13. Only then publish the requested operating mode as actual.

Rules:

- The intentional paused period is **not** `ERROR`. Stay in `APPLYING` for the whole cooling transition **and** the post-PUT resume recovery window.
- Desired == applied → skip pause and cooling PUT (idempotent).
- `cooling_dwell_seconds` (default 600) blocks rapid cooling-only re-transitions so short solar/SOC/slider flaps do not thrash profiles. A committed board-count `apply_mode` still applies that mode's profile while paused.
- Cooling PUT failure: restore the last known-good profile if possible → pause the miner safely → `ERROR`. Resume HTTP 500 after a cooling PUT is **not** an immediate fail — `ERROR` only after settle + bounded resume + Start + BOSminer Restart are exhausted. Do **not** hand off to legacy writers.
- `CHIP_ABORT_F=180`: unconstrained 100 still applies, through the same gated sequence (dwell bypassed). Existing SOC / heartbeat / stale / fault **mining-pause** policy is unchanged.
- Startup / reconnect no longer force a cooling PUT.

The helper `input_number.lard_fan_max_pct` must exist if you want an extra envelope cap. REST cannot create a real `input_number`; on startup the add-on POSTs a state stub if the entity is missing and copies the packages into `/config/packages/` only when that directory already exists.

## Mode reconciliation contract

Desired mode and confirmed operational/actual mode are separate. Published `actual_mode` is one of `PAUSED`, `APPLYING`, `ONE_BOARD`, `TWO_BOARD`, `THREE_BOARD`, `ERROR`.

Hashboard topology alone never confirms a non-`PAUSED` mode. Board set `{1}` while Braiins is still `user_pause` / `MINER_STATUS_PAUSED` is **`PAUSED`**, not `ONE_BOARD`. That is what used to skip resume after an add-on restart (`desired == actual`).

Each tick:

1. Read desired mode (manual / HA / local policy).
2. Read pause/mining state from `GET /api/v1/miner/details` (`status` + `detailed_status`) — already used for live watts; no new Braiins endpoints.
3. Read actual board topology from `GET /api/v1/miner/hw/hashboards`.
4. If desired is `PAUSED`: pause if needed, then mark actual `PAUSED`.
5. If desired is a board mode:
   - ensure required boards (poll PATCH until confirmed; skip writes when already correct)
   - resume only if the miner is paused / not running
   - **Stage A:** resume HTTP accepted and miner no longer reports `user_pause` / paused (wait up to 120s)
   - **Stage B:** requested board set matches (HTTP 200 = accepted; poll topology)
   - **Stage C:** miner enters running / preheat / ramping / starting **and** watts or hashrate begin rising (delta/trend — not a mature TH target)
   - after Stage C, resume is operationally successful; remain `APPLYING` until final confirm
   - while converging, publish `APPLYING`

`ONE_BOARD` / `TWO_BOARD` / `THREE_BOARD` confirm only when all of these are true:

1. Expected hashboards enabled/disabled
2. Miner is not user-paused
3. Mining state is active/running
4. Braiins reports resumed/operational state (`status` normal/running or `detailed_status.running`)
5. After the startup grace window, power or hashrate may be used as extra sanity. **Power=0 alone is not a failure** during immediate resume warmup. **Do not require stable/full hashrate** inside the 120s resume window — low rising watts are enough for Stage C.

A transient Braiins HTTP 500 during board or ramp transitions is retried (2s, 5s, 10s, 20s). Only a sustained 5xx window becomes `ERROR`. Successful authenticated reads reset `api_fail_count`.

Idempotent restart examples (same patterns for one/two/three boards):

| Desired | Miner | Boards | Writes |
| --- | --- | --- | --- |
| `TWO_BOARD` | paused | 1+2 | resume only; converge to `TWO_BOARD` |
| `TWO_BOARD` | running | 1 only | enable board 2; no pause/resume |
| `TWO_BOARD` | running | 1+2 | none |

Dual write gates (`enable_writes` + HA master boolean) and the old-auto refuse path are unchanged.

## Heartbeat entities (every loop)

| Entity | Meaning |
| --- | --- |
| `binary_sensor.lard_controller_online` | `on` while the loop is publishing |
| `sensor.lard_controller_last_seen` | UTC ISO-8601 of last publish |
| `sensor.lard_controller_requested_mode` | Mode the actuator wants |
| `sensor.lard_controller_actual_mode` | Mode inferred / last applied |
| `sensor.lard_controller_error` | Last error or `ok` |
| `sensor.lard_controller_api_fail_count` | HA + Braiins transport failures |
| `sensor.lard_controller_last_braiins_ok` | UTC ISO of last Braiins HTTP 200 |
| `sensor.lard_controller_power_w` | Approx watts from miner stats |
| `sensor.lard_controller_boards` | e.g. `1,2` or `none` |

MQTT discovery (when a broker is available) uses availability + last-will so a dead container goes unavailable. REST entities do **not** expire by themselves — the package template `binary_sensor.lard_controller_fresh` treats `last_seen` older than 120 s as stale.

## Single-writer migration

The live inventory of competing HA Braiins writers (disable these before arming writes):

| Entity | Why it fights this add-on |
| --- | --- |
| `automation.solar_miner_power_governor` | Pause / power-target / solar-follow |
| `automation.solar_miner_soc_hard_pause` | Braiins Mining Pause on SOC |
| `automation.solar_miner_command_verify` | Retry / pause if off target |
| `automation.solar_miner_night_budget` | Night pause |
| `automation.solar_miner_thermal_protect` | Derate / pause via Braiins |
| `automation.solar_miner_fan_watchdog` | Pause if fans are 0 |
| `switch.solar_miner_auto_enable` | Old auto master — **must stay off** |

Not Braiins writers (HVAC / presence): `solar_miner_lower_ac`, `solar_miner_upstairs_ac`, `solar_miner_presence_*`.

Cutover order:

1. Install add-on, `enable_writes: false`, start it.
2. Confirm heartbeat entities and `/health`.
3. Disable the writers in the table. Leave `switch.solar_miner_auto_enable` **off**.
4. Install `package/lard_controller_watchdog.yaml`.
5. Run the recovery tests below **with writes still false**.
6. Operator explicitly sets `enable_writes: true`.
7. Turn on `input_boolean.lard_board_priority_enable`.
8. Watch `sensor.lard_controller_actual_mode` and the miner; keep the old pause button as a human emergency.

## Recovery test checklist

Run these with **`enable_writes: false`** first. None of them should touch the miner.

| Test | What to do | Pass |
| --- | --- | --- |
| Restart add-on | Supervisor → LARD Controller → Restart | Container comes back, `/health` 200, `last_seen` advances |
| Restart Core | Developer Tools → Restart Home Assistant | Add-on stays up; after Core is back, entities update again |
| Restart OS | Settings → System → Restart HAOS (maintenance window) | Add-on auto-starts (`boot: auto`), heartbeat returns |
| Crash process | `kill -9` the python PID **inside** the add-on container (or `ha addons restart local_lard_controller`) | Container exits; Supervisor recreates it. Do **not** start a host `nohup` replacement |
| Braiins unreachable | Unplug miner ethernet or point `miner_url` at a closed port | Loop continues, `api_fail_count` rises, `/health` stays 200, **no reboot attempts** |
| Watchdog YAML | Turn enable on and stop the add-on for >2 min | Persistent notification; **no** Braiins API from HA |
| Dual gate | `enable_writes: true` but master boolean off | Logs `master_gate_off`; no PATCH/pause/resume/**cooling** writes |
| Cooling profile 100→60 while hashing | Set `input_number.lard_fan_max_pct` to 60 (writes armed) | Enters `APPLYING`, pauses, verifies ~0 W, PUTs tagged auto 60, confirms, resumes; then actual returns to the board mode. Never a live mid-hash PUT |
| Cooling idempotent | Desired profile already applied | No pause, no cooling PUT |
| Cooling dwell | Change the helper twice inside 600 s | Second transition is skipped until dwell elapses |
| Cooling PUT / resume fail | (fault injection) | Restore known-good, miner left paused, `ERROR`; old auto stays off |
| Cooling resume 500 then 200 | After a confirmed PUT, first `ResumeMining` 500s until settle | Stays `APPLYING`; succeeds after settle/backoff; not `ERROR` early |
| Cooling resume always 500 | Resume keeps 500 after settle + backoff | Start, then BOSminer Restart, then `ERROR`; no device reboot; no legacy handoff |
| Cooling restore 100 | Thermal abort or helper 100 after a lower profile | Gated pause → PUT 100 → resume (not a live PUT) |
| Old auto | Flip `switch.solar_miner_auto_enable` on (then off) | Writes refused; notification from the package; switch is not turned on by this add-on |

After the table is green, arm writes in a planned window.

## Secrets

Never commit miner or HA tokens.

| Source | When |
| --- | --- |
| Add-on option `braiins_password` | Preferred on HAOS |
| `/data/secrets.json` `{"braiins_password":"..."}` | Persistent add-on data |
| `BRAIINS_PASSWORD` env | Container / local override |
| `SUPERVISOR_TOKEN` | Injected; do not paste it anywhere |
| `ha_token` option | Local/dev only |

`/data` survives add-on rebuilds. `/share/lard_controller_status.json` is a convenience copy, not a secret store.

## Health HTTP

`GET /health` → **200** + JSON when the last loop is newer than `2 × poll_seconds`, else **503**.

`GET /status` and `GET /` expose the same diagnostic snapshot the add-on writes to `/data/status.json`. No credentials in that JSON.
