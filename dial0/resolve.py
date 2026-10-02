"""Ways to get the commands WITHOUT a model call (each model call costs minutes on one CPU core).

1. intents : common read-only questions ("show me the vlans") -> a known show command, if it exists in the reference.
2. plans   : a request you already approved (or one that differs only in values: VLAN id, IP, interface) reuses
             the earlier commands. Only plans that ran successfully are learned; a plan you declined is forgotten.
3. fix     : deterministic repair of a proposed command (SONiC interface names, unique typo in a subcommand,
             Linux `ip` changes -> their SONiC `config interface` equivalent).
"""
import difflib, json, os, re, threading, time
from . import clidoc

STATE = os.getenv("DIAL0_STATE_DIR", "/var/lib/dial0")
PLANS_PATH = os.path.join(STATE, "plans.json")
MAX_PLANS = 300

_WORD = re.compile(r"[a-z0-9]+")
CHANGE_WORDS = {"add", "create", "set", "configure", "config", "delete", "remove", "del", "enable", "disable",
                "shutdown", "shut", "startup", "bring", "change", "assign", "apply", "reload", "save", "clear",
                "reset", "restart", "rename", "attach", "detach", "bind", "unbind", "put", "make", "turn", "move"}

# (words that must all appear, command). Most specific (most words) wins; a tie means "ask the model".
INTENTS = [
    ({"vlan"}, "show vlan brief"),
    ({"interface", "status"}, "show interfaces status"),
    ({"interface", "counter"}, "show interfaces counters"),
    ({"interface", "description"}, "show interfaces description"),
    ({"transceiver"}, "show interfaces transceiver presence"),
    ({"portchannel"}, "show interfaces portchannel"),
    ({"ip", "interface"}, "show ip interfaces"),
    ({"ipv6", "interface"}, "show ipv6 interfaces"),
    ({"route"}, "show ip route"),
    ({"ipv6", "route"}, "show ipv6 route"),
    ({"bgp"}, "show ip bgp summary"),
    ({"bgp", "neighbor"}, "show ip bgp neighbors"),
    ({"lldp"}, "show lldp table"),
    ({"lldp", "neighbor"}, "show lldp neighbors"),
    ({"mac"}, "show mac"),
    ({"arp"}, "show arp"),
    ({"ndp"}, "show ndp"),
    ({"version"}, "show version"),
    ({"uptime"}, "show uptime"),
    ({"running", "config"}, "show runningconfiguration all"),
    ({"ntp"}, "show ntp"),
    ({"acl", "table"}, "show acl table"),
    ({"acl", "rule"}, "show acl rule"),
    ({"feature"}, "show feature status"),
    ({"platform"}, "show platform summary"),
    ({"fan"}, "show platform fan"),
    ({"psu"}, "show platform psustatus"),
    ({"temperature"}, "show platform temperature"),
    ({"loopback"}, "show ip interfaces"),
]
_NORM = {"vlans": "vlan", "interfaces": "interface", "ports": "interface", "port": "interface", "links": "interface",
         "link": "interface", "counters": "counter", "routes": "route", "routing": "route", "neighbors": "neighbor",
         "neighbours": "neighbor", "neighbour": "neighbor", "portchannels": "portchannel", "lag": "portchannel",
         "lags": "portchannel", "macs": "mac", "fans": "fan", "psus": "psu", "temp": "temperature",
         "temperatures": "temperature", "running": "running", "configuration": "config", "transceivers": "transceiver",
         "optics": "transceiver", "sfp": "transceiver", "sfps": "transceiver", "acls": "acl", "rules": "rule",
         "tables": "table", "features": "feature", "loopbacks": "loopback", "descriptions": "description"}

# values in a request: IPv4 (+prefix), MAC, interface names, plain numbers (in this order of priority)
_VALUE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b|\b(?:[0-9a-f]{2}:){5}[0-9a-f]{2}\b"
                    r"|\b(?:ethernet|vlan|portchannel|loopback)\s*[./_-]?\s*\d+\b|\b\d+\b", re.I)
_IFACE = re.compile(r"^(ethernet|vlan|portchannel|loopback)\s*[./_-]?\s*(\d+)$", re.I)
_CANON = {"ethernet": "Ethernet", "vlan": "Vlan", "portchannel": "PortChannel", "loopback": "Loopback"}


def words(text: str) -> set:
    return {_NORM.get(w, w) for w in _WORD.findall(text.lower())}


def canon_iface(tok: str):
    m = _IFACE.match(tok)
    return f"{_CANON[m.group(1).lower()]}{m.group(2)}" if m else None


def values(text: str) -> list[str]:
    out = []
    for m in _VALUE.finditer(text):
        v = m.group(0)
        out.append(canon_iface(v) or v)
    return out


def _exists(cmd: str) -> bool:
    """True if cmd is a leaf command in the reference (source or device) and passes validation.
    With a curated command reference: only if it is one of its commands."""
    toks = cmd.split()
    if clidoc.curated():
        return clidoc.curated_path(toks) == cmd
    for k in range(len(toks), 1, -1):
        p = " ".join(toks[:k])
        n = clidoc._static.get(p) or clidoc._live.get(p)
        if n is not None:
            return (not n.get("children") or n.get("runnable", False)) and clidoc.validate(toks) is None
    return False


# ------------------------------------------------------------------ 1. intents
# Status words that, with one interface named ("is Ethernet4 up?"), mean: show that interface's status.
IFACE_STATUS_WORDS = {"up", "down", "status", "state", "oper", "admin", "link", "operational", "running"}
_OPT_IFACE_ARG = re.compile(r"\[\s*(?:-i\s+)?<?\s*interface[_ ]?name\s*>?\s*\]|\[\s*interface\s*\]", re.I)


def _takes_interface(cmd: str) -> bool:
    d = clidoc._curated.get(cmd)
    if d:  # the reference's own examples show whether an interface follows
        return any(re.match(r"^(Ethernet|PortChannel|Vlan|Loopback)\d", (e[len(cmd):].split() or [""])[0]) for e in d["examples"])
    n = clidoc._static.get(cmd) or {}
    return bool(_OPT_IFACE_ARG.search(n.get("usage", "")))


def intent(text: str):
    """-> (command, reason) for a plain read-only question, or None (then the model decides).
    One interface may be named if the command takes one: "is Ethernet4 up?" -> show interfaces status Ethernet4."""
    vals = values(text)
    ifaces = [v for v in vals if canon_iface(v) and not v.lower().startswith("vlan")]
    if len(vals) != len(ifaces) or len(ifaces) > 1:
        return None  # IPs, numbers, several interfaces...: the model decides
    iface = ifaces[0] if ifaces else None
    stripped = _VALUE.sub(" ", text)
    w = words(stripped)
    if w & CHANGE_WORDS:
        return None
    if iface and w & IFACE_STATUS_WORDS and _exists("show interfaces status"):
        hits = [(99, "show interfaces status", {"interface"} | (w & IFACE_STATUS_WORDS))]
    else:
        if iface:
            w = w | {"interface"}
        hits = [(len(req), cmd, req) for req, cmd in INTENTS if req <= w and _exists(cmd)]
    if not hits:
        return None
    hits.sort(key=lambda h: -h[0])
    if len(hits) > 1 and hits[0][0] == hits[1][0] and hits[0][1] != hits[1][1]:
        return None  # ambiguous: let the model decide
    cmd = hits[0][1]
    if iface:
        if not _takes_interface(cmd):
            return None
        cmd = f"{cmd} {iface}"
    return cmd, "matched the words " + ", ".join(sorted(hits[0][2])) + (f" for {iface}" if iface else "")


# ------------------------------------------------------------------ 1a. answers read straight from the output
def _table_rows(output: str):
    """Parse a SONiC tabular 'show' output (header, dashed line, rows; columns separated by 2+ spaces)."""
    lines = [l for l in output.splitlines() if l.strip()]
    for i, l in enumerate(lines[:-1]):
        if re.match(r"^[\s-]+$", lines[i + 1]) and "-" in lines[i + 1]:
            head = re.split(r"\s{2,}", l.strip())
            return head, [re.split(r"\s{2,}", r.strip()) for r in lines[i + 2:] if not re.match(r"^[\s-]+$", r)]
    return None, []


def answer_from_output(request: str, results: list):
    """A direct answer for simple status questions, read from the command output (no model). None if unsure."""
    for r in results:
        if not r["command"].startswith("show interfaces status") or r["exit"] != 0:
            continue
        head, rows = _table_rows(r["output"])
        if not head or "Oper" not in head:
            return None
        ifaces = [v for v in values(request) if canon_iface(v)]
        rows = [dict(zip(head, row)) for row in rows if len(row) == len(head)]
        if ifaces:
            rows = [x for x in rows if x.get("Interface") == ifaces[0]]
        if len(rows) != 1:
            return None
        x = rows[0]
        name, oper, admin = x.get("Interface", "?"), x.get("Oper", "?"), x.get("Admin", "?")
        extra = ", ".join(f"{k.lower()} {x[k]}" for k in ("Speed", "MTU") if x.get(k) and x[k] != "N/A")
        if oper == "up":
            return f"{name} is up (oper up, admin {admin}{', ' + extra if extra else ''})."
        if admin == "down":
            return f"{name} is down: it is administratively shut down (admin down). 'config interface startup {name}' brings it up."
        return f"{name} is down (oper {oper}) although it is enabled (admin {admin}): check the cable, optic or the other side."
    return None


# ------------------------------------------------------------------ 1b. questions about the command reference itself
_META_NOUN = {"command", "commands", "cmd", "cmds", "cli", "syntax", "reference"}
_META_VERB = {"search", "find", "look", "lookup", "list", "which", "what", "show", "give", "tell", "correct", "right",
              "proper", "available", "suggest", "exact"}
_META_FILLER = _META_NOUN | _META_VERB | set(
    "for the a an to from of sonic me please up is are do i use can all that this it there in on with should would "
    "help out my exist exists".split())


def meta_lookup(text: str):
    """'search for the correct command', 'which command adds an ip to a vlan?' -> the words to look up
    ('' = nothing specific: use the previous request). None if the request is not about the reference."""
    w = re.findall(r"[a-z0-9./:-]+", text.lower())
    if not (set(w) & _META_NOUN and set(w) & _META_VERB):
        return None
    return " ".join(x for x in w if x not in _META_FILLER)


# ------------------------------------------------------------------ 1c. health questions -> the health check workflow
_HEALTH_STRONG = re.compile(r"(?i)\b(health|healthy|unhealthy|healthcheck|health-check)\b|\b(system|switch|overall|device)\s+"
                            r"(status|state|condition)\b|\b(everything|all)\s+(ok|okay|fine|good|alright)\b|"
                            r"\banything\s+(wrong|broken|bad)\b|\bhow\s+is\s+the\s+(switch|system|box|device)\b")
_HEALTH_WEAK = re.compile(r"(?i)\b(errors?|warnings?|critical|alarms?|problems?|issues?|logs?|syslog|crash(es)?|"
                          r"core\s*dumps?|cores|failures?|faults?)\b")
_SPECIFIC = re.compile(r"(?i)\b(interfaces?|ports?|ethernet\d*|vlans?|bgp|mac|routes?|counters?|portchannels?|lags?|"
                       r"transceivers?|lldp|arp|ndp|vrfs?|pktdrops?|fec)\b")


_CVE = re.compile(r"(?i)\b(cves?|vulnerabilit(y|ies)|vulnerable|exploits?|exploited|security\s+(holes?|advisor(y|ies))|"
                  r"patch(es|ed)?\s+(level|status)|unpatched)\b")


_SECURITY = re.compile(r"(?i)\b(security\s+(audit|scan|check|review|posture|report|status)|secure|hardening|harden(ed)?|"
                       r"audit)\b|\bsecurity\b")


def security_request(text: str) -> bool:
    """'run a security audit', 'is the switch secure?', 'security check' -> the security workflow (audit + CVE scan).
    Questions only about vulnerabilities/CVEs go to the CVE scan."""
    if words(_VALUE.sub(" ", text)) & CHANGE_WORDS:
        return False
    return bool(_SECURITY.search(text)) and not re.search(r"(?i)\b(cves?|vulnerabilit|exploit)", text)


def blueprint_request(text: str) -> bool:
    """'apply the clos blueprint', 'configure this switch from a template' -> how to run a blueprint."""
    return bool(re.search(r"(?i)\b(blueprints?|templates?)\b", text))


def cve_request(text: str) -> bool:
    """'any vulnerabilities?', 'scan for CVEs', 'is the switch vulnerable?' -> the CVE scan workflow."""
    return bool(_CVE.search(text)) and not (words(_VALUE.sub(" ", text)) & CHANGE_WORDS)


def health_request(text: str) -> bool:
    """'is the switch healthy?', 'any errors or warnings?', 'check the system status' -> True.
    'show interface counters errors' (a specific command) and anything that changes config -> False."""
    w = words(_VALUE.sub(" ", text))
    if w & CHANGE_WORDS:
        return False
    if _HEALTH_STRONG.search(text):
        return True
    return bool(_HEALTH_WEAK.search(text)) and not _SPECIFIC.search(text)


# ------------------------------------------------------------------ 2. learned plans
_lock = threading.Lock()


def _norm_text(text: str) -> str:
    return " ".join(text.lower().split()).rstrip(" .?!")


def _template(text: str):
    """'Configure ip 1.1.1.1/24 on vlan 20' -> ('configure ip {0} on {1}', ['1.1.1.1/24', 'Vlan20'])"""
    vals, parts, last = [], [], 0
    t = _norm_text(text)
    for m in _VALUE.finditer(t):
        parts.append(t[last:m.start()]); parts.append("{%d}" % len(vals))
        vals.append(canon_iface(m.group(0)) or m.group(0)); last = m.end()
    parts.append(t[last:])
    return "".join(parts), vals


def _load() -> dict:
    try:
        with open(PLANS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: dict):
    try:
        os.makedirs(STATE, exist_ok=True)
        items = sorted(d.items(), key=lambda kv: -kv[1].get("used", 0))[:MAX_PLANS]
        tmp = PLANS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(dict(items), f)
        os.replace(tmp, PLANS_PATH)
    except OSError:
        pass


def _ref_id() -> str:
    return (clidoc._meta.get("commit") or clidoc._meta.get("ref") or "")[:12]


def _num(v: str) -> str:
    m = _IFACE.match(v)
    return m.group(2) if m else v


def _abstract(cmd: str, vals: list[str]):
    """Replace request values in a command by placeholders: {i} = the value, {i#} = its number (Vlan20 -> 20).
    None if a value's digits still appear somewhere we can't account for (e.g. Vlan0020): then the plan is
    only reused for the identical request, never with other values."""
    out = []
    for tok in cmd.split():
        new = tok
        for i, v in enumerate(vals):
            m = _IFACE.match(tok)
            if tok == v:
                new = "{%d}" % i
            elif canon_iface(v) and tok == _num(v):
                new = "{%d#}" % i
            elif not canon_iface(v) and m and m.group(2) == v:
                new = _CANON[m.group(1).lower()] + "{%d}" % i
            else:
                continue
            break
        out.append(new)
    res = " ".join(out)
    left = re.sub(r"\{\d+#?\}", " ", res)
    for v in vals:
        core = v.split("/")[0]
        if core in left or _num(v) in re.findall(r"\d+", left) or any(_num(v) in run for run in re.findall(r"\d+", left)):
            return None
    return res


def _fill(tmpl: str, vals: list[str]) -> str:
    def sub(m):
        i = int(m.group(1))
        return _num(vals[i]) if m.group(2) else vals[i]
    return re.sub(r"\{(\d+)(#?)\}", sub, tmpl)


def remember(text: str, commands: list[str]):
    """Called after the commands ran successfully (and, for changes, after you approved them)."""
    key, vals = _template(text)
    exact = _norm_text(text)
    with _lock:
        d = _load()
        now = time.time()
        d["exact:" + exact] = {"commands": commands, "ref": _ref_id(), "used": now}
        if vals and len(set(vals)) == len(vals):
            tmpl = [_abstract(c, vals) for c in commands]
            if all(t is not None for t in tmpl):
                d["tmpl:" + key] = {"commands": tmpl, "n": len(vals), "ref": _ref_id(), "used": now}
        _save(d)


def forget(text: str):
    """Called when you decline a plan: don't reuse it."""
    key, _ = _template(text)
    with _lock:
        d = _load()
        d.pop("exact:" + _norm_text(text), None)
        d.pop("tmpl:" + key, None)
        _save(d)


def recall(text: str):
    """-> (commands, how) from an earlier approved plan, or None."""
    with _lock:
        d = _load()
    e = d.get("exact:" + _norm_text(text))
    if e and e.get("ref") == _ref_id():
        return list(e["commands"]), "the same request ran successfully before"
    key, vals = _template(text)
    e = d.get("tmpl:" + key)
    if e and e.get("ref") == _ref_id() and e.get("n") == len(vals) and len(set(vals)) == len(vals):
        try:
            cmds = [_fill(c, vals) for c in e["commands"]]
        except (IndexError, ValueError):
            return None
        return cmds, "the same kind of request ran successfully before; filled in " + ", ".join(vals)
    return None


def list_plans() -> list[dict]:
    d = _load()
    return [{"key": k, "commands": v["commands"], "used": v.get("used", 0)} for k, v in
            sorted(d.items(), key=lambda kv: -kv[1].get("used", 0))]


def clear_plans():
    with _lock:
        _save({})


# ------------------------------------------------------------------ 2b. argument values: fix what's certain, reject what's invented
_IPV4 = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})(?:/(\d{1,2}))?$")


def _netmask_len(tok: str):
    m = _IPV4.match(tok)
    if not m or m.group(2):
        return None
    try:
        n = int.from_bytes(bytes(int(x) for x in tok.split(".")), "big")
    except ValueError:
        return None
    bits = bin(n)[2:].zfill(32)
    return bits.count("1") if "01" not in bits and bits.startswith("1") else None


HOSTNAME_RE = re.compile(r"(?=.{1,63}$)[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?")
HOSTNAME_RULE = "letters, digits and hyphens, not starting or ending with a hyphen"


def fix_values(cmd: str, grounding: str):
    """Values in a model-proposed command, checked against what the user actually wrote (`grounding`).
    -> (command, [reasons for changes], error or None).
      - interface names: vlan20 / vlan.20 / ethernet 4 -> Vlan20 / Ethernet4
      - "1.1.1.1 255.255.255.0" -> "1.1.1.1/24" (SONiC takes prefix form)
      - every IP must come from the request; 1.1.1.1 where the request says 1.1.1.1/24 becomes 1.1.1.1/24
    """
    toks = cmd.split()
    if not toks or toks[0] not in ("config", "show"):
        return cmd, [], None
    reasons = []
    out = []
    for t in toks:
        c = canon_iface(t) if re.search(r"\d", t) else None
        if c and c != t:
            reasons.append(f"{t} -> {c}")
            t = c
        out.append(t)
    toks, out = out, []
    i = 0
    while i < len(toks):
        t = toks[i]
        m = _IPV4.match(t)
        if m and not m.group(2) and i + 1 < len(toks) and _netmask_len(toks[i + 1]) is not None and _netmask_len(t) is None:
            new = f"{t}/{_netmask_len(toks[i + 1])}"
            reasons.append(f"{t} {toks[i + 1]} -> {new} (SONiC takes the /prefix form)")
            out.append(new); i += 2
            continue
        out.append(t); i += 1
    given = {v for v in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b", grounding)}
    bases = {v.split("/")[0]: v for v in given}
    final = []
    for t in out:
        m = _IPV4.match(t)
        if m and t not in given:
            base = m.group(1)
            if not m.group(2) and base in bases and "/" in bases[base]:
                reasons.append(f"{t} -> {bases[base]} (as in your request)")
                t = bases[base]
            elif m.group(2) and base in given:
                return cmd, reasons, (f"'{t}': the prefix length /{m.group(2)} isn't in your request; "
                                      f"please give it, e.g. {base}/{m.group(2)}")
            else:
                return cmd, reasons, f"'{t}' is not a value from your request (the model must not invent addresses)"
        final.append(t)
    if final[:2] == ["config", "hostname"]:  # a hostname is a value too: valid, and from the request
        if len(final) < 3:
            return cmd, reasons, "'config hostname' needs the new hostname"
        name = final[2]
        if not HOSTNAME_RE.fullmatch(name):
            return cmd, reasons, f"'{name}' is not a valid hostname ({HOSTNAME_RULE})"
        if not re.search(r"(?i)(?<![\w-])" + re.escape(name) + r"(?![\w-])", grounding):
            return cmd, reasons, f"the hostname '{name}' is not in your request (the model must not invent a name)"
    return " ".join(final), reasons, None


# ------------------------------------------------------------------ 2c. modes chosen by an option (tagged/untagged)
# (command, word that means "with the option", word that means "without it", the option and its long form)
MODES = [("config vlan member add", "untagged", "tagged", "-u", "--untagged")]
_NEG = re.compile(r"(?:\bnot|\bno|\binstead of|\brather than|n't)\s*$", re.I)


def _said(text: str, word: str):
    """-> (said, said-but-negated) for a whole word: 'TAGGED not UNTAGGED' says tagged, negates untagged."""
    pos = neg = False
    for m in re.finditer(r"\b" + word + r"\b", text, re.I):
        if _NEG.search(text[max(0, m.start() - 16):m.start()]):
            neg = True
        else:
            pos = True
    return pos, neg


def fix_modes(cmd: str, request: str):
    """Make the option match what was asked: 'tagged' -> no -u (the default), 'untagged' -> -u.
    -> (command, reason or '')."""
    for path, on_word, off_word, short, long_ in MODES:
        if not (cmd == path or cmd.startswith(path + " ")):
            continue
        on, on_neg = _said(request, on_word)
        off, off_neg = _said(request, off_word)
        want = True if (on and not off) or (on and off_neg) else False if (off and not on) or (off and on_neg) else None
        toks = cmd.split()
        has = short in toks or long_ in toks
        if want is False and has:
            new = " ".join(t for t in toks if t not in (short, long_))
            return new, f"{short} removed: you asked for {off_word} ({off_word} is the default, no option)"
        if want is True and not has:
            n = len(path.split())
            return " ".join(toks[:n] + [short] + toks[n:]), f"{short} added: you asked for {on_word}"
    return cmd, ""


# ------------------------------------------------------------------ 3. deterministic fixes
def fix(cmd: str):
    """-> (fixed command, reason) or None. Never invents: the result must still pass validation afterwards."""
    toks = cmd.split()
    if not toks:
        return None
    reasons = []
    # Linux ip changes -> SONiC config
    m = re.match(r"^(?:sudo\s+)?ip\s+(?:-4\s+|-6\s+)?addr(?:ess)?\s+(add|del|delete)\s+(\S+)\s+dev\s+(\S+)$", cmd)
    if m:
        dev = canon_iface(m.group(3)) or m.group(3)
        verb = "add" if m.group(1) == "add" else "remove"
        new = f"config interface ip {verb} {dev} {m.group(2)}"
        return new, "Linux 'ip addr' bypasses SONiC; the SONiC equivalent is 'config interface ip'"
    m = re.match(r"^(?:sudo\s+)?ip\s+link\s+set\s+(?:dev\s+)?(\S+)\s+(up|down)$", cmd)
    if m:
        dev = canon_iface(m.group(1)) or m.group(1)
        return (f"config interface {'startup' if m.group(2) == 'up' else 'shutdown'} {dev}",
                "Linux 'ip link set' bypasses SONiC; the SONiC equivalent is 'config interface startup/shutdown'")
    # interface names (vlan.20, vlan20, ethernet 4 -> Vlan20, Ethernet4); subcommands like 'vlan' stay as they are
    new = []
    for t in toks:
        c = canon_iface(t) if re.search(r"\d", t) else None
        if c and c != t:
            reasons.append(f"{t} -> {c}")
            t = c
        new.append(t)
    toks = new
    # a unique close match for a misspelled subcommand of config/show
    if toks[0] in clidoc.ROOTS:
        path, i = toks[0], 1
        while i < len(toks):
            kids, _ = clidoc._children(path)
            if not kids or toks[i].startswith("-"):
                break
            if toks[i] not in kids:
                near = difflib.get_close_matches(toks[i], kids, n=2, cutoff=0.75)
                if len(near) == 1:
                    reasons.append(f"{toks[i]} -> {near[0]}")
                    toks[i] = near[0]
                else:
                    break
            path += " " + toks[i]
            i += 1
    if not reasons:
        return None
    return " ".join(toks), "SONiC naming/spelling: " + ", ".join(reasons)
