use std::path::{Path, PathBuf};

use clap::Parser;
use walkdir::{DirEntry, WalkDir};

use stats::*;

mod stats;

#[derive(Parser)]
struct Cli {
    /// Path to the root of a Rust project.
    ///
    /// Unless `-f`/`--file` is specified, this will recursively search the
    /// `src` subdirectory for files ending in `.rs`.
    project_root: PathBuf,

    /// Parse a single file rather than entire project directory
    #[arg(short, long)]
    file: bool,

    /// Enable verbose printing to stderr
    #[arg(short, long)]
    verbose: bool,
}

fn is_hidden(entry: &DirEntry) -> bool {
    entry.file_name().as_encoded_bytes().starts_with(b".")
}

fn iter_rust_files(project_root: &Path) -> impl Iterator<Item = DirEntry> {
    let walker = WalkDir::new(project_root.join("src"))
        .follow_links(true)
        .into_iter();
    walker
        .filter_entry(|e| !is_hidden(e))
        .map(|e| e.unwrap())
        .filter(|e| e.file_type().is_file() && e.path().extension().is_some_and(|ext| ext == "rs"))
}

fn evaluate_project(project_root: &Path, verbose: bool) -> Stats {
    iter_rust_files(project_root)
        .map(|dir_entry| {
            let path = dir_entry.path();
            let stats = Stats::evaluate_file(path);
            if verbose {
                let json = serde_json::to_string_pretty(&stats).unwrap();
                eprintln!("Stats for {path:?}: {json}\n");
            }
            stats
        })
        .sum()
}

impl Cli {
    fn evaluate(&self) -> Stats {
        if self.file {
            Stats::evaluate_file(&self.project_root)
        } else {
            evaluate_project(&self.project_root, self.verbose)
        }
    }
}

fn main() {
    let cli = Cli::parse();

    let stats = cli.evaluate();
    let json = serde_json::to_string_pretty(&stats).unwrap();

    println!("{json}");
}
