# Unit briefs

Each slice gets a tester brief and a coder brief, and each unit gets a contract from `p1-architect`, a plan review brief and `p1-reviewer` packets. Tester and coder briefs are filled from the templates in `prompts/phase-1/roles.md` sections "2.2 Tester", "2.1 Coder" and "2.3 Reviewer". The templates keep the fields of `reports/Interior app phase 1.md` Appendix A and add the role, the frozen tests and the dispute route. The hand-off order is in `swarm-ops.md` section "Unit roles hand-off". A completion claim is not acceptance; acceptance is the gate.

## Phase 1 fill rules

1. The first line after the title is: "Read `docs/token-budget.md` in the main repo, then `prompts/phase-1/swarm-ops.md` sections "Overrides", "Unit roles hand-off" and "Jev for the lead". The Phase 1 rules and dated user rulings override the older document; its render-era roles, routing owners and Sonnet hand-off rewrite instruction do not apply." The next line: "Names, types and tests carry the meaning; the code you write has no comments."
2. One unit is one customer-visible behavior. If "Result" needs "and" between two behaviors, split the unit.
3. "Writable files" lists exact paths. Two concurrent units never share a path.
4. "Read" names one hand-off and exact sections, never "read the hand-offs". For Phase 1 the usual reads are one section of `prompts/phase-1/data-model.md`, `ui-spec.md` or `eval.md`, and one section of a research note in `research_notes/Interior app phase 1/` by absolute path.
5. "Observed" gives the fixture path, its SHA-256 and the measured value. A brief never names a sealed family.
6. "Accepted contract" says: plan IR v0.3 under ruling R1; every committed version passes `parse_plan` and stores its `assess_plan` result, and only `final_render_eligible` versions go to Phase 2; rulings R1 to R6 in `reports/Interior app phase 1.md`; the dated rulings in `prompts/phase-1/progress.md` that touch this unit, quoted with their numbers.
7. Work step 1 for a bug reproduces it through the public path: the `floorplan-render` CLI, the API, or the browser.
8. Commands are exact, in the coder template's step 5 and the tester template's step 6. The first `web/` unit defines `lint`, `typecheck`, `test` and `e2e` in `web/package.json` with the settings of gates G14 to G18 in `roles.md`, adds `stryker.config.json` with `break: 70`, and pins `vitest` and `@vitest/coverage-v8` at `^4.1` until stryker-js issue 6210 is fixed in a release. Blender tests run only at the lead's gate on the host.
9. "Host-only actions" lists what the lead does: dependency installs, network, git commits, Postgres service.
10. "Maximum spend" is the unit's share of the D1 allocation, or 0.
11. Each launch has a unique run tag. Slice results go to `/tmp/factory-fp-0925/swarm/interior-p1/<tag>-s<k>-<role>-result.md`; repair results add `-r<attempt>`. Reviewer packets list the exact result files and hashes for every included slice. Never overwrite an earlier result.
12. The contract file is the single source for each acceptance id; briefs quote it and add no new requirement.
13. The coder brief names the frozen tests commit, the `<tag>-s<k>-tests.sha256` files and the dispute route: a test the coder believes wrong is reported as BLOCKED with a counterexample, never edited.
14. The first `web/` unit pins `react`, `react-dom` and `react-konva` to exact versions with matching minors. In `web/package.json` it defines `api:types` as `openapi-typescript <api>/openapi.json -o src/api/schema.d.ts`. That file is generated and never edited by hand. Verify reproducible generation and compile its consumers. Exclude only this generated file from the handwritten-code comment and type-escape scans; keep strict checks on handwritten API consumers.
15. Slice briefs quote the contract's slice row and nothing else from later slices. Slice 1 starts a fresh Codex thread; slice k > 1 resumes it, with the brief for that slice only.
16. Who runs what: `p1-planner`, `p1-architect` and `p1-reviewer` are Claude subagents; the tester, coder, plan reviewer and designer user are Codex `gpt-6-astra`. No brief goes to a general-purpose agent.

## Reviewer brief

A reviewer is a fresh `p1-reviewer` Claude subagent call per mode: `tests` before the freeze, `code` after the gate, and `product` for parser and UI units. It gets the packet in its template, including the original drawing, overlay, saved IR and screenshots where they apply, and every open finding with its full description. It never gets the coder's or tester's prose beyond the tester's acceptance map. It writes `p1-review-2` JSON to `/tmp/factory-fp-0925/swarm/interior-p1/`: `<tag>-s<k>-tests-review.json` in mode `tests`, `<tag>-code-review.json` in mode `code` and `<tag>-product-review.json` in mode `product`, which `review_check.py` validates before the lead acts on it.

## Pilot test-map schema

For supervised pilot tests-first turns, red_check maps each red item to only assertion-failing nodes with one expected_failure substring and preservation=false. Put passing nodes in a separate <id>-PRES item with expected_failure=null and preservation=true. Pytest projects is ["pytest"] or omitted, never []. Preserve original failed maps and proof; corrections need recorded reasons and new assertion-red validation before freeze.

User rule (October 13, RULING573j, highest priority): never stop mid-turn to ask a question. If a frozen test, allowed path, acceptance line or brief instruction conflicts with another, or a scope question arises, record it in your handoff under "Open questions". Give the exact evidence (file:line, node id, failing message) and your proposed answer. Skip only the blocked item and continue every other in-scope item until all are done or you reach the handoff cutoff. Never edit a frozen test or write outside the allowed paths to get around a conflict. The lead answers open questions in the next brief. Stop early only on a preflight mismatch checked before any edit (wrong HEAD or unexpected dirty files), at the handoff cutoff, or on a guard denial after two retries.

Builder turns use ALLOWANCE=160000 with a handoff note and stop at 145000 new tokens per allocation (user ruling October 12). This supersedes the old 95000/85000 and 70000 allowances and 56000/70000 handoff cutoffs. Existing supervision enforces the allowance; the handoff cutoff is a worker instruction until the accepted supervisor repair adds its trigger.

For every tests-first turn, create the test map before broad exploration or drafting. Update it after each node change and keep a prewritten handoff current. Include every acceptance id; state pending or unexecuted checks honestly in the handoff. A partial map is required at budget stop but does not count as assertion-red evidence or PASS. Stop ordinary work at the handoff cutoff. Budget-stop finalization must use the accepted supervisor report-only reserve, with actual usage charged and original stop evidence preserved. Until that repair is accepted and installed, the lead registers a narrow fresh finalization continuation through existing ownership and admission after verified final accounting. Never reopen a released allocation or bypass a hard stop.

User rule (October 12): resolve every git merge or rebase conflict by following /Users/mukeshkumarmudradi/Documents/AI_Projects/github/dotfiles/home/.agents/skills/resolving-merge-conflicts/SKILL.md. Read it before touching the conflict. Resolve every hunk while preserving both intents, never abort, then run the checks and finish the merge or rebase.
