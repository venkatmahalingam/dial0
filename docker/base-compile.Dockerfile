# Dial 0 BASE image, llama.cpp compiled here (LLAMA_SOURCE=compile). Slower (~10-20 min once), but independent
# of the prebuilt image and tuned to this exact CPU with CPU_TUNE=native.
ARG PY_IMAGE=python:3.12-slim-bookworm

# ---------- llama.cpp engine (CPU only) ----------
FROM ${PY_IMAGE} AS llama-build
ARG LLAMA_CPP_REF=master
ARG CPU_TUNE=native
ARG BUILD_JOBS=2
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone --depth 1 --branch "${LLAMA_CPP_REF}" https://github.com/ggml-org/llama.cpp .
RUN if [ "${CPU_TUNE}" = "native" ]; then TUNE="-DGGML_NATIVE=ON"; \
    else TUNE="-DGGML_NATIVE=OFF -DGGML_AVX=ON -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON"; fi \
 && cmake -B build -DCMAKE_BUILD_TYPE=Release ${TUNE} \
      -DLLAMA_CURL=OFF -DBUILD_SHARED_LIBS=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
 && cmake --build build --target llama-server -j"${BUILD_JOBS}" \
 && strip build/bin/llama-server \
 && mkdir -p /out && cp build/bin/llama-server /out/ \
 && (find build -name '*.so*' -exec cp -P {} /out/ \; || true)
# /out = the binary + any shared libraries this llama.cpp version builds (newer versions split the server into libs)

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

# ---------- base ----------
FROM ${PY_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends \
      libgomp1 libstdc++6 util-linux grep procps \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt
COPY --from=llama-build /out/ /app/
COPY --from=sonic-src /out/commands.json /opt/dial0/data/commands.json
COPY --from=sonic-src /out/src /opt/sonic-utilities
ENV PATH=/opt/venv/bin:/app:$PATH \
    LD_LIBRARY_PATH=/app
RUN cd / && llama-server --version && python -c "import httpx, mcp.client.streamable_http"
