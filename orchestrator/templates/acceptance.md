# Acceptance for <new-goal>

Replace every placeholder before dispatch. This file defines the user's goal, not the implementation's preferred behavior.

- Objective: <observable user outcome>
- Scope and exclusions: <explicit boundaries>
- Allowed writes: <exact paths or bounded directories>
- Acceptance A1: <public behavior, independent oracle, failure cases>
- Acceptance A2: <regression behavior to preserve>
- Product review: <UI IDs, viewports, themes, required artifacts; omit for non-UI work>
- Coverage: 80% for a new goal unless explicitly overridden before the run.
- Mutation: 70% in the imported profile, with every contract-relevant survivor independently classified.
- Authority: <allowed models, repair scope, integration/push permissions and budget policy>

```json
{
  "feature": "replace-with-new-goal-id",
  "journey_specs": ["replace-with-project-e2e-spec"],
  "python_test_paths": [],
  "property_test_paths": [],
  "product_ids": [],
  "views": {},
  "required_artifacts": []
}
```
