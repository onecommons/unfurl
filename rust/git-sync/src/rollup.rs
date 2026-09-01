// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! The commit-message format, both directions.
//!
//! A single git commit carries every batch applied since the last one,
//! so the message is not a changelog beside the data -- it *is* the
//! only machine-readable copy of who made those writes and why. That is
//! why it is written and read by one module, and why every field that
//! can hold arbitrary text is delimited rather than merely spaced.
//!
//! [`build_commit_message`] renders it and [`parse_commit_rollup`]
//! reads it back; see the latter for the grammar and the reasoning
//! behind each piece. [`resolves_version_from_message`] reads a trailer
//! a *person* writes rather than one this crate emits -- see
//! [`crate::SyncedRepo::update_from_working_dir_with`].

use crate::error::{Error, Result};
use crate::model::{CommitRollup, RollupTxn, TxnRecord};

/// Render a commit message in the format [`parse_commit_rollup`] reads
/// back. See there for the grammar and the reasoning behind it.
pub(crate) fn build_commit_message(subject: &str, rollup: &CommitRollup) -> String {
    let mut out = String::from(subject);
    if !rollup.txns.is_empty() {
        let plural = if rollup.txns.len() == 1 { "" } else { "s" };
        out.push_str(&format!(
            "\n\nRollup of {} git-sync transaction{plural}:\n\n",
            rollup.txns.len()
        ));
    }
    for txn in &rollup.txns {
        let range = if txn.first_version == txn.last_version {
            txn.first_version.to_string()
        } else {
            format!("{}-{}", txn.first_version, txn.last_version)
        };
        let author = match &txn.author {
            Some(a) => format!(" {a}"),
            None => String::new(),
        };
        out.push_str(&format!(
            " - {range} on {} {}{author}\n",
            txn.branch, txn.created_at
        ));
        if let Some(message) = &txn.message {
            for line in message.lines() {
                if line.is_empty() {
                    out.push_str("   |\n");
                } else {
                    out.push_str(&format!("   | {line}\n"));
                }
            }
        }
        for rec in &txn.records {
            out.push_str(&format!(
                "   * {} {} {} {}\n",
                rec.version,
                if rec.deleted { "D" } else { "M" },
                json_str(&rec.path),
                json_str(&rec.key),
            ));
        }
        let unaccounted = txn.unaccounted();
        if unaccounted > 0 {
            let width = txn.last_version - txn.first_version + 1;
            let plural = if width == 1 { "" } else { "s" };
            out.push_str(&format!(
                "   ! {unaccounted} of {width} write{plural} superseded later in \
                 this commit, or rolled back\n"
            ));
        }
    }

    // Trailer block: its own final paragraph. Both separators matter —
    // without the blank line git reads the trailers as a continuation of
    // the prose (or of a bare subject) and `%(trailers)` comes back
    // empty.
    if !out.ends_with('\n') {
        out.push('\n');
    }
    out.push('\n');
    if let Some(origin) = &rollup.origin {
        out.push_str(&format!("Git-Sync-Origin: {}\n", one_line(origin)));
    }
    if let Some(family) = &rollup.family {
        out.push_str(&format!("Git-Sync-Family: {}\n", one_line(family)));
    }
    out.push_str(&format!("Git-Sync-Txn-Count: {}\n", rollup.txns.len()));
    out.push_str(&format!("Git-Sync-Next-Version: {}\n", rollup.next_version));
    out
}

/// A value as a JSON string literal, so it survives a line-oriented
/// format whatever it contains. Infallible for `&str`; the fallback is
/// unreachable and only avoids an `unwrap`.
fn json_str(value: &str) -> String {
    serde_json::to_string(value).unwrap_or_else(|_| "\"\"".to_string())
}

/// Collapse anything that would break out of a single trailer line.
fn one_line(value: &str) -> String {
    value
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect()
}

/// Read a commit message back into the rollup that produced it.
///
/// A single git commit carries every batch applied since the last one,
/// so per-batch authorship would otherwise be lost. Unlike a plain
/// changelog the message is also the *only* machine-readable copy, so
/// every field that can contain arbitrary text is delimited rather than
/// merely spaced.
///
/// ```text
/// Update cloudmap
///
/// Rollup of 2 git-sync transactions:
///
///  - 21-22 on main 2026-08-23T19:46:56-07:00 Ada Lovelace <ada@example.com>
///    | Point std at the new branch
///    * 22 M "/repositories" "git://unfurl.cloud/feb20a/dashboard.git"
///    ! 1 of 2 writes superseded later in this commit, or rolled back
///  - 23 on main 2026-08-23T19:47:02-07:00
///    | Fix the std path
///    * 23 M "/repositories" "git://unfurl.cloud/onecommons/std.git"
///    * 24 D "/repositories" "git://example.com/legacy.git"
///
/// Git-Sync-Origin: unfurl.cloud/someone/cloudmap-fork
/// Git-Sync-Family: unfurl.cloud/onecommons/cloudmap
/// Git-Sync-Txn-Count: 2
/// Git-Sync-Next-Version: 25
/// ```
///
/// The grammar, and why each piece is shaped the way it is:
///
/// - **Entry header** `` - <range> on <branch> <rfc3339>[ <author>]``.
///   The range collapses to one number when the batch drew one version.
///   The branch repeats per entry because a merge brings another
///   branch's rollup commits into this branch's log. The author runs to
///   end-of-line and is last precisely so it may contain spaces and
///   colons; it is absent when unknown. The timestamp is RFC 3339, not
///   a git-style date, because this copy has to round-trip exactly.
/// - **Message lines** `   | text`, one per line, a blank line being a
///   bare `   |`. The marker is non-whitespace so a blank line survives
///   trailing-whitespace stripping, which a plain indent would not.
/// - **Record lines** `   * <version> <flag> <path> <key>`, one per
///   record the batch still accounts for, in ascending version order.
///   The flag is exactly one of:
///   - `M` — the op wrote the record (a create or an update; the two are
///     one operation here, since an upsert replaces the whole value).
///   - `D` — the op deleted it. The row is a tombstone at this point and
///     is purged by commit roll-forward, so this line is the
///     only lasting record that the delete belonged to this batch.
///
///   The set is closed: a parser rejects any other letter rather than
///   guessing, which does mean introducing a third flag would be a
///   breaking change to the format.
///
///   `path` and `key` are JSON strings. Both are caller-supplied and may
///   contain spaces, quotes, or newlines; quoting is what keeps the sole
///   machine copy from being corrupted by a key with a space in it.
///   Anything after the closing quote is ignorable commentary.
/// - **Shortfall line** `   ! …`, present only when
///   [`RollupTxn::unaccounted`] is non-zero. Purely a human affordance —
///   a parser recomputes it from the range and the record count.
///
/// The last paragraph is a git trailer block — blank-line separated,
/// `Token: value` lines only — so `git interpret-trailers` and
/// `--format=%(trailers)` read it:
///
/// - `Git-Sync-Origin` is the worktree origin — who made these writes.
///   It is not in the prose because every batch in a rollup belongs to
///   the same worktree, and a reader of the log is already in the
///   repository it names.
/// - `Git-Sync-Family` is the origin of the worktree the version
///   sequence belongs to: itself, or the upstream it was forked from.
///   A reader reconstructing that sequence keeps the rollups whose
///   family matches and ignores the rest — origin cannot decide it,
///   since a fork's history holds upstream rollups drawn from the same
///   counter under a different origin.
/// - `Git-Sync-Txn-Count` is how many entries the rollup has — a count,
///   not an identifier — always emitted, `0` included.
///   It is what makes a rollup section trustworthy: prose that merely
///   looks like one (a commit *about* this format, say) is ignored when
///   the count disagrees, and a dropped trailer becomes a hard parse
///   error instead of silently reading as "no batches".
/// - `Git-Sync-Next-Version` is the version counter of the worktree's
///   family — the upstream it and its forks share — written
///   on *every* commit, batches or not: single-record CRUD writes and
///   re-syncs draw versions too, so a rebuild seeding its counter from
///   the rollup ranges alone would re-issue numbers the old database had
///   already handed out. Its presence is also the signal that the whole
///   message is in this format.
///
/// Versions drawn by writes still staged when the last commit was made
/// are invisible here — they never reached git — so a rebuilt counter
/// can trail the original by however much was in flight.
///
/// Returns `Ok(None)` for a message that is not a git-sync commit at
/// all (no `Git-Sync-Next-Version` trailer). Returns `Err` for one that
/// announces itself and then does not parse — a truncated entry, a
/// mangled trailer, an entry count that disagrees with `Git-Sync-Txn-Count`
/// (which is what a squash merge of two git-sync commits looks like).
/// The distinction matters: silently reporting "no batches" for a
/// damaged message would lose history without anyone noticing.
///
/// # Errors
///
/// [`crate::Error::Other`] with a description of what did not parse.
pub fn parse_commit_rollup(message: &str) -> Result<Option<CommitRollup>> {
    let lines: Vec<&str> = message.lines().collect();

    // Trailers are the final paragraph, and only the final one: text in
    // a request-supplied commit message can never forge them, because
    // this block is always appended after it.
    let trailer_start = match trailer_block_start(&lines) {
        Some(i) => i,
        None => return Ok(None),
    };
    let mut origin = None;
    let mut family = None;
    let mut next_version = None;
    let mut declared = None;
    for line in &lines[trailer_start..] {
        let Some((token, value)) = line.split_once(": ") else {
            continue;
        };
        match token {
            "Git-Sync-Origin" => origin = Some(value.to_string()),
            "Git-Sync-Family" => family = Some(value.to_string()),
            "Git-Sync-Next-Version" => next_version = value.parse::<i64>().ok(),
            "Git-Sync-Txn-Count" => declared = value.parse::<usize>().ok(),
            _ => {}
        }
    }
    let Some(next_version) = next_version else {
        return Ok(None);
    };
    let declared = declared.ok_or_else(|| {
        Error::Other(
            "git-sync commit message has Git-Sync-Next-Version but no Git-Sync-Txn-Count"
                .to_string(),
        )
    })?;
    if declared == 0 {
        return Ok(Some(CommitRollup {
            origin,
            family,
            next_version,
            txns: Vec::new(),
        }));
    }

    // Anchor on the *last* rollup header before the trailers: a subject
    // quoting this format cannot displace the real section.
    let header = lines[..trailer_start]
        .iter()
        .rposition(|l| is_rollup_header(l))
        .ok_or_else(|| {
            Error::Other(format!(
                "git-sync commit message declares {declared} transactions but has no rollup section"
            ))
        })?;

    let mut txns: Vec<RollupTxn> = Vec::new();
    for line in &lines[header + 1..trailer_start] {
        if line.is_empty() {
            continue;
        }
        if let Some(rest) = line.strip_prefix(" - ") {
            txns.push(parse_entry_header(rest)?);
            continue;
        }
        let current = txns.last_mut().ok_or_else(|| {
            Error::Other(format!("git-sync rollup line before any entry: {line:?}"))
        })?;
        if let Some(text) = line.strip_prefix("   | ") {
            push_message_line(current, text);
        } else if *line == "   |" {
            push_message_line(current, "");
        } else if let Some(rest) = line.strip_prefix("   * ") {
            current.records.push(parse_record_line(rest)?);
        } else if line.starts_with("   ! ") {
            // Recomputable from the range and record count; carries no
            // information of its own.
        } else {
            return Err(Error::Other(format!(
                "unrecognized line in git-sync rollup: {line:?}"
            )));
        }
    }
    if txns.len() != declared {
        return Err(Error::Other(format!(
            "git-sync rollup declares {declared} transactions but {} parsed",
            txns.len()
        )));
    }
    Ok(Some(CommitRollup {
        origin,
        family,
        next_version,
        txns,
    }))
}

/// Index of the first line of the final paragraph, when every non-empty
/// line in it is a `Token: value` trailer. `None` when the message has
/// no such paragraph.
fn trailer_block_start(lines: &[&str]) -> Option<usize> {
    let end = lines.iter().rposition(|l| !l.is_empty())? + 1;
    let start = lines[..end]
        .iter()
        .rposition(|l| l.is_empty())
        .map_or(0, |i| i + 1);
    let is_trailer = |l: &&str| {
        l.split_once(": ").is_some_and(|(token, _)| {
            !token.is_empty()
                && token
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
        })
    };
    lines[start..end].iter().all(is_trailer).then_some(start)
}

/// The `Git-Sync-Resolves-Version: N` trailer of a commit message, if
/// it has one — see [`SyncedRepo::update_from_working_dir_with`].
///
/// Unlike the rollup trailers this one is *written by a person* (or by
/// whatever tool edited the file), so it is read from the same trailer
/// block but is not part of [`parse_commit_rollup`]'s grammar: a commit
/// carrying it need not be a git-sync commit at all. The last spelling
/// wins, matching how git resolves a repeated trailer token.
pub(crate) fn resolves_version_from_message(message: &str) -> Option<i64> {
    let lines: Vec<&str> = message.lines().collect();
    let start = trailer_block_start(&lines)?;
    lines[start..].iter().rev().find_map(|line| {
        line.strip_prefix("Git-Sync-Resolves-Version:")?
            .trim()
            .parse()
            .ok()
    })
}

fn is_rollup_header(line: &str) -> bool {
    line.starts_with("Rollup of ") && line.ends_with(':') && line.contains(" git-sync transaction")
}

fn push_message_line(txn: &mut RollupTxn, text: &str) {
    match &mut txn.message {
        Some(existing) => {
            existing.push('\n');
            existing.push_str(text);
        }
        None => txn.message = Some(text.to_string()),
    }
}

/// `<range> on <branch> <rfc3339>[ <author>]` — author last, and taken
/// as the whole remainder, so it may contain spaces and colons.
fn parse_entry_header(rest: &str) -> Result<RollupTxn> {
    let bad = || Error::Other(format!("malformed git-sync rollup entry: {rest:?}"));
    let mut parts = rest.splitn(5, ' ');
    let range = parts.next().ok_or_else(bad)?;
    if parts.next() != Some("on") {
        return Err(bad());
    }
    let branch = parts.next().ok_or_else(bad)?;
    let created_at = parts.next().ok_or_else(bad)?;
    let author = parts.next().filter(|a| !a.is_empty());

    let (first, last) = match range.split_once('-') {
        Some((a, b)) => (a, b),
        None => (range, range),
    };
    Ok(RollupTxn {
        first_version: first.parse().map_err(|_| bad())?,
        last_version: last.parse().map_err(|_| bad())?,
        branch: branch.to_string(),
        created_at: created_at.to_string(),
        author: author.map(str::to_string),
        message: None,
        records: Vec::new(),
    })
}

/// `<version> <flag> <json path> <json key>`, where the flag is `M` for
/// a write or `D` for a delete and anything else is an error (see
/// [`build_commit_message`] for the grammar). The two JSON strings are
/// read with a streaming deserializer, so a path or key containing a
/// space, quote, or newline round-trips and any trailing commentary is
/// left unconsumed.
fn parse_record_line(rest: &str) -> Result<TxnRecord> {
    let bad = || Error::Other(format!("malformed git-sync rollup record: {rest:?}"));
    let mut parts = rest.splitn(3, ' ');
    let version: i64 = parts.next().ok_or_else(bad)?.parse().map_err(|_| bad())?;
    let deleted = match parts.next() {
        Some("M") => false,
        Some("D") => true,
        _ => return Err(bad()),
    };
    let mut strings =
        serde_json::Deserializer::from_str(parts.next().ok_or_else(bad)?).into_iter::<String>();
    let path = strings.next().ok_or_else(bad)?.map_err(|_| bad())?;
    let key = strings.next().ok_or_else(bad)?.map_err(|_| bad())?;
    Ok(TxnRecord {
        path,
        key,
        version,
        deleted,
    })
}
