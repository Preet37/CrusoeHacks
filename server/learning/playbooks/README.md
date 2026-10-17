# Versioned prompt playbooks

`prep.md` supplies the pre-meeting brief. `coach.md` constrains the offline
Coach to one prompt edit. A Coach run snapshots the three live agent prompts as
v1 in its configured store, writes a sandbox v2 candidate, and records the
selected version after validation. The locked split is evaluated only after
that selection and never appears in the Coach's model input.
