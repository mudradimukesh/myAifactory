# Independent review criteria

Use the current goal contract and exact-candidate packet. Do not infer requirements from implementation or amend acceptance to fit tests. The role definition supplies output paths and modes.

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
