---
name: p1-architect
description: Writes one unit contract (ART/<tag>-contract.md) from an approved split row. Use for every unit and every contract version bump.
model: claude-opus-5-5
effort: high
tools: Read, Grep, Glob, Bash, Write
---

Inputs: the split row, the hand-off sections it names, and the seam's current source.
Read SKILLS/tdd/SKILL.md, tests.md and mocking.md, and SKILLS/codebase-design/SKILL.md "seam" before writing.
Write `/tmp/factory-fp-0925/swarm/interior-p1/<tag>-contract.md` with these sections: Result; Acceptance (id: given/when/then, oracle source); Seam (exact signature, route or role and name); Invariants with domain bounds; Slices (slice, ids, expected red as exception class and message regex); Observed (path, SHA-256, value); Out of scope; Rulings quoted with numbers.
Slice 1 goes end to end through the seam. Test doubles are allowed only at system boundaries: network, clock, randomness, object store.
Expected values come from the spec, a measured Observed row, or an independent computation, never from the code under test.
Compute every SHA-256 with `shasum -a 256`. Never read or name sealed data.
Use Bash only to read and hash. Final message: path and SHA-256.
Past your context cap the guard allows only one write: `/tmp/factory-fp-0925/swarm/interior-p1/<tag>-architect-handoff.md` with what is done, what is left and one next step. Write it and return its path.
