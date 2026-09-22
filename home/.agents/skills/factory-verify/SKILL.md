---
name: factory-verify
description: Author independent behavioral checks in the assigned check directory and assess runner results without repairing candidate source.
---

Read the approved brief, exact candidate identity, cumulative acceptance, and [testing quality](../factory-tdd/references/testing-quality.md).

Propose checks for observable acceptance behavior and gaps in existing coverage. Use the separate proposed-check directory and scratch for check code and artifacts. Keep candidate source read-only. Candidate application startup or functional failures go to Coordinator for Developer repair. Do not change the application to make a test run.

Return proposed check paths, their requirement mapping, independent expected values, required environment, and commands to Coordinator. Runner executes accepted checks and captures authoritative results. Inspect those results against this candidate and report missing coverage, infrastructure failures, or unresolved flakiness. A diagnostic rerun does not erase an earlier failure.

Keep evaluation cases and their disclosure status accurate. Do not change frozen criteria or treat a case exposed during repair as unexposed evaluation evidence. Do not dispatch agents. Changing to review responsibility requires a fresh invocation.

For each proposed check, hand off its requirement, input, independently justified expected result, execution requirements, and artifact path. For each executed result, identify the exact candidate and runner evidence. Keep verbose logs out of the summary.

Check whether removing the behavior under test would make the assertion fail. Empty or absent results can be correct for a specified rejection case; include a corresponding accepted case when that distinguishes genuine behavior from a function that always returns nothing. Observe meaningful response data or persisted effects instead of merely counting internal calls.
