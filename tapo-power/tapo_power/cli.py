from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from dataclasses import asdict

from .config import Config, StripConfig, TapoPowerError, store_password
from .strip import SUPPORTED_MODELS, OutletStatus, Strip, StripStatus, WaitTimeout, _norm_mac, discover, find_by_mac

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


def _status_dict(st: StripStatus) -> dict:
    d = asdict(st)
    d["total_w"] = st.total_w
    return d


def _print_status(st: StripStatus) -> None:
    volts = [o.voltage_v for o in st.outlets if o.voltage_v]
    mains = f"  mains {sum(volts) / len(volts):.1f} V" if volts else ""
    print(f"{st.name}  {st.model}  {st.host}  mac {st.mac}  fw {st.fw_ver.split()[0]}  rssi {st.rssi} dBm{mains}")
    width = max([len(o.name) for o in st.outlets] + [4])
    print(f" #  {'name':<{width}}  state  power_W  current_mA  today_Wh  month_Wh  since_change")
    for o in st.outlets:
        state = "on" if o.on else "off"
        print(
            f" {o.position}  {o.name:<{width}}  {state:<5}  {o.power_w:>7.1f}  {o.current_a * 1000:>10.0f}  "
            f"{o.today_wh:>8}  {o.month_wh:>8}  {_duration(o.on_time_s):>12}"
        )
    print(f"    {'total':<{width}}         {st.total_w:>7.1f}")


def _open_strip(args, cfg: Config) -> Strip:
    return Strip(args.strip, host=args.host, config=cfg)


def cmd_discover(args, cfg):
    """Broadcast discovery, plus an mDNS lookup of every configured strip; a
    configured strip found at a new IP has its saved host updated."""
    found = [dict(d, via="broadcast") for d in discover(timeout_s=args.timeout)]
    by_mac = {_norm_mac(d["mac"] or ""): d for d in found}
    moved = []
    for name, sc in cfg.strips.items():
        if not sc.mac:
            continue
        d = by_mac.get(_norm_mac(sc.mac))
        if d is None:
            ip = find_by_mac(sc.mac)
            if ip is None:
                continue
            d = {"ip": ip, "model": None, "mac": sc.mac, "device_id": None, "owner_bound": None,
                 "onboarded_via": None, "via": "mdns"}
            found.append(d)
        d["configured_as"] = name
        if d["ip"] != sc.host:
            moved.append((name, sc.host, d["ip"]))
            sc.host = d["ip"]
    if moved:
        cfg.save()
    if args.json:
        _print_json(found)
        return
    if not found:
        print("No Tapo devices found (UDP broadcast, and mDNS for configured strips).")
    for d in found:
        tag = f"  [configured as {d['configured_as']!r}]" if "configured_as" in d else ""
        if d["via"] == "mdns":
            print(f"{d['ip']:<15}  {'?':<12}  mac {d['mac']}  found via mDNS (no broadcast reply){tag}")
        else:
            owner = "account-bound" if d["owner_bound"] else "no account"
            print(f"{d['ip']:<15}  {d['model']:<12}  mac {d['mac']}  {owner}, onboarded via {d['onboarded_via']}{tag}")
    for name, old, new in moved:
        print(f"Updated {name!r}: {old} -> {new}")


def cmd_add(args, cfg):
    if args.host:
        found = discover(target=args.host, timeout_s=args.timeout)
        host = args.host
        mac = found[0]["mac"] if found else None
    else:
        strips = [d for d in discover(timeout_s=args.timeout) if (d["model"] or "").startswith(SUPPORTED_MODELS)]
        if not strips:
            raise TapoPowerError("No P304M/P316M answered discovery; pass its IP: tapo-power add NAME HOST")
        if len(strips) > 1:
            listing = ", ".join(f"{d['ip']} ({d['model']})" for d in strips)
            raise TapoPowerError(f"Several strips found: {listing}. Pass the IP: tapo-power add NAME HOST")
        host, mac = strips[0]["ip"], strips[0]["mac"]
    with Strip(host=host, config=cfg) as s:
        outlets = s.outlets()
    old = cfg.strips.get(args.name)
    cfg.strips[args.name] = StripConfig(name=args.name, host=host, mac=mac, outlets=old.outlets if old else {})
    if cfg.default is None or args.default:
        cfg.default = args.name
    cfg.save()
    print(f"Saved strip {args.name!r}: {host} (mac {mac or 'unknown'}){'  [default]' if cfg.default == args.name else ''}")
    for o in outlets:
        print(f"  {o.position}  {o.nickname:<20}  {'on' if o.on else 'off'}")
    if mac is None:
        print("Warning: MAC unknown, so the strip can't be re-found automatically if its IP changes.")


def cmd_alias(args, cfg):
    sc = cfg.strip(args.strip)
    if args.alias.strip().isdigit():
        raise TapoPowerError("Alias must not be a number")
    with Strip(sc.name, config=cfg) as s:
        pos = s.resolve(args.outlet)
    sc.outlets = {a: p for a, p in sc.outlets.items() if p != pos and a.lower() != args.alias.lower()}
    sc.outlets[args.alias] = pos
    cfg.save()
    print(f"{sc.name}: outlet {pos} is now {args.alias!r}")


def cmd_unalias(args, cfg):
    sc = cfg.strip(args.strip)
    matches = [a for a in sc.outlets if a.lower() == args.alias.lower()]
    if not matches:
        raise TapoPowerError(f"No alias {args.alias!r} on {sc.name} (have: {', '.join(sc.outlets) or 'none'})")
    for a in matches:
        del sc.outlets[a]
    cfg.save()
    print(f"{sc.name}: removed alias {args.alias!r}")


def cmd_login(args, cfg):
    password = getpass.getpass(f"TP-Link password for {args.email}: ")
    if not password:
        raise TapoPowerError("Empty password; nothing stored")
    store_password(args.email, password)
    cfg.account = args.email
    cfg.save()
    print(f"Stored password for {args.email} in the macOS Keychain (service 'tapo-power').")


def cmd_status(args, cfg):
    with _open_strip(args, cfg) as s:
        st = s.status()
    _print_json(_status_dict(st)) if args.json else _print_status(st)


def _report_switch(args, s: Strip, pos: int, state: str) -> None:
    name = next(o.name for o in s.outlets() if o.position == pos)
    if args.json:
        _print_json({"strip": s.name, "position": pos, "name": name, "state": state})
    else:
        print(f"{name} (outlet {pos}): {state}")


def cmd_on(args, cfg):
    with _open_strip(args, cfg) as s:
        _report_switch(args, s, s.on(args.outlet), "on")


def cmd_off(args, cfg):
    with _open_strip(args, cfg) as s:
        _report_switch(args, s, s.off(args.outlet), "off")


def cmd_cycle(args, cfg):
    with _open_strip(args, cfg) as s:
        pos = s.resolve(args.outlet)
        if not args.json:
            print(f"Cycling outlet {pos}: off for {args.off_s} s ...", flush=True)
        w = s.cycle(pos, off_s=args.off_s, wait_above_w=args.wait_above_w, timeout_s=args.timeout_s)
        if args.json:
            _print_json({"strip": s.name, "position": pos, "state": "on", "power_w": w})
        else:
            print(f"Outlet {pos} back on" + (f", drawing {w:.1f} W" if w is not None else ""))


def cmd_power(args, cfg):
    with _open_strip(args, cfg) as s:
        w = s.power(args.outlet)
    _print_json({"outlet": args.outlet, "power_w": w}) if args.json else print(f"{w:.3f}")


def _live_line(rows: list[OutletStatus]) -> None:
    parts = "  ".join(f"{o.name}={o.power_w:.1f}W" for o in rows)
    print(f"\r{parts}  total={sum(o.power_w for o in rows):.1f}W ", end="", file=sys.stderr, flush=True)


def cmd_log(args, cfg):
    outlets = args.outlets.split(",") if args.outlets else None
    kwargs = dict(interval_s=args.interval_s, duration_s=args.duration_s, outlets=outlets)
    with _open_strip(args, cfg) as s:
        if args.out is None:
            s.log_csv(sys.stdout, **kwargs)
            return
        live = sys.stderr.isatty()
        try:
            n = s.log_csv(args.out, on_sample=_live_line if live else None, **kwargs)
            summary = f"Wrote {n} samples to {args.out}"
        except KeyboardInterrupt:
            summary = f"Stopped; samples are in {args.out}"
        if live:
            print(file=sys.stderr)
        print(summary)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tapo-power",
        description="Local control and power/voltage/current monitoring for Tapo P316M/P304M power strips.",
    )
    p.add_argument("--strip", help="configured strip name (default: the config's default strip)")
    p.add_argument("--host", help="talk to this IP directly instead of a configured strip")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("discover", help="list Tapo devices on the LAN (no credentials needed)")
    s.add_argument("--timeout", type=int, default=3)
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("add", help="save a strip to the config (discovers it if HOST is omitted)")
    s.add_argument("name")
    s.add_argument("host", nargs="?")
    s.add_argument("--default", action="store_true", help="make this the default strip")
    s.add_argument("--timeout", type=int, default=3)
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("alias", help="name an outlet, e.g. `alias arena 3`")
    s.add_argument("alias")
    s.add_argument("outlet")
    s.set_defaults(fn=cmd_alias)

    s = sub.add_parser("unalias", help="remove an outlet alias")
    s.add_argument("alias")
    s.set_defaults(fn=cmd_unalias)

    s = sub.add_parser("login", help="store TP-Link account password in the Keychain (only for account-bound strips)")
    s.add_argument("email")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("status", help="per-outlet state, power, current, and energy")
    s.set_defaults(fn=cmd_status)

    for name, fn in (("on", cmd_on), ("off", cmd_off)):
        s = sub.add_parser(name, help=f"switch an outlet {name}")
        s.add_argument("outlet", help="position 1-6, alias, or Tapo nickname")
        s.set_defaults(fn=fn)

    s = sub.add_parser("cycle", help="power-cycle an outlet")
    s.add_argument("outlet")
    s.add_argument("--off-s", type=float, default=5.0, help="seconds to stay off (default 5)")
    s.add_argument("--wait-above-w", type=float, help="after switching on, wait until draw exceeds this many watts")
    s.add_argument("--timeout-s", type=float, default=60.0, help="limit for --wait-above-w (default 60)")
    s.set_defaults(fn=cmd_cycle)

    s = sub.add_parser("power", help="print one outlet's real power draw in watts")
    s.add_argument("outlet")
    s.set_defaults(fn=cmd_power)

    s = sub.add_parser("log", help="log per-outlet power, voltage, current to CSV (stdout if --out is omitted)")
    s.add_argument("--out", help="CSV file to append to")
    s.add_argument("--interval-s", type=float, default=1.0)
    s.add_argument("--duration-s", type=float, help="stop after this long (default: until Ctrl-C)")
    s.add_argument("--outlets", help="comma-separated outlets (default: all)")
    s.set_defaults(fn=cmd_log)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="tapo-power: %(message)s")
    try:
        args.fn(args, Config.load())
    except WaitTimeout as e:
        print(f"tapo-power: {e}", file=sys.stderr)
        return EXIT_TIMEOUT
    except TapoPowerError as e:
        print(f"tapo-power: {e}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
