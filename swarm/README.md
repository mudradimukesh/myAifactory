# Swarm kit

This directory holds the orchestrator, lead, builder and gate tooling that ran the FloorInteriorDesignAndRenderer Phase 1 swarm. Copy it into a new project to start a swarm there.

The kit comes from commit `aadb2dfb` on `main` of FloorInteriorDesignAndRenderer (remote `interior`), the tooling the NP2 pilot ran on 2026-10-28, plus `claude-reviewer-settings.json`, which exists only in BIN. Read [the loop lessons](../docs/orchestrator-lead-loop-lessons.md) before running it. To monitor or restart the running NP2 pilot, follow [the monitor runbook](MONITOR.md).

## Layout

The paths match the host project, so tests and scripts resolve each other without changes.

| Path | Contents |
| --- | --- |
| `scripts/swarm/` | Run ledger (`run_state.py`), guards (`fp-guard.py`, `pilot_guard.py`), launchers (`codex_launch.py`, `lead-spawn.sh`, `interior-lead.sh`, `orchestrator-spawn.sh`), lead lifecycle (`pilot_lead.py`), gates (`unit_gate.py`, `gate_evidence.py`, `red_check.py`, `mutation_gate.py`, `review_check.py`), integration (`integration.py`, `prepare_candidate.sh`, `merge_unit.sh`), task environments (`task_env.py`), the watchdog, the startup packet, the alert helper, and the Claude settings files |
| `tests/swarm/` | Tests for all of the above |
| `.claude/agents/` | Agent definitions for the leads, planner, architect and reviewers of each phase |
| `prompts/phase-1/` | Orchestrator, pilot lead and builder briefs, and `roles.md` |
| `docs/orchestrator-templates.md` | The command templates orchestrators used for registration, drains and pointers |

## Start a swarm for a new project

1. **Copy the kit.** Copy `scripts/swarm`, `tests/swarm`, `.claude/agents` and `prompts/phase-1` into the new repository at the same paths. Rename the phase prefixes (`p1-`, `interior-`) if the new project uses different phase names.

2. **Replace the host paths.** The scripts hard-code the original host. Replace each value in every copied file:

   | Original | Meaning |
   | --- | --- |
   | `/Users/mukeshkumarmudradi/Documents/AI_Projects/github/FloorInteriorDesignAndRenderer` | Main checkout of the project |
   | `.../FloorInteriorDesignAndRenderer-wt` | Root for builder and integration worktrees |
   | `/tmp/factory-fp-0925/swarm/bin` and `/private/tmp/factory-fp-0925/swarm/bin` | BIN, the installed copy of `scripts/swarm` |
   | `/tmp/factory-fp-0925/swarm/interior-p1-poc`, `.../interior-p1` | ART, the run directory holding `run.json`, `questions.md`, briefs, receipts and logs |
   | `.../interior-p2`, `.../interior-p25`, `.../interior-p3`, `.../fp3d` | Run directories of other phases |
   | `.../dotfiles/home/.agents/skills/...` | Shared skills referenced by briefs |

   Put BIN and ART outside `/tmp`. macOS deletes `/tmp` files that have not been accessed for 3 days, which destroyed the original run's ledger and worktrees. List what remains with:

   ```sh
   grep -rnE 'mukeshkumarmudradi|factory-fp-0925|FloorInteriorDesignAndRenderer|interior-p[0-9]' scripts/swarm tests/swarm .claude prompts
   ```

3. **Adapt the gates to the project's stack.** `unit_gate.py` defines gates G1 to G19 with this project's commands: pytest with randomization and coverage, Ruff, mypy, ESLint, the TypeScript typecheck, Vitest seeds, Playwright journeys and mutation testing. `gate_evidence.runtime()` creates a fresh PostgreSQL database and starts the project's API. Replace these commands, and `PYTEST_IGNORE`, with the new project's checks. Keep one fresh database and API per stateful gate (`STATEFUL` in `unit_gate.py`): a shared test database broke both API and browser tests in the original run, and each runtime drops the database it created.

4. **Install BIN.** Copy each script to a temporary name in BIN, then `mv` it into place, so running scripts never read a half-written file.

5. **Create the run ledger.**

   ```sh
   python3 scripts/swarm/run_state.py --run "$ART/run.json" --as orchestrator init --data '{"started_at":"<UTC time>","lifecycle":"orienting","inventory":{"threads":[],"sessions":[]},"ceilings":{"overall":4000000,"preparation":1000000},"consumed":{"overall":0,"preparation":0},"deadlines":{},"blockers":[],"reservations":{},"runs":{},"approvals":{},"dispatch_frozen":false}'
   ```

   Set ceilings to the token budget for the run. Create `$ART/questions.md` for rulings and lead questions.

6. **Write the briefs.** Adapt `prompts/phase-1/orchestrator-brief.md`, `pilot-lead-brief.md` and `builder-brief.md` to the new project. Write the first orchestrator handoff at `prompts/phase-1/orchestrator-handoff.md`.

7. **Start the watchdog and the first orchestrator.**

   ```sh
   nohup python3 scripts/swarm/orchestrator_watchdog.py "$ART" >/dev/null 2>&1 &
   ART="$ART" scripts/swarm/orchestrator-spawn.sh --start 1
   ```

   The orchestrator starts the lead with `interior-lead.sh --start` (set `REPO`, `BIN`, `LEAD_AGENT`, `LEAD_BRIEF` and `BUILDER_LOGS`). At each handoff it runs `scripts/swarm/orchestrator-spawn.sh <next number>`. Leads hand off through `interior-lead.sh --handoff-begin` and `--handoff-start`.

## Tests

Run the suite from the host project with its Python environment:

```sh
.venv/bin/python -m pytest -q -p no:randomly tests/swarm
```

Run standalone in this directory, 1306 tests pass. The other 59 need a host project and all pass in FloorInteriorDesignAndRenderer at `aadb2dfb`: the `task_env` and TypeScript comment-lint tests need `web/node_modules`, the reviewer-cwd and default-collection tests need the project tree, and the quality-dependency and mypy tests need its `pyproject.toml`.

## Known limits

- The scripts target macOS with WezTerm panes, Claude Code for orchestrators and leads, and the Codex CLI for builders.
- The host paths in step 2 are literal values, not settings.
- `gate_evidence.runtime()` assumes a local PostgreSQL server on the `/tmp` socket with `interior_migrator` and `app_runtime` roles.
