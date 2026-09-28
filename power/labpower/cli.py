from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import logging
import signal
import sys
from dataclasses import asdict

from .config import Config, DeviceEntry, PowerError, WaitTimeout, alias_targets, store_tapo_password
from .core import Power
from .model import ChannelStatus, DeviceStatus

EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_TIMEOUT = 3


def _duration(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h >= 24:
        return f"{h // 24}d{h % 24}h"
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _device_dict(st: DeviceStatus) -> dict:
    return dict(asdict(st), total_w=st.total_w)


def _print_device(st: DeviceStatus) -> None:
    if not st.online:
        print(f"{st.name}  {st.type}  OFFLINE: {st.error}\n")
        return
    i = st.info
    volts = [c.voltage_v for c in st.channels if c.voltage_v]
    head = [st.name, st.type, str(i.get("model") or "?")]
    if i.get("host"):
        head.append(i["host"])
    if i.get("fw_ver"):
        head.append(f"fw {str(i['fw_ver']).split()[0]}")
    if i.get("rssi") is not None:
        head.append(f"rssi {i['rssi']} dBm")
    if i.get("linkquality") is not None:
        head.append(f"lqi {i['linkquality']}")
    if volts:
        head.append(f"mains {sum(volts) / len(volts):.1f} V")
    print("  ".join(head))

    chans = st.channels
    cols: list[tuple[str, int, callable]] = [
        ("ch", 3, lambda c: str(c.channel)),
        ("name", max([len(c.name) for c in chans] + [4]), lambda c: c.name),
        ("state", 5, lambda c: "on" if c.on else "off"),
        ("power_W", 8, lambda c: f"{c.power_w:.1f}" if c.power_w is not None else "-"),
        ("current_mA", 10, lambda c: f"{c.current_a * 1000:.0f}" if c.current_a is not None else "-"),
    ]
    if any(c.today_wh is not None for c in chans):
        cols += [("today_Wh", 8, lambda c: str(c.today_wh)), ("month_Wh", 8, lambda c: str(c.month_wh))]
    if any(c.total_kwh is not None for c in chans):
        cols.append(("total_kWh", 9, lambda c: f"{c.total_kwh:.3f}" if c.total_kwh is not None else "-"))
    if any(c.on_time_s is not None for c in chans):
        cols.append(("since_change", 12, lambda c: _duration(c.on_time_s) if c.on_time_s is not None else "-"))

    def row(values: list[str]) -> str:
        return "  ".join(v.ljust(w) if k in ("name", "state") else v.rjust(w) for (k, w, _), v in zip(cols, values))

    print(row([k for k, _, _ in cols]))
    for c in chans:
        print(row([fmt(c) for _, _, fmt in cols]))
    if len(chans) > 1:
        print(row(["", "total", "", f"{st.total_w:.1f}"] + [""] * (len(cols) - 4)))
    print()


def _outlet_label(p: Power, device: str, channel: int) -> str:
    alias = p.display_name(device, channel, "")
    if alias:
        return f"{alias} ({device}:{channel})"
    return device if p.config.device(device).type == "zigbee2mqtt" else f"{device}:{channel}"


def cmd_status(args, cfg):
    with Power(cfg) as p:
        statuses = p.status(args.devices or None)
    if args.json:
        _print_json([_device_dict(s) for s in statuses])
        return
    if not statuses:
        print("No devices configured. Add one: power add-tapo NAME [HOST] / power add-zigbee NAME")
    for st in statuses:
        _print_device(st)
    if statuses and all(not s.online for s in statuses):
        raise PowerError("no device reachable")


def _switch(args, cfg, on: bool):
    with Power(cfg) as p:
        device, channel = p.on(args.outlet) if on else p.off(args.outlet)
        state = "on" if on else "off"
        if args.json:
            _print_json({"device": device, "channel": channel, "name": p.display_name(device, channel, device),
                         "state": state})
        else:
            print(f"{_outlet_label(p, device, channel)}: {state}")


def cmd_on(args, cfg):
    _switch(args, cfg, True)


def cmd_off(args, cfg):
    _switch(args, cfg, False)


def cmd_cycle(args, cfg):
    with Power(cfg) as p:
        device, channel = p.resolve(args.outlet)
        label = _outlet_label(p, device, channel)
        if not args.json:
            print(f"Cycling {label}: off for {args.off_s} s ...", flush=True)
        w = p.cycle(f"{device}:{channel}", off_s=args.off_s, wait_above_w=args.wait_above_w, timeout_s=args.timeout_s)
        if args.json:
            _print_json({"device": device, "channel": channel, "state": "on", "power_w": w})
        else:
            print(f"{label} back on" + (f", drawing {w:.1f} W" if w is not None else ""))


def cmd_read(args, cfg):
    with Power(cfg) as p:
        r = p.read(args.outlet)
    if args.json:
        _print_json(asdict(r))
    else:
        print(f"{r.name}: {'on' if r.on else 'off'}  {r.power_w:.3f} W  {r.voltage_v:.1f} V  {r.current_a:.3f} A")


def _live_line(rows: list[ChannelStatus]) -> None:
    parts = "  ".join(f"{r.name}={r.power_w:.1f}W" for r in rows if r.power_w is not None)
    print(f"\r{parts}  total={sum(r.power_w or 0 for r in rows):.1f}W ", end="", file=sys.stderr, flush=True)


def _sigterm_as_interrupt(signum, frame):
    raise KeyboardInterrupt


def cmd_log(args, cfg):
    signal.signal(signal.SIGTERM, _sigterm_as_interrupt)  # lets a parent process stop the logger cleanly
    kwargs = dict(interval_s=args.interval_s, duration_s=args.duration_s, outlets=args.outlets or None,
                  best_effort=args.best_effort, retry_s=args.retry_s)
    with Power(cfg) as p:
        if args.out is None:
            p.log_csv(sys.stdout, **kwargs)
            return
        live = sys.stderr.isatty()
        try:
            n = p.log_csv(args.out, on_sample=_live_line if live else None, **kwargs)
            summary = f"Wrote {n} samples to {args.out}"
        except KeyboardInterrupt:
            summary = f"Stopped; samples are in {args.out}"
        if live:
            print(file=sys.stderr)
        print(summary)


def cmd_discover(args, cfg):
    """Tapo: UDP broadcast plus an mDNS lookup of every configured strip (a strip
    found at a new IP has its saved host updated). Zigbee: devices paired with
    Zigbee2MQTT."""
    from .tapo import _norm_mac, discover, find_by_mac

    tapo = [dict(d, via="broadcast") for d in discover(timeout_s=args.timeout)]
    by_mac = {_norm_mac(d["mac"] or ""): d for d in tapo}
    moved = []
    for entry in cfg.devices.values():
        if entry.type != "tapo-strip" or not entry.mac:
            continue
        d = by_mac.get(_norm_mac(entry.mac))
        if d is None:
            ip = find_by_mac(entry.mac)
            if ip is None:
                continue
            d = {"ip": ip, "model": None, "mac": entry.mac, "via": "mdns"}
            tapo.append(d)
        d["configured_as"] = entry.name
        if d["ip"] != entry.host:
            moved.append((entry.name, entry.host, d["ip"]))
            entry.host = d["ip"]
    if moved:
        cfg.save()

    zigbee, zigbee_error = [], None
    try:
        from .zigbee import Z2MBridge

        bridge = Z2MBridge(cfg.mqtt)
        try:
            configured = {(e.friendly_name or e.name): e.name for e in cfg.devices.values() if e.type == "zigbee2mqtt"}
            for d in bridge.devices():
                definition = d.get("definition") or {}
                zigbee.append({
                    "friendly_name": d.get("friendly_name"), "ieee_address": d.get("ieee_address"),
                    "vendor": definition.get("vendor"), "model": definition.get("model"),
                    "description": definition.get("description"), "supported": d.get("supported"),
                    "configured_as": configured.get(d.get("friendly_name")),
                })
        finally:
            bridge.close()
    except PowerError as e:
        zigbee_error = str(e)

    if args.json:
        _print_json({"tapo": tapo, "zigbee": zigbee, "zigbee_error": zigbee_error})
        return
    print("Tapo strips (UDP broadcast + mDNS for configured strips):")
    for d in tapo:
        tag = f"  [configured as {d['configured_as']!r}]" if d.get("configured_as") else ""
        if d["via"] == "mdns":
            print(f"  {d['ip']:<15}  {'?':<12}  mac {d['mac']}  found via mDNS{tag}")
        else:
            owner = "account-bound" if d["owner_bound"] else "no account"
            print(f"  {d['ip']:<15}  {d['model']:<12}  mac {d['mac']}  {owner}, onboarded via {d['onboarded_via']}{tag}")
    if not tapo:
        print("  none found")
    for name, old, new in moved:
        print(f"  Updated {name!r}: {old} -> {new}")
    print("Zigbee2MQTT devices:")
    if zigbee_error:
        print(f"  unavailable: {zigbee_error}")
    for d in zigbee:
        tag = f"  [configured as {d['configured_as']!r}]" if d["configured_as"] else "  [not configured: power add-zigbee NAME]"
        print(f"  {d['friendly_name']:<20}  {d['vendor']} {d['model']} ({d['description']})  {d['ieee_address']}{tag}")
    if not zigbee and not zigbee_error:
        print("  none paired")


def _check_new_name(cfg: Config, name: str) -> None:
    if ":" in name or name.strip().isdigit():
        raise PowerError("Device names can't contain ':' or be a number")
    if any(a.lower() == name.lower() for a in cfg.aliases):
        raise PowerError(f"{name!r} is already an alias")


def cmd_add_tapo(args, cfg):
    from .tapo import SUPPORTED_MODELS, TapoStrip, discover

    _check_new_name(cfg, args.name)
    if args.host:
        found = discover(target=args.host, timeout_s=args.timeout)
        host, mac = args.host, (found[0]["mac"] if found else None)
    else:
        strips = [d for d in discover(timeout_s=args.timeout) if (d["model"] or "").startswith(SUPPORTED_MODELS)]
        if not strips:
            raise PowerError("No P304M/P316M answered discovery; pass its IP: power add-tapo NAME HOST")
        if len(strips) > 1:
            listing = ", ".join(f"{d['ip']} ({d['model']})" for d in strips)
            raise PowerError(f"Several strips found: {listing}. Pass the IP: power add-tapo NAME HOST")
        host, mac = strips[0]["ip"], strips[0]["mac"]
    entry = DeviceEntry(name=args.name, type="tapo-strip", host=host, mac=mac)
    probe = TapoStrip(DeviceEntry(name=args.name, type="tapo-strip", host=host), config=None)
    try:
        channels = probe.channels()
    finally:
        probe.close()
    cfg.devices[args.name] = entry
    cfg.save()
    print(f"Saved Tapo strip {args.name!r}: {host} (mac {mac or 'unknown'})")
    for c in channels:
        print(f"  {args.name}:{c.channel}  {c.native_name:<20}  {'on' if c.on else 'off'}")
    if mac is None:
        print("Warning: MAC unknown, so the strip can't be re-found automatically if its IP changes.")


def cmd_add_zigbee(args, cfg):
    from .zigbee import Z2MBridge

    _check_new_name(cfg, args.name)
    friendly = args.friendly_name or args.name
    bridge = Z2MBridge(cfg.mqtt)
    try:
        d = bridge.device(friendly)
    finally:
        bridge.close()
    cfg.devices[args.name] = DeviceEntry(name=args.name, type="zigbee2mqtt", friendly_name=friendly)
    cfg.save()
    definition = d.get("definition") or {}
    print(f"Saved Zigbee device {args.name!r}: {definition.get('vendor')} {definition.get('model')} "
          f"({definition.get('description')}), Zigbee2MQTT name {friendly!r}")


def cmd_remove(args, cfg):
    entry = cfg.device(args.name)
    del cfg.devices[entry.name]
    dropped = []
    for a, value in list(cfg.aliases.items()):
        kept = [t for t in alias_targets(value) if t.rpartition(":")[0].lower() != entry.name.lower()]
        if not kept:
            del cfg.aliases[a]
            dropped.append(a)
        elif len(kept) < len(alias_targets(value)):
            cfg.aliases[a] = kept[0] if len(kept) == 1 else kept
    cfg.save()
    print(f"Removed {entry.name!r}" + (f" and its aliases {', '.join(dropped)}" if dropped else ""))


def cmd_alias(args, cfg):
    _check_new_name(cfg, args.alias)
    if any(n.lower() == args.alias.lower() for n in cfg.devices):
        raise PowerError(f"{args.alias!r} is already a device name")
    targets = []
    with Power(cfg) as p:
        for outlet in args.outlets:
            if ":" in outlet:  # validate the device without needing it online (fallback targets may be down)
                device, channel = p._parse_target(outlet)
            else:
                device, channel = p.resolve(outlet)
            targets.append(f"{device}:{channel}")
    others = {a: t for a, t in cfg.aliases.items() if a.lower() != args.alias.lower()}
    if len(targets) == 1:  # one name per outlet: drop other single-target aliases of the same outlet
        others = {a: t for a, t in others.items() if t != targets[0]}
    cfg.aliases = {**others, args.alias: targets[0] if len(targets) == 1 else targets}
    cfg.save()
    print(f"{args.alias} -> {' | '.join(targets)}" + ("  (first reachable wins)" if len(targets) > 1 else ""))


def cmd_unalias(args, cfg):
    matches = [a for a in cfg.aliases if a.lower() == args.alias.lower()]
    if not matches:
        raise PowerError(f"No alias {args.alias!r} (have: {', '.join(cfg.aliases) or 'none'})")
    for a in matches:
        del cfg.aliases[a]
    cfg.save()
    print(f"Removed alias {args.alias!r}")


def cmd_login(args, cfg):
    password = getpass.getpass(f"TP-Link password for {args.email}: ")
    if not password:
        raise PowerError("Empty password; nothing stored")
    store_tapo_password(args.email, password)
    cfg.tapo_account = args.email
    cfg.save()
    print(f"Stored password for {args.email} in the macOS Keychain (service 'tapo-power').")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="power",
        description="Switch, power-cycle, and measure lab power outlets: Tapo strips and Zigbee2MQTT plugs.",
    )
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    outlet_help = "alias, Device:channel, a single-outlet device's name, or a Tapo outlet nickname"

    s = sub.add_parser("status", help="every device (or the named ones): state, W, mA, energy")
    s.add_argument("devices", nargs="*")
    s.set_defaults(fn=cmd_status)

    for name, fn in (("on", cmd_on), ("off", cmd_off)):
        s = sub.add_parser(name, help=f"switch an outlet {name} (confirmed)")
        s.add_argument("outlet", help=outlet_help)
        s.set_defaults(fn=fn)

    s = sub.add_parser("cycle", help="power-cycle an outlet")
    s.add_argument("outlet", help=outlet_help)
    s.add_argument("--off-s", type=float, default=5.0, help="seconds to stay off (default 5)")
    s.add_argument("--wait-above-w", type=float, help="after switching on, wait until draw exceeds this many watts")
    s.add_argument("--timeout-s", type=float, default=60.0, help="limit for --wait-above-w (default 60)")
    s.set_defaults(fn=cmd_cycle)

    s = sub.add_parser("read", help="one outlet's state, W, V, A")
    s.add_argument("outlet", help=outlet_help)
    s.set_defaults(fn=cmd_read)

    s = sub.add_parser("log", help="log W/V/A to CSV (stdout if --out is omitted)")
    s.add_argument("outlets", nargs="*", help="outlets to log (default: all)")
    s.add_argument("--out", help="CSV file to append to")
    s.add_argument("--interval-s", type=float, default=1.0)
    s.add_argument("--duration-s", type=float, help="stop after this long (default: until Ctrl-C / SIGTERM)")
    s.add_argument("--best-effort", action="store_true",
                   help="never fail: retry unreachable outlets, fail over fallback aliases, exit 0")
    s.add_argument("--retry-s", type=float, default=30.0, help="best-effort retry period (default 30)")
    s.set_defaults(fn=cmd_log)

    s = sub.add_parser("discover", help="find Tapo strips on the LAN and devices paired with Zigbee2MQTT")
    s.add_argument("--timeout", type=int, default=3)
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("add-tapo", help="save a Tapo strip (discovers it if HOST is omitted)")
    s.add_argument("name")
    s.add_argument("host", nargs="?")
    s.add_argument("--timeout", type=int, default=3)
    s.set_defaults(fn=cmd_add_tapo)

    s = sub.add_parser("add-zigbee", help="save a plug paired with Zigbee2MQTT")
    s.add_argument("name")
    s.add_argument("--friendly-name", help="its Zigbee2MQTT name, if different from NAME")
    s.set_defaults(fn=cmd_add_zigbee)

    s = sub.add_parser("remove", help="forget a device and its aliases")
    s.add_argument("name")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("alias", help="name an outlet, e.g. `alias ArenaPS SmartPowerStrip:6`; "
                                     "several outlets = fallbacks, first reachable wins")
    s.add_argument("alias")
    s.add_argument("outlets", nargs="+", metavar="outlet", help=outlet_help)
    s.set_defaults(fn=cmd_alias)

    s = sub.add_parser("unalias", help="remove an alias")
    s.add_argument("alias")
    s.set_defaults(fn=cmd_unalias)

    s = sub.add_parser("login", help="store a TP-Link account password in the Keychain (account-bound strips only)")
    s.add_argument("email")
    s.set_defaults(fn=cmd_login)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="power: %(message)s")
    try:
        args.fn(args, Config.load())
    except WaitTimeout as e:
        print(f"power: {e}", file=sys.stderr)
        return EXIT_TIMEOUT
    except PowerError as e:
        print(f"power: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main())
