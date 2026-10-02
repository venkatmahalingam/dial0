"""Native tools: click command executor + grep. Runs on the SONiC host via nsenter."""
import os, posixpath, shlex, subprocess

HOST_EXEC = os.getenv("DIAL0_HOST_EXEC", "nsenter")  # nsenter | local
# SONiC's CLI must run with the switch's own clean environment, never this container's (PATH with /opt/venv,
# PYTHONPATH=/opt/dial0, LD_LIBRARY_PATH=/app...): `config`/`show` are Python and would otherwise inherit them.
HOST_ENV = ["env", "-i", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "HOME=/root",
            "USER=root", "LOGNAME=root", "LANG=C.UTF-8", "LC_ALL=C.UTF-8", "TERM=dumb"]
PREFIX = (["nsenter", "-t", "1", "-m", "-u", "-i", "-n", "-p", "--"] + HOST_ENV) if HOST_EXEC == "nsenter" else []
ALLOWED = set(os.getenv("DIAL0_ALLOWED_CMDS", "config,show,sonic-db-cli,sonic-cfggen,vtysh,ip").split(","))
GREP_ROOTS = tuple(os.getenv("DIAL0_GREP_ROOTS", "/etc/sonic,/var/log,/usr/share/sonic").split(","))
DENY_SUB = set(os.getenv("DIAL0_DENY_SUBCMDS", "reload,load_minigraph,reboot,warm_restart_reboot").split(","))
MAX_OUT = int(os.getenv("DIAL0_MAX_OUT", "2000"))
TIMEOUT = int(os.getenv("DIAL0_CMD_TIMEOUT", "30"))

DB_READ_OPS = {"GET", "HGET", "HGETALL", "HKEYS", "HVALS", "KEYS", "SCAN", "EXISTS", "TYPE", "TTL",
               "LRANGE", "SMEMBERS", "ZRANGE"}
IP_WRITE_WORDS = {"add", "del", "delete", "set", "flush", "replace", "change"}


class ToolError(Exception):
    pass


def parse_click(cmd: str) -> list[str]:
    """Split a command line into argv (shell-style quoting). Only the allowed tools are accepted (DIAL0_ALLOWED_CMDS)
    and blocked subcommands (reload, reboot...) are refused. Commands run without a shell, so pipes, redirects and
    substitutions are never interpreted."""
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        raise ToolError(f"bad command syntax: {e}")
    if not argv:
        raise ToolError("empty command")
    if argv[0] == "sudo":
        argv = argv[1:]
    if not argv or argv[0] not in ALLOWED:
        raise ToolError(f"command not allowed. allowed: {sorted(ALLOWED)}")
    if len(argv) > 1 and argv[1] in DENY_SUB:
        raise ToolError(f"'{argv[0]} {argv[1]}' is blocked by policy")
    return argv


def is_mutating(argv: list[str]) -> bool:
    """True if a command can change the switch. Read-only: show, --help, read operations of sonic-db-cli, ip
    without write words, vtysh with only `show` lines, sonic-cfggen without -w. Everything else (config...) counts
    as a change and needs approval."""
    c = argv[0]
    if "--help" in argv:
        return False
    if c == "show":
        return False
    if c == "sonic-db-cli":
        rest = [a for a in argv[1:] if not a.startswith("-") and not a.endswith("_DB")]
        return not (rest and rest[0].upper() in DB_READ_OPS)
    if c == "ip":
        return any(a in IP_WRITE_WORDS for a in argv[1:])
    if c == "vtysh":
        cmds = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "-c"]
        return not cmds or any(not x.strip().startswith("show") for x in cmds)
    if c == "sonic-cfggen":
        return "-w" in argv or "--write-to-db" in argv
    return True  # config, anything else


def _run(argv: list[str], max_out: int = MAX_OUT) -> str:
    try:
        p = subprocess.run(PREFIX + argv, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return f"[timeout after {TIMEOUT}s]"
    except FileNotFoundError as e:
        return f"[exec error: {e}]"
    out = (p.stdout + p.stderr).strip() or "(no output)"
    if len(out) > max_out:
        out = out[:max_out] + f"\n...[truncated {len(out) - max_out} chars]"
    return f"[exit {p.returncode}]\n{out}"


def run_click(argv: list[str], max_out: int = MAX_OUT) -> str:
    """Runs one allow-listed command on the host. Returns '[exit N]\n<output>'."""
    return _run(argv, max_out)


def split_result(out: str) -> tuple[int, str]:
    head, _, body = out.partition("\n")
    try:
        return int(head.strip("[]").split()[1]), body
    except (IndexError, ValueError):
        return -1, out


def run_grep(spec: str) -> str:
    """spec: '<pattern> <path>'"""
    try:
        parts = shlex.split(spec)
    except ValueError as e:
        raise ToolError(f"bad grep syntax: {e}")
    if len(parts) != 2:
        raise ToolError("grep input must be: <pattern> <path>")
    pattern, path = parts
    path = posixpath.normpath(path)
    if ".." in path.split("/") or not any(path == r or path.startswith(r + "/") for r in GREP_ROOTS):
        raise ToolError(f"path must be under {list(GREP_ROOTS)}")
    return _run(["grep", "-rnIiE", "-m", "50", "--", pattern, path])


def run_raw(argv: list[str], timeout: int = 20) -> str:
    """Unfiltered, low-priority host exec used by the CLI-reference crawler."""
    try:
        p = subprocess.run(PREFIX + ["nice", "-n", "19"] + argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return p.stdout + p.stderr
    except Exception:
        return ""


def run_batch(argvs: list[list[str]], mutating: list[bool], max_out: int = MAX_OUT) -> list[dict]:
    """Run a whole approved batch in ONE host invocation (one shell, one nsenter), in order.

    If a command that changes config fails, the rest is skipped (read-only failures don't stop the batch).
    Returns one dict per command: {"exit": int | None, "output": str, "ran": bool}. exit None = skipped.
    Note: SONiC CLI changes are not transactional; commands that already succeeded stay applied.
    """
    if not argvs:
        return []
    lines = ['r() { i=$1; shift; printf "\\n@@D0B %s@@\\n" "$i"; "$@" </dev/null 2>&1; c=$?; printf "\\n@@D0E %s %s@@\\n" "$i" "$c"; return $c; }']
    for i, (argv, mut) in enumerate(zip(argvs, mutating)):
        lines.append(f"r {i} {shlex.join(argv)}" + (" || exit 0" if mut else " || true"))
    script = "\n".join(lines) + "\n"
    try:
        p = subprocess.run(PREFIX + ["bash", "-c", script], capture_output=True, text=True,
                           timeout=TIMEOUT * len(argvs), stdin=subprocess.DEVNULL)
        raw = p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        raw = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        raw += f"\n[batch timeout after {TIMEOUT * len(argvs)}s]"
    except FileNotFoundError as e:
        return [{"exit": -1, "output": f"[exec error: {e}]", "ran": True}] + \
               [{"exit": None, "output": "", "ran": False} for _ in argvs[1:]]
    results = [{"exit": None, "output": "", "ran": False} for _ in argvs]
    cur, buf = None, []
    for line in raw.split("\n"):
        if line.startswith("@@D0B ") and line.endswith("@@"):
            cur, buf = int(line[6:-2]), []
        elif line.startswith("@@D0E ") and line.endswith("@@") and cur is not None:
            i, code = line[6:-2].split()
            out = "\n".join(buf).strip("\n") or "(no output)"
            if len(out) > max_out:
                out = out[:max_out] + f"\n...[truncated {len(out) - max_out} chars]"
            results[int(i)] = {"exit": int(code), "output": out, "ran": True}
            cur = None
        elif cur is not None:
            buf.append(line)
    if cur is not None:  # started but never finished (timeout)
        results[cur] = {"exit": -1, "output": "\n".join(buf).strip() + "\n[did not finish]", "ran": True}
    return results
