---
name: power
description: Switch, power-cycle, and measure lab power outlets across every configured power device — TP-Link Tapo P316M/P304M strips (6 metered outlets, over the LAN) and Zigbee plugs through Zigbee2MQTT (tested: ThirdReality Smart Plug Gen3). Confirmed per-outlet on/off, power-cycle with wait-for-boot, live power/voltage/current, CSV logging, and one namespace of outlet aliases (ArenaPS, BenchPlug) whatever device they're on. CLI `bin/power` and Python `from labpower import Power`, usable from other projects and MATLAB. Use when the user mentions power cycling, turning an outlet or plug on/off, the Tapo strip, P316M, the Zigbee plug, BenchPlug, ArenaPS, "how much power/current is X drawing", "log power", or pairing a Zigbee plug. Do NOT use for programmable bench supplies (voltage/current setpoints over SCPI) or Kasa HS/KP plugs.
argument-hint: [status | on | off | cycle | read | log | discover | add-tapo | add-zigbee | alias] [outlet]
---

# Lab Power Control

One CLI and one Python API over every power device this Mac can reach:

| Device type (`type`) | Hardware | Link | Outlets | Readings |
|---|---|---|---|---|
| `tapo-strip` | TP-Link Tapo P316M (P304M should work, untested) | Wi-Fi LAN, via [python-kasa](https://github.com/python-kasa/python-kasa) | 6, individually switched + metered | W, V, A per outlet; today/month Wh |
| `zigbee2mqtt` | Zigbee plugs paired with Zigbee2MQTT (tested: ThirdReality 3RSP02064Z Smart Plug Gen3) | Zigbee via the Sonoff ZBDongle-P on this Mac → Zigbee2MQTT → local MQTT broker | 1 | W, V, A, total kWh, power factor |

```bash
~/.claude/skills/power/bin/power status          # every device, reachable or not
~/.claude/skills/power/bin/power cycle ArenaPS --off-s 5 --wait-above-w 3
```

The wrapper runs `uv run --project <skill dir>`; dependencies install into the skill's `.venv/` on first use. Add `--json` to any command for machine-readable output.

## Naming outlets

Anywhere an outlet is expected, give one of:

- an **alias** from the config: `ArenaPS`
- **`Device:channel`**: `SmartPowerStrip:6` (strip outlets are numbered 1–6 on the strip)
- a **single-outlet device's name**: `BenchPlug`
- a bare number, only while exactly one strip is configured: `6`
- a Tapo outlet nickname: `"Tapo Smart_Plug_3"` (needs a round trip to the strip)

Matching is case-insensitive and exact. Aliases are global, so moving a load to another device only means re-pointing its alias.

**Fallback aliases.** An alias may list several targets, most preferred first: `power alias G6Arena BenchPlug SmartPowerStrip:1` stores `"G6Arena": ["BenchPlug:1", "SmartPowerStrip:1"]`, and every command uses the first target whose device answers. This is for loads that more than one device can reach — here BenchPlug is plugged into strip outlet 1, so both meter the G6 arena (the strip reads ~1 W more: BenchPlug's own draw) and switching either one cuts the arena. Switching through the fallback also powers down whatever sits between (here BenchPlug itself).

## Before switching anything (agent rule)

Cutting power is not undoable for whatever is plugged in. Before `off` or `cycle` on an outlet the user hasn't explicitly named in this conversation, run `power status` and confirm with the user which load is on it. Never switch every outlet as a "test". An outlet drawing power with no alias is unknown equipment — ask. Don't send commands to a Zigbee plug while its firmware update is running.

## Setup

The config is `~/.config/power/config.json` (override with `POWER_CONFIG`; a legacy `~/.config/tapo-power/config.json` is migrated automatically). Check `power status` first — devices are probably configured already.

```bash
power discover                               # Tapo strips on the LAN + devices paired with Zigbee2MQTT
power add-tapo SmartPowerStrip [IP]          # auto-picks the only P304M/P316M found
power add-zigbee BenchPlug [--friendly-name X]
power alias ArenaPS SmartPowerStrip:6
power unalias ArenaPS
power remove BenchPlug                       # also drops aliases that point at it
```

```json
{
  "devices": {
    "SmartPowerStrip": {"type": "tapo-strip", "host": "192.168.x.y", "mac": "58-D8-..."},
    "BenchPlug": {"type": "zigbee2mqtt", "friendly_name": "BenchPlug"}
  },
  "aliases": {"ArenaPS": "SmartPowerStrip:6"},
  "tapo_account": null,
  "mqtt": {"host": "127.0.0.1", "port": 1883, "base_topic": "zigbee2mqtt"}
}
```

### Tapo strips

- **Credentials.** A strip onboarded via Matter only (Apple Home, never the Tapo app — `discover` shows `no account, onboarded via matter`) accepts TP-Link's factory-default local credentials, which are sent automatically. A strip added to the Tapo app only accepts that account: the user runs `power login you@example.com` in their own terminal (it prompts; Claude can't type the password). The password goes to the macOS Keychain (service `tapo-power`). Credentials are tried stored-account first, then factory default.
- **Protocol.** TP-Link firmware moves strips from KLAP to TPAP on its own (this P316M went 1.0.5 → 1.4.1 through Matter and switched overnight). Each connect asks the strip which it speaks (unicast UDP discovery) and falls back to trying TPAP then KLAP when UDP is blocked. TPAP support is unreleased in python-kasa, so `pyproject.toml` pins a tested commit of its TPAP branch (PR #1592); switch back to a PyPI release once one ships TPAP.
- **Moved IPs.** `add-tapo` stores IP and MAC. After a DHCP change the first failed connect (~5 s timeout) re-finds the strip — first by mDNS (Matter strips advertise `<MAC>.local`, e.g. `58D812141B6F.local`, resolving in milliseconds), then by UDP broadcast — and saves the new IP. `power discover` does the same for every configured strip. A DHCP reservation on the router is still the robust fix.
- **Security.** A Matter-only strip's factory credentials are public, so anything on the LAN can switch it. Fine at home; on shared networks bind it to a TP-Link account or isolate it.

### Zigbee (Zigbee2MQTT) — one-time setup, already done on this Mac

The Zigbee side is two background services on this Mac: **Mosquitto** (MQTT broker, `127.0.0.1:1883` only) and **Zigbee2MQTT 2.14.1** (talks to the Sonoff ZBDongle-P — TI CC2652P, Z-Stack — on `/dev/cu.usbserial-110`). The skill only speaks MQTT to Zigbee2MQTT. The Mac must be awake with the dongle plugged in for Zigbee control; plugs keep their relay state meanwhile (`power_on_behavior: previous`).

| What | Where / how |
|---|---|
| Mosquitto | `brew services start\|stop mosquitto`; config `/opt/homebrew/etc/mosquitto/mosquitto.conf` (listener 127.0.0.1 only) |
| Zigbee2MQTT install | `~/.local/share/zigbee2mqtt` (git tag 2.14.1, built with `npx pnpm@10.18.3 install --frozen-lockfile && npx pnpm@10.18.3 run build`); runs on Homebrew Node |
| Zigbee2MQTT config | `~/.local/share/zigbee2mqtt/data/configuration.yaml` — serial port, `adapter: zstack`, channel 25, frontend on 127.0.0.1:8080. **Contains the network key: never commit it.** |
| Service | LaunchAgent `~/Library/LaunchAgents/io.zigbee2mqtt.plist` (KeepAlive). Restart: `launchctl kickstart -k gui/$(id -u)/io.zigbee2mqtt`. Stop: `launchctl bootout gui/$(id -u)/io.zigbee2mqtt`. Start: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.zigbee2mqtt.plist` |
| Logs | `~/Library/Logs/zigbee2mqtt.log`, `~/.local/share/zigbee2mqtt/data/log/` |
| Web UI | http://127.0.0.1:8080 (pairing, device settings, OTA updates, network map) |
| Back up the network | copy `~/.local/share/zigbee2mqtt/data/` (network key, `coordinator_backup.json`, `database.db`); losing it means re-pairing every device |

**Pairing a new plug** (needs the user at the plug):

```bash
mosquitto_pub -h 127.0.0.1 -t zigbee2mqtt/bridge/request/permit_join -m '{"time": 254}'
# user: hold the plug's button >10 s until its LED flashes red; it goes dark when joined
mosquitto_pub -h 127.0.0.1 -t zigbee2mqtt/bridge/request/device/rename -m '{"from": "0x...", "to": "BenchPlug"}'
mosquitto_pub -h 127.0.0.1 -t zigbee2mqtt/bridge/request/permit_join -m '{"time": 0}'
power add-zigbee BenchPlug
```

Watch joins with `mosquitto_sub -h 127.0.0.1 -t 'zigbee2mqtt/bridge/event' -v`. Firmware updates: web UI → OTA, or publish `{"id": "BenchPlug"}` to `zigbee2mqtt/bridge/request/device/ota_update/update` (~28 min over Zigbee; the Gen3 keeps its outlet powered throughout).

## CLI reference

```bash
power status [DEVICE ...]              # per device: state, W, mA, energy; unreachable devices marked OFFLINE
power on ArenaPS                       # switches, then confirms the relay state
power off BenchPlug
power cycle ArenaPS                    # off, 5 s, on
power cycle ArenaPS --off-s 10 --wait-above-w 3 --timeout-s 90
power read BenchPlug                   # "BenchPlug: off  0.000 W  121.3 V  0.000 A"
power log                              # every outlet, tidy CSV to stdout until Ctrl-C
power log ArenaPS BenchPlug --out run.csv --interval-s 1 --duration-s 3600
```

Exit codes: `0` ok · `1` device / network / auth / unknown-outlet error (message on stderr) · `2` bad arguments · `3` `--wait-above-w` not reached before timeout · `130` Ctrl-C. `status` exits 1 only if no device is reachable.

`log --out` appends (header only for a new file), flushes every sample, and shows a live readout on stderr in a terminal. Devices are sampled in parallel; a device that fails a sample is skipped for that sample with a warning, and the rest keep logging. SIGTERM stops it cleanly (for callers running it as a subprocess).

**Best-effort logging** — `power log G6Arena --out run.csv --best-effort` (Python: `log_csv(..., best_effort=True)` / `background_log(..., best_effort=True)`) is for logging alongside something more important. It never fails the caller: unresolvable or unreachable outlets are retried every `--retry-s` (30 s), fallback aliases fail over on the next sample and move back to the preferred target when it answers again, nothing reachable just leaves a header-only CSV, and the exit code is 0. Measured failover (stopping Zigbee2MQTT mid-log with `G6Arena` = BenchPlug → strip outlet 1): 99 rows in 99 s, largest gap 2.1 s, back on BenchPlug 3 s after Zigbee2MQTT restarted. The `device` column records which meter each row came from.

## Python API (other projects)

```bash
uv add --editable ~/Documents/GitHub/claude-skills/power      # uv projects
pip install -e ~/Documents/GitHub/claude-skills/power          # plain venvs
```

```python
from labpower import Power, WaitTimeout

with Power() as p:                                  # uses ~/.config/power/config.json
    p.power("ArenaPS")                              # 4.289 (W)
    p.read("BenchPlug")                             # ChannelStatus: on, power_w, voltage_v, current_a, ...
    p.off("BenchPlug"); p.on("BenchPlug")           # confirmed; returns (device, channel)

    try:
        w = p.cycle("ArenaPS", off_s=5, wait_above_w=3, timeout_s=60)   # blocks until it draws > 3 W
    except WaitTimeout:
        ...                                         # came back on but never drew power

    with p.background_log("run1_power.csv", interval_s=1.0, outlets=["ArenaPS", "BenchPlug"]):
        run_experiment()

    for dev in p.status():                          # DeviceStatus per device; .online, .error, .channels, .total_w
        ...
    rows = p.sample(["ArenaPS", "BenchPlug"])       # readings without energy; devices read in parallel
```

`Power` is synchronous and thread-safe (each Tapo strip runs its own event loop thread; Zigbee shares one MQTT session), so it works from scripts, Jupyter, and async code. Devices connect lazily on first use. Errors are `PowerError`; `WaitTimeout` subclasses it.

## Using it from other projects (G6 firmware, webDisplayTools, …)

- **Name outlets by role, not device.** Project code only says `G6Arena`; each machine's `~/.config/power/config.json` maps that to whatever is there (fallback list included). Nothing device-specific goes in the project.
- **Prefer the CLI as a subprocess** for projects with their own environments (pixi, MATLAB): no dependency to add, and a missing skill or missing device can't break the project. Start `~/.claude/skills/power/bin/power log G6Arena --out <run>-power.csv --best-effort` next to the run; send SIGTERM at the end. Check the path exists first and skip power logging if not — collaborators without the skill are unaffected.
- **For a Python dependency, pin a tag**, never `main`: `labpower @ git+https://github.com/mbreiser/claude-skills@power-v0.5.0#subdirectory=power`. Guard the import (`try: from labpower import Power` / `except ImportError: Power = None`) and use `best_effort=True`.

## MATLAB

```matlab
pw = '~/.claude/skills/power/bin/power';
[rc, out] = system([pw ' --json read BenchPlug']);  r = jsondecode(out);
[rc, out] = system([pw ' cycle ArenaPS --off-s 5 --wait-above-w 3']);  % rc==3 -> never drew power
```

## CSV format

Tidy, one row per outlet per sample:

```
timestamp,elapsed_s,device,channel,name,on,power_w,voltage_v,current_a
2026-09-28T17:30:01.204-04:00,0.000,SmartPowerStrip,6,ArenaPS,1,4.289,120.4,0.083
2026-09-28T17:30:01.204-04:00,0.000,BenchPlug,1,BenchPlug,0,0.0,121.2,0.0
```

`name` is the alias if set, else the native name. `elapsed_s` is monotonic from the start of that logging call.

## Measurement characteristics

**Tapo P316M** (fw 1.4.1):

| Property | Value |
|---|---|
| Readings | per outlet via `get_emeter_data`: real power (mW), RMS voltage (mV), RMS current (mA) |
| Meter refresh | a new reading every ~1.1 s (median 1.09 s). Polling faster only repeats values; at exactly 1 Hz ~8% of samples repeat. Each reading averages ~1 s, so inrush peaks are smoothed away. |
| Meter lag after switching | ~1.5–3 s, ramping rather than stepping (measured on KLAP firmware; not re-measured) |
| Connect | ~3 s over TPAP; reuse one `Power` for loops and logging |
| Switch latency | ~0.1–0.2 s including the confirming read-back |
| Read throughput | one outlet ~23 ms; all 6 outlets ~250 ms |
| History on the strip | 5-minute average power per outlet since power-up (not exposed by the CLI) |
| Power factor | small switching supplies show PF ≈ 0.5 (ArenaPS idles at ~0.45): W is real power, V × A is apparent |

**ThirdReality Smart Plug Gen3** (fw 1.00.63, one Zigbee hop from the dongle):

| Property | Value |
|---|---|
| Readings | real power (W), RMS voltage (V, 0.1 V steps), RMS current (A), total energy (kWh), power factor, AC frequency |
| Meter refresh | a new reading about every 2 s (median 1.98 s); 1 Hz logging repeats values about half the time |
| Read latency | ~280 ms per W/V/A read (Zigbee2MQTT reads each attribute separately and publishes after each; the backend waits for the burst to finish) |
| Switch latency | ~300 ms including the confirming state message |
| First use | ~0.1 s to open the MQTT session (no device handshake) |
| Firmware updates | over the air via Zigbee2MQTT, ~28 min; the outlet stays powered |

Practical logging rates: 1 Hz for Tapo outlets, 2 s for Zigbee plugs (their meter refresh), ~7 MB/day per outlet as CSV at 1 Hz; 5–10 s for long runs. Sampling both kinds together takes ~0.3 s once sessions are warm (devices are read in parallel).

## Gotchas

| Symptom | Cause | Fix |
|---|---|---|
| `Cannot reach strip ...` | strip offline, other network, or IP moved and mDNS + broadcast both failed | `power discover`; else `power add-tapo NAME NEW_IP` |
| `refused the login` | strip bound to a Tapo account, or stored password stale | `power login <email>` (user runs it) |
| Tapo worked, then stopped after a quiet firmware update | KLAP → TPAP switch | handled automatically; if python-kasa is ever downgraded to a release without TPAP, logins fail with `403 to handshake1` — keep the pinned commit |
| `Can't reach the MQTT broker` | Mosquitto not running | `brew services start mosquitto` |
| `Zigbee2MQTT is not running` / bridge timeout | service stopped, crashed, or dongle unplugged | `tail ~/Library/Logs/zigbee2mqtt.log`; `launchctl kickstart -k gui/$(id -u)/io.zigbee2mqtt` |
| Zigbee2MQTT log: `Cannot lock port` | another process (a manual `node index.js`) holds the dongle | stop it; the LaunchAgent retries every 10 s |
| Zigbee2MQTT fails after moving the dongle to another USB port | `/dev/cu.usbserial-*` name changed | `ls /dev/cu.usbserial-*`, update `serial.port`, restart the service |
| `Timed out ... waiting for BenchPlug to answer` | plug unplugged, out of range, or mid firmware update | check the plug; `power discover` lists it if paired |
| `metering-only mode, which locks its relay on` | plug's `metering_only_mode` is ON (a safety setting) | turn it off in the Zigbee2MQTT web UI if switching is intended |
| Discovery finds nothing but the strip works in Apple Home | Mac on another subnet / VPN, or macOS Local Network permission denied | System Settings → Privacy & Security → Local Network; disconnect VPN |
| Keychain "allow access" dialog | new Python interpreter reading the stored TP-Link password | Always Allow |

## Development

```bash
cd ~/Documents/GitHub/claude-skills/power
uv run pytest tests -q          # fakes only: no network, MQTT, Keychain, or real config
```

- `labpower/core.py` — `Power`: naming, cycle / wait / logging, parallel sampling. Knows nothing about device protocols.
- `labpower/model.py` — `ChannelStatus`, `DeviceStatus`, and the `Backend` protocol (`channels`, `sample`, `status`, `set`, `close`). A new device type implements it and gets a branch in `Power._make`.
- `labpower/tapo.py` — `TapoStrip`; the only python-kasa code (`_open`, `_discover`, `_Connection`, `_Plug`, mDNS lookup).
- `labpower/zigbee.py` — `Z2MBridge` (one MQTT session) and `Z2MPlug`.

Tests monkeypatch `tapo._open` / `tapo._discover`, `Power._make`, and `zigbee._make_client`; an autouse fixture blocks real network and MQTT calls and redirects the config path.
