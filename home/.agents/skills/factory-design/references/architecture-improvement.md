# Architecture improvement

The trigger is an explicit architecture task or observed structural friction in the assigned work. Relevant evidence includes coordinated changes across tightly coupled modules, unclear ownership exposed by a reproduced defect, difficult behavioral testing, or a repeated pattern in change history. Module count and style preferences alone are not defects.

Inspect the affected area and relevant dependencies using the glossary and ADRs. Use change history to select an area only when the task does not already identify one. Assess how many modules must be understood to explain one behavior, how much implementation detail an interface exposes, whether responsibilities have clear owners, and whether the behavior is testable through public interfaces.

A useful proposal may consolidate shallow modules so that one simpler interface hides their complexity. Compare that proposal against leaving the code unchanged. Consider compatibility, migration, data preservation, testing cost, and future change impact. Prefer the smallest change that addresses demonstrated friction.

The proposal identifies the trigger, supporting evidence, affected files, current responsibilities, proposed responsibilities and interfaces, expected testing effect, migration risks, and required verification. Identify ADRs affected by the change. Add a before-and-after diagram only when it clarifies the proposal. Unknown benefits remain hypotheses rather than measured improvements.
