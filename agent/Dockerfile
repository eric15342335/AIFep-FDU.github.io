# The sandbox image for one agent session: the pi agent and a fixed set of offline Python tools.
# It contains no model weights and no OCR engines. `uv run agent.py` builds it automatically.
FROM node:24-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends bash ca-certificates python3 ripgrep \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv
COPY agent/sandbox-requirements.txt /tmp/
RUN uv pip install --system --break-system-packages --no-cache -r /tmp/sandbox-requirements.txt

RUN npm install -g --ignore-scripts @earendil-works/pi-coding-agent@1.5.2 && npm cache clean --force

# The pi settings are read-only here; the entrypoint copies them to a writable temporary folder at start.
COPY agent/pi /opt/pi-agent
# The same answer validator as the baseline (standard library only), plus the helpers behind `zoom` and the answer check.
COPY tableqa/__init__.py tableqa/schema.py /opt/tableqa/
COPY agent/tools.py /opt/tableqa_tools.py
COPY agent/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

ENV PI_OFFLINE=1 PI_SKIP_VERSION_CHECK=1 PI_TELEMETRY=0 PYTHONDONTWRITEBYTECODE=1
USER node
WORKDIR /work
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
