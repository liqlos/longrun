# Loop diagnosis
Operational (per session stream): identical action/observation pair >=3x; same error signature >=3x; alternating edit/revert on one file; >=5 edits to one file without evidence; repeated evaluation with unchanged inputs (skipped by the controller).
Strategic (across checkpoints): two checkpoints with no criterion delta and no new blocker; same failure signature twice without a new hypothesis; two repairs not moving the same criterion; docs/refactor-only work; spend without fresh evidence; repeated plan rewriting.
Response: stop continuation -> snapshot diff+evidence -> failure capsule -> one changed-strategy repair if justified -> else one fresh-context restart -> else RESET_RECOMMENDED / blocker / OWNER_JUDGMENT_REQUIRED.
