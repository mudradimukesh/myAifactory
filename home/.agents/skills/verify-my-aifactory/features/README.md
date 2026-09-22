# Factory verification map

This map covers the implemented library interfaces used by a coordinator. Follow the parent skill's launch and doctor steps first. Each drive starts a fresh Node test process; the tests own their disposable data and child processes.

- [Process execution](processes.md) covers output, redaction, timeouts, cancellation, and Docker ownership fixtures.
- [Candidate and state handling](candidates-state.md) covers exact allowed paths, candidate import, source identity, durable records, and recovery.
- [Worker contracts](workers.md) covers command construction, structured completion, usage, and failure parsing.

The planned operator commands in design section 11.1, including init, start, step, resume, and handoff, are unimplemented. No feature file claims they work. Live Docker stdin, native sandbox permissions, credential refresh, independent review calibration, and the complete acceptance path remain unverified. Add those journeys when their entry points exist, then run the full maintenance pass.
