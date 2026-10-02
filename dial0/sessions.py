"""Named sessions: conversation context + executed-command log, persisted per session.

/var/lib/dial0/sessions/<name>.json
  messages : chat turns (what the model sees; only the tail fits the prompt window)
  log      : every command the agent ran / was denied (audit + continuity, always kept)
  pending  : a state-changing command waiting for y/N (survives CLI disconnects and restarts)
"""
import json, os, re, threading, time

STATE_DIR = os.getenv("DIAL0_STATE_DIR", "/var/lib/dial0")
DIR = os.path.join(STATE_DIR, "sessions")
LEGACY = os.path.join(STATE_DIR, "memory.json")  # pre-session single context, migrated into "default"
CHAR_BUDGET = int(os.getenv("DIAL0_CTX_CHARS", "9000"))  # ~2.5k tokens of a 4k window
MAX_MSGS, MAX_LOG = 200, 500
STATE_MAX_MB = float(os.getenv("DIAL0_STATE_MAX_MB", "200"))  # disk cap for all sessions (oldest dropped first)
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
PROMPT_SESSION = "interactive"               # internal name of the plain `dial0` prompt's session (never shown)
UNNAMED = {"default", PROMPT_SESSION}        # sessions the user didn't name: their names aren't shown
_lock = threading.Lock()
_cache: dict[str, "Session"] = {}


def valid(name: str) -> bool:
    return bool(NAME_RE.match(name or ""))


class Session:
    """One conversation: messages (the model's context), a log of what ran, the pending approval, and the last
    request's steps. Stored as JSON in the state directory."""
    def __init__(self, name: str):
        self.name = name
        self.path = os.path.join(DIR, name + ".json")
        self.messages: list[dict] = []
        self.log: list[dict] = []
        self.pending: dict | None = None
        self.created = self.updated = time.time()
        self.ref = ""     # CLI-reference snippet for the current request (not persisted)
        self.recent = ""  # recent-commands block, frozen per request so the prompt prefix stays cacheable
        self.last_trace: list = []  # steps of the last request (dial0 why)
        self._load()

    def _load(self):
        src = self.path
        if not os.path.exists(src) and self.name == "default" and os.path.exists(LEGACY):
            src = LEGACY
        try:
            with open(src) as f:
                d = json.load(f)
        except Exception:
            return
        if isinstance(d, list):  # legacy format: bare message list
            self.messages = d
        else:
            self.messages = d.get("messages", [])
            self.log = d.get("log", [])
            self.pending = d.get("pending")
            if self.pending and "commands" not in self.pending and "argv" in self.pending:  # older format
                self.pending = {"id": self.pending["id"], "mode": "agent",
                                "commands": [{"argv": self.pending["argv"], "command": self.pending["command"]}]}
            self.created = d.get("created", self.created)
            self.last_trace = d.get("last_trace", [])
            self.updated = d.get("updated", self.updated)

    def save(self):
        """Write the session to disk atomically (temp file + rename), keeping the newest messages and log entries,
        then enforce the size cap on all sessions together (oldest sessions go first)."""
        self.updated = time.time()
        os.makedirs(DIR, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"messages": self.messages[-MAX_MSGS:], "log": self.log[-MAX_LOG:], "pending": self.pending,
                       "last_trace": self.last_trace[-60:], "created": self.created, "updated": self.updated}, f)
        os.replace(tmp, self.path)  # atomic
        _enforce_cap(keep=self.path)

    def add(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})
        self.messages = self.messages[-MAX_MSGS:]
        self.save()

    def record(self, command: str, status: str, detail: str = ""):
        self.log.append({"ts": int(time.time()), "command": command, "status": status, "detail": detail[:200]})
        self.log = self.log[-MAX_LOG:]
        self.save()

    def window(self, system: str, context: bool = True) -> list[dict]:
        """System prompt + as much recent conversation as fits CHAR_BUDGET.

        context=False: only the current request and its own steps (nothing from earlier requests).
        Always keeps the newest message, and always keeps the user's current request
        (re-inserted if it was trimmed away), so the model never loses what was asked.
        """
        msgs = list(self.messages)
        if not context:
            start = max((i for i, m in enumerate(msgs)
                         if m["role"] == "user" and not m["content"].startswith("OBSERVATION:")), default=0)
            msgs = msgs[start:]
        while len(msgs) > 1 and sum(len(m["content"]) for m in msgs) + len(system) > CHAR_BUDGET:
            msgs.pop(0)
        while len(msgs) > 1 and msgs[0]["role"] != "user":
            msgs.pop(0)
        req = self.last_request()
        if req and not any(m["role"] == "user" and m["content"] == req for m in msgs):
            msgs.insert(0, {"role": "user", "content": "(earlier context trimmed) Current request: " + req[:1500]})
        return [{"role": "system", "content": system}] + msgs

    def recent_commands(self, n: int = 5) -> list[dict]:
        return [e for e in self.log if e["status"] in ("executed", "failed")][-n:]

    def last_request(self) -> str:
        for m in reversed(self.messages):
            if m["role"] == "user" and not m["content"].startswith("OBSERVATION:"):
                return m["content"]
        return ""

    def previous_request(self) -> str:
        """The real request before the current one ('' if none)."""
        reqs = [m["content"] for m in self.messages
                if m["role"] == "user" and not m["content"].startswith("OBSERVATION:")]
        return reqs[-2] if len(reqs) >= 2 else ""

    def reset(self):
        """Clear the conversation; the command log is kept as an audit trail (use delete() to drop it)."""
        self.messages, self.pending = [], None
        self.save()

    def summary(self) -> dict:
        return {"name": self.name, "updated": int(self.updated), "messages": len(self.messages),
                "commands": len([e for e in self.log if e["status"] in ("executed", "failed")]),
                "pending": bool(self.pending)}


def _enforce_cap(keep: str):
    """Keep the sessions directory under DIAL0_STATE_MAX_MB by deleting the least recently used sessions."""
    try:
        files = [(e.stat().st_mtime, e.stat().st_size, e.path) for e in os.scandir(DIR) if e.name.endswith(".json")]
    except FileNotFoundError:
        return
    total, cap = sum(f[1] for f in files), STATE_MAX_MB * 1024 * 1024
    for _, size, path in sorted(files):
        if total <= cap:
            break
        if path == keep:
            continue
        try:
            os.remove(path)
            total -= size
            _cache.pop(os.path.basename(path)[:-5], None)
        except OSError:
            pass


def get(name: str) -> Session:
    if not valid(name):
        raise ValueError("session name must match [A-Za-z0-9._-]{1,40}")
    with _lock:
        if name not in _cache:
            _cache[name] = Session(name)
        return _cache[name]


def peek(name: str) -> Session:
    """Read-only lookup: never creates or lists a session that does not exist yet."""
    if not valid(name):
        raise ValueError("session name must match [A-Za-z0-9._-]{1,40}")
    exists = name in _cache or os.path.exists(os.path.join(DIR, name + ".json")) or (name == "default" and os.path.exists(LEGACY))
    return get(name) if exists else Session(name)


def list_all() -> list[dict]:
    """Every session with its size and last use, newest first."""
    names = set()
    try:
        names |= {f[:-5] for f in os.listdir(DIR) if f.endswith(".json") and valid(f[:-5])}
    except FileNotFoundError:
        pass
    if os.path.exists(LEGACY):
        names.add("default")
    out = [get(n).summary() for n in names]
    return sorted(out, key=lambda s: -s["updated"])


def delete_all() -> int:
    """Delete every session (context, command history, pending approvals, last steps). -> how many."""
    n = 0
    with _lock:
        _cache.clear()
        for path in [os.path.join(DIR, f) for f in (os.listdir(DIR) if os.path.isdir(DIR) else [])
                     if f.endswith(".json")] + [LEGACY]:
            try:
                os.remove(path); n += path != LEGACY
            except OSError:
                pass
    return n


def delete(name: str) -> bool:
    """Delete one session (context, history, pending approval). -> True if it existed."""
    if not valid(name):
        return False
    with _lock:
        _cache.pop(name, None)
    try:
        os.remove(os.path.join(DIR, name + ".json"))
        return True
    except FileNotFoundError:
        return False
