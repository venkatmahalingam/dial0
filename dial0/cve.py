"""CVE scan: every installed Debian package (the host, which includes the kernel, and each SONiC container) checked
against the Debian Security Tracker; actively exploited CVEs flagged from CISA's Known Exploited Vulnerabilities.

Matching is done by code, never by the model: installed version older than Debian's fixed version -> vulnerable
(fix available); status open -> vulnerable, no fix yet; 'unimportant' -> not reported. Versions are compared with
dpkg's exact rules (debver.py). Data is downloaded by the switch and refreshed when older than DIAL0_CVE_MAX_AGE_H."""
import gzip, json, os, re, shutil, time, urllib.request
from . import tools, debver

STATE = os.getenv("DIAL0_STATE_DIR", "/var/lib/dial0")
DIR = os.path.join(STATE, "cve")
TRACKER_URL = os.getenv("DIAL0_CVE_TRACKER_URL", "https://security-tracker.debian.org/tracker/data/json")
KEV_URL = os.getenv("DIAL0_CVE_KEV_URL", "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json")
MAX_AGE_H = float(os.getenv("DIAL0_CVE_MAX_AGE_H", "24"))
OS_RELEASE = os.getenv("DIAL0_OS_RELEASE", "/etc/os-release")
OWN = os.getenv("DIAL0_CONTAINER_NAME", "dial0")
TOP = int(os.getenv("DIAL0_CVE_TOP", "8"))
DPKG_FMT = "${binary:Package}\\t${Version}\\t${source:Package}\\t${source:Version}\\n"
URGENCY = {"high": 4, "medium": 3, "not yet assigned": 2, "low": 1}
SONIC_SRC = re.compile(r"^(sonic|swss|syncd|libsai|sai|libswsscommon|frr|libyang|python3?-sonic|sflow|libteam|redis-)", re.I)


# ------------------------------------------------------------------ data (download / refresh)
def _path(name):
    return os.path.join(DIR, name)


def data_age_h(name="tracker.json"):
    try:
        return (time.time() - os.path.getmtime(_path(name))) / 3600
    except OSError:
        return None


def _download(url, dest, timeout=300):
    os.makedirs(DIR, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "dial0-cve", "Accept-Encoding": "gzip"})
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    with open(tmp, "rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    if gz:
        with gzip.open(tmp, "rb") as src, open(tmp + ".u", "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        os.replace(tmp + ".u", tmp)
    with open(tmp, "rb") as f:
        if f.read(1) not in (b"{", b"["):
            os.remove(tmp)
            raise ValueError(f"{url} did not return JSON")
    os.replace(tmp, dest)


def refresh(force=False, step=lambda m, d="": None) -> str:
    """Download the Debian tracker and the KEV list if missing, older than MAX_AGE_H, or forced.
    On failure the previous data is kept and used. -> a status line."""
    age = data_age_h()
    if not force and age is not None and age < MAX_AGE_H:
        return f"CVE data is {age:.0f}h old (refreshed after {MAX_AGE_H:.0f}h)"
    msgs = []
    for url, name in ((TRACKER_URL, "tracker.json"), (KEV_URL, "kev.json")):
        step(f"Downloading {'the Debian Security Tracker' if name == 'tracker.json' else 'the CISA known-exploited list'} ({url})")
        try:
            _download(url, _path(name))
            msgs.append(f"{name} updated")
        except Exception as e:
            msgs.append(f"{name} download failed ({str(e)[:100]})" + (", using the previous copy" if os.path.exists(_path(name)) else ""))
    return "; ".join(msgs)


def load_tracker(needed: set) -> dict:
    """Only the installed source packages' entries, read one package at a time (the file is tens of MB:
    loading it whole would cost about a gigabyte of RAM next to the model)."""
    dec = json.JSONDecoder()
    with open(_path("tracker.json"), encoding="utf-8") as f:
        text = f.read()
    out, i, n = {}, text.index("{") + 1, len(text)
    ws = " \t\r\n,"
    while i < n:
        while i < n and text[i] in ws:
            i += 1
        if i >= n or text[i] == "}":
            break
        key, i = dec.raw_decode(text, i)
        while text[i] in " \t\r\n:":
            i += 1
        val, i = dec.raw_decode(text, i)
        if key in needed:
            out[key] = val
    return out


def load_kev() -> set:
    try:
        with open(_path("kev.json")) as f:
            return {v.get("cveID") for v in json.load(f).get("vulnerabilities", [])}
    except (OSError, ValueError):
        return set()


# ------------------------------------------------------------------ inventory
def _codename(os_release: str) -> str:
    m = re.search(r"^VERSION_CODENAME=\"?(\w+)", os_release, re.M)
    if m:
        return m.group(1)
    m = re.search(r"^VERSION_ID=\"?(\d+)", os_release, re.M)
    return {"10": "buster", "11": "bullseye", "12": "bookworm", "13": "trixie"}.get(m.group(1) if m else "", "")


def _packages(dpkg_out: str) -> dict:
    """source package -> source version (lowest, if binaries of one source differ)."""
    pk = {}
    for line in dpkg_out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        binary, bver, src, sver = parts[:4]
        src = (src or binary).split(" ")[0]
        ver = sver or bver
        if src and ver and (src not in pk or debver.compare(ver, pk[src]) < 0):
            pk[src] = ver
    return pk


def inventory(step=lambda m, d="": None) -> list:
    """Installed Debian packages of the host and of every running SONiC container (not Dial 0's own), each with its
    Debian release. -> [{scope, release, packages: {source: version}}]."""
    scopes = []
    code, rel = tools.split_result(tools._run(["cat", OS_RELEASE], max_out=20000))
    code2, pk = tools.split_result(tools._run(["dpkg-query", "-W", "-f=" + DPKG_FMT], max_out=5_000_000))
    if code2 == 0:
        scopes.append({"scope": "host", "release": _codename(rel), "packages": _packages(pk)})
    code, names = tools.split_result(tools._run(["docker", "ps", "--format", "{{.Names}}"], max_out=20000))
    for name in [n.strip() for n in names.splitlines() if n.strip() and n.strip() != "(no output)"]:
        if name == OWN or name.startswith(OWN):
            continue  # Dial 0's own container isn't part of SONiC
        code, out = tools.split_result(tools._run(
            ["docker", "exec", name, "sh", "-c", f"cat /etc/os-release; echo ---DPKG---; dpkg-query -W -f='{DPKG_FMT}'"],
            max_out=5_000_000))
        rel, _, pk = out.partition("---DPKG---")
        if code == 0 and pk.strip():
            scopes.append({"scope": name, "release": _codename(rel), "packages": _packages(pk)})
        else:
            scopes.append({"scope": name, "release": "", "packages": {}, "error": out.strip().splitlines()[-1:] or ["?"]})
    step("CVE scan: inventory: " + ", ".join(f"{s['scope']} ({s['release'] or '?'}, {len(s['packages'])} packages)"
                                             for s in scopes[:6]) + (f" and {len(scopes) - 6} more" if len(scopes) > 6 else ""))
    return scopes


# ------------------------------------------------------------------ matching + report
def sonic_built(src: str, ver: str) -> bool:
    return bool(SONIC_SRC.match(src)) or "sonic" in ver.lower()


def _upstream(ver: str) -> str:
    m = re.match(r"(\d+(?:\.\d+)+)", ver.split(":")[-1])
    return m.group(1) if m else ""


def sonic_check(scopes: list, tracker: dict, kev: set) -> list:
    """SONiC's own builds (frr, ...) aren't Debian packages, but Debian tracks the same upstream projects. Compare by
    UPSTREAM version (8.5.4-sonic-0 -> 8.5.4): a CVE Debian fixed in a later upstream release may affect this build.
    Approximate: SONiC may have backported the fix without changing the version."""
    out = {}
    for sc in scopes:
        for src, ver in sc["packages"].items():
            if not sonic_built(src, ver) or src not in tracker:
                continue
            up = _upstream(ver)
            if not up:
                continue
            for cve, e in tracker[src].items():
                if not cve.startswith("CVE-"):
                    continue
                rels = e.get("releases") or {}
                r = rels.get(sc["release"]) or rels.get("sid") or {}
                urg = (r.get("urgency") or "").replace("*", "").strip() or "not yet assigned"
                if urg in ("unimportant", "end-of-life") or r.get("status") not in ("resolved", "open"):
                    continue
                fix_up = _upstream(r.get("fixed_version") or "") if r.get("status") == "resolved" else ""
                if r.get("status") == "resolved" and (not fix_up or debver.compare(up, fix_up) >= 0):
                    continue
                it = out.setdefault((cve, src), {"cve": cve, "package": src, "installed": ver, "fixed_upstream": fix_up or None,
                                                 "urgency": urg, "kev": cve in kev, "scopes": []})
                if sc["scope"] not in it["scopes"]:
                    it["scopes"].append(sc["scope"])
    return sorted(out.values(), key=lambda x: (not x["kev"], -URGENCY.get(x["urgency"], 2), x["cve"]))


def match(scopes: list, tracker: dict, kev: set):
    """-> (items, stats). One item per (CVE, source package), with every scope where it applies."""
    items, stats = {}, {"unimportant": 0, "undetermined": 0}
    for sc in scopes:
        rel = sc["release"]
        for src, ver in sc["packages"].items():
            if sonic_built(src, ver):
                continue  # SONiC's own build: checked separately, by upstream version (sonic_check)
            for cve, e in tracker.get(src, {}).items():
                if not cve.startswith("CVE-"):
                    continue
                r = (e.get("releases") or {}).get(rel)
                if not r:
                    continue
                urg = (r.get("urgency") or "").replace("*", "").strip() or "not yet assigned"
                if urg in ("unimportant", "end-of-life"):
                    stats["unimportant"] += 1
                    continue
                status, fixed = r.get("status"), r.get("fixed_version")
                if status == "resolved":
                    if not fixed or fixed == "0" or debver.compare(ver, fixed) >= 0:
                        continue
                elif status == "open":
                    fixed = None
                else:
                    stats["undetermined"] += 1
                    continue
                # one row per CVE, package AND release: each Debian release has its own fixed version and urgency
                it = items.setdefault((cve, src, rel), {"cve": cve, "package": src, "release": rel, "installed": {},
                                                        "fixed": fixed, "urgency": urg, "kev": cve in kev, "scopes": [],
                                                        "description": (e.get("description") or "")[:240]})
                it["scopes"].append(sc["scope"]); it["installed"][sc["scope"]] = ver
                if URGENCY.get(urg, 2) > URGENCY.get(it["urgency"], 2):
                    it["urgency"] = urg
    ranked = sorted(items.values(), key=lambda x: (not x["kev"], -URGENCY.get(x["urgency"], 2), x["fixed"] is None, x["cve"]))
    return ranked, stats


def _row(x) -> str:
    vers = sorted(set(x["installed"].values()))
    where = ", ".join(x["scopes"][:4]) + (f" +{len(x['scopes']) - 4}" if len(x["scopes"]) > 4 else "")
    fix = f"fixed in {x['fixed']}" if x["fixed"] else "no fix yet"
    return f"{x['cve']:<16} {x['package']} {'/'.join(vers)} -> {fix}  [{x['urgency']}]  ({where}; {x.get('release', '?')})"


def run_scan(step=lambda m, d="": None):
    """-> (report, overall, findings, extra) for the 'cve' workflow."""
    step("CVE scan: checking the vulnerability data (Debian Security Tracker + CISA known-exploited list)")
    note = refresh(step=step)
    if not os.path.exists(_path("tracker.json")):
        f = [("info", "CVE data", "no vulnerability data yet: the switch couldn't download it (" + note + "). "
              "Check internet/proxy access (HTTPS_PROXY in dial0.conf), then run: dial0 cve update")]
        return "CVE scan: NOT RUN\n  [info] " + f[0][2], "HEALTHY", f, {"items": []}
    scopes = inventory(step)
    needed = {p for s in scopes for p in s["packages"]}
    tracker = load_tracker(needed)
    kev = load_kev()
    items, stats = match(scopes, tracker, kev)
    step(f"CVE scan: {len(items)} vulnerable (CVE, package) pairs", "\n".join(_row(x) for x in items[:400]))
    kev_items = [x for x in items if x["kev"]]
    high = [x for x in items if not x["kev"] and x["urgency"] == "high"]
    rest = [x for x in items if not x["kev"] and x["urgency"] != "high"]
    findings = []
    if kev_items:
        findings.append(("crit", "Known exploited (CISA KEV)", f"{len(kev_items)} CVE(s) being actively exploited:\n         "
                         + "\n         ".join(_row(x) for x in kev_items[:TOP])))
    if high:
        nf = sum(1 for x in high if x["fixed"])
        findings.append(("warn", "High urgency", f"{len(high)} CVE(s) ({nf} with a fix, {len(high) - nf} not yet):\n         "
                         + "\n         ".join(_row(x) for x in high[:TOP])))
    if rest:
        nf = sum(1 for x in rest if x["fixed"])
        findings.append(("info", "Medium/low/unassigned", f"{len(rest)} CVE(s), {nf} with a fix available"))
    if not items:
        findings.append(("ok", "Vulnerabilities", "no known vulnerable package versions found"))
    sonic = sorted({f"{p} {v}" for s in scopes for p, v in s["packages"].items() if sonic_built(p, v)})
    sitems = sonic_check(scopes, tracker, kev)
    if sitems:
        rows = [f"{x['cve']:<16} {x['package']} {x['installed']} -> " + (f"fixed upstream in {x['fixed_upstream']}" if x["fixed_upstream"]
                else "no upstream fix in Debian's data") + f"  [{'KEV ' if x['kev'] else ''}{x['urgency']}]  ({', '.join(x['scopes'][:4])})"
                for x in sitems[:TOP]]
        findings.append(("info", "SONiC-built packages (approximate)", f"{len(sitems)} CVE(s) may affect SONiC's own builds, "
                         "judged by upstream version (SONiC may have backported fixes; confirm with SONiC's advisories):\n         "
                         + "\n         ".join(rows)))
    elif sonic:
        findings.append(("info", "SONiC-built packages", f"{len(sonic)} SONiC builds; none older than an upstream CVE fix "
                         "in Debian's data: " + ", ".join(sonic[:6]) + (" ..." if len(sonic) > 6 else "")))
    bad_scopes = [s["scope"] for s in scopes if s.get("error")]
    age, kage = data_age_h(), data_age_h("kev.json")
    findings.append(("info", "Scanned", f"{len(scopes)} place(s): " + ", ".join(
        f"{s['scope']} ({s['release'] or '?'})" for s in scopes[:8]) + (" ..." if len(scopes) > 8 else "")
        + (f"; couldn't read: {', '.join(bad_scopes)}" if bad_scopes else "")
        + f". Debian data {age:.0f}h old" + (f", KEV {kage:.0f}h old" if kage is not None else ", KEV list unavailable")
        + f"; {stats['unimportant']} unimportant and {stats['undetermined']} undetermined not counted"))
    overall = "CRITICAL" if kev_items else "WARNING" if high else "HEALTHY"
    order = {"crit": 0, "warn": 1, "ok": 2, "info": 3}
    label = {"crit": "[CRIT]", "warn": "[warn]", "ok": "[ok]  ", "info": "[info]"}
    findings.sort(key=lambda f: order[f[0]])
    head = f"CVE scan: {overall}" + (f" ({len(kev_items)} known exploited, {len(high)} high urgency, {len(rest)} other)" if items else "")
    body = [f"  {label[l]} {t}: {m}" for l, t, m in findings]
    fix = ("Fixes on SONiC come with a newer SONiC image (or your vendor's update) built with the fixed package versions; "
           "upgrading single packages with apt isn't the supported way.") if items else ""
    report = "\n".join([head] + body + ([fix] if fix else []) + ["Full list: dial0 cve list"])
    excerpt = "\n".join(f"{_row(x)} :: {x['description']}" for x in (kev_items + high)[:12])
    return report, overall, findings, {"items": items, "sonic_items": sitems, "excerpt": excerpt}
