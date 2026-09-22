# Factory review status

Reviewed on 2026-09-23. The factory remains incomplete and is not approved for operational use.

## Confirmed fixes

Candidate import now preserves NUL-delimited Git filenames. A reproduced leading-space filename bypassed the allowed-path check before the fix; a Unicode filename was incorrectly rejected. Tests now reject the disallowed path and read the approved imported file from candidate storage.

Process logs flush while the worker runs. Split credentials remain withheld until the redactor can distinguish a complete match from a longer shared-prefix secret. Tests read logs during execution and check literal redacted output.

Timeout cleanup kills surviving descendants even after the direct child's pipes close. The regression launches a descendant that ignores SIGTERM and checks process liveness after cleanup.

Source digests now include symlink targets. Persisted state validates nested attempts and evidence before reading or committing them. Recovery tests reject conflicting history and malformed evidence, repair only the final interrupted event, and exercise the state-root reservation.

Container management checks an ownership label and addresses the inspected immutable container ID. Container launch now enables stdin for worker prompts. The ownership test uses a local Docker fixture; real container launch remains unverified.

## Structural changes

Validation schemas now supply the canonical state, evidence, role, and worker-choice types. Bundle configuration rejects missing roles, unsupported tool names, malformed file records, and changed instruction contents. Reservation files are parsed against their schema. The duplicate runtime interface had no callers and was removed. Source formatting exposes statements previously packed onto single lines. No file approaches 1,000 lines.

The factory was moved into its own Git repository. Bundle resolution now starts at that repository's root. All seven locked role bundles load after the move. The package no longer advertises an unimplemented CLI command.

## Remaining blockers

The operator CLI and acceptance coordinator are absent. Schema-valid state is not proof of independent review, executed mandatory checks, a current artifact, or safe handoff. These gates must be demonstrated before operational acceptance.

Docker is unavailable on the inspected workstation. No prepared image, dedicated subscription-worker login, live prompt-delivery test, credential-refresh test, native sandbox test, or complete worker recovery trial has run. Discovery and architecture scratch-write permissions differ between providers and need a tested implementation that preserves candidate read-only access.

Upstream skill provenance and licenses remain unverified. Role tool metadata and native command permissions are not derived from one checked contract. The reviewer has not been calibrated with known-correct and seeded-defect candidates. The design's full acceptance matrix and product evaluation remain outstanding.

## Verification scope

The maintained [verification skill](../home/.agents/skills/verify-my-aifactory/SKILL.md) has three feature recipes. Independent source readers checked each map entry, and each recipe was executed locally. The skill explicitly records uncovered runtime paths and unknown usage behavior. It does not claim complete library or operational coverage.

The review used the requested design, rationale, blast-radius, behavioral verification, migration, sequencing, subtraction, and strict maintainability skills. `coupling-analysis` could not be located in the supplied skill directories; coupling was inspected directly without claiming that skill ran.

Historical evidence is limited to the supplied handoff, design, and subscription ADR. The imported factory had no Git history, issue tracker, team chat, production observability, error tracking, or analytics evidence. Do not infer implementation-time intent from code alone.
