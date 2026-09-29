// The model's git as a real repository, for an implementation that reads
// real git. Every model commit becomes a commit, and every worktree with
// a working tree a checkout of it: main is the repository, a fork is a
// `git worktree add`. A user branch has none.
//
// Key `k` is record `k{k % KEYS_PER_FILE}` in section `r` of file
// `f{file_of(k)}.json`, whose content is `{"v": <version>}`. So the
// model's key order is `(file_path, path, key)` order.

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
}

struct GitMirror {
    tmp: tempfile::TempDir,
    /// Main's checkout, which holds the repository.
    root: PathBuf,
    /// Each model commit's object id.
    oids: Vec<String>,
    checkouts: BTreeMap<Wt, Checkout>,
}

impl GitMirror {
    fn new() -> Self {
        let tmp = tempfile::tempdir().unwrap();
        let root = tmp.path().join("main");
        std::fs::create_dir(&root).unwrap();
        git(&root, &["init", "-q", "-b", "main"], None);
        // one origin for every checkout: they're branches of one repository
        git(&root, &["remote", "add", "origin", "https://example.com/segtest.git"], None);
        GitMirror {
            tmp,
            root,
            oids: Vec::new(),
            checkouts: BTreeMap::new(),
        }
    }

    /// Bring the repository and the checkouts up to `world`.
    fn sync(&mut self, world: &World) {
        for c in self.oids.len()..world.git.commits.len() {
            let oid = self.make_commit(&world.git, c);
            self.oids.push(oid);
        }
        for w in 0..world.model.wts.len() {
            let m = &world.model.wts[w];
            if m.user {
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
            match self.checkouts.get_mut(&w) {
                Some(c) if c.head == head => {}
                Some(c) => {
                    git(&c.dir, &["reset", "-q", "--mixed", &oid], None);
                    c.head = head;
                }
                None if w == MAIN => {
                    git(&self.root, &["reset", "-q", "--mixed", &oid], None);
                    let dir = self.root.clone();
                    self.checkouts.insert(w, Checkout { dir, head });
                }
                None => {
                    let dir = self.tmp.path().join(format!("w{w}"));
                    let path = dir.to_str().unwrap();
                    let branch = format!("w{w}");
                    git(&self.root, &["worktree", "add", "-q", "-b", &branch, path, &oid], None);
                    self.checkouts.insert(w, Checkout { dir, head });
                }
            }
            // only where the content differs: the implementation writes
            // these files too, in its own layout
            let dir = &self.checkouts[&w].dir;
            for f in 0..KEYS / KEYS_PER_FILE {
                let path = dir.join(file_name(f));
                let want: BTreeMap<Key, Ver> =
                    keys_of(f).filter_map(|k| m.disk.get(&k).map(|&v| (k, v))).collect();
                let now = std::fs::read_to_string(&path).ok().map(|t| parse_file(f, &t));
                if now.as_ref() != Some(&want) {
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
            let spec = format!("{oid}:{}", file_name(f));
            let back = parse_file(f, &git(&self.root, &["show", &spec], None));
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
        let mut entries = String::new();
        for f in 0..KEYS / KEYS_PER_FILE {
            let want: BTreeMap<Key, Ver> =
                keys_of(f).filter_map(|k| g.commits[c].get(&k).map(|&v| (k, v))).collect();
            // A parent's blob where the content is unchanged: the
            // implementation writes files in its own layout, and a
            // checkout's bytes have to stay those of its HEAD.
            let kept = g.parents[c].iter().find_map(|&p| {
                let spec = format!("{}:{}", self.oids[p], file_name(f));
                let text = git(&self.root, &["show", &spec], None);
                (parse_file(f, &text) == want)
                    .then(|| git(&self.root, &["rev-parse", &spec], None))
            });
            let blob = kept.unwrap_or_else(|| {
                let json = file_json(&g.commits[c], f);
                git(&self.root, &["hash-object", "-w", "--stdin"], Some(&json))
            });
            entries += &format!("100644 blob {blob}\t{}\n", file_name(f));
        }
        let tree = git(&self.root, &["mktree"], Some(&entries));
        let message = match (g.replayed_from.get(&c), &g.rollups[c]) {
            (Some(&from), _) => git(&self.root, &["log", "-1", "--format=%B", &self.oids[from]], None),
            (None, Some(ids)) => format!("git-sync commit {c}\n\nrollup: {ids:?}"),
            (None, None) => format!("commit {c}"),
        };
        let mut args = vec!["commit-tree".to_string(), tree, "-F".into(), "-".into()];
        for &p in &g.parents[c] {
            args.extend(["-p".to_string(), self.oids[p].clone()]);
        }
        let args: Vec<&str> = args.iter().map(String::as_str).collect();
        let oid = git(&self.root, &args, Some(&message));
        let parents = git(&self.root, &["rev-list", "--parents", "-n1", &oid], None);
        let want: Vec<&str> = g.parents[c].iter().map(|&p| self.oids[p].as_str()).collect();
        assert_eq!(parents.split(' ').skip(1).collect::<Vec<_>>(), want, "commit {c}'s parents");
        if let Some(&from) = g.replayed_from.get(&c) {
            let copied = git(&self.root, &["log", "-1", "--format=%B", &oid], None);
            let source = git(&self.root, &["log", "-1", "--format=%B", &self.oids[from]], None);
            assert_eq!(copied, source, "commit {c} replays {from}'s message");
        }
        for f in 0..KEYS / KEYS_PER_FILE {
            let spec = format!("{oid}:{}", file_name(f));
            let back = parse_file(f, &git(&self.root, &["show", &spec], None));
            let want: BTreeMap<Key, Ver> =
                keys_of(f).filter_map(|k| g.commits[c].get(&k).map(|&v| (k, v))).collect();
            assert_eq!(back, want, "commit {c}'s {}", file_name(f));
        }
        oid
    }

    /// Each checkout is at its worktree's head, its index is that commit's
    /// tree, and its files are the model's working tree.
    fn check(&self, world: &World, step: usize) {
        for (&w, c) in &self.checkouts {
            let head = git(&c.dir, &["rev-parse", "HEAD"], None);
            assert_eq!(head, self.oids[c.head], "step {step}: worktree {w}'s HEAD");
            git(&c.dir, &["diff", "--cached", "--quiet"], None);
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
