// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! `unfurl-yaml` — covert YAML/JSON in markdown and YAML with merge directives into plain YAML/JSON.
use std::path::{Path, PathBuf};
use std::process::ExitCode;

const USAGE: &str = "\
usage: unfurl-yaml [--extract | --expand] <path>

Extract or expands the given file to stdout.

  --extract   take the yaml out of a literate markdown document's
              fenced code blocks
  --expand    resolve merge directives in a yaml document

Both `--extract --expand` reads a literate
markdown document and resolves the includes in the yaml it holds.

Neither: files .md and .markdown extensions are extracted, anything else is expanded.
";

fn main() -> ExitCode {
    match parse_args(std::env::args().skip(1)).and_then(run) {
        Ok(out) => {
            print!("{out}");
            ExitCode::SUCCESS
        }
        Err(message) => {
            eprintln!("unfurl-yaml: {message}");
            ExitCode::FAILURE
        }
    }
}

/// What the command line asked for.
#[derive(Debug, PartialEq, Eq)]
enum Command {
    /// Print usage and stop, successfully — `--help` is not a failure.
    Help,
    Run {
        stages: Stages,
        path: PathBuf,
    },
}

/// Which stages to run, in the only order they compose in: taking the
/// yaml out of prose comes before resolving what it includes.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct Stages {
    extract: bool,
    expand: bool,
}

impl Stages {
    /// The single stage a path implies when no flag says otherwise.
    ///
    /// Markdown is the only shape that has to be recognised: everything
    /// else is YAML as far as `expand` cares, and guessing wrong there
    /// costs an error rather than a wrong answer.
    fn of(path: &Path) -> Self {
        let ext = path
            .extension()
            .map(|e| e.to_string_lossy().to_ascii_lowercase());
        match ext.as_deref() {
            Some("md" | "markdown") => Stages {
                extract: true,
                expand: false,
            },
            _ => Stages {
                extract: false,
                expand: true,
            },
        }
    }
}

fn parse_args(args: impl Iterator<Item = String>) -> Result<Command, String> {
    let mut asked = Stages::default();
    let mut path: Option<PathBuf> = None;
    for arg in args {
        match arg.as_str() {
            "-h" | "--help" => return Ok(Command::Help),
            "--extract" => asked.extract = true,
            "--expand" => asked.expand = true,
            other if other.starts_with('-') => {
                return Err(format!("unknown option `{other}`\n\n{USAGE}"))
            }
            other => {
                if path.replace(PathBuf::from(other)).is_some() {
                    return Err(format!("expected one path\n\n{USAGE}"));
                }
            }
        }
    }
    let path = path.ok_or_else(|| format!("no path given\n\n{USAGE}"))?;
    let stages = if asked == Stages::default() {
        Stages::of(&path)
    } else {
        asked
    };
    Ok(Command::Run { stages, path })
}

fn run(command: Command) -> Result<String, String> {
    let (stages, path) = match command {
        Command::Help => return Ok(USAGE.to_string()),
        Command::Run { stages, path } => (stages, path),
    };
    let named = |e: unfurl_merge::MergeError| match e {
        // Every other variant names the file it is about; the io one
        // cannot, and "which file?" is the first thing you want.
        unfurl_merge::MergeError::Io(io) => format!("{}: {io}", path.display()),
        other => other.to_string(),
    };
    let text = if stages.extract {
        unfurl_merge::extract_file(&path).map_err(named)?
    } else {
        std::fs::read_to_string(&path).map_err(|e| format!("{}: {e}", path.display()))?
    };
    if !stages.expand {
        return Ok(text);
    }
    // Expanded as though it were `path`, so includes resolve relative
    // to the document they came out of rather than the process's cwd.
    unfurl_merge::expand_text(&text, &path).map_err(named)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Command, String> {
        parse_args(args.iter().map(|s| (*s).to_string()))
    }

    fn stages(args: &[&str]) -> Stages {
        match parse(args).expect("parses") {
            Command::Run { stages, .. } => stages,
            other => panic!("{other:?}"),
        }
    }

    const EXTRACT: Stages = Stages {
        extract: true,
        expand: false,
    };
    const EXPAND: Stages = Stages {
        extract: false,
        expand: true,
    };
    const BOTH: Stages = Stages {
        extract: true,
        expand: true,
    };

    #[test]
    fn the_extension_chooses_one_stage_when_no_flag_does() {
        for (path, want) in [
            ("doc.md", EXTRACT),
            ("doc.MARKDOWN", EXTRACT),
            ("doc.yaml", EXPAND),
            ("doc.json", EXPAND),
            // No extension at all is still a document; only markdown
            // needs recognising.
            ("doc", EXPAND),
        ] {
            assert_eq!(stages(&[path]), want, "{path}");
        }
    }

    #[test]
    fn a_flag_overrides_the_extension() {
        assert_eq!(stages(&["--expand", "doc.md"]), EXPAND);
        assert_eq!(stages(&["--extract", "doc.yaml"]), EXTRACT);
    }

    /// The two compose rather than contradict: take the yaml out of the
    /// prose, then resolve what it includes. Order of the flags does not
    /// matter, because there is only one order the stages run in.
    #[test]
    fn both_flags_ask_for_both_stages() {
        assert_eq!(stages(&["--extract", "--expand", "doc.md"]), BOTH);
        assert_eq!(stages(&["--expand", "--extract", "doc.md"]), BOTH);
        // ...and on a file the extension would have expanded.
        assert_eq!(stages(&["--extract", "--expand", "doc.yaml"]), BOTH);
    }

    #[test]
    fn repeating_a_flag_is_the_same_as_giving_it_once() {
        assert_eq!(stages(&["--extract", "--extract", "doc.yaml"]), EXTRACT);
    }

    fn fixture(rel: &str) -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures")
            .join(rel)
    }

    fn run_on(args: &[&str], rel: &str) -> Result<String, String> {
        let mut argv: Vec<String> = args.iter().map(|s| (*s).to_string()).collect();
        argv.push(fixture(rel).display().to_string());
        run(parse_args(argv.into_iter()).expect("parses"))
    }

    #[test]
    fn extract_prints_the_embedded_yaml() {
        let out = run_on(&[], "literate/document.md").expect("extract");
        assert!(out.contains("org:"), "{out}");
        assert!(!out.contains("An organization"), "prose is not data: {out}");
    }

    #[test]
    fn expand_resolves_includes() {
        let out = run_on(&[], "expand_file_include/parent.yaml").expect("expand");
        assert!(out.contains("DATABASE_URL"), "{out}");
        assert!(!out.contains("+include"), "{out}");
    }

    /// The stages compose in order, so the include inside the markdown
    /// document's yaml is resolved too.
    #[test]
    fn extract_then_expand_runs_both() {
        let out = run_on(&["--extract", "--expand"], "literate/with_include.md").expect("both");
        assert!(out.contains("DATABASE_URL"), "{out}");
        assert!(!out.contains("+include"), "{out}");
        // ...and extract alone leaves the directive standing.
        let out = run_on(&["--extract"], "literate/with_include.md").expect("extract");
        assert!(out.contains("+include"), "{out}");
    }

    /// Settled from the front matter, without reading the file.
    #[test]
    fn extracting_a_file_that_is_not_literate_says_so() {
        let err = run_on(&["--extract"], "expand_file_include/parent.yaml").expect_err("not ours");
        assert!(err.contains("literate-yaml"), "{err}");
        assert!(err.contains("parent.yaml"), "the path is named: {err}");
    }

    /// The io variant carries no path of its own, so the command has to
    /// add it.
    #[test]
    fn a_missing_file_is_named_in_the_error() {
        for args in [vec!["--extract"], vec!["--expand"]] {
            let err = run_on(&args, "literate/nope.md").expect_err("missing");
            assert!(err.contains("nope.md"), "{err}");
        }
    }

    #[test]
    fn help_is_not_a_failure_and_needs_no_path() {
        assert_eq!(parse(&["--help"]), Ok(Command::Help));
        assert_eq!(parse(&["-h"]), Ok(Command::Help));
        assert!(run(Command::Help).expect("help").contains("usage:"));
    }

    #[test]
    fn a_missing_or_repeated_path_says_so() {
        assert!(parse(&[]).unwrap_err().contains("no path"));
        assert!(parse(&["--expand"]).unwrap_err().contains("no path"));
        assert!(parse(&["a.yaml", "b.yaml"])
            .unwrap_err()
            .contains("one path"));
        assert!(parse(&["--nope", "a.yaml"])
            .unwrap_err()
            .contains("unknown option"));
    }
}
