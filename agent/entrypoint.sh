#!/bin/bash
# One agent session. Inputs: /task/doc (pages, text layer), /task/q (prompt.md, question.json). Output: /work.
set -euo pipefail
: "${AGENT_MODEL:?AGENT_MODEL is required}"

export HOME=/tmp PI_CODING_AGENT_DIR=/tmp/pi-agent
cp -r /opt/pi-agent "$PI_CODING_AGENT_DIR"
python3 - <<'EOF'
import json, os
path = os.environ["PI_CODING_AGENT_DIR"] + "/models.json"
config = json.load(open(path))
model = config["providers"]["local"]["models"][0]
model["id"] = model["name"] = os.environ["AGENT_MODEL"]
json.dump(config, open(path, "w"), indent=2)
EOF

pages=()
for page in /task/doc/page-*.jpg; do pages+=("@$page"); done

exec pi --print --offline --no-context-files --no-skills --no-prompt-templates --no-mcp \
  --no-extensions --extension "$PI_CODING_AGENT_DIR/extensions/tableqa.ts" \
  --model "local/$AGENT_MODEL" --thinking "${AGENT_THINKING:-off}" \
  --tools read,bash,write,zoom,answer \
  --append-system-prompt "$PI_CODING_AGENT_DIR/agent-prompt.md" \
  --session-dir /work/session \
  "${pages[@]}" "$(cat /task/q/prompt.md)"
