# Generic Litmus Pod Delete

QuickByte is only the first target. Litmus reads a repository target JSON made
by the deployment stage and applies the same fault to any isolated namespace.

```powershell
python scripts/run_litmus_pod_delete.py --target-file path/to/target.json
```

For a one-off local verification, the same generic values can be supplied on
the command line instead of writing a target file.

The target must contain `namespace`, `deployment`, and `labelSelector` under
`target`. The script creates a namespace-scoped service account and RoleBinding,
then runs the official `pod-delete` ChaosExperiment. It never uses a hard-coded
repository name or image.
