# Factory work

Read `agent-factory-design.md` and `docs/adr/0001-subscription-native-workers.md` before changing execution or acceptance behavior. The ADR supersedes the design's Pi runtime choice.

Use Node 24.19.x. Run the behavior checks described in `home/.agents/skills/verify-my-aifactory/SKILL.md`; its feature map identifies supported interfaces and unverified integrations.

Keep reusable skills under `home/.agents/skills/`. Worker-loaded skills are selected by `roles.json` and hashed by `skills.lock.json`. The repository verification skill is for maintainers and is not a worker capability.

Do not report local process fixtures as live Docker, subscription-worker, or application acceptance. Record remaining gaps in `docs/review-status.md`.
