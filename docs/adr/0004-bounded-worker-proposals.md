# Bounded worker proposals

The operator authorized architect and test-planning worker proposals to use lower-cost models during a supervised factory run. This amends the no-delegation language in `agent-factory-design.md` sections 2, 5, 6, and 9. The original one-writer limit, exact candidate identity, independent acceptance, and subscription-only provider boundary remain in force.

Architect and tester planning invocations may return at most two bounded tasks under the `factory-swarm` contract. They do not start processes. The Coordinator validates proposed role, model, scope, task count, time, token allowance, and run reserve before the runner dispatches a task. Workers cannot request another worker recursively. The first implementation path consumes one developer proposal; check proposals require Coordinator review before execution. Review occurs in a fresh read-only context after the candidate writer stops. A worker cannot accept its own changes.

The project profile pins the provider model for each role. The current local pilot uses `gpt-5.6-sol` for developer and reviewer work and `gpt-5.6-luna` for inspection. The runner records the actual model and stops if it is unavailable; it does not switch models, billing paths, or accounts. Each proposed task has one logical attempt, at most 30 minutes, and a finite token allowance. Failed work and continuations consume that allowance. A task with no remaining verification reserve is rejected.

This change permits bounded proposals, not a general multi-writer swarm. Further concurrency and model routing changes require evaluated acceptance evidence and a separate decision.
