# Files that hold more than one document

Design notes and staged plan for indexing files that hold N documents
rather than one: multi-document YAML (`---` separated) and
JSONL/NDJSON (one JSON value per line).

Status: stages 0, 1 and 1.5 are implemented. Stage 2 onwards is
deferred; this document is why, and what the shape would be.

## The problem

Every abstraction in the crate assumes one file holds one document:

- `Syntax::parse` returned a single value.
- `document_records` addresses a record as `value[prefix][key]`, where
  `prefix` is one of the format's `path_prefixes()`.
- `apply_pending_records` writes at `root[section][key]` and
  deliberately rejects an empty section — *"v1 supports single-segment
  parents only"*.

A multi-document file breaks the first; a file whose *records* are the
documents (JSONL) breaks the second as well, since a line has no parent
key and no inherent map key.

Multi-document YAML was worse than unsupported. `serde_saphyr::from_str`
errors with *"multiple YAML documents detected; use from_multiple"*, and
the scan propagated that with `?` — so one `---` anywhere in a tracked
worktree failed the whole scan and nothing else got indexed. Fixed in
stage 0.

## Two shapes, and only one of them needs a schema

The decisive distinction. A file holding N documents is one of:

**Container-shaped** — each document holds *sections* of records. Three
cloudmaps in one file, each with its own `repositories:` and
`services:`. `document_records` would run per document.

**Record-shaped** — each document *is* a record. A k8s manifest
(`apiVersion`/`kind`/`metadata`/`spec` has no keyed map for
`path_prefixes` to find), or a JSONL line. Document and record are 1:1.

The two differ in whether a record's address is ambiguous:

- Record-shaped: the chunk **is** the record, found by its own key.
  Nothing to store, nothing to disambiguate.
- Container-shaped: a file with two cloudmap documents both having a
  `repositories:` section makes `(file_path, /repositories, foo)`
  ambiguous, and today's `uq_record_path` index cannot even hold both.
  A document reference is not an optimisation here — it is the only
  thing that makes the write addressable.

**Record-shaped only is therefore a zero-migration feature.** All three
consumers of `file.format` keep working: `take_in_deletion`
(`sync.rs`, reads `row.format`), `find_records_follow`'s format cache
(keyed on `file_path`), and `ensure_file_registered` (`crud.rs`, resolves
via `for_path(record_path)`). Record identity stays
`(worktree_id, file_path, path, key)`, and a record-shaped file has
exactly one synthetic section, so nothing can collide.

## Why the container case is deferred rather than designed in

It would need a `document` table with `format` moved off `file`. The
sketch, recorded for the day something needs it:

```sql
CREATE TABLE document (
    id          INTEGER PRIMARY KEY,   -- surrogate: position must not be identity
    worktree_id INTEGER NOT NULL,
    file_path   TEXT    NOT NULL,
    position    INTEGER NOT NULL,      -- index within the file
    format      TEXT    NOT NULL,
    key         TEXT,                  -- format's name for it, when it has one
    deleted     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (worktree_id, file_path, position),
    UNIQUE (id, file_path),            -- lets record's FK check the denormalised path
    FOREIGN KEY (worktree_id, file_path)
        REFERENCES file (worktree_id, path) ON DELETE CASCADE
);
```

Four things that sketch gets right and a naive version would not:

1. **A surrogate `id`, so `position` is mutable ordering and not
   identity.** Inserting a document at the front is then one
   `UPDATE document SET position = position + 1 WHERE position >= n`
   and no record row moves. With position in the primary key, every
   record's parent would have to be rewritten.
2. **`record` needs a `document_id`.** Once format lives on the
   document, a record that doesn't name its document has no resolvable
   format — `find_records_follow` currently caches format by
   `file_path`. Uniqueness also moves to `(document_id, path, key)`.
3. **`document` needs a `deleted` tombstone**, for the reason the
   comment on `file.deleted` gives: `record`'s FK is
   `ON DELETE CASCADE`, so hard-deleting the parent destroys the
   pending rows *including the tombstones that are the deletion*.
4. **`file` keeps `commit_id`, `source_oid`, `deleted`.** A document has
   no blob of its own. The split is: file = bytes and git identity,
   document = one logical unit, its format, and its place.

### Position cannot survive an arbitrary rearrangement — and mostly needn't

The obvious objection to `position` is that a hand edit reordering or
inserting documents makes it name the wrong one. The answer is that the
write **does not match rows to chunks**. `write_file` re-reads and
re-parses the file, and `apply_pending_records` locates a record with
`root.get(section).get(key)`. The chunk holding `(path, key)` is the
chunk to splice into. Nothing stored is consulted.

`classify_conflict` already handles the file having moved, for every row
kind:

| | |
|---|---|
| key found, value differs | the divergence it already reports |
| key absent, pending **update** | `ModifyDelete` |
| key absent, pending **delete** | `None` — the file already agrees |

So updates and deletes are both derivable. **Only a pending create has
no key in the file**, and therefore needs a stored target document — and
a rearrangement that removed that target is a conflict to report, not a
chunk to guess.

That reframes the table: `position` is ordering rebuilt each scan,
`format` is the one durable column, `record.document_id` is a cache the
write re-derives anyway, and the create target is the single
load-bearing use.

Spans are deliberately **not** stored, there or anywhere. A span is a
fact about bytes, so it goes stale exactly when `source_stale()` is true
— the one moment it would be needed — and the write re-parses the file
regardless, so it is re-derivable for free and correct.

### Rejected: putting the document index in the record's `path`

`path = "/2/repositories"` avoids the migration and is reachable
(`apply_pending_records` only rejects an *empty* section). It makes
position **identity**: inserting a document renumbers every later one,
so the database sees a wave of deletes and inserts against records that
never moved. The surrogate `id` exists precisely to avoid that.

## Record identity is a hard requirement, not a detail

`base_value` looks a record up **in the base commit's parsed document**
by `(path, key)`. The key is a cross-commit identity, not a within-file
address. Two consequences:

- **Position-derived keys are wrong, silently.** Key `"3"` in the base
  commit is a different record than line 3 is now, so the conflict check
  compares unrelated records and answers confidently.
- **Content-derived keys (digests) are wrong too.** An edit changes the
  key, so every modification becomes delete+insert and there is never a
  same-key-different-value pair — which is the only thing
  `classify_conflict` looks at. Conflict detection does not degrade, it
  disappears. Identical lines also collapse.

A `position` column on `record`, added to the unique index, relocates
the problem rather than solving it: `base_value` would need position
too, and position in the base commit is not position now.

**If two records are distinguishable only by position, no key scheme
can give them stable identity** — the file does not carry the
information. Such data wants append-and-query, not per-record CRUD with
conflict detection, and that is a different feature.

Where new records need keys minted, the key should be **written into the
record as a field** so the file carries the identity and it survives
reordering, editing, and other tools. A synthetic key held only in the
database does not.

## What the GitLab NDJSON exports actually look like

The motivating format. Layout (confirmed against GitLab 19.3):

```
tree/project.json                  single JSON object, project attributes
tree/project/<relation>.ndjson     one relation per file
tree/groups/<relation>.ndjson      groups add a variable <group_id> segment:
tree/groups/<group_id>/<relation>.ndjson
```

- **One relation is always one file.** No numbered chunking
  (`issues/0.ndjson`) anywhere, so "section = file stem" holds with no
  merge step.
- **Group exports add a variable path segment**, so a format claiming
  these by path needs a glob with a wildcard, not a fixed prefix.
- `lib/gitlab/import_export/project/import_export.yml` enumerates the
  valid relation names — which is what `path_prefixes()` wants.

### Real exports carry no `id`

Surveyed `project-templates/unfurl_dashboard/tree/project` (a real
export used as a project template):

| | |
|---|---|
| **0 records have an `id`** | GitLab strips them on export; they are reassigned at import |
| 12 of 27 files are empty (0 bytes) | issues, labels, milestones, releases, snippets, … |
| 4 hold a literal `null` line | `auto_devops`, `error_tracking_setting`, `push_rule`, `service_desk_setting` |
| most of the rest hold exactly one record | `ci_cd_settings`, `project_feature`, `security_setting`, `metrics_setting`, … |
| one file has more than one record | `project_members` (12) |

GitLab's **hand-written spec fixtures** (`spec/fixtures/lib/gitlab/
import_export/complex/`) *do* carry `id` and have populated relations
(18 services, 12 issues, 10 pipelines, 9 merge requests). They are the
right thing for exercising the multi-record machinery and the wrong
thing for deciding what the key is.

So the key is per relation:

- `project_members` → `user_id` (distinct), or `user.username` if a
  human-readable index is wanted (needs nested-path support)
- `protected_branches` → `name`
- the settings relations → **no key needed: there is exactly one
  record.** Addressed as section `/ci_cd_settings`, key
  `ci_cd_settings`. Stable across commits, which is all `base_value`
  needs, and unambiguous because a singleton cannot collide with itself.

Every edge case that looked hypothetical — empty files, non-object
lines, keyless records — is present in the first real export examined.

## The design

### `SectionKind` — per section, not per file

```rust
enum SectionKind {
    Map,        // the section's children are records, keyed by their map key (today)
    Singleton,  // the section *is* one record, keyed by its own name
}
fn section_kind(&self, path: &str) -> SectionKind { SectionKind::Map }
```

Two unrelated-looking needs turn out to be the same statement: cloudmap's
`metadata:` section (a flat object of fields, not a map of records) and
a singleton NDJSON relation. Implemented in stage 1.5.

Addressing a record inside a document happens in five places, and they
must agree or a pending edit diverges from a file that in fact holds it:
`document_records`, `apply_insert`, `apply_delete`,
`apply_pending_records`, `base_value`. They go through one
get/put/remove trio taking the kind, so the variant lives in one body.

`build.rs` generates `field_order` for a singleton too — a root property
that `$ref`s a definition directly, rather than through
`additionalProperties`. Without it a section created from records would
be written in the database's key order rather than the schema's.

### `chunking` — per file, for chunked syntaxes

```rust
/// Which section this file's chunks fold into, and what keys them.
/// `None` = not a chunked file.
fn chunking(&self, file_path: &str) -> Option<Chunking>;
struct Chunking { section: String, key: Option<String> }
```

Returning `Some` also **claims** the file: a headerless `.jsonl` cannot
be classified by content, so the path is all there is. This inverts the
usual order — format resolved from the path *before* folding, rather
than parse → detect-on-value → fold.

The path is decided **once per file**, not per record: a line in
`issues.ndjson` carries no field saying it is an issue, and the filename
is the only thing that knows. That also buys a validation — a pending
record whose `path` disagrees with the file's section is an error rather
than something that silently writes into the wrong file.

Per-record routing (a k8s file whose Deployments and Services go to
different sections) is not supported. Adding it later is cheap: the fold
already has to return a per-chunk `(section, key)` mapping for the
renderer, so it is mostly letting that vary, plus a `section_of(chunk)`
hook. What it would lose is the validation above.

### The fold

Chunks become `{ section: { key: chunk } }`, so `document_records`,
`apply_pending_records`, `base_value`, the conflict machinery and the
schema all work untouched.

Lines that are not objects, or lack the key field, are **unclaimed**:
never indexed, bytes preserved. A `null` line in a *singleton* section
is the exception — it means unset, so it reads as an empty slot and is
replaced rather than preserved.

Duplicate keys reject the file. The synthetic map would collapse them
and the next rewrite would destroy a line.

### The renderer

Walks the **original** chunk list:

```
claimed & touched    → re-rendered record
claimed & untouched  → original bytes
claimed & deleted    → nothing
unclaimed            → original bytes
then append records that were not in the file
```

Joined with `\n` for JSONL, `---` for YAML. This is what guarantees
nothing untouched moves or disappears, and it is identical for both
syntaxes.

Ordering needs no storage: it is recovered from the file every time.
`apply_insert`'s `section.insert` keeps an existing key's slot, a new
key appends, and `apply_delete` uses `shift_remove` (not the
`swap_remove` that `Map::remove` is) so survivors keep their order. A
JSONL format must therefore stay on `Order::PreserveOrder`; `Sort`
would reorder the whole file on one edit.

**`splice_touched_sections` stays a single-document function and the
caller slices.** `unfurl-merge`'s `Template` stays ignorant of
multi-document YAML. Its self-check (`(got == want)`, re-parsing its own
output) must be applied **per chunk**: on a multi-chunk file
`serde_saphyr::from_str` errors, so a whole-file check fails every time,
falls back to `render_document` on the merged root, and silently deletes
every unrecognised document in the file. That is the single most
dangerous mistake available in this work.

### Library support, verified

| | |
|---|---|
| JSONL read | `serde_json::Deserializer::into_iter::<Value>()`; `byte_offset()` gives exact spans |
| JSONL write | `serde_json::to_string` is newline-free for any value |
| multi-doc YAML read | `serde_saphyr::from_multiple` → `Vec<T>` (skips null-like documents) |
| multi-doc YAML write | `serde_saphyr::to_string_multiple` (`---\n` between, none before the first) |
| YAML document spans | **not** exposed by serde-saphyr; needs `saphyr`'s `Event::DocumentStart` marks, or a `---` scan that is correct about block scalars |

`StreamDeserializer` is more lenient than either JSONL spec — it splits
on any whitespace, so it also accepts pretty-printed JSON concatenated
across lines. Fine on read; writes emit strict one-value-per-line.

NDJSON and JSON Lines are the same format with two specs and two
extensions (`.ndjson`, `.jsonl`); both belong on one `for_extension`
arm. RFC 7464 JSON Text Sequences (`0x1E`-prefixed) is a different
thing.

## Stages

| | | |
|---|---|---|
| **0** | Report unparseable files instead of failing the scan | done |
| **1** | Parse a file as a list of documents (N=1 everywhere) | done |
| **1.5** | `SectionKind::Singleton`, used by cloudmap `metadata` | done |
| **2a** | JSONL read path: syntax, spans, `chunking`, fold, detection by path | deferred |
| **2b** | JSONL write path: chunk renderer, unclaimed passthrough | deferred |
| **3** | Record-shaped multi-document YAML: document spans, per-chunk splice | deferred |
| **4** | JSONL append fast path, behind a measurement | deferred |
| **—** | Container-shaped + the `document` table | until a format needs it |

Stage 1 is a separate commit because `base_value`'s cross-commit lookup
makes a single fold mandatory: if the scan folded JSONL into a synthetic
section but `doc_at_commit` returned the raw first line, every conflict
check against a JSONL file's history would report a false
`ModifyDelete`. One fold makes that unrepresentable.

Stage 1.5 precedes stage 2 because it lands the singleton concept
against one format, one section, no new syntax and no new fixtures —
so stage 2 inherits a proven hook instead of debuting it alongside a new
syntax, a new detection path and a new renderer at once.

Stage 2a precedes 2b because the read path is where the design risk is,
and it is provable end to end with `get_record` / `find_records`.

JSONL precedes multi-document YAML because it is strictly easier:
spans come free from `byte_offset()`, there are no comments so a touched
chunk is just `to_string` and needs no self-check, and `extended` is
always false. It exercises the whole chunk model without the span
extraction or comment preservation.

## Open decisions

- **Key for `project_members`** — `user_id` (top-level, no new
  machinery) or `user.username` (human-readable, needs dotted-path
  support).
- **Where a duplicate-key rejection is reported.**
  `SyncOutcome::unparsed` is documented as "files a scan could not
  parse", and this is not a parse failure — broaden that doc, or add a
  sibling field.
- **Whether a GitLab-export format ships as a builtin** beside
  cloudmap, or stays caller-registered with only a test-only format in
  this crate's tests. Currently: test-only.

## Rejected alternatives

**A `metadata` JSONB column on `file`.** Proposed for cloudmap's
`metadata:` section. Rejected because that section is document *content*
that lives in the file, so it fails the only sensible rule for such a
column — *facts that cannot be re-derived from the file*. It also
already round-trips correctly (the splice preserves untouched sections;
a whole-file re-emit serializes the whole parsed root). What it was
missing was being **addressable**, which a side column does not give: no
`get_record`, no CRUD, no versioning, no conflict detection, no
`list_changes`. `SectionKind::Singleton` gives all of it for free.

The column does have one genuinely good use, left unimplemented:
`write_file` refuses to create a literate markdown file because the
`literate-yaml` front-matter name has nowhere to come from. That *is* a
fact the file cannot supply when there is no file.

**Format objects holding scan state.** One `DataFormat` instance lives
as long as the `SyncedRepo`, shared across every scan and write, with
`&self` methods and a `Send + Sync` bound. Construction-time immutable
state (a relation list, compiled globs) is fine. Accumulating state
across a scan is not: the scan **skips any file whose `source_oid`
matches the blob on disk**, so a format would see only the files that
changed, and every incremental scan would be missing most of the tree.
There is also no reset hook, and scans and writes can overlap. Anything
a format wants to "remember" across files belongs in a record — which
is what `project.json` should become, rather than format state.
