---
literate-yaml: cloudmap@unfurl/v1.0.0
---

# Environment

The shared settings live in their own file, so the prose here can talk
about what they mean without repeating them.

```yaml
app:
  name: literate-app
  env:
    "+include": shared.yaml
    LOG_LEVEL: debug
```
