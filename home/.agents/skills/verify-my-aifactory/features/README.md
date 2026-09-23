# Factory verification map

This map covers the implemented library interfaces used by a coordinator. Follow the parent skill's launch and doctor steps first. Each drive starts a fresh Node test process; the tests own their disposable data and child processes.

- [Operator dashboard](dashboard.md) covers setup, private credentials, budgets, recorded run inspection, and recovery downloads.
- [Process execution](processes.md) covers output, redaction, timeouts, cancellation, and the native sandbox boundary.
- [Candidate and state handling](candidates-state.md) covers exact allowed paths, candidate import, source identity, durable records, and recovery.
- [Worker contracts](workers.md) covers command construction, structured completion, usage, and failure parsing.

The planned operator commands in design section 11.1, including init, start, step, resume, and handoff, are unimplemented. No feature file claims they work. ADR 0002 replaces Docker with `LocalRuntime`; native sandbox tests require the actual supported macOS host. Live provider stdin, attempt-local credential refresh, independent review calibration, and the complete acceptance path remain unverified. Record native test results in the review status; this map is a recipe, not a passing test report. Add those journeys when their entry points exist, then run the full maintenance pass.
