# Follow-up architecture notes

## Long-running Sandbox requests

The current HTTP endpoint remains synchronous for local integration and uses a
600-second AI HTTP timeout. Repository clone, image build, Kubernetes rollout,
and Chaos recovery can each take longer than a normal web request.

Before production use, change this to an asynchronous contract:

```text
AI -> Sandbox: start request
Sandbox -> AI: jobId accepted
Sandbox -> Redis: progress and result events
Backend/AI -> Sandbox or Redis: retrieve final result
```

Keep separate limits for clone, image build, rollout, and Chaos observation;
do not use one global timeout as a correctness decision.

## Patch retry loop

AI Core already generates and validates patch diffs. The external Sandbox must
later accept a `patchDiff`, apply it only to a fresh request-scoped clone, and
create a fresh Kubernetes namespace for every retry. The namespace, Secret,
Pod, Service, image, and clone workspace must be deleted after each round.

## Private repositories

Private repository access must use a short-lived GitHub App installation token
or equivalent ephemeral credential delivered over the private Backend-to-
Sandbox path. Do not put tokens in repository URLs, logs, artifacts, Git, or
Kubernetes manifests. The token must be discarded after clone.

## Container Kill on local Docker Desktop

Litmus `container-kill` was attempted against the local Docker Desktop
`kubeadm` cluster and returned `CHAOS_INJECT_ERROR`: its helper could not use
the cluster's containerd CRI v1 runtime socket. This is a local cluster/runtime
compatibility issue, not a QuickByte application failure.

Keep Pod Delete as the supported local baseline. Re-test Container Kill on a
kind-compatible or managed Kubernetes cluster after confirming the Litmus
runtime socket configuration. Report this condition as infrastructure/cluster
capability unsupported, never as a repository failure.
