#!/bin/bash
# Runs under docker --init. Supervises llama-server (inference engine) and the Python harness; if either exits, the container exits
# and Docker's restart policy brings both back.
set -uo pipefail

if [ ! -s "$MODEL_PATH" ]; then
  echo "ERROR: model not found at $MODEL_PATH (run: dial0 ctl model)" >&2; exit 1
fi

# llama-server's shared libraries live next to it in /app (see docker/base-*.Dockerfile)
export LD_LIBRARY_PATH="/app${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
LLAMA_BIN="$(command -v llama-server || true)"
if [ -z "$LLAMA_BIN" ]; then echo "ERROR: llama-server not found in the image; rebuild: ./setup.sh --rebuild" >&2; exit 1; fi
MISSING="$(ldd "$LLAMA_BIN" 2>/dev/null | grep 'not found' || true)"
if [ -n "$MISSING" ]; then
  echo "ERROR: llama-server can't find its libraries:" >&2; echo "$MISSING" >&2
  echo "Rebuild with: ./setup.sh --rebuild   (or: dial0 ctl set LLAMA_SOURCE=compile, then ./setup.sh)" >&2
  sleep 30; exit 1   # slow down the restart loop
fi

ARGS=(-m "$MODEL_PATH" --host 127.0.0.1 --port "$LLAMA_PORT"
      -t "$LLAMA_THREADS" -tb "$LLAMA_THREADS" -c "$LLAMA_CTX" -np 1 --jinja)
# Keep Qwen3.5 in non-thinking mode so reasoning tokens never eat the 1-core budget
if [ "${DIAL0_DISABLE_THINKING:-1}" = "1" ]; then
  ARGS+=(--chat-template-kwargs '{"enable_thinking":false}')
fi

# Prompt-cache helpers, added only if this llama.cpp build knows them (flags change between versions):
#   --cache-reuse: reuse cached chunks even if the prompt shifted; --ctx-checkpoints: needed to reuse the cache
#   with hybrid/recurrent models such as Qwen3.5
HELP="$(llama-server --help 2>&1 || true)"
grep -q -- '--cache-reuse' <<<"$HELP" && ARGS+=(--cache-reuse 256)
grep -q -- '--ctx-checkpoints' <<<"$HELP" && ARGS+=(--ctx-checkpoints 8)

echo "starting llama-server: ${ARGS[*]}"
llama-server "${ARGS[@]}" &
LLAMA_PID=$!

stop() { kill "$LLAMA_PID" "${API_PID:-}" 2>/dev/null; wait; exit 0; }
trap stop TERM INT

echo "waiting for the model to load (can take a few minutes on one core)..."
for _ in $(seq 1 900); do
  if ! kill -0 "$LLAMA_PID" 2>/dev/null; then echo "ERROR: llama-server exited during startup" >&2; exit 1; fi
  if python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:$LLAMA_PORT/health',timeout=2)" 2>/dev/null; then
    READY=1; break
  fi
  sleep 1
done
if [ -z "${READY:-}" ]; then echo "ERROR: llama-server not healthy after 900s" >&2; kill "$LLAMA_PID"; exit 1; fi
echo "model loaded"

python -m dial0 serve &
API_PID=$!

wait -n "$LLAMA_PID" "$API_PID"
echo "a process exited; stopping container so it can be restarted" >&2
kill "$LLAMA_PID" "$API_PID" 2>/dev/null
wait
exit 1
