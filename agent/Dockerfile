# Sandbox for one agent session: pi + a fixed, offline Python toolset. No model weights, no OCR engines.
# Build from the repo root: docker build -f agent/Dockerfile -t tableqa-agent .
FROM node:24-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates python3 ripgrep \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
COPY agent/sandbox-requirements.txt /tmp/
RUN uv pip install --system --break-system-packages --no-cache -r /tmp/sandbox-requirements.txt

RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.5.2 && npm cache clean --force

# Read-only pi config; the entrypoint copies it to a writable tmpfs at start.
COPY agent/pi /opt/pi-agent
# Same answer validator as the baseline (stdlib only) plus the zoom/check helpers.
COPY tableqa/__init__.py tableqa/schema.py /opt/tableqa/
COPY agent/tools.py /opt/tableqa_tools.py
COPY agent/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

ENV PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0 PYTHONDONTWRITEBYTECODE=1
USER node
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
