#!/bin/bash
TASK_DIR="${1:?Usage: $0 <task_dir>}"
cd "$TASK_DIR"

cat > plan.json << 'EOF'
{
  "parent_id": "test",
  "summary": "Test plan",
  "subtasks": [
    {
      "id": "test-sub",
      "role": "implementer",
      "goal": "Implement test",
      "inputs": {},
      "constraints": {}
    }
  ]
}
EOF

cat > result.json << 'EOF'
{
  "task_id": "test",
  "status": "success",
  "summary": "Orchestrator completed"
}
EOF