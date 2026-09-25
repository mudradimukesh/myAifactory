# Factory verification map

This map covers the implemented library interfaces used by a coordinator. Follow the parent skill's launch and doctor steps first. Each drive starts a fresh Node test process; the tests own their disposable data and child processes.

- [Operator dashboard](dashboard.md) covers setup, private credentials, budgets, recorded run inspection, approved specifications, ticket progress, batch answers, explicit GitHub issue sync, and recovery downloads.
- [Process execution](processes.md) covers output, redaction, timeouts, cancellation, and the native sandbox boundary.
- [Candidate and state handling](candidates-state.md) covers exact allowed paths, candidate import, source identity, durable records, and recovery.
- [Worker contracts](workers.md) covers command construction, structured completion, usage, and failure parsing.

`src/factory-cli.ts` implements run initialization, steps, continuation, supervision, status, and visual-review registration. The dashboard starts, pauses, resumes, and stops a selected run. Verify these controls through the dashboard recipe and `tests/supervisor.test.ts`. ADR 0002 replaces Docker with `LocalRuntime`; native sandbox tests require the supported macOS host. Fixture checks do not establish live subscription access or product acceptance. Claude login integration is paused and its real authentication flow remains unverified. Record results in review status; this map describes verification steps, not passing results.
