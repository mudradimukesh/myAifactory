# Export verification

Verified on macOS on 2026-10-09. This branch starts from `myAifactory` remote main `8335137`; the source harness was extracted from `FloorInteriorDesignAndRenderer` `6d9bda6`. No product goal, product source, credential, live state or transcript was imported. Private source receipts were summarized rather than copied.

| Check | Result |
| --- | --- |
| Fresh Python venv and `pip install './orchestrator[dev]'` | Passed; Python 3.13.15, pytest 9.1.1 imported from the new venv |
| `orchestrator/.venv/bin/python -m pytest orchestrator/tests/swarm -q` | 95 passed, 0 failed, 0 skipped; 21.034 seconds |
| Ruff check and format | Passed for the exported scripts, tests and adapter template |
| zsh syntax | Lead entry and spawn scripts passed |
| JSON parsing | Hook settings and review schema passed |
| Factory Node version | v24.19.0 |
| Factory `tsc --noEmit` | Passed |
| Factory role bundle validation | Seven roles; digest `9feaaf5bdb3783991ce1d5ecd44ca1ddfba4d3d630dab072e764eb77ff3f4fce` |
| Factory `node --test tests/*.test.ts` | 23 passed, 0 failed, 0 skipped |
| Export path/domain scan | No original workstation paths, deployed temporary root or product goal identifiers in runnable kit files |

The Python suite includes real disposable Git repositories and push-retry scenarios, process ownership and cleanup, fake CLI/WezTerm handoffs, frozen and paused alert-hook invocations, coverage reports over changed files, state concurrency and review-history collision checks. These are harness regressions, not live subscription-worker or application acceptance.

Portability failures found during extraction were corrected: fixed paths, a forced proxy route, a fixed alert pane, application-only runtime provisioning, a resume argument containing an unevaluated path expression, tests referring to the source venv, and an application-specific remote alias. The new adapter boundary is tested for missing configuration and cleanup after failure. Goal-specific runtime and expiry checks still belong to the new target project.

No model call, live swarm launch, production deployment or new application acceptance was performed for this export. Hook settings and notification delivery must still be verified with the new operator's native CLI versions and terminal. The kit's workflow hooks are not an OS security boundary. Reviewer in-flight metering and stale mutation-output invalidation remain documented gaps in GUIDE.md. An independent review of the extraction remains outstanding.

Private local logs used for this record are retained by the packaging session. Future maintainers should keep their own command, revision, environment and exit-code receipts and update this record when the supported scope changes.
