"""CLI/API front door (stdlib only)."""
import json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from .agent import Agent
from . import clidoc, sessions, resolve, workflows, cve, blueprints

HOST = os.getenv("DIAL0_API_HOST", "127.0.0.1")
PORT = int(os.getenv("DIAL0_API_PORT", "8090"))
TOKEN = os.getenv("DIAL0_API_TOKEN", "")
CONTEXT_DEFAULT = os.getenv("DIAL0_CONTEXT", "off") == "on"
agent: Agent | None = None


class H(BaseHTTPRequestHandler):
    """The agent's HTTP API on 127.0.0.1: requests, approvals, live progress, sessions, reference lookups,
    workflows, CVE data and blueprints. Optional bearer token (DIAL0_API_TOKEN)."""
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _authed(self) -> bool:
        return not TOKEN or self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def do_GET(self):
        """Read-only endpoints: health, progress, sessions, plans, reference, workflows, CVE list, blueprints."""
        u = urlparse(self.path)
        if u.path == "/health":
            from . import agent as _agent
            return self._send(200, {"ok": True, "cli_index": clidoc.status(), "agent": {
                "confirm": _agent.CONFIRM, "fix_rounds": _agent.FIX_ROUNDS,
                "model": os.path.basename(os.getenv("MODEL_PATH", "")).replace(".gguf", ""),
                "commands": len(clidoc._curated) if clidoc.curated() else clidoc._meta.get("commands", 0),
                "from_reference_file": clidoc.curated()}, "health": (workflows.results("health") or [None])[0]})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        try:
            if u.path == "/progress":  # live steps of a running request (polled by the CLI)
                q = parse_qs(u.query)
                steps = agent.progress.get(q.get("rid", [""])[0], [])
                since = int(q.get("since", ["0"])[0])
                return self._send(200, {"steps": steps[since:], "next": len(steps)})
            if u.path == "/ref":
                return self._send(200, {"text": clidoc.ref(parse_qs(u.query).get("q", [""])[0])})
            if u.path == "/workflows":
                return self._send(200, {"workflows": workflows.overview()})
            if u.path == "/blueprints":
                return self._send(200, {"blueprints": blueprints.catalogue()})
            if u.path in ("/blueprints/questions", "/blueprints/saved"):
                q = parse_qs(u.query)
                nm = q.get("name", [""])[0]
                try:
                    if u.path.endswith("saved"):
                        blueprints._bp(nm)
                        return self._send(200, {"saved": blueprints.saved(nm)})
                    return self._send(200, {"questions": blueprints.questions(nm, q.get("role", [""])[0])})
                except blueprints.BlueprintError as e:
                    return self._send(400, {"error": str(e)})
            if u.path == "/cve/list":
                r = workflows.result("cve", 1)
                return self._send(200, {"items": (r or {}).get("items", []), "ts": (r or {}).get("ts")})
            if u.path == "/workflows/results":
                q = parse_qs(u.query)
                return self._send(200, {"results": workflows.results(q.get("name", [""])[0])})
            if u.path == "/workflows/result":
                q = parse_qs(u.query)
                return self._send(200, {"result": workflows.result(q.get("name", [""])[0], int(q.get("n", ["1"])[0]))})
            if u.path == "/plans":
                return self._send(200, {"plans": resolve.list_plans()})
            if u.path == "/sessions":
                return self._send(200, {"sessions": sessions.list_all()})
            if u.path == "/session":
                s = sessions.peek(parse_qs(u.query).get("name", ["default"])[0])
                chat = [m for m in s.messages]
                pend = None
                if s.pending:
                    pend = {"id": s.pending["id"], "commands": [c["command"] for c in s.pending["commands"]]}
                return self._send(200, {**s.summary(), "messages": chat[-40:], "history": s.log[-50:], "pending": pend,
                                        "last_trace": s.last_trace})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        self._send(404, {})

    def do_POST(self):
        """Endpoints that act: query, confirm, reset, workflows, CVE update, blueprint plan/apply."""
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            name = body.get("session", "default")
            if self.path == "/query":
                ctx = body.get("context")
                return self._send(200, agent.query(body["query"], name, body.get("mode", "fast"), bool(body.get("explain")),
                                                   str(body.get("rid", "")), CONTEXT_DEFAULT if ctx is None else bool(ctx)))
            if self.path == "/confirm":
                return self._send(200, agent.confirm(name, body["id"], bool(body["approve"]), str(body.get("rid", ""))))
            if self.path == "/reset":
                sessions.get(name).reset()
                return self._send(200, {"ok": True})
            if self.path in ("/blueprints/plan", "/blueprints/apply"):
                try:
                    if self.path.endswith("plan"):
                        return self._send(200, blueprints.plan(body.get("name", ""), body.get("params") or {}))
                    with agent.lock:  # never at the same time as a request
                        agent._begin(str(body.get("rid", "")))
                        r = blueprints.apply(body.get("name", ""), body.get("params") or {}, body.get("plan_id", ""),
                                             step=agent._step)
                        r["steps"] = list(agent._live)
                    return self._send(200, r)
                except blueprints.BlueprintError as e:
                    return self._send(400, {"error": str(e)})
            if self.path == "/cve/update":
                return self._send(200, {"status": cve.refresh(force=True)})
            if self.path.startswith("/workflows/"):
                try:
                    act, name = self.path.rsplit("/", 1)[1], body.get("name", "")
                    if act == "run":
                        with agent.lock:  # not at the same time as a request
                            r = workflows.run(name, trigger="manual",
                                              analyse=agent.analyse_workflow if workflows.INSIGHTS else None)
                        return self._send(200, {"result": r})
                    if act == "schedule":
                        sch = workflows.schedule(name, body.get("every", ""), body.get("at", ""), body.get("keep"))
                        return self._send(200, {"schedule": workflows.describe(sch), "keep": workflows.keep_of(name),
                                                "next": workflows.next_due(sch)})
                    if act == "unschedule":
                        return self._send(200, {"removed": workflows.unschedule(name)})
                    if act == "keep":
                        return self._send(200, {"keep": workflows.set_keep(name, int(body.get("keep", 0)))})
                    if act == "clear":
                        return self._send(200, {"cleared": workflows.clear(name)})
                except workflows.WorkflowError as e:
                    return self._send(400, {"error": str(e)})
                return self._send(404, {})
            if self.path == "/reset_all":  # every session + learned requests + the prompt's line history
                with agent.lock:  # not while a request is running
                    n = sessions.delete_all()
                    plans = len(resolve.list_plans())
                    resolve.clear_plans()
                    hist = os.path.join(os.getenv("DIAL0_STATE_DIR", "/var/lib/dial0"), "repl_history")
                    had_hist = os.path.exists(hist)
                    if had_hist:
                        os.remove(hist)
                    agent.progress.clear()
                return self._send(200, {"sessions": n, "plans": plans, "line_history": had_hist})
            if self.path == "/plans/clear":
                resolve.clear_plans()
                return self._send(200, {"ok": True})
            if self.path == "/session/delete":
                return self._send(200, {"deleted": sessions.delete(body["name"])})
            self._send(404, {})
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(500, {"error": str(e)})

    def log_message(self, *a):
        pass


def serve():
    global agent
    agent = Agent()
    workflows.start_scheduler(guard=agent.lock,  # scheduled workflows never overlap a request
                              analyse=agent.analyse_workflow if workflows.INSIGHTS else None)
    threading.Thread(target=agent.warm_up, daemon=True).start()
    print(f"dial0 API on {HOST}:{PORT} (confirm mode: {os.getenv('DIAL0_CONFIRM', 'ask')})", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
