# Dial 0 APP image: only the Dial 0 code, on top of the base image (docker/base-*.Dockerfile). Builds in seconds.
# The model is NOT in the image: it's downloaded once to the switch and mounted read-only at /models.
# Normally built by ./setup.sh / `dial0 ctl rebuild`, which pass BASE_IMAGE.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
WORKDIR /opt/dial0
COPY dial0/ ./dial0/
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# Same conditions as at runtime (this WORKDIR, this environment): fail the build, not the switch, if llama-server
# can't start or can't find its libraries.
RUN missing="$(ldd "$(command -v llama-server)" 2>/dev/null | grep 'not found' || true)"; \
    if [ -n "$missing" ]; then echo "llama-server is missing libraries:"; echo "$missing"; exit 1; fi; \
    llama-server --version
RUN chmod +x /usr/local/bin/entrypoint.sh \
 && printf '#!/bin/sh\nexec python -m dial0.cli "$@"\n' > /usr/local/bin/dial0 \
 && chmod +x /usr/local/bin/dial0 \
 && mkdir -p /var/lib/dial0 \
 && python -c "import dial0.server"

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/dial0 \
    MODEL_PATH=/models/model.gguf \
    LLAMA_PORT=18081 \
    LLAMA_THREADS=1 \
    LLAMA_CTX=8192 \
    DIAL0_DISABLE_THINKING=1 \
    DIAL0_API_HOST=127.0.0.1 \
    DIAL0_API_PORT=8090 \
    DIAL0_STATE_DIR=/var/lib/dial0 \
    DIAL0_STATE_MAX_MB=200 \
    DIAL0_HOST_EXEC=nsenter \
    DIAL0_CONFIRM=ask \
    DIAL0_CMD_INDEX=/opt/dial0/data/commands.json \
    DIAL0_SRC_ROOT=/opt/sonic-utilities \
    DIAL0_CLI_VALIDATE=strict \
    DIAL0_MAX_STEPS=6 \
    MCP_SERVER_URL=

VOLUME ["/var/lib/dial0"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=900s --retries=3 \
  CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ['DIAL0_API_PORT'],timeout=4)" || exit 1
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
