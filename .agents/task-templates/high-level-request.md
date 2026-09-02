# High-level request

Free-form requests are accepted. This optional compact format makes boundaries explicit:

```text
OUTCOME:
What real result should exist?

INPUT:
What material or Evidence should be used?

OUTPUT:
What artifacts are required?

BOUNDARIES:
What must not happen?

STOP:
Where should execution stop?
```

Omitted fields are inferred conservatively from the active project context. Public, paid, credential, destructive, deployment, and irreversible actions remain human-gated.
