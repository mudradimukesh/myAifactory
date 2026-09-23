# Add a local operator dashboard

The user requested visibility into orchestration, worker performance, credentials, budgets, and recovery. The existing system has validated file-backed state and native worker execution primitives, but no execution coordinator.

Run one loopback Node HTTP server. Its domain module projects Store state into dashboard views and owns validated settings and private credentials. The HTTP module validates requests and protects mutations with an origin check and session CSRF token. The browser uses the same server for static assets and API requests.

Usage starts with `npm run dashboard -- --root .factory`. `GET /api/dashboard` returns the current projection. `PUT /api/settings` saves future configuration. `GET /api/runs/:id/recovery` downloads a recovery brief. Existing Store revision locking protects control requests. The dashboard never advances acceptance status or interprets a worker claim as verification evidence.

Two independent design candidates compared this direct adapter with a durable command inbox. Independent review selected the direct adapter because the inbox has no consumer and would add storage and replay semantics without making recovery executable. The selected design adopts the alternative's separation between recorded state and observed process health. A saved active job is not a heartbeat.

We accept polling in exchange for using validated snapshots without another telemetry service. We accept local private-file secret storage in exchange for avoiding a second credential service. Credentials are not returned to the browser or included in recovery packets. Application keys remain separate from subscription worker credentials.

Budget settings are planning inputs until an execution owner enforces them. Existing runs retain their original limits. Recovery briefs preserve spent allowances and candidate identity. Automatic context replacement, model escalation, GitHub operations, and execution remain explicit gaps. No dashboard control signals a persisted PID.
