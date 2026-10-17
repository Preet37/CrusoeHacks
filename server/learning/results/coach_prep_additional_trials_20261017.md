# Additional Prep-only Coach trials — 2026-10-17

Both trials used the same eight train, two validation, and two locked-test cases. Each used a separate Coach store and one live Coach proposal. The locked cases were scored only after the validation selection. These are Prep prediction scores, not full-meeting accuracy.

| Trial | Validation v1 | Validation candidate | Selected | Locked v1 | Locked candidate | Outcome |
| --- | ---: | ---: | --- | ---: | ---: | --- |
| 1 retry | 0.4 | 0.4 | v1 | 0.8 | 0.8 | Candidate rejected: no validation improvement. |
| 2 | 0.4 | 0.4 | v1 | 0.8 | 0.8 | Candidate rejected: no validation improvement. |

Trial 1's first attempt stopped on Crusoe HTTP 503 `capacity_unavailable` during its first train replay and produced no scores. It is recorded separately. The retry completed normally. Neither candidate replaced the active prompt, and neither trial wrote the runtime `latest_metrics.json` feed.

Full raw results: `coach_prep_trial_1_retry_20261017.json`, `coach_prep_trial_2_20261017.json`; failed attempt: `coach_prep_trial_1_attempt1_20261017.json`.
