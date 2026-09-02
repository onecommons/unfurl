---
literate-yaml: cloudmap@unfurl/v1.0.0
---

# The organization

Prose explaining what this document is for. None of it belongs to any
record, and none of it may move when a record changes.

```yaml
components: # entities: !!
  # but this is more like an instance!
  organization@onecommons.org:
    type: # the kind of thing this is
      RealWorldEntity:
    tags: # and a sequence, which merges by replacement
      - co-op
```

Its name and notes live further down, beside the paragraph that explains
them — one record, deliberately split across two blocks.

```yaml
components:
  organization@onecommons.org:
    dependencies: # a new one belongs here, beside foo
      foo:
    name: onecommons
    notes: |
      first line
      # text, not a comment
      last line
```

Here the record is named with nothing under it, purely to anchor the
paragraph that follows. An anchor owns no fields, so an update must
leave it exactly as it is.

```yaml
components:
  organization@onecommons.org:
```

A second record, so a deletion has something to take without emptying
the document:

```yaml
components:
  retired@onecommons.org: # scheduled for removal
    type:
      RealWorldEntity:
```

Tilde-fenced, and holding a block scalar whose content looks like
markdown. Neither the `#` lines nor the indented fence inside it are
markup — a fence only closes on a line indented three spaces or less.

~~~yaml
components:
  documented@onecommons.org:
    notes: |
      Example usage:

      ```
      unfurl deploy
      ```

      # still not a comment
~~~

This block holds no record section, so nothing is ever written into it.

```yaml
apiVersion: unfurl/v1.0.0
```

An illustrative example, opted out so it stays prose:

```yaml
# literate-yaml: ignore
components:
  never@indexed.example:
    type: NotReal
```

And a fence in another language, which is not ours to read:

```json
{"components": {"from@json.example": {"type": "NotReal"}}}
```
