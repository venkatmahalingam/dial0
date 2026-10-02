"""SONiC CLI reference = static index (parsed from the sonic-utilities source) + live cross-check on this switch.

* static  : /opt/dial0/data/commands.json, generated at image build by extract_cli.py from sonic-utilities config/ + show/
            (commands, groups, options, arguments, help text, source file:line). The bundled source is grep-able too.
* live    : background crawl of `<group> --help` on the switch. Only groups are crawled (leaves are known from source).
            The live children of a group are authoritative for THIS device/SONiC version.
Used to (1) put the relevant commands in the prompt, (2) reject anything that does not exist before it can run,
(3) serve the model's `ref` tool.
"""
import difflib, hashlib, json, os, re, subprocess, threading, time
from . import tools

STATE = os.getenv("DIAL0_STATE_DIR", "/var/lib/dial0")
CACHE = os.path.join(STATE, "clidoc.json")
STATIC_PATH = os.getenv("DIAL0_CMD_INDEX", "/opt/dial0/data/commands.json")
SRC_ROOT = os.getenv("DIAL0_SRC_ROOT", "/opt/sonic-utilities")   # GitHub copy bundled at build (fallback)
# The switch's own installed SONiC CLI (config/ and show/ from its dist-packages), mounted read-only by the host.
# When present it is THE source of truth: exactly the commands this switch runs.
SWITCH_SRC = os.getenv("DIAL0_SWITCH_SRC", "/opt/switch-src")
SWITCH_INDEX = os.path.join(os.getenv("DIAL0_STATE_DIR", "/var/lib/dial0"), "switch_commands.json")
MIN_SWITCH_COMMANDS = int(os.getenv("DIAL0_MIN_SWITCH_COMMANDS", "150"))
# The official SONiC Command Reference (doc/Command-Reference.md): descriptions and real examples for commands.
# It never adds commands: a documented command that this switch's CLI doesn't have is ignored.
CMDREF_PATHS = [p for p in (os.getenv("DIAL0_CMDREF", "/opt/cmdref/reference.md"),
                            "/opt/sonic-utilities/Command-Reference.md") if p]
# A curated reference (sets of plain command lines in code blocks, e.g. reference/sonic-command-reference.md):
# ONLY its commands are offered to the model and accepted from it.
_curated: dict = {}  # command path -> {"examples": [...], "area": str, "kind": "show"|"config", "desc": str}
_curated_meta: dict = {}
CMDREF_CACHE = os.path.join(os.getenv("DIAL0_STATE_DIR", "/var/lib/dial0"), "cmdref.json")
_CMDREF_PARSER = "2"
_cmdref: dict = {}   # command path -> {"desc", "examples", "usage"}
_cmdref_meta: dict = {}
ROOTS = ["config", "show"]
MAX_DEPTH = int(os.getenv("DIAL0_CLIDOC_DEPTH", "3"))
LIVE_CHECK = os.getenv("DIAL0_LIVE_CHECK", "on")  # on | off
PACE_S = float(os.getenv("DIAL0_CLIDOC_PACE", "1.0"))
# Set while a request is being served. The crawler's `--help` calls run in this container's CPU cgroup,
# i.e. on the same core as the model, so it waits instead of competing.
BUSY = threading.Event()
BUDGET_S = int(os.getenv("DIAL0_CLIDOC_BUDGET", "1800"))
VALIDATE = os.getenv("DIAL0_CLI_VALIDATE", "strict")  # strict | off
COMMON_FLAGS = {"-h", "--help", "-?", "-n", "--namespace", "-d", "--display", "-y", "--yes", "-v", "--verbose"}

_static: dict[str, dict] = {}
_meta: dict = {}
_live: dict[str, dict] = {}
_state = {"ready": False, "complete": False, "crawled": 0, "key": ""}
_lock = threading.Lock()

_CMD = re.compile(r"^  (\S+)(?:\s{2,}(.*))?$")
_FLAG = re.compile(r"^--?[A-Za-z?][\w-]*")


# ---------------------------------------------------------------- loading / live crawl
def load_static():
    try:
        with open(STATIC_PATH) as f:
            d = json.load(f)
        _static.update(d["nodes"])
        _meta.update(d.get("meta", {}))
    except Exception:
        pass


def available() -> bool:
    return bool(_static) or bool(_curated) or _state["ready"]


def src_root() -> str:
    """Where `ref src` and the source search look: the switch's own CLI code if mounted, else the GitHub copy."""
    return _meta.get("root", SWITCH_SRC) if _meta.get("origin") == "switch" else SRC_ROOT


def _fingerprint(root: str) -> str:
    h = hashlib.sha256()
    for tree in ("config", "show"):
        for dp, _, files in sorted(os.walk(os.path.join(root, tree))):
            for f in sorted(files):
                if f.endswith(".py"):
                    st = os.stat(os.path.join(dp, f))
                    h.update(f"{dp}/{f}:{st.st_size}:{int(st.st_mtime)}".encode())
    return h.hexdigest()[:16]


def load_switch_index(root: str = "") -> bool:
    """Index the switch's installed config/ and show/ (AST only, nothing imported or run). Cached until that code
    changes. Replaces the GitHub index; returns False (keeping the GitHub one) if the code isn't there or doesn't parse."""
    root = root or SWITCH_SRC
    if not (os.path.isfile(os.path.join(root, "config", "main.py")) and os.path.isfile(os.path.join(root, "show", "main.py"))):
        return False
    try:
        fp = _fingerprint(root)
        try:
            with open(SWITCH_INDEX) as f:
                cached = json.load(f)
            if cached.get("fingerprint") == fp:
                nodes, meta = cached["nodes"], dict(cached["meta"], root=root)
            else:
                raise ValueError("changed")
        except (OSError, ValueError, KeyError):
            from . import extract_cli
            nodes, unresolved, orphans = extract_cli.build(root)
            n = sum(1 for x in nodes.values() if x["kind"] == "command")
            if n < MIN_SWITCH_COMMANDS:
                return False
            meta = {"origin": "switch", "ref": "this switch", "commit": "", "commands": n,
                    "unresolved": len(unresolved), "unreachable": len(orphans), "root": root,
                    "path": os.getenv("DIAL0_SWITCH_SRC_HOST", root)}
            try:
                os.makedirs(os.path.dirname(SWITCH_INDEX), exist_ok=True)
                with open(SWITCH_INDEX, "w") as f:
                    json.dump({"fingerprint": fp, "meta": meta, "nodes": nodes}, f)
            except OSError:
                pass
    except (SystemExit, Exception):  # extractor gave up: keep the GitHub index
        return False
    with _lock:
        _static.clear(); _static.update(nodes)
        _meta.clear(); _meta.update(meta)
    return True


def _parse(text: str):
    usage, kids, section = "", {}, None
    for ln in text.splitlines():
        if ln.startswith("Usage:"):
            usage = ln[6:].strip()
        elif re.match(r"^(Commands|Options|Arguments):\s*$", ln):
            section = ln.strip().rstrip(":")
        elif section == "Commands":
            m = _CMD.match(ln)
            if m:
                kids[m.group(1)] = (m.group(2) or "").strip()
            elif ln and not ln.startswith(" "):
                section = None
    return usage, kids


def _version_key() -> str:
    return hashlib.sha256(tools.run_raw(["cat", "/etc/sonic/sonic_version.yml"]).encode()).hexdigest()[:16]


def _crawl(deadline: float) -> bool:
    queue = [(r, 1) for r in ROOTS]
    while queue:
        if time.time() > deadline:
            return False
        while BUSY.is_set():
            time.sleep(0.5)
        path, depth = queue.pop(0)
        s = _static.get(path)
        if s and s["kind"] == "command":
            continue  # leaf known from source: no need to spawn the (slow) CLI
        usage, kids = _parse(tools.run_raw(path.split() + ["--help"]))
        with _lock:
            n = _live.setdefault(path, {"help": "", "usage": "", "children": [], "crawled": False})
            n.update(usage=usage, children=list(kids), crawled=bool(usage))
            for name, desc in kids.items():
                _live.setdefault(f"{path} {name}", {"help": desc, "usage": "", "children": [], "crawled": False})
            _state["crawled"] += 1
        time.sleep(PACE_S)
        if depth < MAX_DEPTH:
            queue += [(f"{path} {k}", depth + 1) for k in kids]
    return True


def _worker():
    key = _version_key()
    _state["key"] = key
    try:
        with open(CACHE) as f:
            c = json.load(f)
        if c.get("key") == key and c.get("complete"):
            _live.update(c["nodes"]); _state.update(ready=True, complete=True)
            return
    except Exception:
        pass
    done = _crawl(time.time() + BUDGET_S)
    if any(n.get("crawled") for n in _live.values()):
        _state.update(ready=True, complete=done)
        try:
            os.makedirs(STATE, exist_ok=True)
            with open(CACHE, "w") as f:
                json.dump({"key": key, "complete": done, "nodes": _live}, f)
        except OSError:
            pass


# ---------------------------------------------------------------- the Command Reference document
_HEAD = re.compile(r"^\s*\*\*\s*(?:sudo\s+)?((?:config|show|sonic-clear)\s[^*]+?)\s*\*\*\s*$")
_PROMPT = re.compile(r"^\s*\S*@[\w.-]+:[^$#]*[$#]\s+(?:sudo\s+)?((?:config|show|sonic-clear)\s.+?)\s*$")


def parse_cmdref(text: str) -> list:
    """Command-Reference.md -> [{"head", "desc", "usage": [...], "examples": [...]}], one per **command** section."""
    sections, cur, in_code, code_kind = [], None, False, ""
    for raw in text.splitlines():
        line = raw.rstrip()
        m = None if in_code else _HEAD.match(line)
        if m:
            cur = {"head": " ".join(m.group(1).split()), "desc": [], "usage": [], "examples": []}
            sections.append(cur)
            continue
        if cur is None:
            continue
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        low = line.strip().lower()
        if not in_code:
            if low.startswith(("- usage", "usage:")):
                code_kind = "usage"
            elif low.startswith(("- example", "example")):
                code_kind = "example"
            elif line.startswith("#") or low.startswith("go back to"):
                cur = None
            elif low and not low.startswith("-") and len(cur["desc"]) < 3 and not code_kind:
                cur["desc"].append(line.strip())
            continue
        pm = _PROMPT.match(line)
        if pm:
            cur["examples"].append(" ".join(pm.group(1).split()))
        elif code_kind == "usage" and re.match(r"^\s*(?:sudo\s+)?(?:config|show|sonic-clear)\s", line):
            cur["usage"].append(" ".join(line.replace("sudo ", "", 1).split()))
    for sct in sections:
        sct["desc"] = " ".join(sct["desc"])[:400]
    return sections


_DIGIT_KEYWORDS = {"ipv4", "ipv6", "ip4", "ip6", "l2", "l3", "v4", "v6", "ospfv2", "ospfv3", "sha1", "sha256"}  # words, not values
_VALUE_WORDS = {"enabled", "disabled", "true", "false", "on", "off", "access", "trunk", "routed", "all", "mgmt",
                "nonzero", "default"}


def _heuristic_path(toks: list) -> str:
    """Command path of an example when the switch index doesn't know it: words up to the first value."""
    out = []
    for t in toks:
        is_value = (t.startswith("-") or t[:1].isdigit() or t[:1].isupper() or re.search(r"[/:.,\[\]]", t)
                    or t in _VALUE_WORDS or re.match(r"(?i)^(eth|ethernet|vlan|portchannel|loopback|lo)\d", t)
                    or (re.fullmatch(r"[a-z][a-z0-9-]*\d", t) and t not in _DIGIT_KEYWORDS))
        # "ipv6", "use-link-local-only" are keywords; "eth0", "Ethernet4", "100", "-u", "leaf1" are values
        if is_value and len(out) >= 2:
            break
        out.append(t)
    return " ".join(out)


def parse_curated(text: str) -> list:
    """Curated reference -> [{"path", "example", "area", "kind", "desc"}] for every command line in a code block."""
    out, area, kind, desc, in_code = [], "", "", [], False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            if not in_code:
                desc = []
            continue
        if in_code:
            note = ""
            if re.search(r"\s#\s*", line):  # "sudo config vlan member add 100 Ethernet0   # tagged member: the default"
                line, _, note = re.split(r"\s(#)\s*", line, maxsplit=1)
                line, note = line.strip(), note.strip()
            m = re.match(r"^(?:sudo\s+)?((?:config|show|sonic-clear)\s.*)$", line)
            if m:
                ex = " ".join(m.group(1).split())
                toks = ex.split()
                path = _heuristic_path(toks)  # from the file alone: the words up to the first value
                n = _static.get(path)
                out.append({"path": path, "example": ex, "area": area, "kind": toks[0], "desc": " ".join(desc)[:2000],
                            "in_index": bool(n and (n["kind"] == "command" or n.get("runnable"))), "note": note})
            continue
        if line.startswith("## "):
            kind = "show" if "show" in line.lower() else ("config" if "config" in line.lower() else kind)
        elif line.startswith("### "):
            area, desc = line[4:].strip(), []
        elif line and not line.startswith("#") and area and not line.startswith("---"):
            desc.append(line)
    return out


def is_curated_format(text: str) -> bool:
    """Plain command lines in code blocks and no **command** headings (the official document has those)."""
    return not re.search(r"^\s*\*\*\s*(?:sudo\s+)?(?:config|show)\s", text, re.M) and bool(
        re.search(r"^\s*(?:sudo\s+)?(?:config|show)\s", text, re.M))


def _flag_note(flag: str, desc: str) -> str:
    """What the reference's description says about an option, e.g. -m -> 'several with -m as a range or list'."""
    f = re.escape(flag)
    for rx in (r"\(([^()]*?(?<![\w-])" + f + r"(?![\w-])[^()]*)\)",       # "(tagged by default, untagged with -u)"
               r"(?<![\w-])" + f + r"(?![\w-])\s*\(([^()]+)\)"):            # "--min-links (minimum links needed ...)"
        m = re.search(rx, desc)
        if m:
            return " ".join(m.group(1).split())[:90]
    for clause in re.split(r";|\.\s", desc):
        if re.search(r"(?<![\w-])" + f + r"(?![\w-])", clause):
            return " ".join(clause.split())[:90]
    return ""


def _auto_notes(d: dict):
    """Notes for examples that have none: flags explained from the description, and for a command whose
    options have a 'by default' form (tagged by default, untagged with -u), the flag-less example is that default."""
    flagged = {}
    for ex in d["examples"]:
        flags = [t for t in ex.split() if re.match(r"^--?[a-zA-Z]", t)]
        if flags and not d["notes"].get(ex):
            d["notes"][ex] = "; ".join(filter(None, (_flag_note(fl, d["desc"]) for fl in flags)))
        for fl in flags:
            flagged[fl] = _flag_note(fl, d["desc"])
    default = next((m.group(1) for n in flagged.values() for m in [re.search(r"(\w+) by default", n)] if m), "")
    if default:
        for ex in d["examples"]:
            if not re.search(r"\s--?[a-zA-Z]", ex) and not d["notes"].get(ex):
                d["notes"][ex] = f"{default}: the default, no flag"


def _load_curated(path: str, raw: str):
    entries = parse_curated(raw)
    cur = {}
    for e in entries:
        d = cur.setdefault(e["path"], {"examples": [], "notes": {}, "area": e["area"], "kind": e["kind"], "desc": e["desc"],
                                       "in_index": e["in_index"]})
        if e["example"] not in d["examples"]:
            d["examples"].append(e["example"])
        if e.get("note"):
            d["notes"][e["example"]] = e["note"]
    for d in cur.values():
        _auto_notes(d)
    _curated.clear(); _curated.update(cur)
    _curated_meta.clear()
    _curated_meta.update({"file": os.getenv("DIAL0_CMDREF_HOST", path), "commands": len(cur),
                          "show": sum(1 for d in cur.values() if d["kind"] == "show"),
                          "config": sum(1 for d in cur.values() if d["kind"] == "config"),
                          "not_in_switch_code": sorted(p for p, d in cur.items() if not d["in_index"])})


def _pick_examples(d: dict, k: int = 2) -> list:
    """Up to k examples, preferring ones that show different forms (with and without options)."""
    exs, seen = [], set()
    for e in d["examples"]:
        form = tuple(t for t in e.split() if t.startswith("-"))
        if form not in seen:
            exs.append(e); seen.add(form)
    for e in d["examples"]:
        if len(exs) >= k:
            break
        if e not in exs:
            exs.append(e)
    return exs[:k]


def full_reference() -> str:
    """Every command from the curated reference, grouped like the file (set -> area), with each area's description
    and each command's examples and notes. Identical for every request, so llama.cpp can keep it cached."""
    if not _curated:
        return ""
    out = []
    for kind, title in (("show", "SET 1: SHOW COMMANDS (read-only; safe to run)"),
                        ("config", "SET 2: CONFIG COMMANDS (change the switch)")):
        out.append(title)
        seen_area = None
        for p, d in _curated.items():
            if d["kind"] != kind:
                continue
            if d["area"] != seen_area:
                seen_area = d["area"]
                out.append(f"## {seen_area}: {d['desc']}" if d.get("desc") else f"## {seen_area}")
            exs = [e + (f" ({d['notes'][e]})" if d.get("notes", {}).get(e) else "") for e in _pick_examples(d, 3)]
            n = _static.get(p)
            usage = f"  usage: {_usage(p)}" if n and (n["kind"] == "command" or n.get("runnable")) and _usage(p) != p else ""
            out.append(f"- {p}{usage}  e.g. " + "; ".join(exs))
        out.append("")
    return "\n".join(out).strip()


def curated() -> bool:
    return bool(_curated)


def curated_path(argv: list):
    """The longest command path from the curated reference that argv starts with, or None."""
    best = None
    for p in _curated:
        pt = p.split()
        if argv[:len(pt)] == pt and (best is None or len(pt) > len(best.split())):
            best = p
    return best


def check_command(argv: list):
    """None if argv may run; else why not. With a curated reference: it must be one of its commands; where the
    switch's CLI code defines the command, options and argument values are checked against it too."""
    if _curated:
        p = curated_path(argv)
        if p is None:
            near = search_paths(" ".join(argv), 3)
            return (f"'{' '.join(argv)}' is not in your command reference ({_curated_meta.get('file', 'reference')}); "
                    "only its commands are used." + (" Closest: " + "; ".join(near) if near else ""))
        n = _static.get(p)
        if n and (n["kind"] == "command" or n.get("runnable")):
            return validate(argv) or check_args(argv)
        return None
    return validate(argv) or check_args(argv)


def _path_of(cmd: str):
    """The longest command path in the index that `cmd` starts with (a runnable command), or None."""
    toks, best = cmd.split(), None
    for k in range(1, len(toks) + 1):
        p = " ".join(toks[:k])
        n = _static.get(p)
        if n is None:
            if k > 1 and " ".join(toks[:k - 1]) in _static:
                break
            continue
        if n["kind"] == "command" or n.get("runnable"):
            best = p
    return best


def load_cmdref(paths=None) -> bool:
    """Attach the Command Reference's descriptions and (validated) examples to the commands this switch has."""
    from . import tools
    for path in (paths or CMDREF_PATHS):
        if not os.path.isfile(path):
            continue
        try:
            raw = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if is_curated_format(raw):
            _load_curated(path, raw)
            return True
        key = hashlib.sha256((raw + _CMDREF_PARSER + str(len(_static)) + (_meta.get("commit") or _meta.get("ref") or "")).encode()).hexdigest()[:16]
        try:
            with open(CMDREF_CACHE) as f:
                c = json.load(f)
            if c.get("key") == key:
                _cmdref.clear(); _cmdref.update(c["docs"]); _cmdref_meta.clear(); _cmdref_meta.update(c["meta"])
                return True
        except (OSError, ValueError, KeyError):
            pass
        docs, ignored, dropped = {}, set(), 0
        for sct in parse_cmdref(raw):
            p = _path_of(sct["head"]) or next((_path_of(u) for u in sct["usage"] if _path_of(u)), None)
            if p is None:
                ignored.add(sct["head"].split(" <")[0].split(" [")[0])
                continue
            d = docs.setdefault(p, {"desc": "", "examples": [], "usage": []})
            d["desc"] = d["desc"] or sct["desc"]
            d["usage"] += [u for u in sct["usage"] if u not in d["usage"]][:2]
            for ex in sct["examples"]:
                try:
                    argv = tools.parse_click(ex)
                except Exception:
                    dropped += 1; continue
                if _path_of(ex) != p or validate(argv) or check_args(argv):
                    dropped += 1  # e.g. syntax from another release
                elif ex not in d["examples"] and len(d["examples"]) < 3:
                    d["examples"].append(ex)
        meta = {"file": path, "documented": len(docs), "ignored": len(ignored), "examples_dropped": dropped,
                "examples": sum(len(d["examples"]) for d in docs.values())}
        _cmdref.clear(); _cmdref.update(docs); _cmdref_meta.clear(); _cmdref_meta.update(meta)
        try:
            with open(CMDREF_CACHE, "w") as f:
                json.dump({"key": key, "docs": docs, "meta": meta}, f)
        except OSError:
            pass
        return True
    return False


def start_background():
    load_static()
    load_switch_index()
    load_cmdref()
    if LIVE_CHECK != "off":
        threading.Thread(target=_worker, daemon=True).start()


def status() -> dict:
    """Where the command reference comes from and how complete it is (for `dial0 ctl status` and /health)."""
    return {"source": "this switch's installed CLI (" + _meta.get("path", SWITCH_SRC) + ")" if _meta.get("origin") == "switch"
            else "sonic-utilities from GitHub (switch CLI source not mounted)",
            "source_commands": _meta.get("commands", 0), "source_ref": _meta.get("ref", ""),
            "source_commit": (_meta.get("commit") or "")[:10], "source_unresolved": _meta.get("unresolved", 0),
            "command_reference_doc": (f"commands from {_curated_meta['file']}: only these are used; "
                                      f"{len(_curated_meta['not_in_switch_code'])} not found in the switch's CLI code (trusted as listed)"
                                      if _curated else
                                      f"{_cmdref_meta.get('documented', 0)} commands described, {_cmdref_meta.get('examples', 0)} "
                                      f"examples; {_cmdref_meta.get('ignored', 0)} documented commands not on this switch ignored"
                                      if _cmdref_meta else "not loaded"),
            "live_ready": _state["ready"], "live_complete": _state["complete"], "live_groups_crawled": _state["crawled"]}


# ---------------------------------------------------------------- lookups
def _children(path: str):
    """Children of a group. The live device answer wins over the source when we have crawled it."""
    l = _live.get(path)
    if l and l.get("crawled"):
        return l["children"], "device"
    s = _static.get(path)
    if s:
        return s["children"], "source"
    return None, None


def _on_device(path: str) -> bool:
    parts = path.split()
    cur = parts[0]
    for seg in parts[1:]:
        l = _live.get(cur)
        if l and l.get("crawled") and seg not in l["children"]:
            return False
        cur += " " + seg
    return True


def _usage(path: str) -> str:
    return (_static.get(path) or {}).get("usage") or (_live.get(path) or {}).get("usage") or path


def _help(path: str) -> str:
    return (_static.get(path) or {}).get("help") or (_live.get(path) or {}).get("help") or ""


def _tok(s: str) -> set:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def _line(path: str) -> str:
    if _curated and path in _curated:
        d = _curated[path]
        n = _static.get(path)
        usage = _usage(path) if n and (n["kind"] == "command" or n.get("runnable")) else path
        h = _help(path) if n else ""
        exs = [e + (f" ({d['notes'][e]})" if d.get("notes", {}).get(e) else "") for e in _pick_examples(d)]
        return f"- {usage}" + (f"   # {h}" if h else "") + "  e.g. " + "; ".join(exs)
    h = _help(path)
    d = _cmdref.get(path, {})
    ex = d["examples"][0] if d.get("examples") else ""  # the document's first example is its canonical one
    return f"- {_usage(path)}" + (f"   # {h}" if h else "") + (f"  e.g. {ex}" if ex else "")


def _candidates():
    """Runnable commands: leaves, plus groups that also run by themselves (click invoke_without_command),
    e.g. `show interfaces counters` next to `show interfaces counters errors`.
    With a curated reference: only its commands."""
    if _curated:
        for path, d in _curated.items():
            n = _static.get(path) or {}
            yield (path, " ".join([n.get("help", "")] + d["examples"] + list(d.get("notes", {}).values())
                                  + [_own_desc(path, d["desc"])]),
                   d["area"] + " " + d["desc"])
        return
    for path, n in _static.items():
        if n["kind"] == "command" or n.get("runnable"):
            extra = " ".join(o["name"] + " " + o["help"] for o in n["options"]) + " " + " ".join(a["name"] for a in n["args"])
            doc = _cmdref.get(path, {}).get("desc", "")
            yield path, (n["help"] + " " + doc).strip(), (n.get("usage", "")[len(path):] + " " + extra)
    for path, n in _live.items():
        if path not in _static and not n["children"]:
            yield path, n["help"], n.get("usage", "")


# ---------------------------------------------------------------- search: request -> relevant real commands
# How a request is read (no model involved):
#  - action words give the direction (config... vs show...) and the command's action (assign -> add, delete -> del/remove)
#  - values are hints, not words: 10.1.1.1/24 -> ip, Ethernet4 -> interface, vlan.20 -> vlan; the numbers are ignored
#  - "X and Y" is split so each part gets its own best commands
#  - path words the request never mentioned (dhcp_relay, helper-address...) count against a command
_STOPW = set("a an the to on in of for and or please with from all my is are can you how i me want need this that it "
             "its into at by be as do does should would could there their them our your any some via using use new "
             "correct right proper".split())
_ACTIONS = {  # word: (intent, actions it maps to in command paths)
    "configure": ("change", ()), "config": ("change", ()), "setup": ("change", ()), "bring": ("change", ()),
    "set": ("change", ("set",)), "change": ("change", ("set",)), "modify": ("change", ("set",)), "update": ("change", ("set", "update")),
    "add": ("change", ("add",)), "assign": ("change", ("add",)), "create": ("change", ("add",)), "attach": ("change", ("add", "bind")),
    "bind": ("change", ("bind",)), "put": ("change", ("add",)), "make": ("change", ("add",)), "allocate": ("change", ("add",)),
    "delete": ("change", ("del", "remove")), "remove": ("change", ("remove", "del")), "del": ("change", ("del", "remove")),
    "unassign": ("change", ("remove", "del")), "detach": ("change", ("del", "remove", "unbind")), "unbind": ("change", ("unbind",)),
    "enable": ("change", ("enable", "startup")), "disable": ("change", ("disable", "shutdown")),
    "shutdown": ("change", ("shutdown",)), "shut": ("change", ("shutdown",)), "startup": ("change", ("startup",)),
    "up": (None, ("startup",)), "down": (None, ("shutdown",)),
    "save": ("change", ("save",)), "load": ("change", ("load",)), "clear": ("change", ("clear",)), "reset": ("change", ("reset",)),
    "show": ("read", ()), "display": ("read", ()), "list": ("read", ()), "get": ("read", ()), "view": ("read", ()),
    "check": ("read", ()), "what": ("read", ()), "which": ("read", ()), "see": ("read", ()), "print": ("read", ()),
    "status": ("read", ("status",)), "verify": ("read", ()),
}
_DEFAULT_CHANGE = ("add", "set")  # plain "configure X" usually means create/set X
_ACTION_WORDS = {"add", "del", "remove", "set", "enable", "disable", "startup", "shutdown", "bind", "unbind", "update",
                 "save", "load", "clear", "reset", "status"}
_OPPOSITE = {"add": {"del", "remove"}, "del": {"add"}, "remove": {"add"}, "startup": {"shutdown"}, "shutdown": {"startup"},
             "enable": {"disable"}, "disable": {"enable"}, "bind": {"unbind"}, "unbind": {"bind"}}
_SYN = {"addr": "address", "ipv4": "ip", "ethernet": "interface", "eth": "interface", "port": "interface",
        "intf": "interface", "iface": "interface", "link": "interface", "routing": "route", "lag": "portchannel", "pc": "portchannel",
        "neighbour": "neighbor", "nbr": "neighbor", "fdb": "mac", "l3": "ip", "gateway": "nexthop", "gw": "nexthop",
        "temp": "temperature", "optic": "transceiver", "sfp": "transceiver", "mem": "memory", "log": "logging",
        "syslog": "syslog", "hostname": "hostname", "running": "runningconfiguration", "startupconfig": "startupconfiguration"}
_SPLIT = re.compile(r"\s*(?:,|;|\band then\b|\bthen\b|\band also\b|\band\b|\balso\b|\bplus\b)\s*", re.I)
_VALUES = [  # (regex, entity hint)
    (re.compile(r"\b[0-9a-f]{0,4}(?::[0-9a-f]{0,4}){2,7}(?:/\d{1,3})?\b", re.I), "ipv6"),
    (re.compile(r"\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", re.I), "mac"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b"), "ip"),
    (re.compile(r"\bethernet\s*[./_-]?\s*\d+(?:/\d+)*\b", re.I), "interface"),
    (re.compile(r"\bvlan\s*[./_-]?\s*\d+\b", re.I), "vlan"),
    (re.compile(r"\bport-?channel\s*[./_-]?\s*\d+\b", re.I), "portchannel"),
    (re.compile(r"\bloopback\s*[./_-]?\s*\d+\b", re.I), "loopback"),
]


_TARGET = re.compile(r"\b(?:on|to|from|onto)\s+(?:the\s+)?(?:vlan|port-?channel|loopback|ethernet)\s*[./_-]?\s*\d+", re.I)
_QUESTION = re.compile(r"^\s*(?:is|are|does|do|has|have|how|why|when|where|who)\b", re.I)


def _stem(t: str) -> str:
    t = t.lower()
    if t in _SYN:
        return _SYN[t]
    if len(t) > 4 and t.endswith("sses"):
        t = t[:-2]
    elif len(t) > 3 and t.endswith("s") and not t.endswith(("ss", "us", "is")):
        t = t[:-1]
    return _SYN.get(t, t)


def _stems(text: str) -> list:
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", text.lower())]


def _parse_clause(text: str):
    """-> (entities, intent, actions)"""
    ents = set()
    target = bool(_TARGET.search(text))  # "... on vlan 20" / "to Ethernet4" (used below, for changes only)
    question = bool(_QUESTION.match(text))
    for rx, hint in _VALUES:
        if rx.search(text):
            ents.add(hint)
        text = rx.sub(" ", text)
    intent, actions = None, set()
    for w in re.findall(r"[a-z0-9-]+", text.lower()):
        for part in [w] + (w.split("-") if "-" in w else []):
            if part in _ACTIONS:
                i, acts = _ACTIONS[part]
                intent = intent or i
                actions |= set(acts)
                continue
            if part.isdigit() or part in _STOPW or len(part) < 2:
                continue
            ents.add(_stem(part))
    ents -= set(_ACTIONS)
    if intent is None and question:
        intent = "read"  # "is Ethernet4 up?", "how many vlans..."
    if intent == "change" and target:
        ents.add("interface")  # the VLAN/port after on/to/from is the interface being configured
    if not ents and actions - set(_DEFAULT_CHANGE):
        ents = set(actions - set(_DEFAULT_CHANGE))  # "save the config": the action is all there is to match
    if intent == "change" and not actions:
        actions = set(_DEFAULT_CHANGE)
    return ents, intent, actions


def _clauses(query: str):
    out = []
    for part in [p for p in _SPLIT.split(query) if p and p.strip()]:
        ents, intent, acts = _parse_clause(part)
        if out and intent is None and not acts:  # "... Ethernet0 and Ethernet4": no new action -> same clause
            e, i, a = out[-1]
            out[-1] = (e | ents, i, a)
        elif ents or intent:
            out.append((ents, intent, acts))
    return out


def _example_words(path: str) -> set:
    """Argument words in a listed command's examples, e.g. 'access' in 'config switchport mode access Ethernet0'."""
    d = _curated.get(path)
    if not d:
        return set()
    out = set()
    for e in d["examples"]:
        for t in e.split()[len(path.split()):]:
            if re.fullmatch(r"[a-z][a-z_-]+", t) and t not in _STOPW:
                out |= {_stem(x) for x in re.split(r"[-_]", t) if x}
    return out


def _weights(cands) -> dict:
    """How specific each word is across the candidate commands: rare words (access, proxy, anycast) count more
    than common ones (interface, config). -> {word: 0.5 .. 1.5}"""
    import math
    docs = [set(re.findall(r"[a-z0-9]+", " ".join(_stems(p + " " + h)))) for p, h, _ in cands]
    n = max(len(docs), 2)
    df = {}
    for d in docs:
        for t in d:
            df[t] = df.get(t, 0) + 1
    return {t: 0.5 + math.log(n / c) / math.log(n) for t, c in df.items()}


IGNORED_WORD_COST = 0.3   # tuned on the ranking checks (tests): realistic requests + your reference file
EXTRA_PATH_WORD_COST = 0.8
_EXPAND = {"pktdrops": ("packet", "drop"), "pktdrop": ("packet", "drop"), "mgmt": ("management",),
           "rif": ("router", "interface"), "naming": ("name",), "autoneg": ("negotiation", "auto")}


def _path_tokens(path: str) -> list:
    out = []
    for x in path.split()[1:]:
        for t in re.split(r"[-_]", x):
            if t:
                out.append(_stem(t))
                out += [_stem(e) for e in _EXPAND.get(t, ())]
    return out


def _clauses_of(desc: str) -> list:
    """Split a description at , ; . outside parentheses: "switchport mode (access, trunk or routed)" stays whole."""
    out, cur, depth = [], "", 0
    for ch in desc:
        depth += (ch == "(") - (ch == ")")
        if ch in ",;." and depth <= 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return [c for c in out if c]


def _own_desc(path: str, desc: str) -> str:
    """The clauses of an area description that are about this command: those naming one of its specific words
    ("switchport mode (access, trunk or routed)" -> config switchport mode), or, for commands made only of common
    words (config interface ip add), clauses naming two of them ("add or remove IP addresses on ...")."""
    generic = {"config", "show", "interface", "interfaces", "add", "del", "remove", "ip", "vlan", "member"}
    words = [w for w in re.split(r"[\s_-]+", path)[1:] if w and len(w) > 1]
    specific = [w for w in words if w not in generic and len(w) > 2]
    parts = []
    for c in _clauses_of(desc):
        cs = set(_stems(c))
        if specific and any(re.search(r"(?i)\b" + re.escape(k), c) for k in specific):
            parts.append(c)
        elif not specific and len({_stem(w) for w in words} & cs) >= 2:
            parts.append(c)
    return " ".join(parts)


def _score(path: str, help_: str, extra: str, ents: set, intent, acts: set, w=None) -> float:
    w = w or {}
    wt = lambda t: w.get(t, 1.0)
    words = path.split()
    root = words[0]
    ptoks = _path_tokens(path)
    pset, hset, xset = set(ptoks), set(_stems(help_)), set(_stems(extra))
    m_path = ents & pset
    m_ex = (ents - m_path) & _example_words(path)          # argument words shown in your examples: nearly as strong
    m_help = (ents - m_path - m_ex) & hset
    m_x = (ents - m_path - m_ex - m_help) & xset
    if not ents or not (m_path or m_ex or m_help or (_curated and m_x)):  # a listed command may match on its description
        return 0.0
    matched = m_path | m_ex | m_help            # m_x (a shared area description) is only a weak hint:
    s = (3.0 * sum(wt(t) for t in m_path) + 2.5 * sum(wt(t) for t in m_ex) + 1.2 * sum(wt(t) for t in m_help)
         + 0.3 * sum(wt(t) for t in m_x) + 2.0 * len(matched | m_x) / len(ents))
    s -= IGNORED_WORD_COST * sum(wt(t) for t in ents - matched)   # a word you said that this command ignores
    if intent == "change":
        s += 3.0 if root == "config" else (-1.0 if root == "show" else 0.0)
    elif intent == "read":
        s += 3.0 if root == "show" else -2.0
    path_acts = pset & _ACTION_WORDS
    if acts & path_acts:
        s += 2.5
    elif any(path_acts & _OPPOSITE.get(a, set()) for a in acts):
        s -= 2.0
    if set(_DEFAULT_CHANGE) == acts and acts & path_acts:
        s -= 1.5  # only the soft default matched: weaker than an explicit "add"/"set"
    s -= EXTRA_PATH_WORD_COST * sum(wt(t) for t in pset - ents - acts - _ACTION_WORDS)  # command words you didn't ask for
    return s


def _ranked(ents, intent, acts):
    scored = []
    cands = list(_candidates())
    w = _weights(cands)
    for path, help_, extra in cands:
        sc = _score(path, help_, extra, ents, intent, acts, w)
        if sc > 0:
            scored.append((-sc, len(path), path))
    scored.sort()
    return [p for _, _, p in scored if _curated or _on_device(p)]  # your listed commands are never hidden


def search(query: str, k: int = 10) -> str:
    """The k real commands most relevant to a request ('' if nothing matches)."""
    return "\n".join(_line(p) for p in search_paths(query, k))


def search_paths(query: str, k: int = 10) -> list:
    """The k most relevant runnable command paths for a request: request-aware ranking (action words, values,
    specific words over common ones, words in examples), with a show command kept for verifying changes."""
    if not available():
        return []
    parts = _clauses(query)
    if not parts:
        return []
    verify = []  # a matching show command per change, so the change can be verified (room reserved for it)
    for ents, intent, acts in parts:
        if intent == "change":
            for p in _ranked(ents, "read", set())[:1]:
                if p not in verify:
                    verify.append(p)
    verify = verify[:max(1, k // 4)]
    budget = k - len(verify)
    per = max(3, -(-budget // len(parts)))
    lists = [_ranked(*c)[:per] for c in parts]
    out = []
    for i in range(per):  # interleave: every part of the request gets its best commands first
        for lst in lists:
            if i < len(lst) and lst[i] not in out and len(out) < budget:
                out.append(lst[i])
    return out + [p for p in verify if p not in out]


def validate(argv: list[str]):
    """None if the command (and, where certain, its options) exists; else an explanation with real alternatives."""
    if argv[0] not in ROOTS:
        # Changes must go through SONiC's `config` CLI so they land in CONFIG_DB and persist. Linux `ip`, vtysh,
        # sonic-db-cli or sonic-cfggen writes bypass SONiC (lost on reload, invisible to `show`), so they're refused.
        if tools.is_mutating(argv):
            hint = search(" ".join(argv), 3)
            return (f"'{argv[0]}' may only be used read-only here; changes must be made with SONiC's 'config' command"
                    " so they are saved in CONFIG_DB." + (f" Relevant config commands:\n{hint}" if hint else ""))
        return None
    if VALIDATE == "off" or not available():
        return None
    path, i = argv[0], 1
    while i < len(argv):
        tok = argv[i]
        kids, src = _children(path)
        if kids is None:
            return None  # beyond anything we know: cannot judge
        if tok in kids:
            path += " " + tok
            i += 1
            continue
        if tok.startswith("-"):
            break
        if kids:
            near = difflib.get_close_matches(tok, kids, n=3)
            where = "on this device" if src == "device" else "in the SONiC reference"
            static_has = tok in (_static.get(path) or {}).get("children", [])
            note = " (it exists in the sonic-utilities source but NOT on this device's SONiC version)" if src == "device" and static_has else ""
            return (f"'{path}' has no subcommand '{tok}' {where}{note}."
                    + (f" Did you mean: {', '.join(near)}?" if near else "") + f" Valid: {', '.join(kids[:30])}")
        break  # leaf: rest are arguments
    if not _on_device(path):
        return f"'{path}' is not available on this device."
    node = _static.get(path)
    if node and node["kind"] == "command" and node.get("opts_complete"):
        known = set(COMMON_FLAGS)
        for o in node["options"]:
            known.update(o["flags"])
        for tok in argv[i:]:
            if tok == "--":
                break
            m = _FLAG.match(tok)
            if m and tok.split("=")[0] not in known:
                return f"'{path}' has no option '{tok.split('=')[0]}'. Usage: {node['usage']}"
    return None


# ---------------------------------------------------------------- argument values vs. the command's definition
_IFACE_NAME = re.compile(r"^(?:Ethernet\d+(?:/\d+)*|Ethernet-(?:BP|IB|Rec)\d+|Vlan\d+|PortChannel\d+|Loopback\d+|"
                         r"eth\d+|[A-Za-z][A-Za-z0-9._/-]*\d)$")


def _arg_kind(a: dict):
    n = (a.get("name") or "").lower().strip("<>[]")
    if n in ("interface_name", "interfacename", "interface", "port", "port_name", "ifname", "intf") or n.endswith("_interface"):
        return "interface"
    if n in ("ip_addr", "ipaddr", "ip_address", "ip", "ip_prefix", "prefix_ip"):
        return "ip"
    if n in ("gw", "gateway", "nexthop", "next_hop"):
        return "gw"
    if n in ("vid", "vlan_id", "vlanid"):
        return "vid"
    if n in ("new_hostname", "hostname"):
        return "hostname"
    return None


def _is_ip(v: str, prefix_ok=True) -> bool:
    import ipaddress
    try:
        (ipaddress.ip_interface if prefix_ok else ipaddress.ip_address)(v)
        return True
    except ValueError:
        return False


def check_args(argv: list):
    """Positional values vs. the command's arguments (from its Click definition): count, and the kind of value
    for arguments whose meaning is clear from their name (interface, IP, gateway, VLAN id).
    None if fine or not checkable (e.g. options not fully known); else an explanation."""
    path, i = argv[0], 1
    while i < len(argv) and f"{path} {argv[i]}" in _static:
        path += " " + argv[i]; i += 1
    node = _static.get(path)
    if not node or not (node["kind"] == "command" or node.get("runnable")) or not node.get("opts_complete"):
        return None
    opts = {f: o for o in node.get("options", []) for f in o["flags"]}
    pos, rest = [], argv[i:]
    j = 0
    while j < len(rest):
        t = rest[j]
        if t == "--":
            pos += rest[j + 1:]; break
        if t.startswith("-") and not re.match(r"^-\d", t):
            o = opts.get(t.split("=")[0])
            if o is None:
                return None  # an option we don't know the arity of: don't guess
            j += 1 if (o["flag"] or "=" in t) else 2
            continue
        pos.append(t); j += 1
    args = node.get("args", [])
    need = [a for a in args if a.get("required")]
    usage = node.get("usage", path)
    if len(pos) < len(need):
        return f"'{path}' needs {len(need)} value(s), got {len(pos)}. Usage: {usage}"
    if not any(a.get("variadic") for a in args) and len(pos) > len(args):
        return f"'{path}' takes at most {len(args)} value(s), got {len(pos)} ({' '.join(pos)}). Usage: {usage}"
    for a, v in zip(args, pos):
        kind, name = _arg_kind(a), a.get("metavar") or a.get("name")
        if kind == "interface" and not _IFACE_NAME.match(v):
            return f"'{v}' is not an interface name for {name} (e.g. Ethernet0, Vlan20, PortChannel0001). Usage: {usage}"
        if kind == "ip" and not _is_ip(v):
            return f"'{v}' is not an IP address for {name}. Usage: {usage}"
        if kind == "gw" and not _is_ip(v, prefix_ok=False):
            return f"'{v}' is not a gateway address for {name}. Usage: {usage}"
        if kind == "hostname":
            from .resolve import HOSTNAME_RE, HOSTNAME_RULE
            if not HOSTNAME_RE.fullmatch(v):
                return f"'{v}' is not a valid hostname for {name} ({HOSTNAME_RULE}). Usage: {usage}"
        if kind == "vid" and not (v.isdigit() and 1 <= int(v) <= 4094):
            return f"'{v}' is not a VLAN id (1-4094) for {name}. Usage: {usage}"
    return None


# ---------------------------------------------------------------- the model's `ref` tool
def _src_grep(pattern: str) -> str:
    try:
        re.compile(pattern)
    except re.error as e:
        return f"bad regex: {e}"
    try:
        root = src_root()
        p = subprocess.run(["grep", "-rnIE", "-m", "4", "--include=*.py", "--include=*.md", "--", pattern, root],
                           capture_output=True, text=True, timeout=10)
    except Exception as e:
        return f"[grep failed: {e}]"
    lines = [ln.replace(root + "/", "")[:200] for ln in p.stdout.splitlines()][:25]
    return "\n".join(lines) or f"NOT FOUND in the SONiC CLI source: {pattern}"


# ---------------------------------------------------------------- deep search: the native tool for a second look
def _by_source():
    """file -> sorted [(line, command path)] from each command's recorded source location."""
    idx = {}
    for path, n in _static.items():
        src = n.get("source", "")
        if (n["kind"] == "command" or n.get("runnable")) and ":" in src:
            f, _, line = src.rpartition(":")
            if line.isdigit():
                idx.setdefault(f, []).append((int(line), path))
    for v in idx.values():
        v.sort()
    return idx


def source_search(words: str, exclude=(), k: int = 8) -> list:
    """Search the CLI source itself (docstrings, option help, code), not just the command names and first help line.
    A hit on line L of a file belongs to the command defined closest above L. Commands hit by more of the
    words rank first. Returns command paths."""
    import bisect
    keys = sorted({w for w in re.findall(r"[a-z0-9_-]+", words.lower())
                   if len(w) >= 3 and w not in _STOPW and not w.isdigit()})[:8]
    if not keys:
        return []
    root, idx = src_root(), _by_source()
    hits = {}
    for key in keys:
        try:
            p = subprocess.run(["grep", "-rniF", "--include=*.py", "--", key] + [os.path.join(root, t) for t in ("config", "show")],
                               capture_output=True, text=True, timeout=15)
        except Exception:
            continue
        for ln in p.stdout.splitlines()[:2000]:
            f, _, rest = ln.partition(":")
            line = rest.split(":", 1)[0]
            rel = os.path.relpath(f, root)
            defs = idx.get(rel)
            if not defs or not line.isdigit():
                continue
            i = bisect.bisect_right(defs, (int(line), "\uffff")) - 1
            if i >= 0:
                hits.setdefault(defs[i][1], set()).add(key)
    ranked = sorted(hits.items(), key=lambda kv: (-len(kv[1]), len(kv[0])))
    return [p for p, _ in ranked if p not in exclude and _on_device(p) and (not _curated or p in _curated)][:k]


def deep_search(words: str, request: str, exclude=(), k: int = 8) -> list:
    """Second look when the first reference didn't fit: the ranked index for the model's keywords, then the
    source itself. Commands already shown are left out, so the model sees new candidates."""
    out = []
    for p in search_paths(words, k) + search_paths(request, k * 2) + source_search(words + " " + request, exclude, k):
        if p not in exclude and p not in out:
            out.append(p)
    return out[:k]


def ref(spec: str) -> str:
    """ref find <words> | ref show <command path> | ref src <regex>"""
    if not available():
        return "The command reference is not available in this build, so commands cannot be verified."
    verb, _, rest = spec.strip().partition(" ")
    rest = " ".join(rest.split())
    verb = verb.lower()
    if verb == "list":
        if not _curated:
            return "No command reference file loaded (CMDREF in dial0.conf)."
        out = [f"Commands from {_curated_meta.get('file')} (only these are used):"]
        for kind, title in (("show", "Set 1: show commands (read-only)"), ("config", "Set 2: config commands (change the switch)")):
            out.append(f"\n{title}")
            areas = {}
            for p, d in _curated.items():
                if d["kind"] == kind:
                    areas.setdefault(d["area"], []).append(p)
            for area, paths in areas.items():
                out.append(f"  {area}:")
                out += [f"    {p}" for p in paths]
        return "\n".join(out)
    if verb == "find":
        return search(rest, 8) or f"NOT FOUND: no SONiC command matches '{rest}'."
    if verb == "src":
        return _src_grep(rest)
    if verb == "show" and _curated:
        if rest not in _curated:
            near = curated_path(rest.split())
            return (f"NOT IN YOUR COMMAND REFERENCE: '{rest}'. Only the commands in {_curated_meta.get('file')} are used."
                    + (f" Closest: {near}" if near else ""))
        d, n = _curated[rest], _static.get(rest)
        out = [f"{rest}   ({d['kind']}, {d['area']})"] + (["  usage: " + _usage(rest)] if n else []) + \
              ["  example: " + e + (f"   ({d['notes'][e]})" if d.get("notes", {}).get(e) else "") for e in d["examples"]]
        return "\n".join(out)[:2000]
    if verb == "show":
        n = _static.get(rest)
        if n is None and rest not in _live:
            near = difflib.get_close_matches(rest, list(_static) + [p for p in _live if p not in _static], n=5, cutoff=0.6)
            return f"NOT FOUND in the SONiC reference: '{rest}'." + (f" Closest: {'; '.join(near)}" if near else "")
        out = [_usage(rest) if (n or {}).get("kind") != "group" else f"{rest}  (group)"]
        if _help(rest):
            out.append(_help(rest))
        doc = _cmdref.get(rest)
        if doc:
            if doc.get("desc") and doc["desc"] != _help(rest):
                out.append("Command Reference: " + doc["desc"])
            for ex in doc.get("examples", []):
                out.append("  example: " + ex)
        for o in (n or {}).get("options", []):
            out.append(f"  {', '.join(o['flags'])}" + ("" if o["flag"] else " <value>") + (f"  {o['help']}" if o["help"] else ""))
        kids, _ = _children(rest)
        if kids:
            out.append("subcommands: " + ", ".join(kids[:40]))
        if n:
            out.append(f"source: {n['source']}")
        if not _on_device(rest):
            out.append("NOTE: not present on this device's SONiC version")
        return "\n".join(out)[:2000]
    return "usage: list | find <words> | show <command path> | src <regex>"
