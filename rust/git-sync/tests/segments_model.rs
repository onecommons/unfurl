//! Model test for the segment design (docs/branch-segments.md).
//!
//! [`Segments`] is an in-memory implementation of the design's storage:
//! segments, immutable row versions and supersession entries, with the
//! operations of §4 — writes, scans, the commit fold, forks and splits,
//! publishing a user branch, deletion and compaction.
//!
//! [`Model`] is a reference that knows nothing about segments: each
//! worktree's committed tree, its working tree, its draft, its conflicts,
//! and the versions each draft edit was made over. After every step of a
//! random history, every worktree's own view, its committed segments and
//! its conflicts must agree with it exactly. A user branch's layered view
//! must never lose a row the model shows, and may only add a copy of a
//! version the user's edit was made over: copies are over-reported, never
//! lost.
//!
//! Versions are shared ids: the harness hands both sides the same id for
//! each edit, so views compare as sets of `(key, version)`, and a version
//! stands for its content. Tombstones aren't compared, only what they
//! hide. Keys are grouped into files, which is what a scan's draft side
//! works on.

use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet};

use proptest::prelude::*;

include!("segments/common.rs");
include!("segments/imp.rs");
include!("segments/model.rs");
include!("segments/world.rs");

fn run(ops: &[Op]) {
    let mut world = World::new();
    world.check(0, &Op::NewUser);
    for (i, op) in ops.iter().enumerate() {
        world.apply(op);
        world.check(i + 1, op);
    }
    case_done();
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(2000))]

    #[test]
    fn segments_agree_with_the_model(ops in prop::collection::vec(op(), 1..60)) {
        run(&ops);
    }

    #[test]
    fn rebases_agree_with_the_model(ops in prop::collection::vec(rebase_op(), 1..60)) {
        run(&ops);
    }

    #[test]
    fn publishing_agrees_with_the_model(ops in prop::collection::vec(publish_op(), 1..16)) {
        run(&ops);
    }

    #[test]
    fn exports_agree_with_the_model(ops in prop::collection::vec(export_op(), 1..40)) {
        run(&ops);
    }
}

include!("segments/histories.rs");
