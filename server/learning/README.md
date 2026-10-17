# Twin learning evaluation

The 12 scripted meetings in `meetings/` are fixed before evaluation: eight
train, two validation, and two locked test. Each JSON file contains a meeting
title, transcript lines, and human-written ground truth. The locked cases are
not included in the Coach prompt. The Coach sees train failures, proposes one
bounded prompt edit, and accepts it only if measured validation quality
improves without a scored regression. Locked cases are scored after that
decision. A proposed edit can be rejected; improvement is never assumed.

The deadline demo uses **Prep-only replay**: one real Crusoe Prep prediction
call per meeting. `prediction_score` is the one-to-one F1 between up to three
predictions and annotated keyword groups. `latency_ms` and `tokens` appear only
when measured. This score does **not** claim that the full meeting pipeline,
drawings, fact checks, or spoken replies improved. The full text-only
Listener→Conductor→BoardState replay is available separately but costs more
model calls.

From `server/`, with model keys in the ignored `.env`:

```sh
.venv/bin/python -m learning.replay --prep-only --split validation --output learning/runs/validation-v1.json
.venv/bin/python -m learning.replay --prep-only --split locked_test --allow-locked --output learning/runs/locked-v1.json
.venv/bin/python -m learning.evaluate --split locked_test --v1 learning/runs/locked-v1.json --v2 learning/runs/locked-v2.json --output learning/runs/locked-comparison.json
```

The comparison command requires **actual** observation files for both
versions. It writes a detailed artifact and a plain array of protocol `metric`
messages to `learning/runs/latest_metrics.json` for the live Scoreboard. To
replay the full mind path, omit `--prep-only`; a candidate prompt can be
provided with `--prompt listener=/path/to/candidate.md` (or another supported
agent name). Candidate text is loaded from a temporary file during replay;
the shared prompt files are restored afterward. Run artifacts and personal
memory under `runs/` are ignored by Git.
