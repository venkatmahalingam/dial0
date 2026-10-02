#!/usr/bin/env bash
# Tests host/dial0 (install, build reuse, model download, set, resources, checks) against fake docker/curl/ss
# and a fake 4-core 12 GB SONiC switch.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
mkdir -p "$T/bin" "$T/state" "$T/usrbin" "$T/dockerroot"
cp -r "$ROOT/." "$T/proj"; rm -rf "$T/proj/build.log" "$T/proj/models"
PASS=0; FAIL=0
check() { if eval "$2"; then echo "PASS $1"; PASS=$((PASS+1)); else echo "FAIL $1"; FAIL=$((FAIL+1)); fi; }

printf 'flags\t\t: fpu sse avx avx2 avx512f\n' > "$T/cpuinfo"
printf 'MemTotal:       12288000 kB\n' > "$T/meminfo"
printf "build_version: 'SONiC.202405.0-abcdef'\nasic_type: broadcom\n" > "$T/sonic_version.yml"

cat > "$T/bin/docker" <<'D'
#!/bin/bash
S="$FAKE_STATE"; echo "docker $*" >> "$S/calls"
img() { echo "$S/img_$(echo "$1" | tr '/:' '__')"; }
case "$1" in
  info) [ "${2:-}" = -f ] && echo "$FAKE_ROOT"; exit 0 ;;
  version) echo 24.0.7 ;;
  ps) if [[ "$*" == *status=running* ]]; then [ -f "$S/running" ] && echo c1; else [ -f "$S/exists" ] && echo c1; fi; exit 0 ;;
  image)
    [ "$2" = inspect ] || exit 0
    name="${@: -1}"; f="$(img "$name")"; [ -f "$f" ] || exit 1
    [[ "$*" == *Size* ]] && echo 1000000000
    [[ "$*" == *Labels* ]] && cat "$f"
    exit 0 ;;
  images) for f in "$S"/img_dial0-base_*; do [ -e "$f" ] && basename "$f" | sed 's/^img_//; s/_/:/'; done; exit 0 ;;
  rmi) rm -f "$(img "$2")"; exit 0 ;;
  build)
    [ -n "${FAKE_BUILD_FAIL:-}" ] && { echo "boom"; exit 1; }
    tag=""; file="Dockerfile"; label=""; prev=""
    for a in "$@"; do
      case "$prev" in -t) tag="$a" ;; -f) file="$a" ;; --label) [[ "$a" == dial0.version=* ]] && label="${a#dial0.version=}" ;; esac
      prev="$a"
    done
    echo "$(basename "$file") $tag" >> "$S/builds"
    [[ "$file" == *base-* ]] && echo "commands: 812  groups: 190  unresolved edges: 3  unreachable defs: 5"
    echo "$label" > "$(img "$tag")" ;;
  run) touch "$S/exists" "$S/running"; cp "$(img dial0-sonic:latest)" "$S/clabel"
       printf '%s\n' "$@" | grep -E '^(LLAMA_PORT|DIAL0_API_PORT)=' > "$S/env"
       grep '^LLAMA_PORT=' "$S/env" | cut -d= -f2 > "$S/ours" ;;
  rm) rm -f "$S/exists" "$S/running" "$S/ours" ;;
  exec) [[ "$*" == *health* ]] && { echo "  source_commands: 812"; exit 0; }; [[ "$*" == *du* ]] && echo "3 /var/lib/dial0"; exit 0 ;;
  inspect) if [[ "$*" == *Config.Env* ]]; then cat "$S/env" 2>/dev/null; elif [[ "$*" == *Labels* ]]; then cat "$S/clabel" 2>/dev/null; else echo "3 1000000000 5368709120"; fi ;;
  stats) echo "  now: cpu 1.0%  ram 3.1GiB / 5GiB" ;;
esac
exit 0
D
cat > "$T/bin/ss" <<'X'
#!/bin/bash
echo 'LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:(("sonic-webserver",pid=900,fd=3))'
echo 'LISTEN 0 128 127.0.0.1:6379 0.0.0.0:* users:(("redis-server",pid=800,fd=6))'
[ -f "$FAKE_STATE/ours" ] && echo "LISTEN 0 128 127.0.0.1:$(cat "$FAKE_STATE/ours") 0.0.0.0:* users:((\"llama-server\",pid=1,fd=3))"
exit 0
X
cat > "$T/bin/curl" <<'C'
#!/bin/bash
out=""; prev=""; resume=0
for a in "$@"; do [ "$prev" = -o ] && out="$a"; [ "$prev" = -C ] && resume=1; prev="$a"; done
if [[ "$*" == *raw.githubusercontent.com* ]]; then
  printf '```\nsudo config vlan add 100\nshow vlan brief\n```\n' > "$out"; exit 0
fi
if [[ "$*" == *huggingface* ]]; then
  if [ -z "$out" ]; then  # HEAD
    [[ "$*" == *NOPE* ]] && { printf 'HTTP/2 404\r\n'; exit 0; }
    printf 'HTTP/2 302\r\nlocation: x\r\n\r\nHTTP/2 200\r\nx-linked-size: 2910000000\r\n'; exit 0
  fi
  echo "download $(basename "$out") resume=$resume" >> "$FAKE_STATE/downloads"
  [ -n "${FAKE_DL_FAIL:-}" ] && { printf 'GG' > "$out"; exit 56; }
  printf 'GGUFfake-model-data' > "$out"; truncate -s 2910000000 "$out"; exit 0  # sparse: realistic size, no disk
fi
exit 0
C
chmod +x "$T/bin/"*
export PATH="$T/bin:$PATH" FAKE_STATE="$T/state" FAKE_ROOT="$T/dockerroot" DIAL0_NPROC=4 \
       DIAL0_CPUINFO="$T/cpuinfo" DIAL0_MEMINFO="$T/meminfo" DIAL0_SONIC_VERSION="$T/sonic_version.yml" DIAL0_BIN_DIR="$T/usrbin"
H="$T/proj/host/dial0"; CONF="$T/proj/dial0.conf"; MD="$T/proj/models"
nbuilds() { grep -c "$1" "$T/state/builds" 2>/dev/null || true; }

# ---------------------------------------------------------------- fresh install
out="$("$T/proj/setup.sh" 2>&1)"; rc=$?
check "setup.sh succeeds" '[ $rc = 0 ]'
check "setup: checks shown" 'grep -q "CPU: 1 of 4 cores for Dial 0 (cores 3)" <<<"$out" && grep -q "RAM: 5g" <<<"$out" && grep -q "model Qwen3.5-4B-UD-Q4_K_XL.gguf: 2775 MB to download" <<<"$out"'
check "setup: base built from the prebuilt llama.cpp image (no compile)" '[ "$(nbuilds base-prebuilt)" = 1 ] && [ "$(nbuilds base-compile)" = 0 ] && grep -q "No compiling" <<<"$out"'
check "setup: base build gets the SONiC branch (202405) and llama image" 'grep "base-prebuilt" "$T/state/calls" | grep -q "SONIC_UTILITIES_REF=202405.*" && grep "base-prebuilt" "$T/state/calls" | grep -q "LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp:server"'
check "setup: app image built on top of the base" 'grep " -f .*/Dockerfile " "$T/state/calls" | grep -q "BASE_IMAGE=dial0-base:"'
check "setup: model downloaded to the switch disk, not the image" '[ -f "$MD/Qwen3.5-4B-UD-Q4_K_XL.gguf" ] && [ -f "$MD/Qwen3.5-4B-UD-Q4_K_XL.gguf.ok" ] && ! grep -q "MODEL_REPO" "$T/state/calls"'
check "setup: model download runs in parallel with the build" 'grep -q "Downloading the model in the background" <<<"$out"'
check "setup: container gets model mounted read-only + --init" 'grep "docker run" "$T/state/calls" | grep -q -- "--init" && grep "docker run" "$T/state/calls" | grep -q -- "-v $MD:/models:ro -e MODEL_PATH=/models/Qwen3.5-4B-UD-Q4_K_XL.gguf"'
check "setup: context off by default" 'grep "docker run" "$T/state/calls" | grep -q -- "-e DIAL0_CONTEXT=off -e DIAL0_LEARN=on"'
check "setup: workflow retention + the switch's time zone (for daily schedules)" 'grep "docker run" "$T/state/calls" | grep -q -- "-e DIAL0_WORKFLOW_KEEP=10 -e DIAL0_WORKFLOW_INSIGHTS=on -v /etc/localtime:/etc/localtime:ro"'
check "setup: runs with 1 cpu on core 3, 5g, threads 1" 'grep "docker run" "$T/state/calls" | grep -q -- "--cpuset-cpus=3 --cpus=1 --memory=5g --memory-swap=5g.*LLAMA_THREADS=1"'
check "setup: llama.cpp on port 18081 (8080 is taken)" 'grep "docker run" "$T/state/calls" | grep -q -- "-e LLAMA_PORT=18081 -e LLAMA_URL=http://127.0.0.1:18081" && grep -q "ports: llama.cpp 18081" <<<"$out"'
check "setup: log rotation + restart policy" 'grep "docker run" "$T/state/calls" | grep -q -- "--restart unless-stopped.*max-size=10m"'
check "setup: installs dial0 command" '[ -L "$T/usrbin/dial0" ]'
check "setup: waits for ready" 'grep -q "ready." <<<"$out"'
check "setup: build cache pruned (base image kept)" 'grep -q "docker builder prune -af" "$T/state/calls" && ls "$T/state"/img_dial0-base_* >/dev/null'

# ---------------------------------------------------------------- reuse
: > "$T/state/builds"; : > "$T/state/downloads"
out="$("$T/proj/setup.sh" 2>&1)"
check "re-run with nothing changed: no build, no download" '[ ! -s "$T/state/builds" ] && [ ! -s "$T/state/downloads" ] && grep -q "up to date with this folder" <<<"$out"'

echo "# changed" >> "$T/proj/dial0/agent.py"
touch "$T/state/running"
out2="$("$H" "show vlan brief" 2>&1 </dev/null)"
check "request: warns when running build is older than the folder" 'grep -q "older build than" <<<"$out2"'
out="$("$T/proj/setup.sh" 2>&1)"
check "code change: only the app image is rebuilt (base reused, no download)" '[ "$(nbuilds base-)" = 0 ] && [ "$(nbuilds "^Dockerfile")" = 1 ] && [ ! -s "$T/state/downloads" ] && grep -q "Base image dial0-base:.* is up to date: reused" <<<"$out"'
out2="$("$H" "show vlan brief" 2>&1 </dev/null)"
check "request: warning gone after update" '! grep -q "older build" <<<"$out2"'

: > "$T/state/builds"
old_base="$(ls "$T/state" | grep img_dial0-base_ | head -1)"
echo "# pin" >> "$T/proj/requirements.txt"
out="$("$T/proj/setup.sh" 2>&1)"
check "requirements change: base rebuilt" '[ "$(nbuilds base-prebuilt)" = 1 ]'
check "old base image removed" '[ ! -e "$T/state/$old_base" ] && grep -q "removed old base image" <<<"$out"'

: > "$T/state/builds"
out="$("$T/proj/setup.sh" --rebuild 2>&1)"
check "setup --rebuild: base rebuilt with --pull (newer llama.cpp)" '[ "$(nbuilds base-prebuilt)" = 1 ] && grep "base-prebuilt" "$T/state/calls" | tail -1 | grep -q -- "--pull"'

# ---------------------------------------------------------------- model switching (no rebuild)
: > "$T/state/builds"; : > "$T/state/downloads"; : > "$T/state/calls"
out="$("$H" ctl set MODEL_REPO=unsloth/Qwen3.5-2B-GGUF MODEL_FILE=Qwen3.5-2B-UD-Q4_K_XL.gguf 2>&1)"; rc=$?
check "set MODEL_FILE: downloads + restarts, no rebuild" '[ $rc = 0 ] && [ ! -s "$T/state/builds" ] && grep -q "download Qwen3.5-2B" "$T/state/downloads" && grep "docker run" "$T/state/calls" | grep -q "MODEL_PATH=/models/Qwen3.5-2B-UD-Q4_K_XL.gguf"'
check "set MODEL_FILE: previous model deleted (disk)" '[ ! -e "$MD/Qwen3.5-4B-UD-Q4_K_XL.gguf" ] && grep -q "removed old model" <<<"$out"'
out="$(FAKE_DL_FAIL=1 "$H" ctl set MODEL_FILE=Qwen3.5-4B-UD-Q4_K_XL.gguf MODEL_REPO=unsloth/Qwen3.5-4B-GGUF 2>&1)"; rc=$?
check "failed model download reported, resumable" '[ $rc != 0 ] && grep -q "ctl model" <<<"$out" && [ -f "$MD/Qwen3.5-4B-UD-Q4_K_XL.gguf.part" ]'
out="$("$H" ctl model 2>&1)"; rc=$?
check "ctl model resumes the download (-C -)" '[ $rc = 0 ] && tail -1 "$T/state/downloads" | grep -q "resume=1" && [ -f "$MD/Qwen3.5-4B-UD-Q4_K_XL.gguf.ok" ]'
"$H" ctl set MODEL_FILE=Qwen3.5-4B-UD-Q4_K_XL.gguf >/dev/null 2>&1

# ---------------------------------------------------------------- context / settings
: > "$T/state/calls"
out="$("$H" ctl set CONTEXT=on 2>&1)"; rc=$?
check "set CONTEXT=on applied" '[ $rc = 0 ] && grep "docker run" "$T/state/calls" | grep -q "DIAL0_CONTEXT=on"'
"$H" ctl set CONTEXT=off >/dev/null 2>&1
out="$("$H" ctl set CONTEXT=maybe 2>&1)"; rc=$?
check "invalid CONTEXT rejected" '[ $rc != 0 ]'

: > "$T/state/calls"
out="$("$H" ctl set CPUS=2 MEMORY=6g 2>&1)"; rc=$?
check "set CPUS=2 MEMORY=6g ok" '[ $rc = 0 ] && grep -q "^CPUS=2 " "$CONF" && grep -q "^MEMORY=6g " "$CONF"'
check "set: comments in dial0.conf preserved" 'grep -q "^CPUS=2 .*# CPU cores for Dial 0" "$CONF"'
check "set: recreated with cores 2,3 and 2 threads" 'grep "docker run" "$T/state/calls" | grep -q -- "--cpuset-cpus=2,3 --cpus=2 --memory=6g.*LLAMA_THREADS=2"'
cp "$CONF" "$T/conf.before"
out="$("$H" ctl set CPUS=4 2>&1)"; rc=$?
check "set CPUS=4 rejected (must leave 1 core for SONiC)" '[ $rc != 0 ] && grep -q "leave at least 1 for SONiC" <<<"$out"'
check "rejected change rolled back" 'cmp -s "$CONF" "$T/conf.before"'
out="$("$H" ctl set MEMORY=2g 2>&1)"; rc=$?
check "set MEMORY=2g rejected (model needs more)" '[ $rc != 0 ] && grep -q "too small for the model" <<<"$out"'
out="$("$H" ctl set MEMORY=20g 2>&1)"; rc=$?
check "set MEMORY=20g rejected (more than switch)" '[ $rc != 0 ] && grep -q "more than the switch has" <<<"$out"'
out="$("$H" ctl set STORAGE=2g 2>&1)"; rc=$?
check "set STORAGE=2g rejected" '[ $rc != 0 ] && grep -q "STORAGE=2g too small" <<<"$out"'
out="$("$H" ctl set CPUSET=1,2 2>&1)"; rc=$?
check "set CPUSET=1,2 explicit" '[ $rc = 0 ] && grep "docker run" "$T/state/calls" | tail -1 | grep -q -- "--cpuset-cpus=1,2 --cpus=2"'
out="$("$H" ctl set CPUSET=7 2>&1)"; rc=$?
check "set CPUSET=7 rejected (no such cpu)" '[ $rc != 0 ] && grep -q "only has CPUs 0-3" <<<"$out"'
out="$("$H" ctl set FOO=1 2>&1)"; rc=$?
check "unknown key rejected" '[ $rc != 0 ] && grep -q "unknown setting" <<<"$out"'
out="$("$H" ctl set 'MCP_SERVER_URL=https://mcp.example.com/a&b' 2>&1)"; rc=$?
check "URL with & and / saved verbatim" '[ $rc = 0 ] && grep -q "^MCP_SERVER_URL=https://mcp.example.com/a&b" "$CONF"'
out="$("$H" ctl set CONFIRM=maybe 2>&1)"; rc=$?
check "invalid CONFIRM rejected" '[ $rc != 0 ]'
out="$("$H" ctl set LLAMA_PORT=8080 2>&1)"; rc=$?
check "port already in use rejected (8080)" '[ $rc != 0 ] && grep -q "port 8080 is already in use on the switch (sonic-webserver)" <<<"$out"'
out="$("$H" ctl set API_PORT=18081 2>&1)"; rc=$?
check "same port for both rejected" '[ $rc != 0 ] && grep -q "both 18081" <<<"$out"'
out="$("$H" ctl set LLAMA_PORT=80 2>&1)"; rc=$?
check "privileged/invalid port rejected" '[ $rc != 0 ] && grep -q "1024-65535" <<<"$out"'
out="$("$H" ctl set CPUS=1 2>&1)"; rc=$?
check "our own running llama port is not reported as a conflict" '[ $rc = 0 ] && ! grep -q "already in use" <<<"$out"'
out="$("$H" ctl set LLAMA_PORT=18090 API_PORT=18091 2>&1)"; rc=$?
check "ports changed and applied" '[ $rc = 0 ] && grep "docker run" "$T/state/calls" | tail -1 | grep -q -- "-e LLAMA_PORT=18090 -e LLAMA_URL=http://127.0.0.1:18090 -e DIAL0_API_PORT=18091"'
"$H" ctl set LLAMA_PORT=18081 API_PORT=8090 >/dev/null 2>&1

# ---------------------------------------------------------------- compile option
: > "$T/state/builds"; : > "$T/state/calls"
out="$("$H" ctl set LLAMA_SOURCE=compile 2>&1)"; rc=$?
check "set LLAMA_SOURCE=compile asks for rebuild" '[ $rc = 0 ] && grep -q "dial0 ctl rebuild" <<<"$out"'
out="$("$H" ctl rebuild 2>&1)"; rc=$?
check "compile base uses all cores but one while building" '[ $rc = 0 ] && [ "$(nbuilds base-compile)" = 1 ] && grep "base-compile" "$T/state/calls" | grep -q "BUILD_JOBS=3"'
"$H" ctl set LLAMA_SOURCE=prebuilt >/dev/null 2>&1

# ---------------------------------------------------------------- misc
out="$("$H" ctl resources 2>&1)"
check "resources shows config and usage" 'grep -q "CPUS=1" <<<"$out" && grep -q "sessions/state: 3 MB" <<<"$out" && grep -q "image: 953 MB" <<<"$out"'
out="$("$H" ctl status 2>&1)"
check "status shows model" 'grep -q "model: Qwen3.5-4B-UD-Q4_K_XL.gguf" <<<"$out"'
sed -i 's/^MODEL_FILE=[^ ]*/MODEL_FILE=NOPE.gguf/' "$CONF"
out="$("$H" ctl check 2>&1)"; rc=$?
check "check: wrong model file caught before anything is built" '[ $rc != 0 ] && grep -q "not found on Hugging Face" <<<"$out"'
sed -i 's/^MODEL_FILE=[^ ]*/MODEL_FILE=Qwen3.5-4B-UD-Q4_K_XL.gguf/' "$CONF"
: > "$T/state/calls"
out="$(DIAL0_SESSION=s1 "$H" -v "show vlan brief" </dev/null 2>&1)"
check "request forwarded into container with session" 'grep -q "docker exec -i -e DIAL0_SESSION=s1 dial0 dial0 -v show vlan brief" "$T/state/calls"'
: > "$T/state/calls"
"$H" </dev/null >/dev/null 2>&1
check "plain dial0 opens interactive mode inside the container" 'grep -qx "docker exec -i dial0 dial0" "$T/state/calls"'
out="$(FAKE_BUILD_FAIL=1 "$H" ctl rebuild 2>&1)"; rc=$?
check "failed build reported" '[ $rc != 0 ] && grep -q "build failed" <<<"$out"'
rm -f "$T/state/running"
out="$("$H" "show vlan brief" 2>&1)"; rc=$?
check "clear message when not running" '[ $rc != 0 ] && grep -q "not running" <<<"$out"'

: > "$T/state/calls"; touch "$T/state/running"
"$H" ctl doctor >/dev/null 2>&1
check "ctl doctor runs inside the container" 'grep -q "docker exec dial0 python -m dial0.doctor" "$T/state/calls"'

# ---------------------------------------------------------------- pipefail safety
# `cmd | grep -q` under `set -o pipefail` fails when grep quits early and cmd gets SIGPIPE (this broke
# `ctl update` once the tarball grew): pipes must feed grep without -q (and >/dev/null) instead.
check "no 'cmd | grep -q' in the host script (pipefail + SIGPIPE)" '! grep -nE "\\| *grep -q" "$H"'

# ---------------------------------------------------------------- the command reference (your file)
: > "$T/state/calls"; touch "$T/state/running"
out="$("$H" ctl cmdref 2>&1)"; rc=$?
check "cmdref: your reference file is used by default (no count shown) and mounted read-only" '[ $rc = 0 ] && grep -q "command reference: .*reference/sonic-command-reference.md$" <<<"$out" && ! grep -qE "[0-9]+ command" <<<"$out" && grep "docker run" "$T/state/calls" | tail -1 | grep -q -- "-v $T/proj/reference/sonic-command-reference.md:/opt/cmdref/reference.md:ro"'
out="$("$H" ctl check 2>&1)"
check "cmdref: checks say only these commands are used" 'grep -q "only these commands are used" <<<"$out"'
: > "$T/state/calls"
out="$("$H" ctl set CMDREF=https://github.com/sonic-net/sonic-utilities/blob/master/doc/my-ref.md 2>&1)"; rc=$?
check "cmdref: a URL is downloaded (github blob -> raw) and mounted" '[ $rc = 0 ] && [ -f "$T/proj/data/reference.md" ] && grep "docker run" "$T/state/calls" | tail -1 | grep -q -- "-v $T/proj/data/reference.md:/opt/cmdref/reference.md:ro"'
out="$("$H" ctl set CMDREF=reference/nope.md 2>&1)"; rc=$?
check "cmdref: a missing file is refused" '[ $rc != 0 ] && grep -q "command reference .*nope.md not found" <<<"$out"'
"$H" ctl set CMDREF=reference/sonic-command-reference.md >/dev/null 2>&1

# ---------------------------------------------------------------- the switch's installed SONiC CLI source
mkdir -p "$T/dist/config" "$T/dist/show"; touch "$T/dist/config/main.py" "$T/dist/show/main.py"
: > "$T/state/calls"
out="$(DIAL0_SRC_CANDIDATES="$T/nothing-here $T/dist" "$H" ctl set CONFIRM=ask 2>&1)"; rc=$?
check "switch CLI source found and reported" '[ $rc = 0 ] && grep -q "SONiC CLI source: $T/dist (config/, show/): used as the command reference" <<<"$out"'
check "switch CLI source mounted read-only" 'grep "docker run" "$T/state/calls" | tail -1 | grep -q -- "-v $T/dist/config:/opt/switch-src/config:ro -v $T/dist/show:/opt/switch-src/show:ro -e DIAL0_SWITCH_SRC=/opt/switch-src"'
out="$(DIAL0_SRC_CANDIDATES="$T/nothing-here" "$H" ctl check 2>&1)"
check "no switch CLI source: warns, falls back to the GitHub index" 'grep -q "switch.s SONiC CLI source not found" <<<"$out"'
out="$("$H" ctl set SONIC_SRC_DIR=/does/not/exist 2>&1)"; rc=$?
check "SONIC_SRC_DIR pointing nowhere rejected" '[ $rc != 0 ] && grep -q "has no config/main.py" <<<"$out"'
out="$("$H" ctl set SONIC_SRC_DIR="$T/dist" 2>&1)"; rc=$?
check "SONIC_SRC_DIR set explicitly" '[ $rc = 0 ] && grep "docker run" "$T/state/calls" | tail -1 | grep -q -- "-v $T/dist/config:/opt/switch-src/config:ro"'
"$H" ctl set SONIC_SRC_DIR=auto >/dev/null 2>&1
: > "$T/state/calls"
"$H" ctl set HTTPS_PROXY=http://proxy.example:3128 >/dev/null 2>&1; "$H" ctl recreate >/dev/null 2>&1 || "$H" ctl down >/dev/null 2>&1; "$H" ctl up >/dev/null 2>&1
check "proxy passed into the container (CVE data download)" 'grep "docker run" "$T/state/calls" | tail -1 | grep -q -- "-e HTTPS_PROXY=http://proxy.example:3128"'
"$H" ctl set HTTPS_PROXY= >/dev/null 2>&1

# ---------------------------------------------------------------- update from a tarball (keeps settings)
touch "$T/state/running"
"$H" ctl set CPUS=2 >/dev/null 2>&1
"$H" ctl rebuild >/dev/null 2>&1   # settle: current base + app built
mkdir -p "$T/new/dial0-sonic" && tar -C "$T/proj" --exclude=./models -cf - . | tar -C "$T/new/dial0-sonic" -xf -  # never copy the model
echo "# new version" >> "$T/new/dial0-sonic/dial0/agent.py"
sed -i 's/^CPUS=[^ ]*/CPUS=1/' "$T/new/dial0-sonic/dial0.conf"     # the tarball ships defaults
sed -i '/^API_PORT=/d' "$T/new/dial0-sonic/dial0.conf"             # ...and lacks one setting
echo "NEWSETTING=on                 # a setting added in the new version" >> "$T/new/dial0-sonic/dial0.conf"
tar czf "$T/proj/dial0-sonic.tar.gz" -C "$T/new" dial0-sonic
: > "$T/state/builds"
out="$("$H" --version 2>&1)"
check "--version shows running vs folder build" 'grep -q "this folder:" <<<"$out" && grep -q "running:" <<<"$out"'
out="$("$H" ctl update 2>&1)"; rc=$?
check "ctl update: finds the tarball, extracts in place (no nested folder)" '[ $rc = 0 ] && grep -q "# new version" "$T/proj/dial0/agent.py" && [ ! -d "$T/proj/dial0-sonic" ]'
check "ctl update: your settings are kept (CPUS=2), new settings added" 'grep -q "^CPUS=2 " "$CONF" && grep -q "^NEWSETTING=on" "$CONF"'
check "ctl update: a setting missing from the new file is kept too" 'grep -q "^API_PORT=" "$CONF"'
check "ctl update: then installs (app rebuilt, base reused)" '[ "$(nbuilds "^Dockerfile")" = 1 ] && [ "$(nbuilds base-)" = 0 ]'
out="$("$H" --version 2>&1)"
check "--version: up to date after update" 'grep -q "(up to date)" <<<"$out"'
out="$("$H" ctl update /nonexistent.tar.gz 2>&1)"; rc=$?
check "ctl update: clear error for a missing tarball" '[ $rc != 0 ] && grep -q "usage: dial0 ctl update" <<<"$out"'

echo "host tests: $PASS passed, $FAIL failed"
[ "$FAIL" = 0 ]
