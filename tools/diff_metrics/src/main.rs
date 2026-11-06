//! diff_metrics: stage-neutral before/after metrics for a Rust crate, so every
//! PROCTOR stage can be measured the same way (stage N-1 output vs stage N).
//!
//! Usage: diff_metrics --before <crate_dir> --after <crate_dir> [--api <file>] [--no-rustfmt]
//!
//! Emits JSON:
//!   - totals: `files_changed`, `lines_added`, `lines_removed`, `touched_files`;
//!   - per changed file: `status`, `lines_added`, `lines_removed`, `rustfmt_ok`
//!     (see `churn`);
//!   - `targets`: which files each Cargo target compiles (see `layout`);
//!   - `abi`: exported symbols, signatures and reachable type layouts, before
//!     vs after, with an overall `ok` (see `abi`).
//!
//! Purely syntactic (syn v2): no name resolution, type inference, or build.
//! Unreadable inputs are errors (exit 2), never an empty or "clean" report.

mod abi;
mod churn;
mod layout;

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};

use clap::Parser;
use serde::Serialize;

#[derive(Parser)]
#[command(
    about = "Before/after metrics for a Rust crate: files and lines changed, exported ABI diff."
)]
struct Cli {
    /// Crate directory BEFORE the stage (the previous stage's output).
    #[arg(long)]
    before: PathBuf,
    /// Crate directory AFTER the stage.
    #[arg(long)]
    after: PathBuf,
    /// Restrict the ABI diff to the public API, e.g. from the case's C headers:
    /// one entry per line, `symbol NAME` (or a bare `NAME`) for an exported
    /// function or global, `type NAME` for a type whose layout the headers
    /// define. Without it every exported symbol counts — including helpers
    /// c2rust exported that a later stage legitimately internalizes — and every
    /// reachable layout change is treated as breaking.
    #[arg(long)]
    api: Option<PathBuf>,
    /// Compare raw text instead of rustfmt-normalized text.
    #[arg(long)]
    no_rustfmt: bool,
}

#[derive(Serialize)]
struct Report {
    schema_version: u32,
    files_changed: u64,
    lines_added: u64,
    lines_removed: u64,
    touched_files: Vec<String>,
    files: BTreeMap<String, FileReport>,
    targets: Sides<layout::Layout>,
    abi: abi::AbiDiff,
}

#[derive(Serialize)]
struct Sides<T> {
    before: T,
    after: T,
}

#[derive(Serialize)]
struct FileReport {
    status: &'static str,
    lines_added: u64,
    lines_removed: u64,
    rustfmt_ok: bool,
}

fn main() {
    let cli = Cli::parse();
    match run(&cli) {
        Ok(report) => println!(
            "{}",
            serde_json::to_string_pretty(&report).expect("serializable")
        ),
        Err(e) => {
            eprintln!("diff_metrics: error: {e}");
            std::process::exit(2);
        }
    }
}

fn run(cli: &Cli) -> Result<Report, String> {
    let before = collect_rs(&cli.before)?;
    let after = collect_rs(&cli.after)?;
    let api = match &cli.api {
        Some(p) => Some(abi::Api::parse(&read(p)?)?),
        None => None,
    };

    let all: BTreeSet<&String> = before.keys().chain(after.keys()).collect();
    let mut files = BTreeMap::new();
    let mut touched = Vec::new();
    let (mut added, mut removed) = (0, 0);
    for f in all {
        let d = churn::diff_file(before.get(f), after.get(f), !cli.no_rustfmt);
        if !d.changed() {
            continue;
        }
        touched.push(f.clone());
        added += d.lines_added;
        removed += d.lines_removed;
        files.insert(
            f.clone(),
            FileReport {
                status: d.status,
                lines_added: d.lines_added,
                lines_removed: d.lines_removed,
                rustfmt_ok: d.rustfmt_ok,
            },
        );
    }

    let model = abi::CTypeModel::host();
    let lb = layout::layout(&read(&cli.before.join("Cargo.toml"))?, &before)
        .map_err(|e| format!("{}: {e}", cli.before.display()))?;
    let la = layout::layout(&read(&cli.after.join("Cargo.toml"))?, &after)
        .map_err(|e| format!("{}: {e}", cli.after.display()))?;
    let abi = abi::diff(
        &abi::side(&before, &lb, &model),
        &abi::side(&after, &la, &model),
        api.as_ref(),
        model,
    );

    Ok(Report {
        schema_version: 3,
        files_changed: touched.len() as u64,
        lines_added: added,
        lines_removed: removed,
        touched_files: touched,
        files,
        targets: Sides {
            before: lb,
            after: la,
        },
        abi,
    })
}

fn read(p: &Path) -> Result<String, String> {
    std::fs::read_to_string(p).map_err(|e| format!("{}: {e}", p.display()))
}

/// rel-path -> source for every `*.rs` under `root` (excluding `target/`,
/// hidden dirs, and `build.rs`, which is not part of any compiled target).
fn collect_rs(root: &Path) -> Result<BTreeMap<String, String>, String> {
    if !root.is_dir() {
        return Err(format!("{}: not a directory", root.display()));
    }
    let mut out = BTreeMap::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        let entries = std::fs::read_dir(&dir).map_err(|e| format!("{}: {e}", dir.display()))?;
        for entry in entries {
            let entry = entry.map_err(|e| format!("{}: {e}", dir.display()))?;
            let path = entry.path();
            let name = entry.file_name().to_string_lossy().to_string();
            if path.is_dir() {
                if name != "target" && !name.starts_with('.') {
                    stack.push(path);
                }
            } else if name.ends_with(".rs") && name != "build.rs" {
                let rel = path.strip_prefix(root).unwrap_or(&path);
                let rel = rel
                    .components()
                    .map(|c| c.as_os_str().to_string_lossy())
                    .collect::<Vec<_>>()
                    .join("/");
                out.insert(rel, read(&path)?);
            }
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn crate_dir(root: &Path, lib: &str) -> PathBuf {
        std::fs::create_dir_all(root.join("src")).unwrap();
        std::fs::write(root.join("Cargo.toml"), "[package]\nname = \"t\"\n").unwrap();
        std::fs::write(root.join("src/lib.rs"), lib).unwrap();
        root.to_path_buf()
    }

    fn tmp(name: &str) -> PathBuf {
        let d =
            std::env::temp_dir().join(format!("diff_metrics_test_{}_{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        d
    }

    fn cli(before: &Path, after: &Path, api: Option<PathBuf>) -> Cli {
        Cli {
            before: before.into(),
            after: after.into(),
            api,
            no_rustfmt: true,
        }
    }

    #[test]
    fn missing_input_dir_is_an_error_not_an_empty_crate() {
        let a = crate_dir(&tmp("a1"), "");
        let e = run(&cli(&tmp("does_not_exist"), &a, None)).err().unwrap();
        assert!(e.contains("not a directory"), "{e}");
    }

    #[test]
    fn unreadable_api_file_is_an_error() {
        let a = crate_dir(&tmp("a2"), "");
        let e = run(&cli(&a, &a, Some(tmp("no_api.txt")))).err().unwrap();
        assert!(e.contains("no_api.txt"), "{e}");
    }

    #[test]
    fn missing_manifest_is_an_error() {
        let a = crate_dir(&tmp("a3"), "");
        std::fs::remove_file(a.join("Cargo.toml")).unwrap();
        assert!(run(&cli(&a, &a, None))
            .err()
            .unwrap()
            .contains("Cargo.toml"));
    }

    #[test]
    fn end_to_end_totals_and_abi() {
        let b = crate_dir(
            &tmp("b4"),
            "#[no_mangle] pub extern \"C\" fn f(x: i32) -> i32 { x }\n",
        );
        let a = crate_dir(
            &tmp("a4"),
            "#[no_mangle] pub extern \"C\" fn f(x: i32) -> i32 {\nx + 1\n}\n",
        );
        let r = run(&cli(&b, &a, None)).unwrap();
        assert_eq!((r.files_changed, r.lines_added, r.lines_removed), (1, 3, 1));
        assert!(r.abi.ok && r.abi.changed.is_empty());
    }
}
