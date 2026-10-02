"""Dial 0 harness.

Default (fast) mode, for "do X" / "show Y" requests:
  1. If the input already is a valid SONiC command -> run it. No model call.
  2. Otherwise ONE model call turns the request into a list of real commands (from the reference).
     If a command fails validation, ONE repair call; still invalid -> "I don't know" + closest real commands.
  3. The commands then run as plain tool calls (one y/N for the batch if anything changes config).
     Output is shown as-is; no model call to summarise unless --explain.
Agent mode (--agent): the step-by-step loop that reads command output between steps; slower, for troubleshooting.

Every decision is recorded as a plain-English step ("trace"): streamed live to the CLI through /progress,
returned with the result, and saved in the session (`dial0 why`).
"""
import json, os, re, threading, time, uuid
from . import llm, tools, mcp_adapter, clidoc, sessions, resolve, health, workflows

MAX_STEPS = int(os.getenv("DIAL0_MAX_STEPS", "6"))
CONFIRM = os.getenv("DIAL0_CONFIRM", "ask")  # ask | dry-run | auto
SHOW_MAX = int(os.getenv("DIAL0_SHOW_MAX", "200000"))  # output shown to the user (not fed to the model)
MAX_REJECTS = 2
FIX_ROUNDS = int(os.getenv("DIAL0_FIX_ROUNDS", "3"))  # after a SONiC error: rounds of "model proposes a fix" before giving up

# "already done" errors: the thing the command creates/sets is already there -> count the step as done, go on.
_ALREADY = re.compile(r"(?i)already\s+exists?|exists\s+already|\balready\b.{0,40}\b(configured|added|present|assigned|"
                      r"enabled|disabled|member|bound|created|set|in use)\b")
_ENT = re.compile(r"(?i)\b(vlan|ethernet|portchannel|loopback|vrf)[\s-]*0*(\d+)\b|\b(\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?)\b")


def _entities(text: str, vlan_context: bool = False) -> set:
    """(type, id) pairs named in a command or an error: Vlan100 / 'Vlan 100' -> ('vlan', '100'); IPs as ('ip', a)."""
    out = set()
    for m in _ENT.finditer(text):
        if m.group(3):
            out.add(("ip", m.group(3).split("/")[0]))
        else:
            out.add((m.group(1).lower(), str(int(m.group(2)))))
    if vlan_context:  # 'config vlan member add 40 ...': the bare number is the VLAN id
        for t in text.split():
            if t.isdigit():
                out.add(("vlan", str(int(t))))
    return out


def already_done(command: str, error: str) -> bool:
    """True if the error says the result of this very command is already in place (same VLAN/port/IP),
    e.g. 'Vlan100 already exists' for 'config vlan add 100'. A conflict with something else
    ('PortChannel100 is already untagged member of Vlan100' while adding it to VLAN 40) is not."""
    if not _ALREADY.search(error):
        return False
    err_ents = _entities(error)
    cmd_ents = _entities(command, vlan_context=" vlan " in f" {command} ")
    if not err_ents:
        return bool(re.search(r"(?i)already\s+exists?|exists\s+already", error))
    return err_ents <= cmd_ents
REASONING = os.getenv("DIAL0_REASONING", "on") != "off"  # model states a one-sentence "why" (~20 extra tokens)
# A model call costs minutes on one CPU core, so the harness only makes one when nothing else can answer:
MODEL_REPAIR = os.getenv("DIAL0_MODEL_REPAIR", "off") == "on"  # 2nd call to fix invalid commands (default: code fixes only)
WARMUP = os.getenv("DIAL0_WARMUP", "auto")  # on | off | auto (= on when a command reference file is loaded: its
                                            # full list is a big, fixed prompt start worth pre-loading once)
LEARN = os.getenv("DIAL0_LEARN", "on") != "off"                  # reuse earlier approved plans (no model call)
SEARCH_RETRY = os.getenv("DIAL0_SEARCH_RETRY", "on") != "off"    # nothing fits -> search the switch's CLI source, ask once more

# ---- fast mode prompts: the system prompt is static so llama.cpp can keep it cached between requests ----
_WHY_RULE = '- "why": first, in at most 25 words, say which reference commands fit the request and why.\n' if REASONING else ""
_WHY_EX = '"why": "config vlan add creates the VLAN and config vlan member add -u adds an untagged port; show vlan brief verifies.", ' if REASONING else ""
PLAN_SYSTEM = f"""You translate requests into SONiC CLI commands for the switch you run on.
Reply only with JSON: {{{'"why": "...", ' if REASONING else ''}"commands": ["..."], "search": "", "note": "..."}}.
Rules:
{_WHY_RULE}- Use ONLY commands shown to you: the COMMAND REFERENCE below (if given) and the list in the request message. Copy their
  exact syntax and replace <placeholders> and example values with values from the request.
- Obey the rules in the area descriptions (what must exist first, what can't be combined, which order to use).
- Follow the examples and their notes. An option exists only where an example shows it; a behaviour with no option in the
  reference is the default, so use the example without the option (e.g. a tagged VLAN member is added without -u).
  Never answer that no command fits just because there is no option for the default behaviour.
- Never invent commands, options or names. If the reference has no suitable command, return "commands": [] and put 2-5 SONiC keywords in "search" (e.g. "interface ip address") so more commands can be looked up for you; otherwise "search": "".
- If the request is unclear or a required value is missing, return "commands": [] and ask for it in the note.
- Change configuration ONLY with "config ..." commands. Never use ip, ifconfig, vtysh, sonic-db-cli or sonic-cfggen to change anything.
- SONiC interface names: VLAN 20 -> Vlan20, port channel 5 -> PortChannel5 (or PortChannel0005 if the switch uses that), loopback 0 -> Loopback0, port -> Ethernet<N> (e.g. "Ethernet 1/1" usually means an EthernetN name: ask if unsure).
- For configuration changes, add a show command from the reference at the end to verify.
- At most 6 commands. note: at most 20 words for the user.
Example: reference has "config vlan add <vid>", "config vlan member add [-u|--untagged] <vid> <port>", "show vlan brief".
Request: create vlan 20 with Ethernet8 untagged
{{{_WHY_EX}"commands": ["config vlan add 20", "config vlan member add -u 20 Ethernet8", "show vlan brief"], "search": "", "note": "Creates VLAN 20 and adds Ethernet8 untagged."}}"""
PLAN_SCHEMA = llm.plan_schema(REASONING)

ANALYSE_TASK = """This is not a command request. TASK: analyse the result of read-only checks of this SONiC switch
(below) and the evidence after it (system log messages, or CVE details from Debian's security data), and give the
operator insights. Never state facts about a CVE (impact, exploits, scores) that aren't in the data below:
- "summary": 1-2 sentences: the overall state and the most important problem.
- "causes": the most likely causes, each tied to the evidence below (name the check or quote the log line).
  Use only this data; if a cause is unclear, say what is unknown instead of guessing.
- "next_steps": what to check or do next, most useful first. Prefer show commands from the COMMAND REFERENCE (write them
  exactly). Never suggest reboot or reload unless the evidence clearly points there, and then say it's the operator's call.
Reply only with JSON: {"summary": "...", "causes": ["..."], "next_steps": ["..."]}"""

EXPLAIN_SYSTEM = """Answer the user's request using only the command output given. Be brief (1-3 sentences).
If the output does not answer it, say so. Reply only with JSON: {"answer": "..."}."""

# ---- agent mode prompt ----
AGENT_SYSTEM = """You are Dial 0, an assistant running on a SONiC network switch. You turn requests into SONiC CLI commands.
RULES
1. Never invent a command, subcommand, option or interface name. Use only commands listed in the reference below or returned by the ref tool.
2. If the command you need is not listed, use ref (find <words>, then show <command path>). If ref says NOT FOUND, or you are not sure, finish with final and say "I don't know a SONiC command for ..." with the closest matches. Not knowing is fine; guessing is not.
3. Look before you change: run show commands first, change only what was asked, then verify.
4. Change configuration ONLY with "config ..." commands (never ip, vtysh, sonic-db-cli or sonic-cfggen). SONiC names: VLAN 20 -> Vlan20, loopback 0 -> Loopback0, ports Ethernet<N>.
Reply ONLY with JSON: {"thought": "...", "tool": "click|ref|grep|mcp|final", "input": "..."}. Keep thought very short.
Tools:
- click: input is one SONiC command (config ..., show ..., sonic-db-cli ..., vtysh -c "show ...", ip ...). One command per step.
- ref: input is "find <words>" | "show <command path>" | "src <regex>" (searches the real sonic-utilities command reference and source).
- grep: input is "<pattern> <path>" under /etc/sonic or /var/log on the switch.
- mcp: input is JSON {"name": "...", "arguments": {...}} for a remote MCP tool.
- final: input is your answer to the user. Use it when done, when unsure, or when the request is unclear.
Never invent command output."""

NO_MATCH = "(No command in the SONiC reference matches this request. Try ref find with other keywords; if nothing turns up, answer with final that you do not know.)"
NO_REF = "(No command reference is available in this build: say so instead of guessing commands; use '<group> --help' via click to discover them.)"


def _add_timing(total: dict, t: dict):
    for k, v in t.items():
        if isinstance(v, (int, float)):
            total[k] = round(total.get(k, 0) + v, 1)
    total["llm_calls"] = total.get("llm_calls", 0) + 1


def _ref_names(ref: str) -> list[str]:
    """'- config vlan add [OPTIONS] <vid>   # Add VLAN' -> 'config vlan add'"""
    names = []
    for line in ref.splitlines():
        usage = line[2:].split("   #")[0]
        words = []
        for w in usage.split():
            if w[0] in "[<" or w.isupper():
                break
            words.append(w)
        if words:
            names.append(" ".join(words))
    return names


def _sonic_error(output: str) -> str:
    """The line that says what went wrong in a failed SONiC command's output."""
    lines = [l.strip() for l in output.splitlines() if l.strip() and l.strip() != "(no output)"]
    for l in reversed(lines):
        if re.match(r"(?i)^(error|usage error|traceback|.*exception|.*invalid|.*not found|.*failed|.*denied)", l):
            return l[:200]
    return (lines[-1][:200] if lines else "no output")


def _status(r: dict) -> str:
    """ok / already done / skipped / exit N: <SONiC's error>, for the context and the fix rounds."""
    if r["exit"] is None:
        return "not run (an earlier command failed)"
    if r.get("already"):
        return "already in place: " + _sonic_error(r["output"])
    return "ok" if r["exit"] == 0 else f"FAILED, exit {r['exit']}: {_sonic_error(r['output'])}"


def _lines(text: str) -> int:
    return 0 if text.strip() in ("", "(no output)") else len(text.strip().splitlines())


class Agent:
    """The agent harness: routes each request (no-model paths first), plans with the model, checks every command
    (reference, switch CLI, values), asks for approval, runs, recovers from errors, and keeps sessions. One request at
    a time: the switch gives us one CPU core."""
    def __init__(self):
        self.lock = threading.Lock()  # one request at a time: we only have one core
        self.mcp_tools = mcp_adapter.list_tools()
        self.progress: dict[str, list] = {}  # request id -> live steps (read by /progress)
        self._trace: list = []
        self._live: list = []
        self._ctx = False
        self._closest: list = []
        self._final_note = ""
        self._t0 = time.time()
        clidoc.start_background()

    # ------------------------------------------------------------------ trace
    def _begin(self, rid, carry=None):
        """carry: steps from before an approval, kept for `dial0 why` but not re-streamed live."""
        self._t0 = time.time()
        self._trace = list(carry or [])
        self._live = []
        if rid:
            self.progress[rid] = self._live
            for old in list(self.progress)[:-20]:
                self.progress.pop(old, None)

    def _step(self, msg: str, detail: str = ""):
        e = {"t": round(time.time() - self._t0, 1), "msg": msg}
        if detail:
            e["detail"] = detail
        self._trace.append(e)
        self._live.append(e)

    def _end(self, s: sessions.Session, result: dict) -> dict:
        s.last_trace = list(self._trace)
        s.save()
        result["steps"] = list(self._live)
        return result

    # ------------------------------------------------------------------ entry points
    def query(self, text: str, session: str = "default", mode: str = "fast", explain: bool = False, rid: str = "",
              context: bool = False) -> dict:
        """context=False (default): the model sees only this request. True: also earlier requests/commands of the session."""
        with self.lock:
            clidoc.BUSY.set()
            try:
                s = sessions.get(session)
                self._begin(rid)
                self._ctx = context
                if s.pending:  # an unanswered y/N from before: a new request cancels it (nothing was run)
                    old = [c["command"] for c in s.pending["commands"]]
                    for c in old:
                        s.record(c, "cancelled", "not answered before a new request")
                    s.add("assistant", "RESULT: cancelled, not approved: " + "; ".join(old))
                    s.pending = None
                    s.save()
                    self._step("Cancelled the earlier unanswered approval (" + "; ".join(old) + "); nothing from it was run.")
                self._say_context(s)
                if mode == "agent":
                    self._freeze(s, text)
                    self._step("Agent mode: the model works step by step and reads each command's output before the next step.",
                               s.ref)
                    s.add("user", text)
                    return self._end(s, self._loop(s))
                return self._end(s, self._fast(s, text, explain))
            finally:
                clidoc.BUSY.clear()

    def confirm(self, session: str, pid: str, approve: bool, rid: str = "") -> dict:
        """Answer a pending approval (y/N). Yes: run the approved commands, learn the plan if it worked, and start a
        fix round if SONiC returned an error. No: end the request without another model call."""
        with self.lock:
            clidoc.BUSY.set()
            try:
                s = sessions.get(session)
                p = s.pending
                if not p or p["id"] != pid:
                    return {"status": "error", "error": "no such pending command in this session"}
                self._begin(rid, carry=s.last_trace)
                self._ctx = bool(p.get("context"))
                s.pending = None
                s.save()
                if p.get("mode") == "agent":
                    cmd = p["commands"][0]
                    self._freeze(s, s.last_request())
                    if approve:
                        self._step(f"You approved. Running: {cmd['command']}")
                        out = tools.run_click(cmd["argv"])
                        code, body = tools.split_result(out)
                        self._step(f"  result: exit {code}, {_lines(body)} line(s) of output")
                        s.record(cmd["command"], "executed" if code == 0 else "failed", body[:150])
                        self._obs(s, out)
                    else:
                        self._step("You declined, so the request ends here (no further model call).")
                        s.record(cmd["command"], "denied")
                        self._obs(s, "USER DENIED this command.")
                        s.add("assistant", "RESULT: denied by user: " + cmd["command"])
                        return self._end(s, {"status": "done", "session": s.name, "answer": "Nothing was changed.", "results": []})
                    # one y/N per request: approving also covers the further changes this request needs
                    return self._end(s, self._loop(s, granted=True))
                timing = {"total_s": 0}
                t0 = time.time()
                if not approve:
                    self._step("You declined, so nothing was run or changed.")
                    if p.get("learn"):
                        resolve.forget(p.get("request", ""))
                    for c in p["commands"]:
                        s.record(c["command"], "denied")
                    s.add("assistant", "RESULT: denied by user: " + "; ".join(c["command"] for c in p["commands"]))
                    return self._end(s, {"status": "done", "session": s.name, "answer": "Nothing was changed.", "results": [], "timing": timing})
                self._step("You approved.")
                self._request = p.get("request", "")
                self._grounding = self._request + ("\n" + self._history(s) if self._ctx else "")
                res = self._run_all(s, p["commands"], p.get("request", ""))
                if p.get("learn") and res and all(r["exit"] == 0 or r.get("already") for r in res):
                    resolve.remember(p.get("request", ""), [c["command"] for c in p["commands"]])
                    self._step("Remembered these commands: the same kind of request won't need the model next time.")
                pend = self._recover(s, p.get("request", ""), res, p.get("history", []), p.get("fix_round", 0), timing)
                if pend:
                    timing["total_s"] = round(time.time() - t0, 1)
                    pend["timing"] = timing
                    return self._end(s, pend)
                answer = self._final_note or p.get("note", "")
                if p.get("explain") and res:
                    answer = self._explain(s, p.get("request", ""), res, timing) or answer
                timing["total_s"] = round(time.time() - t0, 1)
                return self._end(s, {"status": "done", "session": s.name, "answer": answer, "results": res, "timing": timing})
            finally:
                clidoc.BUSY.clear()

    def _say_context(self, s):
        if not self._ctx:
            self._step("Fresh request: nothing from earlier requests is given to the model (use --context to include it).")
            return
        n = len([m for m in s.messages if m["role"] == "user" and not m["content"].startswith("OBSERVATION:")])
        where = "" if s.name in sessions.UNNAMED else f" of session '{s.name}'"
        self._step(f"Including context{where}: {n} earlier request(s) and the commands they ran.")

    # ------------------------------------------------------------------ fast mode
    def _fast(self, s: sessions.Session, text: str, explain: bool) -> dict:
        t0 = time.time()
        timing: dict = {}
        s.add("user", text)
        self._request = text
        # what the user actually wrote: values in proposed commands must come from here
        self._grounding = text + ("\n" + self._history(s) if self._ctx else "")

        if resolve.blueprint_request(text):  # blueprints ask their own questions: point to the command, no model
            from . import blueprints as _bp
            names = ", ".join(b["name"] for b in _bp.catalogue())
            self._step("Blueprint question: blueprints are applied with their own guided command. No model needed.")
            timing["total_s"] = round(time.time() - t0, 1)
            return {"status": "done", "session": s.name, "results": [], "timing": timing, "how": "blueprint",
                    "answer": f"Blueprints configure this switch from a template: you answer a few questions, review the "
                              f"full configuration, and it's applied after y/N.\nAvailable: {names}\n"
                              f"Run: dial0 blueprint apply <name>   (at the prompt: /blueprint apply <name>)"}
        if resolve.security_request(text):  # configuration audit + CVE scan, by code; the model only adds insights
            return self._workflow(s, "security", "Security question: running the security audit (passwords, accounts, SSH, "
                                  "exposed services, SNMP, ACLs, logging, login attempts, software age, file permissions) "
                                  "and the CVE scan. Read-only.", timing, t0)
        if resolve.cve_request(text):  # the CVE scan workflow: matched by code; the model only adds insights
            return self._workflow(s, "cve", "Vulnerability question: running the CVE scan (installed packages of the host "
                                  "and every SONiC container vs Debian's security data; known-exploited flags from CISA).",
                                  timing, t0)
        if resolve.health_request(text):  # the health check workflow: fixed read-only checks, no model
            return self._health(s, timing, t0)

        meta = resolve.meta_lookup(text)
        if meta is not None:  # a question about the command reference itself: answered without the model
            return self._lookup(s, text, meta, timing, t0)

        direct = self._as_command(text)
        known = None if direct else self._known(text)
        if direct:
            if direct.get("unlisted"):
                self._step(f"'{direct['command']}' is not in your command reference, so Dial 0 doesn't run it.")
                s.add("assistant", "RESULT: not in the command reference: " + direct["command"])
                timing["total_s"] = round(time.time() - t0, 1)
                return {"status": "done", "session": s.name, "results": [], "timing": timing, "how": "direct",
                        "answer": (f"'{direct['command']}' is not in your command reference "
                                   f"({clidoc._curated_meta.get('file', 'reference')}), so Dial 0 doesn't run it. Run it in "
                                   "the switch shell yourself, or add it to the file and run: dial0 ctl cmdref")}
            else:
                self._step(f"'{direct['command']}' is a real SONiC command (found in the reference), so it runs as typed. No model needed.")
            cmds, note, how = [direct], "", "direct"
        elif known:
            cmds, note, how = known
        else:
            cmds, note, err = self._plan(s, text, timing)
            how = "planned"
            if err:
                s.add("assistant", "RESULT: " + err)
                timing["total_s"] = round(time.time() - t0, 1)
                return {"status": "done", "session": s.name, "answer": err, "results": [], "timing": timing, "how": how}
            if not cmds:
                s.add("assistant", "RESULT: no command. " + note)
                close = ("\n".join(clidoc._line(p) for p in self._closest[:6]) if getattr(self, "_closest", None)
                         else clidoc.search(text, 6))
                if close:
                    self._step("Showing the closest real commands instead (no extra model call).")
                ans = (note or "I don't know a SONiC command for that.") + (
                    "\n\nClosest real SONiC commands (type one with your values to run it directly, no model needed):\n"
                    + close if close else "")
                timing["total_s"] = round(time.time() - t0, 1)
                return {"status": "done", "session": s.name, "answer": ans, "results": [], "timing": timing, "how": how}

        mutating = [c for c in cmds if tools.is_mutating(c["argv"])]
        if mutating and CONFIRM == "ask":
            self._step(f"{len(mutating)} of {len(cmds)} command(s) change the switch configuration, so they need your approval "
                       "(one y/N for all).")
            s.pending = {"id": uuid.uuid4().hex[:8], "mode": "fast", "commands": cmds, "note": note,
                         "request": text, "explain": explain, "learn": LEARN and how in ("planned", "learned"),
                         "context": self._ctx}
            s.save()
            r = self._pending_reply(s)
            timing["total_s"] = round(time.time() - t0, 1)
            r.update(timing=timing, how=how, note=note)
            return r
        if mutating and CONFIRM == "dry-run":
            self._step(f"CONFIRM=dry-run: {len(mutating)} change(s) are shown but not run.")
            for c in mutating:
                s.record(c["command"], "dry-run")
            cmds = [c for c in cmds if c not in mutating]
            note = (note + " " if note else "") + "DRY-RUN, not executed: " + "; ".join(c["command"] for c in mutating)
        elif mutating:
            self._step("CONFIRM=auto: changes run without asking.")
        else:
            self._step("Nothing here changes configuration (read-only), so it runs without asking.")
        res = self._run_all(s, cmds, text)
        pend = self._recover(s, text, res, [], 0, timing)
        if pend:
            timing["total_s"] = round(time.time() - t0, 1)
            pend.update(timing=timing, how=how)
            return pend
        if self._final_note:
            note = self._final_note
        if LEARN and how == "planned" and not mutating and res and all(r["exit"] == 0 for r in res):
            resolve.remember(text, [c["command"] for c in cmds])
            self._step("Remembered these commands: the same kind of request won't need the model next time.")
        direct_answer = resolve.answer_from_output(text, res) if res else None
        if direct_answer:
            self._step("Answered from the command output directly (read the table; no model needed).")
            note = direct_answer
        elif explain and res:
            note = self._explain(s, text, res, timing) or note
        timing["total_s"] = round(time.time() - t0, 1)
        return {"status": "done", "session": s.name, "answer": note, "results": res, "timing": timing, "how": how}

    def _workflow(self, s, name, intro, timing, t0):
        self._step(intro)
        r = workflows.run(name, self._step, trigger="request",
                          analyse=(lambda *a: self.analyse_workflow(*a, step=self._step)) if workflows.INSIGHTS else None)
        s.add("assistant", f"RESULT: {name}: {r['overall']}; " + "; ".join(f"{f['check']} {f['level']}" for f in r["findings"]))
        timing["total_s"] = round(time.time() - t0, 1)
        return {"status": "done", "session": s.name, "answer": r["report"], "results": [], "timing": timing, "how": name}

    def _health(self, s, timing, t0):
        self._step("Health question: running the health check (system health, containers, interfaces, BGP, resources, "
                   "the system log via grep, crash dumps). Read-only, no model needed.")
        r = workflows.run("health", self._step, trigger="request",  # kept like any workflow result
                          analyse=(lambda *a: self.analyse_workflow(*a, step=self._step)) if workflows.INSIGHTS else None)
        report, overall = r["report"], r["overall"]
        findings = [(f["level"], f["check"], f["message"]) for f in r["findings"]]
        for lvl, title, msg in findings:
            s.record(f"health check: {title}", "executed", f"{lvl}: {msg}")
        s.add("assistant", f"RESULT: health check: {overall}; " + "; ".join(f"{t} {l}" for l, t, _ in findings))
        timing["total_s"] = round(time.time() - t0, 1)
        return {"status": "done", "session": s.name, "answer": report, "results": [], "timing": timing, "how": "health"}

    def _lookup(self, s, text, words, timing, t0):
        q = words or s.previous_request()
        if not words and q:
            self._step(f"This asks about the SONiC command reference itself; nothing specific named, so I looked up your "
                       f"previous request: '{q}'. No model needed.")
        else:
            self._step("This asks about the SONiC command reference itself, so I searched it directly. No model needed.")
        s.add("assistant", "RESULT: looked up commands for: " + (q or "(nothing)"))
        timing["total_s"] = round(time.time() - t0, 1)
        if not q:
            return {"status": "done", "session": s.name, "results": [], "timing": timing, "how": "lookup",
                    "answer": "What should I look up? For example: find the command to add an ip address to a vlan"}
        found = clidoc.search(q, 10)
        ans = (f"Real SONiC commands for '{q}':\n{found}\nType one with your values to run it directly (no model needed)."
               if found else f"Nothing in the SONiC command reference matches '{q}'. Try other words.")
        return {"status": "done", "session": s.name, "answer": ans, "results": [], "timing": timing, "how": "lookup"}

    def _known(self, text: str):
        """Commands without a model call: an earlier approved plan, or a common read-only question."""
        r = resolve.recall(text) if LEARN else None
        if r:
            cmds, bad, _ = self._check(r[0])
            if cmds and not bad:
                self._step(f"Reusing earlier commands ({r[1]}). No model needed: " + " | ".join(c["command"] for c in cmds))
                return cmds, "", "learned"
            self._step("An earlier plan for this request no longer validates, so it isn't reused.")
        i = resolve.intent(text)
        if i:
            cmds, bad, _ = self._check([i[0]])
            if cmds and not bad:
                self._step(f"Plain read-only question: {i[1]} -> '{i[0]}'. No model needed.")
                return cmds, "", "intent"
        return None

    @staticmethod
    def _as_command(text: str):
        """The user typed a real command: use it as is (no model call)."""
        try:
            argv = tools.parse_click(text.strip())
        except tools.ToolError:
            return None
        if argv[0] in clidoc.ROOTS:
            if len(argv) < 2 or not clidoc.available():
                return None
            if clidoc.check_command(argv) is None:
                return {"argv": argv, "command": text.strip()}
            if clidoc.curated() and clidoc.curated_path(argv) is None and clidoc.validate(argv) is None \
                    and clidoc._path_of(" ".join(argv)):
                return {"argv": argv, "command": text.strip(), "unlisted": True}  # a real command, just not in your list
            return None  # e.g. "show me the vlans": not a command, let the model plan it
        if clidoc.curated():
            return None  # other tools (ip, vtysh...) aren't in the command reference
        return {"argv": argv, "command": text.strip()}

    def _history(self, s: sessions.Session, n: int = 6) -> str:
        """Compact recent context: requests and what was run (no command output, to keep the prompt small)."""
        items, msgs = [], s.messages
        if msgs and msgs[-1]["role"] == "user":
            msgs = msgs[:-1]  # the request being planned right now
        for m in msgs:
            c = m["content"]
            if m["role"] == "user" and not c.startswith("OBSERVATION:"):
                items.append("user: " + c[:200])
            elif m["role"] == "assistant" and c.startswith("RESULT:"):
                items.append("done: " + c[7:].strip()[:400])
        return "\n".join(items[-n:])

    def _plan_system(self) -> str:
        """The model's instructions; with a command reference file, followed by the COMPLETE command list, grouped
        like the file, with every area's description and every command's examples and notes. Fixed for every
        request (llama.cpp keeps it cached); rebuilt only when the reference changes (container restart)."""
        if getattr(self, "_plan_sys", None) is None:
            full = clidoc.full_reference()
            self._plan_sys = PLAN_SYSTEM + (
                "\n\nCOMMAND REFERENCE: the ONLY commands you may use, from the switch owner's reference file.\n" + full
                if full else "")
        return self._plan_sys

    def _plan_messages(self, s, text, ref, extra=""):
        hist = self._history(s) if self._ctx else ""
        label = ("Most relevant for this request (all are in the COMMAND REFERENCE; use any command from it that fits):"
                 if clidoc.curated() else "Reference (real commands on this switch):")
        user = (label + "\n" + (ref or "(nothing relevant found)") + "\n"
                + (f"Recent in this session:\n{hist}\n" if hist else "")
                + (extra + "\n" if extra else "")
                + "Request: " + text)
        return [{"role": "system", "content": self._plan_system()}, {"role": "user", "content": user}]

    def _ask_model(self, messages, timing, label):
        chars = sum(len(m["content"]) for m in messages)
        self._step(f"{label} (~{chars // 4} tokens to read; on one CPU this can take a minute or more).")
        out, t = llm.complete(messages, PLAN_SCHEMA, max_tokens=300)
        _add_timing(timing, t)
        cached = f", {t['cached_tokens']} from cache" if t.get("cached_tokens") else ""
        self._step(f"Model answered in {t.get('llm_s', '?')}s{cached}.")
        if out.get("why"):
            self._step(f"Model's reasoning: {out['why']}")
        cmds = [c for c in out.get("commands", []) if (c or "").strip()]
        if cmds:
            self._step("Model proposed: " + " | ".join(cmds))
        else:
            self._step("Model proposed no commands" + (f": {out.get('note')}" if out.get("note") else "."))
        return out

    def _report_check(self, cmds, errors, fixed=()):
        for orig, new, why in fixed:
            self._step(f"Fixed without the model: '{orig}' -> '{new}' ({why}).")
        if cmds and not errors:
            self._step(f"Checked against the SONiC reference: all {len(cmds)} command(s) exist with valid options.")
        for c, m in errors:
            self._step(f"Rejected '{c}': {m}")
        if errors and cmds:
            self._step(f"{len(cmds)} other command(s) are valid.")

    def _reference(self, s, text):
        """The reference commands shown to the model. With context, a follow-up ("add it as tagged instead") also
        gets the commands the previous request ran and a search on its words: the follow-up alone may name no
        command at all. -> (reference lines, whether context contributed)."""
        paths = clidoc.search_paths(text, 5 if clidoc.curated() else 8)
        if not self._ctx:
            return "\n".join(clidoc._line(p) for p in paths), False
        prev_paths = []
        for e in reversed(s.recent_commands(6)):
            try:
                argv = tools.parse_click(e["command"])
            except tools.ToolError:
                continue
            p = clidoc.curated_path(argv) if clidoc.curated() else clidoc._path_of(e["command"])
            if p and p not in prev_paths:
                prev_paths.append(p)
        prev_req = s.previous_request()
        extra = prev_paths + (clidoc.search_paths(prev_req, 6) if prev_req else [])
        merged = []
        for p in prev_paths[:3] + paths + extra:  # what was just run first: a follow-up is usually about it
            if p not in merged:
                merged.append(p)
        merged = merged[:10]
        return "\n".join(clidoc._line(p) for p in merged), bool([p for p in merged if p not in paths])

    def _plan(self, s, text, timing):
        """-> (commands, note, error). commands are validated real commands."""
        if (not self._ctx and not resolve.values(text)
                and re.search(r"\b(it|that|this|them|those|same|again|instead)\b", text, re.I)):
            self._step("This refers back to an earlier request but has no context and names no port, VLAN or address, "
                       "so the model couldn't know what it means. No model call.")
            return [], "", ("That seems to refer to an earlier request. Use the dial0> prompt (just `dial0`) or add "
                            "--context, so it's read together with that request, or name the port / VLAN / address.")
        ref, from_ctx = self._reference(s, text)
        if not ref and clidoc.available():
            self._step("Searched the SONiC command reference: nothing matches this request, so I'm answering "
                       "'I don't know' without asking the model to guess.")
            hint = ""
            if not self._ctx and re.search(r"\b(it|that|this|them|those|same|again|instead)\b", text, re.I):
                hint = (" If this refers to your previous request, use the dial0> prompt (just `dial0`) "
                        "or add --context, so it's read together with that request.")
            return [], "", ("I don't know a SONiC command for that: nothing in the SONiC command reference matches. "
                            "Try other words, or `dial0 ref find <words>`." + hint)
        names = _ref_names(ref)
        if clidoc.curated():
            self._step(f"The model sees every command of your reference, with their descriptions and "
                       f"examples; most relevant{' (with context)' if from_ctx else ''}: " + ", ".join(names[:4])
                       + ("..." if len(names) > 4 else ""), ref)
        else:
            self._step((f"Searched the SONiC command reference for this request and the previous one (context): "
                        if from_ctx else "Searched the SONiC command reference: ")
                       + f"{len(names)} relevant command(s), e.g. " + ", ".join(names[:4]) + ("..." if len(names) > 4 else ""), ref)
        try:
            out = self._ask_model(self._plan_messages(s, text, ref), timing, "Asking the model which of these fit the request")
        except llm.LLMError as e:
            self._step(f"The model call failed: {e}")
            return [], "", str(e)
        cmds, errors, fixed = self._check(out.get("commands", []))
        note = out.get("note", "")
        self._report_check(cmds, errors, fixed)
        self._closest = []
        if not cmds and not errors and SEARCH_RETRY and not clidoc.curated() and not note.rstrip().endswith("?"):
            # (with a command reference file the model already sees every command: no second look needed)
            return self._search_again(s, text, ref, out, timing)
        if errors and not MODEL_REPAIR:
            self._step("Some proposed commands are invalid and couldn't be fixed by code. I don't guess, and I don't spend "
                       "another model call on it (REPAIR_WITH_MODEL=off), so nothing is run.")
            for e in errors:
                s.record(e[0], "rejected", e[1])
            close = clidoc.search(text, 5)
            return [], "", ("I don't know a valid SONiC command for that. What the model proposed is not in the SONiC "
                            "reference for this switch, so nothing was run:\n"
                            + "\n".join(f"  {c}: {m.splitlines()[0]}" for c, m in errors)
                            + (f"\nClosest real commands:\n{close}" if close else "")
                            + "\nTip: rephrase with the SONiC terms above, or type the command directly (no model needed).")
        if errors:  # one repair round with the validator's feedback (REPAIR_WITH_MODEL=on)
            for e in errors:
                s.record(e[0], "rejected", e[1])
            fix = "Your previous answer had invalid commands:\n" + "\n".join(f"- {c}: {m}" for c, m in errors) + \
                  "\nReturn corrected JSON using only the reference, or commands [] if you don't know."
            more = "\n".join(filter(None, {clidoc.search(c, 4) for c, _ in errors}))
            try:
                out = self._ask_model(self._plan_messages(s, text, (ref + "\n" + more).strip(), fix), timing,
                                      f"Giving the model the validation errors and asking it to fix {len(errors)} command(s)")
            except llm.LLMError as e:
                self._step(f"The model call failed: {e}")
                return [], "", str(e)
            cmds, errors, fixed = self._check(out.get("commands", []))
            note = out.get("note", "")
            self._report_check(cmds, errors, fixed)
            if errors:
                self._step("Still invalid after one correction. I won't guess further, so nothing is run.")
                for e in errors:
                    s.record(e[0], "rejected", e[1])
                close = clidoc.search(text, 5)
                return [], "", ("I don't know a valid SONiC command for that. What I came up with is not in the SONiC "
                                "reference for this switch, so nothing was run:\n"
                                + "\n".join(f"  {c}: {m}" for c, m in errors)
                                + (f"\nClosest real commands:\n{close}" if close else ""))
        return cmds, note, None

    def _search_again(self, s, text, ref, out, timing):
        """The first reference had nothing that fits: search this switch's own CLI source (the native tool:
        index + docstrings/option help/code), then ask the model once more with the new candidates."""
        words = (out.get("search") or "").strip()
        shown = clidoc.search_paths(text, 8)
        new = clidoc.deep_search(words, text, exclude=shown, k=8)
        where = ("this switch's installed SONiC CLI source" if clidoc._meta.get("origin") == "switch"
                 else "the SONiC CLI source")
        if not new:
            self._step(f"Searched {where} for '{words or text}' as well: nothing new found.")
            return [], out.get("note", ""), None
        more = "\n".join(clidoc._line(p) for p in new)
        self._closest = new
        self._step(f"The first results didn't fit, so I searched {where} for '{words or text}' (native search, no model): "
                   f"{len(new)} more command(s), e.g. " + ", ".join(new[:4]) + ("..." if len(new) > 4 else ""), more)
        extra = ("None of the first reference commands fitted. More commands, found by searching the switch's SONiC CLI "
                 "source, were added to the reference above. Use them if they fit; otherwise commands [] again.")
        try:
            out = self._ask_model(self._plan_messages(s, text, ref + "\n" + more, extra), timing,
                                  "Asking the model again with the extra commands")
        except llm.LLMError as e:
            self._step(f"The model call failed: {e}")
            return [], "", str(e)
        cmds, errors, fixed = self._check(out.get("commands", []))
        self._report_check(cmds, errors, fixed)
        if errors:
            for e in errors:
                s.record(e[0], "rejected", e[1])
            self._step("Still not valid after the second look. I won't guess further, so nothing is run.")
            return [], "", ("I don't know a valid SONiC command for that. What the model proposed is not a command on "
                            "this switch, so nothing was run:\n" + "\n".join(f"  {c}: {m.splitlines()[0]}" for c, m in errors)
                            + "\nClosest real commands:\n" + more)
        return cmds, out.get("note", ""), None

    @staticmethod
    def _valid(c):
        try:
            argv = tools.parse_click(c)
        except tools.ToolError as e:
            return None, str(e)
        err = clidoc.check_command(argv)
        return (None, err) if err else (argv, None)

    def _check(self, commands):
        """-> (valid commands, [(bad, error)], [(original, fixed, reason)]). No model call:
        1. values: interface names (vlan20 -> Vlan20), IP + netmask -> /prefix, every IP must be in the request;
        2. the command, its options and its argument values must match the switch's CLI definition;
        3. if not: a deterministic fix (unique typo, Linux ip -> config interface), then checked again."""
        good, bad, fixed = [], [], []
        grounding = getattr(self, "_grounding", "")
        for orig in commands[:6]:
            orig = (orig or "").strip()
            if not orig:
                continue
            c, reasons, verr = resolve.fix_values(orig, grounding)
            if verr:
                bad.append((orig, verr))
                continue
            c, mode_reason = resolve.fix_modes(c, getattr(self, "_request", ""))
            if mode_reason:
                reasons.append(mode_reason)
            argv, err = self._valid(c)
            if err:
                f = resolve.fix(c)
                if f:
                    c2, r2, verr2 = resolve.fix_values(f[0], grounding)
                    argv2, err2 = self._valid(c2) if not verr2 else (None, verr2)
                    if not err2:
                        good.append({"argv": argv2, "command": c2})
                        fixed.append((orig, c2, "; ".join(filter(None, [", ".join(reasons), f[1], ", ".join(r2)]))))
                        continue
                bad.append((c, err))
                continue
            good.append({"argv": argv, "command": c})
            if reasons:
                fixed.append((orig, c, "values: " + ", ".join(reasons)))
        return good, bad, fixed

    def _run_all(self, s, cmds, request):
        """All commands of an approved request run at once: one host invocation, in order. If a change fails only
        because its result is already in place ("Vlan100 already exists"), that step counts as done and the rest of
        the approved batch continues (no new approval, no model call)."""
        if not cmds:
            return []
        t0 = time.time()
        self._step(f"Running {len(cmds)} command(s) on the switch in one batch, in order.")
        res, todo = [], list(cmds)
        while todo:
            mut = [tools.is_mutating(c["argv"]) for c in todo]
            out = tools.run_batch([c["argv"] for c in todo], mut, max_out=SHOW_MAX)
            batch = []
            for c, r in zip(todo, out):
                if not r["ran"]:
                    batch.append({"command": c["command"], "exit": None, "output": "(skipped: an earlier change failed)"})
                else:
                    batch.append({"command": c["command"], "exit": r["exit"], "output": r["output"]})
            k = next((i for i, r in enumerate(batch) if r["exit"] not in (0, None)), None)
            if k is not None and tools.is_mutating(todo[k]["argv"]) and already_done(batch[k]["command"], batch[k]["output"]):
                batch[k]["already"] = True
                self._step(f"'{batch[k]['command']}': {_sonic_error(batch[k]['output'])} That is already what this step "
                           "wanted, so it counts as done; continuing with the rest.")
                res += batch[:k + 1]
                todo = todo[k + 1:]
                continue
            res += batch
            break
        for r in res:
            if r["exit"] is None:
                s.record(r["command"], "skipped", "an earlier change failed")
            elif r.get("already"):
                s.record(r["command"], "already done", _sonic_error(r["output"]))
            else:
                s.record(r["command"], "executed" if r["exit"] == 0 else "failed", r["output"][:150])
        secs = round(time.time() - t0, 1)
        failed = [r for r in res if r["exit"] not in (0, None) and not r.get("already")]
        skipped = [r for r in res if r["exit"] is None]
        if not failed:
            self._step(f"Finished in {secs}s: all {len(res)} command(s) done.")
        else:
            self._step(f"Finished in {secs}s: '{failed[0]['command']}' failed (exit {failed[0]['exit']}): "
                       + _sonic_error(failed[0]["output"])
                       + (f" {len(skipped)} later command(s) skipped because a change failed." if skipped else ""))
        if res:
            res[0]["batch_seconds"] = secs
        s.add("assistant", "RESULT: " + "; ".join(f"{r['command']} ({_status(r)})" for r in res))
        return res

    # ---------------------------------------------------------------- error recovery loop
    def _recover(self, s, request, res, history, rnd, timing, note=""):
        """After a SONiC error: give the model the request, what succeeded, the exact error and what didn't run;
        it proposes the commands that still need to run (or asks you). Repeats up to FIX_ROUNDS times.
        -> a pending-approval reply (the fix changes config), or None (done / gave up; self._final_note says why)."""
        self._final_note = ""
        history = history + [f"- {r['command']}: {_status(r)}" for r in res]
        failed = [r for r in res if r["exit"] not in (0, None) and not r.get("already")]
        if not failed:
            return None
        if rnd >= FIX_ROUNDS:
            self._step(f"Still failing after {FIX_ROUNDS} fix round(s); giving up (FIX_ROUNDS in dial0.conf).")
            self._final_note = (f"Couldn't complete this after {FIX_ROUNDS} attempt(s) to fix it. Last error from SONiC: "
                                + _sonic_error(failed[0]["output"]))
            return None
        err = _sonic_error(failed[0]["output"])
        self._step(f"Fix round {rnd + 1} of {FIX_ROUNDS}: giving the model SONiC's error so it can work out what to do instead.")
        paths = []
        for c in [failed[0]["command"]] + [r["command"] for r in res]:
            try:
                p = clidoc.curated_path(tools.parse_click(c)) if clidoc.curated() else clidoc._path_of(c)
            except tools.ToolError:
                p = None
            if p and p not in paths:
                paths.append(p)
        for p in clidoc.search_paths(request + " " + err, 5):
            if p not in paths:
                paths.append(p)
        ref = "\n".join(clidoc._line(p) for p in paths[:8])
        fix = ("What happened so far (in order):\n" + "\n".join(history) + "\n"
               "Give ONLY the commands that still need to run to complete the request, taking SONiC's error into "
               "account: don't repeat what succeeded or already exists; change what caused the error. If the error "
               "needs the user's decision (e.g. removing something they didn't ask to remove), return commands [] and "
               "ask in the note.")
        try:
            out = self._ask_model(self._plan_messages(s, request, ref, fix), timing, "Asking the model how to fix it")
        except llm.LLMError as e:
            self._step(f"The model call failed: {e}")
            self._final_note = str(e)
            return None
        cmds, errors, fixed = self._check(out.get("commands", []))
        self._report_check(cmds, errors, fixed)
        if errors or not cmds:
            self._final_note = (out.get("note") or "The model found no way to fix this.") + (
                "\n" + "\n".join(f"  {c}: {m.splitlines()[0]}" for c, m in errors) if errors else "")
            self._step("No valid fix proposed; stopping here.")
            return None
        mutating = [c for c in cmds if tools.is_mutating(c["argv"])]
        if mutating and CONFIRM == "ask":
            self._step(f"The fix changes the switch: {len(mutating)} command(s) need your approval.")
            s.pending = {"id": uuid.uuid4().hex[:8], "mode": "fast", "commands": cmds, "note": out.get("note", ""),
                         "request": request, "explain": False, "learn": False, "context": self._ctx,
                         "fix_round": rnd + 1, "history": history}
            s.save()
            r = self._pending_reply(s)
            r["results"] = res  # show what failed before asking about the fix
            return r
        res2 = self._run_all(s, cmds if CONFIRM != "dry-run" else [c for c in cmds if c not in mutating], request)
        return self._recover(s, request, res2, history, rnd + 1, timing, out.get("note", ""))

    def analyse_workflow(self, name, report, findings, log_excerpt, step=None):
        """The model's insights on a workflow result (one call). Uses the same instructions as planning, so llama.cpp's
        cached prefix is reused. Suggested commands are checked: invalid ones are dropped, changes are marked."""
        step = step or (lambda m, d="": None)
        user = (ANALYSE_TASK + "\n\nCHECK RESULTS:\n" + report[:3000]
                + (("\n\nSECURITY FINDINGS (configuration issues, then CVE details from Debian's security data):\n" if name == "security" else
                     "\n\nCVE DETAILS (from Debian's security data):\n" if name == "cve" else
                     "\n\nSYSTEM LOG (most frequent, with counts):\n") + log_excerpt[:3500] if log_excerpt else ""))
        step("Asking the model to analyse the findings and the system log (one call; on one CPU this can take a minute).")
        out, t = llm.complete([{"role": "system", "content": self._plan_system()}, {"role": "user", "content": user}],
                              llm.INSIGHTS_SCHEMA, max_tokens=500)
        steps, dropped = [], 0
        for x in out.get("next_steps", []):
            toks = [t.strip("`'\".,;") for t in re.sub(r"^\W*(?:run\s+)?(?:sudo\s+)?", "", x).split()]
            if toks and toks[0] in ("show", "config"):  # it suggests a command: it must be a valid one
                argv = None
                for k in range(len(toks), 1, -1):  # longest prefix that is a valid command ("... to check the optic")
                    try:
                        cand = tools.parse_click(" ".join(toks[:k]))
                    except tools.ToolError:
                        continue
                    if clidoc.check_command(cand) is None:
                        argv = cand
                        break
                if argv is None:
                    dropped += 1
                    continue
                if tools.is_mutating(argv):
                    x += " (a change: ask Dial 0 to do it, and it will ask y/N)"
            steps.append(x)
        step(f"Model analysed it in {t.get('llm_s', '?')}s" + (f"; {dropped} suggested command(s) not in the reference dropped"
                                                                 if dropped else "") + ".")
        return {"summary": out.get("summary", ""), "causes": out.get("causes", [])[:4], "next_steps": steps[:4],
                "dropped": dropped}

    def _explain(self, s, request, res, timing):
        if not any(_lines(r["output"]) for r in res if r["exit"] is not None):
            self._step("No output to explain, so no model call.")
            return ""
        text = "\n".join(f"$ {r['command']}\n{r['output'][:1500]}" for r in res)[:4000]
        self._step("Asking the model to explain the output in words (--explain).")
        try:
            out, t = llm.complete([{"role": "system", "content": EXPLAIN_SYSTEM},
                                   {"role": "user", "content": f"Request: {request}\nOutput:\n{text}"}],
                                  llm.ANSWER_SCHEMA, max_tokens=200)
        except llm.LLMError as e:
            self._step(f"The model call failed: {e}")
            return ""
        _add_timing(timing, t)
        self._step(f"Model explained it in {t.get('llm_s', '?')}s.")
        return out.get("answer", "")

    # ------------------------------------------------------------------ agent mode (step by step)
    def _system(self, s: sessions.Session) -> str:
        out = AGENT_SYSTEM
        full = clidoc.full_reference()
        if full:  # fixed part first (cacheable), per-request parts after
            out += "\nCOMMAND REFERENCE: the ONLY commands you may use, from the switch owner's reference file.\n" + full
        if s.ref:
            out += "\nSONiC command reference (real commands, most relevant to this request):\n" + s.ref
        else:
            out += "\n" + (NO_MATCH if clidoc.available() else NO_REF)
        if s.recent and self._ctx:
            out += "\nCommands already run in this session (oldest first):\n" + s.recent
        if self.mcp_tools:
            out += "\nRemote MCP tools:\n" + self.mcp_tools
        return out

    @staticmethod
    def _freeze(s: sessions.Session, request: str):
        """Per-request prompt parts, computed once so the prompt prefix stays stable for llama.cpp's cache."""
        s.ref = clidoc.search(request)
        s.recent = "\n".join(f"- {e['command']} ({e['status']})" for e in s.recent_commands())

    @staticmethod
    def _obs(s: sessions.Session, text: str):
        s.add("user", "OBSERVATION: " + text)

    @staticmethod
    def _pending_reply(s: sessions.Session, note: str = "") -> dict:
        p = s.pending
        cmds = [c["command"] for c in p["commands"]]
        return {"status": "needs_confirmation", "session": s.name, "id": p["id"], "mode": p.get("mode", "fast"),
                "command": "; ".join(cmds),
                "commands": cmds, "changes": [c["command"] for c in p["commands"] if tools.is_mutating(c["argv"])],
                "note": note}

    def _loop(self, s: sessions.Session, granted: bool = False) -> dict:
        trace, rejects, t0, timing = [], 0, time.time(), {}
        system = self._system(s)  # constant for the whole request
        for n in range(1, MAX_STEPS + 1):
            self._step(f"Step {n}: asking the model what to do next.")
            ts = time.time()
            try:
                step = llm.next_step(s.window(system, self._ctx))
            except llm.LLMError as e:
                self._step(f"The model call failed: {e}")
                return {"status": "error", "session": s.name, "error": str(e), "trace": trace}
            timing["llm_calls"] = timing.get("llm_calls", 0) + 1
            s.add("assistant", json.dumps(step))
            tool, inp = step["tool"], step["input"]
            trace.append({"tool": tool, "input": inp})
            why = f" Reasoning: {step['thought']}" if step.get("thought") else ""
            if tool == "final":
                self._step(f"Step {n} ({round(time.time() - ts, 1)}s): done.{why}")
                timing["total_s"] = round(time.time() - t0, 1)
                return {"status": "done", "session": s.name, "answer": inp, "trace": trace, "timing": timing}
            self._step(f"Step {n} ({round(time.time() - ts, 1)}s): {tool} {inp}.{why}")
            try:
                if tool == "click":
                    argv = tools.parse_click(inp)
                    bad = clidoc.check_command(argv)
                    if bad:
                        self._step(f"  rejected, not in the SONiC reference: {bad}")
                        s.record(inp, "rejected", bad)
                        rejects += 1
                        if rejects >= MAX_REJECTS:
                            self._step(f"  {MAX_REJECTS} invalid commands in this request, so I stop instead of guessing.")
                            close = clidoc.search(s.last_request(), 5)
                            ans = ("I don't know a valid SONiC command for that request. The commands I tried are not in the "
                                   "SONiC reference for this device, so I did not run them." + (f"\nClosest real commands:\n{close}" if close else ""))
                            return {"status": "done", "session": s.name, "answer": ans, "trace": trace, "timing": timing}
                        self._obs(s, "REJECTED, this command does not exist. " + bad + " Use ref to find the real command, or say you don't know.")
                        continue
                    if tools.is_mutating(argv) and CONFIRM != "auto" and not (granted and CONFIRM == "ask"):
                        if CONFIRM == "dry-run":
                            self._step("  changes config; CONFIRM=dry-run, so not run.")
                            s.record(inp, "dry-run")
                            self._obs(s, f"DRY-RUN, not executed: {inp}")
                            continue
                        self._step("  this changes config, so I'm asking for your approval (it covers the rest of this request).")
                        s.pending = {"id": uuid.uuid4().hex[:8], "mode": "agent", "commands": [{"argv": argv, "command": inp}],
                                     "context": self._ctx}
                        s.save()
                        r = self._pending_reply(s)
                        r["trace"] = trace
                        return r
                    if tools.is_mutating(argv):
                        self._step("  changes config: already approved for this request.")
                    out = tools.run_click(argv)
                    code, body = tools.split_result(out)
                    self._step(f"  result: exit {code}, {_lines(body)} line(s) of output", body[:800])
                    s.record(inp, "executed" if code == 0 else "failed", body[:150])
                    self._obs(s, out)
                elif tool == "ref":
                    r = clidoc.ref(inp)
                    self._step(f"  reference lookup: {'not found' if r.startswith('NOT FOUND') else str(len(r.splitlines())) + ' line(s)'}", r[:800])
                    self._obs(s, r)
                elif tool == "grep":
                    r = tools.run_grep(inp)
                    self._step(f"  grep: {_lines(tools.split_result(r)[1])} line(s)", r[:800])
                    self._obs(s, r)
                elif tool == "mcp":
                    r = mcp_adapter.call(inp)
                    self._step("  remote MCP tool called", r[:800])
                    self._obs(s, r)
            except tools.ToolError as e:
                self._step(f"  error: {e}")
                self._obs(s, f"ERROR: {e}")
        self._step(f"Stopped after {MAX_STEPS} steps.")
        return {"status": "done", "session": s.name, "answer": "Stopped: step limit reached.", "trace": trace, "timing": timing}

    # ------------------------------------------------------------------ warm-up
    def warm_up(self):
        """Pre-load the static planner prompt into llama.cpp's cache (WARMUP=on only: it occupies the one core
        for minutes at startup, and a request arriving meanwhile would wait behind it)."""
        if WARMUP == "off" or (WARMUP == "auto" and not clidoc.curated()):
            return
        try:
            llm.complete([{"role": "system", "content": self._plan_system()},
                          {"role": "user", "content": "Reference (real commands on this switch):\n- show vlan brief\nRequest: show vlans"}],
                         PLAN_SCHEMA, max_tokens=8)
        except Exception:
            pass
