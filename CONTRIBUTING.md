# Contributing to Dial 0

## Principles

These are what make Dial 0 safe on a production switch. Changes should keep them.

1. **The model proposes; code decides.** Anything that must be exact (which commands exist, argument values,
   findings in a health or security check, CVE matching) is done by code. The model chooses commands and explains.
2. **Every change needs the operator's approval** (`CONFIRM=ask`), and is shown in full first.
3. **Nothing is removed or overwritten silently.** Conflicts are reported; the operator decides.
4. **Every model call must earn its place.** The switch gives Dial 0 one CPU core, where a call takes seconds to
   minutes. Prefer a no-model path when the answer can be read or derived by code.
5. **Nothing leaves the switch** except downloads the operator enabled (the model, public CVE data).
6. **Never run commands through a shell.** Host commands go through `tools._run` / `tools.run_batch` as argv lists.

## Setup and tests

```
pip install -r requirements.txt
python3 tests/run_tests.py       # agent, harness, workflows, security, CVE, blueprints, CLI end to end
bash tests/test_host.sh          # the host command (fake docker / curl / ss)
python3 tests/py310_check.py     # Python 3.10 compatibility
```

No Docker, model or switch is needed: the tests stand in fake SONiC commands (small scripts on `PATH`) and a stub
model. Every behaviour change should come with a test. Keep code compatible with Python 3.10, since the switch's
base image may ship an older Python.

## Common changes

### Allow a command

Add it to `reference/sonic-command-reference.md`, in a code block under the right set (show or config) and area,
in the same style. A note after `#` explains an example to the model, e.g.
`sudo config vlan member add 100 Ethernet0   # tagged member: the default, no flag`.
On a running switch, `dial0 ctl cmdref` reloads the file without a rebuild.

### Add a health check

In `dial0/health.py`:
1. Write a function returning `(level, name, message, raw_output)`, where level is `ok`, `info`, `warn` or `crit`.
2. Read outputs with code (`_table` reads SONiC tables). "Not available on this switch" is `info`, not a failure.
3. Add the function to `CHECKS`, and to any workflow in `dial0/workflows.py` that should run it.
4. Add tests with a fake command that prints realistic good and bad outputs.

### Add a workflow

Add an entry to `REGISTRY` in `dial0/workflows.py`:
- either `checks` (a list of check functions);
- or a `runner(step) -> (report, overall, findings, extra)` for a custom sequence, as `security` and `cve` do.

Scheduling, retention and insights then work automatically.

### Add a blueprint

In `dial0/blueprints.py`:
1. Add an entry to `BLUEPRINTS`, with its questions per role.
2. Write a generator: `generate()` is specific to the 2-tier Clos blueprint today, so dispatch on the blueprint's
   name.
3. Add its conflict checks to `check()`.
4. Generate commands only from validated values; mark steps already in place with `skip`.
5. Test the generated configuration exactly, plus conflicts, a failure mid-way and the verification.

### Regenerate the architecture diagram

`python3 docs/architecture.py` rewrites `docs/architecture.png` (matplotlib; uses the Poppins font if installed).

## Building the release tarball

The switch installs from a tarball of the project folder:

```
tar czf dial0-sonic.tar.gz --exclude=dial0-sonic/models --exclude=dial0-sonic/data \
    --exclude='__pycache__' --exclude=dial0-sonic/.git dial0-sonic
```

On the switch: `dial0 ctl update` (or `./setup.sh` for a first install).
