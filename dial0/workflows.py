"""Workflows: fixed, read-only check sequences (no model call) that can be run on demand or on a schedule.
Each run's result is kept on the switch: the last WORKFLOW_KEEP (default 10) per workflow, adjustable per workflow.

    state dir/workflows.json             schedules and per-workflow retention
    state dir/workflow-results/<name>/   one JSON file per run (oldest deleted beyond the retention)
"""
import datetime, json, os, re, threading, time
from . import health, cve, security

STATE = os.getenv("DIAL0_STATE_DIR", "/var/lib/dial0")
CONF = os.path.join(STATE, "workflows.json")
RESULTS = os.path.join(STATE, "workflow-results")
DEFAULT_KEEP = int(os.getenv("DIAL0_WORKFLOW_KEEP", "10"))
MIN_EVERY = 5 * 60          # don't let a schedule hammer the switch
MAX_KEEP = 500
TICK_S = float(os.getenv("DIAL0_WORKFLOW_TICK", "30"))
INSIGHTS = os.getenv("DIAL0_WORKFLOW_INSIGHTS", "on") != "off"  # model analysis when a result isn't healthy

REGISTRY = {
    "health": {"title": "Switch health", "checks": None,
               "desc": "everything below, as one report"},
    "logs": {"title": "System log", "checks": [health.logs, health.cores],
             "desc": "errors/warnings in the system log (grep) and crash dumps"},
    "interfaces": {"title": "Interfaces", "checks": [health.interfaces], "desc": "enabled ports that are down"},
    "bgp": {"title": "BGP", "checks": [health.bgp], "desc": "BGP neighbors that are not established"},
    "services": {"title": "Services", "checks": [health.system_ready, health.system_health, health.containers],
                 "desc": "system ready status, system health and the containers of enabled features"},
    "hardware": {"title": "Hardware", "checks": [health.ssd, health.fans, health.transceivers],
                 "desc": "SSD health, fans, transceiver status"},
    "counters": {"title": "Interface counters", "checks": [health.counters],
                 "desc": "RX/TX error, drop and overrun counters per interface"},
    "resources": {"title": "Resources", "checks": [health.resources], "desc": "disk, memory and load"},
    "security": {"title": "Security audit", "checks": None, "runner": security.run,
                 "desc": "configuration audit (passwords, accounts, SSH, exposed services, SNMP, ACLs, logging...) + CVE scan"},
    "cve": {"title": "CVE scan", "checks": None, "runner": cve.run_scan,
            "desc": "known vulnerabilities (CVEs) in the host's and every SONiC container's Debian packages"},
}

_lock = threading.Lock()


class WorkflowError(Exception):
    pass


# ------------------------------------------------------------------ settings (schedules + retention)
def _load() -> dict:
    try:
        with open(CONF) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("schedules", {}); d.setdefault("keep", {})
    return d


def _save(d: dict):
    os.makedirs(STATE, exist_ok=True)
    tmp = CONF + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, CONF)


def _check_name(name: str):
    if name not in REGISTRY:
        raise WorkflowError(f"no workflow '{name}'. Workflows: {', '.join(REGISTRY)}")


def keep_of(name: str, d=None) -> int:
    return int((d or _load())["keep"].get(name, DEFAULT_KEEP))


def set_keep(name: str, n: int) -> int:
    _check_name(name)
    if not 1 <= int(n) <= MAX_KEEP:
        raise WorkflowError(f"keep must be 1-{MAX_KEEP}")
    with _lock:
        d = _load(); d["keep"][name] = int(n); _save(d)
    _prune(name, int(n))
    return int(n)


def parse_every(text: str) -> int:
    """'30m', '2h', '1d', '45 min' -> seconds (at least 5 minutes)."""
    m = re.fullmatch(r"\s*(\d+)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)\s*", text.lower())
    if not m:
        raise WorkflowError("interval must look like 30m, 2h or 1d")
    sec = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)[0]]
    if sec < MIN_EVERY:
        raise WorkflowError("the shortest interval is 5m")
    return sec


def parse_at(text: str) -> str:
    m = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", text)
    if not m:
        raise WorkflowError("time must look like 06:00 (24h, this switch's time zone)")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def schedule(name: str, every: str = "", at: str = "", keep=None) -> dict:
    """Schedule a workflow `every` 30m/2h/1d or daily `at` HH:MM, optionally setting how many results to keep."""
    _check_name(name)
    if bool(every) == bool(at):
        raise WorkflowError("give either 'every <interval>' or 'daily <HH:MM>'")
    sch = {"every": parse_every(every)} if every else {"at": parse_at(at)}
    sch["created"] = time.time()
    with _lock:
        d = _load()
        old = d["schedules"].get(name, {})
        sch["last_run"] = old.get("last_run")
        d["schedules"][name] = sch
        if keep is not None:
            if not 1 <= int(keep) <= MAX_KEEP:
                raise WorkflowError(f"keep must be 1-{MAX_KEEP}")
            d["keep"][name] = int(keep)
        _save(d)
    if keep is not None:
        _prune(name, int(keep))
    return sch


def unschedule(name: str) -> bool:
    _check_name(name)
    with _lock:
        d = _load()
        had = d["schedules"].pop(name, None) is not None
        _save(d)
    return had


def describe(sch: dict) -> str:
    if not sch:
        return "not scheduled"
    if sch.get("every"):
        s = sch["every"]
        n, u = (s // 86400, "d") if s % 86400 == 0 else (s // 3600, "h") if s % 3600 == 0 else (s // 60, "m")
        return f"every {n}{u}"
    return f"daily {sch['at']}"


def next_due(sch: dict, now=None) -> float:
    """When a schedule should run next (epoch seconds). A new interval schedule runs right away."""
    now = time.time() if now is None else now
    last = sch.get("last_run")
    if sch.get("every"):
        return (last + sch["every"]) if last else now
    hh, mm = map(int, sch["at"].split(":"))
    after = datetime.datetime.fromtimestamp(last or sch.get("created", now))
    nxt = after.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= after:
        nxt += datetime.timedelta(days=1)
    return nxt.timestamp()


# ------------------------------------------------------------------ results
def _dir(name: str) -> str:
    return os.path.join(RESULTS, name)


def _files(name: str) -> list:
    try:
        return sorted((f for f in os.listdir(_dir(name)) if f.endswith(".json")), reverse=True)
    except FileNotFoundError:
        return []


def _prune(name: str, keep: int):
    for f in _files(name)[keep:]:
        try:
            os.remove(os.path.join(_dir(name), f))
        except OSError:
            pass


def format_insights(ins: dict) -> str:
    """The model's insights as report lines (summary, likely causes, next steps), or why they're unavailable."""
    if not ins:
        return ""
    if ins.get("error"):
        return "Insights (SLM): unavailable: " + ins["error"]
    out = ["Insights (SLM, from the findings above):", "  " + ins.get("summary", "")]
    if ins.get("causes"):
        out += ["  Likely causes:"] + [f"   - {c}" for c in ins["causes"]]
    if ins.get("next_steps"):
        out += ["  Next steps:"] + [f"   - {c}" for c in ins["next_steps"]]
    return "\n".join(out)


def run(name: str, step=lambda m, d="": None, trigger: str = "manual", analyse=None) -> dict:
    """Run a workflow now, keep its result (pruning beyond the retention), return it.
    analyse(name, report, findings, log_excerpt) -> insights dict: the model's analysis, asked only when
    something isn't healthy (a healthy result has nothing to analyse, so no model call)."""
    _check_name(name)
    wf = REGISTRY[name]
    t0 = time.time()
    raws, extra = {}, {}
    if wf.get("runner"):
        report, overall, findings, extra = wf["runner"](step)
        evidence = extra.get("excerpt", "")
    else:
        report, overall, findings = health.run(step, checks=wf["checks"], title=wf["title"], raws=raws)
        evidence = health.log_excerpt(raws.get("Logs", ""))
    insights = None
    if analyse and overall != "HEALTHY":
        try:
            insights = analyse(name, report, findings, evidence)
        except Exception as e:  # analysis is a bonus: the checks' result stands either way
            insights = {"error": str(e)[:200]}
        if insights:
            tail = "\nRaw outputs: dial0 why" if "\nRaw outputs: dial0 why" in report else ("\n  Full list: dial0 cve list" if "\n  Full list: dial0 cve list" in report else "\nFull list: dial0 cve list" if "\nFull list: dial0 cve list" in report else "")
            report = report.replace(tail, "") + "\n" + format_insights(insights) + tail
    res = {"workflow": name, "ts": t0, "trigger": trigger, "overall": overall, "seconds": round(time.time() - t0, 1),
           "findings": [{"level": l, "check": t, "message": m} for l, t, m in findings], "report": report,
           "insights": insights, "items": extra.get("items", [])[:3000]}
    os.makedirs(_dir(name), exist_ok=True)
    fn = os.path.join(_dir(name), time.strftime("%Y%m%d-%H%M%S", time.localtime(t0)) + f"-{int(t0 * 1000) % 1000:03d}.json")
    with open(fn, "w") as f:
        json.dump(res, f)
    _prune(name, keep_of(name))
    return res


def results(name: str) -> list:
    """Kept results, newest first: summaries only."""
    _check_name(name)
    out = []
    for f in _files(name):
        try:
            with open(os.path.join(_dir(name), f)) as fh:
                r = json.load(fh)
        except (OSError, ValueError):
            continue
        bad = [x for x in r["findings"] if x["level"] in ("crit", "warn")]
        out.append({"ts": r["ts"], "overall": r["overall"], "trigger": r.get("trigger", ""), "seconds": r.get("seconds"),
                    "issues": [f"{x['check']}: {x['message']}"[:120] for x in bad][:4],
                    "insight": ((r.get("insights") or {}).get("summary") or "")[:160]})
    return out


def result(name: str, n: int = 1):
    """The n-th newest full result (1 = latest), or None."""
    _check_name(name)
    files = _files(name)
    if not 1 <= n <= len(files):
        return None
    with open(os.path.join(_dir(name), files[n - 1])) as f:
        return json.load(f)


def clear(name: str) -> int:
    """Delete the kept results of one workflow, or of all with 'all'. -> how many were deleted."""
    names = list(REGISTRY) if name == "all" else [name]
    if name != "all":
        _check_name(name)
    n = 0
    for nm in names:
        for f in _files(nm):
            try:
                os.remove(os.path.join(_dir(nm), f)); n += 1
            except OSError:
                pass
    return n


def overview() -> list:
    """Every workflow with its schedule, next run, retention and latest result (for `dial0 workflows`)."""
    d = _load()
    out = []
    for name, wf in REGISTRY.items():
        sch = d["schedules"].get(name)
        latest = (results(name) or [None])[0]
        out.append({"name": name, "desc": wf["desc"], "schedule": describe(sch) if sch else "",
                    "next": next_due(sch) if sch else None, "keep": keep_of(name, d), "kept": len(_files(name)),
                    "latest": latest})
    return out


# ------------------------------------------------------------------ scheduler
def tick(now=None, guard=None, analyse=None) -> list:
    """Run every workflow that is due. guard: a lock held while running (the agent's, so a scheduled run never
    overlaps a request). -> names run."""
    now = time.time() if now is None else now
    ran = []
    for name, sch in list(_load()["schedules"].items()):
        if name not in REGISTRY or next_due(sch, now) > now:
            continue
        if guard is not None:
            with guard:
                run(name, trigger="schedule", analyse=analyse)
        else:
            run(name, trigger="schedule", analyse=analyse)
        with _lock:
            d = _load()
            if name in d["schedules"]:
                d["schedules"][name]["last_run"] = now
                _save(d)
        ran.append(name)
    return ran


def start_scheduler(guard=None, delay: float = 60.0, analyse=None):
    """Background thread: first check after `delay` (let the model finish loading), then every TICK_S seconds."""
    def loop():
        time.sleep(delay)
        while True:
            try:
                tick(guard=guard, analyse=analyse)
            except Exception as e:  # a failing run must not stop the scheduler
                print(f"workflow scheduler: {e}", flush=True)
            time.sleep(TICK_S)
    threading.Thread(target=loop, daemon=True).start()
