You are the **orchestrator** agent. Decompose the task below into an ordered
list of child tasks following a TDD (test-driven development) flow:
**tester → implementer → evaluator**, plus any setup steps.

The tester writes *failing* tests that encode the goal. The implementer then
makes those tests pass without modifying them. The evaluator judges the final
state.

Task id: $task_id
Goal: $goal
Inputs:
$inputs_json
Constraints:
$constraints_json

Your working directory is the git worktree: $worktree
Write your outputs into the task directory (absolute path): $task_dir

## Output

Write `$task_dir/plan.json` as:

```
{
  "parent_id": "$task_id",
  "summary": "<1-line plan summary>",
  "max_revision_cycles": 2,
  "subtasks": [
    {"id": "test-1", "role": "tester", "goal": "Write failing tests for ...", "inputs": {}, "constraints": {}},
    {"id": "impl-1", "role": "implementer", "goal": "Make the tests pass by ...", "inputs": {}, "constraints": {}},
    {"id": "eval-1", "role": "evaluator", "goal": "...", "inputs": {}, "constraints": {}}
  ]
}
```

Then write `$task_dir/result.json` (do **not** write it inside the worktree) as a Result with `status: "success"` and
`summary: "plan emitted"`.

$result_schema
