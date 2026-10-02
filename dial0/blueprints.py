"""Blueprints: template-based configuration of THIS switch. The operator answers a few questions (role, loopback,
ASN, ports, VLAN...); Dial 0 generates the full configuration from the template, checks it against the switch's
running configuration (conflicts are reported, never removed), shows it, and applies it after one y/N:
backup -> apply in order -> save -> verify.

Commands are generated only from validated values, so templates may use what normal requests can't: the vtysh
tool (FRR/BGP) and `config save`. The model is not involved."""
import ipaddress, json, os, re, shlex, time
from . import tools

STATE = os.getenv("DIAL0_STATE_DIR", "/var/lib/dial0")
SAVED = os.path.join(STATE, "blueprints")
BACKUP_DIR = os.getenv("DIAL0_BACKUP_DIR", "/etc/sonic/dial0-backups")
BGP_WAIT_S = float(os.getenv("DIAL0_BGP_WAIT_S", "30"))
MTU = 9216
PERSISTENT_MODES = ("split", "split-unified")  # FRR modes where vtysh + 'write memory' survive a reload


class BlueprintError(Exception):
    pass


# ------------------------------------------------------------------ the blueprint catalogue
CLOS = "2-tier-clos-ai-fabric-scale-out"
BLUEPRINTS = {
    CLOS: {
        "title": "2-tier Clos leaf/spine AI fabric (scale-out)",
        "desc": "Loopback, fabric links with MTU 9216 and IPv6 link-local (BGP unnumbered), eBGP with ECMP; "
                "leaf: one server VLAN with a gateway IP",
        "questions": [  # key, prompt, roles
            ("role", "Role of this switch (spine/leaf)", ("spine", "leaf")),
            ("loopback", "Loopback0 IP (e.g. 10.0.0.1 or 10.0.0.1/32)", ("spine", "leaf")),
            ("asn", "BGP ASN of this switch", ("spine", "leaf")),
            ("downlinks", "Downlinks: ports to the leaf switches (e.g. Ethernet0-Ethernet60 or Ethernet0,Ethernet4)", ("spine",)),
            ("downlinks", "Downlinks: server ports (e.g. Ethernet0-Ethernet60 or Ethernet0,Ethernet4)", ("leaf",)),
            ("uplinks", "Uplinks: ports to the spine switches", ("leaf",)),
            ("vlan", "Server VLAN ID", ("leaf",)),
            ("gateway", "VLAN gateway IP with prefix (e.g. 10.10.1.1/24)", ("leaf",)),
        ],
    },
}


def catalogue() -> list:
    return [{"name": n, "title": b["title"], "desc": b["desc"], "saved": saved(n)} for n, b in BLUEPRINTS.items()]


def questions(name: str, role: str = "") -> list:
    bp = _bp(name)
    return [{"key": k, "prompt": p} for k, p, roles in bp["questions"] if k == "role" or not role or role in roles]


def _bp(name: str) -> dict:
    if name not in BLUEPRINTS:
        raise BlueprintError(f"no blueprint '{name}'. Blueprints: {', '.join(BLUEPRINTS)}")
    return BLUEPRINTS[name]


def saved(name: str) -> dict:
    try:
        with open(os.path.join(SAVED, name + ".json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(name: str, params: dict):
    os.makedirs(SAVED, exist_ok=True)
    with open(os.path.join(SAVED, name + ".json"), "w") as f:
        json.dump(params, f, indent=1)


# ------------------------------------------------------------------ the switch's running configuration
def snapshot() -> dict:
    """The running CONFIG_DB as JSON (one host call) plus FRR's running BGP section."""
    code, out = tools.split_result(tools._run(["sonic-cfggen", "-d", "--print-data"], max_out=20_000_000))
    if code != 0:
        raise BlueprintError("couldn't read the switch's configuration (sonic-cfggen -d --print-data): "
                             + (out.strip().splitlines() or ["?"])[-1])
    try:
        db = json.loads(out)
    except ValueError:
        raise BlueprintError("the switch's configuration (sonic-cfggen) was not valid JSON")
    code, frr = tools.split_result(tools._run(["vtysh", "-c", "show running-config"], max_out=2_000_000))
    db["_frr"] = frr if code == 0 else ""
    return db


def _port_key(p: str):
    m = re.match(r"([A-Za-z-]+)(\d+)$", p)
    return (m.group(1), int(m.group(2))) if m else (p, 0)


def switch_ports(db: dict) -> list:
    return sorted(db.get("PORT", {}), key=_port_key)


# ------------------------------------------------------------------ validating the answers
def parse_ports(text: str, ports: list) -> list:
    """'Ethernet0-Ethernet60, Ethernet64' -> the switch's ports in that range (so breakout numbering, steps of
    4 or 8, comes from the switch itself). Every port must exist on this switch."""
    out, known = [], set(ports)
    for part in re.split(r"[,\s]+", (text or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(Ethernet)(\d+)-(?:Ethernet)?(\d+)", part, re.I)
        if m:
            lo, hi = int(m.group(2)), int(m.group(3))
            if lo > hi:
                raise BlueprintError(f"'{part}': the range goes backwards")
            sel = [p for p in ports if p.startswith("Ethernet") and p[8:].isdigit() and lo <= int(p[8:]) <= hi]
            if not sel:
                raise BlueprintError(f"'{part}': no ports of this switch in that range")
            out += [p for p in sel if p not in out]
            continue
        p = part[0].upper() + part[1:] if part.lower().startswith("ethernet") else part
        p = "Ethernet" + p[8:] if p.lower().startswith("ethernet") else p
        if p not in known:
            raise BlueprintError(f"'{part}' is not a port on this switch")
        if p not in out:
            out.append(p)
    if not out:
        raise BlueprintError("give at least one port")
    return out


def validate(name: str, raw: dict, db: dict):
    """-> (normalized params, {field: error})."""
    _bp(name)
    p, err = {}, {}
    role = (raw.get("role") or "").strip().lower()
    if role not in ("spine", "leaf"):
        err["role"] = "must be spine or leaf"
        return p, err
    p["role"] = role
    try:
        lo = ipaddress.ip_interface((raw.get("loopback") or "").strip() if "/" in (raw.get("loopback") or "")
                                    else (raw.get("loopback") or "").strip() + "/32")
        if lo.version != 4 or lo.network.prefixlen != 32 or lo.ip.is_unspecified or lo.ip.is_multicast or lo.ip.is_loopback:
            raise ValueError
        p["loopback"] = str(lo.ip)
    except ValueError:
        err["loopback"] = "must be an IPv4 address (a /32), e.g. 10.0.0.1"
    a = str(raw.get("asn") or "").strip()
    if a.isdigit() and 1 <= int(a) <= 4294967295 and int(a) not in (23456,):
        p["asn"] = int(a)
    else:
        err["asn"] = "must be a number from 1 to 4294967295"
    ports = switch_ports(db)
    for key in ("downlinks",) + (("uplinks",) if role == "leaf" else ()):
        try:
            p[key] = parse_ports(raw.get(key, ""), ports)
        except BlueprintError as e:
            err[key] = str(e)
    if "downlinks" in p and "uplinks" in p:
        both = [x for x in p["downlinks"] if x in p["uplinks"]]
        if both:
            err["uplinks"] = "these ports are also downlinks: " + ", ".join(both)
    if role == "leaf":
        v = str(raw.get("vlan") or "").strip()
        if v.isdigit() and 2 <= int(v) <= 4094:
            p["vlan"] = int(v)
        else:
            err["vlan"] = "must be a VLAN ID from 2 to 4094"
        try:
            gw = ipaddress.ip_interface((raw.get("gateway") or "").strip())
            if gw.version != 4 or "/" not in raw.get("gateway", "") or gw.network.prefixlen > 30:
                raise ValueError("prefix")
            if gw.ip in (gw.network.network_address, gw.network.broadcast_address):
                raise ValueError("host")
            p["gateway"] = str(gw)
            if "loopback" in p and ipaddress.ip_address(p["loopback"]) in gw.network:
                err["gateway"] = f"the loopback {p['loopback']} is inside this subnet"
        except ValueError as e:
            err["gateway"] = ("must be a host address in the subnet, not its network or broadcast address"
                              if str(e) == "host" else "must be an IPv4 address with prefix /30 or larger, e.g. 10.10.1.1/24")
    return p, err


# ------------------------------------------------------------------ conflicts (reported, never removed)
def _rows(db, table):
    return db.get(table, {}) or {}


def check(p: dict, db: dict):
    """-> (conflicts, blockers). Conflicts: existing settings that differ from the blueprint. Blockers: things that
    stop the blueprint from working at all. Nothing is changed or removed."""
    conflicts, blockers = [], []
    fabric = p["downlinks"] + p.get("uplinks", []) if p["role"] == "spine" else p.get("uplinks", [])
    servers = p["downlinks"] if p["role"] == "leaf" else []
    pc = {k.split("|")[1]: k.split("|")[0] for k in _rows(db, "PORTCHANNEL_MEMBER") if "|" in k}
    vm = {}
    for k in _rows(db, "VLAN_MEMBER"):
        if "|" in k:
            v, port = k.split("|", 1)
            vm.setdefault(port, []).append(v)
    ips = {}
    for k in _rows(db, "INTERFACE"):
        if "|" in k:
            port, ip = k.split("|", 1)
            ips.setdefault(port, []).append(ip)
    for port in p["downlinks"] + p.get("uplinks", []):
        if port in pc:
            conflicts.append(f"{port} is a member of {pc[port]}")
        vrf = (_rows(db, "INTERFACE").get(port) or {}).get("vrf_name")
        if vrf:
            conflicts.append(f"{port} is bound to VRF {vrf}")
    for port in fabric:
        if port in vm:
            conflicts.append(f"{port} (fabric link) is in {', '.join(vm[port])}")
        v4 = [ip for ip in ips.get(port, []) if ":" not in ip]
        if v4:
            conflicts.append(f"{port} (fabric link) has IP {', '.join(v4)}")
    for port in servers:
        other = [v for v in vm.get(port, []) if v != f"Vlan{p['vlan']}"]
        if other:
            conflicts.append(f"{port} (server port) is in {', '.join(other)}")
        if ips.get(port):
            conflicts.append(f"{port} (server port) has IP {', '.join(ips[port])}")
    lo_ips = [k.split("|", 1)[1] for k in _rows(db, "LOOPBACK_INTERFACE") if k.startswith("Loopback0|")]
    other_lo = [ip for ip in lo_ips if ip != f"{p['loopback']}/32"]
    if other_lo:
        conflicts.append(f"Loopback0 already has IP {', '.join(other_lo)}")
    if p["role"] == "leaf":
        vint = [k.split("|", 1)[1] for k in _rows(db, "VLAN_INTERFACE") if k.startswith(f"Vlan{p['vlan']}|")]
        other = [ip for ip in vint if ip != p["gateway"]]
        if other:
            conflicts.append(f"Vlan{p['vlan']} already has IP {', '.join(other)}")
    asns = re.findall(r"^router bgp (\d+)\s*$", db.get("_frr", ""), re.M)
    if asns and str(p["asn"]) not in asns:
        conflicts.append(f"BGP is already running with ASN {', '.join(asns)} (this blueprint uses {p['asn']})")
    mode = ((_rows(db, "DEVICE_METADATA").get("localhost") or {}).get("docker_routing_config_mode") or "")
    if mode not in PERSISTENT_MODES:
        blockers.append(f"FRR routing-config mode is '{mode or 'not set'}': BGP configured through vtysh would not "
                        "survive a reload in this mode. It needs 'split' (DEVICE_METADATA docker_routing_config_mode; "
                        "changing it restarts BGP), which Dial 0 doesn't change for you.")
    return conflicts, blockers


# ------------------------------------------------------------------ generating the configuration
def _vtysh(lines: list) -> dict:
    return {"tool": "vtysh", "argv": ["vtysh"] + [x for l in lines for x in ("-c", l)],
            "cmd": "vtysh " + " ".join(f'-c "{l}"' for l in lines)}


def _cfg(cmd: str, skip: str = "") -> dict:
    return {"tool": "config", "argv": cmd.split(), "cmd": cmd, "skip": skip}


def generate(name: str, p: dict, db: dict) -> list:
    """-> steps [{title, commands}]. Commands already satisfied by the running configuration are marked skip."""
    _bp(name)
    lo = p["loopback"]
    have_lo = "Loopback0" in _rows(db, "LOOPBACK_INTERFACE") or any(k.startswith("Loopback0|") for k in _rows(db, "LOOPBACK_INTERFACE"))
    have_lo_ip = f"Loopback0|{lo}/32" in _rows(db, "LOOPBACK_INTERFACE")
    steps = [{"title": "Loopback0", "commands": [
        _cfg("config loopback add Loopback0", "already exists" if have_lo else ""),
        _cfg(f"config interface ip add Loopback0 {lo}/32", "already set" if have_lo_ip else "")]}]

    def link_steps(ports, what, link_local):
        st = [{"title": f"Bring up {what}", "commands": [_cfg(f"config interface startup {x}") for x in ports]},
              {"title": f"MTU {MTU} on {what}", "commands": [_cfg(f"config interface mtu {x} {MTU}") for x in ports]}]
        if link_local:
            st.append({"title": f"IPv6 link-local on {what} (BGP unnumbered)",
                       "commands": [_cfg(f"config interface ipv6 enable use-link-local-only {x}") for x in ports]})
        return st

    if p["role"] == "spine":
        steps += link_steps(p["downlinks"], "downlinks", True)
        peers, group, extra = p["downlinks"], "LEAF", []
    else:
        v = p["vlan"]
        steps += link_steps(p["downlinks"], "downlinks (servers)", False)
        members = {k.split("|", 1)[1] for k in _rows(db, "VLAN_MEMBER") if k.startswith(f"Vlan{v}|")}
        steps.append({"title": f"Server VLAN {v}", "commands": [
            _cfg(f"config vlan add {v}", "already exists" if f"Vlan{v}" in _rows(db, "VLAN") else ""),
            _cfg(f"config interface ip add Vlan{v} {p['gateway']}",
                 "already set" if f"Vlan{v}|{p['gateway']}" in _rows(db, "VLAN_INTERFACE") else "")]
            + [_cfg(f"config vlan member add -u {v} {x}", "already a member" if x in members else "") for x in p["downlinks"]]})
        steps += link_steps(p["uplinks"], "uplinks", True)
        peers, group, extra = p["uplinks"], "SPINE", ["redistribute connected"]
    bgp = (["configure terminal", f"router bgp {p['asn']}", f"bgp router-id {lo}", "bgp bestpath as-path multipath-relax",
            "no bgp ebgp-requires-policy", f"neighbor {group} peer-group", f"neighbor {group} remote-as external"]
           + [f"neighbor {x} interface peer-group {group}" for x in peers]
           + ["address-family ipv4 unicast", f"network {lo}/32"] + extra
           + ["maximum-paths 64", "exit-address-family", "end", "write memory"])
    steps.append({"title": f"BGP (ASN {p['asn']}, peer-group {group})", "commands": [_vtysh(bgp)]})
    steps.append({"title": "Save the configuration", "commands": [_cfg("config save -y")]})
    return steps


def plan_id(steps: list) -> str:
    import hashlib
    return hashlib.sha256(json.dumps([[c["cmd"] for c in s["commands"]] for s in steps]).encode()).hexdigest()[:12]


def plan(name: str, raw: dict) -> dict:
    """Validate the answers against the switch, then generate the configuration and the conflict/blocker report.
    -> {errors} or {params, steps, conflicts, blockers, plan_id, commands}. Read-only."""
    db = snapshot()
    p, err = validate(name, raw, db)
    if err:
        return {"errors": err, "params": p}
    conflicts, blockers = check(p, db)
    steps = generate(name, p, db)
    return {"params": p, "steps": steps, "conflicts": conflicts, "blockers": blockers, "plan_id": plan_id(steps),
            "commands": sum(1 for s in steps for c in s["commands"] if not c.get("skip"))}


# ------------------------------------------------------------------ applying
def _run(argv, max_out=20000):
    return tools.split_result(tools._run(argv, max_out=max_out))


def _vtysh_error(out: str) -> str:
    bad = [l.strip() for l in out.splitlines() if l.strip().startswith("%")]
    return "; ".join(bad[:3])


def backup(step) -> dict:
    """Save the running CONFIG_DB and FRR configuration under BACKUP_DIR/<time>/ before anything is changed.
    -> {dir, restore commands}. The restore is only shown, never run."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    d = f"{BACKUP_DIR}/{ts}"
    q = shlex.quote
    script = (f"mkdir -p {q(d)} && sonic-cfggen -d --print-data > {q(d + '/config_db.json')} && "
              f"vtysh -c 'show running-config' > {q(d + '/frr-running.conf')}")
    code, out = _run(["sh", "-c", script])
    if code != 0:
        raise BlueprintError("couldn't back up the current configuration: " + (out.strip().splitlines() or ["?"])[-1])
    step(f"Backed up the running configuration to {d}")
    return {"dir": d, "restore": [f"sudo cp {d}/config_db.json /etc/sonic/config_db.json && sudo config reload -y",
                                  f"(FRR) compare with: sudo vtysh -c 'show running-config'  vs  {d}/frr-running.conf"]}


def bgp_status(peers: list) -> tuple:
    """Which of `peers` (unnumbered BGP neighbors, named by interface) are established.
    -> (established peers, {peer: state}, raw output)."""
    code, out = _run(["show", "ip", "bgp", "summary"], max_out=200000)
    up, state = [], {}
    for line in out.splitlines():
        f = line.split()
        if len(f) > 9 and f[0] in peers:
            state[f[0]] = f[9]
            if f[9].isdigit():
                up.append(f[0])
    return up, state, out


def apply(name: str, raw: dict, approved_plan: str, step=lambda m, d="": None) -> dict:
    """Re-plan against the CURRENT configuration; refuse if anything changed since it was shown, or if there are
    conflicts or blockers. Then backup -> commands in order (stop at the first real error) -> verify."""
    from .agent import already_done
    pl = plan(name, raw)
    if pl.get("errors"):
        return {"ok": False, "error": "invalid values: " + "; ".join(f"{k}: {v}" for k, v in pl["errors"].items())}
    if pl["plan_id"] != approved_plan:
        return {"ok": False, "error": "the switch's configuration changed since the plan was shown; review it again"}
    if pl["conflicts"] or pl["blockers"]:
        return {"ok": False, "error": "conflicts or blockers on this switch (nothing applied)",
                "conflicts": pl["conflicts"], "blockers": pl["blockers"]}
    p = pl["params"]
    save(name, dict(raw, **{k: (", ".join(v) if isinstance(v, list) else v) for k, v in p.items()}))
    bk = backup(step)
    done, skipped = 0, 0
    for s in pl["steps"]:
        step(f"Applying: {s['title']}")
        for c in s["commands"]:
            if c.get("skip"):
                skipped += 1
                continue
            code, out = _run(c["argv"], max_out=20000)
            verr = _vtysh_error(out) if c["tool"] == "vtysh" else ""
            if code == 0 and not verr:
                done += 1
                continue
            if c["tool"] == "config" and already_done(c["cmd"], out):
                step(f"  '{c['cmd']}': already in place; continuing")
                skipped += 1
                continue
            msg = verr or (out.strip().splitlines() or ["?"])[-1]
            step(f"  FAILED: {c['cmd'][:120]}: {msg}", out[-2000:])
            return {"ok": False, "error": f"stopped at '{c['cmd'][:200]}': {msg}", "applied": done, "skipped": skipped,
                    "backup": bk, "params": p}
    step(f"Applied {done} command(s) ({skipped} already in place). Verifying.")
    verify = {}
    if p["role"] == "leaf":
        code, out = _run(["show", "vlan", "brief"], max_out=100000)
        verify["vlan"] = f"Vlan{p['vlan']} present" if re.search(rf"\b{p['vlan']}\b", out) else f"Vlan{p['vlan']} NOT found"
    code, out = _run(["show", "ip", "interfaces"], max_out=100000)
    verify["loopback"] = "Loopback0 " + ("has " + p["loopback"] if p["loopback"] in out else "IP NOT found")
    peers = p["downlinks"] if p["role"] == "spine" else p["uplinks"]
    t0 = time.time()
    while True:
        up, state, raw_out = bgp_status(peers)
        if len(up) == len(peers) or time.time() - t0 >= BGP_WAIT_S:
            break
        time.sleep(5)
    down = [f"{x} ({state.get(x, 'not listed')})" for x in peers if x not in up]
    verify["bgp"] = f"{len(up)} of {len(peers)} BGP neighbors established" + (
        f"; not yet: {', '.join(down[:8])}. They come up once the other side is configured." if down else "")
    step("Verified: " + "; ".join(verify.values()), raw_out[-3000:])
    return {"ok": True, "applied": done, "skipped": skipped, "backup": bk, "verify": verify, "params": p}
