---
name: tapo-power
description: Control and monitor TP-Link Tapo P316M (and P304M) smart power strips over the local network — switch individual outlets on/off, power-cycle lab electronics (optionally waiting until the device draws power again), read per-outlet watts and today/month energy, and log power to CSV during experiments. Provides a CLI (`bin/tapo-power`) and an importable Python module (`from tapo_power import Strip`) for other projects, scripts, and MATLAB. Use when the user mentions the Tapo strip, P316M, smart power strip, "power cycle", "turn off/on outlet N", "reboot the arena power supply", "how much power is X drawing", "log power consumption", or names an outlet alias like ArenaPS. Do NOT use for Kasa-brand plugs (HS/KP/EP series) or non-Tapo PDUs.
argument-hint: [status | on | off | cycle | power | log | discover | add | alias] [outlet]
---

# Tapo Power Strip Control

Local-network control of a Tapo P316M (6 individually switched, individually metered outlets) via the [`tapo`](https://github.com/mihai-dinculescu/tapo) Rust/Python library. No cloud round-trip, and it doesn't touch the strip's Matter pairing — the strip stays in Apple Home.

| Capability | How |
|---|---|
| Per-outlet on / off | `tapo-power on ArenaPS`, `tapo-power off 3` |
| Power-cycle, optionally wait for boot | `tapo-power cycle ArenaPS --off-s 5 --wait-above-w 3` |
| Live readout (W, today/month Wh) | `tapo-power status` |
| One outlet's watts (script-friendly) | `tapo-power power ArenaPS` → `11` |
| CSV power logging | `tapo-power log --out run.csv --interval-s 1` |
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
tapo-power discover                    # no credentials needed; UDP 20002 broadcast
tapo-power add SmartPowerStrip         # auto-picks the only P304M/P316M found; or: add NAME IP
tapo-power alias ArenaPS 6             # name outlet 6
tapo-power unalias ArenaPS
```

`add` stores the IP and MAC. If the IP later changes (DHCP), a failed connection triggers a discovery broadcast, the strip is re-found by MAC, and the new IP is saved automatically. A DHCP reservation on the router is still the robust fix.

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

- **Matter-only strips** (set up via Apple Home, never added to the Tapo app — discovery shows `no account, onboarded via matter`) accept TP-Link's factory-default local credentials. Nothing to configure. This is the current setup.
- **Account-bound strips** (added to the Tapo app) only accept that TP-Link account. The user must run, in their own terminal (it prompts for a password — Claude can't type it):
  ```bash
  tapo-power login you@example.com
  ```
  The password goes to the macOS Keychain (service `tapo-power`); only the email is written to the config. The Tapo app's **Me → Third-Party Services → Third-Party Compatibility** must also be ON, or newer firmware refuses local control.

Credentials are tried in order: stored account, then factory default — so a mix of owned and unowned strips works.

## CLI reference

```bash
tapo-power status                       # table: state, W, today/month Wh, time since last switch
tapo-power --json status                # includes total_w
tapo-power on ArenaPS                   # switches, then re-reads the relay to confirm
tapo-power off 3
tapo-power cycle ArenaPS                # off, 5 s, on
tapo-power cycle ArenaPS --off-s 10 --wait-above-w 3 --timeout-s 90
tapo-power power ArenaPS                # prints integer watts
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
    print(strip.power("ArenaPS"))               # 11.0 (W)
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
    rows = strip.sample()                       # list[OutletStatus] with power_w, no energy (faster)
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
timestamp,elapsed_s,strip,position,name,on,power_w
2026-09-26T22:18:18.412-04:00,0.000,SmartPowerStrip,6,ArenaPS,1,9.0
```

`name` is the alias if set, otherwise the Tapo nickname. `elapsed_s` is monotonic from the start of that logging call.

## Measurement characteristics (measured on this P316M, fw 1.0.5)

| Property | Value |
|---|---|
| Power resolution | **1 W** (integer watts — no mW field on P316M outlets) |
| Meter lag / smoothing | Reading trails reality by ~1.5–3 s after a switch and ramps rather than steps. Don't use it for sub-second timing. |
| Switch latency | ~0.3–0.5 s including the confirming read-back |
| Read latency | ~20 ms per outlet; a full 6-outlet sample ~0.2 s. 0.5 s logging intervals work. |
| Voltage / current | Not exposed by the local API |
| Energy counters | today / month Wh per outlet (`status`) |

Consequence for `--wait-above-w`: after switching on, the reading stays at 0 W for a couple of seconds even if the load draws immediately; the threshold wait handles this, but give `--timeout-s` headroom.

## Network notes

- Control is HTTP (TCP 80, KLAP-encrypted) to the strip's IP; discovery is UDP 20002 broadcast. The Mac and strip must be on the same LAN segment for discovery; direct control only needs routability.
- **Security:** a Matter-only strip accepts the published factory-default credentials, so anything on the same network can switch it. Fine at home; on shared networks (Janelia) prefer binding it to a TP-Link account (`login`) or an isolated IoT VLAN.
- Janelia's managed Wi-Fi likely blocks the strip from joining or isolates clients; expect to need a lab-controlled network (e.g., a travel router on the bench). Broadcast discovery won't cross subnets — use `add NAME IP`.

## Gotchas

| Symptom | Cause | Fix |
|---|---|---|
| `Authentication failed ... HASH_MISMATCH` | Strip is bound to a Tapo account, or the stored password is stale | `tapo-power login <email>` (user runs it); enable Third-Party Compatibility in the Tapo app |
| `Cannot reach strip ... ` | IP changed and MAC re-discovery failed, strip offline, or different network | `tapo-power discover`; then `tapo-power add NAME NEW_IP` |
| Discovery finds nothing but the strip works in Apple Home | Mac on a different subnet / VPN, or macOS Local Network permission denied | System Settings → Privacy & Security → Local Network → allow the terminal app / Claude; disconnect VPN |
| `discover` occasionally returns nothing | Discovery is a single UDP broadcast; replies sometimes drop | Rerun it (automatic IP re-find already tries twice) |
| Keychain "allow access" dialog | New Python interpreter reading the stored password (after a uv Python upgrade) | Click Always Allow |
| Power reads 0 W right after `on` | Meter lag (see above) | Use `--wait-above-w` / `wait_for_power` rather than a single read |
| `No outlet at position 7` / `Unknown outlet` | Typo or alias not set | `tapo-power status` lists names; `tapo-power alias NAME N` |

## Development

```bash
cd ~/Documents/GitHub/claude-skills/tapo-power
uv run pytest tests -q          # fakes only — no network, no Keychain
```

The device seam is two async functions in `tapo_power/strip.py` — `_open(host, creds)` and `_discover(target, timeout_s)`; tests monkeypatch them. Other Tapo models (P110 plugs, P300 strips) would need their own `ApiClient` method in `_open`; only P304M/P316M are supported today.
