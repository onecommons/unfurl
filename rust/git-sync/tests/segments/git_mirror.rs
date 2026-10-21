// The model's git as a real repository, for an implementation that reads
// real git. Every model commit becomes a commit, and every worktree with
// a working tree a checkout of it: main is the repository, a fork is a
// `git worktree add`. A user branch has none.
//
// Key `k` is record `k{k % KEYS_PER_FILE}` in section `r` of file
// `f{file_of(k)}.json`, whose content is `{"v": <version>}`. So the
// model's key order is `(file_path, path, key)` order.
//
// It writes and reads objects through gix, in process, and moves
// checkouts with git itself; never through git-sync's own git code, as it
// checks what git-sync does.

/// The format the mirrored files are in.
struct SegTest;

impl DataFormat for SegTest {
    fn name(&self) -> &str {
        "segtest"
    }

    fn is_format(&self, json: &serde_json::Value) -> bool {
        json.get("kind").and_then(|k| k.as_str()) == Some("segtest")
    }

    fn new_document(&self) -> serde_json::Value {
        serde_json::json!({ "kind": "segtest" })
    }

    fn path_prefixes(&self) -> &[&str] {
        &["r"]
    }

    fn find_alias(&self, _record: &Record) -> Vec<(String, String)> {
        Vec::new()
    }

    fn follow(&self, _record: &Record) -> Vec<String> {
        Vec::new()
    }
}

fn file_name(file: Key) -> String {
    format!("f{file}.json")
}

fn record_key(key: Key) -> String {
    format!("k{}", key % KEYS_PER_FILE)
}

/// File `file`'s content for a tree.
fn file_json(tree: &BTreeMap<Key, Ver>, file: Key) -> String {
    let records: serde_json::Map<String, serde_json::Value> = keys_of(file)
        .filter_map(|k| tree.get(&k).map(|&v| (record_key(k), serde_json::json!({ "v": v }))))
        .collect();
    let doc = serde_json::json!({ "kind": "segtest", "r": records });
    serde_json::to_string_pretty(&doc).unwrap() + "\n"
}

/// The records of file `file`'s content, as model keys and versions.
fn parse_file(file: Key, text: &str) -> BTreeMap<Key, Ver> {
    let doc: serde_json::Value = serde_json::from_str(text).unwrap();
    let Some(records) = doc.get("r").and_then(|r| r.as_object()) else {
        return BTreeMap::new();
    };
    keys_of(file)
        .filter_map(|k| {
            let v = records.get(&record_key(k))?.get("v")?.as_u64()?;
            Some((k, v))
        })
        .collect()
}

fn git(dir: &Path, args: &[&str], stdin: Option<&str>) -> String {
    let mut cmd = std::process::Command::new("git");
    cmd.current_dir(dir)
        .args(["-c", "commit.gpgsign=false"])
        .args(args)
        .env("GIT_AUTHOR_NAME", "harness")
        .env("GIT_AUTHOR_EMAIL", "harness@example.com")
        .env("GIT_COMMITTER_NAME", "harness")
        .env("GIT_COMMITTER_EMAIL", "harness@example.com")
        .stdin(std::process::Stdio::piped())
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::piped());
    let mut child = cmd.spawn().expect("git runs");
    if let Some(input) = stdin {
        child
            .stdin
            .take()
            .unwrap()
            .write_all(input.as_bytes())
            .unwrap();
    }
    let out = child.wait_with_output().unwrap();
    assert!(
        out.status.success(),
        "git {args:?} failed: {}",
        String::from_utf8_lossy(&out.stderr)
    );
    String::from_utf8(out.stdout).unwrap().trim_end().to_string()
}

struct Checkout {
    dir: PathBuf,
    head: CommitId,
    repo: gix::Repository,
}

struct GitMirror {
    tmp: tempfile::TempDir,
    /// Main's checkout, which holds the repository.
    root: PathBuf,
    repo: gix::Repository,
    /// Each model commit's object id.
    oids: Vec<String>,
    checkouts: BTreeMap<Wt, Checkout>,
}

fn object_id(hex: &str) -> gix::ObjectId {
    gix::ObjectId::from_hex(hex.as_bytes()).expect("an object id")
}

impl GitMirror {
    fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("main");
        std::fs::create_dir(&root).unwrap();
        git(&root, &["init", "-q", "-b", "main"], None);
        // one origin for every checkout: they're branches of one repository
        git(&root, &["remote", "add", "origin", "https://example.com/segtest.git"], None);
        let repo = gix::open(&root).expect("open the mirror");
        GitMirror {
            tmp,
            root,
            repo,
            oids: Vec::new(),
            checkouts: BTreeMap::new(),
        }
    }

    /// File `file` in commit `oid`: its blob's id and its text.
    fn file_at(&self, oid: &str, file: Key) -> (gix::ObjectId, String) {
        let spec = format!("{oid}:{}", file_name(file));
        let id = self.repo.rev_parse_single(spec.as_str()).expect("a file in a commit").detach();
        let blob = self.repo.find_blob(id).expect("a blob");
        (id, String::from_utf8(blob.data.clone()).unwrap())
    }

    fn message(&self, oid: &str) -> gix::bstr::BString {
        let commit = self.repo.find_commit(object_id(oid)).expect("a commit");
        commit.message_raw().expect("a message").to_owned()
    }

    /// Make the commits `world` has that the repository doesn't.
    fn make_commits(&mut self, world: &World) {
        self.make_commits_upto(world, world.git.commits.len());
    }

    /// Make those before model commit `end`.
    fn make_commits_upto(&mut self, world: &World, end: usize) {
        for c in self.oids.len()..end {
            let oid = self.make_commit(&world.git, c);
            self.oids.push(oid);
        }
    }

    /// Bring the repository and the checkouts up to `world`.
    fn sync(&mut self, world: &World) {
        self.make_commits(world);
        for w in 0..world.model.wts.len() {
            let m = &world.model.wts[w];
            if m.user || m.exported {
                continue;
            }
            if !m.alive {
                if let Some(gone) = self.checkouts.remove(&w) {
                    let dir = gone.dir.to_str().unwrap();
                    git(&self.root, &["worktree", "remove", "--force", dir], None);
                }
                continue;
            }
            let head = *world.imp.wts[w].history.last().unwrap();
            let oid = self.oids[head].clone();
            let mut moved = false;
            match self.checkouts.get_mut(&w) {
                Some(c) if c.head == head => {}
                Some(c) => {
                    git(&c.dir, &["reset", "-q", "--mixed", &oid], None);
                    c.head = head;
                    moved = true;
                }
                None if w == MAIN => {
                    git(&self.root, &["reset", "-q", "--mixed", &oid], None);
                    let dir = self.root.clone();
                    let repo = gix::open(&dir).expect("open main's checkout");
                    self.checkouts.insert(w, Checkout { dir, head, repo });
                }
                None => {
                    let dir = self.tmp.path().join(format!("w{w}"));
                    let path = dir.to_str().unwrap();
                    let branch = format!("w{w}");
                    git(&self.root, &["worktree", "add", "-q", "-b", &branch, path, &oid], None);
                    let repo = gix::open(&dir).expect("open a checkout");
                    self.checkouts.insert(w, Checkout { dir, head, repo });
                }
            }
            // only where the content differs, or HEAD moved: the
            // implementation writes these files too, in its own layout
            let dir = &self.checkouts[&w].dir;
            for f in 0..KEYS / KEYS_PER_FILE {
                let path = dir.join(file_name(f));
                let want: BTreeMap<Key, Ver> =
                    keys_of(f).filter_map(|k| m.disk.get(&k).map(|&v| (k, v))).collect();
                let now = std::fs::read_to_string(&path).ok().map(|t| parse_file(f, &t));
                let differs = now.as_ref() != Some(&want);
                if !differs && !moved {
                    continue;
                }
                // HEAD's own bytes where it's HEAD's content, as a checkout
                // writes them
                let (_, text) = self.file_at(&self.oids[self.checkouts[&w].head], f);
                if parse_file(f, &text) == want {
                    git(dir, &["checkout", "-q", "HEAD", "--", &file_name(f)], None);
                } else if differs {
                    std::fs::write(path, file_json(&m.disk, f)).unwrap();
                }
            }
        }
    }

    /// Take `oid`, a commit the implementation made on `w`'s checkout, as
    /// model commit `c`, the next one; its tree must be the model's.
    fn adopt(&mut self, g: &Git, w: Wt, c: CommitId, oid: &str) {
        assert_eq!(c, self.oids.len(), "commits adopted in order");
        for f in 0..KEYS / KEYS_PER_FILE {
            let back = parse_file(f, &self.file_at(oid, f).1);
            let want: BTreeMap<Key, Ver> =
                keys_of(f).filter_map(|k| g.commits[c].get(&k).map(|&v| (k, v))).collect();
            assert_eq!(back, want, "the implementation's commit {c}'s {}", file_name(f));
        }
        self.oids.push(oid.to_string());
        if let Some(checkout) = self.checkouts.get_mut(&w) {
            checkout.head = c;
        }
    }

    /// A real commit for model commit `c`, read back to check it.
    fn make_commit(&self, g: &Git, c: CommitId) -> String {
        let mut entries = Vec::new();
        for f in 0..KEYS / KEYS_PER_FILE {
            let want: BTreeMap<Key, Ver> =
                keys_of(f).filter_map(|k| g.commits[c].get(&k).map(|&v| (k, v))).collect();
            // A parent's blob where the content is unchanged: the
            // implementation writes files in its own layout, and a
            // checkout's bytes have to stay those of its HEAD.
            let kept = g.parents[c].iter().find_map(|&p| {
                let (id, text) = self.file_at(&self.oids[p], f);
                (parse_file(f, &text) == want).then_some(id)
            });
            let oid = kept.unwrap_or_else(|| {
                let json = file_json(&g.commits[c], f);
                self.repo.write_blob(json).expect("write a blob").detach()
            });
            entries.push(gix::objs::tree::Entry {
                mode: gix::objs::tree::EntryKind::Blob.into(),
                filename: file_name(f).into(),
                oid,
            });
        }
        let tree = gix::objs::Tree { entries };
        let tree = self.repo.write_object(&tree).expect("write a tree").detach();
        let message: gix::bstr::BString = match (g.replayed_from.get(&c), &g.rollups[c]) {
            (Some(&from), _) => self.message(&self.oids[from]),
            (None, Some(ids)) => format!("git-sync commit {c}\n\nrollup: {ids:?}").into(),
            (None, None) => format!("commit {c}").into(),
        };
        // a date of its own: a replay copies its source's message, and two
        // replays of one commit onto one parent would be one git object
        let signature = gix::actor::Signature {
            name: "harness".into(),
            email: "harness@example.com".into(),
            time: gix::date::Time {
                seconds: 1_700_000_000 + c as i64,
                offset: 0,
            },
        };
        let parents: Vec<gix::ObjectId> = g.parents[c].iter().map(|&p| object_id(&self.oids[p])).collect();
        let commit = gix::objs::Commit {
            tree,
            parents: parents.iter().copied().collect(),
            author: signature.clone(),
            committer: signature,
            encoding: None,
            message,
            extra_headers: Vec::new(),
        };
        let oid = self.repo.write_object(&commit).expect("write a commit").detach();
        let back: Vec<gix::ObjectId> = self
            .repo
            .find_commit(oid)
            .expect("the commit")
            .parent_ids()
            .map(|p| p.detach())
            .collect();
        assert_eq!(back, parents, "commit {c}'s parents");
        let oid = oid.to_string();
        if let Some(&from) = g.replayed_from.get(&c) {
            let (copied, source) = (self.message(&oid), self.message(&self.oids[from]));
            assert_eq!(copied, source, "commit {c} replays {from}'s message");
        }
        for f in 0..KEYS / KEYS_PER_FILE {
            let back = parse_file(f, &self.file_at(&oid, f).1);
            let want: BTreeMap<Key, Ver> =
                keys_of(f).filter_map(|k| g.commits[c].get(&k).map(|&v| (k, v))).collect();
            assert_eq!(back, want, "commit {c}'s {}", file_name(f));
        }
        oid
    }

    /// Each checkout is at its worktree's head, its index is that commit's
    /// tree, and its files are the model's working tree.
    fn check(&self, world: &World, step: usize) {
        let entries = |index: &gix::index::File| -> Vec<(gix::bstr::BString, gix::ObjectId)> {
            index.entries().iter().map(|e| (e.path(index).to_owned(), e.id)).collect()
        };
        for (&w, c) in &self.checkouts {
            let head = c.repo.head_id().expect("HEAD").detach();
            assert_eq!(head.to_string(), self.oids[c.head], "step {step}: worktree {w}'s HEAD");
            let tree = c.repo.find_commit(head).expect("HEAD's commit").tree_id().expect("a tree");
            let want = c.repo.index_from_tree(&tree).expect("HEAD's tree as an index");
            let index = c.repo.open_index().expect("the index");
            assert_eq!(entries(&index), entries(&want), "step {step}: worktree {w}'s index");
            for f in 0..KEYS / KEYS_PER_FILE {
                let text = std::fs::read_to_string(c.dir.join(file_name(f))).unwrap();
                let want: BTreeMap<Key, Ver> = keys_of(f)
                    .filter_map(|k| world.model.wts[w].disk.get(&k).map(|&v| (k, v)))
                    .collect();
                assert_eq!(parse_file(f, &text), want, "step {step}: worktree {w}'s {}", file_name(f));
            }
        }
    }
}
