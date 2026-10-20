# Unit roles: tester, coder, reviewer and the quality gates

Every unit runs a Codex `gpt-6-astra` tester and coder at medium effort, each one thread resumed across the unit's 1 to 3 slices, and the Claude `p1-reviewer` subagent at high effort. Contracts come from the Claude `p1-architect`, milestone splits from `p1-planner`, and the Codex plan reviewer (section 2.4) checks both. Tests follow the `tdd` skill (`SKILLS/tdd/SKILL.md`, `tests.md`, `mocking.md`) and bug fixes the `tdd-pstack` skill. The templates and gates below come from `research_notes/Interior app phase 1/09-astra-quality-opus.md` sections 2 and 3, with the amendments from `08-astra-quality-astra.md` in section "Amendments". The hand-off order is in `swarm-ops.md` section "Unit roles hand-off". `SWARM` means `/tmp/factory-fp-0925/swarm`, `REPO` the main checkout, `SKILLS` `/Users/mukeshkumarmudradi/Documents/AI_Projects/github/dotfiles/home/.agents/skills`.

## Role templates

All three templates follow the same pattern. Each step ends in a completion criterion. Evidence is labelled Observed, Assumed or Unknown. Each template states its stop rules and an exact report format. The lead fills every `<...>` field. These templates replace the Appendix A template for Phase 1. Appendix A's fields are kept so that archive comparisons stay valid.

### 2.1 Coder

```markdown
# <unit id> coder: <one customer-visible behavior>

Read `docs/token-budget.md` in the main repo, then `prompts/phase-1/swarm-ops.md` sections "Overrides", "Unit roles hand-off" and "Jev for the lead". The Phase 1 rules and dated user rulings override the older document; its render-era roles, routing owners and Sonnet hand-off rewrite instruction do not apply.
Names, types and tests carry the meaning; the code you write has no comments.
Role: coder. Model: gpt-6-astra. Effort: medium.
Slice: <k> of <n>. Frozen tests commit for this slice: <sha>.
Checkout: <absolute worktree path>. Base commit: <sha>. Frozen tests commit: <sha>.
Contract: <SWARM>/interior-p1/<tag>-contract.md, SHA-256 <hash>.
Frozen test files (run and read; their bytes stay as committed): <paths>.
Their hashes: <SWARM>/interior-p1/<tag>-s<1..k>-tests.sha256.
Writable files: <exact src or web/src paths>; <one coder-owned test file path, or none>.
Read: <one hand-off section by absolute path>.

## Result

Given <input and starting state>, the customer can <action> and observes <result>.
Done means acceptance ids <ids> pass through the frozen tests on your candidate.
Deferred work: <named units>.

## Evidence

Observed: <fixture path, SHA-256, measured value>, one row per fixture.
Assumed: <value, provenance, effect on the result>, or none.
Unknown: <missing evidence and how the product shows it>, or none.
Accepted contract: <IR version, interfaces, tolerances, ruling ids, quoted>.

## How the code must look

1. The frozen tests are the specification of done. Make them pass by implementing the
   behavior the contract states for every input of that kind, including inputs no test names.
   When you believe a frozen test contradicts the contract, report a test dispute (Stop rules).
2. Behavior is derived from the input data. Source files carry no fixture file name, family id,
   image hash or fixture-specific constant; the gate scans added lines for them.
3. Types: annotate every function fully. Parse external data (files, HTTP bodies, database rows,
   environment, CLI arguments) once at the boundary into a Pydantic model or frozen dataclass.
   Model variants as enums or Literal unions and end every match with `assert_never`.
   In TypeScript use discriminated unions, `unknown` at the boundary narrowed by a parser,
   `satisfies`, and a `never` check in every switch. The code type-checks with no `Any`, `cast`,
   `as`, `# type: ignore`, `@ts-ignore`, `@ts-expect-error` or non-null `!`.
4. Errors: raise a typed error where the failure happens, catch only an exception type you handle
   at that point, and let the rest propagate. A failed operation returns a typed failure or raises.
5. Simplicity: before adding a function, search `src/` and `web/src/` for an existing helper and
   reuse it. Add a function when it has two callers or names a domain concept. Keep each file
   under 1000 lines and each function under cyclomatic complexity 10.
6. You edit only the writable files. Any other file is a BLOCKED report naming the path.

## Work

1. Prove the environment and the red state. Run
   `.venv/bin/python -m pytest -q -p no:randomly <frozen test paths>` (web: `npm --prefix web test -- --run <paths>`).
   Complete when the output shows <N> collected and <N> failed with the reasons the contract
   states, and 0 errors and 0 skipped. Any other output is BLOCKED with that output.
2. For a bug, reproduce it through <CLI, API route or browser steps>.
   Complete when <command> shows <expected failure>. For a new feature, show the missing behavior
   through the same path.
3. List the existing functions and types you will reuse, with path:line.
   Complete when the list is in your notes for the report's "Reused" field.
4. Make this slice's frozen tests pass with the smallest change that is correct for every input
   the contract describes, and keep every earlier slice's tests passing. Do not restructure code
   you are not changing for this slice; refactoring requests come from the reviewer.
   Complete when every frozen test of slices 1..k passes.
5. Run the unit checks:
   Python: `.venv/bin/python -m pytest -q <frozen and coder-owned test paths> && .venv/bin/ruff check . && .venv/bin/ruff format --check <changed .py files> && .venv/bin/mypy --strict --follow-imports=silent <changed src files>`
   Web: `npm --prefix web run lint && npm --prefix web run typecheck && npm --prefix web test`
   plus `npm --prefix web run e2e -- <spec>` for UI units.
   Complete when every command exits 0; record collected, passed, failed, skipped counts.
6. Exercise the customer path <steps> and save <overlay, screenshot or response body> under
   <SWARM>/interior-p1/<tag>-artifacts/. Complete when the artifact exists and you have its SHA-256.
7. Write the result record. Complete when <SWARM>/interior-p1/<tag>-s<k>-coder-result.md (repairs add -r<attempt>) exists with
   every field below filled.

## Stop rules

- Every completion criterion met: Status PASS.
- The same assertion still fails after two different fixes: Status ISSUES. Report both fixes,
  the premise they shared and the last output. Make no third attempt on that premise.
- A frozen test contradicts the contract, or passing it requires special-casing an input:
  Status BLOCKED with a test dispute. Leave the test untouched.
- Three consecutive command runs with no change in which tests pass: stop, Status ISSUES,
  report the last output.
- A missing file, permission, dependency, service or network access: Status BLOCKED, name the
  exact item and ask one question.
- An ambiguity whose choice is reversible and inside your writable files: choose, record it
  under Assumed, and continue.
- At <soft context stop> tokens, make no new edits; run step 5 and write the record.
- Stopped by the launcher with `CONTEXT` and resumed for a hand-off: write only
  <SWARM>/interior-p1/<tag>-coder-handoff.md (slice, files changed, tests red or green with node
  ids, rejected approaches, one next step), then stop.

## Result record

Status: PASS | ISSUES | BLOCKED
Role: coder. Unit: <unit id>.
Code identity: base <sha>; frozen tests <sha>; candidate patch SHA-256 <hash of `git diff <base>`>.
Assertions: one line per acceptance id: <id> | <test node> | observed pass or fail | exit code.
Commands: one line per command: <command> | exit code | collected/passed/failed/skipped.
Artifacts: <path> | SHA-256.
Reused: <path:line> per reused helper.
Observed: <value, command or file that produced it>, one per line.
Assumed: <value, why, effect>, or none.
Unknown: <item>, or none.
Files changed: <paths>.
Test dispute: <test node> | <contract line quoted> | <input that shows the contradiction> | <proposed ruling>, or none.
Remaining work: <ids> and one exact next step, or none.
```

### 2.2 Tester

```markdown
# <unit id> tester: acceptance and property tests for <one behavior>

Read `docs/token-budget.md` in the main repo, then `prompts/phase-1/swarm-ops.md` sections "Overrides", "Unit roles hand-off" and "Jev for the lead". The Phase 1 rules and dated user rulings override the older document; its render-era roles, routing owners and Sonnet hand-off rewrite instruction do not apply.
Names, types and tests carry the meaning; the code you write has no comments.
Role: tester. Model: gpt-6-astra. Effort: medium.
Slice: <k> of <n>; ids: <slice ids>; seam: <contract seam>. Test only at this seam; a test at
any other seam is a BLOCKED report. Write tests for this slice's ids only, never for later slices.
Checkout: <absolute worktree path>. Base commit: <sha>.
Contract: <SWARM>/interior-p1/<tag>-contract.md, SHA-256 <hash>.
Writable files: <exact test paths>; <one src stub path, or none>.
Read: the contract, <the seam's current source by path, read-only>, <one hand-off section>.
You work before the coder or apart from it. No implementation of this behavior exists for you to read.

## Contract summary

Acceptance ids: <id: given/when/then, quoted from the contract>, one per line.
Seam: <function signature, HTTP route and status codes, CLI command, or browser role and name>.
Invariants: <properties the contract states: round trip, idempotency, ordering, tolerance,
invariance under file rename, translation, scale or input order>.
Observed: <fixture path, SHA-256, measured value>, one row per fixture.
Out of scope: <behaviors this unit leaves alone>.

## How the tests must look

1. Each test calls only the seam. It survives any correct rewrite of the internals.
2. Each expected value is a literal from the contract, a measured value from an Observed row,
   or an independent computation in the test written from the contract's definition.
   The code under test never produces an expected value.
3. Each property test uses Hypothesis (Python) or fast-check (TypeScript) with strategies built
   from the contract's domain types and bounds. Health checks stay on.
4. Tests are deterministic: `tmp_path` for files, injected clocks or `time.process_time` per the
   contract, fixed seeds, no network, no sleep. Playwright uses role locators and web-first
   assertions with expect timeouts.
5. One acceptance id maps to one named test node; property tests name the invariant they check.
6. Test doubles only at system boundaries (network, clock, randomness, object store, Supabase,
   OpenAI, Jev); never for a module in `src/` or `web/src/`.
7. No expected value recomputes what the code computes (a tautological test).

## Work

1. Map each acceptance id to its contract line and test node name.
   Complete when the table has one row per id and every row quotes a contract line.
2. If the seam is a new Python symbol, write its typed signature from the contract in the one
   stub path, with body `raise NotImplementedError`.
   Complete when `.venv/bin/mypy --strict --follow-imports=silent <stub path>` exits 0.
3. Write one test for the current slice's single observable rule at its agreed seam. Use either
   an example test or a property test, with an independent oracle. Review it, prove it red and
   make it green before writing another test. Additional rules or invariants become later slices.
   Split the unit if it needs more than three slices.
   Complete when the test's expected value cites a contract line, an Observed row or an
   independent computation.
4. If the slice's rule is an invariant, its test is a property test whose inputs differ from every
   fixture (generated geometry, renamed files, shuffled input order).
   Complete when the test is a property test, or the slice's rule is not an invariant.
5. Write down three plausible wrong implementations: one that hard-codes a fixture answer, one
   off-by-one or swapped-axis, and one that returns a success-shaped default. For each, name the
   test that fails on it. Complete when each of the three names a killing test node.
6. Run the tests on the base:
   `.venv/bin/python -m pytest -q -p randomly --count=3 --repeat-scope=session <test paths>`
   (web: `npm --prefix web test -- --run <paths>` three times; `npm --prefix web run e2e -- <spec> --retries=0`).
   Complete when every acceptance and property test fails for its stated reason in all three runs
   (assertion mismatch, HTTP status, missing element, or NotImplementedError from the stub),
   with 0 collection errors and 0 skipped, and the failing set is identical across runs.
7. Run the gate's banned-construct scan on your files:
   `<SWARM>/bin/diff_lint.py --base <base sha> --paths <test paths>`.
   Complete when it exits 0.
8. Write the result record. Complete when <SWARM>/interior-p1/<tag>-s<k>-tester-result.md (repairs add -r<attempt>) exists
   with every field below filled.

## Stop rules

- Every completion criterion met: Status PASS.
- The contract leaves an expected value undetermined, or two contract lines conflict: Status
  BLOCKED. Quote both lines and propose one value with its source.
- A test cannot fail on the base for a stated reason (the behavior already exists): Status
  ISSUES with the passing output; the lead decides whether the unit is needed.
- Two attempts at a deterministic test for the same id still vary across runs: Status ISSUES with
  both outputs.
- A missing file, dependency or permission: Status BLOCKED, name the exact item, ask one question.
- At <soft context stop> tokens, make no new tests; run step 6 and write the record.
- Stopped by the launcher with `CONTEXT` and resumed for a hand-off: write only
  <SWARM>/interior-p1/<tag>-tester-handoff.md, then stop.
- Bug unit (`tdd-pstack`): reproduce through the CLI, API or browser first. If a failing test
  would be a bad test per `SKILLS/tdd-pstack/SKILL.md` "If a Failing Test Is Impractical" (it
  mostly tests mocks, encodes implementation details, depends on timing, or needs expensive
  infrastructure for a small fix), write <SWARM>/interior-p1/<tag>-no-test.md with the reason,
  the closest check command and its expected output on the base, and report PASS.

## Result record

Status: PASS | ISSUES | BLOCKED
Role: tester. Unit: <unit id>.
Code identity: base <sha>; test patch SHA-256 <hash>.
Test files: <path> | SHA-256, one per line.
Acceptance map: <id> | <contract line quoted> | <test node> | oracle: spec_literal | observed_fixture | independent_computation | property.
Invariants: <invariant> | <test node> | strategy bounds.
Wrong implementations: <description> | <killing test node>, three lines.
Red on base: <test node> | <exception class> | <message regex>, one per line; runs 3/3 identical: yes | no.
Observed: <value and source>, one per line.
Assumed: <value, why, effect>, or none.
Unknown: <item>, or none.
Contract questions: <quoted lines and proposed value>, or none.
```

### 2.3 Reviewer

The reviewer runs in one of three modes. `tests` reviews the frozen tests before the coder starts. `code` reviews the gated candidate. `product` keeps the visual packet that the current reviewer brief describes (`prompts/phase-1/builder-brief.md` section "Reviewer brief").

```markdown
# <unit id> reviewer, mode <tests | code | product>

Role: reviewer (`p1-reviewer`). Model: claude-opus-5-5. Effort: high. Writes only its review JSON.
Checkout: <absolute gate worktree path>. Base <sha>. Candidate <sha>.
Packet, all under <SWARM>/interior-p1/:
- <tag>-contract.md (the specification you judge against)
- <tag>-diff.patch (tests mode: the test patch; code mode: base to candidate)
- <tag>-s<k>-tester-result.md acceptance map for every included slice, each with its SHA-256 (tests and code modes)
- <tag>-gate.json (code mode: every gate's command, exit code, value and threshold)
- <tag>-survivors.json (code mode: surviving mutants on changed lines)
- product mode: drawing, overlay, saved IR, issues, screenshots
- open findings with full text: <list>
The coder's report is not in the packet. Judge the artifacts.

Your output is one JSON object matching <SWARM>/bin/review-schema.json. Report a finding when
it changes correctness, a contract requirement, type safety, test validity, determinism, or a
rule listed in step 4. Lint and format issues are already gated; leave them out.

## Work

1. Statuses. For each open finding id, read the evidence and set fixed, partly_fixed, not_fixed
   or not_assessed. Complete when every open id has exactly one status entry with evidence.
2. Acceptance. For each acceptance id, find its test node, quote the contract line it checks,
   and classify coverage (asserted, weak, missing) and oracle source (spec_literal,
   observed_fixture, independent_computation, property, from_code_under_test, none).
   An oracle from code under test, or an assertion that would pass if every import returned
   undefined, is `weak`. Complete when every id has one entry.
3. Over-specification (tests mode and code mode). For each test, check that every assertion
   traces to a contract line. An assertion on internals, call order, private names or a value
   the contract leaves free is a major finding in category test_quality.
   Complete when every test node is traced or has a finding.
4. Checks (code mode). Fill each check with pass, fail or not_assessed and evidence:
   frozen_tests_unchanged (hash file), no_fixture_literals, no_special_case_branch (a branch on
   a fixture-specific value or shape), no_skip_or_xfail, no_operator_or_equality_override,
   no_broad_except_or_success_fallback, no_type_escape (Any, cast, as, ignores), no_comments,
   deterministic_tests (sleep, wall clock, unseeded randomness, shared state, order coupling).
   Complete when all nine checks have a result.
5. Spec axis (code mode). Read the diff against the contract. Report a missing requirement, a
   wrong implementation, or behavior outside the unit, quoting the contract line.
   Complete when every contract line in "Result" is covered by a code location or a finding.
6. Standards axis (code mode). Refactoring is requested here and only here: name the refactor as the fix. Report: a new helper that duplicates an existing one (name both
   path:lines), a wrapper with one caller, a bag of optional fields where a union fits, a parse
   outside the boundary, a file over 1000 lines, a function over complexity 10.
   Complete when each changed file has been read once in full.
7. Mutants (code mode). For each survivor, mark equivalent (no input distinguishes it; state
   why), test_gap (name the input that would kill it) or out_of_scope (line outside the
   contract). Complete when every survivor has a disposition.
8. Decide merge. merge is false when any finding is blocker or major, any acceptance entry is
   weak or missing, any check is fail, or any survivor is a test_gap on a line the contract
   covers. Put a question needing the lead's decision in needs_ruling.
   Complete when the JSON validates against the schema.

Label every finding's evidence_kind: observed when you ran or read the thing, inferred otherwise.
Severity: blocker breaks an acceptance id, auth or data integrity, or cheats a test; major breaks
a contract line, typing, determinism or test validity; minor is a maintainability cost with a named fix.
Box is [x0, y0, x1, y1] in view pixels; [0, 0, 0, 0] means the whole view or no view.
Stop rules: when the packet lacks an item a step needs, mark that step's entries not_assessed,
set merge false and name the missing item in needs_ruling.
```

Proposed `review-schema.json` v2 (it replaces `REPO/scripts/swarm/review-schema.json`):

```json
{"type":"object","additionalProperties":false,
 "required":["schema_version","mode","unit","base_sha","candidate_sha","statuses","acceptance","checks","mutants","findings","needs_ruling","merge"],
 "properties":{
  "schema_version":{"type":"string","enum":["p1-review-2"]},
  "mode":{"type":"string","enum":["tests","code","product"]},
  "unit":{"type":"string"},
  "base_sha":{"type":"string","pattern":"^[0-9a-f]{40}$"},
  "candidate_sha":{"type":"string","pattern":"^[0-9a-f]{40}$"},
  "statuses":{"type":"array","items":{"type":"object","additionalProperties":false,"required":["id","status","evidence"],"properties":{"id":{"type":"string"},"status":{"type":"string","enum":["fixed","partly_fixed","not_fixed","not_assessed"]},"evidence":{"type":"string"}}}},
  "acceptance":{"type":"array","items":{"type":"object","additionalProperties":false,"required":["id","test_node","contract_ref","coverage","oracle","evidence_kind","evidence"],"properties":{"id":{"type":"string"},"test_node":{"type":"string"},"contract_ref":{"type":"string"},"coverage":{"type":"string","enum":["asserted","weak","missing"]},"oracle":{"type":"string","enum":["spec_literal","observed_fixture","independent_computation","property","from_code_under_test","none"]},"evidence_kind":{"type":"string","enum":["observed","inferred"]},"evidence":{"type":"string"}}}},
  "checks":{"type":"array","items":{"type":"object","additionalProperties":false,"required":["check","result","evidence"],"properties":{"check":{"type":"string","enum":["frozen_tests_unchanged","no_fixture_literals","no_special_case_branch","no_skip_or_xfail","no_operator_or_equality_override","no_broad_except_or_success_fallback","no_type_escape","no_comments","deterministic_tests"]},"result":{"type":"string","enum":["pass","fail","not_assessed"]},"evidence":{"type":"string"}}}},
  "mutants":{"type":"array","items":{"type":"object","additionalProperties":false,"required":["mutant","file","line","disposition","evidence"],"properties":{"mutant":{"type":"string"},"file":{"type":"string"},"line":{"type":"integer"},"disposition":{"type":"string","enum":["equivalent","test_gap","out_of_scope"]},"evidence":{"type":"string"}}}},
  "findings":{"type":"array","items":{"type":"object","additionalProperties":false,"required":["id","axis","view","box","file","line","severity","category","evidence_kind","text"],"properties":{"id":{"type":"string"},"axis":{"type":"string","enum":["spec","standards","test","product"]},"view":{"type":"string"},"box":{"type":"array","items":{"type":"integer"},"minItems":4,"maxItems":4},"file":{"type":["string","null"]},"line":{"type":["integer","null"]},"severity":{"type":"string","enum":["blocker","major","minor"]},"category":{"type":"string","enum":["fidelity","geometry","asset","material","light","camera","noise","site","parse","ir","api","data","auth","ui","accessibility","eval","tooling","typing","simplicity","test_quality","flakiness","overfitting"]},"evidence_kind":{"type":"string","enum":["observed","inferred"]},"text":{"type":"string"}}}},
  "needs_ruling":{"type":["string","null"]},
  "merge":{"type":"boolean"}}}
```

Every property is required and every optional value is nullable. That follows the strict structured-output convention, which I infer is how Codex `--output-schema` applies it. I have not confirmed that strict mode supports `minItems` and `maxItems`, so `review_check.py` re-checks box length, SHAs, ids and bounds in code either way.

### 2.4 Plan reviewer (Codex)

Role: plan reviewer. Model: gpt-6-astra. Effort: high. Sandbox: read-only. It reviews what Claude wrote: each milestone split from `p1-planner`, each unit contract from `p1-architect`, and each lead ruling that changes a contract. Its check table is `research_notes/Interior app phase 1/11-loop-adapt-opus.md` F13 and its output schema is F14 (`plan-review-schema.json`, validated by `plan_review_check.py`). The brief gives the artifact path and SHA-256, the check table for that artifact kind, and these rules: quote the artifact line for each finding; `approve` is false when any blocker or major finding exists; do not rewrite the artifact.

### 2.5 Designer user (Codex)

Role: designer user. Model: gpt-6-astra. Effort: high. Sandbox: read-only, with Playwright MCP (`swarm-ops.md` Tooling item 13). Persona, launch line, task rules and `ux-schema.json` are in `swarm-ops.md` section "Designer user". Tasks name goals, never controls; each task set runs 3 times per viewport in fresh sessions. `ux_check.py` verifies every claimed success through the API before the lead reads a finding.

## Gates

Source ids F<n> are findings in note 09 section 1. The lead runs the gates in `p1-gate` on the merged candidate. The contract lists each gate as applicable or not applicable, with a reason checked by the Astra plan reviewer. Missing infrastructure that the unit must create is a failure. A gate for a language or application absent from the candidate may be not applicable. Record that disposition explicitly; never report an unrun gate as passed. Before review, `scripts/swarm/unit_gate.sh <tag> <base> <candidate>` runs G1 to G18 and records every applicable result in `<tag>-gate.json`, one record per gate with command, exit code, measured value and threshold. All applicable command checks and numerical thresholds must pass. Survivor dispositions remain pending until code review. After review, G19 validates the required reviews and survivor dispositions against the same candidate. Merge requires both stages to pass. It runs every gate even after one fails, so a single repair round sees all failures (inference from F3, stale verification). Below, `PY` = `.venv/bin/python`, `CHANGED_PY` = changed `.py` files under `src/`, `CHANGED_TS` = changed files under `web/src/`, and `ART` = `SWARM/interior-p1`.

Slice records: each slice has a predecessor SHA, tests commit, frozen file list and hash file `ART/<tag>-s<k>-tests.sha256`. Slice 1 uses the unit base; slice k uses the green candidate of slice k-1. Each slice writes new test files and leaves earlier frozen files unchanged. G1 verifies every slice's frozen hashes. G2 runs the new test on its predecessor plus its tests commit, verifies the stated failure, and verifies all accepted tests pass on the candidate.

| Id | Gate | Exact command | Threshold | Source |
|---|---|---|---|---|
| G1 | Frozen tests unchanged | `shasum -a 256 -c ART/<tag>-s<k>-tests.sha256` for every slice k, and `git diff --name-only <last tests commit> <candidate> -- tests web/tests web/e2e` | Every slice's hashes OK; the diff lists only the coder-owned test file | F10-F11 |
| G2 | Red on predecessor, green on candidate | For each slice k, `PY -m pytest -q -p no:randomly <slice-k nodes>` on its predecessor plus its tests commit; then every frozen node on the candidate | Each slice's new nodes fail on its predecessor for the reason in its red table; every frozen node passes on the candidate | `swarm-ops.md` section "Unit roles hand-off", step 7 |
| G3 | Acceptance ids collected and passed | `PY -m pytest -q -p no:randomly <frozen nodes> --junitxml=ART/<tag>-junit.xml && SWARM/bin/acceptance_check.py --junit ART/<tag>-junit.xml --ids ART/<tag>-ids.txt` | Every id passed; 0 skipped, 0 xfail, 0 errors | F2 |
| G4 | Lint and format | `.venv/bin/ruff check . && .venv/bin/ruff format --check CHANGED_PY` | 0 violations | `pyproject.toml:33-39` |
| G5 | Complexity on changed files | `.venv/bin/ruff check --extend-select C90,PLR0911,PLR0912,PLR0913,PLR0915 --config "lint.mccabe.max-complexity = 10" CHANGED_PY` | 0 violations; a violation that already exists in a touched file gets a waiver row in `progress.md` with file:line, or a refactor unit first | F31 |
| G6 | Strict types (Python) | `.venv/bin/mypy --strict --follow-imports=silent CHANGED_PY` with `[tool.mypy] plugins = ["pydantic.mypy"]` | 0 errors | F30 |
| G7 | Banned constructs and fixture literals | `SWARM/bin/diff_lint.py --base <base> --candidate <candidate>` | 0 added comments (Python `tokenize`, TypeScript scanner), 0 `Any`, `cast(`, `# type: ignore`, `# noqa`, `except Exception`, `pytest.skip`, `xfail`, `suppress_health_check`, `@ts-ignore`, `@ts-expect-error`, `eslint-disable`, `.only`, `.skip`, `test.fixme`, `waitForTimeout`; 0 added `src/` or `web/src/` lines containing a dev family id, fixture file stem or SHA-256 from `corpus/manifest-p1-dev.csv` and `tests/fixtures/` | F4, F12, F15 |
| G8 | File size | `SWARM/bin/diff_lint.py --max-lines 1000` (same run) | Each changed file at most 1000 lines | F31 |
| G9 | Hypothesis CI profile | `CI=1 PY -m pytest -q <property test files>` | 0 failures; each contract invariant has at least 1 `@given` node (tester map) | F21, F26 |
| G10 | Full suite, random order | `PY -m pytest -q -p randomly` (record the printed seed) | 0 failures | F25, F26 |
| G11 | Flake repeat (Python) | `PY -m pytest -q -p randomly --count=5 --repeat-scope=session <unit test paths>` | 0 failures in 5 sessions; timing tests run alone (`swarm-ops.md` section "Gate", step 3) | F25, F26 |
| G12 | Changed-line coverage | `PY -m pytest -q --cov=src --cov-branch --cov-report=xml:ART/<tag>-cov.xml && .venv/bin/diff-cover ART/<tag>-cov.xml --compare-branch=<base> --fail-under=90` | At least 90% of changed lines covered | Inference: a cheap filter before mutation |
| G13 | Mutation (Python) | `SWARM/bin/mutation_gate.py --changed CHANGED_PY --threshold 70 --out ART/<tag>-survivors.json` (runs `mutmut run` limited to the changed modules, then `mutmut export-cicd-stats`, and scores killed plus timeout over killed plus timeout plus survived, with no-test mutants counted as survived) | Score at least 70% on changed modules, and every survivor on a changed line has a reviewer disposition; a `test_gap` on a contract line blocks merge | F23, F24, F29 |
| G14 | Lint (web) | `npm --prefix web run lint` = `eslint . --max-warnings=0` with typescript-eslint `strictTypeChecked`, `consistent-type-assertions: {assertionStyle: "never"}`, `no-explicit-any`, `no-non-null-assertion`, `switch-exhaustiveness-check`, `complexity: 10`, `max-lines: 1000`, and the Playwright and Vitest plugins' focused, skipped and wait-for-timeout rules | 0 errors, 0 warnings | F31; https://typescript-eslint.io/users/configs/ |
| G15 | Types (web) | `npm --prefix web run typecheck` = `tsc -b` with `strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `noImplicitOverride`, `noFallthroughCasesInSwitch`, `verbatimModuleSyntax` | 0 errors | F31 |
| G16 | Unit tests (web), shuffled and repeated | `for s in 1 2 3 4 5; do npm --prefix web test -- --run --retry=0 --sequence.shuffle.files --sequence.shuffle.tests --sequence.seed=$s \|\| exit 1; done`, with fast-check seeded in the Vitest setup | 0 failures in 5; coverage lines at least 90 and branches at least 85 | F27 |
| G17 | E2E repeat | `npm --prefix web run e2e -- <unit spec> --retries=0 --repeat-each=5 --workers=1` with `forbidOnly: true` | 0 failures; at milestone ends `--repeat-each=20` on the representative suite, per `prompts/phase-1/eval.md` section "Browser tests" | F25, F27 |
| G18 | Mutation (web) | `npx --prefix web stryker run --mutate "<CHANGED_TS comma list>"` with `coverageAnalysis: "perTest"`, `thresholds: {high: 80, low: 70, break: 70}`, Vitest pinned `^4.1` | Score at least 70; the log line "Ran N tests per mutant" has N above 0 | F28 |
| G19 | Reviewer JSON | `SWARM/bin/review_check.py --review ART/<tag>-code-review.json [--review ART/<tag>-product-review.json] --gate ART/<tag>-gate.json --ids ART/<tag>-ids.txt` | Schema valid; `candidate_sha` matches the gate; every acceptance id appears once; every open finding id has a status; boxes are inside view bounds; merge is treated as false if any blocker or major, weak or missing coverage, failed check, or test_gap survivor on a contract line exists | F6 |

Milestone-end gates, run once per milestone:
- `PY -m pytest -q --hypothesis-profile=deep <all property tests>`. The `deep` profile is registered in `tests/conftest.py` with `derandomize=False` and `max_examples=2000`. Threshold: 0 failures. A new counterexample becomes a unit and is pinned with `@example`.
- G13 and G18 over every module changed in the milestone, at the same 70 threshold.
- The sealed evaluation in `eval.md` sections "Sealed gates" and "Browser tests" stays as written.

Threshold choice:
- 70 for mutation and 90 for changed-line coverage are inference. Google surfaces survivors in review rather than scoring them (F23), so the score is a floor, and the reviewer's per-survivor disposition carries the judgment.
- Equivalent-mutant labels from an LLM run at precision 0.79 in ACH (F24). The lead therefore spot-checks one `equivalent` disposition per unit by reading the mutant.

## Amendments

From note 08 section 4, applied over the templates and gates above:

1. Red first applies to new or repaired behavior only. Preservation tests pass on the base and the candidate. A behavior-preserving refactor proves itself with equivalence and mutation checks instead of a manufactured base failure. A collection error, missing dependency, skip or xfail is never red evidence.
2. Builders cannot commit. The lead collects every owned file the worker changed, tracked and untracked, checks it against the brief's writable list, and commits the patch on the host before integration. A branch with no collected patch is not a delivery.
3. The tester counts toward the two-writer limit. A tester on one unit may run beside a coder on another when their files, databases and artifact directories are disjoint.
4. Code decides status enums, ids, hashes, exit codes, ownership and budgets. Jev gets only ambiguous prose judgments, with an "unknown, lead decides" option; low confidence, malformed output, timeout or budget refusal returns the decision to the lead and never accepts anything.
5. Before the first product unit, `p1-reviewer` is calibrated on the seeded-defect and judgment seeds (`swarm-ops.md` Tooling items 11 and 15), and the Astra plan reviewer on the plan seeds. Each must flag every seed in 3 of 3 runs and give the clean candidate 0 blocker or major findings in 3 of 3.
6. Per unit, record separately: process exit, role completion, each gate result, review verdict and lead acceptance. Per prompt revision, track first-gate acceptance rate, rework count, escaped defects and cost per accepted unit in `progress.md`. The 70 mutation and 90 coverage thresholds are starting points; change them only by a dated ruling backed by those numbers.
