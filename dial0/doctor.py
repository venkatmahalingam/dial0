"""dial0 ctl doctor: checks that Dial 0 can run SONiC commands exactly the way requests run them.
Harmless: only `id`, `env`, `show version` and `--help` are executed on the switch."""
import json, os, sys, urllib.request
from . import tools

PROBLEMS = []


def line(ok, msg, detail=""):
    print(f"  [{'ok' if ok else 'FAIL'}]  {msg}")
    if not ok:
        PROBLEMS.append(msg)
        for d in detail.strip().splitlines()[-6:]:
            print(f"          | {d}")


def run(argv):
    code, body = tools.split_result(tools._run(argv, max_out=4000))
    return code, body


def main():
    """Run the checks and print [ok]/[FAIL] lines; exit code 1 if anything failed."""
    print("Dial 0 doctor: running harmless commands on the switch the same way requests do\n")
    code, body = run(["id", "-u"])
    line(code == 0 and body.strip() == "0", "commands run on the switch as root", body)

    if tools.HOST_EXEC == "nsenter":
        code, body = run(["env"])
        leaked = [k for k in ("PYTHONPATH", "LD_LIBRARY_PATH", "VIRTUAL_ENV", "PYTHONHOME", "DIAL0_API_PORT")
                  if any(l.startswith(k + "=") for l in body.splitlines())]
        line(code == 0 and not leaked, "SONiC commands get the switch's clean environment"
             + (f" (leaked: {', '.join(leaked)})" if leaked else ""), body)
    else:
        print("  [skip]  environment check (not running against a switch)")

    for argv, what in ((["show", "version"], "show commands run"),
                       (["config", "--help"], "config runs"),
                       (["config", "vlan", "add", "--help"], "config subcommands run (config vlan add --help)")):
        code, body = run(argv)
        line(code == 0, f"{what}: {' '.join(argv)}" + ("" if code == 0 else f" -> exit {code}"), body)

    port = os.getenv("DIAL0_API_PORT", "8090")
    try:
        h = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5))
        idx = h.get("cli_index", {})
        line(True, f"agent answering; command reference: {idx.get('source', '?')}, {idx.get('source_commands', '?')} commands")
    except Exception as e:
        line(False, "agent API answering", str(e))
    llama = os.getenv("LLAMA_URL") or f"http://127.0.0.1:{os.getenv('LLAMA_PORT', '18081')}"
    try:
        urllib.request.urlopen(llama + "/health", timeout=5)
        line(True, "model engine (llama.cpp) answering")
    except Exception as e:
        line(False, "model engine (llama.cpp) answering (still loading?)", str(e))

    print("\n" + ("All good." if not PROBLEMS else f"{len(PROBLEMS)} problem(s). The lines after [FAIL] show the switch's own output."))
    return 1 if PROBLEMS else 0


if __name__ == "__main__":
    sys.exit(main())
