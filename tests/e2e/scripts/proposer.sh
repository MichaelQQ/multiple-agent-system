#!/bin/bash
TASK_DIR="${1:?Usage: $0 <task_dir>}"
cd "$TASK_DIR"

cat > result.json << 'EOF'
{
  "task_id": "test",
  "status": "success",
  "summary": "Proposal accepted"
}
EOF