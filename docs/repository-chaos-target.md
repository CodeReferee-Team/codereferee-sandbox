# Repository Chaos Target Contract

QuickByte is a proof-of-concept target, not a product-specific execution path.
The Pod Kill observer accepts any Kubernetes workload that has already been
deployed into an isolated namespace.

## Target file

Pass a JSON target file with `--target-file`. This keeps a repository's
deployment facts outside the observer source code.

```json
{
  "repository": {
    "url": "https://github.com/example/service.git",
    "branch": "main",
    "commitSha": "optional-resolved-commit"
  },
  "target": {
    "name": "api",
    "namespace": "codereferee-run-<request-id>",
    "deployment": "api",
    "service": "api",
    "servicePort": 8080,
    "labelSelector": "app.kubernetes.io/name=api"
  }
}
```

Run the generic observer:

```powershell
python scripts/run_pod_kill_experiment.py `
  --target-file path/to/target.json `
  --baseline-probes 20 `
  --recovery-timeout-seconds 180
```

Explicit command-line target flags override values in the file. The JSON result
preserves the target and repository metadata alongside the observed Kubernetes
evidence.

## Deliberate boundary

This contract starts **after** a repository has been safely cloned, built, and
deployed. The upcoming repository executor owns that earlier stage: it must
create a request-scoped namespace, apply resource/network policies, detect or
receive an approved deployment plan, and then emit this target file. Keeping
deployment and fault observation separate prevents an untrusted repository from
selecting arbitrary cluster resources to delete.
