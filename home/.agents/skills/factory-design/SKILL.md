---
name: factory-design
description: Design a bounded factory implementation slice or inspect evidenced architecture problems without changing candidate source.
---

Read the approved task, domain glossary, relevant ADRs, and affected source. Define the slice boundaries, public interfaces, failure behavior, applicable UI conventions, and test strategy. Confirm which observable behaviors and testing interfaces the brief authorizes.

Read [architecture improvement](references/architecture-improvement.md) when architecture review or refactoring is requested, coordinated changes expose tight coupling, a reproduced defect exposes unclear ownership or difficult testing, or repeated changes reveal the same structural problem. Otherwise design the assigned slice without a codebase-wide architecture review.

Write proposed designs and authorized prototype files only in assigned scratch. Request execution of an experiment through Coordinator and the runner. When another bounded worker would improve the assigned slice, use `factory-swarm` to propose a developer or reviewer task to Coordinator. The proposal does not authorize dispatch. Do not execute host commands.

Recommend an ADR when a decision has meaningful reversal cost, requires explanation to future maintainers, and involved a real trade-off, or the user requested one. Routine decisions belong in the task record.

Return affected interfaces and files, evidence, risks, verification requirements, and unresolved product decisions. A design proposal does not authorize implementation or a change to accepted domain rules.

Keep context limited to the assigned slice and dependencies needed to explain it. Return a design handoff that maps requirements to interfaces, proposed artifacts, risks, and independent verification needs. Reference existing ADRs and source evidence rather than duplicating them. Include failed experiments that would change the next worker's approach.
