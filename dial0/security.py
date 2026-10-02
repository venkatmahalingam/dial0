"""Security audit workflow: the switch's configuration and exposure (read-only), followed by the CVE scan.
Findings are made by code. The password check runs entirely on the switch: password hashes are never shown,
stored, kept as raw output, or given to the model."""
import datetime, os, re, shlex
from . import tools, cve
from .health import _table, LABEL, ORDER

DEFAULT_PASSWORDS = {"admin": "YourPaSsWoRd"}  # SONiC's well-known default
AUTH_LOG = os.getenv("DIAL0_AUTH_LOG", "/var/log/auth.log")
WW_PATHS = os.getenv("DIAL0_WW_PATHS", "/etc /usr/local/bin")
API_PORT = os.getenv("DIAL0_API_PORT", "8090")
BRUTE_MIN = 20


def _run(argv):
    return tools.split_result(tools._run(argv, max_out=400000))


def _hash_is(password: str, h: str) -> bool:
    """Does this crypt(3) hash belong to `password`? Computed on the switch; nothing leaves it."""
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import crypt
        return crypt.crypt(password, h) == h
    except Exception:
        m = re.match(r"^\$(1|5|6)\$(?:rounds=\d+\$)?([^$]+)\$", h)
        if not m:
            return False
        code, out = _run(["openssl", "passwd", "-" + m.group(1), "-salt", m.group(2), password])
        return code == 0 and out.strip() == h


def passwords():
    """Accounts with SONiC's default password or no password. Hashes are compared on the switch and never shown,
    kept, or given to the model."""
    code, out = _run(["getent", "shadow"])
    if code != 0:
        return "info", "Passwords", "couldn't read the account database", "(not shown)"
    default, empty = [], []
    for line in out.splitlines():
        f = line.split(":")
        if len(f) < 2:
            continue
        user, h = f[0], f[1]
        if h == "":
            empty.append(user)
        elif user in DEFAULT_PASSWORDS and h[:1] not in ("!", "*") and _hash_is(DEFAULT_PASSWORDS[user], h):
            default.append(user)
    raw = "(password hashes are never shown or kept)"
    if default or empty:
        msg = "; ".join(x for x in (f"default SONiC password still set for: {', '.join(default)}" if default else "",
                                     f"no password at all: {', '.join(empty)}" if empty else "") if x)
        return "crit", "Passwords", msg, raw
    return "ok", "Passwords", "no default or empty passwords", raw


def accounts():
    """Extra UID-0 accounts and passwordless sudo rules."""
    code, out = _run(["getent", "passwd"])
    if code != 0:
        return "info", "Accounts", "couldn't read /etc/passwd", out
    root_like, logins = [], []
    for line in out.splitlines():
        f = line.split(":")
        if len(f) < 7:
            continue
        if f[2] == "0" and f[0] != "root":
            root_like.append(f[0])
        if not re.search(r"(nologin|false|sync|shutdown|halt)$", f[6]) and f[0] != "root":
            logins.append(f[0])
    c2, sud = _run(["sh", "-c", "grep -rhsE '^[^#]*NOPASSWD' /etc/sudoers /etc/sudoers.d 2>/dev/null"])
    nopass = [l.strip() for l in sud.splitlines() if l.strip() and l.strip() != "(no output)"]
    issues, level = [], "ok"
    if root_like:
        issues.append(f"accounts with root privileges (UID 0) besides root: {', '.join(root_like)}"); level = "crit"
    if nopass:
        issues.append(f"sudo without a password: {'; '.join(nopass[:3])}"); level = "warn" if level == "ok" else level
    if not issues:
        return "ok", "Accounts", f"login accounts: {', '.join(logins[:8]) or 'none besides root'}; no extra root accounts", out
    return level, "Accounts", "; ".join(issues) + f" (login accounts: {', '.join(logins[:8])})", out


def ssh():
    """Effective SSH server settings (`sshd -T`): root login, empty passwords, weak ciphers/MACs/key exchange, X11."""
    code, out = _run(["sshd", "-T"])
    if code != 0 or "permitrootlogin" not in out.lower():
        return "info", "SSH", "couldn't read the SSH server settings (sshd -T)", out
    cfg = {}
    for line in out.lower().splitlines():
        k, _, v = line.partition(" ")
        cfg[k] = v.strip()
    crit, warn, note = [], [], []
    if cfg.get("permitrootlogin") == "yes":
        crit.append("root can log in with a password (PermitRootLogin yes)")
    if cfg.get("permitemptypasswords") == "yes":
        crit.append("empty passwords allowed (PermitEmptyPasswords yes)")
    weak_c = [c for c in cfg.get("ciphers", "").split(",") if re.search(r"cbc|3des|arcfour|blowfish", c)]
    weak_m = [m for m in cfg.get("macs", "").split(",") if re.search(r"md5|-96|^hmac-sha1(-etm@openssh\.com)?$|umac-64", m)]
    weak_k = [k for k in cfg.get("kexalgorithms", "").split(",") if re.search(r"sha1|group1-|group-exchange-sha1", k)]
    if weak_c:
        warn.append(f"weak ciphers: {', '.join(weak_c)}")
    if weak_m:
        warn.append(f"weak MACs: {', '.join(weak_m)}")
    if weak_k:
        warn.append(f"weak key exchange: {', '.join(weak_k)}")
    if cfg.get("x11forwarding") == "yes":
        warn.append("X11 forwarding on")
    if cfg.get("maxauthtries", "6").isdigit() and int(cfg.get("maxauthtries", "6")) > 6:
        note.append(f"MaxAuthTries {cfg['maxauthtries']}")
    if cfg.get("passwordauthentication") == "yes":
        note.append("password logins allowed; keys only is stronger")
    level = "crit" if crit else "warn" if warn else "ok"
    msg = "; ".join(crit + warn) or "no weak settings"
    return level, "SSH", msg + (f". Also: {'; '.join(note)}" if note else ""), out


RISKY_PORTS = {23: ("crit", "telnet"), 21: ("warn", "ftp"), 69: ("warn", "tftp"), 80: ("warn", "plain http"),
               512: ("crit", "rexec"), 513: ("crit", "rlogin"), 514: ("warn", "rsh/syslog")}


def services():
    """Ports listening beyond localhost; insecure services (telnet, rlogin, ftp, tftp, plain http) flagged."""
    code, out = _run(["ss", "-tulpnH"])
    if code != 0:
        return "info", "Exposed services", "couldn't list listening ports (ss)", out
    exposed, level, risky = [], "ok", []
    for line in out.splitlines():
        p = line.split()
        if len(p) < 5:
            continue
        local = p[4]
        addr, _, port = local.rpartition(":")
        if not port.isdigit() or addr.strip("[]").startswith(("127.", "::1")) or addr in ("localhost",):
            continue
        proc = (re.search(r'\(\("([^"]+)"', line) or [None, "?"])[1]
        entry = f"{p[0]}/{port} {proc}"
        if entry not in exposed:
            exposed.append(entry)
        if int(port) in RISKY_PORTS and not (p[0] == "udp" and int(port) == 514):
            lvl, name = RISKY_PORTS[int(port)]
            risky.append(f"{name} ({p[0]}/{port}, {proc})")
            level = "crit" if lvl == "crit" else ("warn" if level == "ok" else level)
    if risky:
        return level, "Exposed services", f"insecure services listening: {', '.join(risky)}; all exposed: {', '.join(exposed[:12])}", out
    return "ok", "Exposed services", f"{len(exposed)} listening beyond localhost: {', '.join(exposed[:12])}", out


def _keys(pattern):
    code, out = _run(["sonic-db-cli", "CONFIG_DB", "keys", pattern])
    return code, [l.split("|", 1)[1] for l in out.splitlines() if "|" in l]


def snmp():
    code, comms = _keys("SNMP_COMMUNITY|*")
    if code != 0:
        return "info", "SNMP", "couldn't read SNMP settings", ""
    default = [c for c in comms if c.lower() in ("public", "private", "community", "snmp")]
    if default:
        return "warn", "SNMP", f"guessable community name(s): {', '.join(default)} (use unique names, or SNMPv3)", "\n".join(comms)
    return "ok", "SNMP", f"{len(comms)} community(ies), none of the well-known names" if comms else "no SNMP communities", "\n".join(comms)


def mgmt_acl():
    """Whether a control-plane ACL limits who can reach SSH/SNMP."""
    code, out = _run(["show", "acl", "table"])
    if code != 0:
        return "info", "Management ACL", "couldn't read ACL tables", out
    ctrl = [r for r in _table(out) if "CTRLPLANE" in " ".join(r.values()).upper()]
    if not ctrl:
        return "warn", "Management ACL", "no control-plane ACL: SSH/SNMP accept connections from any address", out
    return "ok", "Management ACL", "control-plane ACL(s): " + ", ".join(
        f"{r.get('Name', '?')} ({r.get('Binding', r.get('Services', '')).strip()})" for r in ctrl[:4]), out


def aaa_logging():
    """Central authentication (TACACS+/RADIUS), remote syslog and NTP."""
    issues, level, raw = [], "ok", ""
    code, out = _run(["show", "aaa"])
    raw += out + "\n"
    if code == 0 and re.search(r"(?i)login\s+local", out) and not re.search(r"(?i)tacacs|radius", out.split("login", 1)[-1][:60]):
        issues.append("logins are checked against local accounts only (TACACS+/RADIUS add central control and accounting)")
    c1, syslog = _keys("SYSLOG_SERVER|*")
    c2, ntp = _keys("NTP_SERVER|*")
    if c1 == 0 and not syslog:
        issues.append("no remote syslog server: logs stay on the switch only"); level = "warn"
    if c2 == 0 and not ntp:
        issues.append("no NTP server: log times may be unreliable"); level = "warn"
    if not issues:
        return "ok", "AAA and logging", f"remote syslog: {', '.join(syslog[:3])}; NTP: {', '.join(ntp[:3])}", raw
    return (level if level != "ok" else "info"), "AAA and logging", "; ".join(issues), raw


def brute_force():
    """Failed logins in the auth log, grouped by source; 20+ from one source is flagged."""
    code, out = _run(["sh", "-c", f"cat {shlex.quote(AUTH_LOG)} {shlex.quote(AUTH_LOG + '.1')} 2>/dev/null | grep -E 'Failed password|Invalid user|authentication failure' | tail -n 20000"])
    lines = [l for l in out.splitlines() if l.strip() and l.strip() != "(no output)"]
    if not lines:
        return "ok", "Login attempts", "no failed logins in the auth log", ""
    by_ip = {}
    for l in lines:
        m = re.search(r"from (\S+)|rhost=(\S+)", l)
        ip = (m.group(1) or m.group(2)) if m else "?"
        by_ip[ip] = by_ip.get(ip, 0) + 1
    top = sorted(by_ip.items(), key=lambda kv: -kv[1])
    heavy = [f"{ip} (x{c})" for ip, c in top if c >= BRUTE_MIN]
    if heavy:
        return "warn", "Login attempts", f"{len(lines)} failed logins; repeated from: {', '.join(heavy[:5])}", "\n".join(lines[-50:])
    return "info", "Login attempts", f"{len(lines)} failed login(s), none repeated {BRUTE_MIN}+ times from one source", "\n".join(lines[-20:])


def image_age():
    """SONiC image build date; over a year old is flagged (check SONiC's advisories for a newer image)."""
    code, out = _run(["show", "version"])
    v = (re.search(r"SONiC Software Version:\s*(\S+)", out) or [None, "?"])[1]
    k = (re.search(r"Kernel:\s*(\S+)", out) or [None, ""])[1]
    m = re.search(r"Build date:\s*\w{3}\s+(\w{3})\s+(\d+)\s+[\d:]+\s+\w+\s+(\d{4})", out)
    if not m:
        return "info", "Software", f"{v}" + (f", kernel {k}" if k else "") + " (build date not found)", out
    built = datetime.datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%b %d %Y")
    days = (datetime.datetime.now() - built).days
    msg = f"{v}, built {built:%Y-%m-%d} ({days} days ago)" + (f", kernel {k}" if k else "")
    if days > 365:
        return "warn", "Software", msg + ": over a year old; check SONiC's advisories for a newer image", out
    return "ok", "Software", msg, out


def file_perms():
    paths = " ".join(shlex.quote(x) for x in WW_PATHS.split())
    code, out = _run(["sh", "-c", f"find {paths} -xdev -type f -perm -0002 2>/dev/null | head -20"])
    files = [l for l in out.splitlines() if l.strip() and l.strip() != "(no output)"]
    if files:
        return "warn", "File permissions", f"{len(files)} world-writable file(s): {', '.join(files[:6])}", out
    return "ok", "File permissions", f"no world-writable files in {WW_PATHS}", out


def dial0_itself():
    code, out = _run(["ss", "-tlnH"])
    api = [l for l in out.splitlines() if re.search(r":" + API_PORT + r"\s", l + " ")]
    if api and not all(re.search(r"127\.0\.0\.1:|\[::1\]:", l) for l in api):
        return "warn", "Dial 0 itself", f"Dial 0's API (port {API_PORT}) is reachable from the network", out
    return "ok", "Dial 0 itself", f"Dial 0's API listens on 127.0.0.1 only", out


AUDIT = [passwords, accounts, ssh, services, snmp, mgmt_acl, aaa_logging, brute_force, image_age, file_perms, dial0_itself]


def run(step=lambda m, d="": None):
    """-> (report, overall, findings, extra): the configuration audit, then the CVE scan."""
    audit = []
    for chk in AUDIT:
        try:
            level, name, msg, raw = chk()
        except Exception as e:
            level, name, msg, raw = "info", chk.__name__, f"check failed: {e}", ""
        audit.append((level, name, msg))
        step(f"Security: {name}: {LABEL[level].strip()} {msg}", raw[-3000:])
    c_report, c_overall, c_findings, c_extra = cve.run_scan(step)
    findings = audit + [(l, "CVE: " + t, m) for l, t, m in c_findings]
    n_crit = sum(1 for f in findings if f[0] == "crit")
    n_warn = sum(1 for f in findings if f[0] == "warn")
    overall = "CRITICAL" if n_crit else "WARNING" if n_warn else "HEALTHY"
    head = f"Security audit: {overall}" + (f" ({n_crit} critical, {n_warn} warning(s))" if n_crit or n_warn else "")
    body = ["Configuration:"] + [f"  {LABEL[l]} {t}: {m}" for l, t, m in sorted(audit, key=lambda f: ORDER[f[0]])]
    body += ["Vulnerabilities:"] + ["  " + line for line in c_report.splitlines()]
    issues = [f"{l.upper()} {t}: {m}" for l, t, m in sorted(audit, key=lambda f: ORDER[f[0]]) if l in ("crit", "warn")]
    excerpt = "CONFIGURATION ISSUES:\n" + ("\n".join(issues) or "none") + "\nCVE DETAILS:\n" + c_extra.get("excerpt", "")
    return "\n".join([head] + body), overall, findings, {"items": c_extra.get("items", []), "excerpt": excerpt}
