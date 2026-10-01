# Repository validation configuration

Repositories can opt into a supported Sandbox deployment profile by adding
`.codereferee/validation.yaml`:

```yaml
deploymentProfile: quickbyte-demo
```

The profile is a temporary compatibility step: it lets the repository declare
how it should be reproduced without requiring Backend or AI to hard-code a
profile name. Future revisions will add explicit service, dependency, health,
and scenario-plan fields.
