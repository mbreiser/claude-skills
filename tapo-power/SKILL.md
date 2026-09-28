---
name: tapo-power
description: Control and monitor TP-Link Tapo P316M (and P304M) smart power strips over the local network — switch individual outlets on/off, power-cycle lab electronics (optionally waiting until the device draws power again), read per-outlet power, current and voltage (mW/mA/mV resolution) plus today/month energy, and log them to CSV during experiments. Provides a CLI (`bin/tapo-power`) and an importable Python module (`from tapo_power import Strip`) for other projects, scripts, and MATLAB. Use when the user mentions the Tapo strip, P316M, smart power strip, "power cycle", "turn off/on outlet N", "reboot the arena power supply", "how much power/current is X drawing", "log power consumption", or names an outlet alias like ArenaPS. Do NOT use for Kasa-brand plugs (HS/KP/EP series) or non-Tapo PDUs.
argument-hint: [status | on | off | cycle | power | log | discover | add | alias] [outlet]
---

# Tapo Power Strip Control

Local-network control of a Tapo P316M (6 individually switched, individually metered outlets) via [`python-kasa`](https://github.com/python-kasa/python-kasa) (the library behind Home Assistant's TP-Link integration). No cloud round-trip, and it doesn't touch the strip's Matter pairing — the strip stays in Apple Home.

| Capability | How |
|---|---|
| Per-outlet on / off | `tapo-power on ArenaPS`, `tapo-power off 3` |
| Power-cycle, optionally wait for boot | `tapo-power cycle ArenaPS --off-s 5 --wait-above-w 3` |
| Live readout (W, mA, mains V, today/month Wh) | `tapo-power status` |
| One outlet's watts (script-friendly) | `tapo-power power ArenaPS` → `2.705` |
| CSV logging of W / V / A | `tapo-power log --out run.csv --interval-s 1` |
| Python API for other projects | `from tapo_power import Strip` |
| Find strips on the LAN | `tapo-power discover` |

## Invocation

```bash
~/.claude/skills/tapo-power/bin/tapo-power status
```

The wrapper runs `uv run --project <skill dir>`, so dependencies install into the skill's own `.venv/` on first use. Add `--json` to any command for machine-readable output. `--strip NAME` picks a configured strip (default: the config's `default`); `--host IP` talks to an IP directly.

**Outlets** are named by position (`1`–`6`, printed on the strip), by a local alias from the config (`ArenaPS`), or by the outlet's Tapo nickname (`"Tapo Smart_Plug_3"`). Matching is case-insensitive and exact — no fuzzy matching, by design.

## Before switching anything (agent rule)

Cutting power is not undoable for whatever is plugged in. Before `off` or `cycle` on an outlet the user hasn't explicitly named in this conversation, run `tapo-power status` and confirm with the user which load is on it. Never switch every outlet as a "test". An outlet drawing power with no alias is unknown equipment — ask.

## Setup

The config lives at `~/.config/tapo-power/config.json` (override with `TAPO_POWER_CONFIG`). Check it first — if a strip is already configured, skip to usage.

```bash
tapo-power discover                    # no credentials needed; UDP broadcast
tapo-power add SmartPowerStrip         # auto-picks the only P304M/P316M found; or: add NAME IP
tapo-power alias ArenaPS 6             # name outlet 6
tapo-power unalias ArenaPS
```

`add` stores the IP and MAC. If the IP later changes (DHCP), the first failed connection (~5 s timeout) re-finds the strip by MAC and saves the new IP: first via mDNS — Matter strips continuously advertise `<MAC>.local` (e.g. `58D812141B6F.local`), which resolves in milliseconds — then via TP-Link's UDP broadcast, which strips sometimes stop answering. `tapo-power discover` does the same lookup for every configured strip and updates moved ones. A DHCP reservation on the router is still the robust fix.

Config format (hand-editable):

```json
{
  "default": "SmartPowerStrip",
  "account": null,
  "strips": {
    "SmartPowerStrip": {"host": "192.168.x.y", "mac": "58-D8-...", "outlets": {"ArenaPS": 6}}
  }
}
```

### Credentials

- **Matter-only strips** (set up via Apple Home, never added to the Tapo app — discovery shows `no account, onboarded via matter`) accept TP-Link's factory-default local credentials, which python-kasa uses automatically. Nothing to configure. This is the current setup.
- **Account-bound strips** (added to the Tapo app) only accept that TP-Link account. The user must run, in their own terminal (it prompts for a password — Claude can't type it):
  ```bash
  tapo-power login you@example.com
  ```
  The password goes to the macOS Keychain (service `tapo-power`); only the email is written to the config. The Tapo app's **Me → Third-Party Services → Third-Party Compatibility** must also be ON, or newer firmware refuses local control.

With an account stored, python-kasa tries it first and still falls back to the factory defaults, so a mix of owned and unowned strips works.

## CLI reference

```bash
tapo-power status                       # table: state, W, mA, today/month Wh, time since last switch; mains V in header
tapo-power --json status                # per-outlet power_w, voltage_v, current_a; plus total_w
tapo-power on ArenaPS                   # switches, then re-reads the relay to confirm
tapo-power off 3
tapo-power cycle ArenaPS                # off, 5 s, on
tapo-power cycle ArenaPS --off-s 10 --wait-above-w 3 --timeout-s 90
tapo-power power ArenaPS                # prints watts, e.g. 2.705
tapo-power log                          # tidy CSV to stdout until Ctrl-C (a live readout)
tapo-power log --out run.csv --interval-s 0.5 --duration-s 3600 --outlets ArenaPS,1
```

Exit codes: `0` ok · `1` device / network / auth / unknown-outlet error (message on stderr) · `2` bad arguments · `3` `--wait-above-w` threshold not reached before timeout · `130` Ctrl-C.

`log --out` appends (header only when the file is new), flushes every sample, and shows a live one-line readout on stderr when run in a terminal. A failed sample is logged as a warning and skipped; the logger reconnects and keeps going.

## Python API (other projects)

Install into another project's environment as an editable dependency:

```bash
uv add --editable ~/Documents/GitHub/claude-skills/tapo-power      # uv projects
pip install -e ~/Documents/GitHub/claude-skills/tapo-power          # plain venvs
```

```python
from tapo_power import Strip, WaitTimeout

with Strip() as strip:                          # default strip from config; Strip("name") or Strip(host="1.2.3.4")
    print(strip.power("ArenaPS"))               # 2.705 (W)
    strip.off("ArenaPS"); strip.on("ArenaPS")   # each confirms the relay state, returns the position

    # Power-cycle and block until the device is drawing > 3 W again (booted).
    try:
        w = strip.cycle("ArenaPS", off_s=5, wait_above_w=3, timeout_s=60)
    except WaitTimeout:
        ...                                     # it came back on but never drew power

    # Log in the background for the duration of an experiment.
    with strip.background_log("run1_power.csv", interval_s=1.0, outlets=["ArenaPS"]):
        run_experiment()

    st = strip.status()                         # StripStatus: model, host, rssi, outlets[..], total_w
    rows = strip.sample()                       # list[OutletStatus]: power_w, voltage_v, current_a (no energy; faster)
    strip.wait_for_power("ArenaPS", below_w=1, timeout_s=30)
```

`Strip` is synchronous: it runs its own event loop on a background thread, so it never touches the caller's loop (usable from Jupyter or async code) and is safe to share across threads. Errors are `TapoPowerError`; `WaitTimeout` subclasses it.

## MATLAB

```matlab
tp = '~/.claude/skills/tapo-power/bin/tapo-power';
[rc, out] = system([tp ' --json status']);  s = jsondecode(out);
[rc, out] = system([tp ' cycle ArenaPS --off-s 5 --wait-above-w 3']);  % rc==3 → never drew power
```

## CSV format

Tidy — one row per outlet per sample:

```
timestamp,elapsed_s,strip,position,name,on,power_w,voltage_v,current_a
2026-09-26T23:13:36.891-04:00,0.000,SmartPowerStrip,6,ArenaPS,1,2.719,121.159,0.052
```

`name` is the alias if set, otherwise the Tapo nickname. `elapsed_s` is monotonic from the start of that logging call.

## Measurement characteristics (measured on this P316M, fw 1.0.5)

| Property | Value |
|---|---|
| Readings | Per outlet via `get_emeter_data`: real power (mW), RMS voltage (mV), RMS current (mA). Needs the outlet's `energy_monitoring` component v2 (P316M fw 1.0.5 has it). |
| Meter refresh | A new reading every ~1.1 s (median 1.09 s, IQR 1.05–1.12 s, timed from mV voltage changes). This is the real ceiling: polling faster only repeats values, and at exactly 1 Hz ~8% of samples repeat. Each reading is averaged over that window, so short inrush peaks are smoothed away. |
| Meter lag after switching | ~1.5–3 s, and the reading ramps rather than steps (measured on the earlier `tapo`-library backend; not yet re-measured). Don't use it for sub-second timing. |
| Power factor | Power is real W; V × A is apparent VA. Small switching supplies show PF ≈ 0.5 (ArenaPS idles at ~0.48). |
| Connect | ~1 s (KLAP handshake + first update); a `Strip` reuses the session afterwards |
| Switch latency | ~0.1–0.2 s including the confirming read-back |
| Read throughput | `power()` on one outlet ~23 ms (~40 req/s); `sample()` one outlet ~150 ms, all 6 outlets ~250 ms (~4/s); `status` (adds energy) ~0.8 s. The network is never the bottleneck. |
| Energy counters | today / month Wh per outlet (`status`). The strip also stores 5-minute average power per outlet (back to when it was powered up), not exposed by the CLI. |
| Practical logging rates | 1 Hz to catch transients (~7 MB/day per outlet as CSV); 5–10 s for long runs; for hours-to-days trends the strip's own 5-minute history needs nothing running. |
| Mains voltage | ~121 V RMS at home, varying ±0.2 V — the same on every outlet, so `status` shows it once |

Consequence for `--wait-above-w`: after switching on, the reading may stay near 0 W for a couple of seconds even if the load draws immediately; the threshold wait handles this, but give `--timeout-s` headroom.

## Network notes

- Control is HTTP (TCP 80, KLAP-encrypted) to the strip's IP, connecting with known protocol parameters, so it needs no UDP. Finding a moved strip uses mDNS (`<MAC>.local`) and UDP broadcast (ports 20002 and 9999); both only work on the same LAN segment. Direct control only needs routability.
- **Security:** a Matter-only strip accepts the published factory-default credentials, so anything on the same network can switch it. Fine at home; on shared networks (Janelia) prefer binding it to a TP-Link account (`login`) or an isolated IoT VLAN.
- Janelia's managed Wi-Fi likely blocks the strip from joining or isolates clients; expect to need a lab-controlled network (e.g., a travel router on the bench). Broadcast discovery won't cross subnets — use `add NAME IP`.

## Gotchas

| Symptom | Cause | Fix |
|---|---|---|
| `Authentication failed at ...` | Strip is bound to a Tapo account, or the stored password is stale | `tapo-power login <email>` (user runs it); enable Third-Party Compatibility in the Tapo app |
| `Cannot reach strip ... ` | IP changed and MAC re-discovery failed, strip offline, or different network | `tapo-power discover`; then `tapo-power add NAME NEW_IP` |
| Discovery finds nothing but the strip works in Apple Home | Mac on a different subnet / VPN, or macOS Local Network permission denied | System Settings → Privacy & Security → Local Network → allow the terminal app / Claude; disconnect VPN |
| `discover` lists nothing from the broadcast | Strip not answering TP-Link discovery, or on TPAP firmware (python-kasa drops those replies) | Configured strips are still found via mDNS (`found via mDNS`); for a new strip, `tapo-power add NAME IP` |
| `refused the KLAP login (HTTP 403)` | Firmware update switched the strip to TP-Link's TPAP protocol (discovery reply shows `"encrypt_type": "TPAP"`); neither python-kasa 0.10.x nor `tapo` 0.10 supports it | Add the strip to the Tapo app, enable Me → Third-Party Services → Third-Party Compatibility (reverts to KLAP), then `tapo-power login <email>` |
| Keychain "allow access" dialog | New Python interpreter reading the stored password (after a uv Python upgrade) | Click Always Allow |
| Power reads 0 W right after `on` | Meter lag (see above) | Use `--wait-above-w` / `wait_for_power` rather than a single read |
| `No outlet at position 7` / `Unknown outlet` | Typo or alias not set | `tapo-power status` lists names; `tapo-power alias NAME N` |

## Development

```bash
cd ~/Documents/GitHub/claude-skills/tapo-power
uv run pytest tests -q          # fakes only — no network, no Keychain
```

All python-kasa code lives in `tapo_power/strip.py`: `_open(host, creds)` returns a `_Connection` (child list, device info, per-outlet `_Plug` with `reading()` / `energy()` / `on()` / `off()`), and `_discover(target, timeout_s)` wraps broadcast discovery. Tests monkeypatch `_open`/`_discover` with fakes of that interface, and an autouse fixture blocks real network calls. `_Plug` sends the strip's own JSON methods (`get_emeter_data`, `get_energy_usage`) through python-kasa's authenticated protocol. Only P304M/P316M are accepted (`SUPPORTED_MODELS`, and `_CONNECTION` hard-codes their KLAP v2 parameters); other Tapo strips would need both relaxed.
