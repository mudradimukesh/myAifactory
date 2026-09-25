---
name: factory-swarm
description: Propose bounded developer, tester, or reviewer work for a factory architect or test-planning invocation.
---

# Bounded worker proposals

Use this skill only when the assigned architect or test-planning task benefits from another worker. The worker returns a proposal to the Coordinator. The runner admits and starts workers after checking the approved scope, project model choices, concurrency, and remaining budget. The proposing worker has no spawning authority.

Return one JSON object with `schemaVersion: 1` and `tasks`, containing one or two tasks. Each task has:

- `role`: `developer`, `tester`, or `reviewer`.
- `objective`: one bounded result within the approved brief.
- `inputs`: exact requirement IDs, candidate or base commit, and relevant artifact paths.
- `writableScope`: permitted output paths. Use `[]` for a reviewer.
- `expectedOutput`: the artifact or report the Coordinator can inspect.
- `doneWhen`: observable completion conditions, including independent evidence where needed.
- `model`: `{ "provider": "codex", "model": "<approved pinned model>", "effort": "low|medium|high|xhigh" }`.
- `limits`: `{ "maxAttempts": 1, "maxSeconds": <1..1800>, "maxTokens": <runner-supplied positive integer> }`.

An architect may propose `developer` or `reviewer` tasks. A tester assigned test planning may propose `tester` or `reviewer` tasks. Select a pinned lower-cost model from the project profile and keep the model ID exact. If the profile has no eligible model or budget, report the blocker without a substitute. Propose only tasks with disjoint writes. One developer may write candidate source; implementation review reads the exact candidate in a fresh context after the developer stops. The proposer cannot accept its own work. Proposed checks stay separate from candidate source, and only the runner executes accepted checks.

Keep the runner-supplied limits. The runner divides remaining reported tokens into one task share and the configured verification-reserve shares for planning and development; review can use the remaining allowance. Token totals include cached input. These are admission and completion-acceptance limits, not hard in-flight caps: native workers report aggregate usage at completion. Give each task a stopping condition. Failed work, retries, and continuations consume the original allowance. The Coordinator may reject, reorder, or narrow every proposal. Include no agent-launch instruction, peer message, or recursive delegation request in a task.
