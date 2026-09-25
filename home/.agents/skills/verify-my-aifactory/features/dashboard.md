# Operator dashboard

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
