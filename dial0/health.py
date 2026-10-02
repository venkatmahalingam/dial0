"""Health check workflow: the switch's overall state from fixed, read-only checks plus the native grep over the
system log. No model call: every finding is read from the outputs by code, so the verdict is never made up.
Runs for health questions ("is the switch healthy?", "any errors?") and for `dial0 health`.

These checks are part of Dial 0 itself (like `dial0 ctl doctor`), not model proposals, so they are not limited to
the command reference file. A check that doesn't exist on this switch is reported as "not available"."""
import os, re, shlex
from . import tools

SYSLOG = os.getenv("DIAL0_SYSLOG", "/var/log/syslog")
LOG_LINES = int(os.getenv("DIAL0_HEALTH_LOG_LINES", "20000"))
OWN = os.getenv("DIAL0_CONTAINER_NAME", "dial0")
CORE = {"database", "swss", "syncd", "bgp", "teamd", "pmon", "lldp"}
ORDER = {"crit": 0, "warn": 1, "ok": 2, "info": 3}
LABEL = {"crit": "[CRIT]", "warn": "[warn]", "ok": "[ok]  ", "info": "[info]"}


def _run(argv):
    return tools.split_result(tools._run(argv, max_out=200000))


def _table(out):
    """SONiC tables: a header line, a dashed line, rows split on 2+ spaces."""
    lines = [l for l in out.splitlines() if l.strip()]
    for i in range(len(lines) - 1):
        if re.fullmatch(r"[\s-]+", lines[i + 1]) and "-" in lines[i + 1]:
            head = re.split(r"\s{2,}", lines[i].strip())
            rows = [re.split(r"\s{2,}", r.strip()) for r in lines[i + 2:] if not re.fullmatch(r"[\s-]+", r)]
            # rows with empty trailing columns (e.g. no SetOwner) are shorter than the header: pad them
            return [dict(zip(head, r + [""] * (len(head) - len(r)))) for r in rows if 0 < len(r) <= len(head)]
    return []


def system_health():
    """`show system-health summary`: LED colour, services and hardware status (with what's not running)."""
    code, out = _run(["show", "system-health", "summary"])
    if code != 0:
        return "info", "System health", "not available here ('show system-health summary' failed)", out
    led = (re.search(r"System status LED\s+(\S+)", out) or [None, "?"])[1]
    parts, level = [], "ok"
    for block in ("Services", "Hardware"):
        m = re.search(block + r":\s*\n\s*Status:\s*([^\n]+)((?:\n\s{4,}[^\n]+)*)", out)
        if m:
            st = m.group(1).strip()
            why = "; ".join(" ".join(x.split()) for x in m.group(2).splitlines() if x.strip())
            parts.append(f"{block.lower()} {st}" + (f" ({why[:200]})" if st.lower() != "ok" and why else ""))
            if st.lower() != "ok":
                level = "crit"
    if led.lower() in ("red",):
        level = "crit"
    elif led.lower() not in ("green", "?") and level == "ok":
        level = "warn"
    msg = f"LED {led}, " + ", ".join(parts)
    return level, "System health", msg, out


def containers():
    """Containers that aren't running, for enabled features only (disabled features aren't problems). Core SONiC
    containers down are critical, others are warnings."""
    code, out = _run(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Status}}"])
    if code != 0:
        return "info", "Containers", "not available here (docker ps failed)", out
    fcode, fout = _run(["show", "feature", "status"])
    enabled = {r.get("Feature"): r.get("State", "") for r in _table(fout)} if fcode == 0 else {}
    down_crit, down_warn, up = [], [], 0
    for line in out.splitlines():
        name, _, status = line.partition("|")
        if not name or name == OWN or name.startswith(OWN):
            continue
        if status.startswith("Up"):
            up += 1
            continue
        feat = re.sub(r"\d+$", "", name)
        state = enabled.get(name, enabled.get(feat, ""))
        if state and state.lower() not in ("enabled", "always_enabled"):
            continue  # feature disabled on purpose
        (down_crit if feat in CORE else down_warn).append(f"{name} ({status.strip() or 'not running'})")
    if down_crit:
        return "crit", "Containers", "not running: " + ", ".join(down_crit + down_warn), out
    if down_warn:
        return "warn", "Containers", "not running: " + ", ".join(down_warn), out
    return "ok", "Containers", f"{up} running", out


def interfaces():
    """Ports that are admin up but oper down (ports shut down on purpose are ignored)."""
    code, out = _run(["show", "interfaces", "status"])
    if code != 0:
        return "info", "Interfaces", "not available here", out
    rows = _table(out)
    enabled = [r for r in rows if r.get("Admin") == "up"]
    down = [r.get("Interface", "?") for r in enabled if r.get("Oper") != "up"]
    if not rows:
        return "info", "Interfaces", "couldn't read the table", out
    if down:
        return ("warn", "Interfaces", f"{len(down)} of {len(enabled)} enabled ports are down: " + ", ".join(down[:8])
                + ("..." if len(down) > 8 else ""), out)
    return "ok", "Interfaces", f"all {len(enabled)} enabled ports are up", out


def bgp():
    """BGP neighbors not established. Understands IP neighbors and unnumbered ones named by interface."""
    code, out = _run(["show", "ip", "bgp", "summary"])
    if code != 0 or "not running" in out.lower() or "No IPv4 neighbor" in out:
        return "info", "BGP", "no IPv4 BGP neighbors here", out
    rows = [l.split() for l in out.splitlines()  # IP neighbors, or unnumbered ones named by interface
            if re.match(r"^(\d{1,3}(\.\d{1,3}){3}|Ethernet\d+|PortChannel\d+)\s", l)]
    if not rows:
        return "info", "BGP", "no IPv4 BGP neighbors here", out
    # Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd [NeighborName]
    bad = [f"{r[0]} ({r[9]})" for r in rows if len(r) > 9 and not r[9].isdigit()]
    if bad:
        return "warn", "BGP", f"{len(bad)} of {len(rows)} neighbors not established: " + ", ".join(bad[:6]), out
    return "ok", "BGP", f"all {len(rows)} neighbors established", out


def resources():
    """Disk and memory use (>=85% warning, >=95% critical) and load above the CPU count."""
    findings, level, raw = [], "ok", ""
    code, out = _run(["df", "-P", "/"])
    raw += out + "\n"
    m = re.search(r"\s(\d+)%\s+/\s*$", out, re.M)
    if m:
        u = int(m.group(1)); findings.append(f"disk / {u}% used")
        level = "crit" if u >= 95 else "warn" if u >= 85 and level == "ok" else level
    code, out = _run(["free", "-m"])
    raw += out + "\n"
    m = re.search(r"^Mem:\s+(\d+)\s+\d+\s+\d+\s+\d+\s+\d+\s+(\d+)", out, re.M)
    if m and int(m.group(1)):
        u = round(100 * (int(m.group(1)) - int(m.group(2))) / int(m.group(1)))
        findings.append(f"memory {u}% used")
        level = "crit" if u >= 95 else ("warn" if u >= 85 and level == "ok" else level)
    code, out = _run(["cat", "/proc/loadavg"])
    ncode, nout = _run(["nproc"])
    raw += out + "\n"
    if code == 0 and out.split():
        load = float(out.split()[0]); findings.append(f"load {load}")
        if ncode == 0 and nout.strip().isdigit() and load > int(nout.strip()) and level == "ok":
            level = "warn"
    return (level, "Resources", ", ".join(findings) or "not available here", raw)


def logs():
    """Native grep over the recent system log: CRIT/ALERT/EMERG are critical, ERR a warning; the most frequent
    messages are grouped (numbers ignored) so repeats show as one line with a count."""
    script = (f"tail -n {int(LOG_LINES)} {shlex.quote(SYSLOG)} 2>/dev/null | grep -E ' (EMERG|ALERT|CRIT|ERR|WARNING) ' | tail -n 3000")
    code, out = _run(["sh", "-c", script])
    lines = [l for l in out.splitlines() if l.strip() and l.strip() != "(no output)"]
    sev = {"crit": [], "err": [], "warn": []}
    for l in lines:
        m = re.search(r" (EMERG|ALERT|CRIT|ERR|WARNING) (.*)$", l)
        if not m:
            continue
        key = "crit" if m.group(1) in ("EMERG", "ALERT", "CRIT") else "err" if m.group(1) == "ERR" else "warn"
        sev[key].append(m.group(2))
    def top(msgs, n=3):
        counts = {}
        for x in msgs:
            norm = re.sub(r"\b0x[0-9a-f]+\b|\b\d+\b", "#", x)[:140]
            counts[norm] = counts.get(norm, 0) + 1
        return "; ".join(f"(x{c}) {t}" for t, c in sorted(counts.items(), key=lambda kv: -kv[1])[:n])
    msg = f"last {LOG_LINES} lines of {SYSLOG}: {len(sev['crit'])} critical, {len(sev['err'])} errors, {len(sev['warn'])} warnings"
    if sev["crit"]:
        return ("crit", "Logs", msg + ". Most frequent critical: " + top(sev["crit"])
                + (". Most frequent errors: " + top(sev["err"], 2) if sev["err"] else ""), out)
    if sev["err"]:
        return "warn", "Logs", msg + ". Most frequent errors: " + top(sev["err"]), out
    return ("info" if sev["warn"] else "ok"), "Logs", msg, out


def cores():
    code, out = _run(["sh", "-c", "find /var/core -type f -mtime -7 2>/dev/null | head -20"])
    files = [l for l in out.splitlines() if l.strip() and l.strip() != "(no output)"]
    if files:
        return "warn", "Crashes", f"{len(files)} core dump(s) in the last 7 days: " + ", ".join(os.path.basename(f) for f in files[:5]), out
    return "ok", "Crashes", "no core dumps in the last 7 days", out


def _num(v) -> int:
    v = (v or "").replace(",", "").strip()
    return int(v) if v.isdigit() else 0


def _unavailable(code, out) -> bool:
    return (not out.strip() or out.strip() == "(no output)"
            or bool(re.search(r"(?i)no such command|not supported|not implemented|command not found|usage:", out) and code != 0))


def system_ready():
    """'show system status' must say "System is ready"; otherwise report the services/containers that aren't OK.
    Falls back to 'show system-health sysready-status' (the same report on releases that name it that way)."""
    code, out = _run(["show", "system", "status"])
    if "System is" not in out:
        code, out = _run(["show", "system-health", "sysready-status"])
    if "System is" not in out:
        return "info", "System ready", "not available here ('show system status' failed)", out
    if re.search(r"System is ready", out):
        return "ok", "System ready", "System is ready", out
    bad = []
    for r in _table(out):
        cols = list(r)
        name = r[cols[0]]
        states = [r[c] for c in cols if "status" in c.lower()]
        reason = next((r[c] for c in cols if "reason" in c.lower()), "")
        if any(x.strip().upper() not in ("OK", "") for x in states):
            bad.append(f"{name} ({'/'.join(x for x in states if x)}" + (f", {reason}" if reason and reason != "-" else "") + ")")
    head = (re.search(r"(System is not ready[^\n]*)", out) or [None, "System is not ready"])[1].strip()
    return "crit", "System ready", head + (": " + ", ".join(bad[:8]) if bad else ""), out


def ssd():
    """'show platform ssdhealth' must show no errors."""
    code, out = _run(["show", "platform", "ssdhealth"])
    if _unavailable(code, out):
        return "info", "SSD", "not available here ('show platform ssdhealth')", out
    errors = [l.strip() for l in out.splitlines()
              if re.search(r"(?i)\b(errors?|fail(ed|ure|s)?|critical|bad|unrecoverable)\b", l)
              and not re.search(r"[:=]\s*0+\s*$", l)]  # "Error count : 0" is fine
    h = re.search(r"(?i)health\s*:\s*([\d.]+)\s*%", out)
    t = re.search(r"(?i)temperature\s*:\s*([\d.]+\s*C)", out)
    info = ", ".join(x for x in ((f"health {h.group(1)}%" if h else ""), (f"temperature {t.group(1)}" if t else "")) if x)
    if code != 0 or errors:
        return "crit", "SSD", "errors reported: " + "; ".join(errors[:4] or [out.strip().splitlines()[-1]]), out
    return "ok", "SSD", "no errors" + (f" ({info})" if info else ""), out


COUNTER_COLS = ("RX_ERR", "RX_DRP", "RX_OVR", "TX_ERR", "TX_DRP", "TX_OVR")


def counters():
    """'show interfaces counters': RX_ERR, RX_DRP, RX_OVR (and TX_ERR, TX_DRP, TX_OVR) must be zero."""
    code, out = _run(["show", "interfaces", "counters"])
    rows = _table(out)
    if code != 0 or not rows:
        return "info", "Counters", "not available here ('show interfaces counters')", out
    bad = []
    for r in rows:
        nz = [f"{c}={r[c].strip()}" for c in COUNTER_COLS if c in r and _num(r[c])]
        if nz:
            bad.append(f"{r.get('IFACE', '?')} " + " ".join(nz))
    if bad:
        return ("warn", "Counters", f"{len(bad)} interface(s) with non-zero error/drop/overrun counters (since the last clear): "
                + "; ".join(bad[:8]) + ("..." if len(bad) > 8 else ""), out)
    return "ok", "Counters", f"RX/TX ERR, DRP and OVR are zero on all {len(rows)} interfaces", out


def fans():
    """'show platform fan': the Status column must be OK for every fan."""
    code, out = _run(["show", "platform", "fan"])
    rows = _table(out)
    if code != 0 or not rows:
        return "info", "Fans", "not available here ('show platform fan')" if not re.search(r"(?i)not detected", out) \
            else "no fans detected", out
    bad = []
    for r in rows:
        st = next((r[c] for c in r if c.lower() == "status"), "")
        if st.strip().upper() != "OK":
            name = r.get("FAN") or r.get("Fan") or r.get("Name") or next(iter(r.values()))
            bad.append(f"{r.get('Drawer', '')} {name} ({st or 'no status'})".strip())
    if bad:
        return "crit", "Fans", f"{len(bad)} of {len(rows)} fans not OK: " + ", ".join(bad[:8]), out
    return "ok", "Fans", f"all {len(rows)} fans OK", out


def transceivers():
    """'show interfaces transceiver summary': every port with a transceiver must have status Ready."""
    code, out = _run(["show", "interfaces", "transceiver", "summary"])
    rows = _table(out)
    if code != 0 or not rows:
        return "info", "Transceivers", "not available here ('show interfaces transceiver summary')", out
    cols = list(rows[0])
    st_col = next((c for c in cols if "status" in c.lower()), None)
    pr_col = next((c for c in cols if "presen" in c.lower()), None)
    if not st_col:
        return "info", "Transceivers", "couldn't find a status column (send a sample output to tune this check)", out
    absent = ("", "-", "n/a", "na", "not present", "absent", "empty")
    valid = [r for r in rows if (pr_col is None or "not" not in r[pr_col].lower() and r[pr_col].strip().lower() not in absent)
             and r[st_col].strip().lower() not in absent]
    bad = [f"{r[cols[0]]} ({r[st_col].strip()})" for r in valid if r[st_col].strip().lower() != "ready"]
    if bad:
        return "warn", "Transceivers", f"{len(bad)} of {len(valid)} present transceivers not Ready: " + ", ".join(bad[:8]), out
    return "ok", "Transceivers", f"all {len(valid)} present transceivers Ready", out


def log_excerpt(raw: str, n: int = 12) -> str:
    """The most frequent critical/error messages (and a few warnings) from the grep output, with counts: what the
    model gets to analyse, kept short."""
    groups = {}
    for l in raw.splitlines():
        m = re.search(r" (EMERG|ALERT|CRIT|ERR|WARNING) (.*)$", l)
        if not m:
            continue
        sev = "CRIT" if m.group(1) in ("EMERG", "ALERT", "CRIT") else m.group(1)
        key = (sev, re.sub(r"\b0x[0-9a-f]+\b|\b\d+\b", "#", m.group(2))[:160])
        g = groups.setdefault(key, [0, m.group(2)[:200]])
        g[0] += 1
    order = {"CRIT": 0, "ERR": 1, "WARNING": 2}
    top = sorted(groups.items(), key=lambda kv: (order[kv[0][0]], -kv[1][0]))
    lines = [f"{sev} (x{c}) {example}" for (sev, _), (c, example) in top if sev != "WARNING"][:n]
    lines += [f"WARNING (x{c}) {example}" for (sev, _), (c, example) in top if sev == "WARNING"][:3]
    return "\n".join(lines)


def version():
    code, out = _run(["show", "version"])
    v = (re.search(r"SONiC Software Version:\s*(\S+)", out) or [None, ""])[1]
    up = (re.search(r"Uptime:\s*([^\n]+)", out) or [None, ""])[1]
    return v, up.strip()


CHECKS = [system_ready, system_health, containers, ssd, fans, transceivers, interfaces, counters, bgp, resources,
          logs, cores]


def run(step=lambda msg, detail="": None, checks=None, title="Switch health", raws=None):
    """-> (report text, overall level, findings). step(msg, detail) reports progress.
    checks: a subset of CHECKS (the workflows `logs`, `interfaces`... run one or two of them)."""
    findings = []
    for chk in (checks or CHECKS):
        try:
            level, name, msg, raw = chk()
        except Exception as e:  # one broken check must not hide the others
            level, name, msg, raw = "info", chk.__name__, f"check failed: {e}", ""
        findings.append((level, name, msg))
        if raws is not None:
            raws[name] = raw
        step(f"Health: {name}: {LABEL[level].strip()} {msg}", raw[-3000:])
    v, up = version()
    n_crit = sum(1 for f in findings if f[0] == "crit")
    n_warn = sum(1 for f in findings if f[0] == "warn")
    overall = "CRITICAL" if n_crit else "WARNING" if n_warn else "HEALTHY"
    head = f"{title}: {overall}" + (f" ({n_crit} critical, {n_warn} warning(s))" if n_crit or n_warn else "") + \
           (f"   [{v}" + (f", up {up}" if up else "") + "]" if v else "")
    body = [f"  {LABEL[l]} {t}: {m}" for l, t, m in sorted(findings, key=lambda f: ORDER[f[0]])]
    return "\n".join([head] + body + ["Raw outputs: dial0 why"]), overall, findings
