# longrun knowledge map (compact; routed by phase, never injected wholesale)

| phase | doc | mechanism / source |
|---|---|---|
| planning | planning.md | one macro outcome, criteria default-FAIL, work-package floor (Anthropic long-running-agents post: initializer + fresh sessions + progress file) |
| verifier design | verifier_design.md | every verifier is a proxy; test for gaming; evidence kinds per criterion kind; criterion-level truth |
| builder | builder.md | evidence-first, no self-certification, blockers with proof |
| evaluator | evaluator.md | fresh context, read-only, single JSON object, cite evidence ids |
| loop diagnosis | loop_diagnosis.md | OpenHands StuckDetector patterns (repeat action/observation, repeated errors, alternation, monologue) + strategic stagnation |
| restart | restart.md | terminate contaminated trajectory, compact failure capsule, APPLY/PARTIALLY_APPLY/DISCARD (context-contamination restart model, arXiv 2605.08563) |
| harness modification | harness_modification.md | when to REBASE a contract; never widen scope silently |
| domain adapter | adapters.md | adapter contract: evidence kinds, baseline commands, guidance fragments |

Sources: anthropic.com/engineering/effective-harnesses-for-long-running-agents; github.com/anthropics/cwc-long-running-agents;
OpenHands openhands/controller/stuck.py; arXiv 2605.08563; AMAP-ML/LongHorizon-Harness (ADAPTed primitives: atomic state + flock, process-group kill, fresh episode).
