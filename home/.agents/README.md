# Shared skills

Reusable project skills live in `home/.agents/skills/<name>/SKILL.md`. This repository mirrors a home-directory layout; creating a skill here does not install it into a user account.

The factory loads only skills selected by `roles.json`, with file hashes and reference dependencies in `skills.lock.json`. It must preserve the relative directory structure when assembling a bundle so shared references resolve. Role selection does not grant filesystem or shell authority; the runner enforces those boundaries.

These skills are original local instructions based on `agent-factory-design.md`. Upstream pins in the lock file record the design's research provenance. Those upstream sources and licenses were not fetched or verified for this bundle, and no upstream workflow or plugin is installed. This is a local adaptation milestone, not completion of the design's exact-upstream bootstrap requirement.

After editing a skill or reference, review its role boundaries and refresh its hash in the lock file. Evaluate changed behavior before promoting a bundle. A valid hash or frontmatter does not establish behavioral correctness.

The existing role skills also adapt inspected local handoff, context, testing, and review guidance. The lock records each inspected file hash and the adopted behavior. Original source workflows are not loaded into workers. Subscription-native Claude or Codex profiles select models separately from skills, with strongest-model orchestration and cheaper workers subject to the same acceptance criteria.
