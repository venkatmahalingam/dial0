# Dial 0 architecture

![Dial 0 architecture](architecture.png)

Dial 0 is a small local model plus an **agent harness**. The model's job is narrow: turn a plain-English request
into commands, and explain findings. Everything that must be exact is done by code: deciding whether the model is
needed at all, checking every command, running it, recovering from errors, and remembering context.

Everything runs on the switch, in one persistent Docker container:

| Process | What it is |
|---|---|
| `llama-server` (llama.cpp) | the local model (Qwen3.5-4B GGUF, mounted read-only from the switch's disk), CPU only |
| `python -m dial0 serve` | the agent harness and its local HTTP API (`dial0/server.py`), on 127.0.0.1 |
| `dial0` (host) → `docker exec` → `dial0/cli.py` | the operator's command and the `dial0>` prompt |

SONiC commands run **on the host**, not in the container: `nsenter` into the host's namespaces with a clean
environment (`dial0/tools.py`), so SONiC's CLI behaves exactly as when the operator types it.

## A request, step by step

`Agent.query()` in `dial0/agent.py`:

1. **Route without the model when possible** (`dial0/resolve.py`):
   - a typed SONiC command that exists → runs as typed;
   - a status question ("is Ethernet4 up?") → answered from the live table;
   - a "which command does..." question → answered from the reference;
   - a plain read-only request matched to a show command;
   - a learned plan (the same kind of request worked before);
   - health, security, CVE and blueprint questions → their workflows.
2. **Plan** (one model call): the model sees the complete command reference (fixed, so llama.cpp keeps it cached),
   a short ranked list of the most relevant commands, and, with context on, the conversation so far. It answers in a
   fixed JSON shape (`dial0/llm.py`): commands, a note, optionally a one-line reason.
3. **Check** every proposed command (guardrails, below). Certain mistakes are fixed by code without another model
   call (interface names, address/netmask form, typos, tagged/untagged); anything else is rejected.
4. **Approve:** read-only commands run right away; changes are shown and run only after one y/N for the batch.
5. **Act:** the approved batch runs in order, in one host invocation.
6. **Observe and fix:**
   - an error saying the result is already in place ("Vlan100 already exists") counts as done, and the batch goes on;
   - any other error goes back to the model with the request, what succeeded, SONiC's exact error and what didn't run;
   - up to `FIX_ROUNDS` rounds, each with its own y/N.

## Guardrails

| Guardrail | Where |
|---|---|
| Only commands from the operator's reference file are offered to or accepted from the model | `clidoc.check_command`, `clidoc.curated_path` |
| Commands, options and argument values are checked against the switch's own installed CLI code | `clidoc.validate`, `clidoc.check_args` (index built by `extract_cli.py`) |
| IPs, prefixes and interface names must come from the request; netmasks become prefixes | `resolve.fix_values` |
| Only `config` may change the switch; Linux tools that bypass SONiC are refused or translated | `tools.parse_click`, `resolve.fix` |
| Dangerous subcommands (reload, reboot...) are blocked | `tools.DENY_SUB` |
| Every change needs the operator's y/N (`CONFIRM=ask`; `dry-run` and `auto` exist) | `Agent._plan`, `Agent.confirm` |
| SONiC's CLI runs with the host's clean environment, never the container's | `tools.HOST_ENV` |

## Working memory

`dial0/sessions.py`: one JSON file per session in the state directory (a Docker volume).
- **messages:** the model's context; compact summaries of requests and what ran, including SONiC's errors.
- **log:** what ran.
- **pending:** a change waiting for approval.
- **last_trace:** the steps of the last request (`dial0 why`).

Context is off for single requests unless asked for (`--context`, `-s NAME`), and always on at the `dial0>` prompt.

## Tools

| Tool | Used by |
|---|---|
| SONiC CLI runner (`tools.run_batch`, `tools._run`) | requests, workflows, blueprints |
| Command reference lookup (`clidoc.ref`, `clidoc.search_paths`) | the planner, `dial0 ref ...` |
| Log search (native grep) | health and security workflows |
| CVE matcher (`cve.py`, `debver.py`) | the CVE scan |
| vtysh (FRR/BGP) | blueprints only |

## Workflows

`dial0/workflows.py`: fixed, read-only check sequences. Code decides every finding, so the verdict is never made
up; when something isn't healthy, the model adds insights (summary, likely causes, next steps), and any command it
suggests is checked against the reference.

| Workflow | Checks |
|---|---|
| `health` | all of the below except security and CVE |
| `services` | system ready, system health, containers of enabled features |
| `hardware` | SSD health, fans, transceivers |
| `interfaces`, `counters`, `bgp`, `resources`, `logs` | one area each |
| `security` | configuration audit (`security.py`) + CVE scan |
| `cve` | Debian packages of the host and every SONiC container vs the Debian Security Tracker; CISA KEV |

A scheduler thread runs due workflows (interval or daily time), never at the same time as a request (it holds the
agent's lock), and keeps the last N results per workflow.

## Blueprints

`dial0/blueprints.py`: template-based configuration of the switch Dial 0 runs on.
1. **Questions:** the operator answers the blueprint's questions; the answers are saved per switch.
2. **Validate** against the running configuration, read once with `sonic-cfggen -d --print-data`.
3. **Generate** the full configuration. Steps already in place are marked and skipped.
4. **Check:** conflicts with the running configuration are reported, never removed; a blocker (FRR not in `split`
   mode) stops it.
5. **Apply** after y/N: re-read the switch and refuse if it changed since the review, then back up, apply in order
   (stop at the first real error), save, and verify (VLAN, loopback, BGP neighbors).

Blueprints don't use the model, and may use what normal requests can't (vtysh, `config save`), because their
commands are generated only from validated values.

## Security model

- **Nothing leaves the switch** except the downloads you enable: the model at install time and public CVE data.
- **The API listens on 127.0.0.1 only**, with an optional bearer token (`DIAL0_API_TOKEN`).
- **The container is privileged**, because it must enter the host's namespaces to run SONiC's CLI. Every command
  passes the guardrails above.
- **Password hashes (security audit)** are compared on the switch and never shown, stored or given to the model.

## State on the switch

| Where | What |
|---|---|
| `models/` (switch disk) | the model file |
| Docker volume `dial0-state` | sessions, learned plans, workflow results and schedules, caches, CVE data, blueprint answers |
| `/etc/sonic/dial0-backups/` | configuration backups taken before a blueprint is applied |
| `dial0.conf` | all settings |
