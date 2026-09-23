# Operator dashboard

Run the HTTP behavioral suite with the Node executable selected in the parent skill:

```sh
"$FACTORY_NODE" --test tests/dashboard.test.ts
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
5. Against fixture run state, inspect attempts, unknown usage, events, and actionable warnings. Download a recovery brief and confirm its candidate and spent counters match the saved run. A control request remains pending and does not claim a stopped process.
6. At a narrow viewport, use every navigation item and form without horizontal scrolling. Check labels, keyboard focus, save feedback, and readable errors.

Retain the commands, API responses without secrets, and browser screenshots. HTTP fixtures establish local request behavior. Native login, GitHub access, provider quota, model access, and automatic continuation remain separate unverified integrations.
