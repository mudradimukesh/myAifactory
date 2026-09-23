# Factory work

Read `agent-factory-design.md` and both files under `docs/adr/` before changing execution or acceptance behavior. ADR 0001 replaces Pi; ADR 0002 replaces Docker with fail-closed native macOS sandbox execution. Historical review findings remain evidence of the reviewed revision.

Use Node 24.19.x. Run the behavior checks described in `home/.agents/skills/verify-my-aifactory/SKILL.md`; its feature map identifies supported interfaces and unverified integrations.

Keep reusable skills under `home/.agents/skills/`. Worker-loaded skills are selected by `roles.json` and hashed by `skills.lock.json`. The repository verification skill is for maintainers and is not a worker capability.

Do not report process or sandbox fixtures as subscription-worker or application acceptance. Native runtime has no container-equivalent CPU, memory or PID quotas. Record remaining gaps in `docs/review-status.md`.
