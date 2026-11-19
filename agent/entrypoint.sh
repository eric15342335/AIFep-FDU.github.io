#!/bin/bash
# Runs one agent session inside the sandbox container.
# Input: /task/doc holds the page images and the text layer; /task/q holds prompt.md and question.json.
# Output: the agent writes its answer and the session transcript to /work.
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

common=(--print --offline --no-context-files --no-skills --no-prompt-templates --no-mcp
  --no-extensions --extension "$PI_CODING_AGENT_DIR/extensions/tableqa.ts"
  --model "local/$AGENT_MODEL" --thinking "${AGENT_THINKING:-off}"
  --append-system-prompt "$PI_CODING_AGENT_DIR/agent-prompt.md")

# For a session that hit the turn limit before the final answer-only turn existed, run only that turn.
if [[ -n "${AGENT_RESUME:-}" ]]; then
  exec pi "${common[@]}" --tools answer --session "$AGENT_RESUME" "$(cat "$PI_CODING_AGENT_DIR/grace.md")"
fi

pages=()
for page in /task/doc/page-*.jpg; do pages+=("@$page"); done

exec pi "${common[@]}" --tools read,bash,write,zoom,answer --session-dir /work/session \
  "${pages[@]}" "$(cat /task/q/prompt.md)"
