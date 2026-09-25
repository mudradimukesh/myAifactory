# Add a local operator dashboard

The user requested visibility into orchestration, worker performance, credentials, budgets, and recovery. The existing system has validated file-backed state and native worker execution primitives, but no execution coordinator.

Run one loopback Node HTTP server. Its domain module projects Store state into dashboard views and owns validated settings and private credentials. The HTTP module validates requests and protects mutations with an origin check and session CSRF token. The browser uses the same server for static assets and API requests.

Usage starts with `npm run dashboard -- --root .factory`. `GET /api/dashboard` returns the current projection. `PUT /api/settings` saves future configuration. `GET /api/runs/:id/recovery` downloads a recovery brief. Existing Store revision locking protects control requests. The dashboard never advances acceptance status or interprets a worker claim as verification evidence.

Two independent design candidates compared this direct adapter with a durable command inbox. Independent review selected the direct adapter because the inbox has no consumer and would add storage and replay semantics without making recovery executable. The selected design adopts the alternative's separation between recorded state and observed process health. A saved active job is not a heartbeat.

We accept polling in exchange for using validated snapshots without another telemetry service. We accept local private-file secret storage in exchange for avoiding a second credential service. Credentials are not returned to the browser or included in recovery packets. Application keys remain separate from subscription worker credentials.

Budget settings apply to future configuration. Existing runs retain their original limits. Recovery briefs preserve spent allowances and candidate identity. Automatic context replacement and model escalation remain explicit gaps. GitHub issue synchronization is an explicit operator action.

The dashboard now starts one supervisor for the selected run. Pause suspends its worker group and prevents new work. Stop cancels the run and waits for owned processes to terminate. A persisted process record includes its PID, process group, and start time. Controls compare that identity with the live process before signalling it. Unconfirmed survivors remain visible and keep their ownership records for recovery.


## Approved records and operator answers

The dashboard projects approved business and domain specifications, architect tickets, and consequential question batches from run state. The coordinator uses `Store.recordCollaboration` to record these artifacts. Existing runs without them remain readable. Approved revisions and surfaced questions retain their history. Every mutation uses the Store lock and an expected revision.

`POST /api/runs/:id/answers` persists one complete batch with the operator name. It does not change acceptance or execution state. `POST /api/runs/:id/tickets/sync` explicitly synchronizes issues using private GitHub credentials. Issue bodies include approved specifications, requirements, and derived ticket progress. A stable ownership marker supports retries after interrupted remote writes. Recovery briefs include the same redacted collaboration records.

The UI keeps these records inside the selected run. Separate navigation pages would hide the run context needed to review answers. Saved answers require coordinator reconciliation, and remote progress requires an explicit sync.
