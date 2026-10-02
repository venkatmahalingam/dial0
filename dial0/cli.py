"""dial0 - talk to the on-box agent.

  dial0                      the dial0> prompt: type requests one after another, context on (/help inside; /exit to leave)
  dial0 -s NAME | --new      the prompt in a separate named session | starting fresh
  dial0 "request"            one request: plain English or a SONiC command (fresh, no context), then back to the shell
  dial0 "show vlan brief"    a real command runs directly (no model call)
  dial0 -e "is Ethernet4 up?"   also explain the output in words (1 more model call)
  dial0 -a "why is BGP down?"   agent mode: step-by-step, reads output between steps (slower; troubleshooting)
  dial0 -q ...               quiet: don't show the steps (shown live by default)
  dial0 why [-s NAME]        show the steps of the last request again (with details)
  dial0 -t ...               show where the time went     -v  steps with details + timing
  dial0 --context ...        let the model see earlier requests and the commands they ran (default: fresh, no context)
  dial0 -s NAME ... | -c ... named session | continue the most recent session (both include context; --no-context to skip)
  dial0 [-s NAME] resume     approve/deny commands left waiting
  dial0 sessions | context | history | reset [-s NAME] | rm NAME
  dial0 reset --all [-y]     clear ALL sessions: context, command history, learned requests, prompt history
  dial0 health               health check: system health, containers, ports, BGP, resources, log errors (no model)
  dial0 workflows            list workflows (health, logs, interfaces...), their schedules and last results
  dial0 security             security audit: passwords, accounts, SSH, exposed services, SNMP, ACLs, logging + CVE scan
  dial0 blueprints           list configuration blueprints (templates)
  dial0 blueprint apply NAME [key=value ...]   answer a few questions, review the full config, apply after y/N
  dial0 blueprint show NAME  the configuration your saved answers would generate (nothing applied)
  dial0 cve | cve update | cve list [all]   CVE scan of the host + SONiC containers | refresh the data | full list
  dial0 workflow run NAME | schedule NAME every 30m|daily 06:00 [--keep N] | unschedule NAME
                 results NAME | show NAME [N] | keep NAME N | clear NAME|all
  dial0 ref list             the commands Dial 0 uses (from your command reference file)
  dial0 ref find <words> | ref show <command path> | ref src <regex>
  dial0 plans [clear]        requests Dial 0 has learned (answered next time without the model)
"""
import json, os, re, sys, threading, time, uuid, urllib.error, urllib.parse, urllib.request
from .sessions import PROMPT_SESSION, UNNAMED

BASE = f"http://{os.getenv('DIAL0_API_HOST', '127.0.0.1')}:{os.getenv('DIAL0_API_PORT', '8090')}"
TOKEN = os.getenv("DIAL0_API_TOKEN", "")


def call(path, obj=None):
    """One request to the agent API (JSON in, JSON out). Exits with a clear message if the agent isn't running."""
    h = {"Content-Type": "application/json"}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    data = json.dumps(obj).encode() if obj is not None else None
    req = urllib.request.Request(BASE + path, data, h)
    try:
        return json.load(urllib.request.urlopen(req, timeout=3600))
    except urllib.error.HTTPError as e:
        return json.load(e)
    except urllib.error.URLError:
        sys.exit("dial0 agent is not reachable (still starting? try: dial0 ctl status)")


class Steps:
    """Shows the agent's steps live (stderr) while a request runs, by polling /progress.
    On a terminal, a ticker shows elapsed time during long waits (e.g. while the model reads the prompt)."""

    def __init__(self, verbose=False, quiet=False):
        self.verbose, self.quiet = verbose, quiet
        self.tty = sys.stderr.isatty()
        self.shown_header = False

    def show(self, step):
        """Print one step (with its detail lines when verbose)."""
        if self.quiet:
            return
        if not self.shown_header:
            print("What Dial 0 is doing:", file=sys.stderr)
            self.shown_header = True
        print(f"  [{step['t']:>6.1f}s] {step['msg']}", file=sys.stderr)
        if self.verbose and step.get("detail"):
            for line in step["detail"].rstrip().splitlines()[:20]:
                print(f"             | {line}", file=sys.stderr)

    def run(self, path, body):
        """POST body to path while showing progress; returns the response."""
        rid = uuid.uuid4().hex
        body = dict(body, rid=rid)
        if self.quiet:
            return call(path, body)
        box, done = {}, threading.Event()

        def worker():
            box["res"] = call(path, body)
            done.set()
        threading.Thread(target=worker, daemon=True).start()
        since, t0, ticking = 0, time.time(), False
        while True:
            finished = done.wait(1.0)
            try:
                p = call(f"/progress?rid={rid}&since={since}")
            except SystemExit:
                p = {"steps": [], "next": since}
            if ticking and (p["steps"] or finished):
                print("\r\033[K", end="", file=sys.stderr); ticking = False
            for st in p["steps"]:
                self.show(st)
            since = p.get("next", since)
            if finished:
                return box["res"]
            if self.tty:
                print(f"\r  ... working ({int(time.time() - t0)}s)", end="", file=sys.stderr, flush=True)
                ticking = True


def ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def show_timing(t):
    """Print where the time went (`-t`): total, model calls, prompt tokens and cache use."""
    if not t:
        return
    parts = [f"total {t.get('total_s', '?')}s"]
    if t.get("llm_calls"):
        parts.append(f"model: {t['llm_calls']} call(s), {t.get('llm_s', '?')}s")
        if t.get("prompt_tokens") is not None:
            parts.append(f"prompt {t.get('prompt_tokens')} tok in {t.get('prompt_s')}s (cached {t.get('cached_tokens')})")
            parts.append(f"generated {t.get('gen_tokens')} tok in {t.get('gen_s')}s")
    else:
        parts.append("model: not used")
    print("[time] " + " | ".join(parts), file=sys.stderr)


def show_result(res, verbose, timing, already=""):
    """Print what ran: each command with its output and status, then the answer."""
    results = res.get("results", [])
    for r in results:
        status = "" if r["exit"] == 0 else ("   [skipped]" if r["exit"] is None else f"   [exit {r['exit']}]")
        print(f"$ {r['command']}{status}")
        if r["exit"] is not None and r["output"].strip() and r["output"].strip() != "(no output)":
            print(r["output"].rstrip())
    failed = [r for r in results if r["exit"] not in (0, None)]
    skipped = [r for r in results if r["exit"] is None]
    if skipped:
        ok = [r["command"] for r in results if r["exit"] == 0]
        print(f"Stopped after a failed change; {len(skipped)} command(s) skipped."
              + (f" Already applied: {'; '.join(ok)}" if ok else ""))
    elif failed:
        print(f"{len(failed)} command(s) returned an error.")
    if res.get("answer") and res["answer"] != already:
        print(res["answer"])
    if res.get("error"):
        print("error: " + res["error"])
    if timing:
        show_timing(res.get("timing"))


def ask_yes_no(prompt: str) -> bool:
    """Only y/yes approves. n/no/Enter declines. Ctrl-C or end of input also declines (never leaves it hanging)."""
    while True:
        try:
            ans = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n(no answer: treated as No)")
            return False
        if ans in ("y", "yes"):
            return True
        if ans in ("", "n", "no"):
            return False
        print("Please answer y or n.")


def drive(res, session, verbose, timing, steps=None, ask_always=False):
    """Send a request and see it through: show steps live, ask y/N for pending changes (on a terminal), print results."""
    steps = steps or Steps(verbose)
    shown = ""
    while res.get("status") == "needs_confirmation":
        if res.get("results"):  # a fix round: show what ran and what failed before asking about the fix
            show_result({"results": res["results"]}, verbose, False)
        if res.get("note"):
            print(res["note"])
            shown = res["note"]
        cmds = res.get("commands") or [res["command"]]
        changes = set(res.get("changes") or cmds)
        if not sys.stdin.isatty() and not ask_always:
            print("Pending approval:\n" + "\n".join(f"  {c}" for c in cmds)
                  + f"\n(run `dial0 -s {session} resume` interactively to approve or deny)")
            return
        if res.get("mode") == "agent":
            print("Agent mode wants to change the switch:")
            for c in cmds:
                print(f"  * {c}")
            ok = ask_yes_no("Apply this and any further changes this request needs? [y/N] ")
        else:
            print(f"Will run on the switch ({len(cmds)} command{'s' if len(cmds) != 1 else ''}, all at once):")
            for c in cmds:
                print(f"  {'*' if c in changes else ' '} {c}")
            ok = ask_yes_no("Apply? (* = changes config) [y/N] ")
        res = steps.run("/confirm", {"session": session, "id": res["id"], "approve": ok})
    show_result(res, verbose, timing, shown)


def main():
    """The `dial0` command: parses options, then dispatches to a subcommand, the prompt, or a single request."""
    args = sys.argv[1:]
    session, cont, verbose, timing, mode, explain, quiet, rest = os.getenv("DIAL0_SESSION", "default"), False, False, False, "fast", False, False, []
    context, named, new = None, bool(os.getenv("DIAL0_SESSION")), False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-s", "--session") and i + 1 < len(args):
            session = args[i + 1]; named = True; i += 2; continue
        if a in ("--context", "-x"):
            context = True
        elif a == "--no-context":
            context = False
        elif a in ("-c", "--continue"):
            cont = True
        elif a == "--new":
            new = True
        elif a == "-v":
            verbose = timing = True
        elif a in ("-t", "--timing"):
            timing = True
        elif a in ("-a", "--agent"):
            mode = "agent"
        elif a in ("-e", "--explain"):
            explain = True
        elif a in ("-q", "--quiet"):
            quiet = True
        elif a in ("-h", "--help"):
            print(__doc__); return
        else:
            rest.append(a)
        i += 1
    if cont:
        ss = call("/sessions")["sessions"]
        if ss:
            session = ss[0]["name"]
    if not rest:  # plain `dial0`: the prompt
        repl(session if (named or cont) else PROMPT_SESSION, new, verbose, timing, quiet)
        return

    cmd = rest[0]
    if cmd == "enter" and len(rest) == 1:  # old spelling, kept so it never turns into a model request
        repl(session if (named or cont) else PROMPT_SESSION, new, verbose, timing, quiet)
    elif cmd == "blueprints" and len(rest) == 1:
        show_blueprints()
    elif cmd == "blueprint" and len(rest) >= 3 and rest[1] in ("apply", "show"):
        blueprint_cmd(rest[1], rest[2], rest[3:], verbose, quiet)
    elif cmd == "security" and len(rest) == 1:
        request("security audit", session, verbose=verbose, timing=timing, quiet=quiet)
    elif cmd == "cve" and len(rest) <= 3:
        sub = rest[1] if len(rest) > 1 else "run"
        if sub == "run":
            request("scan for CVEs", session, verbose=verbose, timing=timing, quiet=quiet)
        elif sub == "update":
            print("Downloading the Debian Security Tracker and the CISA known-exploited list ...")
            print(call("/cve/update", {}).get("status", "?"))
        elif sub == "list":
            show_cve_list(len(rest) > 2 and rest[2] == "all")
        else:
            print("usage: dial0 cve | cve update | cve list [all]")
    elif cmd == "workflows" and len(rest) == 1:
        show_workflows()
    elif cmd == "workflow" and len(rest) >= 2:
        workflow_cmd(rest[1:], verbose, timing, quiet)
    elif cmd == "health" and len(rest) == 1:
        request("health check", session, verbose=verbose, timing=timing, quiet=quiet)
    elif cmd == "sessions" and len(rest) == 1:
        show_sessions()
    elif cmd == "context" and len(rest) == 1:
        show_context(session)
    elif cmd == "history" and len(rest) == 1:
        show_history(session)
    elif cmd == "reset" and len(rest) >= 2 and set(rest[1:]) <= {"--all", "all", "-y", "--yes"} and set(rest[1:]) & {"--all", "all"}:
        reset_all(assume_yes=bool(set(rest[1:]) & {"-y", "--yes"}))
    elif cmd == "reset" and len(rest) == 1:
        call("/reset", {"session": session}); print(f"session '{session}' cleared")
    elif cmd == "why" and len(rest) == 1:
        show_why(session)
    elif cmd == "plans" and len(rest) <= 2:
        show_plans(len(rest) == 2 and rest[1] == "clear")
    elif cmd == "ref" and len(rest) >= 2:
        print(call("/ref?" + urllib.parse.urlencode({"q": " ".join(rest[1:])}))["text"])
    elif cmd == "rm" and len(rest) == 2:
        print("deleted" if call("/session/delete", {"name": rest[1]}).get("deleted") else "no such session")
    elif cmd == "resume" and len(rest) == 1:
        d = call("/session?name=" + urllib.parse.quote(session))
        if d.get("pending"):
            drive({"status": "needs_confirmation", "id": d["pending"]["id"], "commands": d["pending"]["commands"]},
                  session, verbose, timing, Steps(verbose, quiet))
        else:
            print("nothing pending.")
    else:
        if session != "default":
            print(f"(session: {session})", file=sys.stderr)
        if context is None and (named or cont):
            context = True  # a named/continued session is used for continuity
        request(" ".join(rest), session, mode, explain, context, verbose, timing, quiet)


# ---------------------------------------------------------------- shared pieces
def request(text, session, mode="fast", explain=False, context=None, verbose=False, timing=False, quiet=False,
            interactive=False):
    """One request: live steps, one y/N if anything changes config, then the results."""
    steps = Steps(verbose, quiet)
    body = {"query": text, "session": session, "mode": mode, "explain": explain}
    if context is not None:
        body["context"] = context  # otherwise the switch default (CONTEXT in dial0.conf) applies
    res = steps.run("/query", body)
    drive(res, session, verbose, timing, steps, ask_always=interactive)


def reset_all(assume_yes=False, interactive=False) -> bool:
    """One command for everything: every session's context and command history, learned requests, prompt history."""
    if not assume_yes:
        if not (sys.stdin.isatty() or interactive):
            print("This clears ALL sessions and history. Add -y to confirm: dial0 reset --all -y")
            return False
        if not ask_yes_no("Clear ALL sessions (context + command history), learned requests and prompt history? [y/N] "):
            print("Nothing cleared.")
            return False
    r = call("/reset_all", {})
    if "error" in r:
        print("error: " + r["error"]); return False
    print(f"Cleared: {r['sessions']} session(s) with their context and command history, {r['plans']} learned "
          f"request(s)" + (", the prompt's line history" if r.get("line_history") else "") + ".")
    return True


def ago(ts):
    d = int(time.time() - ts)
    return f"{d // 86400}d ago" if d >= 86400 else f"{d // 3600}h ago" if d >= 3600 else f"{d // 60}m ago" if d >= 60 else "just now"


def show_workflows():
    """Print every workflow with its schedule, retention and latest result."""
    rows = call("/workflows")["workflows"]
    print("Workflows (read-only checks, no model; results kept on the switch):")
    for w in rows:
        latest = w["latest"]
        last = f"last: {latest['overall']} {ago(latest['ts'])}" if latest else "last: never run"
        sch = w["schedule"] or "not scheduled"
        nxt = f", next {time.strftime('%H:%M', time.localtime(w['next']))}" if w.get("next") else ""
        print(f"  {w['name']:<11} {w['desc']}")
        print(f"  {'':<11} {sch}{nxt} | keeps {w['keep']} ({w['kept']} now) | {last}")
    print("Run: dial0 workflow run NAME   Schedule: dial0 workflow schedule NAME every 30m [--keep N]   "
          "Results: dial0 workflow results NAME")


def _ask_keep(name):
    """When scheduling from a terminal without --keep: ask how many results to keep."""
    if not sys.stdin.isatty():
        return None
    try:
        ans = input(f"How many results of '{name}' should be kept on the switch? [10]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print(); return None
    return int(ans) if ans.isdigit() else None


def workflow_cmd(args, verbose=False, timing=False, quiet=False):
    """`dial0 workflow run|schedule|unschedule|results|show|keep|clear ...`"""
    act, rest = args[0], args[1:]
    name = rest[0] if rest else ""
    if act == "run" and name:
        if name == "health":
            return request("health check", os.getenv("DIAL0_SESSION", "default"), verbose=verbose, timing=timing, quiet=quiet)
        r = call("/workflows/run", {"name": name})
        print(r["result"]["report"] if "result" in r else "error: " + r.get("error", "?"))
    elif act == "schedule" and len(rest) >= 3:
        keep = None
        if "--keep" in rest:
            i = rest.index("--keep")
            keep = int(rest[i + 1]) if i + 1 < len(rest) and rest[i + 1].isdigit() else None
            rest = rest[:i] + rest[i + 2:]
        how, val = rest[1], " ".join(rest[2:])
        if keep is None:
            keep = _ask_keep(name)
        body = {"name": name, "keep": keep}
        if how == "every":
            body["every"] = val
        elif how in ("daily", "at"):
            body["at"] = val
        else:
            print("usage: dial0 workflow schedule NAME every 30m|2h|1d  or  daily 06:00  [--keep N]"); return
        r = call("/workflows/schedule", body)
        if "error" in r:
            print("error: " + r["error"]); return
        print(f"'{name}' scheduled {r['schedule']}; next run {time.strftime('%Y-%m-%d %H:%M', time.localtime(r['next']))}; "
              f"keeping the last {r['keep']} results.")
    elif act == "unschedule" and name:
        r = call("/workflows/unschedule", {"name": name})
        print(r.get("error") or (f"'{name}' unscheduled (its results are kept)." if r["removed"] else f"'{name}' wasn't scheduled."))
    elif act == "results" and name:
        r = call("/workflows/results?" + urllib.parse.urlencode({"name": name}))
        if "error" in r:
            print("error: " + r["error"]); return
        if not r["results"]:
            print(f"no results kept for '{name}' yet (dial0 workflow run {name})"); return
        for i, x in enumerate(r["results"], 1):
            print(f"{i:>3}. {ts(x['ts'])}  {x['overall']:<8} ({x['trigger']})" + ("" if not x["issues"] else "  " + "; ".join(x["issues"]))[:200])
        print(f"Full report: dial0 workflow show {name} [N]")
    elif act == "show" and name:
        n = int(rest[1]) if len(rest) > 1 and rest[1].isdigit() else 1
        r = call("/workflows/result?" + urllib.parse.urlencode({"name": name, "n": n}))
        if "error" in r:
            print("error: " + r["error"]); return
        res = r.get("result")
        print(f"{ts(res['ts'])} ({res['trigger']}, {res['seconds']}s)\n{res['report']}" if res else f"no result #{n} for '{name}'")
    elif act == "keep" and len(rest) == 2 and rest[1].isdigit():
        r = call("/workflows/keep", {"name": name, "keep": int(rest[1])})
        print(r.get("error") or f"'{name}': keeping the last {r['keep']} results.")
    elif act == "clear" and name:
        r = call("/workflows/clear", {"name": name})
        print(r.get("error") or f"deleted {r['cleared']} kept result(s).")
    else:
        print("usage: dial0 workflow run NAME | schedule NAME every 30m|daily 06:00 [--keep N] | unschedule NAME |\n"
              "                  results NAME | show NAME [N] | keep NAME N | clear NAME|all")


def show_blueprints():
    for b in call("/blueprints")["blueprints"]:
        sv = b.get("saved") or {}
        print(f"{b['name']}\n  {b['title']}: {b['desc']}"
              + (f"\n  saved answers for this switch: role {sv.get('role')}, loopback {sv.get('loopback')}, ASN {sv.get('asn')}"
                 if sv else ""))
    print("Apply: dial0 blueprint apply NAME   Review only: dial0 blueprint show NAME")


def _print_plan(r):
    p = r["params"]
    print(f"\nConfiguration for this switch ({p['role']}): {r['commands']} command(s) to run")
    for st in r["steps"]:
        print(f"  {st['title']}")
        for c in st["commands"]:
            if c["tool"] == "vtysh":
                lines = c["argv"][2::2]
                print("    vtysh " + " \\\n          ".join(f'-c "{l}"' for l in lines))
            else:
                print(f"    {c['cmd']}" + (f"   ({c['skip']})" if c.get("skip") else ""))
    for title, items in (("CONFLICTS with this switch's configuration (nothing will be removed):", r["conflicts"]),
                         ("BLOCKERS:", r["blockers"])):
        if items:
            print("\n" + title)
            for x in items:
                print(f"  - {x}")


def blueprint_cmd(act, name, args, verbose=False, quiet=False):
    """`dial0 blueprint apply|show NAME [key=value ...]`: ask the blueprint's questions (saved answers are the
    defaults), re-ask invalid ones, show the full configuration and any conflicts, then apply after y/N."""
    sv = call("/blueprints/saved?" + urllib.parse.urlencode({"name": name}))
    if "error" in sv:
        print("error: " + sv["error"]); return
    saved = sv["saved"]
    answers = {}
    for a in args:  # key=value answers given on the command line
        k, _, v = a.partition("=")
        if v:
            answers[k.strip()] = v.strip()
    if act == "show":
        if not saved and not answers:
            print(f"no saved answers for '{name}' yet: dial0 blueprint apply {name}"); return
        r = call("/blueprints/plan", {"name": name, "params": dict(saved, **answers)})
        if r.get("errors") or r.get("error"):
            print("error: " + (r.get("error") or "; ".join(f"{k}: {v}" for k, v in r["errors"].items()))); return
        _print_plan(r); return

    def ask(key, prompt):
        """Ask one blueprint question unless it was given as key=value; Enter keeps the saved answer."""
        if key in answers:
            return
        d = saved.get(key)
        try:
            v = input(f"{prompt}" + (f" [{d}]" if d not in (None, "") else "") + ": ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); raise SystemExit("cancelled: nothing applied")
        answers[key] = v or (str(d) if d not in (None, "") else "")

    print(f"Blueprint {name}. Answers are saved for this switch; Enter keeps the value in [brackets].")
    ask("role", call("/blueprints/questions?" + urllib.parse.urlencode({"name": name}))["questions"][0]["prompt"])
    for q in call("/blueprints/questions?" + urllib.parse.urlencode({"name": name, "role": answers["role"].lower()}))["questions"][1:]:
        ask(q["key"], q["prompt"])
    for _ in range(5):
        r = call("/blueprints/plan", {"name": name, "params": answers})
        if r.get("error"):
            print("error: " + r["error"]); return
        if not r.get("errors"):
            break
        for k, msg in r["errors"].items():
            print(f"  {k}: {msg}")
            answers.pop(k, None)
            prompt = next((q["prompt"] for q in call("/blueprints/questions?" + urllib.parse.urlencode(
                {"name": name, "role": answers.get("role", "").lower()}))["questions"] if q["key"] == k), k)
            ask(k, prompt)
    else:
        print("too many invalid answers; nothing applied"); return
    _print_plan(r)
    if r["conflicts"] or r["blockers"]:
        print("\nNothing applied. Resolve these on the switch yourself, then run this again."); return
    if not ask_yes_no(f"\nBack up the current configuration, then apply these {r['commands']} command(s) to this switch? [y/N] "):
        print("Nothing applied."); return
    res = Steps(verbose=verbose, quiet=quiet).run("/blueprints/apply", {"name": name, "params": answers, "plan_id": r["plan_id"]})
    if res.get("ok"):
        print(f"\nDone: {res['applied']} command(s) applied, {res['skipped']} already in place. Configuration saved.")
        for v in res.get("verify", {}).values():
            print(f"  {v}")
    else:
        print("\nNOT completed: " + res.get("error", "?"))
        for x in res.get("conflicts", []) + res.get("blockers", []):
            print(f"  - {x}")
        if res.get("applied"):
            print(f"  {res['applied']} command(s) were applied before it stopped.")
    if res.get("backup"):
        print(f"Backup: {res['backup']['dir']}\nTo go back (disruptive: reloads the configuration):")
        for line in res["backup"]["restore"]:
            print(f"  {line}")


def show_cve_list(show_all=False):
    """Print the latest CVE scan as a table (60 rows, or all with `cve list all`)."""
    r = call("/cve/list")
    items = r.get("items") or []
    if not r.get("ts"):
        print("no CVE scan yet: dial0 cve"); return
    print(f"CVE scan of {ts(r['ts'])}: {len(items)} vulnerable (CVE, package) pairs" + ("" if show_all or len(items) <= 60
          else f"; showing 60 (dial0 cve list all)"))
    print(f"{'CVE':<16} {'urgency':<17} {'package':<22} {'installed':<26} {'fixed in':<26} where")
    for x in items if show_all else items[:60]:
        vers = "/".join(sorted(set(x["installed"].values())))
        print(f"{x['cve']:<16} {('KEV ' if x['kev'] else '') + x['urgency']:<17} {x['package'][:22]:<22} {vers[:26]:<26} "
              f"{(x['fixed'] or 'no fix yet')[:26]:<26} {', '.join(x['scopes'])[:60]}")


def show_sessions():
    for s in call("/sessions")["sessions"]:
        name = "(dial0 prompt)" if s["name"] == PROMPT_SESSION else s["name"]
        print(f"{name:<20} {ts(s['updated'])}  {s['messages']:>3} msgs  {s['commands']:>3} cmds" + ("  [pending approval]" if s["pending"] else ""))


def show_context(session):
    """Print exactly what the model gets as context for this session."""
    d = call("/session?name=" + urllib.parse.quote(session))
    print(("context" if session in UNNAMED else f"session: {d['name']}") + f"  (updated {ts(d['updated'])})")
    for m in d["messages"][-16:]:
        c = m["content"]
        if m["role"] == "assistant":
            try:
                j = json.loads(c); c = f"[{j['tool']}] {j['input']}"
            except Exception:
                pass
            who = "bot"
        else:
            who = "out" if c.startswith("OBSERVATION:") else "you"
            c = c.replace("OBSERVATION: ", "").replace("\n", " | ")
        print(f"  {who}: {c[:160]}")


def show_history(session):
    hist = call("/session?name=" + urllib.parse.quote(session))["history"]
    if not hist:
        print("no commands run in this session yet")
    for e in hist:
        print(f"{ts(e['ts'])}  {e['status']:<9} {e['command']}")


def show_why(session):
    trace = call("/session?name=" + urllib.parse.quote(session)).get("last_trace") or []
    if not trace:
        print("no steps recorded for this session yet")
    st = Steps(verbose=True)
    for step in trace:
        st.show(step)


def show_plans(clear=False):
    """Print the learned requests (reused without the model)."""
    if clear:
        call("/plans/clear", {}); print("learned plans cleared"); return
    plans = call("/plans")["plans"]
    if not plans:
        print("nothing learned yet")
    for p in plans:
        kind, key = p["key"].split(":", 1)
        print(f"{'pattern' if kind == 'tmpl' else 'exact  '}  {key}\n           -> " + " | ".join(p["commands"]))


# ---------------------------------------------------------------- the dial0> prompt (plain `dial0`)
LOGO = r"""
    ____    ___      _      _           ___
   |  _ \  |_ _|    / \    | |         / _ \
   | | | |  | |    / _ \   | |        | | | |
   | |_| |  | |   / ___ \  | |___     | |_| |
   |____/  |___| /_/   \_\ |_____|     \___/
"""

BANNER = """   Dial 0 for SONiC - talk to your switch in plain English
   -------------------------------------------------------------------------"""


def banner_text() -> str:
    """The Dial 0 banner: logo, title and a few things to try. Plain ASCII, under 80 columns, so it renders on
    serial consoles and narrow SSH windows."""
    tips = '   Try: "show me the vlans"   "is Ethernet4 up?"   /health   /help   /exit'
    return "\n".join((LOGO.rstrip("\n"), BANNER, tips, ""))


REPL_HELP = """Type a request in plain English or a SONiC command; each one sees what you did before in this session.
  /agent <request>     step-by-step agent mode (reads output between steps; slower)
  /explain <request>   also explain the output in words
  /fresh <request>     this one request without the earlier context
  /why                 steps of the last request      /history   commands run in this session
  /context             what the model gets as context /reset     start this session over (forget context)
  /reset all           clear ALL sessions and history (asks first)
  /health              health check of the switch (or just ask: "is the switch healthy?")
  /blueprints          configuration blueprints      /blueprint apply|show NAME
  /workflows           workflows, schedules, last results   /workflow run|schedule|results|show ...
  /ref list            the commands Dial 0 uses        /ref find <words>  look up commands
  /plans               learned requests
  /verbose on|off   /time on|off   /quiet on|off       /sessions  list sessions
  /exit (or Ctrl-D)    leave
"""


def _setup_readline():
    try:
        import readline, atexit
    except ImportError:
        return
    path = os.path.join(os.getenv("DIAL0_STATE_DIR", "/var/lib/dial0"), "repl_history")
    try:
        readline.read_history_file(path)
    except OSError:
        pass
    readline.set_history_length(500)

    def save():
        try:
            readline.write_history_file(path)
        except OSError:
            pass
    atexit.register(save)


def repl(session, new=False, verbose=False, timing=False, quiet=False):
    """Plain `dial0`: the dial0> prompt, context always on. Session names are shown only for named sessions."""
    if new:
        call("/reset", {"session": session})
    _setup_readline()
    tty = sys.stdin.isatty()
    named = session not in UNNAMED
    if tty and not quiet and os.getenv("DIAL0_BANNER", "on") != "off":
        print(banner_text())
        print("   Context is on: each request builds on the ones before"
              + (f" (session '{session}'" + (", started fresh" if new else "") + ")" if named
                 else (" (started fresh)" if new else "")) + ".\n")
    else:
        print("Dial 0" + (f", session '{session}'" if named else "") + (" (started fresh)" if new else "")
              + ": type a request or a SONiC command; each one builds on the ones before. /help for commands, /exit to leave.")
    prompt = f"dial0 [{session}]> " if named else "dial0> "
    while True:
        try:
            line = input(prompt if tty else "").strip()
        except EOFError:
            print()
            return
        except KeyboardInterrupt:
            print("^C")
            continue
        if not line:
            continue
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        try:
            if cmd in ("/exit", "/quit", "exit", "quit"):
                return
            elif cmd in ("/help", "help", "?"):
                print(REPL_HELP)
            elif cmd == "/why":
                show_why(session)
            elif cmd == "/history":
                show_history(session)
            elif cmd == "/context":
                show_context(session)
            elif cmd == "/blueprints":
                show_blueprints()
            elif cmd == "/blueprint":
                a = arg.split()
                if len(a) >= 2 and a[0] in ("apply", "show"):
                    blueprint_cmd(a[0], a[1], a[2:], verbose, quiet)
                else:
                    print("usage: /blueprint apply NAME | /blueprint show NAME")
            elif cmd == "/workflows":
                show_workflows()
            elif cmd == "/workflow":
                workflow_cmd(arg.split() or ["help"], verbose, timing, quiet)
            elif cmd == "/health":
                request("health check", session, context=True, verbose=verbose, timing=timing, quiet=quiet, interactive=True)
            elif cmd == "/sessions":
                show_sessions()
            elif cmd == "/reset" and arg in ("all", "--all"):
                if reset_all(interactive=True):
                    try:
                        import readline
                        readline.clear_history()  # or it would be written back when the prompt exits
                    except ImportError:
                        pass
            elif cmd == "/reset":
                call("/reset", {"session": session})
                print("Context cleared" + (f" (session '{session}')" if named else "") + ": the next request starts fresh.")
            elif cmd == "/plans":
                show_plans(arg == "clear")
            elif cmd == "/ref":
                print(call("/ref?" + urllib.parse.urlencode({"q": arg}))["text"] if arg else "usage: /ref find <words>")
            elif cmd in ("/verbose", "/time", "/quiet"):
                on = arg != "off"
                if cmd == "/verbose":
                    verbose = timing = on
                elif cmd == "/time":
                    timing = on
                else:
                    quiet = on
                print(f"{cmd[1:]} {'on' if on else 'off'}")
            elif cmd in ("/agent", "/explain", "/fresh"):
                if not arg:
                    print(f"usage: {cmd} <request>")
                    continue
                request(arg, session, mode="agent" if cmd == "/agent" else "fast", explain=cmd == "/explain",
                        context=cmd != "/fresh", verbose=verbose, timing=timing, quiet=quiet, interactive=True)
            elif cmd.startswith("/"):
                print(f"unknown command {cmd}; /help lists them")
            else:
                request(line, session, context=True, verbose=verbose, timing=timing, quiet=quiet, interactive=True)
        except KeyboardInterrupt:
            print("\n(interrupted: a request already sent keeps running on the switch; /why shows its steps when done)")
        except SystemExit as e:  # agent unreachable etc.: report and stay in the prompt
            if e.code not in (None, 0):
                print(e.code)

if __name__ == "__main__":
    main()
