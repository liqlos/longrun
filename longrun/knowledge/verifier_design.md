# Verifier design
- Every verifier is a proxy. Ask: how could a builder satisfy this check without meeting the outcome? Add a second evidence kind that closes that gap.
- User-facing criteria: never PASS on unit tests alone. Require screenshot/http/artifact/metric.
- Deterministic checks are run by the controller at the evaluated revision; their results are evidence, not verdicts.
- Update the verifier with the task: if a criterion is REBASEd, its checks and evidence kinds are re-reviewed.
