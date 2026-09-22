---
name: factory-tdd
description: Implement and repair one approved factory slice in the Developer candidate clone using behavior-based tests.
---

Read the approved task, exact base revision, cumulative acceptance criteria, and [testing quality](references/testing-quality.md).

For a bug, first reproduce the failure through the closest available end-user journey. Record the inputs, environment, and observed failure. If reproduction is blocked, report the blocker rather than claim the defect reproduced.

Implement one behavior at a time. Begin behavior changes with a failing test whose expected result comes from an independent requirement or reviewed example. Apply the smallest robust implementation, run relevant checks, and refactor when behavior is covered. Preserve cumulative regressions and agreed acceptance thresholds. Repair implementation defects returned by Coordinator.

Write only in the candidate clone and assigned scratch. Do not edit runner policies, acceptance evidence, approved check definitions, or state. Do not dispatch agents. Return specialist requests to Coordinator.

Submit a clean local candidate commit with its base and candidate identifiers, changed behavior, commands and results, evidence paths, deviations, and unresolved failures. A local pass or commit is a submission for independent verification, not acceptance or permission to integrate or release.

Keep the evidence handoff compact. Map each changed behavior to its requirement, candidate revision, relevant check result, and remaining limitation. Preserve detailed command output in artifact files and return their paths. A test should fail when the required behavior is removed or replaced by an empty result; an assertion that merely repeats a prompt or implementation constant does not demonstrate that behavior.
