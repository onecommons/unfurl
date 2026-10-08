// Scripted histories, each found by a random run or a mutation. Every
// entry point that includes this file runs them through its own `run`.

/// A key changed before a split point and back after it, with a user branch
/// that edited over the original version: the split's restored row has to
/// stay hidden from that user.
#[test]
fn split_restores_a_row_with_its_entries() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 1, false),
        Op::External(0, vec![(1, false)]),
        Op::External(0, vec![(1, true)]),
        Op::External(0, vec![(1, false)]),
        Op::Fork(0, 2),
    ]);
}

/// A fold took the tombstone a segment's own deletion superseded, so the
/// segment hides the row below by an entry alone: a split must restore
/// git's value at its end, not that row.
#[test]
fn split_restores_git_value_not_the_row_below() {
    run(&[
        Op::Write(0, 0, false),
        Op::NewUser,
        Op::Fork(0, 0),
        Op::Delete(1),
        Op::Commit(0),
        Op::External(0, vec![(3, true)]),
        Op::NewUser,
        Op::External(0, vec![(0, false)]),
        Op::Write(0, 0, false),
        Op::Commit(0),
        Op::External(0, vec![(3, false)]),
        Op::External(0, vec![(3, true)]),
        Op::Delete(11),
        Op::Fork(0, 1),
        Op::Write(0, 0, false),
    ]);
}

/// Main deletes a record a user edited, leaving a tombstone that changes
/// nothing in git. Splits must move it past the split point, and not let
/// the user's draft supersede its restoration, or the conflict is lost.
#[test]
fn split_keeps_a_later_deletion_newer_than_a_layered_edit() {
    run(&[
        Op::NewUser,
        Op::NewUser,
        Op::Fork(0, 0),
        Op::Rebuild(20, 253, 0, false),
        Op::Delete(0),
        Op::External(6, vec![(3, false)]),
        Op::UserWrite(0, vec![], 3, false),
        Op::Delete(253),
        Op::External(0, vec![(3, true)]),
        Op::Fork(0, 2),
        Op::Fork(14, 1),
        Op::Publish(0),
    ]);
}

/// Main's committed deletion replaces a tombstone below its head that the
/// user saw: the fold must carry the user's entry across, or publishing
/// reads the unchanged absence as new.
#[test]
fn fold_carries_entries_from_the_row_below() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 0, false),
        Op::Rebuild(0, 31, 1, false),
        Op::Write(0, 0, false),
        Op::Publish(0),
        Op::UserWrite(0, vec![], 0, true),
        Op::Write(0, 0, true),
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::Commit(0),
        Op::Write(0, 0, false),
        Op::Publish(0),
    ]);
}

/// A split re-creates main's value in a new row, which a stack shows as a
/// copy beside a lower user's edit; the upper user edits over it, so
/// publishing finds no conflict with it.
#[test]
fn stack_copy_edited_over_is_no_conflict() {
    run(&[
        Op::NewUser,
        Op::DiskEdit(0, 4, false, FileWins::Never),
        Op::UserWrite(0, vec![], 4, false),
        Op::Commit(0),
        Op::External(0, vec![(0, false)]),
        Op::Write(0, 4, false),
        Op::Fork(0, 0),
        Op::Delete(31),
        Op::Commit(0),
        Op::Fork(0, 1),
        Op::NewUser,
        Op::Rebuild(14, 33, 0, false),
        Op::UserWrite(5, vec![104], 4, false),
        Op::Publish(47),
    ]);
}

/// A rebuild deletes a record a user edited: it leaves a tombstone, which
/// the user's later edit supersedes, so publishing sees that edit was
/// made after the deletion.
#[test]
fn rebuild_leaves_a_tombstone_for_a_held_key() {
    run(&[
        Op::NewUser,
        Op::Write(0, 3, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::Fork(0, 0),
        Op::Rebuild(30, 125, 3, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::Write(0, 0, false),
        Op::Publish(0),
    ]);
}

/// Main deletes twice over its own draft tombstone, which a user saw: the
/// second takes over the user's entry from the one it replaces.
#[test]
fn write_carries_entries_from_the_replaced_draft_row() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 4, false),
        Op::Write(0, 4, true),
        Op::UserWrite(0, vec![], 4, false),
        Op::Write(0, 4, true),
        Op::Commit(0),
        Op::Publish(0),
    ]);
}

/// A user edits a record main has deleted, which the user's own view still
/// holds: the edit's base comes from the layered view, so it has none.
#[test]
fn layered_edit_takes_its_base_from_the_view_written_through() {
    run(&[
        Op::NewUser,
        Op::Rebuild(0, 91, 0, false),
        Op::UserWrite(0, vec![], 0, false),
        Op::UserWrite(0, vec![], 4, false),
        Op::External(0, vec![(4, false)]),
        Op::Write(0, 4, true),
        Op::Commit(0),
        Op::Publish(0),
    ]);
}

/// A user's edit, conflicting with main's value at one publish, is made
/// again over main's newer value: that conflict is over at the next.
#[test]
fn publish_drops_a_conflict_the_edit_moved_past() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 1, false),
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::Fork(0, 0),
        Op::Commit(6),
        Op::Publish(0),
        Op::Write(188, 1, false),
        Op::Fork(0, 0),
        Op::Commit(3),
        Op::UserWrite(0, vec![], 1, false),
        Op::Publish(0),
    ]);
}

/// Main's draft tombstone, written over an absence a user saw, continues
/// that absence once committed, though git had the record in between.
#[test]
fn committed_tombstone_continues_the_absence_it_was_written_over() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 3, false),
        Op::Rebuild(0, 0, 3, true),
        Op::UserWrite(0, vec![], 3, false),
        Op::Write(0, 3, true),
        Op::External(0, vec![(3, false)]),
        Op::DiskEdit(0, 3, true, FileWins::Never),
        Op::Write(0, 0, false),
        Op::Commit(0),
        Op::Publish(0),
    ]);
}

/// A record a user edited moves to the other file unchanged: the moved row
/// takes over the user's entry, so the user's layered view shows only
/// their edit. Checked on the implementation directly, since the model
/// allows a moved row to show as an extra copy.
#[test]
fn move_carries_entries_to_the_moved_row() {
    let mut world = World::new();
    for op in [
        Op::External(0, vec![(other_file(1), true)]),
        Op::NewUser,
        Op::UserWrite(0, vec![], 1, false),
        Op::Move(0, 1, false),
    ] {
        world.apply(&op);
    }
    assert!(world.model.wts[MAIN].committed.contains_key(&other_file(1)));
    let rows: BTreeSet<(Key, Ver)> = world
        .imp
        .visible(&world.imp.layered(MAIN, &[1]), None)
        .iter()
        .map(|r| &world.imp.rows[r])
        .filter(|r| !r.deleted)
        .map(|r| (r.key, r.ver))
        .collect();
    let edit = world.model.wts[1].draft[&1].ver;
    assert!(rows.contains(&(1, edit)), "{rows:?}");
    assert!(
        !rows.iter().any(|&(k, _)| k == other_file(1)),
        "the moved row shows beside the edit: {rows:?}"
    );
}

/// A record moves to the other file, then a fork at the commit before the
/// move and a rebuild: the id pairing gave the moved row carries through both.
#[test]
fn scan_pairing_keeps_a_moved_records_id() {
    run(&[
        Op::Rebuild(0, 0, 5, false),
        Op::Move(0, 5, false),
        Op::Fork(0, 1),
        Op::Rebuild(0, 8, 0, false),
    ]);
}

/// Main's edit, held back from a commit by a hand deletion, is resolved for
/// the file. Users writing and publishing after see main's value, not a
/// conflict: fold step 4 and the re-link after it keep the draft in step.
#[test]
fn fold_step_4_rewrites_a_resolved_file_row() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 2, false),
        Op::Write(0, 2, false),
        Op::DiskEdit(0, 2, true, FileWins::Never),
        Op::Commit(0),
        Op::NewUser,
        Op::NewUser,
        Op::UserWrite(1, vec![], 2, false),
        Op::Resolve(0, false, 0, false),
        Op::UserWrite(21, vec![7], 2, false),
        Op::Publish(33),
    ]);
}

/// Main moves a record a user edits into a key the user also edits. Main's
/// copy stays merged away while the user edits that record elsewhere, and
/// shows once publishing makes the edit a new record.
#[test]
fn merged_copy_shows_once_its_edit_is_a_new_record() {
    run(&[
        Op::NewUser,
        Op::Fork(0, 0),
        Op::Fork(0, 0),
        Op::External(180, vec![(3, true)]),
        Op::Fork(0, 0),
        Op::UserWrite(0, vec![], 3, false),
        Op::UserWrite(0, vec![], 0, false),
        Op::Move(84, 0, false),
        Op::Write(4, 3, false),
        Op::UserWrite(0, vec![], 3, false),
        Op::Publish(0),
    ]);
}

/// Main's edit follows its record to the other file, then the file wins over
/// it. Every entry the edit made goes with it, so after a rebuild the
/// record's old row shows again.
#[test]
fn withdrawn_edit_takes_its_entries_with_it() {
    run(&[
        Op::NewUser,
        Op::External(0, vec![(1, true)]),
        Op::Write(0, 4, false),
        Op::Move(0, 4, true),
        Op::Write(0, 4, false),
        Op::DiskEdit(0, 0, false, FileWins::Always),
        Op::DiskEdit(0, 3, false, FileWins::Always),
        Op::Rebuild(0, 68, 0, false),
    ]);
}

/// A user's edit follows its record at publish, and a new edit of another
/// record takes the key it left. Resolving the first for the file leaves
/// the old record's tombstone hidden by the second's tag.
#[test]
fn write_tags_entries_already_at_its_key() {
    run(&[
        Op::DiskEdit(0, 4, true, FileWins::Never),
        Op::Commit(0),
        Op::Fork(0, 0),
        Op::NewUser,
        Op::Fork(0, 0),
        Op::Move(120, 1, false),
        Op::External(21, vec![(4, false)]),
        Op::UserWrite(0, vec![], 1, false),
        Op::Publish(0),
        Op::UserWrite(0, vec![], 1, false),
        Op::Resolve(0, true, 0, false),
    ]);
}

/// A user's edit at a key doesn't claim main's row there of a record the
/// user edits elsewhere: the merge hid it. When publishing makes that edit
/// a new record, the row shows.
#[test]
fn write_leaves_a_merged_row_it_never_saw() {
    run(&[
        Op::NewUser,
        Op::External(0, vec![(2, true)]),
        Op::Move(0, 5, false),
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::UserWrite(0, vec![], 2, false),
        Op::DiskEdit(0, 0, false, FileWins::Never),
        Op::UserWrite(0, vec![], 5, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::Publish(0),
    ]);
}

/// A user moves their edit of a record to where main deleted it, while the
/// merge by key_id hides main's tombstone. They saw their own edit, not the
/// deletion, so publishing conflicts.
#[test]
fn own_edit_misses_a_deletion_the_merge_hides() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 5, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::External(0, vec![(2, true)]),
        Op::Move(0, 5, false),
        Op::Rebuild(0, 49, 0, false),
        Op::DiskEdit(0, 3, false, FileWins::Never),
        Op::UserWrite(0, vec![], 2, false),
        Op::Publish(0),
    ]);
}

/// Two draft rows share a record's id. Re-identifying one leaves the entries
/// at the other's key, so a rebuild can't show a row beside it.
#[test]
fn retag_leaves_another_rows_entries() {
    run(&[
        Op::External(0, vec![(5, true)]),
        Op::Rebuild(0, 0, 2, false),
        Op::Fork(0, 0),
        Op::Move(71, 2, false),
        Op::DiskEdit(43, 5, true, FileWins::Never),
        Op::DiskEdit(89, 2, false, FileWins::Never),
        Op::Rebuild(1, 188, 0, false),
    ]);
}

/// A write replacing a draft row with a new record moves the old row's
/// entries to it, so none stay behind once the row goes.
#[test]
fn write_retags_the_row_it_replaces() {
    run(&[
        Op::Fork(0, 0),
        Op::NewUser,
        Op::Delete(2),
        Op::External(0, vec![(0, false)]),
        Op::Write(0, 3, false),
        Op::NewUser,
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::UserWrite(0, vec![], 0, false),
        Op::Rebuild(0, 33, 2, true),
        Op::NewUser,
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::External(0, vec![(2, false)]),
        Op::NewUser,
        Op::Delete(2),
        Op::Rebuild(0, 69, 3, false),
    ]);
}

/// A user's edit of a record moves to another key: their own tree's row of
/// another record at the key it left shows again, in their view and in a
/// stack.
#[test]
fn replaced_edit_shows_its_own_trees_row_again() {
    run(&[
        Op::Write(0, 3, true),
        Op::Commit(0),
        Op::Move(0, 0, false),
        Op::Fork(0, 1),
        Op::Write(0, 1, false),
        Op::NewUser,
        Op::UserWrite(0, vec![], 3, false),
        Op::Rebuild(30, 24, 3, false),
        Op::UserWrite(0, vec![], 3, false),
        Op::Publish(0),
        Op::UserWrite(0, vec![], 0, false),
    ]);
}

/// A user's edit moves away from a key after seeing main's row there. Once
/// published, main's rows are the user's own tree, and the key shows it.
#[test]
fn publish_shows_main_rows_where_the_draft_holds_no_edit() {
    run(&[
        Op::Write(0, 3, false),
        Op::NewUser,
        Op::External(0, vec![(2, true)]),
        Op::UserWrite(0, vec![], 5, false),
        Op::Write(0, 3, false),
        Op::External(0, vec![(2, false), (5, true)]),
        Op::NewUser,
        Op::External(0, vec![(5, false)]),
        Op::Write(0, 0, false),
        Op::NewUser,
        Op::UserWrite(31, vec![], 2, false),
        Op::UserWrite(180, vec![], 5, false),
        Op::UserWrite(63, vec![76], 2, false),
        Op::Publish(45),
    ]);
}

/// A user's edit written over another user's edit moves to another key. The
/// draft keeps hiding what it saw at the key it left.
#[test]
fn replaced_edit_keeps_hiding_what_it_saw() {
    run(&[
        Op::NewUser,
        Op::Write(0, 1, true),
        Op::Commit(0),
        Op::UserWrite(0, vec![], 1, false),
        Op::NewUser,
        Op::Move(0, 4, false),
        Op::UserWrite(19, vec![48], 1, false),
        Op::UserWrite(7, vec![], 4, false),
    ]);
}

/// A fork at an older commit splits a segment: entries on rows the new
/// segment keeps move with them.
#[test]
fn split_moves_entries_on_rows_after_the_split_point() {
    run(&[
        Op::Fork(0, 0),
        Op::External(103, vec![(0, false)]),
        Op::External(7, vec![(1, false)]),
        Op::Fork(19, 1),
    ]);
}

/// A conflict resolved for the database's side stays resolved when a later
/// scan finds the file unchanged under it.
#[test]
fn scan_keeps_a_resolution_the_file_hasnt_moved_under() {
    run(&[
        Op::Fork(0, 0),
        Op::NewUser,
        Op::Write(14, 2, false),
        Op::Fork(0, 0),
        Op::NewUser,
        Op::External(129, vec![(2, false)]),
        Op::Delete(20),
        Op::Resolve(72, false, 0, true),
        Op::External(50, vec![(0, false)]),
    ]);
}

/// A hand edit scanned with the file winning where it diverged doesn't undo
/// a resolution the file hasn't moved under.
#[test]
fn diverged_file_leaves_a_standing_resolution() {
    run(&[
        Op::Write(0, 2, false),
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::Resolve(0, false, 0, true),
        Op::DiskEdit(0, 0, false, FileWins::Diverged),
    ]);
}

/// A split restoring a row after a move puts back the entries newer
/// segments had on it, so no second row shows at its key.
#[test]
fn split_restores_the_entries_on_a_row_it_brings_back() {
    run(&[
        Op::Rebuild(0, 0, 2, false),
        Op::Move(0, 2, false),
        Op::Write(0, 2, false),
        Op::Fork(0, 1),
    ]);
}

/// A move carries entries only an edit of the moved record made: a user's
/// edit of another record at the old key doesn't follow it.
#[test]
fn move_carries_only_its_records_entries() {
    run(&[
        Op::DiskEdit(0, 2, true, FileWins::Never),
        Op::NewUser,
        Op::UserWrite(0, vec![], 5, false),
        Op::Commit(0),
        Op::UserWrite(0, vec![], 2, false),
        Op::Move(0, 5, true),
        Op::Publish(0),
        Op::Move(0, 2, false),
        Op::DiskEdit(0, 0, false, FileWins::Never),
        Op::Publish(0),
        Op::Resolve(0, true, 0, false),
    ]);
}

/// A user's edit, moved to follow its record, is written again through
/// another user's edit of it, after rebuilds deleted the record. They saw
/// the other user's edit, not the deletion, so publishing conflicts.
#[test]
fn edit_through_another_users_edit_misses_mains_deletion() {
    run(&[
        Op::Rebuild(0, 0, 5, false),
        Op::NewUser,
        Op::Move(0, 5, false),
        Op::NewUser,
        Op::UserWrite(19, vec![], 2, false),
        Op::Rebuild(0, 2, 5, false),
        Op::Rebuild(0, 0, 0, false),
        Op::UserWrite(0, vec![], 5, false),
        Op::UserWrite(11, vec![20], 5, false),
        Op::Publish(47),
    ]);
}

/// An edit whose record moves to a key another edit holds stays put as a
/// new record, so no two files share the id.
#[test]
fn edit_that_cant_follow_becomes_a_new_record() {
    run(&[
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::Rebuild(0, 0, 0, false),
        Op::Commit(0),
        Op::Write(0, 4, false),
        Op::Write(0, 1, false),
        Op::Move(0, 1, false),
    ]);
}

/// An edit moved to its record's new key at publish keeps only the entries
/// an edit of that record made on it.
#[test]
fn relocated_edit_keeps_only_its_records_entries() {
    run(&[
        Op::Rebuild(0, 0, 0, false),
        Op::Write(0, 1, false),
        Op::NewUser,
        Op::Commit(0),
        Op::UserWrite(0, vec![], 1, false),
        Op::Move(0, 1, false),
        Op::Publish(0),
        Op::Move(0, 4, false),
        Op::NewUser,
        Op::Write(0, 4, false),
        Op::UserWrite(215, vec![188], 4, false),
        Op::Publish(32),
    ]);
}

/// A user's edit returns to the key main moved its record from, then a
/// rebuild drops the record. The user saw the key empty, but only because
/// of the move, so the edit conflicts with the deletion.
#[test]
fn edit_conflicts_with_a_deletion_it_saw_only_as_a_move() {
    run(&[
        Op::NewUser,
        Op::Rebuild(0, 125, 3, false),
        Op::Move(0, 3, false),
        Op::UserWrite(0, vec![], 0, false),
        Op::UserWrite(0, vec![], 3, false),
        Op::Rebuild(0, 0, 1, false),
        Op::Publish(0),
    ]);
}

/// Main moves a record a user then edits at the old key, and a rebuild
/// drops it. The user saw the record live at its new key, never deleted, so
/// publishing conflicts.
#[test]
fn edit_at_a_moves_old_key_misses_a_rebuilds_deletion() {
    run(&[
        Op::NewUser,
        Op::External(0, vec![(4, true)]),
        Op::UserWrite(0, vec![], 1, false),
        Op::Publish(0),
        Op::Move(0, 1, false),
        Op::UserWrite(0, vec![], 1, false),
        Op::Rebuild(0, 47, 0, false),
        Op::Publish(0),
    ]);
}

/// A user's deletion agrees with main's at publishing, and they then write
/// the record again. Their tree has held the deletion since, so it's no
/// conflict.
#[test]
fn rewrite_after_publishing_over_a_deletion_saw_it() {
    run(&[
        Op::NewUser,
        Op::Rebuild(0, 254, 0, false),
        Op::UserWrite(0, vec![], 2, true),
        Op::NewUser,
        Op::Rebuild(0, 7, 0, false),
        Op::Write(0, 0, false),
        Op::Fork(0, 0),
        Op::Rebuild(8, 3, 0, false),
        Op::Write(20, 2, false),
        Op::UserWrite(0, vec![], 0, false),
        Op::Publish(44),
        Op::Delete(220),
        Op::Write(0, 0, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::Publish(0),
    ]);
}

/// A user edits a record that main's draft then deletes and main commits.
/// Writing again, the user sees their own edit, not the deletion, so
/// publishing conflicts.
#[test]
fn own_edit_hides_mains_pending_deletion() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 0, false),
        Op::Rebuild(0, 73, 1, false),
        Op::Write(0, 0, true),
        Op::UserWrite(0, vec![], 0, false),
        Op::Write(0, 1, false),
        Op::Commit(0),
        Op::Publish(0),
    ]);
}

/// A user moves their edit of a record to a key where main, after a
/// rebuild, has the record nowhere. Their own edit still showed it live,
/// so the moved edit keeps its base and conflicts with the deletion.
#[test]
fn moved_edit_keeps_its_base_after_a_deletion() {
    run(&[
        Op::External(0, vec![(2, true)]),
        Op::NewUser,
        Op::Write(0, 0, false),
        Op::Move(0, 5, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::Rebuild(0, 29, 0, false),
        Op::UserWrite(0, vec![], 5, false),
        Op::Publish(0),
    ]);
}

/// A user edits a record, then writes it again while rewrites and moves
/// around it end with main deleting it. Their own edit is what they saw, so
/// publishing conflicts.
#[test]
fn rebuilds_deletion_under_an_own_edit_is_unseen() {
    run(&[
        Op::Write(0, 0, false),
        Op::Write(0, 0, false),
        Op::NewUser,
        Op::UserWrite(0, vec![], 3, false),
        Op::Rebuild(0, 43, 0, false),
        Op::DiskEdit(0, 0, false, FileWins::Never),
        Op::NewUser,
        Op::Write(0, 0, false),
        Op::Move(0, 0, false),
        Op::Move(0, 3, false),
        Op::UserWrite(6, vec![], 3, false),
        Op::Rebuild(0, 0, 0, true),
        Op::Publish(62),
    ]);
}

/// Main's pending edit of another record covers the key a move took a
/// user's record to. The user writes over their own edit, and a rebuild
/// then deletes the record, which they never saw deleted: a conflict.
#[test]
fn deletion_after_a_rewrite_of_an_own_edit_is_unseen() {
    run(&[
        Op::Write(0, 2, false),
        Op::Rebuild(0, 0, 5, false),
        Op::NewUser,
        Op::UserWrite(0, vec![], 5, false),
        Op::Move(0, 5, false),
        Op::UserWrite(0, vec![], 5, false),
        Op::Rebuild(0, 73, 0, false),
        Op::Publish(0),
    ]);
}

/// A user edits over main's deletion of a record. Main then creates another
/// record at that key and deletes it too: that second absence the user
/// never saw, so publishing finds a conflict.
#[test]
fn another_records_deletion_at_the_key_is_unseen() {
    run(&[
        Op::NewUser,
        Op::UserWrite(220, vec![], 1, false),
        Op::Rebuild(209, 255, 2, true),
        Op::Move(95, 2, true),
        Op::UserWrite(96, vec![], 1, false),
        Op::NewUser,
        Op::Rebuild(243, 39, 1, false),
        Op::Rebuild(55, 196, 4, false),
        Op::Rebuild(52, 47, 5, true),
        Op::Publish(140),
    ]);
}

/// A user edits a record, and main's draft and then a rebuild delete it.
/// Writing again, the user sees their own edit, not the deletion, so
/// publishing conflicts.
#[test]
fn rewriting_an_own_edit_misses_a_rebuilds_deletion() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 1, false),
        Op::Write(0, 1, true),
        Op::Rebuild(0, 187, 0, false),
        Op::UserWrite(0, vec![], 1, false),
        Op::Publish(0),
    ]);
}

/// Main moves a record, a user edits it at its old key over the move's
/// tombstone, and main then deletes it at its new key. The user saw only
/// the move, so publishing finds the deletion unseen.
#[test]
fn deletion_at_the_records_new_key_is_unseen() {
    run(&[
        Op::NewUser,
        Op::DiskEdit(0, 1, true, FileWins::Never),
        Op::Fork(0, 0),
        Op::Commit(104),
        Op::External(8, vec![(1, false), (4, true)]),
        Op::UserWrite(0, vec![], 1, false),
        Op::UserWrite(0, vec![], 4, false),
        Op::External(74, vec![(1, true)]),
        Op::Publish(0),
    ]);
}

/// Main's pending edit of a record covers a rebuild's deletion of it. A
/// user writing over that edit twice never saw the deletion.
#[test]
fn deletion_under_mains_pending_edit_is_unseen() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 4, false),
        Op::Write(0, 4, false),
        Op::Rebuild(0, 5, 0, false),
        Op::UserWrite(0, vec![], 4, false),
        Op::UserWrite(0, vec![], 4, false),
        Op::Write(0, 0, false),
        Op::Publish(0),
    ]);
}

/// Main moves a record a user edits, then a rebuild deletes it and puts a
/// new record at the user's key. The conflict is with that new value.
#[test]
fn deletion_elsewhere_under_a_new_value_conflicts_with_the_value() {
    run(&[
        Op::Fork(0, 0),
        Op::NewUser,
        Op::UserWrite(0, vec![], 2, false),
        Op::Rebuild(48, 5, 2, false),
        Op::DiskEdit(5, 5, false, FileWins::Never),
        Op::Move(20, 2, false),
        Op::Rebuild(20, 0, 2, false),
        Op::Publish(0),
    ]);
}

/// A user writes over main's deletion of a record, which main's rewrites
/// then bring back and delete again. The user saw only the first deletion.
#[test]
fn deletion_after_the_record_came_back_is_unseen() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 5, false),
        Op::External(0, vec![(5, true)]),
        Op::UserWrite(0, vec![], 5, false),
        Op::Rebuild(0, 72, 0, false),
        Op::Rebuild(0, 73, 0, false),
        Op::Publish(0),
    ]);
}

/// Main moves a record back from the key a user edits it at, and then
/// deletes it there without a new tombstone. The user saw it live at the
/// other key, so the deletion is unseen.
#[test]
fn deletion_where_the_record_was_live_when_written_is_unseen() {
    run(&[
        Op::Write(0, 0, true),
        Op::Commit(0),
        Op::Fork(0, 0),
        Op::Move(84, 3, false),
        Op::NewUser,
        Op::UserWrite(0, vec![], 0, false),
        Op::Move(20, 0, false),
        Op::Fork(0, 1),
        Op::UserWrite(0, vec![], 0, false),
        Op::Fork(1, 0),
        Op::Fork(1, 0),
        Op::External(15, vec![(3, true)]),
        Op::Publish(0),
    ]);
}

/// A user writes over another user's deletion of a record that main's
/// rebuild also deleted. What they saw was the other user's tombstone, not
/// main's deletion.
#[test]
fn another_users_tombstone_hides_mains_deletion() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 3, false),
        Op::Rebuild(0, 61, 0, false),
        Op::NewUser,
        Op::Write(0, 0, false),
        Op::Write(0, 0, false),
        Op::UserWrite(91, vec![], 3, true),
        Op::Write(0, 0, false),
        Op::UserWrite(36, vec![43], 3, false),
        Op::Publish(44),
    ]);
}

/// Main rewrites its pending deletion of a record that one user edited
/// over. The new tombstone takes over that user's entry, so a second user
/// writing through the first never saw the deletion.
#[test]
fn rewritten_tombstone_stays_hidden_under_an_edit() {
    run(&[
        Op::NewUser,
        Op::NewUser,
        Op::Fork(0, 0),
        Op::NewUser,
        Op::UserWrite(55, vec![], 0, false),
        Op::DiskEdit(8, 0, true, FileWins::Never),
        Op::UserWrite(150, vec![], 0, false),
        Op::Write(188, 0, true),
        Op::Delete(122),
        Op::UserWrite(55, vec![234], 0, false),
        Op::NewUser,
        Op::UserWrite(0, vec![], 0, false),
        Op::Commit(0),
        Op::Publish(37),
    ]);
}

/// Main's pending tombstone continues a rebuild's absence that one user
/// edited over, taking over their entry. A second user writing through
/// the first never saw it.
#[test]
fn continuing_tombstone_stays_hidden_under_an_edit() {
    run(&[
        Op::NewUser,
        Op::NewUser,
        Op::Fork(0, 0),
        Op::NewUser,
        Op::UserWrite(55, vec![], 0, false),
        Op::Rebuild(2, 55, 1, false),
        Op::UserWrite(150, vec![], 0, false),
        Op::Write(188, 0, true),
        Op::Delete(122),
        Op::UserWrite(55, vec![234], 0, false),
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::NewUser,
        Op::UserWrite(0, vec![], 0, false),
        Op::Commit(0),
        Op::Publish(37),
    ]);
}

/// A fork at an older commit splits a segment where one key's value at
/// that commit is the row below, and another's is restored. The restored
/// row can't take the id the row below keeps.
#[test]
fn split_skips_an_id_the_row_below_keeps() {
    run(&[
        Op::Rebuild(0, 0, 3, false),
        Op::Fork(0, 0),
        Op::Move(32, 3, false),
        Op::NewUser,
        Op::Rebuild(62, 190, 3, false),
        Op::External(6, vec![(3, true)]),
        Op::Delete(6),
        Op::Move(0, 0, false),
        Op::External(0, vec![(0, false)]),
        Op::Write(0, 3, false),
        Op::Fork(0, 3),
        Op::Rebuild(224, 190, 1, false),
    ]);
}

/// A user's edit of a record main then deleted is rebased at a publish onto
/// main's rebuilt tree, which has another record at the key. Main then
/// deletes that record, and the commit folds its own old tombstone over the
/// deletion. The user never saw that deletion.
#[test]
fn deletion_after_an_earlier_publish_is_unseen() {
    run(&[
        Op::NewUser,
        Op::External(0, vec![(0, true)]),
        Op::External(0, vec![(0, false)]),
        Op::UserWrite(0, vec![], 0, false),
        Op::Write(0, 0, true),
        Op::Rebuild(0, 12, 1, false),
        Op::Write(0, 1, false),
        Op::Publish(0),
        Op::External(0, vec![(0, true)]),
        Op::Commit(0),
        Op::Publish(0),
    ]);
}

/// A user edits a record main then deletes, and writes again at a key where
/// main deleted another record it had moved there. Their own edits are what
/// they saw, so both deletions conflict.
#[test]
fn own_edit_under_a_merged_tombstone_misses_the_deletion() {
    run(&[
        Op::NewUser,
        Op::Fork(0, 0),
        Op::UserWrite(0, vec![], 4, false),
        Op::NewUser,
        Op::Delete(142),
        Op::External(0, vec![(4, true)]),
        Op::Move(0, 1, false),
        Op::UserWrite(0, vec![], 1, false),
        Op::Write(0, 4, true),
        Op::UserWrite(14, vec![], 4, false),
        Op::Fork(0, 0),
        Op::Delete(164),
        Op::Commit(0),
        Op::Publish(56),
    ]);
}

/// A move carries a user's entry onto main's tombstone at the record's new
/// key, where another user writes through the first over their own edit.
/// They saw their edit, not main's deletion, so publishing conflicts.
#[test]
fn own_edit_misses_a_deletion_another_drafts_entry_hides() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 5, false),
        Op::Write(0, 2, true),
        Op::External(0, vec![(5, true)]),
        Op::NewUser,
        Op::NewUser,
        Op::UserWrite(232, vec![], 2, false),
        Op::Move(0, 2, false),
        Op::Commit(0),
        Op::UserWrite(66, vec![25], 5, false),
        Op::Publish(39),
    ]);
}

/// A user edits a record, main moves it and a rebuild deletes it, and the
/// user writes again while main's pending row covers the key. Their own
/// edit still showed the record, so the edit keeps its base and conflicts.
#[test]
fn write_over_mains_pending_row_keeps_the_edits_base() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 2, false),
        Op::External(0, vec![(5, true)]),
        Op::Move(0, 2, false),
        Op::UserWrite(0, vec![], 2, false),
        Op::Rebuild(0, 181, 0, false),
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::UserWrite(0, vec![], 2, false),
        Op::Publish(0),
    ]);
}

/// A user writes through another user's deletion of a record that main
/// has moved. Merged by key_id, the stack shows the record deleted, so the
/// write re-creates it, and main deleting its copy later is no conflict.
#[test]
fn write_through_a_lower_deletion_re_creates_the_record() {
    run(&[
        Op::NewUser,
        Op::NewUser,
        Op::UserWrite(73, vec![], 0, true),
        Op::DiskEdit(0, 3, true, FileWins::Never),
        Op::Write(0, 0, false),
        Op::Commit(0),
        Op::Move(0, 0, false),
        Op::UserWrite(20, vec![67], 0, false),
        Op::External(0, vec![(3, true)]),
        Op::Publish(68),
    ]);
}

/// A user's edit that another user's edit superseded follows its record to
/// another file, then can't follow it again at publishing and becomes a
/// new record. The other user's entry was about the old record, so the
/// stack shows the new one.
#[test]
fn renewed_edit_drops_entries_on_the_old_record() {
    run(&[
        Op::NewUser,
        Op::NewUser,
        Op::Write(0, 4, false),
        Op::UserWrite(110, vec![], 4, false),
        Op::External(0, vec![(1, true)]),
        Op::UserWrite(13, vec![152], 4, false),
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::Move(0, 4, false),
        Op::Publish(104),
        Op::UserWrite(86, vec![], 4, false),
        Op::Rebuild(0, 177, 0, false),
        Op::Publish(44),
    ]);
}

/// A scan deletes main's head row in place and compaction drops the
/// tombstone below, leaving the head with no rows but the entry hiding the
/// old record. A fork at the head has to keep it.
#[test]
fn fork_keeps_a_head_with_only_entries() {
    run(&[
        Op::NewUser,
        Op::NewUser,
        Op::Rebuild(0, 104, 4, true),
        Op::NewUser,
        Op::External(0, vec![(4, false)]),
        Op::External(0, vec![(4, true)]),
        Op::Delete(29),
        Op::Fork(0, 0),
    ]);
}

/// A user's edit that can't follow its record at publishing becomes a new
/// record at a key main has nothing at. Whatever absence the user saw
/// there, an added record conflicts with none.
#[test]
fn renewed_edit_over_an_absence_is_no_conflict() {
    run(&[
        Op::Rebuild(0, 0, 0, false),
        Op::Write(0, 5, false),
        Op::External(0, vec![(2, false)]),
        Op::Move(0, 2, false),
        Op::NewUser,
        Op::UserWrite(0, vec![], 2, false),
        Op::Write(0, 5, true),
        Op::Fork(0, 1),
        Op::Rebuild(20, 129, 0, false),
        Op::UserWrite(0, vec![], 5, false),
        Op::Publish(0),
    ]);
}

/// A split restores a record at a key whose row below is another record
/// that moved to a key the split also restores. The row below's id
/// belongs to that key, not this one.
#[test]
fn split_leaves_the_row_belows_id_to_the_record_that_moved() {
    run(&[
        Op::Rebuild(0, 0, 2, false),
        Op::Rebuild(0, 0, 1, false),
        Op::NewUser,
        Op::Move(0, 1, false),
        Op::External(0, vec![(1, false)]),
        Op::External(0, vec![(1, true), (4, false)]),
        Op::Fork(0, 1),
        Op::Fork(1, 2),
        Op::Write(229, 4, false),
        Op::Rebuild(136, 15, 2, false),
    ]);
}

/// Main moves a record and deletes it with the other key's new record. A
/// split restoring both can't give the new record the moved one's id.
#[test]
fn split_gives_a_moved_records_old_row_id_to_the_record() {
    run(&[
        Op::Rebuild(0, 0, 2, false),
        Op::Rebuild(0, 0, 1, false),
        Op::NewUser,
        Op::Move(0, 1, false),
        Op::External(0, vec![(1, false)]),
        Op::External(0, vec![(1, true), (4, true)]),
        Op::Fork(0, 1),
        Op::Fork(1, 2),
        Op::Write(229, 4, false),
        Op::Rebuild(136, 15, 2, false),
    ]);
}

/// A user's deletion held back by the fold while another user's worktree is
/// deleted around it. Folding away the tombstone that still hides a row
/// would lose that user's conflict at publishing.
#[test]
fn fold_keeps_a_tombstone_that_still_hides_a_row() {
    run(&[
        Op::NewUser,
        Op::UserWrite(0, vec![], 4, false),
        Op::Rebuild(0, 55, 0, false),
        Op::Publish(0),
        Op::NewUser,
        Op::Fork(0, 0),
        Op::Delete(8),
        Op::UserWrite(92, vec![], 4, true),
        Op::NewUser,
        Op::External(0, vec![(4, false)]),
        Op::Delete(16),
        Op::DiskEdit(0, 4, true, FileWins::Never),
        Op::Delete(13),
        Op::Commit(0),
        Op::Publish(0),
    ]);
}

/// A user writes through another user's draft, whose entry hides main's
/// tombstone at the key. With no edit of the other user there, what they
/// saw was main's absence, and the write supersedes that tombstone.
#[test]
fn write_supersedes_mains_tombstone_another_draft_hides() {
    run(&[
        Op::NewUser,
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::UserWrite(0, vec![], 1, true),
        Op::NewUser,
        Op::Write(0, 1, false),
        Op::DiskEdit(0, 0, false, FileWins::Always),
        Op::UserWrite(1, vec![20], 1, false),
        Op::Write(0, 0, false),
        Op::Rebuild(0, 43, 0, false),
        Op::Publish(47),
    ]);
}

/// A user edits over main's pending file value; main replaces it with its
/// own edit, taking the user's entry with it, and a rebuild re-creates the
/// value as another record. The user's next write settles that record, so
/// publishing leaves the edit where it is though its record is elsewhere:
/// the re-created row over-report (§4.7).
#[test]
fn edit_settles_a_record_seen_only_in_a_re_created_row() {
    run(&[
        Op::Rebuild(0, 0, 1, false),
        Op::NewUser,
        Op::Move(0, 1, false),
        Op::DiskEdit(0, 4, false, FileWins::Never),
        Op::UserWrite(0, vec![], 4, false),
        Op::Write(0, 4, false),
        Op::Rebuild(0, 6, 0, false),
        Op::Commit(0),
        Op::UserWrite(0, vec![], 4, false),
        Op::Publish(0),
    ]);
}

/// A rebuild onto no base makes a new root, and a merge of the unrelated
/// histories merges against an empty tree.
#[test]
fn merge_of_unrelated_histories_uses_an_empty_base() {
    run(&[
        Op::Fork(0, 0),
        Op::Rebuild(0, 83, 0, false),
        Op::Merge(0, vec![(253, 0)], MergeKind::Regular, 0, vec![]),
    ]);
}

/// A merge brings back a version a user edited over, after a rebuild dropped
/// it. The user's entry carries to it, so they don't see it again.
#[test]
fn merged_back_version_takes_the_users_entry() {
    run(&[
        Op::NewUser,
        Op::External(0, vec![(0, false)]),
        Op::Fork(0, 0),
        Op::UserWrite(0, vec![], 1, false),
        Op::Rebuild(44, 80, 2, false),
        Op::Merge(30, vec![(71, 0)], MergeKind::Regular, 0, vec![]),
    ]);
}

/// A merge brings back a version a user saw at its key while editing another
/// record there: their entry at that key carries to it.
#[test]
fn merged_back_version_takes_entries_made_at_its_key() {
    run(&[
        Op::DiskEdit(0, 3, false, FileWins::Never),
        Op::Fork(0, 0),
        Op::NewUser,
        Op::Rebuild(30, 11, 0, false),
        Op::UserWrite(0, vec![], 3, false),
        Op::Merge(72, vec![(29, 0)], MergeKind::Regular, 0, vec![]),
    ]);
}

/// A user writes through another's draft at a key where main holds a
/// version the writer's own tree showed. The write supersedes main's copy
/// too, though the lower draft hides it.
#[test]
fn write_supersedes_mains_copy_a_lower_draft_hides() {
    run(&[
        Op::Fork(0, 0),
        Op::NewUser,
        Op::Rebuild(20, 43, 0, false),
        Op::NewUser,
        Op::Merge(8, vec![(89, 0)], MergeKind::Regular, 0, vec![]),
        Op::UserWrite(29, vec![], 1, false),
        Op::NewUser,
        Op::UserWrite(93, vec![82], 1, false),
    ]);
}

/// A rebase replays a commit made outside git-sync: no rollup, so the
/// record's id comes from the rows.
#[test]
fn replayed_commit_without_a_rollup_names_no_record() {
    run(&[
        Op::Rebuild(0, 0, 0, false),
        Op::Fork(0, 0),
        Op::Rebuild(74, 43, 0, true),
        Op::Merge(128, vec![(55, 0)], MergeKind::Rebase, 0, vec![]),
        Op::Write(0, 0, false),
        Op::Merge(72, vec![(7, 0)], MergeKind::Regular, 0, vec![]),
    ]);
}

/// Two fast-forwards to another worktree's commits: the ids before each are
/// the merging worktree's own, not the other's for that commit.
#[test]
fn fast_forward_keeps_the_worktrees_own_ids() {
    run(&[
        Op::External(0, vec![(3, true)]),
        Op::Fork(0, 1),
        Op::Move(2, 0, false),
        Op::External(12, vec![(0, true)]),
        Op::Move(30, 3, false),
        Op::Merge(29, vec![(92, 2)], MergeKind::Regular, 0, vec![]),
        Op::Merge(7, vec![(44, 0)], MergeKind::Regular, 0, vec![]),
    ]);
}

/// A merge brings back the version a user's edit was made over, after a
/// rebuild collected the row the user saw: the edit's base finds it.
#[test]
fn merged_back_version_takes_the_entry_of_an_edit_made_over_it() {
    run(&[
        Op::Fork(0, 0),
        Op::Rebuild(44, 49, 1, false),
        Op::NewUser,
        Op::Merge(20, vec![(103, 0)], MergeKind::Regular, 0, vec![]),
        Op::UserWrite(0, vec![], 0, false),
        Op::Fork(49, 0),
        Op::Rebuild(177, 0, 1, false),
        Op::Merge(33, vec![(1, 0)], MergeKind::Regular, 0, vec![]),
    ]);
}

/// A merge brings back an old version as a second copy of a record another
/// key holds: it gets a new id, and no key after it reuses one given.
#[test]
fn merge_gives_a_repeated_version_a_new_id() {
    run(&[
        Op::Fork(0, 0),
        Op::Fork(0, 0),
        Op::Write(11, 3, false),
        Op::Rebuild(53, 187, 3, false),
        Op::Move(176, 3, false),
        Op::Merge(68, vec![(0, 0)], MergeKind::Regular, 91, vec![]),
    ]);
}

/// A squash merge brings a moved version to main at a key another record
/// holds. An entry a move carried there was about the moved record, so it
/// doesn't hide main's new one.
#[test]
fn merged_back_version_ignores_entries_a_move_carried() {
    run(&[
        Op::NewUser,
        Op::Rebuild(0, 19, 4, false),
        Op::UserWrite(0, vec![], 4, false),
        Op::Write(0, 0, false),
        Op::Fork(0, 0),
        Op::Fork(0, 0),
        Op::Delete(25),
        Op::Fork(0, 0),
        Op::Write(0, 0, false),
        Op::Move(8, 4, false),
        Op::Merge(45, vec![(29, 0)], MergeKind::Squash, 0, vec![(4, false)]),
    ]);
}

/// A merge brings back main's original version after a rebuild collected
/// the row a user saw: it's a re-created row, allowed as a copy.
#[test]
fn merged_back_version_is_a_re_created_row() {
    run(&[
        Op::NewUser,
        Op::Rebuild(0, 44, 4, false),
        Op::UserWrite(0, vec![], 4, false),
        Op::Publish(0),
        Op::Fork(0, 0),
        Op::Rebuild(30, 47, 0, false),
        Op::Merge(104, vec![(49, 1)], MergeKind::Regular, 0, vec![]),
    ]);
}

/// A merge into a fork brings a version a user's edit was made over. Users
/// layer over main only, so the fork's row gets no user entry, and
/// publishing still finds the user's conflict with main.
#[test]
fn fork_merges_give_users_no_entries() {
    run(&[
        Op::NewUser,
        Op::External(0, vec![(1, false)]),
        Op::Fork(0, 1),
        Op::Fork(0, 0),
        Op::DiskEdit(3, 1, false, FileWins::Never),
        Op::UserWrite(0, vec![], 1, true),
        Op::Merge(4, vec![(2, 0)], MergeKind::Regular, 0, vec![]),
        Op::Publish(0),
    ]);
}

/// A rebase replays git-sync commits; a later merge of the replay finds the
/// source's ids in their rollups, whatever the rebasing worktree gave them.
#[test]
fn replayed_commits_record_their_sources_ids() {
    run(&[
        Op::Write(0, 0, true),
        Op::Commit(0),
        Op::DiskEdit(0, 3, false, FileWins::Never),
        Op::External(0, vec![(1, false)]),
        Op::Commit(0),
        Op::Fork(0, 2),
        Op::Fork(0, 0),
        Op::Move(106, 3, false),
        Op::Merge(76, vec![(44, 0)], MergeKind::Rebase, 0, vec![]),
        Op::Fork(0, 1),
        Op::Fork(0, 0),
        Op::Merge(18, vec![(16, 0)], MergeKind::Regular, 0, vec![]),
    ]);
}

/// A merge moves the record of an edit conflicting with a hand edit. The
/// edit follows it, and the file it left is reconciled, so the hand edit
/// stays pending and is committed.
#[test]
fn scan_reconciles_the_file_an_edit_followed_its_record_out_of() {
    run(&[
        Op::Rebuild(0, 0, 4, false),
        Op::Write(0, 4, false),
        Op::Fork(0, 0),
        Op::DiskEdit(49, 4, false, FileWins::Never),
        Op::Commit(13),
        Op::Move(20, 4, false),
        Op::Rebuild(152, 85, 2, false),
        Op::DiskEdit(50, 1, false, FileWins::Never),
        Op::NewUser,
        Op::Move(19, 4, false),
        Op::Merge(162, vec![(31, 1)], MergeKind::Regular, 0, vec![]),
        Op::Commit(110),
    ]);
}

/// A scan reconciles the files edits left in it, not ones left earlier and
/// already reconciled.
#[test]
fn scan_reconciles_only_files_left_in_this_scan() {
    run(&[
        Op::Write(0, 1, false),
        Op::DiskEdit(0, 0, false, FileWins::Never),
        Op::Rebuild(0, 0, 1, false),
        Op::Write(0, 1, false),
        Op::Move(0, 1, false),
        Op::Write(0, 0, false),
        Op::External(0, vec![(3, false)]),
    ]);
}

/// Main fast-forwards to another worktree's commit and later rebuilds onto
/// it: the base's ids are main's own.
#[test]
fn rebuild_onto_a_fast_forwarded_commit_uses_the_worktrees_ids() {
    run(&[
        Op::Fork(0, 0),
        Op::Fork(0, 0),
        Op::Write(107, 0, false),
        Op::Delete(32),
        Op::Rebuild(0, 71, 0, true),
        Op::External(0, vec![(0, false)]),
        Op::Merge(29, vec![(126, 0)], MergeKind::Regular, 0, vec![]),
        Op::NewUser,
        Op::Merge(44, vec![(13, 0)], MergeKind::Regular, 0, vec![]),
        Op::NewUser,
        Op::External(48, vec![(0, false)]),
        Op::Publish(0),
        Op::Rebuild(72, 85, 0, false),
    ]);
}

/// A split restores a key whose row below is a record deleted before the
/// split point: the key didn't hold a record throughout, so its id isn't the
/// row below's.
#[test]
fn split_row_below_needs_the_key_held_since() {
    run(&[
        Op::DiskEdit(0, 5, true, FileWins::Never),
        Op::Commit(0),
        Op::Move(0, 2, false),
        Op::Fork(0, 2),
        Op::External(126, vec![(5, true)]),
        Op::Fork(104, 1),
        Op::Rebuild(165, 151, 0, false),
    ]);
}

/// A split at an older commit gives a record a new id where no rollup names
/// it; the worktree forked from shares that segment, so its ids there change
/// too.
#[test]
fn split_that_loses_an_id_changes_the_forked_worktrees_too() {
    run(&[
        Op::External(0, vec![(0, false)]),
        Op::External(0, vec![(0, false)]),
        Op::External(0, vec![(0, true)]),
        Op::Fork(0, 1),
        Op::Rebuild(110, 6, 0, false),
    ]);
}

/// A rebuild onto a segment a split made takes the base's ids from its rows
/// where no rollup names them.
#[test]
fn rebuild_onto_a_split_base_takes_its_ids() {
    run(&[
        Op::External(0, vec![(2, false)]),
        Op::External(0, vec![(2, true)]),
        Op::External(0, vec![(0, false)]),
        Op::External(0, vec![(0, false)]),
        Op::Write(0, 0, false),
        Op::Fork(0, 2),
        Op::Fork(43, 1),
        Op::Rebuild(88, 77, 0, false),
        Op::Rebuild(189, 20, 0, false),
    ]);
}

/// A split's moved-record id is only as good as the scan that gave it: one
/// another key of the tree holds is passed over.
#[test]
fn split_moved_id_stays_unique() {
    run(&[
        Op::Fork(0, 0),
        Op::Write(187, 0, true),
        Op::Fork(0, 0),
        Op::Commit(43),
        Op::Move(112, 3, false),
        Op::Merge(33, vec![(136, 0)], MergeKind::Regular, 0, vec![]),
        Op::Delete(0),
        Op::Delete(0),
        Op::Fork(0, 0),
        Op::Fork(0, 1),
        Op::NewUser,
        Op::External(44, vec![(3, false)]),
    ]);
}

/// A squash, a rebase and a regular merge in turn: a value set by a replayed
/// commit in the middle of a rebase takes its source's id.
#[test]
fn merge_through_a_replayed_commit_reads_its_sources_ids() {
    run(&[
        Op::Write(0, 0, false),
        Op::Commit(0),
        Op::External(0, vec![(1, false)]),
        Op::Fork(0, 0),
        Op::NewUser,
        Op::Rebuild(29, 73, 1, false),
        Op::Write(8, 1, false),
        Op::Commit(104),
        Op::Fork(47, 0),
        Op::External(9, vec![(0, false)]),
        Op::Merge(137, vec![(204, 1)], MergeKind::Squash, 0, vec![]),
        Op::Merge(29, vec![(21, 1)], MergeKind::Rebase, 0, vec![]),
        Op::Merge(9, vec![(29, 0)], MergeKind::Regular, 13, vec![]),
    ]);
}

/// A rebase brings back a record's old content where the worktree had moved
/// the record, so its scan gave the key a new id. A fork there can't tell
/// from the rollup, which names the source's: the split's id is a loss.
#[test]
fn split_rollup_proves_only_what_the_worktree_gave() {
    run(&[
        Op::External(0, vec![(5, true)]),
        Op::Write(0, 2, false),
        Op::Commit(0),
        Op::External(0, vec![(0, false)]),
        Op::Write(0, 0, false),
        Op::Fork(0, 2),
        Op::Move(215, 2, false),
        Op::Merge(73, vec![(12, 0)], MergeKind::Rebase, 0, vec![]),
        Op::Move(0, 0, false),
        Op::External(49, vec![(0, false)]),
        Op::External(181, vec![(5, false), (2, false)]),
        Op::Fork(13, 1),
    ]);
}

/// A split after resolved conflicts and commits: the strongest evidence for
/// each re-created key's id is used across all keys before weaker kinds.
#[test]
fn split_resolves_ids_by_tier_across_keys() {
    run(&[
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::Write(0, 3, false),
        Op::NewUser,
        Op::Write(0, 2, false),
        Op::Rebuild(0, 13, 0, false),
        Op::Resolve(0, false, 43, false),
        Op::Commit(0),
        Op::External(0, vec![(0, false)]),
        Op::Resolve(0, false, 0, true),
        Op::Commit(0),
        Op::Fork(0, 1),
    ]);
}

/// A merge brings a value whose rollup names a record another key of the
/// tree keeps: the scan passes over that id.
#[test]
fn scan_passes_over_a_rollup_id_another_key_keeps() {
    run(&[
        Op::DiskEdit(0, 5, false, FileWins::Never),
        Op::Rebuild(0, 0, 5, false),
        Op::Commit(0),
        Op::Fork(0, 1),
        Op::External(66, vec![(0, false)]),
        Op::Write(0, 0, false),
        Op::Move(1, 5, false),
        Op::Fork(0, 0),
        Op::Merge(223, vec![(104, 0)], MergeKind::Regular, 32, vec![]),
        Op::External(142, vec![(2, false)]),
    ]);
}

/// After rebase and regular merges move records around, an edit that can't
/// follow its record again becomes a new one; other drafts' entries about
/// the old record go.
#[test]
fn renewed_edit_after_rebase_merges_drops_other_entries() {
    run(&[
        Op::Rebuild(0, 0, 1, false),
        Op::Fork(0, 0),
        Op::External(56, vec![(3, false)]),
        Op::Merge(49, vec![(56, 0)], MergeKind::Rebase, 0, vec![]),
        Op::Move(49, 1, false),
        Op::NewUser,
        Op::Move(8, 3, false),
        Op::Merge(72, vec![(187, 0)], MergeKind::Rebase, 0, vec![]),
        Op::Merge(2, vec![(89, 0)], MergeKind::Regular, 0, vec![]),
        Op::External(158, vec![(1, false)]),
        Op::Delete(30),
        Op::External(0, vec![(3, false)]),
        Op::Fork(0, 1),
        Op::Write(115, 0, false),
    ]);
}

/// A split after merges and moves: rows written after the split point go to
/// the new segment.
#[test]
fn split_moves_late_rows_after_merges() {
    run(&[
        Op::Fork(0, 0),
        Op::Rebuild(44, 115, 0, false),
        Op::Write(19, 2, false),
        Op::Fork(0, 0),
        Op::Write(100, 5, true),
        Op::Commit(46),
        Op::Move(0, 0, false),
        Op::Merge(33, vec![(19, 0)], MergeKind::Regular, 0, vec![]),
        Op::Move(4, 2, false),
        Op::External(66, vec![(0, false)]),
        Op::Merge(142, vec![(66, 0)], MergeKind::Rebase, 0, vec![]),
        Op::Fork(55, 2),
    ]);
}

/// A split after rebase and regular merges restores a record that moved
/// after the split point: the moved row gives its id.
#[test]
fn split_finds_a_record_moved_after_the_split_point() {
    run(&[
        Op::Write(0, 2, true),
        Op::Fork(0, 0),
        Op::Commit(2),
        Op::Write(5, 5, false),
        Op::Move(134, 5, false),
        Op::Commit(55),
        Op::NewUser,
        Op::Merge(44, vec![(19, 0)], MergeKind::Rebase, 0, vec![]),
        Op::Merge(104, vec![(125, 0)], MergeKind::Regular, 0, vec![]),
        Op::Write(44, 0, false),
        Op::Fork(14, 0),
        Op::Rebuild(106, 0, 0, false),
        Op::Fork(2, 1),
        Op::Commit(28),
        Op::Merge(41, vec![(4, 0)], MergeKind::Regular, 0, vec![]),
    ]);
}

/// After merges and moves, a write adds its tag to the entries its draft
/// already has at its key.
#[test]
fn write_joins_entries_at_its_key_after_merges() {
    run(&[
        Op::Rebuild(0, 0, 4, false),
        Op::Fork(0, 0),
        Op::Fork(0, 0),
        Op::Rebuild(202, 181, 0, false),
        Op::Merge(91, vec![(23, 0)], MergeKind::Rebase, 0, vec![]),
        Op::Move(80, 4, false),
        Op::Merge(83, vec![(79, 0)], MergeKind::Regular, 0, vec![]),
        Op::External(11, vec![(4, false)]),
        Op::Delete(68),
        Op::Move(5, 0, false),
        Op::Fork(103, 2),
        Op::Delete(126),
        Op::External(91, vec![(4, false)]),
    ]);
}

/// A merge whose first parent with the value is a squash, and a later one
/// reaches the commit git-sync made: its rollup names the record.
#[test]
fn merge_finds_the_rollup_past_a_squash() {
    run(&[
        Op::Rebuild(0, 0, 2, false),
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::Commit(0),
        Op::Fork(0, 1),
        Op::External(84, vec![(2, false)]),
        Op::External(1, vec![(1, false)]),
        Op::Fork(1, 1),
        Op::Merge(44, vec![(63, 0)], MergeKind::Squash, 0, vec![]),
        Op::Merge(130, vec![(80, 0), (63, 0)], MergeKind::Regular, 164, vec![]),
    ]);
}

/// An edit made over a record main moved supersedes the record's copy in
/// the other file, which publishing would otherwise report as a conflict.
#[test]
fn write_supersedes_the_records_copy_in_another_file() {
    run_exact(&[
        Op::Rebuild(0, 0, 2, false),
        Op::NewUser,
        Op::Move(0, 2, false),
        Op::UserWrite(0, vec![], 5, true),
        Op::Rebuild(0, 32, 0, false),
        Op::Publish(0),
    ]);
}

/// A version a merge brings back into main takes the entries drafts made
/// over it, so a layered user doesn't see it as a copy.
#[test]
fn merge_brought_back_version_takes_the_entries_made_over_it() {
    run_exact(&[
        Op::Fork(0, 0),
        Op::NewUser,
        Op::Fork(0, 0),
        Op::Fork(0, 0),
        Op::UserWrite(0, vec![], 0, false),
        Op::NewUser,
        Op::Rebuild(20, 91, 1, false),
        Op::NewUser,
        Op::Delete(122),
        Op::Fork(1, 0),
        Op::Merge(4, vec![(1, 0)], MergeKind::Regular, 0, vec![]),
    ]);
}

/// A record deleted, committed, and added back is a new record: its old
/// id is never reused, though its row, the highest, is gone.
#[test]
fn re_added_record_is_a_new_one() {
    run(&[
        Op::DiskEdit(0, 5, true, FileWins::Never),
        Op::Commit(0),
        Op::DiskEdit(0, 5, false, FileWins::Never),
    ]);
}

/// `force` takes every file's value over pending edits, not only the
/// file just edited.
#[test]
fn force_takes_every_files_value() {
    run(&[
        Op::Write(0, 0, false),
        Op::External(0, vec![(0, false)]),
        Op::DiskEdit(0, 3, false, FileWins::Always),
    ]);
}

/// A conflict resolved for the file's side takes the file's value in,
/// not as a pending edit: a commit that changes the record replaces it
/// without a conflict.
#[test]
fn file_side_resolution_is_not_a_pending_edit() {
    run(&[
        Op::Write(0, 5, false),
        Op::DiskEdit(0, 5, false, FileWins::Never),
        Op::Resolve(0, false, 0, false),
        Op::External(0, vec![(5, false)]),
    ]);
}

/// A committed row where the chain has no record takes the one the draft
/// holds at its key: the file's value taken in, edited since, then
/// committed with the edit held back by a conflict.
#[test]
fn committed_row_takes_the_drafts_record_at_its_key() {
    run(&[
        Op::External(0, vec![(3, true)]),
        Op::DiskEdit(0, 3, false, FileWins::Never),
        Op::Write(0, 3, false),
        Op::External(0, vec![(4, false)]),
        Op::Commit(0),
    ]);
}

/// An edit that made its record, stranded when a move takes the record to
/// a key another edit holds, becomes a new record with an id of its own:
/// its version is its old record's id.
#[test]
fn stranded_edit_that_made_its_record_gets_a_new_id() {
    run(&[
        Op::Rebuild(0, 0, 2, false),
        Op::Write(0, 4, false),
        Op::External(0, vec![(4, false)]),
        Op::Write(0, 1, false),
        Op::Move(0, 4, false),
        Op::DiskEdit(0, 0, false, FileWins::Diverged),
    ]);
}

/// Resolving for the file's side takes the file's value in as the record
/// the edit was of, where the chain has none at the key.
#[test]
fn file_side_resolution_continues_the_edits_record() {
    run(&[
        Op::External(0, vec![(3, true)]),
        Op::DiskEdit(0, 3, false, FileWins::Never),
        Op::Write(0, 3, false),
        Op::External(0, vec![(4, false)]),
        Op::Resolve(0, false, 0, false),
    ]);
}

/// An edit that followed its record into another file is checked against
/// the version it was made over, not what that file held at the base.
#[test]
fn followed_edit_keeps_its_base_across_files() {
    run(&[
        Op::Write(0, 3, true),
        Op::Commit(0),
        Op::Write(0, 0, false),
        Op::Move(0, 0, false),
    ]);
}

/// A pending delete the file already agrees with writes nothing, so the
/// commit that follows has nothing to carry.
#[test]
fn delete_the_file_already_made_writes_nothing() {
    run(&[
        Op::Write(0, 4, true),
        Op::External(0, vec![(4, true)]),
        Op::Commit(0),
    ]);
}

/// A value taken in from the file where git has since moved its record
/// becomes a new record, so two places don't share the id.
#[test]
fn taken_in_value_whose_record_moved_is_a_new_one() {
    run(&[
        Op::DiskEdit(0, 5, false, FileWins::Never),
        Op::External(0, vec![(2, true)]),
        Op::Move(0, 5, false),
    ]);
}

/// An edit whose record moves onto a key another edit holds stays where
/// it is, as a new record.
#[test]
fn edit_following_onto_another_edit_is_a_new_one() {
    run(&[
        Op::External(0, vec![(2, true)]),
        Op::Write(0, 2, false),
        Op::Write(0, 5, false),
        Op::Move(0, 5, false),
    ]);
}

/// Found with the rollup ids: a record the file deleted under a pending
/// edit keeps its id through the commit that carries the deletion.
#[test]
fn file_deletion_under_an_edit_keeps_the_id() {
    run(&[
        Op::Write(0, 1, true),
        Op::Commit(0),
        Op::Write(0, 1, false),
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::Write(0, 4, false),
        Op::DiskEdit(0, 4, true, FileWins::Never),
        Op::Commit(0),
    ]);
}

/// A pending delete of a hand edit's record, over a key an outside commit
/// already deleted, gives the commit nothing to carry.
#[test]
fn deleting_a_hand_edits_record_commits_nothing() {
    run(&[
        Op::External(0, vec![(4, true)]),
        Op::DiskEdit(0, 4, false, FileWins::Never),
        Op::Write(0, 4, true),
        Op::Commit(0),
    ]);
}

/// A commit after an outside change has nothing to carry, and makes none.
#[test]
fn commit_after_an_outside_change_makes_none() {
    run(&[
        Op::Write(0, 0, false),
        Op::Move(0, 0, false),
        Op::Write(0, 0, false),
        Op::Commit(0),
        Op::External(0, vec![(3, false)]),
        Op::Commit(0),
    ]);
}

/// A hand edit deleting a record already deleted changes nothing, with an
/// edit pending over another hand edit.
#[test]
fn hand_deleting_a_deleted_record_changes_nothing() {
    run(&[
        Op::Write(0, 5, true),
        Op::Commit(0),
        Op::DiskEdit(0, 4, false, FileWins::Never),
        Op::Write(0, 4, false),
        Op::DiskEdit(0, 5, true, FileWins::Never),
    ]);
}

/// An edit follows its record to the other file and back, keeping its id.
#[test]
fn edit_follows_its_record_there_and_back() {
    run(&[
        Op::External(0, vec![(4, true)]),
        Op::Move(0, 1, false),
        Op::Write(0, 4, false),
        Op::Move(0, 4, false),
    ]);
}

/// Resolving one conflict for the file's side leaves the other key's
/// pending edit alone.
#[test]
fn file_side_resolution_leaves_the_other_edit() {
    run(&[
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::Write(0, 0, false),
        Op::DiskEdit(0, 0, false, FileWins::Never),
        Op::Write(0, 1, false),
        Op::Resolve(0, false, 0, false),
    ]);
}

/// An outside commit deleting a record already deleted changes nothing,
/// with an edit pending over a hand edit.
#[test]
fn outside_delete_of_a_deleted_record_changes_nothing() {
    run(&[
        Op::Write(0, 3, true),
        Op::Commit(0),
        Op::DiskEdit(0, 4, false, FileWins::Never),
        Op::Write(0, 4, false),
        Op::External(0, vec![(3, true)]),
    ]);
}

/// A pending delete at a key another record has since moved to deletes
/// that record, and the rollup names it.
#[test]
fn delete_at_a_key_a_record_moved_to_names_that_record() {
    run(&[
        Op::Write(0, 2, true),
        Op::External(0, vec![(2, true)]),
        Op::Move(0, 5, false),
        Op::DiskEdit(0, 2, true, FileWins::Never),
        Op::Commit(0),
    ]);
}

/// A fork at main's head shares main's rows: each side's edits and
/// commits stay its own, and the rows below stay shared.
#[test]
fn fork_at_the_head_shares_the_rows_below() {
    run(&[
        Op::Write(0, 1, false),
        Op::Commit(0),
        Op::Fork(0, 0),
        Op::Write(1, 1, false),
        Op::Write(1, 4, true),
        Op::Commit(1),
        Op::Write(0, 1, false),
        Op::Commit(0),
        Op::Fork(1, 0),
        Op::Write(2, 2, false),
        Op::Commit(2),
    ]);
}

/// A fork before a commit that deleted a record re-creates it as a new
/// one: nothing names its old id, since the fold dropped its rows and the
/// first commit has no rollup.
#[test]
fn fork_before_a_delete_recreates_the_record() {
    run(&[
        Op::DiskEdit(0, 5, true, FileWins::Never),
        Op::Commit(0),
        Op::Fork(0, 3),
    ]);
}

/// A branch reset onto main's later tip: it rebuilds on main's head,
/// which closes, and takes main's records from main's chain.
#[test]
fn rebase_onto_mains_later_tip() {
    for back in 0..4 {
        run(&[
            Op::Write(0, 1, false),
            Op::Commit(0),
            Op::Fork(0, 0),
            Op::Write(1, 2, false),
            Op::Commit(1),
            Op::Write(0, 3, false),
            Op::Commit(0),
            Op::Rebase(1, 0, back, 0, false),
            Op::Write(0, 4, false),
            Op::Commit(0),
            Op::Write(1, 5, false),
            Op::Commit(1),
        ]);
    }
}

/// A "rebase" onto a line that already holds the worktree's HEAD is a
/// fast-forward, which a scan takes in as one: the op mustn't model it
/// as a rewrite.
#[test]
fn rebases_onto_each_others_lines() {
    run(&[
        Op::Fork(0, 0),
        Op::External(5, vec![(1, true)]),
        Op::Rebase(188, 0, 7, 1, false),
        Op::Rebase(35, 0, 53, 2, false),
    ]);
}

#[test]
fn rebase_model_seed() {
    run(&[
        Op::Write(0, 4, true),
        Op::Commit(0),
        Op::Fork(0, 0),
        Op::Write(125, 0, false),
        Op::Commit(35),
        Op::Rebase(0, 0, 0, 0, false),
        Op::Fork(19, 0),
        Op::DiskEdit(29, 1, false, FileWins::Never),
        Op::Move(129, 1, false),
        Op::Rebase(47, 44, 73, 0, false),
    ]);
}

#[test]
fn rebase_sql_seed() {
    run(&[
        Op::Write(0, 3, false),
        Op::Fork(0, 0),
        Op::Commit(2),
        Op::Write(151, 0, false),
        Op::Commit(109),
        Op::Rebase(19, 0, 89, 0, false),
        Op::Commit(49),
    ]);
}

/// A rebuild over a working-tree edit and a client edit in the same file,
/// then the edit deleted and committed.
#[test]
fn rebuild_under_a_disk_and_a_client_edit() {
    run(&[
        Op::DiskEdit(0, 1, false, FileWins::Never),
        Op::Write(0, 0, false),
        Op::Rebuild(0, 0, 3, false),
        Op::Write(0, 1, true),
        Op::Commit(0),
    ]);
}

#[test]
fn fork_after_a_rebuild_and_a_deletion() {
    run(&[
        Op::Fork(0, 0),
        Op::Rebuild(11, 103, 0, false),
        Op::External(30, vec![(0, true)]),
        Op::Fork(8, 1),
    ]);
}

/// A worktree rebased onto main's line with a pending deletion that main
/// already committed: the commit after it has nothing to carry.
#[test]
fn rebase_under_a_deletion_main_committed() {
    run(&[
        Op::Write(0, 2, true),
        Op::Fork(0, 0),
        Op::Commit(104),
        Op::Rebase(20, 0, 0, 2, true),
        Op::Commit(30),
    ]);
}

/// A "rebuild" onto the segment ending at HEAD, below the empty head a
/// fork left, is a fast-forward and a commit: the op mustn't model it as
/// a rewrite, which would compact where a scan doesn't.
#[test]
fn rebuild_onto_head_below_an_empty_head_is_a_fast_forward() {
    run(&[
        Op::Write(0, 0, false),
        Op::Fork(0, 0),
        Op::Write(0, 0, false),
        Op::Write(0, 0, false),
        Op::Write(0, 0, false),
        Op::Delete(0),
        Op::Write(0, 0, false),
        Op::Commit(0),
        Op::Fork(0, 0),
        Op::Rebuild(0, 235, 0, false),
    ]);
}

/// An export onto a base older than HEAD: the edit was made before an
/// outside commit moved HEAD on.
#[test]
fn an_export_forks_where_its_edit_was_made() {
    run(&[
        Op::Write(0, 0, false),
        Op::External(0, vec![(1, false)]),
        Op::DiskEdit(0, 0, false, FileWins::Never),
        Op::Export(0),
    ]);
}

/// An export whose edit followed its record to another file since its
/// base: on the branch it's a new record, and the record stays where the
/// base has it.
#[test]
fn an_export_renews_an_edit_whose_record_moved() {
    run(&[
        Op::Write(0, 2, false),
        Op::External(0, vec![(2, false)]),
        Op::Write(0, 0, true),
        Op::Commit(0),
        Op::Write(0, 3, false),
        Op::Export(0),
        Op::External(0, vec![(3, true), (0, false)]),
        Op::Export(0),
    ]);
}

/// The file's value an export leaves continues the record the chain has
/// at its key, not the withdrawn edit's re-create.
#[test]
fn an_export_takes_in_the_value_a_scan_would() {
    run(&[
        Op::External(0, vec![(2, true)]),
        Op::Write(0, 2, false),
        Op::Move(0, 5, false),
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::Export(0),
    ]);
}

/// A user edited over a value an export brings back: the re-created row
/// stays hidden from them.
#[test]
fn an_export_hides_what_a_user_edited_over() {
    run(&[
        Op::NewUser,
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::UserWrite(0, vec![], 2, false),
        Op::Write(0, 2, false),
        Op::DiskEdit(0, 0, false, FileWins::Never),
        Op::Export(0),
    ]);
}

/// An exported deletion: the branch's draft tombstone has to hide the row
/// the base has, which only the re-link of its draft makes it do.
#[test]
fn an_export_of_a_deletion_hides_the_base_row() {
    run(&[
        Op::Write(0, 0, false),
        Op::Write(0, 2, true),
        Op::DiskEdit(0, 2, false, FileWins::Never),
        Op::Export(0),
    ]);
}
