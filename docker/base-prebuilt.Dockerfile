# Dial 0 BASE image, prebuilt llama.cpp (default, fastest build: nothing is compiled).
# Built only when this file, requirements.txt, the SONiC command extractor, LLAMA_IMAGE or the SONiC branch
# change; everyday code updates only rebuild the thin app image on top of it (seconds).
# The official llama.cpp server image picks the best CPU code path (AVX2/AVX-512...) at runtime.
ARG LLAMA_IMAGE=ghcr.io/ggml-org/llama.cpp:server
ARG PY_IMAGE=python:3.12-slim-bookworm

# ---------- sonic-utilities source -> command index ----------
FROM ${PY_IMAGE} AS sonic-src
ARG SONIC_UTILITIES_REF=master
ARG MIN_COMMANDS=150
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY dial0/extract_cli.py /extract_cli.py
RUN REF="${SONIC_UTILITIES_REF}" \
 && if ! git clone --depth 1 --branch "$REF" https://github.com/sonic-net/sonic-utilities.git /src; then \
      echo "sonic-utilities branch '$REF' not found, using master" \
      && REF=master && git clone --depth 1 https://github.com/sonic-net/sonic-utilities.git /src; fi \
 && mkdir -p /out/src \
 && python /extract_cli.py --root /src --out /out/commands.json --ref "$REF" \
      --commit "$(git -C /src rev-parse HEAD)" --min-commands "${MIN_COMMANDS}" \
 && cd /src && find config show -name '*.py' | tar cf - -T - | tar xf - -C /out/src \
 && (cp doc/Command-Reference.md /out/src/ 2>/dev/null || true)

# ---------- base: official llama.cpp server image + Python ----------
FROM ${LLAMA_IMAGE}
USER root
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-venv util-linux grep procps ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=sonic-src /out/commands.json /opt/dial0/data/commands.json
COPY --from=sonic-src /out/src /opt/sonic-utilities
# llama-server loads its own shared libraries (libllama*, libggml*, libllama-server-impl...) from /app. Point the
# loader there explicitly so it works from any working directory (the app image runs from /opt/dial0).
ENV PATH=/opt/venv/bin:/app:$PATH \
    LD_LIBRARY_PATH=/app
# checked from / on purpose: must not depend on the working directory
RUN cd / && llama-server --version \
 && python -c "import sys, httpx, mcp.client.streamable_http; assert sys.version_info >= (3, 10)"
ENTRYPOINT []
