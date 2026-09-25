# Operator dashboard

Running segment rows show last activity age. Recorded worker stalls show their reason and a next action to inspect the provider or capture and reconcile usage before retrying.

Run the HTTP behavioral suite with the Node executable selected in the parent skill:

```sh
"$FACTORY_NODE" --test tests/dashboard.test.ts tests/collaboration.test.ts
```

Use a disposable state root for browser verification. Start the server with:

```sh
"$FACTORY_NODE" src/dashboard-server.ts --root /tmp/factory-dashboard-browser --port 4317
```

Open the printed URL and verify these public workflows:

1. An empty root shows no runs and explains that live execution is unavailable.
2. Project setup saves a valid GitHub URL, brief, recipient, and model choices. Refresh and confirm they persist. An invalid URL is rejected without replacing saved settings.
3. Connections saves a synthetic application key and shows only its name. Refresh and confirm its value is absent. Remove it and confirm the name disappears. Use synthetic values, never real credentials, in evidence.
4. Budgets saves total attempts and a smaller verification reserve. Reject a reserve equal to total attempts. Confirm the displayed allocation after refresh.
5. Against fixture run state, inspect attempts, unknown usage, events, and actionable warnings. Download a recovery brief and confirm its candidate and spent counters match the saved run. Use a disposable run to exercise Start, Pause, Resume and Stop through the real HTTP dashboard. Confirm each state, enabled action, server response and saved run state. Stop requires confirmation and ends in a cancelled run with all controls disabled. Check conflict and launch failure responses without claiming that a worker ran.
6. At a narrow viewport, use every navigation item and form without horizontal scrolling. Check labels, keyboard focus, save feedback, and readable errors. Focus each run control before activation and confirm focus stays on the control bar after a redraw. Escape or Keep run must close the Stop dialog without cancelling the run.

7. Against a run populated through `Store.recordCollaboration`, expand both approved specification kinds and inspect approval evidence, requirements, and revision digests. Inspect architect ticket progress and linked attempts.
8. Answer a complete question batch and reload. Confirm the saved answers and named operator persist, while the run remains awaiting coordinator reconciliation. Submit against a stale revision and confirm the UI preserves the draft and asks for review.
9. GitHub sync tests use an injected HTTP transport. Confirm approved specifications appear in the outgoing issue body, retries reuse an owned issue, and an unrelated issue cannot be overwritten. A mock transport does not establish live repository permissions or remote delivery.


Retain the commands, API responses without secrets, and browser screenshots. HTTP fixtures establish local request behavior. The Claude sign-in dialog and native login integration are deferred and must not be marked verified. GitHub access, provider quota, model access, and automatic continuation remain separate unverified integrations.

For token usage, compare each segment's raw and normalized counters against the saved segment or active attempt meter. Check role totals, hand-off rows, unknown totals with known lower bounds, and current versus peak context. Update the disposable meter and confirm the existing one-second poll refreshes the progress bar. Check desktop and narrow layouts. The projection tests can run without loopback using `--test-name-pattern="segment projection|snapshot reads active" tests/dashboard.test.ts`. Browser assertions and screenshots require loopback and Chrome; projection tests alone do not establish visual acceptance.

Run `tests/supervisor.test.ts` alongside `tests/dashboard.test.ts` to check CLI run/resume supervisor events, exact JSON output formatting, conflict exit code 3, and dashboard process-group pause/resume. Pause must return HTTP 409 for a live unsupervised worker without changing state. Idle pause must report "Paused. Nothing was running." The CLI fixtures stop at awaiting_input and do not invoke a provider.
