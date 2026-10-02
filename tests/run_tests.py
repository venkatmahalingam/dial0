#!/usr/bin/env python3
"""Offline test suite for Dial 0: no Docker, no model, no SONiC needed.

  python3 tests/run_tests.py

Uses a small fixture tree that mimics sonic-utilities' Click patterns, a stubbed LLM and a stubbed
command executor. Verifies: command extraction, validation, `ref`, the no-guess give-up path,
approvals, sessions (persistence, isolation, context window), prompt-cache stability, HTTP API + CLI.
"""
import json, os, re, shutil, socket, subprocess, sys, tempfile, threading, time, types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "tests", "fixtures", "sonic-utilities")
TMP = tempfile.mkdtemp(prefix="dial0-test-")
INDEX = os.path.join(TMP, "commands.json")

try:
    import httpx  # noqa: F401
except ImportError:  # the LLM is stubbed anyway
    sys.modules["httpx"] = types.SimpleNamespace(HTTPError=Exception, post=None)

os.environ.update(DIAL0_STATE_DIR=os.path.join(TMP, "state"), DIAL0_CMD_INDEX=INDEX, DIAL0_SRC_ROOT=FIX,
                  DIAL0_HOST_EXEC="local", DIAL0_CONFIRM="ask", DIAL0_CLIDOC_BUDGET="0", DIAL0_CTX_CHARS="9000")
sys.path.insert(0, ROOT)

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond:
        FAILS.append(name)


# ------------------------------------------------------------------ 1. extraction
r = subprocess.run([sys.executable, os.path.join(ROOT, "dial0", "extract_cli.py"), "--root", FIX, "--out", INDEX,
                    "--ref", "fixture", "--min-commands", "5"], capture_output=True, text=True)
check("extractor runs", r.returncode == 0, r.stderr + r.stdout)
nodes = json.load(open(INDEX))["nodes"]
check("extractor: add_command across modules", "config vlan add" in nodes)
check("extractor: nested group with name=", "config interface ip add" in nodes)
check("extractor: package module (show/interfaces/__init__)", "show interfaces transceiver eeprom" in nodes)
check("extractor: plugins register(cli)", "config pluginthing" in nodes and "show plug" in nodes and "show other" in nodes)
check("extractor: usage with options/args", nodes["config vlan member add"]["usage"] == "config vlan member add [-u|--untagged] <vid> <port>",
      nodes["config vlan member add"]["usage"])
check("extractor: unknown decorator marks options incomplete", nodes["config interface shutdown"]["opts_complete"] is False)
check("extractor: no unresolved edges", "unresolved edges: 0" in r.stdout, r.stdout)
check("extractor: a group that also runs by itself is runnable (show interfaces counters)",
      nodes["show interfaces counters"].get("runnable") is True and "show interfaces counters errors" in nodes
      and nodes["show interfaces counters"]["usage"].startswith("show interfaces counters [-i|--interface"), nodes.get("show interfaces counters"))
r2 = subprocess.run([sys.executable, os.path.join(ROOT, "dial0", "extract_cli.py"), "--root", FIX, "--out", INDEX + ".x",
                     "--min-commands", "999"], capture_output=True, text=True)
check("extractor: fails loudly when too few commands", r2.returncode != 0)

# ------------------------------------------------------------------ 2. validation / ref
from dial0 import clidoc, tools, llm, sessions, agent, server, resolve, health  # noqa: E402

clidoc.load_static()


def v(cmd):
    return clidoc.validate(tools.parse_click(cmd))


check("valid command accepted", v("config vlan add 100") is None)
check("valid option accepted", v("config vlan member add -u 100 Ethernet1") is None)
check("sudo prefix accepted", v("sudo config vlan add 100") is None)
check("--help accepted anywhere", v("config vlan --help") is None)
check("invented subcommand rejected", "no subcommand 'create'" in (v("config vlan create 100") or ""))
check("typo rejected with suggestion", "Did you mean: member" in (v("config vlan memebr add 100 Ethernet1") or ""))
check("invented option rejected", "no option '--tagged'" in (v("config vlan add --tagged 100") or ""))
check("options not checked when source incomplete", v("config interface shutdown --foo Ethernet0") is None)
check("leaf arguments not treated as subcommands", v("show interfaces status Ethernet0") is None)
try:
    tools.parse_click("config reload -y"); check("config reload blocked", False)
except tools.ToolError:
    check("config reload blocked", True)
try:
    tools.parse_click("rm -rf /"); check("non-allow-listed binary blocked", False)
except tools.ToolError:
    check("non-allow-listed binary blocked", True)
check("show is read-only", not tools.is_mutating(["show", "vlan", "brief"]))
check("config is mutating", tools.is_mutating(["config", "vlan", "add", "100"]))
check("db read is read-only", not tools.is_mutating(["sonic-db-cli", "CONFIG_DB", "HGETALL", "VLAN|Vlan100"]))
check("db write is mutating", tools.is_mutating(["sonic-db-cli", "CONFIG_DB", "HSET", "x", "a", "b"]))

check("search finds real commands", "config vlan member add" in clidoc.search("add Ethernet1 to vlan 100 untagged"))
check("search returns nothing for nonsense", clidoc.search("reticulate the splines") == "")
check("ref show", "--untagged" in clidoc.ref("show config vlan member add"))
check("ref show unknown -> NOT FOUND", clidoc.ref("show config vlan frobnicate").startswith("NOT FOUND"))
check("ref find unknown -> NOT FOUND", clidoc.ref("find quantum teleport").startswith("NOT FOUND"))
check("ref src greps bundled source", "add_vlan_member" in clidoc.ref("src def add_vlan_member"))

clidoc._live["config"] = {"help": "", "usage": "u", "children": ["vlan", "interface", "save"], "crawled": True}
clidoc._live["config vlan"] = {"help": "", "usage": "u", "children": ["add", "member"], "crawled": True}
check("device wins over source (missing on this SONiC version)", "NOT on this device" in (v("config vlan del 100") or ""))
check("device-missing command hidden from search", "config vlan del" not in clidoc.search("delete vlan"))
clidoc._live.clear()


# ------------------------------------------------------------------ 2b. batch execution (real shell)
r = tools.run_batch([["echo", "first"], ["echo", "it's \"quoted\" $HOME & ; | `x`"], ["echo", "third"]], [True, True, False])
check("batch: all commands run in one invocation, in order", [x["exit"] for x in r] == [0, 0, 0] and r[0]["output"] == "first" and r[2]["output"] == "third", r)
check("batch: arguments with quotes/$/;/| passed literally (no shell injection)", r[1]["output"] == "it's \"quoted\" $HOME & ; | `x`", r[1])
r = tools.run_batch([["echo", "a"], ["sh", "-c", "echo boom; exit 3"], ["echo", "never"]], [True, True, True])
check("batch: failed change stops the rest", r[0]["exit"] == 0 and r[1]["exit"] == 3 and r[1]["output"] == "boom" and r[2]["ran"] is False, r)
r = tools.run_batch([["sh", "-c", "exit 1"], ["echo", "still"]], [False, False])
check("batch: failed read-only command does not stop the batch", r[0]["exit"] == 1 and r[1]["output"] == "still", r)
r = tools.run_batch([["sh", "-c", "read x; echo got:$x"]], [True])
check("batch: commands get no stdin (a y/N prompt can't hang)", r[0]["output"] == "got:", r)

# ------------------------------------------------------------------ 3. agent flows
EXEC = []
tools.run_click = lambda argv, max_out=2000: (EXEC.append(argv) or "[exit 0]\nok")
BATCHES = []


def fake_batch(argvs, mutating, max_out=0):
    BATCHES.append(list(argvs)); EXEC.extend(argvs)
    return [{"exit": 0, "output": "ok", "ran": True} for _ in argvs]


tools.run_batch = fake_batch
SYSTEMS = []


def script(steps):
    it = iter(steps)

    def fake(messages):
        SYSTEMS.append(messages[0]["content"])
        return next(it)
    llm.next_step = fake


A = agent.Agent()

script([{"thought": "", "tool": "click", "input": "config vlan create 100"},
        {"thought": "", "tool": "click", "input": "config vlan new 100"},
        {"thought": "", "tool": "click", "input": "config vlan make 100"}])
res = A.query("create vlan 100", "t-guess", "agent")
check("repeated invented commands -> deterministic I don't know", res["answer"].startswith("I don't know"), res)
check("nothing executed on invented commands", EXEC == [])
check("rejections are logged", [e["status"] for e in sessions.get("t-guess").log] == ["rejected", "rejected"])

script([{"thought": "", "tool": "ref", "input": "show config vlan member add"},
        {"thought": "", "tool": "click", "input": "show vlan brief"},
        {"thought": "", "tool": "click", "input": "config vlan member add -u 100 Ethernet1"}])
SYSTEMS.clear()
res = A.query("put Ethernet1 in vlan 100 untagged", "t-ok", "agent")
check("agent: mutating command waits for approval", res["status"] == "needs_confirmation" and res["commands"][0].startswith("config vlan member"), res)
check("read-only command ran without approval", EXEC == [["show", "vlan", "brief"]], EXEC)
check("system prompt constant within a request (prompt cache)", len(set(SYSTEMS)) == 1, len(set(SYSTEMS)))


script([{"thought": "", "tool": "final", "input": "Done: Ethernet1 is an untagged member of Vlan100."}])
res = A.confirm("t-ok", res["id"], True)
check("approved command executed", EXEC[-1] == ["config", "vlan", "member", "add", "-u", "100", "Ethernet1"])
check("loop continues to final after approval", res["status"] == "done" and res["answer"].startswith("Done"), res)

script([{"thought": "", "tool": "click", "input": "config vlan add 200"},
        {"thought": "", "tool": "final", "input": "Not done, you denied it."}])
res = A.query("add vlan 200", "t-deny", "agent")
n = len(EXEC)
res = A.confirm("t-deny", res["id"], False)
check("denied command not executed", len(EXEC) == n and sessions.get("t-deny").log[-1]["status"] == "denied")

def boom(m):
    raise llm.LLMError("inference engine error: connection refused")
llm.next_step = boom
res = A.query("show vlans", "t-err", "agent")
check("LLM failure returns a clean error", res["status"] == "error" and "connection refused" in res["error"])



# agent mode: one y/N per request, even when the model finds several changes step by step
EXEC.clear()
script([{"thought": "", "tool": "click", "input": "config vlan add 400"},
        {"thought": "", "tool": "click", "input": "config vlan member add -u 400 Ethernet1"},
        {"thought": "", "tool": "click", "input": "show vlan brief"},
        {"thought": "", "tool": "final", "input": "done"}])
res = A.query("vlan 400 with Ethernet1", "t-grant", "agent")
check("agent: first change asks once", res["status"] == "needs_confirmation" and res["mode"] == "agent" and EXEC == [])
res = A.confirm("t-grant", res["id"], True)
check("agent: approval covers the rest of the request (no second y/N)", res["status"] == "done" and EXEC == [
    ["config", "vlan", "add", "400"], ["config", "vlan", "member", "add", "-u", "400", "Ethernet1"], ["show", "vlan", "brief"]], (res, EXEC))
script([{"thought": "", "tool": "click", "input": "config vlan add 401"}])
res = A.query("vlan 401", "t-grant", "agent")
check("agent: approval does not carry over to the next request", res["status"] == "needs_confirmation")
A.confirm("t-grant", res["id"], False) if False else None
sessions.get("t-grant").pending = None

# ------------------------------------------------------------------ 3b. fast mode (default)
CALLS = []


def plan_stub(*replies):
    it = iter(replies)

    def fake(messages, schema, max_tokens=200):
        CALLS.append({"messages": messages, "schema": schema})
        return next(it), {"llm_s": 1.0, "prompt_tokens": 500, "prompt_s": 0.8, "cached_tokens": 400, "gen_tokens": 30, "gen_s": 0.2}
    llm.complete = fake


EXEC.clear(); CALLS.clear()
plan_stub()
res = A.query("show vlan brief", "f1")
check("fast: typed command runs with ZERO model calls", CALLS == [] and EXEC == [["show", "vlan", "brief"]] and res["how"] == "direct", (CALLS, EXEC))
check("fast: output returned to user", res["results"][0]["output"] == "ok" and res["results"][0]["exit"] == 0)

EXEC.clear(); CALLS.clear()
res = A.query("config vlan add 100", "f1")
check("fast: typed config command -> approval, no model call", res["status"] == "needs_confirmation" and CALLS == [] and EXEC == [])
res = A.confirm("f1", res["id"], True)
check("fast: approved typed command executed", EXEC == [["config", "vlan", "add", "100"]] and res["status"] == "done")

EXEC.clear(); CALLS.clear()
plan_stub({"commands": ["show vlan brief"], "note": ""})
res = A.query("which vlans is Ethernet1 a member of", "f2")
check("fast: English read request = exactly 1 model call", len(CALLS) == 1 and EXEC == [["show", "vlan", "brief"]], (len(CALLS), EXEC))
check("fast: planner prompt is small and has the reference", "config vlan" in CALLS[0]["messages"][1]["content"] or "show vlan" in CALLS[0]["messages"][1]["content"])
check("fast: timing reported", res["timing"]["llm_calls"] == 1 and "total_s" in res["timing"])

EXEC.clear(); CALLS.clear()
plan_stub({"commands": ["config vlan add 100", "config vlan member add -u 100 Ethernet1", "show vlan brief"], "note": "Adds VLAN 100."})
res = A.query("create vlan 100 with Ethernet1 untagged", "f3")
check("fast: change = 1 model call, then one approval for the batch", len(CALLS) == 1 and res["status"] == "needs_confirmation"
      and res["commands"] == ["config vlan add 100", "config vlan member add -u 100 Ethernet1", "show vlan brief"]
      and res["changes"] == ["config vlan add 100", "config vlan member add -u 100 Ethernet1"], res)
check("fast: nothing runs before approval", EXEC == [])
res = A.confirm("f3", res["id"], True)
check("fast: whole batch executed at once (one host call)", len(BATCHES) >= 1 and len(BATCHES[-1]) == 3, BATCHES[-1:])
check("fast: all commands run in order after approval, no more model calls", len(CALLS) == 1 and EXEC == [
    ["config", "vlan", "add", "100"], ["config", "vlan", "member", "add", "-u", "100", "Ethernet1"], ["show", "vlan", "brief"]], EXEC)
check("fast: results returned", [r["command"] for r in res["results"]][-1] == "show vlan brief")

EXEC.clear(); CALLS.clear()
agent.MODEL_REPAIR = True  # the next tests exercise the optional 2nd (repair) model call
plan_stub({"commands": ["config vlan create 100"], "note": ""}, {"commands": ["config vlan add 100"], "note": ""})
res = A.query("make vlan 100", "f4")
check("fast: invalid command -> 1 repair call with validator feedback", len(CALLS) == 2 and "invalid commands" in CALLS[1]["messages"][1]["content"]
      and res["status"] == "needs_confirmation" and res["commands"] == ["config vlan add 100"], (len(CALLS), res))
A.confirm("f4", res["id"], False)
check("fast: denied batch not executed", EXEC == [])

EXEC.clear(); CALLS.clear()
plan_stub({"commands": ["config vlan create 100"], "note": ""}, {"commands": ["config vlan new 100"], "note": ""})
res = A.query("make vlan 100", "f5")
check("fast: still invalid after repair -> I don't know, nothing run", res["answer"].startswith("I don't know") and EXEC == [] and len(CALLS) == 2, res)

EXEC.clear(); CALLS.clear()
plan_stub()
res = A.query("reticulate the splines", "f6")
check("fast: nothing in reference -> I don't know with ZERO model calls", res["answer"].startswith("I don't know") and CALLS == [])

EXEC.clear(); CALLS.clear()
plan_stub({"commands": [], "note": "Which VLAN ID should I create?"})
res = A.query("create a vlan", "f7")
check("fast: model may ask for missing info (plus closest real commands)", res["answer"].startswith("Which VLAN ID should I create?")
      and "Closest real SONiC commands" in res["answer"] and EXEC == [], res["answer"])

EXEC.clear(); CALLS.clear()
plan_stub({"answer": "There is one VLAN, 100."})
res = A.query("how many vlans are there", "f8", explain=True)
check("fast: --explain adds exactly one call (commands found without the model)", len(CALLS) == 1 and res["how"] == "intent"
      and res["answer"] == "There is one VLAN, 100.", (len(CALLS), res.get("how")))

agent.CONFIRM = "dry-run"
EXEC.clear(); CALLS.clear()
plan_stub({"commands": ["config vlan add 100", "show vlan brief"], "note": ""})
res = A.query("add vlan 100", "f9")
check("fast: dry-run runs only read-only commands", EXEC == [["show", "vlan", "brief"]] and "DRY-RUN" in res["answer"], (EXEC, res))
agent.CONFIRM = "ask"

check("fast: planner system prompt identical across requests (cacheable)",
      len({c["messages"][0]["content"] for c in CALLS if c["schema"] is agent.PLAN_SCHEMA}) <= 1)
hist = A._history(sessions.get("f3"))
check("fast: history is compact (requests + what ran, no raw output)", "user: create vlan 100" in hist and "done:" in hist
      and "(ok)" in hist and len(hist) < 600, hist)

import threading as _th
spawned = []
_orig_raw = tools.run_raw
tools.run_raw = lambda argv, timeout=20: (spawned.append(time.time()) or "")
clidoc.PACE_S = 0
clidoc.BUSY.set()
started = time.time()
_th.Timer(0.6, clidoc.BUSY.clear).start()
clidoc._crawl(time.time() + 5)
check("crawler waits while a request is being served", spawned and spawned[0] - started >= 0.5, spawned[:1])
tools.run_raw = _orig_raw
clidoc._live.clear()


# ------------------------------------------------------------------ 3c. steps / reasoning
def msgs(res):
    return " || ".join(x["msg"] for x in res.get("steps", []))


EXEC.clear(); CALLS.clear()
plan_stub()
res = A.query("show vlan brief", "r1")
m = msgs(res)
check("steps: typed command explained (no model)", "is a real SONiC command" in m and "No model needed" in m and "read-only" in m
      and "all 1 command(s) done" in m, m)

plan_stub({"why": "show vlan brief lists all VLANs.", "commands": ["show vlan brief"], "note": ""})
res = A.query("list vlans that include Ethernet1", "r2")
m = msgs(res)
check("steps: reference search shown", "Searched the SONiC command reference" in m, m)
check("steps: model reasoning shown", "Model's reasoning: show vlan brief lists all VLANs." in m, m)
check("steps: proposal + validation shown", "Model proposed: show vlan brief" in m and "all 1 command(s) exist" in m, m)
check("steps: reference list kept as detail", any("detail" in x and "show vlan brief" in x["detail"] for x in res["steps"]))
check("steps: timestamps increase", all(a["t"] <= b["t"] for a, b in zip(res["steps"], res["steps"][1:])))

plan_stub({"why": "", "commands": ["config vlan create 100"], "note": ""}, {"why": "", "commands": ["config vlan add 100"], "note": ""})
res = A.query("make vlan 100", "r3")
m = msgs(res)
check("steps: rejection + repair explained", "Rejected 'config vlan create 100'" in m and "asking it to fix 1 command" in m, m)
check("steps: approval reason shown", "need your approval (one y/N for all)" in m, m)
n_before = len(sessions.get("r3").last_trace)
res = A.confirm("r3", res["id"], True, rid="conf-1")
check("steps: after approval only new steps are streamed", res["steps"][0]["msg"] == "You approved." and "Rejected" not in msgs(res), msgs(res))
check("steps: why keeps the whole story", len(sessions.get("r3").last_trace) > n_before
      and "Rejected" in " ".join(x["msg"] for x in sessions.get("r3").last_trace))

plan_stub({"why": "", "commands": ["config vlan make 1"], "note": ""}, {"why": "", "commands": ["config vlan new 1"], "note": ""})
res = A.query("vlan 1", "r4")
check("steps: give-up explained", "won't guess further" in msgs(res), msgs(res))
agent.MODEL_REPAIR = False  # back to the default

plan_stub()
res = A.query("reticulate the splines", "r5")
check("steps: no-match explained (no model)", "without asking the model to guess" in msgs(res))

script([{"thought": "check vlans first", "tool": "click", "input": "show vlan brief"},
        {"thought": "all good", "tool": "final", "input": "VLAN 100 exists."}])
res = A.query("does vlan 100 exist", "r6", "agent")
m = msgs(res)
check("steps: agent mode shows each step with its reasoning", "Step 1" in m and "Reasoning: check vlans first" in m
      and "result: exit 0" in m and "Step 2" in m and "done. Reasoning: all good" in m, m)

# live: steps are visible while the model is still working
import threading as _th2
def slow(messages, schema, max_tokens=200):
    time.sleep(1.5)
    return {"why": "x", "commands": ["show vlan brief"], "note": ""}, {"llm_s": 1.5}
llm.complete = slow
box = {}
t = _th2.Thread(target=lambda: box.update(r=A.query("what vlans contain Ethernet4", "r7", rid="live-1")))
t.start(); time.sleep(0.5)
live = [x["msg"] for x in A.progress.get("live-1", [])]
t.join()
check("steps: streamed live while the model is working", any("Asking the model" in x for x in live)
      and not any("Model answered" in x for x in live), live)

check("reasoning can be turned off (schema has no 'why')", "why" not in llm.plan_schema(False)["properties"]
      and list(llm.plan_schema(True)["properties"])[0] == "why")


# ------------------------------------------------------------------ 3d. policy: only `config` changes the switch
def v2(cmd):
    return clidoc.validate(tools.parse_click(cmd))
check("policy: Linux 'ip addr add' refused (bypasses SONiC)", "changes must be made with SONiC's 'config' command" in (v2("ip addr add 123.1.1.1/24 dev vlan.20") or ""))
check("policy: refusal points to the real config command", "config interface ip add" in (v2("ip addr add 123.1.1.1/24 dev vlan.20") or ""))
check("policy: 'ip addr show' still allowed (read-only)", v2("ip addr show") is None)
check("policy: vtysh show allowed, vtysh config refused", v2('vtysh -c "show ip bgp summary"') is None and v2('vtysh -c "configure terminal"') is not None)
check("policy: sonic-db-cli write refused, read allowed", v2("sonic-db-cli CONFIG_DB HSET a b c") is not None and v2("sonic-db-cli CONFIG_DB HGETALL a") is None)
check("SONiC form accepted", v2("config interface ip add Vlan20 123.1.1.1/24") is None)

EXEC.clear(); CALLS.clear()
plan_stub({"why": "", "commands": ["ip addr add 123.1.1.1/24 dev vlan.20"], "note": ""},
          {"why": "config interface ip add sets an IP on Vlan20.", "commands": ["config interface ip add Vlan20 123.1.1.1/24", "show vlan brief"], "note": ""})
res = A.query("Configure ip address 123.1.1.1/24 on vlan 20", "p1")
check("your case: 'ip addr add' corrected to 'config interface ip add Vlan20 ...' by code, with ONE model call",
      res["status"] == "needs_confirmation" and res["commands"][0] == "config interface ip add Vlan20 123.1.1.1/24" and EXEC == []
      and len(CALLS) == 1 and "Fixed without the model" in msgs(res), (len(CALLS), res))
check("your case: the reference given to the model contains config interface ip add",
      "config interface ip add" in CALLS[0]["messages"][1]["content"])
check("planner prompt teaches SONiC names and config-only changes", "VLAN 20 -> Vlan20" in agent.PLAN_SYSTEM and "ONLY with \"config ...\"" in agent.PLAN_SYSTEM)

res = A.confirm("p1", res["id"], False)
check("'N' ends the request immediately: nothing run, no model call", EXEC == [] and len(CALLS) == 1 and res["answer"] == "Nothing was changed.")

plan_stub({"why": "", "commands": ["config vlan add 30"], "note": ""}, {"why": "", "commands": ["show vlan brief"], "note": ""})
res = A.query("add vlan 30", "p2")
res = A.query("show vlans", "p2")
check("a new request cancels an unanswered approval (nothing from it runs)",
      res["status"] == "done" and ["config", "vlan", "add", "30"] not in EXEC and "Cancelled the earlier unanswered approval" in msgs(res)
      and sessions.get("p2").log[0]["status"] == "cancelled", (res, EXEC))

from dial0 import cli as _cli
import builtins
def answers(*xs):
    it = iter(xs)
    def fake_input(prompt=""):
        x = next(it)
        if isinstance(x, BaseException):
            raise x
        return x
    return fake_input
_orig_input = builtins.input
for seq, want, label in [(("N",), False, "N"), (("n",), False, "n"), (("no",), False, "no"), (("",), False, "Enter"),
                         (("Y",), True, "Y"), (("yes",), True, "yes"), (("maybe", "N"), False, "junk then N"),
                         ((EOFError(),), False, "end of input"), ((KeyboardInterrupt(),), False, "Ctrl-C")]:
    builtins.input = answers(*seq)
    check(f"y/N prompt: {label} -> {'yes' if want else 'no'}", _cli.ask_yes_no("?") is want)
builtins.input = _orig_input


# ------------------------------------------------------------------ 3e. no unnecessary model calls
CALLS.clear(); EXEC.clear()
plan_stub()
res = A.query("show me the vlans", "n1")
check("no-model: plain read-only question answered by intent (0 calls)", CALLS == [] and res["how"] == "intent"
      and EXEC == [["show", "vlan", "brief"]] and "No model needed" in msgs(res), (CALLS, res.get("how")))
check("no-model: intent not used when the request changes something", resolve.intent("add a vlan") is None)
check("no-model: intent not used when the request has specific values", resolve.intent("status of vlan 20") is None)
_intents = list(resolve.INTENTS)
resolve.INTENTS.append(({"interface", "vlan"}, "show vlan config"))  # same specificity as interface+status
check("no-model: ambiguous intent left to the model", resolve.intent("interface status vlan") is None)
resolve.INTENTS[:] = _intents
check("no-model: intent only for commands that exist here", resolve.intent("show ntp") is None and resolve.intent("show version") is not None)

CALLS.clear(); EXEC.clear()
res = A.query("create vlan 200 with Ethernet5 untagged", "n2")
check("no-model: learned pattern reused with new values (0 calls)", CALLS == [] and res["how"] == "learned"
      and res["commands"] == ["config vlan add 200", "config vlan member add -u 200 Ethernet5", "show vlan brief"], (CALLS, res))
check("no-model: learned plan still asks y/N for changes", res["status"] == "needs_confirmation")
A.confirm("n2", res["id"], False)
CALLS.clear()
plan_stub({"why": "", "commands": ["config vlan add 200", "config vlan member add -u 200 Ethernet5", "show vlan brief"], "note": ""})
res = A.query("create vlan 200 with Ethernet5 untagged", "n2")
check("no-model: a declined plan is forgotten (model asked again)", len(CALLS) == 1 and res["how"] == "planned", (len(CALLS), res.get("how")))
A.confirm("n2", res["id"], False)

CALLS.clear(); EXEC.clear()
plan_stub()
res = A.query("which vlans is Ethernet1 a member of", "n3")
check("no-model: identical earlier read-only request reused (0 calls)", CALLS == [] and res["how"] == "learned", (CALLS, res.get("how")))

CALLS.clear()
plan_stub({"why": "", "commands": ["config vlan memebr add -u 300 ethernet.4"], "note": ""})
res = A.query("put ethernet 4 untagged into vlan 300", "n4")
check("no-model: typo + interface name fixed by code (still 1 call)", len(CALLS) == 1
      and res["commands"] == ["config vlan member add -u 300 Ethernet4"], (len(CALLS), res))
A.confirm("n4", res["id"], False)

CALLS.clear()
plan_stub({"why": "", "commands": ["config vlan frobnicate 300"], "note": ""}, {"why": "", "commands": ["config vlan add 300"], "note": ""})
res = A.query("frobnicate vlan 300", "n5")
check("no-model: unfixable command -> I don't know after ONE call (no repair call by default)", len(CALLS) == 1
      and res["answer"].startswith("I don't know") and "REPAIR_WITH_MODEL=off" in msgs(res), (len(CALLS), res))

steps_called = []
def one_step(messages):
    steps_called.append(1)
    return {"thought": "", "tool": "click", "input": "config vlan add 500"}
llm.next_step = one_step
res = A.query("add vlan 500", "n6", "agent")
A.confirm("n6", res["id"], False)
check("no-model: agent mode 'N' ends without another model call", len(steps_called) == 1, len(steps_called))

CALLS.clear()
fake_batch_orig = tools.run_batch
tools.run_batch = lambda argvs, mutating, max_out=0: [{"exit": 0, "output": "(no output)", "ran": True} for _ in argvs]
res = A.query("show me the vlans", "n7", explain=True)
check("no-model: --explain skipped when there is no output", CALLS == [] and "No output to explain" in msgs(res))
tools.run_batch = fake_batch_orig

CALLS.clear()
A.warm_up()
check("no-model: no warm-up call at startup by default", CALLS == [])

check("plans can be listed", any(p["key"] == "tmpl:which vlans is {0} a member of" for p in resolve.list_plans()),
      [p["key"] for p in resolve.list_plans()])
_saved = resolve._ref_id
resolve._ref_id = lambda: "other-ref"
check("learned plans ignored after the command reference changes", resolve.recall("which vlans is Ethernet1 a member of") is None)
resolve._ref_id = _saved
resolve.clear_plans()
check("plans can be cleared", resolve.list_plans() == [])


# ------------------------------------------------------------------ 3f. context: off by default, on when asked
agent.LEARN = False  # so every request below really reaches the model (learned plans would answer them)
CALLS.clear()
plan_stub({"why": "", "commands": ["show vlan brief"], "note": ""}, {"why": "", "commands": ["show vlan brief"], "note": ""},
          {"why": "", "commands": ["show vlan brief"], "note": ""})
A.query("which vlans have Ethernet9", "c1")
res_off = A.query("which vlans have Ethernet7", "c1")
p_off = CALLS[-1]["messages"][1]["content"]
res_on = A.query("which vlans have Ethernet5", "c1", context=True)
p_on = CALLS[-1]["messages"][1]["content"]
agent.LEARN = True
check("context: default request gets no earlier history", len(CALLS) == 3 and "Recent in this session" not in p_off
      and "Ethernet9" not in p_off, p_off)
check("context: --context includes earlier requests and what ran", "Recent in this session" in p_on and "Ethernet7" in p_on
      and "show vlan brief (ok)" in p_on, p_on)
check("context: steps say which it is", "Fresh request" in msgs(res_off) and "Including context of session 'c1'" in msgs(res_on))

sx = sessions.get("c2")
sx.add("user", "EARLIER REQUEST about vlan 5"); sx.add("assistant", json.dumps({"thought": "", "tool": "final", "input": "x"}))
sx.add("user", "current request"); sx.add("assistant", json.dumps({"thought": "", "tool": "click", "input": "show vlan brief"}))
sx.add("user", "OBSERVATION: ok")
w_off = sx.window("SYS", context=False)
w_on = sx.window("SYS", context=True)
check("context: agent mode without context sees only the current request and its steps",
      not any("EARLIER REQUEST" in m["content"] for m in w_off) and w_off[1]["content"] == "current request" and len(w_off) == 4, w_off)
check("context: agent mode with context sees earlier requests", any("EARLIER REQUEST" in m["content"] for m in w_on))


# ------------------------------------------------------------------ 3g. finding the right commands at real-world scale
sys.path.insert(0, os.path.join(ROOT, "tests", "fixtures"))
from realistic_commands import build_index, COMMANDS as REAL_CMDS
_saved_static, _saved_meta = dict(clidoc._static), dict(clidoc._meta)
clidoc._static.clear(); clidoc._static.update(build_index()); clidoc._live.clear()
RANK = [
 ("configure vlan 200 and assign ip address 123.2.2.1/24", ["config vlan add", "config interface ip add"], 3),
 ("Configure ip address 123.1.1.1/24 on vlan 20", ["config interface ip add"], 2),
 ("create vlan 100 with Ethernet1 untagged", ["config vlan add", "config vlan member add"], 3),
 ("add Ethernet4 untagged to vlan 20", ["config vlan member add"], 2),
 ("delete vlan 100", ["config vlan del"], 1),
 ("remove ip 10.0.0.1/24 from Ethernet4", ["config interface ip remove"], 1),
 ("shutdown Ethernet8", ["config interface shutdown"], 1),
 ("bring up Ethernet8", ["config interface startup"], 1),
 ("set mtu 9100 on Ethernet0", ["config interface mtu"], 1),
 ("change the speed of Ethernet0 to 100000", ["config interface speed"], 1),
 ("add portchannel 10 with members Ethernet0 and Ethernet4", ["config portchannel add", "config portchannel member add"], 3),
 ("create loopback 1", ["config loopback add"], 1),
 ("assign 10.1.1.1/32 to loopback 0", ["config interface ip add"], 2),
 ("add static route 10.1.0.0/16 via 10.0.0.1", ["config route add"], 1),
 ("save the config", ["config save"], 1),
 ("show bgp neighbors", ["show ip bgp neighbors"], 2),
 ("bgp summary", ["show ip bgp summary"], 2),
 ("is Ethernet4 up", ["show interfaces status"], 2),
 ("show interface counters", ["show interfaces counters"], 1),
 ("list ip interfaces", ["show ip interfaces"], 2),
 ("show the routing table", ["show ip route"], 1),
 ("what is the switch uptime", ["show uptime"], 1),
 ("show the mac table for vlan 10", ["show mac"], 1),
 ("add ntp server 10.0.0.5", ["config ntp add"], 1),
 ("add a static mac 00:11:22:33:44:55 on vlan 10 port Ethernet4", ["config mac add"], 2),
 ("shut down bgp neighbor 10.0.0.2", ["config bgp shutdown neighbor"], 2),
 ("set hostname to leaf1", ["config hostname"], 1),
 ("add dhcp relay 10.0.0.9 to vlan 20", ["config vlan dhcp_relay add"], 1),
]
missed = []
for q, want, within in RANK:
    got = clidoc.search_paths(q, 8)
    if not all(w in got[:within] for w in want):
        missed.append((q, got[:4]))
check(f"search: right commands at the top for {len(RANK) - len(missed)}/{len(RANK)} typical requests ({len(REAL_CMDS)} real commands)",
      not missed, missed)
check("search: a change request also brings a show command to verify with",
      any(p.startswith("show vlan") for p in clidoc.search_paths("create vlan 300", 8)))

# your exact failing requests, end to end
CALLS.clear(); EXEC.clear()
plan_stub({"why": "", "commands": ["config vlan add 200", "config interface ip add Vlan200 123.2.2.1/24", "show vlan brief"], "note": ""})
res = A.query("configure vlan 200 and assign ip address 123.2.2.1/24", "u1")
ref_lines = CALLS[0]["messages"][1]["content"].split("Reference (real commands on this switch):\n")[1].splitlines()
check("your case: the model is now shown config vlan add + config interface ip add first",
      ref_lines[0].startswith("- config vlan add") and ref_lines[1].startswith("- config interface ip add"), ref_lines[:4])
check("your case: the plan validates and waits for one y/N", res["status"] == "needs_confirmation"
      and res["commands"][:2] == ["config vlan add 200", "config interface ip add Vlan200 123.2.2.1/24"], res)
A.confirm("u1", res["id"], False)

CALLS.clear()
res = A.query("search for the correct command list from the SONiC command reference", "u1")
check("reference question answered with ZERO model calls, using the previous request", CALLS == [] and res["how"] == "lookup"
      and "config vlan add" in res["answer"] and "config interface ip add" in res["answer"]
      and "configure vlan 200 and assign ip address 123.2.2.1/24" in msgs(res), (CALLS, res["answer"]))
res = A.query("which command adds an ip address to a vlan?", "u2")
check("'which command ...' answered from the reference, ZERO model calls", CALLS == [] and "config interface ip add" in res["answer"], res["answer"])
res = A.query("list the available commands", "u3")
check("reference question with nothing to look up asks what to look up", CALLS == [] and res["answer"].startswith("What should I look up?"))
check("ordinary requests are not mistaken for reference questions",
      resolve.meta_lookup("show vlan brief") is None and resolve.meta_lookup("configure vlan 200") is None
      and resolve.meta_lookup("show running config") is None)

plan_stub({"why": "", "commands": [], "search": "teleport", "note": "No command for teleporting packets."},
          {"why": "", "commands": [], "search": "", "note": "Still nothing."})
res = A.query("teleport packets from Ethernet4 to Ethernet8", "u4")
check("when nothing fits even after the second look, the closest real commands are shown", len(CALLS) <= 2
      and "Closest real SONiC commands" in res["answer"] and "- config " in res["answer"], res["answer"])
# "is Ethernet4 up?": no model to pick the command, and the answer is read from the table (even with -e)
STATUS_OUT = """  Interface            Lanes    Speed    MTU    FEC    Alias             Vlan    Oper    Admin             Type    Asym PFC
-----------  ---------------  -------  -----  -----  -------  ---------------  ------  -------  ---------------  ----------
  Ethernet4          5,6,7,8     100G   9100    N/A   etp2         routed    {oper}     {admin}  QSFP28 or later         off"""
check("status question -> show interfaces status <port>, no model",
      resolve.intent("is Ethernet4 up?") == ("show interfaces status Ethernet4", "matched the words interface, up for Ethernet4"))
check("status question variants", resolve.intent("status of Ethernet 8")[0] == "show interfaces status Ethernet8"
      and resolve.intent("is Ethernet4 down")[0] == "show interfaces status Ethernet4"
      and resolve.intent("show lldp neighbors on Ethernet4")[0] == "show lldp neighbors Ethernet4")
check("changes and other values still go to the model", resolve.intent("shutdown Ethernet4") is None
      and resolve.intent("is vlan 20 up") is None and resolve.intent("show bgp summary for Ethernet4") is None)
_rb = tools.run_batch
for oper, admin, want in [("up", "up", "Ethernet4 is up (oper up, admin up, speed 100G, mtu 9100)."),
                          ("down", "down", "Ethernet4 is down: it is administratively shut down"),
                          ("down", "up", "Ethernet4 is down (oper down) although it is enabled")]:
    tools.run_batch = lambda argvs, mut, max_out=0, o=STATUS_OUT.format(oper=oper, admin=admin): [
        {"exit": 0, "output": o, "ran": True} for _ in argvs]
    CALLS.clear(); plan_stub()
    res = A.query("is Ethernet4 up?", "st1", explain=True)
    check(f"your case (-e 'is Ethernet4 up?', oper {oper}/admin {admin}): answered with ZERO model calls",
          CALLS == [] and res["how"] == "intent" and res["answer"].startswith(want)
          and "Answered from the command output directly" in msgs(res), (CALLS, res.get("answer")))
tools.run_batch = _rb
plan_stub({"answer": "Unclear from the output."})
tools.run_batch = lambda argvs, mut, max_out=0: [{"exit": 0, "output": "some unparsable text", "ran": True} for _ in argvs]
CALLS.clear()
res = A.query("is Ethernet4 up?", "st2", explain=True)
check("unreadable output: -e falls back to the model (one call)", len(CALLS) == 1 and res["answer"] == "Unclear from the output.")
tools.run_batch = _rb

clidoc._static.clear(); clidoc._static.update(_saved_static); clidoc._meta.clear(); clidoc._meta.update(_saved_meta)


# ------------------------------------------------------------------ 3h. the switch's own installed CLI source
import shutil as _shutil
SW = tempfile.mkdtemp(prefix="dial0-switch-")
_shutil.copytree(os.path.join(FIX, "config"), os.path.join(SW, "config"))
_shutil.copytree(os.path.join(FIX, "show"), os.path.join(SW, "show"))
open(os.path.join(SW, "config", "anycast.py"), "w").write('''import click


@click.group()
def anycast():
    """Anycast settings"""


@anycast.command('add')
@click.argument('ip_addr', metavar='<ip_addr>')
def anycast_add(ip_addr):
    """Add an address.

    Configures the static anycast gateway (SAG) address used by the VLAN interfaces.
    """
''')
with open(os.path.join(SW, "config", "main.py"), "a") as f:
    f.write("\nfrom . import anycast\nconfig.add_command(anycast.anycast)\n")
_saved_static, _saved_meta = dict(clidoc._static), dict(clidoc._meta)
clidoc.MIN_SWITCH_COMMANDS = 3
clidoc.SWITCH_INDEX = os.path.join(TMP, "switch_commands.json")
check("switch source: indexed from the installed config/ and show/", clidoc.load_switch_index(SW)
      and clidoc._meta.get("origin") == "switch" and "config anycast add" in clidoc._static)
check("switch source: status says the reference is this switch's installed CLI",
      clidoc.status()["source"].startswith("this switch's installed CLI"))
check("switch source: ref src searches the switch's code", "anycast.py" in clidoc.ref("src static anycast gateway").replace(SW, ""))
_build = __import__("dial0.extract_cli", fromlist=["build"]).build
__import__("dial0.extract_cli", fromlist=["build"]).build = lambda root: (_ for _ in ()).throw(RuntimeError("must use cache"))
check("switch source: cached until that code changes", clidoc.load_switch_index(SW) and "config anycast add" in clidoc._static)
__import__("dial0.extract_cli", fromlist=["build"]).build = _build
with open(os.path.join(SW, "config", "anycast.py"), "a") as f:
    f.write("\n\n@anycast.command('del')\ndef anycast_del():\n    \"\"\"Remove the anycast address\"\"\"\n")
os.utime(os.path.join(SW, "config", "anycast.py"), (time.time() + 5, time.time() + 5))
check("switch source: re-indexed when that code changes", clidoc.load_switch_index(SW) and "config anycast del" in clidoc._static)
check("source search: finds a command from words only in its docstring body (not in the index)",
      clidoc.search_paths("sag", 8) == [] and "config anycast add" in clidoc.source_search("sag gateway"))
check("switch source missing -> keeps the current index", clidoc.load_switch_index(os.path.join(SW, "nope")) is False
      and "config anycast add" in clidoc._static)

CALLS.clear(); EXEC.clear()
plan_stub({"why": "", "commands": [], "search": "sag gateway", "note": "No command for this in the reference."},
          {"why": "", "commands": ["config anycast add 10.0.0.1"], "search": "", "note": "Sets the anycast gateway."})
check("the first search alone can't find it (only its docstring says SAG)", "config anycast add" not in clidoc.search_paths("set the SAG to 10.0.0.1", 8))
res = A.query("set the SAG to 10.0.0.1", "sw1")
m = msgs(res)
check("second look: nothing fitted -> switch source searched natively -> one more model call finds it",
      len(CALLS) == 2 and "config anycast add" in CALLS[1]["messages"][1]["content"]
      and "installed SONiC CLI source" in m and res["status"] == "needs_confirmation"
      and res["commands"] == ["config anycast add 10.0.0.1"], (len(CALLS), m, res))
A.confirm("sw1", res["id"], False)
CALLS.clear()
plan_stub({"why": "", "commands": [], "search": "", "note": "Which VLAN should get the address?"})
res = A.query("set the SAG address on vlan 20", "sw2")
check("second look not used when the model is asking you a question", len(CALLS) == 1)
agent.SEARCH_RETRY = False
CALLS.clear()
plan_stub({"why": "", "commands": [], "search": "sag gateway", "note": "No command for this."})
res = A.query("set the SAG to 10.0.0.2", "sw3")
check("SEARCH_RETRY=off: no second model call", len(CALLS) == 1)
agent.SEARCH_RETRY = True
clidoc._static.clear(); clidoc._static.update(_saved_static); clidoc._meta.clear(); clidoc._meta.update(_saved_meta)


# ------------------------------------------------------------------ 3i. argument values (your failed command)
REQ = "configure ip address 1.1.1.1/24 to interface vlan20"
c, why, err = resolve.fix_values("config interface ip add vlan20 1.1.1.1 255.255.255.0", REQ)
check("values: your command fixed to SONiC form", c == "config interface ip add Vlan20 1.1.1.1/24" and err is None, (c, why, err))
check("values: reasons given", any("vlan20 -> Vlan20" in w for w in why) and any("-> 1.1.1.1/24" in w for w in why), why)
check("values: bare IP gets the prefix from the request", resolve.fix_values("config interface ip add Vlan20 1.1.1.1", REQ)[0]
      == "config interface ip add Vlan20 1.1.1.1/24")
check("values: an address not in the request is rejected", "not a value from your request" in
      (resolve.fix_values("config interface ip add Vlan20 2.2.2.2/24", REQ)[2] or ""))
check("values: an invented prefix length is questioned", "prefix length /24 isn't in your request" in
      (resolve.fix_values("config interface ip add Vlan20 1.1.1.1/24", "put 1.1.1.1 on vlan20")[2] or ""))
check("values: typed commands and non-config/show commands are left alone",
      resolve.fix_values("vtysh -c 'show ip route 9.9.9.9'", "")[2] is None)

check("args: right count and kinds pass", clidoc.check_args(tools.parse_click("config interface ip add Vlan20 1.1.1.1/24")) is None)
check("args: missing value", "needs 2 value(s)" in (clidoc.check_args(tools.parse_click("config interface ip add 1.1.1.1/24")) or ""))
check("args: interface slot holding an IP", "is not an interface name" in
      (clidoc.check_args(tools.parse_click("config interface ip add 1.1.1.1/24 Vlan20")) or ""))
check("args: IP slot holding garbage", "is not an IP address" in
      (clidoc.check_args(tools.parse_click("config interface ip add Vlan20 one.one")) or ""))
check("args: VLAN id out of range", "is not a VLAN id" in (clidoc.check_args(tools.parse_click("config vlan add 5000")) or ""))
check("args: too many values", "takes at most" in
      (clidoc.check_args(tools.parse_click("config vlan add 10 20")) or ""))
check("args: options understood (flag does not count as a value)",
      clidoc.check_args(tools.parse_click("config vlan member add -u 10 Ethernet1")) is None)

CALLS.clear(); EXEC.clear()
plan_stub({"why": "", "commands": ["config interface ip add vlan20 1.1.1.1 255.255.255.0", "show vlan brief"], "search": "", "note": "Sets it."})
res = A.query(REQ, "v1")
check("your case end to end: the proposed command is corrected before you're asked",
      res["status"] == "needs_confirmation" and res["commands"][0] == "config interface ip add Vlan20 1.1.1.1/24"
      and "Fixed without the model" in msgs(res), (res.get("commands"), msgs(res)))
A.confirm("v1", res["id"], False)
_rb = tools.run_batch
tools.run_batch = lambda argvs, mut, max_out=0: [
    {"exit": 2, "output": 'Usage: config interface ip add [OPTIONS] <interface_name> <ip_addr> [gw]\nError: Invalid value for "<ip_addr>"', "ran": True},
    {"exit": None, "output": "", "ran": False}][:len(argvs)]
res = A.query("config interface ip add Vlan20 1.1.1.1/24", "v2")
agent.FIX_ROUNDS = 0  # this test is about showing the error, not fixing it
res = A.confirm("v2", res["id"], True)
agent.FIX_ROUNDS = 3
check("a failed command's SONiC error is in the steps", 'failed (exit 2): Error: Invalid value for "<ip_addr>"' in msgs(res), msgs(res))
tools.run_batch = _rb

# SONiC's CLI gets the switch's clean environment, not this container's
r = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import os; os.environ['DIAL0_HOST_EXEC']='nsenter';"
                    "from dial0 import tools; print(' '.join(tools.PREFIX))", ROOT], capture_output=True, text=True,
                   env=dict(os.environ, DIAL0_HOST_EXEC="nsenter"))
check("host commands: nsenter + env -i with a system PATH", r.stdout.startswith("nsenter -t 1") and " env -i PATH=/usr/local/sbin" in r.stdout, r.stdout + r.stderr)
_pre = tools.PREFIX
tools.PREFIX = list(tools.HOST_ENV)
code, body = tools.split_result(tools._run(["env"], max_out=100000))
tools.PREFIX = _pre
check("host commands: no PYTHONPATH / LD_LIBRARY_PATH / container settings leak into SONiC's CLI",
      code == 0 and not any(l.split("=")[0] in ("PYTHONPATH", "LD_LIBRARY_PATH", "DIAL0_STATE_DIR", "VIRTUAL_ENV")
                            for l in body.splitlines()), body)

# doctor, against fake show/config commands
FAKEBIN = tempfile.mkdtemp(prefix="dial0-bin-")
for name, script in (("show", "#!/bin/sh\necho 'SONiC Software Version: SONiC.202405'\n"),
                     ("config", "#!/bin/sh\n[ \"$1\" = vlan ] && { echo 'Error: No such command \"vlan\".' >&2; exit 2; }\necho 'Usage: config'\n"),
                     ("id", "#!/bin/sh\necho 0\n")):
    open(os.path.join(FAKEBIN, name), "w").write(script); os.chmod(os.path.join(FAKEBIN, name), 0o755)
r = subprocess.run([sys.executable, "-m", "dial0.doctor"], capture_output=True, text=True, cwd=ROOT,
                   env=dict(os.environ, PATH=FAKEBIN + ":" + os.environ["PATH"], PYTHONPATH=ROOT, DIAL0_API_PORT="1"))
check("doctor: passing checks shown, failing one shows the switch's own error", r.returncode == 1
      and "[ok]  show commands run" in r.stdout and "[ok]  config runs" in r.stdout
      and "[FAIL]  config subcommands run" in r.stdout and 'No such command "vlan"' in r.stdout, r.stdout)


# ------------------------------------------------------------------ 3j. the official Command Reference document
DOC = os.path.join(FIX, "..", "Command-Reference-sample.md")
secs = clidoc.parse_cmdref(open(DOC).read())
heads = [x["head"] for x in secs]
check("cmdref: every **command** section parsed", heads == [
    "config interface ip add <interface_name> <ip_addr> [default_gw]", "config interface ip remove <interface_name> <ip_addr>",
    "config interface shutdown <interface_name>", "config interface startup <interface_name>", "show vlan brief",
    "config vlan add <vid>"], heads)
ipadd = secs[0]
check("cmdref: description, usage and examples read", ipadd["desc"].startswith("This command is used for adding the IP address")
      and ipadd["usage"] == ["config interface ip add <interface_name> <ip_addr> [default_gw]"]
      and ipadd["examples"] == ["config interface ip add Ethernet63 10.11.12.13/24", "config interface ip add Vlan100 10.1.1.1/24"], ipadd)
check("cmdref: output lines in examples are not taken as commands", secs[4]["examples"] == ["show vlan brief"], secs[4])
clidoc.CMDREF_CACHE = os.path.join(TMP, "cmdref.json")
check("cmdref: loaded", clidoc.load_cmdref([DOC]))
check("cmdref: attached to commands this switch has", set(clidoc._cmdref) == {
      "config interface ip add", "config interface shutdown", "show vlan brief", "config vlan add"}, set(clidoc._cmdref))
check("cmdref: documented commands this switch doesn't have are ignored (other releases)",
      "config interface ip remove" not in clidoc._cmdref and "config interface startup" not in clidoc._cmdref
      and clidoc._cmdref_meta["ignored"] == 2, clidoc._cmdref_meta)
check("cmdref: an example that doesn't validate here is dropped", clidoc._cmdref["config vlan add"]["examples"] == ["config vlan add 100"]
      and clidoc._cmdref_meta["examples_dropped"] >= 1, clidoc._cmdref["config vlan add"])
check("cmdref: the model's reference line carries a real example",
      clidoc._line("config interface ip add").endswith("e.g. config interface ip add Ethernet63 10.11.12.13/24"), clidoc._line("config interface ip add"))
check("cmdref: ref show includes description and examples", "example: config interface ip add Vlan100 10.1.1.1/24"
      in clidoc.ref("show config interface ip add") and "Command Reference: This command is used" in clidoc.ref("show config interface ip add"))
check("cmdref: the document's words help the search", "config interface ip add" in
      clidoc.search_paths("configure an address on a portchannel interface", 4), clidoc.search_paths("configure an address on a portchannel interface", 4))
check("cmdref: status reports it", clidoc.status()["command_reference_doc"].startswith("4 commands described"), clidoc.status())
_saved_doc = dict(clidoc._cmdref); clidoc._cmdref.clear()
check("cmdref: cached", clidoc.load_cmdref([DOC]) and clidoc._cmdref == _saved_doc)
CALLS.clear()
plan_stub({"why": "", "commands": ["config interface ip add Vlan30 10.3.3.1/24"], "search": "", "note": ""})
res = A.query("put 10.3.3.1/24 on vlan 30", "doc1")
check("cmdref: the model sees the documented example", "e.g. config interface ip add Ethernet63 10.11.12.13/24"
      in CALLS[0]["messages"][1]["content"], CALLS[0]["messages"][1]["content"])
A.confirm("doc1", res["id"], False)
check("cmdref: missing file -> not loaded, nothing breaks", clidoc.load_cmdref([os.path.join(TMP, "nope.md")]) is False)


# ------------------------------------------------------------------ 3k. YOUR command reference: only its commands are used
REFFILE = os.path.join(ROOT, "reference", "sonic-command-reference.md")
check("reference file ships with Dial 0 and is recognised as a command list", os.path.isfile(REFFILE)
      and clidoc.is_curated_format(open(REFFILE).read()))
check("cmd ref: loaded", clidoc.load_cmdref([REFFILE]) and clidoc.curated())
m = clidoc._curated_meta
N_REF = len(clidoc._curated)
check("cmd ref: every command of the file, split into show and config", (m["commands"], m["show"], m["config"]) == (N_REF, 35, 29)
      and "config hostname" in clidoc._curated and clidoc._curated["config hostname"]["area"] == "System", m)
check("cmd ref: 'eth0' is a value (management interface example belongs to config interface ip add)",
      "config interface ip add eth0 20.11.12.13/24 20.11.12.254" in clidoc._curated["config interface ip add"]["examples"])
_st = dict(clidoc._static); clidoc._static.clear(); clidoc._load_curated(REFFILE, open(REFFILE).read())
check("cmd ref: the same commands even without the switch's CLI index", len(clidoc._curated) == N_REF
      and "show interfaces counters detailed" in clidoc._curated and "show interfaces counters rif" in clidoc._curated, sorted(clidoc._curated))
clidoc._static.update(_st); clidoc.load_cmdref([REFFILE])
check("cmd ref: 'ipv6' and 'use-link-local-only' are part of the command, not values",
      "config interface ipv6 enable use-link-local-only" in clidoc._curated and "config interface" not in clidoc._curated)
check("cmd ref: status says only these are used", "only these are used" in clidoc.status()["command_reference_doc"])
cc = lambda c: clidoc.check_command(tools.parse_click(c))
check("cmd ref: a listed command the switch code defines is checked against it", cc("config vlan add 100") is None
      and "is not a VLAN id" in (cc("config vlan add 5000") or ""))
check("cmd ref: a listed command the index can't see is trusted as listed", cc("config portchannel add PortChannel0011") is None
      and cc("show interface counters") is None)
check("cmd ref: a command NOT in your file is rejected, even if the switch has it",
      "not in your command reference" in (cc("config save -y") or "") and "not in your command reference" in (cc("show version") or ""))
check("cmd ref: search only offers your commands", clidoc.search_paths("save the config", 8) == []
      and all(p in clidoc._curated for p in clidoc.search_paths("add ip address to vlan 20", 8)))
check("cmd ref: the model's list carries your examples with what they mean", clidoc._line("config vlan member add").endswith(
      "e.g. config vlan member add 100 Ethernet0 (tagged member: the default, no flag); "
      "config vlan member add -u 100 Ethernet4 (untagged member: -u)"), clidoc._line("config vlan member add"))
check("cmd ref: a '# note' is not part of the command", "config vlan member add 100 Ethernet0" in clidoc._curated["config vlan member add"]["examples"]
      and not any("#" in e for d in clidoc._curated.values() for e in d["examples"]))
check("cmd ref: options explained from your file's description", "(one at a time, or several with -m as a range or list)"
      in clidoc._line("config vlan add") and "(minimum links needed to bring the LAG up)" in clidoc._line("config portchannel add"),
      (clidoc._line("config vlan add"), clidoc._line("config portchannel add")))
check("cmd ref: without '# note's, the default form is still derived from the description",
      clidoc._flag_note("-u", "add or remove member ports (tagged by default, untagged with -u); set proxy ARP")
      == "tagged by default, untagged with -u")
_d = {"examples": ["config vlan member add 100 Ethernet0", "config vlan member add -u 100 Ethernet4"], "notes": {},
      "desc": "add member ports (tagged by default, untagged with -u)"}
clidoc._auto_notes(_d)
check("cmd ref: the flag-less example is marked as the default automatically",
      _d["notes"]["config vlan member add 100 Ethernet0"] == "tagged: the default, no flag", _d["notes"])
check("model instructions: no option = the default behaviour, never 'no command fits' for it",
      "a tagged VLAN member is added without -u" in agent.PLAN_SYSTEM and "Never answer that no command fits" in agent.PLAN_SYSTEM)
for req, cmd, want in [("Add it as TAGGED not UNTAGGED", "config vlan member add -u 40 PortChannel100", "config vlan member add 40 PortChannel100"),
                       ("add Ethernet4 untagged to vlan 40", "config vlan member add 40 Ethernet4", "config vlan member add -u 40 Ethernet4"),
                       ("add Ethernet4 as tagged to vlan 40", "config vlan member add --untagged 40 Ethernet4", "config vlan member add 40 Ethernet4"),
                       ("add Ethernet4 to vlan 40", "config vlan member add -u 40 Ethernet4", "config vlan member add -u 40 Ethernet4"),
                       ("tagged on 40 and untagged on 50", "config vlan member add 40 Ethernet4", "config vlan member add 40 Ethernet4")]:
    check(f"tagged/untagged by code: {req!r}", resolve.fix_modes(cmd, req)[0] == want, resolve.fix_modes(cmd, req))
check("cmd ref: ref show / ref show for an unlisted command", "example: config portchannel add PortChannel0012 --min-links 2"
      in clidoc.ref("show config portchannel add") and clidoc.ref("show config save").startswith("NOT IN YOUR COMMAND REFERENCE"))

CALLS.clear(); EXEC.clear()
plan_stub({"why": "", "commands": ["config interface ip add vlan20 1.1.1.1 255.255.255.0"], "search": "", "note": ""})
res = A.query("configure ip address 1.1.1.1/24 to interface vlan20", "cr1")
check("cmd ref: your earlier case -> config interface ip add Vlan20 1.1.1.1/24, with your examples shown to the model",
      res.get("commands") == ["config interface ip add Vlan20 1.1.1.1/24"]
      and "e.g. config interface ip add Ethernet63 10.11.12.13/24; config interface ip add Vlan100 10.11.12.13/24"
      in CALLS[0]["messages"][1]["content"], (res.get("commands"), CALLS[0]["messages"][1]["content"][:600]))
A.confirm("cr1", res["id"], False)
CALLS.clear()
plan_stub({"why": "", "commands": ["config ntp add 10.0.0.6"], "search": "", "note": ""})
res = A.query("add ntp server 10.0.0.6 for the vlan 20 interface", "cr2")
check("cmd ref: a model proposal outside your file is not run", res["status"] == "done" and not res.get("results")
      and "not in your command reference" in res["answer"], res.get("answer"))
CALLS.clear()
res = A.query("set the timezone to UTC", "cr3")
check("cmd ref: nothing in your file fits -> 'I don't know' with ZERO model calls", CALLS == [] and res["answer"].startswith("I don't know"))
CALLS.clear(); EXEC.clear()
res = A.query("show interface counters", "cr4")
CALLS.clear()
plan_stub({"why": "", "commands": ["config hostname leaf9"], "search": "", "note": ""},
          {"why": "", "commands": ["config hostname leaf_1"], "search": "", "note": ""},
          {"why": "", "commands": ["config hostname leaf1"], "search": "", "note": "Sets the hostname."})
r1 = A.query("change the hostname to leaf1", "hn1")
r2 = A.query("change the hostname to leaf1", "hn2")
r3 = A.query("change the hostname to leaf1", "hn3")
check("hostname: an invented name is rejected (must come from your request)", not r1.get("commands")
      and "the hostname 'leaf9' is not in your request" in r1.get("answer", ""), r1.get("answer"))
check("hostname: an invalid name is rejected", not r2.get("commands") and "'leaf_1' is not a valid hostname" in r2.get("answer", ""), r2.get("answer"))
check("hostname: the right one is offered for y/N", r3.get("commands") == ["config hostname leaf1"], r3)
A.confirm("hn3", r3["id"], False)
check("hostname: your request's words find it", clidoc.search_paths("change the hostname to leaf1", 5)[0] == "config hostname")
CALLS.clear(); EXEC.clear()
res = A.query("show interface counters", "cr4")
check("cmd ref: your listed alias form runs directly (no model)", CALLS == [] and EXEC == [["show", "interface", "counters"]], (CALLS, EXEC))
EXEC.clear(); CALLS.clear()
res = A.query("show version", "cr5")
check("cmd ref: a command not in your file doesn't run, even typed (no model call either)",
      EXEC == [] and CALLS == [] and "is not in your command reference" in res["answer"]
      and "dial0 ctl cmdref" in res["answer"], (EXEC, res.get("answer")))
lst = clidoc.ref("list")
check("cmd ref: 'ref list' shows every command grouped like your file, without a count", lst.startswith("Commands from")
      and not re.match(r"\d", lst)
      and "Set 1: show commands" in lst and "Set 2: config commands" in lst and "  IP / IPv6:" in lst
      and "    config interface ipv6 enable use-link-local-only" in lst and "show version" not in lst, lst[:300])
_st2 = dict(clidoc._static)
clidoc._static.clear(); clidoc._load_curated(REFFILE, open(REFFILE).read()); _no_index = list(clidoc._curated)
clidoc._static.update(_st2); clidoc._load_curated(REFFILE, open(REFFILE).read())
check("cmd ref: the list comes from the file alone (identical with or without the switch's CLI code)",
      _no_index == list(clidoc._curated) and "show ip route vrf" in clidoc._curated)
check("cmd ref: 'is Ethernet4 up?' -> show interfaces status Ethernet4 (your examples show it takes a port)",
      resolve.intent("is Ethernet4 up?")[0] == "show interfaces status Ethernet4")
check("cmd ref: no-model intents only use your commands", resolve.intent("show the lldp table") is None
      and resolve.intent("show lldp neighbors")[0] == "show lldp neighbors")
check("cmd ref: Linux 'ip addr add' still becomes the listed config command",
      resolve.fix("ip addr add 10.0.0.1/31 dev portchannel11") is not None)
clidoc._curated.clear(); clidoc._curated_meta.clear()


# ------------------------------------------------------------------ 3l. follow-ups use the context (your case)
clidoc.load_cmdref([REFFILE])
agent.LEARN = False
CALLS.clear(); EXEC.clear()
plan_stub({"why": "", "commands": ["config vlan member add -u 40 PortChannel100", "show vlan brief"], "search": "", "note": ""})
res = A.query("add PortChannel100 to vlan 40 untagged", "tg", context=True)
_rb = tools.run_batch
tools.run_batch = lambda argvs, mut, max_out=0: [
    {"exit": 2, "output": 'Usage: config vlan member add [OPTIONS] <vid> port\nTry "config vlan member add -h" for help.\n\n'
                          "Error: PortChannel100 is already untagged member of Vlan100", "ran": True},
    {"exit": None, "output": "", "ran": False}][:len(argvs)]
agent.FIX_ROUNDS = 0  # here the follow-up request does the fixing
res = A.confirm("tg", res["id"], True)
agent.FIX_ROUNDS = 3
tools.run_batch = _rb
check("follow-up: the failure is recorded with SONiC's error for the context",
      "exit 2: Error: PortChannel100 is already untagged member of Vlan100" in sessions.get("tg").messages[-1]["content"],
      sessions.get("tg").messages[-1]["content"])
CALLS.clear()
plan_stub({"why": "", "commands": ["config vlan member add -u 40 PortChannel100", "show vlan brief"],
           "search": "", "note": "Adds PortChannel100 to VLAN 40 as tagged."})  # the model wrongly keeps -u
res = A.query("Add it as TAGGED not UNTAGGED", "tg", context=True)
check("your case: -u removed by code because you asked for tagged", "-u removed: you asked for tagged" in msgs(res), msgs(res))
prompt = CALLS[0]["messages"][1]["content"] if CALLS else ""
ref_lines = prompt.split("use any command from it that fits):\n")[1].splitlines() if prompt else []
check("your case: the follow-up reaches the model instead of 'I don't know'", len(CALLS) == 1, msgs(res))
check("your case: the model is shown the command just run (config vlan member add) first",
      ref_lines and ref_lines[0].startswith("- config vlan member add") and "(with context)" in msgs(res), ref_lines[:3])
check("your case: the model sees why it failed", "already untagged member of Vlan100" in prompt, prompt[-600:])
check("your case: the corrected command is offered for one y/N", res.get("commands") == [
      "config vlan member add 40 PortChannel100", "show vlan brief"], res)
A.confirm("tg", res["id"], False)
CALLS.clear()
res = A.query("Add it as TAGGED not UNTAGGED", "tg2", context=False)
check("fresh one-shot follow-up: says to use the prompt or --context (no model call)", CALLS == []
      and "--context" in res["answer"] and "dial0> prompt" in res["answer"], res["answer"])
agent.LEARN = True
clidoc._curated.clear(); clidoc._curated_meta.clear()


# ------------------------------------------------------------------ 3m. the model sees the COMPLETE command list
clidoc.load_cmdref([REFFILE])
A._plan_sys = None
full = clidoc.full_reference()
check("full list: every command of the file, both sets", all(("- " + p) in full for p in clidoc._curated)
      and "SET 1: SHOW COMMANDS" in full and "SET 2: CONFIG COMMANDS" in full)
check("full list: each area's rules from your file are included",
      "A port or PortChannel can be an untagged member of only one VLAN" in full
      and "A PortChannel can only be deleted after all its members are removed" in full
      and "vlan static-anycast-gateway enable <vlan_id>" in full, full[:200])
check("full list: examples carry their notes", "config vlan member add -u 100 Ethernet4 (untagged member: -u)" in full)
CALLS.clear(); EXEC.clear(); agent.LEARN = False
plan_stub({"why": "", "commands": ["config vlan add 70"], "search": "", "note": ""},
          {"why": "", "commands": ["config vlan add 71"], "search": "", "note": ""})
r1 = A.query("create vlan 70", "fl1"); A.confirm("fl1", r1["id"], False)
r2 = A.query("create vlan 71", "fl2"); A.confirm("fl2", r2["id"], False)
sys1, sys2 = CALLS[0]["messages"][0]["content"], CALLS[1]["messages"][0]["content"]
check("full list: in the model's instructions, identical for every request (cached by llama.cpp)",
      sys1 == sys2 and "COMMAND REFERENCE: the ONLY commands you may use" in sys1 and full in sys1)
check("full list: each request adds a short ranked list pointing into it", "Most relevant for this request" in CALLS[0]["messages"][1]["content"]
      and CALLS[0]["messages"][1]["content"].count("\n- ") <= 6)
total = sum(len(m["content"]) for m in CALLS[0]["messages"])
ctx = int(re.search(r"LLAMA_CTX=(\d+)", open(os.path.join(ROOT, "dial0.conf")).read()).group(1))
check(f"full list: the whole prompt (~{total // 4} tokens) fits the model's window ({ctx}) with room to answer",
      total // 3 + 400 < ctx and "LLAMA_CTX=8192" in open(os.path.join(ROOT, "Dockerfile")).read(), (total, ctx))
CUR_RANK = [("make Ethernet4 an access port", ["config switchport mode"], 2),
            ("enable proxy arp on vlan 1000", ["config vlan proxy_arp"], 1),
            ("configure ip address 1.1.1.1/24 to interface vlan20", ["config interface ip add"], 2),
            ("add Ethernet4 to vlan 40 as tagged", ["config vlan member add"], 1),
            ("create portchannel 11 with Ethernet4", ["config portchannel add", "config portchannel member add"], 3),
            ("remove the ip from Ethernet63", ["config interface ip remove"], 1),
            ("bind Ethernet0 to vrf Vrf-red", ["config interface vrf bind"], 1),
            ("set up a static anycast gateway 1.1.1.1/24 on vlan 100", ["config interface ip anycast-address add"], 2),
            ("show packet drops", ["show interface pktdrops"], 2),
            ("set trunk mode on Ethernet4", ["config switchport mode"], 1),
            ("show bgp neighbors", ["show ip bgp neighbors"], 1),
            ("delete portchannel 11", ["config portchannel del"], 1),
            ("show mac table for vlan 1000", ["show mac"], 1),
            ("change mtu of Ethernet64 to 1500", ["config interface mtu"], 1)]
cur_miss = [(q, clidoc.search_paths(q, 5)[:3]) for q, want, k in CUR_RANK if not all(x in clidoc.search_paths(q, 5)[:k] for x in want)]
check(f"search in your file: right commands at the top for {len(CUR_RANK) - len(cur_miss)}/{len(CUR_RANK)} typical requests", not cur_miss, cur_miss)
check("search: a command found through its area's description ('access port' -> config switchport mode)",
      "config switchport mode" in clidoc.search_paths("make Ethernet4 an access port", 5), clidoc.search_paths("make Ethernet4 an access port", 5))
check("search: 'enable proxy arp on vlan 1000' -> config vlan proxy_arp", clidoc.search_paths("enable proxy arp on vlan 1000", 5)[0] == "config vlan proxy_arp")
CALLS.clear()
plan_stub({"why": "", "commands": [], "search": "anycast gateway", "note": "No command fits."})
res = A.query("set up an anycast gateway on vlan 100", "fl3")
check("full list: no 'second look' model call (the model already saw every command)", len(CALLS) == 1)
CALLS.clear(); plan_stub({"why": "", "commands": [], "search": "", "note": ""})
A.warm_up()
check("warm-up (auto): with a reference file, the full list is pre-loaded once at startup", len(CALLS) == 1
      and CALLS[0]["messages"][0]["content"] == sys1)
check("agent mode sees the full list too", full in A._system(sessions.get("fl4")))
agent.LEARN = True
clidoc._curated.clear(); clidoc._curated_meta.clear(); A._plan_sys = None
CALLS.clear(); A.warm_up()
check("warm-up (auto): no reference file -> no startup model call", CALLS == [])


# ------------------------------------------------------------------ 3n. recovering from SONiC errors (loop)
for cmd, err, want in [("config vlan add 100", "Error: Vlan100 already exists", True),
                       ("config vlan add 100", "Error: Vlan 100 already exists!", True),
                       ("config portchannel add PortChannel0011", "Error: PortChannel0011 already exists!", True),
                       ("config vlan member add 40 Ethernet4", "Error: Ethernet4 is already a member of Vlan40", True),
                       ("config interface ip add Vlan20 1.1.1.1/24", "Error: IP address 1.1.1.1/24 already exists on Vlan20", True),
                       ("config vlan member add -u 40 PortChannel100", "Error: PortChannel100 is already untagged member of Vlan100", False),
                       ("config vlan add 100", "Error: Invalid VLAN ID 100", False)]:
    check(f"already-done? {'yes' if want else 'no '}: {err}", agent.already_done(cmd, err) is want)

agent.LEARN = False
RUNS = []
def batch_script(*replies):
    it = iter(replies)
    def fake(argvs, mut, max_out=0):
        RUNS.append([" ".join(a) for a in argvs]); EXEC.extend(argvs)
        return next(it)(argvs)
    tools.run_batch = fake
ok_all = lambda argvs: [{"exit": 0, "output": "(no output)", "ran": True} for _ in argvs]
def fail_first(msg):
    return lambda argvs: [{"exit": 2, "output": "Usage: ...\n\nError: " + msg, "ran": True}] + \
                         [{"exit": None, "output": "", "ran": False} for _ in argvs[1:]]

CALLS.clear(); EXEC.clear(); RUNS.clear()
plan_stub({"why": "", "commands": ["config vlan add 100", "config vlan member add -u 100 Ethernet4", "show vlan brief"], "search": "", "note": ""})
batch_script(fail_first("Vlan100 already exists"), ok_all)
res = A.query("create vlan 100 with Ethernet4 untagged", "lp1")
res = A.confirm("lp1", res["id"], True)
check("loop: 'already exists' counts as done and the rest of the approved batch continues (no model call, no new y/N)",
      res["status"] == "done" and len(CALLS) == 1 and RUNS[-1] == ["config vlan member add -u 100 Ethernet4", "show vlan brief"]
      and "counts as done" in msgs(res), (res["status"], len(CALLS), RUNS, msgs(res)))
check("loop: recorded as 'already done' in the history", [e["status"] for e in sessions.get("lp1").log][:1] == ["already done"])

CALLS.clear(); EXEC.clear(); RUNS.clear()
plan_stub({"why": "", "commands": ["config vlan member add -u 40 PortChannel100", "show vlan brief"], "search": "", "note": ""},
          {"why": "untagged in Vlan100 already; tagged is allowed", "commands": ["config vlan member add 40 PortChannel100", "show vlan brief"],
           "search": "", "note": "Adds PortChannel100 to VLAN 40 as tagged, since it is untagged in VLAN 100."})
batch_script(fail_first("PortChannel100 is already untagged member of Vlan100"), ok_all)
res = A.query("add PortChannel100 to vlan 40", "lp2")
res = A.confirm("lp2", res["id"], True)
fixp = CALLS[1]["messages"][1]["content"] if len(CALLS) > 1 else ""
check("loop: a real error goes back to the model with what succeeded, the exact error and what didn't run",
      len(CALLS) == 2 and "FAILED, exit 2: Error: PortChannel100 is already untagged member of Vlan100" in fixp
      and "not run (an earlier command failed)" in fixp and "don't repeat what succeeded" in fixp, fixp[-700:])
check("loop: the fix is offered for its own y/N, with the failed round shown first",
      res["status"] == "needs_confirmation" and res["commands"] == ["config vlan member add 40 PortChannel100", "show vlan brief"]
      and res["results"][0]["exit"] == 2 and "Fix round 1 of 3" in msgs(res), res)
res = A.confirm("lp2", res["id"], True)
check("loop: the fix runs and completes the request", res["status"] == "done" and RUNS[-1] == ["config vlan member add 40 PortChannel100", "show vlan brief"]
      and all(r["exit"] == 0 for r in res["results"]), res)

CALLS.clear(); RUNS.clear()
plan_stub({"why": "", "commands": ["config vlan add 300"], "search": "", "note": ""},
          *[{"why": "", "commands": ["config vlan add 300"], "search": "", "note": ""} for _ in range(3)])
batch_script(*[fail_first("VLAN table is full") for _ in range(4)])
res = A.query("create vlan 300", "lp3")
rounds = 0
while res["status"] == "needs_confirmation":
    res = A.confirm("lp3", res["id"], True); rounds += 1
check("loop: keeps trying for FIX_ROUNDS (3) rounds, then gives up with the last SONiC error",
      rounds == 4 and len(CALLS) == 4 and res["answer"].startswith("Couldn't complete this after 3 attempt(s)")
      and "VLAN table is full" in res["answer"], (rounds, len(CALLS), res.get("answer")))

CALLS.clear(); RUNS.clear()
plan_stub({"why": "", "commands": ["config vlan del 300"], "search": "", "note": ""},
          {"why": "", "commands": [], "search": "", "note": "Vlan300 still has members (Ethernet4). Remove them first?"})
batch_script(fail_first("Vlan300 can not be removed. First remove vlan members"))
res = A.query("delete vlan 300", "lp4")
res = A.confirm("lp4", res["id"], True)
check("loop: when the fix needs your decision, the model asks instead of guessing", res["status"] == "done"
      and res["answer"].startswith("Vlan300 still has members") and len(RUNS) == 1, res.get("answer"))

agent.CONFIRM = "auto"
CALLS.clear(); RUNS.clear()
plan_stub({"why": "", "commands": ["config vlan member add -u 40 PortChannel100"], "search": "", "note": ""},
          {"why": "", "commands": ["config vlan member add 40 PortChannel100"], "search": "", "note": ""})
batch_script(fail_first("PortChannel100 is already untagged member of Vlan100"), ok_all)
res = A.query("put PortChannel100 in vlan 40", "lp5")
check("loop: with CONFIRM=auto the fix rounds run without asking", res["status"] == "done" and len(CALLS) == 2
      and RUNS[-1] == ["config vlan member add 40 PortChannel100"], (res, RUNS))
agent.CONFIRM = "ask"; agent.LEARN = True
tools.run_batch = fake_batch


# ------------------------------------------------------------------ 3o. health check workflow
HBIN = tempfile.mkdtemp(prefix="dial0-health-")
HSCEN = os.path.join(HBIN, "scenario")
def _hscript(name, body):
    open(os.path.join(HBIN, name), "w").write("#!/bin/bash\nS=$(cat " + HSCEN + ")\n" + body)
    os.chmod(os.path.join(HBIN, name), 0o755)
_hscript("show", r"""
case "$*" in
 "system-health summary")
   [ "$S" = nosh ] && { echo 'Error: No such command "system-health".'; exit 2; }
   if [ "$S" = bad ]; then printf 'System status summary\n\n  System status LED  red\n  Services:\n    Status: Not OK\n    Not Running: snmp\n  Hardware:\n    Status: OK\n'
   else printf 'System status summary\n\n  System status LED  green\n  Services:\n    Status: OK\n  Hardware:\n    Status: OK\n'; fi ;;
 "feature status")
   printf 'Feature         State           AutoRestart     SetOwner\n--------------  --------------  --------------  ----------\nbgp             enabled         enabled\ndatabase        always_enabled  always_enabled\ndhcp_relay      disabled        enabled\nsyncd           enabled         enabled\nswss            enabled         enabled\n' ;;
 "interfaces status")
   printf '  Interface            Lanes    Speed    MTU    FEC    Alias             Vlan    Oper    Admin             Type    Asym PFC\n-----------  ---------------  -------  -----  -----  -------  ---------------  ------  -------  ---------------  ----------\n  Ethernet0          1,2,3,4     100G   9100    N/A   etp1         routed      up       up  QSFP28 or later         off\n'
   [ "$S" = bad ] && printf '  Ethernet8       9,10,11,12     100G   9100    N/A   etp3         routed    down       up  QSFP28 or later         off\n  Ethernet12     13,14,15,16     100G   9100    N/A   etp4         routed    down     down  QSFP28 or later         off\n' ;;
 "ip bgp summary")
   printf 'IPv4 Unicast Summary:\nNeighbhor      V     AS    MsgRcvd    MsgSent    TblVer    InQ    OutQ  Up/Down    State/PfxRcd    NeighborName\n-----------  ---  -----  ---------  ---------  --------  -----  ------  ---------  --------------  --------------\n10.0.0.57      4  64600       3995       4001         0      0       0  00:39:32   6402            ARISTA01T1\n'
   [ "$S" = bad ] && printf '10.0.0.59      4  64600       3995       3998         0      0       0  never      Active          ARISTA02T1\n' ;;
 "version") printf 'SONiC Software Version: SONiC.202505.0-test\nUptime: 3 days, 2:01\n' ;;
 "system status")
   [ "$S" = nosh ] && { echo 'Error: No such command "status".'; exit 2; }
   if [ "$S" = bad ]; then printf 'System is not ready - one or more services are not up\n\nService-Name        Service-Status    App-Ready-Status    Down-Reason\n------------------  ----------------  ------------------  -------------\nbgp                 OK                OK                  -\nsyncd               Down              Down                Inactive\n'
   else printf 'System is ready\n'; fi ;;
 "platform ssdhealth")
   printf 'Device Model : SATA SSD 64GB\nHealth       : 98.0%%\nTemperature  : 32C\nUncorrectable Error Count : 0\n'
   [ "$S" = bad ] && printf 'Error : SMART read failed on /dev/sda\n' ;;
 "platform fan")
   printf '  Drawer    LED    FAN    Speed    Direction    Presence    Status          Timestamp\n---------  -----  -----  -------  -----------  ----------  --------  -----------------\nFanTray1   green  Fan1     40%%    intake       Present     OK        20260930 05:00:00\n'
   [ "$S" = bad ] && printf 'FanTray2   red    Fan2     0%%     intake       Present     Not OK    20260930 05:00:00\n' ;;
 "interfaces counters")
   printf '      IFACE    STATE    RX_OK    RX_BPS    RX_UTIL    RX_ERR    RX_DRP    RX_OVR    TX_OK    TX_BPS    TX_UTIL    TX_ERR    TX_DRP    TX_OVR\n-----------  -------  -------  --------  ---------  --------  --------  --------  -------  --------  ---------  --------  --------  --------\n'
   if [ "$S" = bad ]; then printf '  Ethernet0        U    1,234  0.00 B/s      0.00%%         0         0         0    5,678  0.00 B/s      0.00%%         0     1,024         0\n  Ethernet8        U      100  0.00 B/s      0.00%%        12         3         0       10  0.00 B/s      0.00%%         0         0         0\n'
   else printf '  Ethernet0        U    1,234  0.00 B/s      0.00%%         0         0         0    5,678  0.00 B/s      0.00%%         0         0         0\n'; fi ;;
 "interfaces transceiver summary")
   printf 'Interface    Presence     Vendor    Status\n-----------  -----------  --------  ----------\nEthernet0    Present      ACME      Ready\nEthernet12   Not present  N/A       N/A\n'
   [ "$S" = bad ] && printf 'Ethernet8    Present      ACME      Not Ready\n' ;;
esac
exit 0  # real show commands succeed; without this the last '[ ... ] &&' sets exit 1""")
_hscript("docker", r"""printf 'swss|Up 3 days\nbgp|Up 3 days\ndatabase|Up 3 days\ndial0|Up 2 hours\ndhcp_relay|Exited (0) 3 days ago\n'
[ "$S" = bad ] && printf 'syncd|Exited (1) 2 hours ago\n' || printf 'syncd|Up 3 days\n'""")
_hscript("df", r"""printf 'Filesystem     1024-blocks     Used Available Capacity Mounted on\nroot-overlay      16000000  6400000   9600000      %s /\n' $([ "$S" = bad ] && echo 91% || echo 40%)""")
_hscript("free", r"""printf '               total        used        free      shared  buff/cache   available\nMem:            7949        3000        1000         100        3949        4500\n'""")
_hscript("find", r"""[ "$S" = bad ] && echo /var/core/orchagent.1727670000.123.core.gz; exit 0""")
SYSLOG_F = os.path.join(HBIN, "syslog")
health.SYSLOG = SYSLOG_F
def _scenario(name, log):
    open(HSCEN, "w").write(name)
    open(SYSLOG_F, "w").write(log)
_old_path, _old_prefix = os.environ["PATH"], tools.PREFIX
os.environ["PATH"] = HBIN + ":" + _old_path; tools.PREFIX = []

_scenario("good", "2026 Sep 30 05:00:01.1 sonic INFO swss#orchagent: all good\n2026 Sep 30 05:00:02.1 sonic WARNING lldp#lldpmgrd: neighbor flap\n")
report, overall, findings = health.run()
check("health: a healthy switch is reported HEALTHY", overall == "HEALTHY" and report.startswith("Switch health: HEALTHY   [SONiC.202505.0-test, up 3 days, 2:01]"), report)
check("health: healthy new checks", all(x in report for x in ("[ok]   System ready: System is ready",
      "[ok]   SSD: no errors (health 98.0%, temperature 32C)", "[ok]   Fans: all 1 fans OK",
      "[ok]   Counters: RX/TX ERR, DRP and OVR are zero on all 1 interfaces", "[ok]   Transceivers: all 1 present transceivers Ready")), report)
check("health: disabled features' containers are not false alarms (dhcp_relay)", "dhcp_relay" not in report and "[ok]   Containers: 4 running" in report, report)

_scenario("bad", "".join(
    ["2026 Sep 30 05:00:01.1 sonic CRIT kernel: [123.4] watchdog: BUG: soft lockup - CPU#2 stuck for 22s\n"] * 2 +
    ["2026 Sep 30 05:00:03.1 sonic ERR swss#orchagent: :- addNeighbor: Failed to add neighbor 10.0.0.59 on Ethernet8\n"] * 5 +
    ["2026 Sep 30 05:00:04.1 sonic WARNING bgp#bgpd: BGP neighbor 10.0.0.59 Down\n"] * 3))
report, overall, findings = health.run()
lv = {t: l for l, t, _ in findings}
check("health: problems found and graded", overall == "CRITICAL" and lv == {"System ready": "crit", "System health": "crit",
      "Containers": "crit", "SSD": "crit", "Fans": "crit", "Transceivers": "warn", "Interfaces": "warn", "Counters": "warn",
      "BGP": "warn", "Resources": "warn", "Logs": "crit", "Crashes": "warn"}, (overall, lv))
check("health: show system status not ready -> which service, with its reason", "System is not ready - one or more services are not up: "
      "syncd (Down/Down, Inactive)" in report, report)
check("health: ssdhealth error reported ('Error Count : 0' is fine)", "SSD: errors reported: Error : SMART read failed on /dev/sda" in report
      and "Uncorrectable" not in report, report)
check("health: fans not OK named", "1 of 2 fans not OK: FanTray2 Fan2 (Not OK)" in report, report)
check("health: non-zero RX/TX ERR/DRP/OVR per interface", "2 interface(s) with non-zero error/drop/overrun counters (since the last clear): "
      "Ethernet0 TX_DRP=1,024; Ethernet8 RX_ERR=12 RX_DRP=3" in report, report)
check("health: transceivers not Ready (absent ones ignored)", "1 of 2 present transceivers not Ready: Ethernet8 (Not Ready)" in report
      and "Ethernet12" not in report.split("Transceivers")[1].split("\n")[0], report)
check("health: says what is wrong", "services Not OK (Not Running: snmp), hardware OK" in report and "not running: syncd (Exited (1) 2 hours ago)" in report
      and "1 of 2 enabled ports are down: Ethernet8" in report and "1 of 2 neighbors not established: 10.0.0.59 (Active)" in report
      and "disk / 91% used" in report and "2 critical, 5 errors, 3 warnings" in report and "orchagent.1727670000.123.core.gz" in report, report)
check("health: log messages grouped, numbers ignored", "(x2) kernel: [#.#] watchdog: BUG: soft lockup - CPU## stuck for #s" in report
      or "(x2) kernel:" in report, report)
check("health: critical findings listed first", report.splitlines()[1].strip().startswith("[CRIT]"), report)
check("health: an admin-down port is not a problem (Ethernet12)", "Ethernet12" not in report)

_scenario("nosh", "")
report, overall, findings = health.run()
check("health: a check missing on this switch is 'not available', not a failure", "[info] System health: not available here" in report
      and "[info] System ready: not available here" in report and overall == "HEALTHY", report)

CALLS.clear()
_scenario("good", "")
res = A.query("is the switch healthy?", "h0")
check("health question, healthy switch -> ZERO model calls (nothing to analyse)", CALLS == [] and res["answer"].startswith("Switch health: HEALTHY")
      and "Insights" not in res["answer"], (CALLS, res.get("answer")))
CALLS.clear()
_scenario("bad", "2026 Sep 30 05:00:01.1 sonic ERR swss#orchagent: something failed\n")
plan_stub({"summary": "syncd is down, so the switch isn't ready; Ethernet8 errors and the BGP neighbor down likely follow from it.",
           "causes": ["syncd exited (Containers, System ready): ASIC programming stopped",
                      "Ethernet8 RX_ERR=12 (Counters) and Not Ready transceiver: possible optic or cable fault"],
           "next_steps": ["show interfaces transceiver presence Ethernet8 to check the optic", "show interfaces counters -i Ethernet8",
                          "show made up command to check", "config interface startup Ethernet8 once the optic is fixed"]})
res = A.query("is the switch healthy?", "h1")
an = CALLS[0]["messages"][1]["content"] if CALLS else ""
check("health question, problems -> checks by code, then ONE model call for insights", len(CALLS) == 1 and res["how"] == "health"
      and res["answer"].startswith("Switch health: CRITICAL") and "Health: Containers: [CRIT]" in msgs(res), (len(CALLS), res.get("answer")))
check("insights: the model gets the findings and the grouped system log", "CHECK RESULTS:" in an and "syncd (Down/Down, Inactive)" in an
      and "SYSTEM LOG (most frequent, with counts):\nERR (x1) swss#orchagent: something failed" in an, an[-900:])
check("insights: same instructions as planning (llama.cpp's cache is kept)", CALLS[0]["messages"][0]["content"] == A._plan_system())
check("insights: shown under the report", "Insights (SLM, from the findings above):" in res["answer"]
      and "Likely causes:" in res["answer"] and "syncd exited" in res["answer"], res["answer"])
check("insights: suggested commands checked: invalid dropped, changes marked", "show made up command" not in res["answer"]
      and "(a change: ask Dial 0 to do it, and it will ask y/N)" in res["answer"]
      and "show interfaces counters -i Ethernet8" in res["answer"], res["answer"])
from dial0 import workflows as _wf
check("insights: kept with the result", "syncd" in (_wf.result("health", 1).get("insights") or {}).get("summary", ""))
def _boom(*a, **k):
    raise llm.LLMError("inference engine error: connection refused")
llm.complete = _boom
res = A.query("is the switch healthy?", "h1b")
check("insights: if the model fails, the checks' report still stands", res["answer"].startswith("Switch health: CRITICAL")
      and "Insights (SLM): unavailable: inference engine error" in res["answer"], res["answer"][-300:])
plan_stub({"summary": "Errors in the log.", "causes": [], "next_steps": []})
check("health: raw outputs kept for 'dial0 why'", any("swss|Up 3 days" in x.get("detail", "") for x in res["steps"]))
res = A.query("any errors or warnings?", "h2")
check("health: 'any errors or warnings?' also runs it", res["how"] == "health")
for q in ("show interface counters errors", "add vlan 10", "is Ethernet4 up?"):
    check(f"health: not triggered by '{q}'", not resolve.health_request(q))

# ------------------------------------------------------------------ 3p. workflows: run, keep, schedule
from dial0 import workflows
check("workflows: health, logs, interfaces, bgp, services, hardware, counters, resources, security, cve", list(workflows.REGISTRY) ==
      ["health", "logs", "interfaces", "bgp", "services", "hardware", "counters", "resources", "security", "cve"])
_scenario("bad", "2026 Sep 30 05:00:03.1 sonic ERR swss#orchagent: failed\n")
r = workflows.run("interfaces")
check("workflow run: a focused report, kept on the switch", r["report"].startswith("Interfaces: WARNING")
      and "Ethernet8" in r["report"] and workflows.results("interfaces")[0]["overall"] == "WARNING")
check("workflow: default retention is 10", workflows.keep_of("logs") == 10)
workflows.set_keep("logs", 3)
for _ in range(5):
    workflows.run("logs"); time.sleep(0.002)
kept = workflows.results("logs")
check("workflow: only the last N results are kept (keep 3, ran 5)", len(kept) == 3 and kept[0]["ts"] >= kept[-1]["ts"], len(kept))
workflows.set_keep("logs", 2)
check("workflow: lowering 'keep' prunes right away", len(workflows.results("logs")) == 2)
check("workflow: full report of the Nth result", workflows.result("logs", 2)["report"].startswith("System log:")
      and workflows.result("logs", 9) is None)
for bad_every in ("1m", "every day", "0h"):
    try:
        workflows.parse_every(bad_every); check(f"schedule: '{bad_every}' rejected", False)
    except workflows.WorkflowError:
        check(f"schedule: '{bad_every}' rejected", True)
check("schedule: intervals", (workflows.parse_every("30m"), workflows.parse_every("2h"), workflows.parse_every("1d")) == (1800, 7200, 86400))
try:
    workflows.schedule("nope", every="30m"); check("schedule: unknown workflow rejected", False)
except workflows.WorkflowError as e:
    check("schedule: unknown workflow rejected", "Workflows: health" in str(e))
base = time.mktime((2026, 9, 30, 5, 0, 0, 0, 0, -1))
check("schedule: daily 06:00 created at 05:00 -> today 06:00; at 07:00 -> tomorrow 06:00",
      workflows.next_due({"at": "06:00", "created": base}) == base + 3600
      and workflows.next_due({"at": "06:00", "created": base + 7200}) == base + 3600 + 86400)
sch = workflows.schedule("resources", every="30m", keep=4)
check("schedule: stored with its retention", workflows.describe(sch) == "every 30m" and workflows.keep_of("resources") == 4)
t = time.time()
check("scheduler: a new interval schedule runs on the next tick", workflows.tick(now=t) == ["resources"]
      and workflows.results("resources")[0]["trigger"] == "schedule")
check("scheduler: not again before the interval", workflows.tick(now=t + 600) == [])
check("scheduler: again once the interval has passed", workflows.tick(now=t + 1801) == ["resources"])
import threading as _thr
_g = _thr.Lock(); _held = []
_orig_run = workflows.run
workflows.run = lambda name, step=None, trigger="", analyse=None: (_held.append(_g.locked()), _orig_run(name, trigger=trigger))[1]
workflows.tick(now=t + 4000, guard=_g)
workflows.run = _orig_run
check("scheduler: runs while holding the agent's lock (never overlaps a request)", _held == [True])
check("schedule: survives a restart (kept in the state dir)", json.load(open(workflows.CONF))["schedules"]["resources"]["every"] == 1800)
check("unschedule", workflows.unschedule("resources") and "resources" not in workflows._load()["schedules"]
      and workflows.results("resources"))
CALLS.clear()
res = A.query("is the switch healthy?", "wf1")
check("a health question's result is kept as a 'health' workflow result", workflows.results("health")[0]["trigger"] == "request")
from dial0 import cli as _cli2
check("banner: never shows the last health check", "last health check" not in _cli2.banner_text())
os.environ["PATH"] = _old_path; tools.PREFIX = _old_prefix


# ------------------------------------------------------------------ 3q. CVE scan
from dial0 import cve, debver
CF = os.path.join(FIX, "..", "cve")
check("debver: dpkg ordering", debver.compare("1:9.2p1-2+deb12u2", "1:9.2p1-2+deb12u3") < 0 and debver.compare("1.0~rc1", "1.0") < 0
      and debver.compare("2:1.0", "1:9.9") > 0 and debver.compare("1.0", "1.0") == 0)
if shutil.which("dpkg"):
    import random as _r
    _r.seed(7); mism = 0
    parts = ["0", "1", "9", "10", "a", "~", "+", ".", "rc1", "deb12u1", "deb12u10", "+dfsg", "~bpo"]
    for _ in range(400):
        a = _r.choice(["", "1:"]) + str(_r.randint(0, 9)) + "".join(_r.choice(parts) for _ in range(_r.randint(0, 4))) + "-" + str(_r.randint(0, 3))
        b = _r.choice(["", "1:"]) + str(_r.randint(0, 9)) + "".join(_r.choice(parts) for _ in range(_r.randint(0, 4))) + "-" + str(_r.randint(0, 3))
        rr = subprocess.run(["dpkg", "--compare-versions", a, "lt", b], capture_output=True)
        if rr.stderr:
            continue
        eq = subprocess.run(["dpkg", "--compare-versions", a, "eq", b]).returncode == 0
        want = -1 if rr.returncode == 0 else (0 if eq else 1)
        mism += debver.compare(a, b) != want
    check("debver: identical to the real dpkg on random versions", mism == 0, mism)

CBIN = tempfile.mkdtemp(prefix="dial0-cve-bin-")
def _cs(name, body):
    open(os.path.join(CBIN, name), "w").write("#!/bin/bash\n" + body + "\nexit 0\n"); os.chmod(os.path.join(CBIN, name), 0o755)
HOSTPK = ("openssl\t3.0.13-1~deb12u1\topenssl\t3.0.13-1~deb12u1\\nopenssh-client\t1:9.2p1-2+deb12u2\topenssh\t1:9.2p1-2+deb12u2\\n"
          "linux-image-6.1.0-23-amd64\t6.1.99-1\tlinux\t6.1.99-1\\nfrr\t8.5.4-sonic-0\tfrr\t8.5.4-sonic-0\\nbash\t5.2.15-2+b2\tbash\t5.2.15-2")
_cs("dpkg-query", f'printf "{HOSTPK}\\n"')
_cs("docker", r"""
if [ "$1" = ps ]; then printf 'bgp\nsyncd\ndial0\nbroken\n'; exit 0; fi
if [ "$1" = exec ]; then
  case "$2" in
   bgp)   printf 'VERSION_CODENAME=bookworm\n---DPKG---\nopenssh-server\t1:9.2p1-2+deb12u2\topenssh\t1:9.2p1-2+deb12u2\nfrr\t8.5.4-sonic-0\tfrr\t8.5.4-sonic-0\n' ;;
   syncd) printf 'VERSION_CODENAME=bullseye\n---DPKG---\nlibssl1.1\t1.1.1n-0+deb11u5\topenssl\t1.1.1n-0+deb11u5\n' ;;
   dial0) echo SHOULD-NOT-BE-SCANNED; exit 3 ;;
   broken) echo 'Error response from daemon: container broken is not running'; exit 1 ;;
  esac
fi""")
_old_path2, _old_prefix2 = os.environ["PATH"], tools.PREFIX
os.environ["PATH"] = CBIN + ":" + os.environ["PATH"]; tools.PREFIX = []
cve.DIR = os.path.join(TMP, "cve"); cve.OS_RELEASE = os.path.join(CF, "os-release-host")
cve.TRACKER_URL = "file://" + os.path.join(CF, "tracker.json"); cve.KEV_URL = "file://" + os.path.join(CF, "kev.json")

r0 = cve.refresh()
check("cve data: downloaded (tracker + KEV)", "tracker.json updated" in r0 and "kev.json updated" in r0
      and os.path.exists(os.path.join(cve.DIR, "tracker.json")), r0)
mt = os.path.getmtime(os.path.join(cve.DIR, "tracker.json"))
check("cve data: not downloaded again while fresh", cve.refresh().startswith("CVE data is 0h old") and os.path.getmtime(os.path.join(cve.DIR, "tracker.json")) == mt)
scopes = cve.inventory()
names = [x["scope"] for x in scopes]
check("cve inventory: host + each SONiC container with its own Debian release; Dial 0's own container skipped",
      names == ["host", "bgp", "syncd", "broken"] and [x["release"] for x in scopes][:3] == ["bookworm", "bookworm", "bullseye"]
      and scopes[3].get("error"), [(x["scope"], x["release"]) for x in scopes])
tracker = cve.load_tracker({p for x in scopes for p in x["packages"]})
check("cve data: only installed packages are loaded", set(tracker) == {"openssl", "openssh", "linux", "bash", "frr"}, set(tracker))
items, stats = cve.match(scopes, tracker, cve.load_kev())
byid = {(x["cve"], x["package"]): x for x in items if x["release"] == "bookworm"}
byrel = {(x["cve"], x["package"], x["release"]): x for x in items}
check("cve match: known exploited, on host and bgp (grouped), fix version given", byid[("CVE-2024-6387", "openssh")]["kev"]
      and byid[("CVE-2024-6387", "openssh")]["scopes"] == ["host", "bgp"] and byid[("CVE-2024-6387", "openssh")]["fixed"] == "1:9.2p1-2+deb12u3")
check("cve match: each release gets its own row with ITS fixed version and urgency (not merged)",
      byrel[("CVE-2024-0001", "openssl", "bookworm")]["installed"] == {"host": "3.0.13-1~deb12u1"}
      and byrel[("CVE-2024-0001", "openssl", "bookworm")]["fixed"] == "3.0.15-1~deb12u1"
      and byrel[("CVE-2024-0001", "openssl", "bookworm")]["urgency"] == "high"
      and byrel[("CVE-2024-0001", "openssl", "bullseye")]["installed"] == {"syncd": "1.1.1n-0+deb11u5"}
      and byrel[("CVE-2024-0001", "openssl", "bullseye")]["fixed"] == "1.1.1w-0+deb11u2"
      and byrel[("CVE-2024-0001", "openssl", "bullseye")]["urgency"] == "medium", byrel)
check("cve match: open = vulnerable with no fix yet", byid[("CVE-2024-0003", "linux")]["fixed"] is None)
check("cve match: already fixed, unimportant, undetermined, TEMP ids and uninstalled packages are not reported",
      ("CVE-2023-0002", "openssh") not in byid and ("CVE-2024-0004", "linux") not in byid and ("CVE-2024-0005", "linux") not in byid
      and not any(x["cve"].startswith("TEMP") for x in items) and not any(x["package"] == "notinstalled" for x in items)
      and stats == {"unimportant": 1, "undetermined": 1}, (sorted(byid), stats))
check("cve match: known exploited first", items[0]["cve"] == "CVE-2024-6387")
report, overall, findings, extra = cve.run_scan()
check("cve report: CRITICAL because of a known-exploited CVE", overall == "CRITICAL" and report.startswith(
      "CVE scan: CRITICAL (1 known exploited, 1 high urgency, 2 other)"), report)
check("cve report: says what, where, installed and fixed version", "CVE-2024-6387    openssh 1:9.2p1-2+deb12u2 -> fixed in 1:9.2p1-2+deb12u3  [high]  (host, bgp; bookworm)" in report, report)
check("cve report: SONiC's FRR judged by upstream version, clearly marked approximate", "SONiC-built packages (approximate)" in report
      and "CVE-2024-0100    frr 8.5.4-sonic-0 -> fixed upstream in 8.5.6" in report and "CVE-2024-0101" not in report
      and "SONiC may have backported fixes" in report, report)
check("cve: SONiC builds never raise the verdict and aren't in Debian rows", not any(x["package"] == "frr" for x in extra["items"]))
check("cve report: unreadable container named; data age shown", "couldn't read: broken" in report and "Debian data 0h old, KEV 0h old" in report)
check("cve report: how fixes work on SONiC", "newer SONiC image" in report)
check("cve evidence for the model: CVE rows with Debian's descriptions", "regreSSHion" in extra["excerpt"])

cve.TRACKER_URL = "file:///nonexistent/tracker.json"
st = cve.refresh(force=True)
check("cve data: a failed download keeps and uses the previous copy", "download failed" in st and "using the previous copy" in st
      and os.path.exists(os.path.join(cve.DIR, "tracker.json")), st)
import gzip as _gz
gzf = os.path.join(TMP, "tracker.json.gz")
with open(os.path.join(CF, "tracker.json"), "rb") as fsrc, _gz.open(gzf, "wb") as fdst:
    fdst.write(fsrc.read())
cve.TRACKER_URL = "file://" + gzf
cve.refresh(force=True)
check("cve data: gzip-compressed download handled", cve.load_tracker({"openssl"}).get("openssl"))
big = os.path.join(TMP, "big.json")
with open(big, "w") as f:
    f.write("{\n")
    f.write(",\n".join(f'  "pkg{i}": {{"CVE-2024-{i:05d}": {{"description": "{"x" * 200}", "releases": {{"bookworm": {{"status": "open", "urgency": "low"}}}}}}}}' for i in range(60000)))
    f.write(',\n  "openssl": ' + json.dumps(json.load(open(os.path.join(CF, "tracker.json")))["openssl"], indent=2) + "\n}")
os.replace(big, os.path.join(cve.DIR, "tracker.json"))
t0 = time.time(); got = cve.load_tracker({"openssl", "pkg123"}); dt = time.time() - t0
sz = os.path.getsize(os.path.join(cve.DIR, "tracker.json")) / 1e6
check(f"cve data: a {sz:.0f} MB pretty-printed file is read package by package ({dt:.1f}s), keeping only what's installed",
      set(got) == {"openssl", "pkg123"} and dt < 20, (set(got), dt))
cve.TRACKER_URL = "file://" + os.path.join(CF, "tracker.json"); cve.refresh(force=True)
_saved_dir = cve.DIR; cve.DIR = os.path.join(TMP, "cve-empty"); cve.TRACKER_URL = "file:///nonexistent"
report, overall, findings, extra = cve.run_scan()
check("cve: no data at all -> says so and how to fix it, nothing made up", report.startswith("CVE scan: NOT RUN") and "dial0 cve update" in report)
cve.DIR = _saved_dir; cve.TRACKER_URL = "file://" + os.path.join(CF, "tracker.json")

for q in ("any vulnerabilities on the switch?", "scan for CVEs", "is the switch vulnerable to known exploits?", "check for unpatched packages"):
    check(f"cve question: '{q}'", resolve.cve_request(q))
for q in ("show vlan brief", "is the switch healthy?", "add vlan 10"):
    check(f"cve question: not '{q}'", not resolve.cve_request(q))
CALLS.clear()
plan_stub({"summary": "regreSSHion (CVE-2024-6387) is known exploited and affects the host and bgp container.",
           "causes": ["openssh 1:9.2p1-2+deb12u2 is older than the fixed 1:9.2p1-2+deb12u3"],
           "next_steps": ["Plan an upgrade to a SONiC image built with openssh 1:9.2p1-2+deb12u3 or later"]})
res = A.query("any vulnerabilities on the switch?", "cv1")
check("cve question -> the scan, then ONE model call for insights on the CVE details", res["how"] == "cve" and len(CALLS) == 1
      and "CVE DETAILS (from Debian's security data):" in CALLS[0]["messages"][1]["content"]
      and "Never state facts about a CVE" in CALLS[0]["messages"][1]["content"] and "regreSSHion (CVE-2024-6387)" in res["answer"], res.get("answer"))
check("cve: kept as a workflow result with the full list", len(workflows.result("cve", 1)["items"]) == 4)

# ------------------------------------------------------------------ 3r. security audit workflow
import warnings as _w
def _sha512_crypt(pw):
    """A SHA-512 crypt hash: Python's crypt module where it exists (removed in 3.13), else openssl."""
    try:
        with _w.catch_warnings():
            _w.simplefilter("ignore"); import crypt as _crypt
        return _crypt.crypt(pw, _crypt.mksalt(_crypt.METHOD_SHA512))
    except ImportError:
        return subprocess.run(["openssl", "passwd", "-6", pw], capture_output=True, text=True).stdout.strip()
from dial0 import security
SBIN = tempfile.mkdtemp(prefix="dial0-sec-bin-"); SSCEN = os.path.join(SBIN, "scenario")
DEF_HASH = _sha512_crypt("YourPaSsWoRd")
GOOD_HASH = _sha512_crypt("S0me-Strong-Pass!")
open(os.path.join(SBIN, "shadow-bad"), "w").write(f"root:*:19000::::::\nadmin:{DEF_HASH}:19000:0:99999:7:::\nguest::19000::::::\n")
open(os.path.join(SBIN, "shadow-good"), "w").write(f"root:*:19000::::::\nadmin:{GOOD_HASH}:19000:0:99999:7:::\n")
def _ss(name, body):
    open(os.path.join(SBIN, name), "w").write("#!/bin/bash\nS=$(cat " + SSCEN + ")\n" + body + "\nexit 0\n")
    os.chmod(os.path.join(SBIN, name), 0o755)
_ss("getent", f"""
if [ "$1" = shadow ]; then cat {SBIN}/shadow-$S; exit 0; fi
printf 'root:x:0:0:root:/root:/bin/bash\\nadmin:x:1000:1000::/home/admin:/bin/bash\\nsshd:x:110:65534::/run/sshd:/usr/sbin/nologin\\n'
[ "$S" = bad ] && printf 'toor:x:0:0::/root:/bin/bash\\n'""")
_ss("sshd", r"""printf 'port 22\nmaxauthtries 6\npasswordauthentication yes\nkexalgorithms curve25519-sha256,diffie-hellman-group14-sha256\n'
if [ "$S" = bad ]; then printf 'permitrootlogin yes\npermitemptypasswords no\nciphers aes128-ctr,aes256-cbc\nmacs hmac-sha2-256,hmac-sha1\nx11forwarding yes\n'
else printf 'permitrootlogin no\npermitemptypasswords no\nciphers chacha20-poly1305@openssh.com,aes256-gcm@openssh.com\nmacs hmac-sha2-256-etm@openssh.com\nx11forwarding no\n'; fi""")
_ss("ss", r"""printf 'tcp LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=1,fd=3))\ntcp LISTEN 0 128 127.0.0.1:8090 0.0.0.0:* users:(("python",pid=2,fd=3))\nudp UNCONN 0 0 0.0.0.0:161 0.0.0.0:* users:(("snmpd",pid=3,fd=3))\n'
[ "$S" = bad ] && printf 'tcp LISTEN 0 128 0.0.0.0:23 0.0.0.0:* users:(("telnetd",pid=4,fd=3))\n'""")
_ss("sonic-db-cli", r"""case "$3" in
 'SNMP_COMMUNITY|*') [ "$S" = bad ] && echo 'SNMP_COMMUNITY|public' || echo 'SNMP_COMMUNITY|n0c-r3ad0nly' ;;
 'SYSLOG_SERVER|*') [ "$S" = good ] && echo 'SYSLOG_SERVER|10.0.0.5' ;;
 'NTP_SERVER|*') [ "$S" = good ] && echo 'NTP_SERVER|10.0.0.6' ;;
esac""")
_ss("show", r"""case "$*" in
 "acl table") printf 'Name       Type       Binding    Description    Stage\n---------  ---------  ---------  -------------  -------\n'
              [ "$S" = good ] && printf 'SSH_ONLY   CTRLPLANE  SSH        mgmt ssh       ingress\n' ;;
 "aaa") printf 'AAA authentication login local (default)\nAAA authentication failthrough False (default)\n' ;;
 "version") if [ "$S" = bad ]; then printf 'SONiC Software Version: SONiC.202305.0-old\nKernel: 5.10.0-21-2-amd64\nBuild date: Mon Jun  5 10:00:00 UTC 2023\n'
            else printf 'SONiC Software Version: SONiC.202505.0-new\nKernel: 6.1.0-29-2-amd64\nBuild date: Mon Sep  1 10:00:00 UTC 2026\n'; fi ;;
esac""")
AUTHLOG = os.path.join(SBIN, "auth.log"); WWD = os.path.join(SBIN, "etcdir"); os.makedirs(WWD)
security.AUTH_LOG = AUTHLOG; security.WW_PATHS = WWD
os.environ["PATH"] = SBIN + ":" + os.environ["PATH"]
def _sec_scen(name):
    open(SSCEN, "w").write(name)
    lines = ([f"Sep 30 05:{i % 60:02d}:01 sonic sshd[1]: Failed password for root from 203.0.113.9 port 4{i:04d} ssh2" for i in range(25)]
             if name == "bad" else ["Sep 30 05:00:01 sonic sshd[1]: Failed password for admin from 10.0.0.7 port 40000 ssh2"])
    open(AUTHLOG, "w").write("\n".join(lines) + "\n")
    wf = os.path.join(WWD, "evil.conf"); open(wf, "w").write("x")
    os.chmod(wf, 0o666 if name == "bad" else 0o644)

_sec_scen("bad")
report, overall, findings, extra = security.run()
lv = {t: l for l, t, _ in findings if not t.startswith("CVE: ")}
check("security: problems found and graded", overall == "CRITICAL" and lv == {
      "Passwords": "crit", "Accounts": "crit", "SSH": "crit", "Exposed services": "crit", "SNMP": "warn", "Management ACL": "warn",
      "AAA and logging": "warn", "Login attempts": "warn", "Software": "warn", "File permissions": "warn", "Dial 0 itself": "ok"}, lv)
check("security: default SONiC password and empty password found", "default SONiC password still set for: admin; no password at all: guest" in report, report)
check("security: extra root account and passwordless sudo", "accounts with root privileges (UID 0) besides root: toor" in report)
check("security: SSH weaknesses named", "root can log in with a password (PermitRootLogin yes)" in report and "weak ciphers: aes256-cbc" in report
      and "weak MACs: hmac-sha1" in report and "X11 forwarding on" in report, report)
check("security: telnet listening is critical", "insecure services listening: telnet (tcp/23, telnetd)" in report)
check("security: SNMP 'public', no control-plane ACL, no remote syslog/NTP", "guessable community name(s): public" in report
      and "no control-plane ACL" in report and "no remote syslog server" in report and "no NTP server" in report)
check("security: brute-force source found", "repeated from: 203.0.113.9 (x25)" in report)
check("security: old image flagged", "SONiC.202305.0-old, built 2023-06-05" in report and "over a year old" in report)
check("security: world-writable file found", "evil.conf" in report)
check("security: includes the CVE scan", "Vulnerabilities:" in report and "CVE scan: CRITICAL" in report and "CVE-2024-6387" in report)
res_all = json.dumps(findings) + report + json.dumps(extra)
check("security: password hashes appear nowhere (report, findings, evidence)", DEF_HASH not in res_all and DEF_HASH[:20] not in res_all
      and "$6$" not in res_all)
_steps = []
security.run(lambda m, d="": _steps.append(m + d))
check("security: password hashes not in the steps or raw output either", not any("$6$" in x for x in _steps))

_sec_scen("good")
report, overall, findings, extra = security.run()
lv = {t: l for l, t, _ in findings if not t.startswith("CVE: ")}
check("security: a hardened switch passes the configuration audit", all(v in ("ok", "info") for v in lv.values())
      and "no default or empty passwords" in report and "control-plane ACL(s): SSH_ONLY" in report, (lv, report[:900]))

for q in ("run a security audit", "is the switch secure?", "security check please", "security scan"):
    check(f"security question: '{q}'", resolve.security_request(q) and not resolve.health_request(q) or resolve.security_request(q))
for q in ("any vulnerabilities?", "show vlan brief", "add vlan 10"):
    check(f"security question: not '{q}'", not resolve.security_request(q))
_sec_scen("bad")
CALLS.clear()
plan_stub({"summary": "The default admin password and telnet make the switch easy to take over; regreSSHion is known exploited.",
           "causes": ["admin still has the SONiC default password (Passwords)", "telnet listening on tcp/23 (Exposed services)"],
           "next_steps": ["Change the admin password now", "show acl table"]})
res = A.query("run a security audit", "sec1")
check("security question -> audit + CVE scan, then ONE model call with the issues as evidence", res["how"] == "security" and len(CALLS) == 1
      and "SECURITY FINDINGS" in CALLS[0]["messages"][1]["content"] and "CRIT Passwords: default SONiC password" in CALLS[0]["messages"][1]["content"]
      and "Insights (SLM, from the findings above):" in res["answer"] and DEF_HASH not in CALLS[0]["messages"][1]["content"], res.get("answer", "")[:400])
check("security: kept as a workflow result", workflows.results("security")[0]["overall"] == "CRITICAL")
os.environ["PATH"] = _old_path2; tools.PREFIX = _old_prefix2


# ------------------------------------------------------------------ 3s. blueprints (template-based configuration)
from dial0 import blueprints as bp
BBIN = tempfile.mkdtemp(prefix="dial0-bp-bin-"); BLOG = os.path.join(BBIN, "calls.log"); BSCEN = os.path.join(BBIN, "scenario")
PORTS = {f"Ethernet{i}": {"admin_status": "down", "mtu": "9100"} for i in range(0, 512, 4)}
def _bdb(**extra):
    db = {"PORT": dict(PORTS), "DEVICE_METADATA": {"localhost": {"hostname": "leaf1", "docker_routing_config_mode": "split"}}}
    for k, v in extra.items():
        db[k] = v
    open(os.path.join(BBIN, "db.json"), "w").write(json.dumps(db))
def _bs(name, body):
    open(os.path.join(BBIN, name), "w").write("#!/bin/bash\nS=$(cat " + BSCEN + " 2>/dev/null)\necho \"" + name + " $*\" >> " + BLOG + "\n" + body + "\n")
    os.chmod(os.path.join(BBIN, name), 0o755)
_bs("sonic-cfggen", f"cat {BBIN}/db.json")
_bs("vtysh", r"""if [ "$2" = "show running-config" ]; then cat """ + BBIN + r"""/frr.conf 2>/dev/null; exit 0; fi
[ "$S" = vtysh-error ] && { echo '% Unknown command: bgp bestpath as-path multipath-relax'; exit 0; }
echo 'Integrated configuration saved to /etc/frr/frr.conf'; echo '[OK]'""")
_bs("config", r"""case "$S:$*" in
 "vlan-exists:vlan add 100") echo 'Error: Vlan100 already exists'; exit 1 ;;
 "mtu-error:interface mtu Ethernet4 9216") echo 'Error: Interface MTU is invalid. Please enter a valid MTU'; exit 2 ;;
esac; exit 0""")
_bs("show", r"""case "$*" in
 "ip bgp summary") printf 'Neighbhor    V  AS  MsgRcvd  MsgSent  TblVer  InQ  OutQ  Up/Down  State/PfxRcd  NeighborName\n'
   [ "$S" = bgp-down ] && printf 'Ethernet256  4  65000  10  10  0  0  0  never  Active  spine1\nEthernet260  4  65000  10  10  0  0  0  never  Active  spine2\n' \
   || printf 'Ethernet256  4  65000  10  10  0  0  0  00:00:05  3  spine1\nEthernet260  4  65000  10  10  0  0  0  00:00:05  3  spine2\n' ;;
 "vlan brief") printf '| 100 | 10.10.1.1/24 | Ethernet0 | untagged |\n' ;;
 "ip interfaces") printf 'Loopback0  10.0.0.11/32  up/up\nVlan100  10.10.1.1/24  up/up\n' ;;
esac; exit 0""")
_old_path3, _old_prefix3 = os.environ["PATH"], tools.PREFIX
os.environ["PATH"] = BBIN + ":" + os.environ["PATH"]; tools.PREFIX = []
bp.BACKUP_DIR = os.path.join(BBIN, "backups"); bp.BGP_WAIT_S = 0
def _scen(x): open(BSCEN, "w").write(x)
def _calls():
    return [l.rstrip("\n") for l in open(BLOG)] if os.path.exists(BLOG) else []
_scen(""); _bdb(); open(os.path.join(BBIN, "frr.conf"), "w").write("")

LEAF = {"role": "leaf", "loopback": "10.0.0.11", "asn": "65001", "downlinks": "Ethernet0-Ethernet4",
        "uplinks": "Ethernet256, Ethernet260", "vlan": "100", "gateway": "10.10.1.1/24"}
SPINE = {"role": "spine", "loopback": "10.0.0.1/32", "asn": "65000", "downlinks": "Ethernet0,Ethernet4,Ethernet504-Ethernet508"}
check("blueprints: the catalogue has the 2-tier Clos AI fabric", [b["name"] for b in bp.catalogue()] == ["2-tier-clos-ai-fabric-scale-out"])
check("blueprints: questions depend on the role", [q["key"] for q in bp.questions(bp.CLOS, "spine")] == ["role", "loopback", "asn", "downlinks"]
      and [q["key"] for q in bp.questions(bp.CLOS, "leaf")] == ["role", "loopback", "asn", "downlinks", "uplinks", "vlan", "gateway"])
ports = bp.switch_ports(bp.snapshot())
check("ports: ranges expand to the switch's own ports (step 4 here)", bp.parse_ports("Ethernet0-Ethernet12", ports) ==
      ["Ethernet0", "Ethernet4", "Ethernet8", "Ethernet12"] and bp.parse_ports("ethernet504-508", ports) == ["Ethernet504", "Ethernet508"])
for bad, why in (("Ethernet2", "not a port"), ("Ethernet12-Ethernet0", "backwards"), ("Ethernet600-Ethernet700", "no ports")):
    try:
        bp.parse_ports(bad, ports); check(f"ports: '{bad}' rejected", False)
    except bp.BlueprintError as e:
        check(f"ports: '{bad}' rejected ({why})", why in str(e), str(e))
db0 = bp.snapshot()
pl, err = bp.validate(bp.CLOS, LEAF, db0)
check("validate: a good leaf", not err and pl == {"role": "leaf", "loopback": "10.0.0.11", "asn": 65001, "downlinks": ["Ethernet0", "Ethernet4"],
      "uplinks": ["Ethernet256", "Ethernet260"], "vlan": 100, "gateway": "10.10.1.1/24"}, (pl, err))
_, err = bp.validate(bp.CLOS, dict(LEAF, loopback="10.0.0.11/24", asn="0", vlan="1", gateway="10.10.1.0/24", uplinks="Ethernet4"), db0)
check("validate: every bad value explained", set(err) == {"loopback", "asn", "vlan", "gateway", "uplinks"}
      and "not its network or broadcast" in err["gateway"] and "also downlinks: Ethernet4" in err["uplinks"], err)
_, err = bp.validate(bp.CLOS, dict(LEAF, gateway="10.0.0.1/8"), db0)
check("validate: loopback inside the server subnet is rejected", "loopback 10.0.0.11 is inside this subnet" in err.get("gateway", ""), err)
check("validate: role must be spine or leaf", bp.validate(bp.CLOS, {"role": "core"}, db0)[1] == {"role": "must be spine or leaf"})

steps = bp.generate(bp.CLOS, pl, db0)
cmds = [c["cmd"] for s in steps for c in s["commands"]]
check("leaf: the corrected sample, in order", cmds[:9] == [
    "config loopback add Loopback0", "config interface ip add Loopback0 10.0.0.11/32",
    "config interface startup Ethernet0", "config interface startup Ethernet4",
    "config interface mtu Ethernet0 9216", "config interface mtu Ethernet4 9216",
    "config vlan add 100", "config interface ip add Vlan100 10.10.1.1/24", "config vlan member add -u 100 Ethernet0"]
      and cmds[9:17] == ["config vlan member add -u 100 Ethernet4", "config interface startup Ethernet256", "config interface startup Ethernet260",
                        "config interface mtu Ethernet256 9216", "config interface mtu Ethernet260 9216",
                        "config interface ipv6 enable use-link-local-only Ethernet256", "config interface ipv6 enable use-link-local-only Ethernet260",
                        cmds[16]] and cmds[-1] == "config save -y", cmds)
vt = [c for s in steps for c in s["commands"] if c["tool"] == "vtysh"][0]["argv"][2::2]
check("leaf BGP: peers on the UPLINKS (not Ethernet0/8 as in the sample), redistribute connected, write memory", vt == [
    "configure terminal", "router bgp 65001", "bgp router-id 10.0.0.11", "bgp bestpath as-path multipath-relax", "no bgp ebgp-requires-policy",
    "neighbor SPINE peer-group", "neighbor SPINE remote-as external", "neighbor Ethernet256 interface peer-group SPINE",
    "neighbor Ethernet260 interface peer-group SPINE", "address-family ipv4 unicast", "network 10.0.0.11/32", "redistribute connected",
    "maximum-paths 64", "exit-address-family", "end", "write memory"], vt)
check("leaf: no IPv6 link-local on server ports (they're in the VLAN)", "config interface ipv6 enable use-link-local-only Ethernet0" not in cmds)
sp, err = bp.validate(bp.CLOS, SPINE, db0)
st = bp.generate(bp.CLOS, sp, db0)
scmds = [c["cmd"] for s in st for c in s["commands"]]
svt = [c for s in st for c in s["commands"] if c["tool"] == "vtysh"][0]["argv"][2::2]
check("spine: matches the sample (loopback, downlinks up + MTU + link-local, BGP to LEAF, save)", not err
      and scmds[:2] == ["config loopback add Loopback0", "config interface ip add Loopback0 10.0.0.1/32"]
      and [x for x in scmds if "ipv6" in x] == [f"config interface ipv6 enable use-link-local-only Ethernet{i}" for i in (0, 4, 504, 508)]
      and not any("vlan" in x for x in scmds)
      and svt[:7] == ["configure terminal", "router bgp 65000", "bgp router-id 10.0.0.1", "bgp bestpath as-path multipath-relax",
                      "no bgp ebgp-requires-policy", "neighbor LEAF peer-group", "neighbor LEAF remote-as external"]
      and [x for x in svt if x.startswith("neighbor Ethernet")] == [f"neighbor Ethernet{i} interface peer-group LEAF" for i in (0, 4, 504, 508)]
      and "redistribute connected" not in svt and svt[-1] == "write memory" and scmds[-1] == "config save -y", (scmds, svt))

_bdb(LOOPBACK_INTERFACE={"Loopback0": {}, "Loopback0|10.0.0.11/32": {}}, VLAN={"Vlan100": {"vlanid": "100"}},
     VLAN_MEMBER={"Vlan100|Ethernet0": {"tagging_mode": "untagged"}})
r = bp.plan(bp.CLOS, LEAF)
skips = {c["cmd"]: c["skip"] for s in r["steps"] for c in s["commands"] if c.get("skip")}
check("plan: what's already in place is marked, not repeated", skips == {"config loopback add Loopback0": "already exists",
      "config interface ip add Loopback0 10.0.0.11/32": "already set", "config vlan add 100": "already exists",
      "config vlan member add -u 100 Ethernet0": "already a member"} and not r["conflicts"], (skips, r["conflicts"]))

_bdb(PORTCHANNEL_MEMBER={"PortChannel1|Ethernet256": {}}, VLAN_MEMBER={"Vlan200|Ethernet4": {}, "Vlan300|Ethernet260": {}},
     INTERFACE={"Ethernet0|192.168.9.1/24": {}, "Ethernet260": {"vrf_name": "Vrf-red"}},
     LOOPBACK_INTERFACE={"Loopback0|10.9.9.9/32": {}}, VLAN_INTERFACE={"Vlan100|10.20.0.1/24": {}},
     DEVICE_METADATA={"localhost": {"docker_routing_config_mode": "unified"}})
open(os.path.join(BBIN, "frr.conf"), "w").write("!\nrouter bgp 64999\n bgp router-id 10.9.9.9\n!\n")
r = bp.plan(bp.CLOS, LEAF)
check("conflicts: every one reported, in plain words", sorted(r["conflicts"]) == sorted([
    "Ethernet256 is a member of PortChannel1", "Ethernet260 is bound to VRF Vrf-red", "Ethernet260 (fabric link) is in Vlan300",
    "Ethernet4 (server port) is in Vlan200", "Ethernet0 (server port) has IP 192.168.9.1/24",
    "Loopback0 already has IP 10.9.9.9/32", "Vlan100 already has IP 10.20.0.1/24",
    "BGP is already running with ASN 64999 (this blueprint uses 65001)"]), r["conflicts"])
check("blocker: FRR mode where vtysh changes wouldn't persist", len(r["blockers"]) == 1 and "'unified'" in r["blockers"][0])
open(BLOG, "w").close()
res = bp.apply(bp.CLOS, LEAF, r["plan_id"])
check("apply: refused with conflicts; NOTHING run or removed", not res["ok"] and "nothing applied" in res["error"]
      and not any(c.startswith(("config ", "vtysh -c configure")) for c in _calls()), (res, _calls()))

_bdb(); open(os.path.join(BBIN, "frr.conf"), "w").write("")
r = bp.plan(bp.CLOS, LEAF)
res = bp.apply(bp.CLOS, LEAF, "stale-plan")
check("apply: refused if the switch changed since the plan was shown", not res["ok"] and "changed since the plan was shown" in res["error"])
open(BLOG, "w").close()
_steps = []
res = bp.apply(bp.CLOS, LEAF, r["plan_id"], step=lambda m, d="": _steps.append(m))
calls = _calls()
first_cfg = next(i for i, c in enumerate(calls) if c.startswith("config "))
bk = [i for i, c in enumerate(calls) if c.startswith("sonic-cfggen -d --print-data")]
check("apply: backup BEFORE any change", res["ok"] and os.path.exists(os.path.join(res["backup"]["dir"], "config_db.json"))
      and os.path.exists(os.path.join(res["backup"]["dir"], "frr-running.conf")) and bk and max(bk) < first_cfg, (res, calls[:6]))
ran = [c for c in calls if c.startswith("config ") or c.startswith("vtysh -c configure")]
check("apply: every command in order, BGP through the vtysh tool, saved last", [c[7:] for c in ran if c.startswith("config ")][:3] == [
      "loopback add Loopback0", "interface ip add Loopback0 10.0.0.11/32", "interface startup Ethernet0"]
      and ran[-2].startswith("vtysh -c configure terminal -c router bgp 65001") and ran[-1] == "config save -y" and len(ran) == res["applied"], ran[-3:])
check("apply: verified (VLAN, loopback, BGP neighbors on the uplinks)", res["verify"] == {"vlan": "Vlan100 present",
      "loopback": "Loopback0 has 10.0.0.11", "bgp": "2 of 2 BGP neighbors established"}, res["verify"])
check("apply: answers saved for next time", bp.saved(bp.CLOS)["asn"] == 65001 and bp.saved(bp.CLOS)["uplinks"] == "Ethernet256, Ethernet260")
check("apply: restore instructions given (not run)", "config reload -y" in res["backup"]["restore"][0]
      and not any("reload" in c for c in calls))
_scen("vlan-exists"); r = bp.plan(bp.CLOS, LEAF)
res = bp.apply(bp.CLOS, LEAF, r["plan_id"])
check("apply: 'already exists' from SONiC counts as done and continues", res["ok"] and res["skipped"] >= 1)
_scen("mtu-error"); r = bp.plan(bp.CLOS, LEAF); open(BLOG, "w").close()
res = bp.apply(bp.CLOS, LEAF, r["plan_id"])
check("apply: a real error stops it, says where and why; later commands not run", not res["ok"]
      and "stopped at 'config interface mtu Ethernet4 9216': Error: Interface MTU is invalid" in res["error"]
      and not any("vlan add" in c for c in _calls()) and res["applied"] == 5 and res["backup"]["dir"], res)
_scen("vtysh-error"); r = bp.plan(bp.CLOS, LEAF)
res = bp.apply(bp.CLOS, LEAF, r["plan_id"])
check("apply: FRR's '%' errors are caught (vtysh exits 0 even then)", not res["ok"] and "% Unknown command" in res["error"]
      and not any(c == "config save -y" for c in _calls()[-3:]), res.get("error"))
_scen("bgp-down"); r = bp.plan(bp.CLOS, LEAF)
res = bp.apply(bp.CLOS, LEAF, r["plan_id"])
check("apply: BGP not up yet -> says so and why", res["ok"] and res["verify"]["bgp"].startswith("0 of 2 BGP neighbors established; not yet: "
      "Ethernet256 (Active), Ethernet260 (Active). They come up once the other side is configured."), res["verify"])
_scen("")
from dial0 import health as _h
check("health: unnumbered BGP neighbors (by interface) are understood", _h.bgp()[2] == "all 2 neighbors established")
check("plain English 'apply the clos blueprint' -> how to run it, no model", (lambda r: r["how"] == "blueprint" and
      "dial0 blueprint apply" in r["answer"])(A.query("apply the clos blueprint on this switch", "bpq")))
_bp_env = (BBIN, _old_path3, _old_prefix3)
os.environ["PATH"] = _old_path3; tools.PREFIX = _old_prefix3

# ------------------------------------------------------------------ 4. sessions
s = sessions.get("t-ok")
sessions._cache.clear()
s2 = sessions.get("t-ok")
check("session persists across reload", len(s2.messages) == len(s.messages) and s2.log == s.log)
check("sessions isolated", sessions.get("t-deny").log != sessions.get("t-ok").log)
big = sessions.get("t-window")
big.add("user", "ORIGINAL REQUEST: configure vlan 300")
for i in range(40):
    big.add("assistant", json.dumps({"thought": "x" * 200, "tool": "ref", "input": "find vlan"}))
    big.add("user", "OBSERVATION: " + "y" * 300)
w = big.window("S" * 3000)
check("window fits budget", sum(len(m["content"]) for m in w) <= 9000 + 1600)
check("window keeps the current request", any("ORIGINAL REQUEST" in m["content"] for m in w))
check("window ends with newest message", w[-1]["content"].startswith("OBSERVATION"))
w2 = big.window("S" * 20000)
check("window never empty even if system prompt is huge", len(w2) >= 2 and w2[-1]["role"] == "user")
sessions.peek("ghost")
check("peek does not create sessions", "ghost" not in [x["name"] for x in sessions.list_all()])
try:
    sessions.get("../etc"); check("bad session name rejected", False)
except ValueError:
    check("bad session name rejected", True)

# session disk cap: oldest sessions are dropped, the active one never is
sessions.STATE_MAX_MB = 0.02  # ~20 KB
for i in range(6):
    x = sessions.get(f"cap{i}")
    x.add("user", "z" * 6000)
    time.sleep(0.01)
files = [f for f in os.listdir(sessions.DIR) if f.endswith(".json")]
total = sum(os.path.getsize(os.path.join(sessions.DIR, f)) for f in files)
check("session storage capped", total <= 0.02 * 1024 * 1024 + 7000, total)
check("newest session kept under cap", "cap5.json" in files)
check("oldest session dropped under cap", "cap0.json" not in files)
sessions.STATE_MAX_MB = 200

# ------------------------------------------------------------------ 5. LLM output parsing
check("parse plain JSON", llm._parse('{"thought":"","tool":"final","input":"hi"}')["input"] == "hi")
check("parse fenced JSON", llm._parse('```json\n{"thought":"","tool":"ref","input":"find vlan"}\n```')["tool"] == "ref")
check("parse strips <think>", llm._parse('<think>hmm</think>{"thought":"","tool":"final","input":"x"}')["input"] == "x")
try:
    llm._parse('{"tool":"shell","input":"rm"}'); check("invalid tool rejected", False)
except llm.LLMError:
    check("invalid tool rejected", True)

# ------------------------------------------------------------------ 6. HTTP API + CLI end to end
sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
server.agent = A
httpd = server.ThreadingHTTPServer(("127.0.0.1", port), server.H)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
plan_stub({"commands": ["config vlan add 300"], "note": ""})
env = dict(os.environ, DIAL0_API_PORT=str(port), PYTHONPATH=ROOT)


def cli(*args):
    return subprocess.run([sys.executable, "-m", "dial0.cli", *args], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)


out = cli("-s", "e2e", "add vlan 300")
check("cli: pending shown when not interactive", "Pending approval:" in out.stdout and "config vlan add 300" in out.stdout, out.stdout + out.stderr)
out = cli("-t", "-s", "e2e2", "show vlan brief")
check("cli: steps printed by default", "What Dial 0 is doing:" in out.stderr and "No model needed" in out.stderr, out.stderr)
outq = cli("-q", "-s", "e2e2", "show vlan brief")
check("cli: -q hides steps", "What Dial 0 is doing" not in outq.stderr and "$ show vlan brief" in outq.stdout, outq.stderr)
outw = cli("-s", "e2e2", "why")
check("cli: why replays last steps", "No model needed" in outw.stderr, outw.stdout + outw.stderr)
check("cli: typed command output printed", "$ show vlan brief" in out.stdout and "ok" in out.stdout, out.stdout)
check("cli: timing shows model not used", "model: not used" in out.stderr, out.stderr)
out = cli("sessions")
check("cli: sessions lists pending", "e2e" in out.stdout and "[pending approval]" in out.stdout, out.stdout)
out = cli("ref", "show", "config", "vlan", "add")
check("cli: ref", "config vlan add" in out.stdout and "source:" in out.stdout, out.stdout)
out = cli("-s", "e2e", "history")
check("cli: history (nothing run yet)", out.returncode == 0)
out = cli("-s", "bad name!", "x")
check("cli: bad session name error shown", "session name must match" in out.stdout, out.stdout)
def cli_in(stdin, *args):
    return subprocess.run([sys.executable, "-m", "dial0.cli", *args], capture_output=True, text=True, env=env, input=stdin)


EXEC.clear()
plan_stub({"why": "", "commands": ["config vlan add 600", "show vlan brief"], "note": "Adds VLAN 600."})
out = cli_in("show vlan brief\n\ncreate vlan 600\ny\n/history\n/why\n/bogus\n/fresh\n/exit\n", "--new")
o, e = out.stdout, out.stderr
check("prompt: plain banner, no session name shown", o.startswith("Dial 0 (started fresh): type a request or a SONiC command")
      and "interactive" not in o, o[:200])
check("interactive: typed SONiC command runs directly", "$ show vlan brief" in o and "No model needed" in e, (o, e))
check("prompt: requests use the context, no session name in the steps",
      "Including context: 1 earlier request(s)" in e and "interactive" not in e, e)
check("interactive: approval asked inside the prompt and applied", "Apply? (* = changes config)" in o and ["config", "vlan", "add", "600"] in EXEC, (o, EXEC))
check("interactive: /history lists what ran", "executed  config vlan add 600" in o, o)
check("interactive: /why replays steps", "You approved." in e, e)
check("interactive: unknown / command explained, empty /fresh gets usage", "unknown command /bogus" in o and "usage: /fresh <request>" in o, o)
check("interactive: /exit leaves cleanly", out.returncode == 0, out.returncode)
out = cli_in("")
check("plain dial0 opens the prompt; Ctrl-D (end of input) leaves cleanly", out.returncode == 0 and out.stdout.startswith("Dial 0: type a request"), out.stdout)
out = cli_in("/context\n/reset\n/context\n", "-s", "lab1")
check("dial0 -s NAME: named session shown, /reset clears it", out.stdout.startswith("Dial 0, session 'lab1':")
      and "Context cleared (session 'lab1')" in out.stdout and "context  (updated" not in out.stdout, out.stdout)
out = cli_in("/sessions\n")
check("prompt: session kept across visits, listed without its internal name", "(dial0 prompt)" in out.stdout
      and "interactive" not in out.stdout, out.stdout)
CALLS.clear()
out = cli_in("/exit\n", "enter")
check("old 'dial0 enter' spelling still opens interactive mode (never sent to the model)",
      out.stdout.startswith("Dial 0: type a request") and CALLS == [], out.stdout)
import pty as _pty, select as _select
def tty_run(args, feed):
    pid, fd = _pty.fork()
    if pid == 0:
        os.execvpe(sys.executable, [sys.executable, "-m", "dial0.cli", *args], env)
    buf, sent = b"", False
    end = time.time() + 20
    while time.time() < end:
        r, _, _ = _select.select([fd], [], [], 0.3)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        elif not sent:
            os.write(fd, feed); sent = True
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    return buf.decode(errors="replace")
t_out = tty_run([], b"/exit\n")
check("prompt on a terminal is just 'dial0> '", "dial0> " in t_out and "dial0:" not in t_out and "interactive" not in t_out, t_out)
check("banner: followed by a short context note (no long intro line)", "Context is on: each request builds on the ones before." in t_out
      and "type a request or a SONiC command; each one" not in t_out)
banner_lines = [l for l in t_out.replace("\r", "").splitlines()]
check("banner: shown when entering the prompt on a terminal (logo + title, no 'how it helps' section)",
      "|____/  |___| /_/   \\_\\ |_____|     \\___/" in t_out and "Dial 0 for SONiC - talk to your switch in plain English" in t_out
      and "How it helps" not in t_out and "Only real commands run" not in t_out, t_out[:600])
check("banner: no status or health lines (logo, title, tips only)", "changes ask y/N" not in t_out and "commands from your reference" not in t_out
      and "last health check" not in t_out and "model Qwen" not in t_out and 'Try: "show me the vlans"' in t_out, t_out[-500:])
check("banner: plain ASCII within 80 columns", all(ord(c) < 128 for c in t_out) and max(len(l) for l in banner_lines) <= 80,
      max(len(l) for l in banner_lines))
check("banner: not shown with -q", "Dial 0 for SONiC - talk" not in tty_run(["-q"], b"/exit\n"))
_env_saved = dict(env); env["DIAL0_BANNER"] = "off"
check("banner: not shown with BANNER=off", "Dial 0 for SONiC - talk" not in tty_run([], b"/exit\n"))
env.clear(); env.update(_env_saved)
check("banner: not shown when commands are piped in (scripts)", "Dial 0 for SONiC - talk" not in cli_in("/exit\n").stdout)
t_out = tty_run(["-s", "lab1"], b"/exit\n")
check("named session prompt shows its name: 'dial0 [lab1]> '", "dial0 [lab1]> " in t_out, t_out)
out = cli("-h")
check("dial0 -h shows help (starts with the prompt usage)", "dial0                      the dial0> prompt" in out.stdout, out.stdout[:200])


# workflows through the CLI
out = cli("workflows")
check("cli: dial0 workflows lists them with schedule, retention and last result", all(n in out.stdout for n in
      ("health", "logs", "interfaces", "bgp", "services", "resources")) and "keeps 10" in out.stdout and "last:" in out.stdout, out.stdout)
out = cli("workflow", "schedule", "bgp", "every", "2h", "--keep", "5")
check("cli: schedule with --keep", "'bgp' scheduled every 2h" in out.stdout and "keeping the last 5 results" in out.stdout, out.stdout)
out = cli("workflow", "schedule", "services", "daily", "06:00")
check("cli: schedule daily, default retention when not asked (script)", "scheduled daily 06:00" in out.stdout
      and "keeping the last 10 results" in out.stdout, out.stdout)
out = cli("workflow", "schedule", "bgp", "every", "1m")
check("cli: too-short interval explained", "the shortest interval is 5m" in out.stdout, out.stdout)
out = cli("workflows")
check("cli: schedules shown in the list", "every 2h" in out.stdout and "daily 06:00" in out.stdout, out.stdout)
out = cli("workflow", "run", "resources")
check("cli: workflow run prints the report", out.stdout.startswith("Resources:"), out.stdout)
out = cli("workflow", "results", "logs")
check("cli: results listed newest first", out.stdout.lstrip().startswith("1.") and "Full report: dial0 workflow show logs" in out.stdout, out.stdout)
out = cli("workflow", "show", "logs", "1")
check("cli: show the full report", "System log:" in out.stdout, out.stdout)
out = cli("workflow", "keep", "logs", "7")
check("cli: change retention", "keeping the last 7 results" in out.stdout, out.stdout)
t_out = tty_run(["workflow", "schedule", "logs", "every", "1h"], b"25\n")
check("cli: scheduling from a terminal without --keep asks how many results to keep",
      "How many results of 'logs' should be kept on the switch? [10]" in t_out and "keeping the last 25 results" in t_out, t_out)
out = cli("workflow", "unschedule", "bgp")
check("cli: unschedule", "'bgp' unscheduled" in out.stdout, out.stdout)
out = cli("workflow", "run", "nope")
check("cli: unknown workflow explained", "no workflow 'nope'" in out.stdout, out.stdout)
out = cli("cve", "list")
check("cli: dial0 cve list shows the full table (known exploited marked)", "CVE-2024-6387" in out.stdout and "KEV high" in out.stdout
      and "1:9.2p1-2+deb12u3" in out.stdout and "host, bgp" in out.stdout, out.stdout)
out = cli("cve", "update")
check("cli: dial0 cve update refreshes the data", "tracker.json updated" in out.stdout, out.stdout)
# blueprints through the CLI (the in-process server runs the fake switch's commands)
_bbin, _op, _opx = _bp_env
os.environ["PATH"] = _bbin + ":" + _op; tools.PREFIX = []
_scen(""); _bdb(); open(os.path.join(_bbin, "frr.conf"), "w").write("")
import shutil as _sh
_sh.rmtree(bp.SAVED, ignore_errors=True)
out = cli("blueprints")
check("cli: dial0 blueprints lists them", "2-tier-clos-ai-fabric-scale-out" in out.stdout and "Apply: dial0 blueprint apply NAME" in out.stdout, out.stdout)
out = cli("blueprint", "show", bp.CLOS)
check("cli: show without saved answers says how to start", "no saved answers" in out.stdout, out.stdout)
answers = "leaf\n10.0.0.11\n65001\nEthernet0-Ethernet4\nEthernet256,Ethernet260\n100\n10.10.1.0/24\n10.10.1.1/24\ny\n"
out = cli_in(answers, "blueprint", "apply", bp.CLOS)
o = out.stdout
check("cli: asks the questions for the role, re-asks only a bad answer", "Role of this switch (spine/leaf)" in o and "Uplinks: ports to the spine switches" in o
      and "gateway: must be a host address in the subnet" in o and o.count("VLAN gateway IP with prefix") == 2, o[:1500])
check("cli: shows the FULL configuration before asking", "Configuration for this switch (leaf):" in o and "config vlan member add -u 100 Ethernet4" in o
      and '-c "neighbor Ethernet256 interface peer-group SPINE"' in o and "config save -y" in o
      and o.index("Configuration for this switch") < o.index("[y/N]"), o[-2500:])
check("cli: applied, verified, backup and how to go back", "Done: " in o and "2 of 2 BGP neighbors established" in o
      and "Backup: " in o and "config reload -y" in o, o[-900:])
out = cli_in("\n\n\n\n\n\n\nn\n", "blueprint", "apply", bp.CLOS)
check("cli: saved answers are the defaults; 'n' applies nothing", "[leaf]" in out.stdout and "[65001]" in out.stdout
      and "Nothing applied." in out.stdout, out.stdout[-600:])
out = cli("blueprint", "show", bp.CLOS)
check("cli: show = the configuration from the saved answers, nothing applied", "Configuration for this switch (leaf)" in out.stdout
      and "[y/N]" not in out.stdout, out.stdout[:400])
out = cli_in("y\n", "blueprint", "apply", bp.CLOS, "role=spine", "loopback=10.0.0.1", "asn=65000", "downlinks=Ethernet0,Ethernet4")
check("cli: answers can be given as key=value (scripts)", "Configuration for this switch (spine)" in out.stdout
      and "Role of this switch" not in out.stdout and "Done: " in out.stdout, out.stdout[-800:])
_bdb(VLAN_MEMBER={"Vlan200|Ethernet4": {}})
out = cli_in("", "blueprint", "apply", bp.CLOS, *[f"{k}={v}" for k, v in LEAF.items()])
check("cli: conflicts listed, nothing applied, nothing removed", "CONFLICTS with this switch's configuration (nothing will be removed):" in out.stdout
      and "Ethernet4 (server port) is in Vlan200" in out.stdout and "Nothing applied. Resolve these" in out.stdout
      and "[y/N]" not in out.stdout, out.stdout[-700:])
_bdb()
os.environ["PATH"] = _op; tools.PREFIX = _opx
out = cli("rm", "e2e")
check("cli: rm", "deleted" in out.stdout)
# one command for everything: dial0 reset --all
def _seed():
    for nm in ("ra1", "ra2", "interactive"):
        x = sessions.get(nm); x.add("user", "something"); x.record("show vlan brief", "executed")
    resolve.remember("list vlans please", ["show vlan brief"])
    open(os.path.join(os.environ["DIAL0_STATE_DIR"], "repl_history"), "w").write("show vlan brief\n")
_seed()
out = cli("reset", "--all")
check("reset --all from a script without -y: refused, nothing cleared", "Add -y to confirm" in out.stdout
      and len(sessions.list_all()) >= 3 and resolve.list_plans(), out.stdout)
out = cli("reset", "--all", "-y")
state = os.environ["DIAL0_STATE_DIR"]
check("reset --all -y: every session (context + history), learned requests and prompt history cleared",
      out.stdout.startswith("Cleared: ") and sessions.list_all() == [] and resolve.list_plans() == []
      and not os.path.exists(os.path.join(state, "repl_history")), (out.stdout, sessions.list_all()))
check("reset --all: reports what it cleared", "session(s) with their context and command history" in out.stdout
      and "learned request(s)" in out.stdout and "prompt's line history" in out.stdout, out.stdout)
_seed()
out = cli_in("/reset all\nn\n/exit\n")
check("/reset all at the prompt asks first; 'n' keeps everything", "Nothing cleared." in out.stdout and len(sessions.list_all()) >= 3, out.stdout)
out = cli_in("/reset all\ny\n/sessions\n/exit\n")
check("/reset all at the prompt: 'y' clears everything", "Cleared: " in out.stdout and resolve.list_plans() == []
      and sessions.list_all() == [], (out.stdout, sessions.list_all()))
open(os.path.join(state, "switch_commands.json"), "w").write("{}")
cli("reset", "--all", "-y")
check("caches (e.g. the switch's command index) are not touched by reset --all",
      os.path.exists(os.path.join(state, "switch_commands.json")))


httpd.shutdown()

# ------------------------------------------------------------------ 6b. llama-server finds its libraries from any directory
rd = lambda f: open(os.path.join(ROOT, f)).read()
for f in ("docker/base-prebuilt.Dockerfile", "docker/base-compile.Dockerfile"):
    check(f"{f}: LD_LIBRARY_PATH=/app and check run from another directory",
          "LD_LIBRARY_PATH=/app" in rd(f) and "cd / && llama-server --version" in rd(f))
check("base-compile copies the shared libraries too", "find build -name '*.so*'" in rd("docker/base-compile.Dockerfile")
      and "COPY --from=llama-build /out/ /app/" in rd("docker/base-compile.Dockerfile"))
check("app image fails the build if llama-server misses libraries (runtime conditions)",
      "grep 'not found'" in rd("Dockerfile") and rd("Dockerfile").index("grep 'not found'") > rd("Dockerfile").index("WORKDIR /opt/dial0"))
check("entrypoint sets the library path and reports missing libraries clearly",
      'export LD_LIBRARY_PATH="/app' in rd("entrypoint.sh") and "can't find its libraries" in rd("entrypoint.sh"))
import shutil as _sh
if _sh.which("gcc"):
    d = tempfile.mkdtemp(prefix="dial0-ld-")
    app, other = os.path.join(d, "app"), os.path.join(d, "other")
    os.makedirs(app); os.makedirs(other)
    open(os.path.join(app, "impl.c"), "w").write("int f(void){return 7;}\n")
    open(os.path.join(app, "main.c"), "w").write('#include <stdio.h>\nint f(void);\nint main(void){printf("ok\\n");return f()-7;}\n')
    subprocess.run("gcc -shared -fPIC -o libllama-server-impl.so impl.c && gcc -o llama-server main.c -L. -lllama-server-impl -Wl,-rpath,.",
                   shell=True, cwd=app, check=True)
    bin_ = os.path.join(app, "llama-server")
    r1 = subprocess.run([bin_], cwd=other, capture_output=True, text=True)
    r2 = subprocess.run([bin_], cwd=other, capture_output=True, text=True, env=dict(os.environ, LD_LIBRARY_PATH=app))
    check("repro: a cwd-relative library path breaks outside /app (the reported error)", r1.returncode != 0 and "libllama-server-impl.so" in r1.stderr, r1.stderr)
    check("repro: LD_LIBRARY_PATH=/app fixes it", r2.returncode == 0 and r2.stdout.strip() == "ok", (r2.stdout, r2.stderr))
else:
    print("SKIP library-path reproduction (no gcc)")

# ------------------------------------------------------------------ 7. python 3.10 compatibility (prebuilt base image)
r = subprocess.run([sys.executable, os.path.join(ROOT, "tests", "py310_check.py")], capture_output=True, text=True)
check("code runs on python 3.10+ (" + r.stdout.strip().splitlines()[-1] + ")", r.returncode == 0, r.stdout)

# ------------------------------------------------------------------ 8. host script (install / set / resources)
r = subprocess.run(["bash", os.path.join(ROOT, "tests", "test_host.sh")], capture_output=True, text=True)
for line in r.stdout.splitlines():
    if line.startswith("FAIL"):
        print(line)
check("host script tests (" + (r.stdout.strip().splitlines() or ["?"])[-1] + ")", r.returncode == 0, r.stdout[-500:] + r.stderr[-300:])

print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
