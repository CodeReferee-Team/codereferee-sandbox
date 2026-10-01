# Chaos evidence batch

`scripts/collect_chaos_evidence_batch.py` executes the controlled Kubernetes
fixture repeatedly and stores each real Pod Kill observation as one JSONL row.

```bash
python scripts/collect_chaos_evidence_batch.py --runs 5 --baseline-probes 20
```

The generated file is written under `data/actual/` unless `--output` is given.
Each row contains the unmodified `chaos-v1` response beneath `observation` and
therefore preserves the status, replicas, target and replacement Pod UIDs,
probe configuration, recovery window, error-rate denominator, events, and
logs.

This is real execution evidence, not an automatically approved fine-tuning
dataset. AI team members can use it for rule evaluation, explanation quality
evaluation, and reviewed dataset construction.
