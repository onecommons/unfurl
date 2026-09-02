---
literate-yaml: cloudmap@unfurl/v1.0.0
---

# An organization

Prose that belongs to no record.

```yaml
components: # a trailing comment
  org:
    type: RealWorldEntity
```

Its name lives beside the paragraph explaining it.

```yaml
components:
  org:
    name: onecommons
```

Opted out, so it stays an example:

```yaml
# literate-yaml: ignore
components:
  never:
    type: NotReal
```

And another language entirely:

```json
{"components": {"from-json": {}}}
```
