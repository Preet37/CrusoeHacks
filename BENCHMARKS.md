# Nemotron benchmark: measured, reproducible probes

| Model | Task | Scored / attempted | Quality | p50 / p95 latency | Mean total tokens |
|---|---|---:|---:|---:|---:|
| Lightning | extraction | 12 / 12 | entity 0.502; edge 0.450; combined 0.432 | 662 / 2225 ms | 105 |
| Lightning | visual routing | 3 / 3 | 0.333 | 283 / 426 ms | 96 |
| Lightning | fact check | 2 / 2 | 2/2 caught | 944 / 1025 ms | 324 |
| Lightning | 3d validity | 1 / 1 | 0.000 | 3233 / 3233 ms | 530 |
| Lightning | paragraph completeness | 1 / 1 | node 1.000; edge 1.000 | 4200 / 4200 ms | 686 |
| Nano | extraction | 12 / 12 | entity 0.666; edge 0.167; combined 0.491 | 1028 / 1802 ms | 142 |
| Nano | visual routing | 3 / 3 | 0.667 | 391 / 429 ms | 96 |
| Nano | fact check | 2 / 2 | 2/2 caught | 769 / 818 ms | 283 |
| Nano | 3d validity | 1 / 1 | 1.000 | 2846 / 2846 ms | 582 |
| Nano | paragraph completeness | 1 / 1 | node 1.000; edge 0.900 | 3587 / 3587 ms | 722 |
| Super | extraction | 8 / 12 | entity 0.558; edge 0.875; combined 0.530 | 1486 / 2961 ms | 93 |
| Super | visual routing | 3 / 3 | 0.667 | 1119 / 1960 ms | 98 |
| Super | fact check | 1 / 2 | 1/2 caught | 3327 / 3327 ms | 326 |
| Super | 3d validity | 0 / 1 | — | — | — |
| Super | paragraph completeness | 0 / 1 | — | — | — |
| Super-VL | extraction | 12 / 12 | entity 0.813; edge 0.694; combined 0.766 | 1090 / 2119 ms | 101 |
| Super-VL | visual routing | 3 / 3 | 0.333 | 387 / 1485 ms | 97 |
| Super-VL | fact check | 2 / 2 | 2/2 caught | 1800 / 1878 ms | 266 |
| Super-VL | 3d validity | 1 / 1 | 0.000 | 3708 / 3708 ms | 448 |
| Super-VL | paragraph completeness | 1 / 1 | node 1.000; edge 1.000 | 3191 / 3191 ms | 757 |

The table reports the [full raw responses](server/bench/results/full_20261017.json), 76 real calls across four models. An API error, timeout, or malformed response stays in the denominator and is never assigned a made-up latency, token count, or quality result. Single-observation p95 values equal that observation's latency and must not be read as a population tail estimate.

## Method

Run `cd server && .venv/bin/python bench/run_bench.py --full --timeout 8 --output bench/results/full_20261017.json`. The harness calls the four configured Crusoe IDs: Nemotron 3.5 Lightning 30B A3B, Nemotron 3 Nano 30B A3B, Nemotron 3 Super 120B A12B, and Nemotron 3.5 Super VL. All probes use JSON output, reasoning disabled, at most 900 output tokens, up to four simultaneous requests, and an eight-second per-call bound in the full run. The API may retry transient server failures inside that bound. The raw file records each request, response or error, latency, token usage returned by Crusoe, deterministic score, and scoring labels. No LLM judges or simulated model responses are used.

The full matrix has 76 calls: 12 extraction fixtures, three visual routing fixtures, two fact-check fixtures, one 3D robot arm request, and one architecture paragraph, for each model. Extraction labels are explicitly enumerated in `server/bench/run_bench.py` from the 12 scripted meeting utterances. An entity is a literal technical component or concept; an edge is a directly asserted directed relation. Entity, edge, and combined precision/recall F1 penalize omitted and invented items. The table shows the macro mean of per-meeting F1 over scored responses. A fixed canonicalization table resolves common synonymous labels (for example, `auth service` to `auth`) and accepts source–relation–target triples. This scorer was corrected after inspecting the initial smoke responses; the saved smoke file was rescored without new model calls, and the full run uses the corrected scorer from the start.

Visual routing accuracy requires the expected operation exactly. Fact checking requires both the contradiction decision and the exact repository source citation. The 3D score invokes Twin's own scene and structural validator for a three-joint robot arm; an invalid scene scores zero, with the validation issue saved. Paragraph completeness compares 11 named components and 10 explicit directed relations in a shopping checkout spec, along with the requested 8–20 nodes and at least three layers. The fixed spec and expected graph are saved in the raw file. Latency percentiles are interpolated over successful API responses; errors are counted separately. Tokens are API-reported total tokens only.

The [20-call smoke raw responses](server/bench/results/smoke_20261017.json) cover one case for each model/task pair with a 25-second per-call bound. In that run, all four models caught the planted Redis-vs-JWT contradiction with the exact `demo_project/auth/login.py:18-24` citation. All four generated robot-arm scenes failed the strict structural validator. Super returned three `capacity_unavailable` 503 errors after bounded SDK retries; these are availability failures, not model-quality scores. This small smoke run informed the eight-second full-run bound; it is not used as a substitute for the 12-meeting extraction results.


## Interpretation

The full run found Nano the only valid three-joint scene (1/1); Lightning and Super-VL each generated invalid geometry (0/1), while Super returned no response within the bound. Super also lost four of 12 extraction calls to availability or the eight-second bound. Its 0.530 extraction F1 therefore describes only eight scored cases, not all 12. All returned fact-check responses cited the exact source, but Super completed only one of two cases. Paragraph scores are one case per model; all responded models recovered all 11 named components, while Nano missed one of 10 stated edges. These are probe measurements, not estimates of production reliability.

## Version N+1: additional Coach trials

Two additional real, independent Prep-only trials each proposed one prompt change. Both tied their baseline on validation (0.4 versus 0.4), so both candidates were rejected and v1 remained active. Locked-test scores were 0.8 for baseline and candidate in each trial; locked results did not drive selection. One initial attempt failed with HTTP 503 before scoring and its single retry completed. These runs demonstrate the rejection guard, not an improvement claim. [Trial summary and raw result filenames](server/learning/results/coach_prep_additional_trials_20261017.md).

## Scope limits at the 15:12 freeze

These are fixed-task probes, not maximum-size sweeps. Cold/warm separation, tool-calling reliability, fallback-rate instrumentation and PNG charts remain unmeasured. The new production paragraph path received two live checks that rejected invalid plans; its final ID-normalization fix passes automated checks but has no verified live success yet. Do not equate the semantic paragraph benchmark with end-to-end UI success.
