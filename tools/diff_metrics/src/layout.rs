//! Which Cargo targets (the library, each binary) every source file is compiled
//! into, so an exported symbol is attributed to the artifact that exports it.
//!
//! Targets come from `Cargo.toml` (`[lib]`, `[[bin]]`, and Cargo's defaults:
//! `src/lib.rs`, `src/main.rs`, `src/bin/*` unless `autobins = false`). A
//! target's files are found by following `mod x;` declarations from its root
//! file — `x.rs` / `x/mod.rs`, inline `mod x { .. }`, and `#[path]` — the way
//! rustc does. `#[cfg]` is not evaluated (every `mod` is followed), and
//! `include!` is not followed. A file reached from no target is not compiled,
//! so it exports nothing.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path, PathBuf};

use serde::Serialize;

#[derive(Clone, Copy, PartialEq, Eq, Debug, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Kind {
    Lib,
    Bin,
}

#[derive(Clone, Debug, Serialize)]
pub struct Target {
    pub kind: Kind,
    pub name: String,
    /// root source file, relative to the crate dir
    pub root: String,
}

#[derive(Debug, Serialize)]
pub struct Layout {
    pub targets: Vec<Target>,
    /// `.rs` files no target compiles (stale copies, unused modules); their
    /// symbols are left out of the ABI diff.
    pub unreachable_files: Vec<String>,
    /// files compiled into each target, parallel to `targets`
    #[serde(skip)]
    pub files_of: Vec<BTreeSet<String>>,
}

/// The layout of a crate whose manifest is `manifest` (Cargo.toml text) and
/// whose sources are `files` (relative path -> contents).
pub fn layout(manifest: &str, files: &BTreeMap<String, String>) -> Result<Layout, String> {
    let targets = targets(manifest, files)?;
    let mut files_of = Vec::new();
    for t in &targets {
        let mut seen = BTreeSet::new();
        walk_file(files, Path::new(&t.root), true, &mut seen);
        files_of.push(seen);
    }
    let reached: BTreeSet<&String> = files_of.iter().flatten().collect();
    let unreachable_files = files
        .keys()
        .filter(|f| !reached.contains(f))
        .cloned()
        .collect();
    Ok(Layout {
        targets,
        unreachable_files,
        files_of,
    })
}

fn targets(manifest: &str, files: &BTreeMap<String, String>) -> Result<Vec<Target>, String> {
    let doc: toml::Value = manifest.parse().map_err(|e| format!("Cargo.toml: {e}"))?;
    let package = doc.get("package");
    let pkg = package
        .and_then(|p| p.get("name"))
        .and_then(|n| n.as_str())
        .ok_or("Cargo.toml: no [package] name")?
        .replace('-', "_");
    let str_of = |t: &toml::Value, k: &str| t.get(k).and_then(|v| v.as_str()).map(String::from);
    let has = |p: &str| files.contains_key(p);

    let mut out = Vec::new();
    match doc.get("lib") {
        Some(lib) => out.push(Target {
            kind: Kind::Lib,
            name: str_of(lib, "name").unwrap_or_else(|| pkg.clone()),
            root: clean(&str_of(lib, "path").unwrap_or_else(|| "src/lib.rs".into())),
        }),
        None if has("src/lib.rs") => out.push(Target {
            kind: Kind::Lib,
            name: pkg.clone(),
            root: "src/lib.rs".into(),
        }),
        None => {}
    }
    for b in doc
        .get("bin")
        .and_then(|b| b.as_array())
        .into_iter()
        .flatten()
    {
        let name = str_of(b, "name").ok_or("Cargo.toml: a [[bin]] has no name")?;
        let root = match str_of(b, "path") {
            Some(p) => clean(&p),
            None if name == pkg && has("src/main.rs") => "src/main.rs".into(),
            None => format!("src/bin/{name}.rs"),
        };
        out.push(Target {
            kind: Kind::Bin,
            name,
            root,
        });
    }
    let autobins = package
        .and_then(|p| p.get("autobins"))
        .and_then(|v| v.as_bool())
        .unwrap_or(true);
    if autobins {
        let mut auto: Vec<(String, String)> = Vec::new();
        if has("src/main.rs") {
            auto.push((pkg.clone(), "src/main.rs".into()));
        }
        for f in files.keys() {
            let Some(rest) = f.strip_prefix("src/bin/") else {
                continue;
            };
            match rest.split('/').collect::<Vec<_>>().as_slice() {
                [file] if file.ends_with(".rs") => {
                    auto.push((file.trim_end_matches(".rs").into(), f.clone()))
                }
                [dir, "main.rs"] => auto.push(((*dir).into(), f.clone())),
                _ => {}
            }
        }
        for (name, root) in auto {
            if !out
                .iter()
                .any(|t| t.kind == Kind::Bin && (t.name == name || t.root == root))
            {
                out.push(Target {
                    kind: Kind::Bin,
                    name,
                    root,
                });
            }
        }
    }
    for t in &out {
        if !has(&t.root) {
            return Err(format!(
                "Cargo.toml: {:?} target `{}` has root {} but that file is missing",
                t.kind, t.name, t.root
            ));
        }
    }
    Ok(out)
}

/// Add `file`, then every file its `mod x;` declarations load. `owns_dir`: the
/// file's child modules live beside it (crate roots, `mod.rs`, `#[path]` files)
/// rather than in a directory named after it.
fn walk_file(
    files: &BTreeMap<String, String>,
    file: &Path,
    owns_dir: bool,
    seen: &mut BTreeSet<String>,
) {
    let key = rel_str(file);
    let Some(src) = files.get(&key) else { return };
    if !seen.insert(key) {
        return;
    }
    // an unparsable file is reported by the ABI pass; its modules can't be found
    let Ok(ast) = syn::parse_file(src) else {
        return;
    };
    let file_dir = file.parent().unwrap_or(Path::new("")).to_path_buf();
    let stem = file
        .file_stem()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_default();
    let mod_dir = if owns_dir || stem == "mod" {
        file_dir.clone()
    } else {
        file_dir.join(stem)
    };
    walk_items(files, &ast.items, &file_dir, &mod_dir, false, seen);
}

fn walk_items(
    files: &BTreeMap<String, String>,
    items: &[syn::Item],
    file_dir: &Path,
    mod_dir: &Path,
    inline: bool,
    seen: &mut BTreeSet<String>,
) {
    for item in items {
        let syn::Item::Mod(m) = item else { continue };
        let name = m.ident.to_string();
        let path_attr = path_attr(&m.attrs);
        match &m.content {
            Some((_, inner)) => {
                let child = mod_dir.join(path_attr.as_deref().unwrap_or(&name));
                walk_items(files, inner, file_dir, &child, true, seen);
            }
            None => match path_attr {
                // `#[path]` is relative to the current file's directory, or to
                // the module directory inside an inline module
                Some(p) => {
                    let base = if inline { mod_dir } else { file_dir };
                    walk_file(files, &base.join(p), true, seen);
                }
                None => {
                    walk_file(files, &mod_dir.join(format!("{name}.rs")), false, seen);
                    walk_file(files, &mod_dir.join(&name).join("mod.rs"), true, seen);
                }
            },
        }
    }
}

fn path_attr(attrs: &[syn::Attribute]) -> Option<String> {
    attrs.iter().find_map(|a| match &a.meta {
        syn::Meta::NameValue(nv) if nv.path.is_ident("path") => match &nv.value {
            syn::Expr::Lit(syn::ExprLit {
                lit: syn::Lit::Str(s),
                ..
            }) => Some(s.value()),
            _ => None,
        },
        _ => None,
    })
}

/// `./a/../b.rs` -> `b.rs`, with `/` separators (the form `files` is keyed by).
fn rel_str(p: &Path) -> String {
    let mut parts: Vec<String> = Vec::new();
    for c in p.components() {
        match c {
            Component::CurDir => {}
            Component::ParentDir => {
                parts.pop();
            }
            Component::Normal(s) => parts.push(s.to_string_lossy().to_string()),
            Component::RootDir | Component::Prefix(_) => {}
        }
    }
    parts.join("/")
}

fn clean(p: &str) -> String {
    rel_str(&PathBuf::from(p))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn files(list: &[(&str, &str)]) -> BTreeMap<String, String> {
        list.iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    const PKG: &str = "[package]\nname = \"driver\"\nautobins = false\n";

    /// c2rust's layout: `lib.rs` at the crate root with `pub mod src;`, plus a
    /// separate `[[bin]]` root. `src/main.rs` defines `fn main` but is a lib
    /// module — it must not be treated as a binary.
    #[test]
    fn c2rust_layout_assigns_files_to_the_right_target() {
        let f = files(&[
            ("lib.rs", "pub mod src;"),
            ("src/mod.rs", "pub mod inventory; pub mod main;"),
            ("src/inventory.rs", ""),
            ("src/main.rs", "fn main() {}"),
            ("src_main_main.rs", "fn main() {}"),
            ("stale.rs", ""),
        ]);
        let m = format!(
            "{PKG}[lib]\nname = \"driver\"\npath = \"lib.rs\"\n\
             [[bin]]\nname = \"driver\"\npath = \"src_main_main.rs\"\n"
        );
        let l = layout(&m, &f).unwrap();
        assert_eq!(l.targets.len(), 2);
        assert_eq!(l.targets[0].kind, Kind::Lib);
        assert!(l.files_of[0].contains("src/main.rs"));
        assert!(l.files_of[0].contains("src/inventory.rs"));
        assert_eq!(
            l.files_of[1],
            BTreeSet::from(["src_main_main.rs".to_string()])
        );
        assert_eq!(l.unreachable_files, vec!["stale.rs"]);
    }

    #[test]
    fn non_mod_rs_children_inline_mods_and_path_attrs() {
        let f = files(&[
            (
                "src/lib.rs",
                "mod a; mod outer { mod inner; } #[path = \"other/p.rs\"] mod p;",
            ),
            ("src/a.rs", "mod b;"),
            ("src/a/b.rs", ""),
            ("src/outer/inner.rs", ""),
            ("src/other/p.rs", "mod q;"),
            ("src/other/q.rs", ""),
        ]);
        let l = layout("[package]\nname = \"x\"\n", &f).unwrap();
        assert_eq!(l.files_of[0].len(), 6, "{:?}", l.files_of[0]);
        assert!(l.unreachable_files.is_empty());
    }

    #[test]
    fn cargo_default_targets() {
        let f = files(&[
            ("src/lib.rs", ""),
            ("src/main.rs", ""),
            ("src/bin/tool.rs", ""),
        ]);
        let l = layout("[package]\nname = \"my-crate\"\n", &f).unwrap();
        let t: Vec<_> = l
            .targets
            .iter()
            .map(|t| (t.kind, t.name.as_str()))
            .collect();
        assert_eq!(
            t,
            [
                (Kind::Lib, "my_crate"),
                (Kind::Bin, "my_crate"),
                (Kind::Bin, "tool")
            ]
        );
    }

    #[test]
    fn missing_target_root_is_an_error() {
        let f = files(&[("src/lib.rs", "")]);
        let m = "[package]\nname = \"x\"\n[lib]\npath = \"lib.rs\"\n";
        assert!(layout(m, &f).unwrap_err().contains("lib.rs"));
        assert!(layout("not toml [", &f).is_err());
    }
}
