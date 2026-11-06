//! Line churn between two versions of one file, ignoring formatting.
//!
//! Both sides are rustfmt-normalized, then compared as a multiset of trimmed,
//! non-blank lines: added = lines in after beyond before, removed = the
//! reverse. Moving lines is therefore not churn. A file whose lines are the same
//! multiset in a different order is reported `reordered` (it counts as changed,
//! with 0 lines added / removed) rather than hidden.

use std::collections::{BTreeSet, HashMap};

/// How one file differs between the two crates.
pub struct FileDiff {
    pub status: &'static str, // "modified" | "added" | "removed" | "reordered" | "unchanged"
    pub lines_added: u64,
    pub lines_removed: u64,
    /// false if rustfmt was missing or rejected a side; raw text was compared.
    pub rustfmt_ok: bool,
}

impl FileDiff {
    pub fn changed(&self) -> bool {
        self.status != "unchanged"
    }
}

pub fn diff_file(before: Option<&String>, after: Option<&String>, use_rustfmt: bool) -> FileDiff {
    // rustfmt is a subprocess: only run it on files whose raw text differs
    let differs = before != after;
    let (b, b_ok) = norm(before, differs && use_rustfmt);
    let (a, a_ok) = norm(after, differs && use_rustfmt);
    let rustfmt_ok = b_ok && a_ok;
    let (status, lines_added, lines_removed) = match (&b, &a) {
        (None, Some(a)) => ("added", normalized_lines(a).len() as u64, 0),
        (Some(b), None) => ("removed", 0, normalized_lines(b).len() as u64),
        (None, None) => ("unchanged", 0, 0),
        (Some(b), Some(a)) => {
            let (bl, al) = (normalized_lines(b), normalized_lines(a));
            let (added, removed) = churn(&bl, &al);
            let status = if added + removed > 0 {
                "modified"
            } else if bl != al {
                "reordered"
            } else {
                "unchanged"
            };
            (status, added, removed)
        }
    };
    FileDiff {
        status,
        lines_added,
        lines_removed,
        rustfmt_ok,
    }
}

/// rustfmt-normalize `src` when asked; returns (text, rustfmt_ok). Falls back to
/// the raw text (ok=false) if rustfmt is missing or rejects the file.
fn norm(src: Option<&String>, use_rustfmt: bool) -> (Option<String>, bool) {
    let Some(s) = src else { return (None, true) };
    if !use_rustfmt {
        return (Some(s.clone()), true);
    }
    match rustfmt(s) {
        Some(f) => (Some(f), true),
        None => (Some(s.clone()), false),
    }
}

fn rustfmt(src: &str) -> Option<String> {
    use std::io::Write;
    use std::process::{Command, Stdio};
    let mut child = Command::new("rustfmt")
        .args(["--edition", "2021", "--quiet"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    child.stdin.take()?.write_all(src.as_bytes()).ok()?;
    let out = child.wait_with_output().ok()?;
    if out.status.success() {
        String::from_utf8(out.stdout).ok()
    } else {
        None
    }
}

/// Trimmed, non-blank lines.
fn normalized_lines(s: &str) -> Vec<String> {
    s.lines()
        .map(|l| l.trim().to_string())
        .filter(|l| !l.is_empty())
        .collect()
}

/// Multiset line difference: (added, removed). Not an edit distance.
fn churn(before: &[String], after: &[String]) -> (u64, u64) {
    fn count(ls: &[String]) -> HashMap<&str, i64> {
        let mut m = HashMap::new();
        for l in ls {
            *m.entry(l.as_str()).or_default() += 1;
        }
        m
    }
    let (b, a) = (count(before), count(after));
    let keys: BTreeSet<&str> = a.keys().chain(b.keys()).copied().collect();
    let (mut added, mut removed) = (0i64, 0i64);
    for k in keys {
        let d = a.get(k).copied().unwrap_or(0) - b.get(k).copied().unwrap_or(0);
        if d > 0 {
            added += d;
        } else {
            removed -= d;
        }
    }
    (added as u64, removed as u64)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(x: &str) -> String {
        x.to_string()
    }

    #[test]
    fn churn_is_multiset_line_difference() {
        let d = diff_file(Some(&s("a\nb\nc\n")), Some(&s("a\nb\nd\ne\n")), false);
        assert_eq!(
            (d.status, d.lines_added, d.lines_removed),
            ("modified", 2, 1)
        );
    }

    #[test]
    fn reordering_is_reported_not_hidden() {
        let d = diff_file(Some(&s("a\nb\n")), Some(&s("b\na\n")), false);
        assert_eq!(
            (d.status, d.lines_added, d.lines_removed),
            ("reordered", 0, 0)
        );
        assert!(d.changed());
    }

    #[test]
    fn whitespace_only_change_is_unchanged() {
        let d = diff_file(Some(&s("a\n\n  b\n")), Some(&s("a\nb\n")), false);
        assert!(!d.changed());
    }

    #[test]
    fn added_and_removed_files_count_all_lines() {
        let added = diff_file(None, Some(&s("a\n\nb\n")), false);
        assert_eq!((added.status, added.lines_added), ("added", 2));
        let removed = diff_file(Some(&s("a\n")), None, false);
        assert_eq!((removed.status, removed.lines_removed), ("removed", 1));
    }

    #[test]
    fn pure_reformat_is_not_churn_after_rustfmt() {
        let d = diff_file(
            Some(&s("fn f(x:i32)->i32{x+1}\n")),
            Some(&s("fn f(x: i32) -> i32 {\n    x + 1\n}\n")),
            true,
        );
        if d.rustfmt_ok {
            assert!(!d.changed());
        } // rustfmt unavailable here: the fallback is covered below
    }

    #[test]
    fn rustfmt_failure_falls_back_to_raw_text() {
        let bad = s("fn {{ not rust");
        let (n, ok) = norm(Some(&bad), true);
        assert!(!ok);
        assert_eq!(n.unwrap(), bad);
    }
}
