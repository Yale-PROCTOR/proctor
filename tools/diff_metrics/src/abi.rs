//! The exported-symbol diff: which symbols the library exports, their
//! signatures, and the layout of crate-defined types reachable from them.
//!
//! What it checks, syntactically (syn v2):
//! - **exported symbols** — items with `#[no_mangle]` or `#[export_name = ".."]`
//!   (incl. the `#[unsafe(..)]` forms), keyed by the *symbol* name, per target.
//!   `extern "C"` alone is a calling convention, not an export, so it is part of
//!   the signature, not of export detection;
//! - **signatures** — calling convention, parameter and return types (or a
//!   static's type), built from the parsed type tree with crate type aliases
//!   expanded and C type aliases (`size_t`, `c_long`, …) mapped to the
//!   primitive they denote on the host target (see [`CTypeModel`]);
//! - **layouts** — the C ABI is structural, so two crate types are compared by
//!   layout, not by name: `#[repr]`, field names, order and types (generic
//!   arguments substituted), enum variants and discriminants. A stage that
//!   replaces `struct list_int_t { .. }` with a generic `List<i32>` of the same
//!   layout keeps the ABI; one that reorders a field does not. Every crate type
//!   reached from an exported signature is compared, through fields, pointers
//!   and generic arguments.
//!
//! What it does not do: evaluate `#[cfg]`, resolve paths (types are identified
//! by name, so same-named types in different modules are one set of
//! definitions), or compute sizes and alignment. It is a conservative change
//! detector, not a proof of ABI compatibility.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};

use quote::ToTokens;
use serde::Serialize;
use syn::punctuated::Punctuated;
use syn::visit::Visit;

use crate::layout::{Kind, Layout};

/// Sizes and signedness of C types on the target, needed to equate e.g.
/// `c_long` with `i64` or `c_char` with `i8`. Taken from the target this tool
/// is compiled for: the pipeline builds, measures and tests a crate on one
/// machine, so it is the crate's target. Reported in the output.
#[derive(Serialize, Clone)]
pub struct CTypeModel {
    pub target: String,
    pub c_char: &'static str,
    pub c_long: &'static str,
    pub c_ulong: &'static str,
    pub usize: &'static str,
    pub isize: &'static str,
}

impl CTypeModel {
    pub fn host() -> Self {
        let (c_long, c_ulong) = match std::mem::size_of::<std::ffi::c_long>() {
            4 => ("i32", "u32"),
            _ => ("i64", "u64"),
        };
        let (usize, isize) = match usize::BITS {
            32 => ("u32", "i32"),
            16 => ("u16", "i16"),
            _ => ("u64", "i64"),
        };
        CTypeModel {
            target: format!("{}-{}", std::env::consts::ARCH, std::env::consts::OS),
            c_char: if (std::ffi::c_char::MIN as i64) < 0 {
                "i8"
            } else {
                "u8"
            },
            c_long,
            c_ulong,
            usize,
            isize,
        }
    }

    /// The fixed-width primitive a C alias, `usize`/`isize`, or a Rust
    /// primitive denotes. `c_int` is taken as 32 bits, true of every target
    /// Rust supports but 16-bit AVR/MSP430.
    fn primitive(&self, ident: &str) -> Option<&'static str> {
        Some(match ident {
            "usize" | "size_t" | "uintptr_t" => self.usize,
            "isize" | "ssize_t" | "ptrdiff_t" | "intptr_t" => self.isize,
            "c_char" => self.c_char,
            "c_long" => self.c_long,
            "c_ulong" => self.c_ulong,
            "i8" | "c_schar" | "int8_t" => "i8",
            "u8" | "c_uchar" | "uint8_t" => "u8",
            "i16" | "c_short" | "int16_t" => "i16",
            "u16" | "c_ushort" | "uint16_t" => "u16",
            "i32" | "c_int" | "int32_t" => "i32",
            "u32" | "c_uint" | "uint32_t" => "u32",
            "i64" | "c_longlong" | "int64_t" => "i64",
            "u64" | "c_ulonglong" | "uint64_t" => "u64",
            "f32" | "c_float" => "f32",
            "f64" | "c_double" => "f64",
            "i128" => "i128",
            "u128" => "u128",
            "bool" => "bool",
            "char" => "char",
            "str" => "str",
            _ => return None,
        })
    }
}

/// The public API to restrict the diff to (from the C headers): exported
/// symbols, and the types whose layout the headers expose.
#[derive(Default)]
pub struct Api {
    pub symbols: BTreeSet<String>,
    pub types: BTreeSet<String>,
}

impl Api {
    /// One entry per line: `symbol NAME`, `type NAME`, or a bare `NAME`
    /// (a symbol). Blank lines and `#` comments are skipped.
    pub fn parse(text: &str) -> Result<Api, String> {
        let mut api = Api::default();
        for (i, line) in text.lines().enumerate() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            match line.split_whitespace().collect::<Vec<_>>().as_slice() {
                [name] | ["symbol", name] => api.symbols.insert(name.to_string()),
                ["type", name] => api.types.insert(name.to_string()),
                _ => {
                    return Err(format!(
                        "api line {}: expected `symbol NAME`, `type NAME` or `NAME`, got {line:?}",
                        i + 1
                    ))
                }
            };
        }
        Ok(api)
    }
}

// ---- the type model -----------------------------------------------------------

/// A type as the ABI sees it: aliases expanded, C aliases resolved, lifetimes,
/// parameter names and `unsafe` dropped.
#[derive(Clone, PartialEq, Debug)]
enum Ty {
    Prim(&'static str),
    Ptr {
        mutable: bool,
        to: Box<Ty>,
    },
    Ref {
        mutable: bool,
        to: Box<Ty>,
    },
    Array(Box<Ty>, String),
    Slice(Box<Ty>),
    Tuple(Vec<Ty>),
    Fn(FnTy),
    /// a named type with generic arguments: a crate type, or an external one
    /// (`Option`, `c_void`, …), or a generic parameter inside a definition
    Named(String, Vec<Ty>),
    /// anything else (trait objects, `impl Trait`, qualified paths), as tokens
    Other(String),
}

#[derive(Clone, PartialEq, Debug)]
struct FnTy {
    abi: String,
    params: Vec<Ty>,
    variadic: bool,
    ret: Box<Ty>, // `()` when there is no return type
}

/// What an exported symbol is.
#[derive(Clone)]
enum Item {
    Fn(FnTy),
    Static { mutable: bool, ty: Ty },
}

/// One definition of a crate type.
#[derive(Clone, PartialEq)]
struct TypeDef {
    reprs: String,
    /// `#[repr(uN)]`/`#[repr(iN)]` integer, for a fieldless enum
    int_repr: Option<&'static str>,
    params: Vec<String>,
    body: Body,
}

#[derive(Clone, PartialEq)]
enum Body {
    Struct(Fields),
    Union(Fields),
    Enum(Vec<Variant>),
    /// `extern { type T; }`: declared, no layout
    Opaque,
}

/// (field name or tuple index, type)
type Fields = Vec<(String, Ty)>;

#[derive(Clone, PartialEq)]
struct Variant {
    name: String,
    fields: Fields,
    discriminant: Option<String>,
}

fn render(t: &Ty) -> String {
    match t {
        Ty::Prim(p) => p.to_string(),
        Ty::Ptr { mutable, to } => {
            format!("*{} {}", if *mutable { "mut" } else { "const" }, render(to))
        }
        Ty::Ref { mutable, to } => format!("&{}{}", if *mutable { "mut " } else { "" }, render(to)),
        Ty::Array(e, n) => format!("[{}; {n}]", render(e)),
        Ty::Slice(e) => format!("[{}]", render(e)),
        Ty::Tuple(v) if v.len() == 1 => format!("({},)", render(&v[0])), // a 1-tuple
        Ty::Tuple(v) => format!("({})", join(v)),
        Ty::Fn(f) => render_fn(f),
        Ty::Named(n, a) if a.is_empty() => n.clone(),
        Ty::Named(n, a) => format!("{n}<{}>", join(a)),
        Ty::Other(s) => s.clone(),
    }
}

fn join(v: &[Ty]) -> String {
    v.iter().map(render).collect::<Vec<_>>().join(", ")
}

fn render_fn(f: &FnTy) -> String {
    let mut params: Vec<String> = f.params.iter().map(render).collect();
    if f.variadic {
        params.push("...".into());
    }
    let ret = match &*f.ret {
        Ty::Tuple(v) if v.is_empty() => String::new(),
        r => format!(" -> {}", render(r)),
    };
    format!("extern \"{}\" fn({}){ret}", f.abi, params.join(", "))
}

fn render_item(i: &Item) -> String {
    match i {
        Item::Fn(f) => render_fn(f),
        Item::Static { mutable, ty } => format!(
            "static {}{}",
            if *mutable { "mut " } else { "" },
            render(ty)
        ),
    }
}

fn render_fields(f: &Fields) -> String {
    let named = f.first().is_some_and(|(n, _)| n.parse::<usize>().is_err());
    let parts: Vec<String> = f
        .iter()
        .map(|(n, t)| {
            if named {
                format!("{n}: {}", render(t))
            } else {
                render(t)
            }
        })
        .collect();
    match (f.is_empty(), named) {
        (true, _) => String::new(),
        (false, true) => format!("{{{}}}", parts.join(", ")),
        (false, false) => format!("({})", parts.join(", ")),
    }
}

fn render_def(d: &TypeDef) -> String {
    let params = if d.params.is_empty() {
        String::new()
    } else {
        format!("<{}>", d.params.join(", "))
    };
    let body = match &d.body {
        Body::Opaque => return "opaque".into(),
        Body::Struct(f) => format!("struct{params} {}", render_fields(f)),
        Body::Union(f) => format!("union{params} {}", render_fields(f)),
        Body::Enum(vs) => {
            let vs: Vec<String> = vs
                .iter()
                .map(|v| {
                    let d = v
                        .discriminant
                        .as_ref()
                        .map(|d| format!(" = {d}"))
                        .unwrap_or_default();
                    format!("{}{}{d}", v.name, render_fields(&v.fields))
                })
                .collect();
            format!("enum{params} {{{}}}", vs.join(", "))
        }
    };
    format!("{}{body}", d.reprs)
}

/// Replace generic parameters by their arguments.
fn subst(t: &Ty, map: &HashMap<&str, &Ty>) -> Ty {
    let s = |t: &Ty| Box::new(subst(t, map));
    match t {
        Ty::Named(n, a) if a.is_empty() => map
            .get(n.as_str())
            .map_or_else(|| t.clone(), |r| (*r).clone()),
        Ty::Named(n, a) => Ty::Named(n.clone(), a.iter().map(|x| subst(x, map)).collect()),
        Ty::Ptr { mutable, to } => Ty::Ptr {
            mutable: *mutable,
            to: s(to),
        },
        Ty::Ref { mutable, to } => Ty::Ref {
            mutable: *mutable,
            to: s(to),
        },
        Ty::Array(e, n) => Ty::Array(s(e), n.clone()),
        Ty::Slice(e) => Ty::Slice(s(e)),
        Ty::Tuple(v) => Ty::Tuple(v.iter().map(|x| subst(x, map)).collect()),
        Ty::Fn(f) => Ty::Fn(FnTy {
            abi: f.abi.clone(),
            params: f.params.iter().map(|x| subst(x, map)).collect(),
            variadic: f.variadic,
            ret: s(&f.ret),
        }),
        Ty::Prim(_) | Ty::Other(_) => t.clone(),
    }
}

fn subst_fields(f: &Fields, map: &HashMap<&str, &Ty>) -> Fields {
    f.iter().map(|(n, t)| (n.clone(), subst(t, map))).collect()
}

// ---- one crate ---------------------------------------------------------------

/// One crate's exports and type definitions.
pub struct Side {
    lib: BTreeMap<String, Vec<Item>>,
    bins: BTreeSet<(String, String)>,
    types: BTreeMap<String, Vec<TypeDef>>,
    unparsed: Vec<String>,
}

/// How a name resolves in one crate.
enum Lookup {
    /// not defined in the crate: std, libc, a primitive alias, a generic param
    External,
    One(TypeDef),
    /// several distinct definitions under one name (path resolution would be
    /// needed to tell which applies); compared as a set
    Many(String),
}

impl Side {
    fn lookup(&self, name: &str, args: &[Ty]) -> Lookup {
        let Some(defs) = self.types.get(name) else {
            return Lookup::External;
        };
        let mut distinct: Vec<&TypeDef> = Vec::new();
        for d in defs {
            if !distinct.contains(&d) {
                distinct.push(d);
            }
        }
        // forward declarations merge with the definition (c2rust emits both)
        if distinct.len() > 1 {
            distinct.retain(|d| d.body != Body::Opaque);
        }
        if distinct.len() > 1 {
            let mut r: Vec<String> = distinct.iter().map(|d| render_def(d)).collect();
            r.sort();
            return Lookup::Many(r.join(" | "));
        }
        let d = distinct[0];
        if d.params.len() != args.len() {
            return Lookup::One(d.clone());
        }
        let map: HashMap<&str, &Ty> = d.params.iter().map(String::as_str).zip(args).collect();
        let body = match &d.body {
            Body::Struct(f) => Body::Struct(subst_fields(f, &map)),
            Body::Union(f) => Body::Union(subst_fields(f, &map)),
            Body::Enum(vs) => Body::Enum(
                vs.iter()
                    .map(|v| Variant {
                        fields: subst_fields(&v.fields, &map),
                        ..v.clone()
                    })
                    .collect(),
            ),
            Body::Opaque => Body::Opaque,
        };
        Lookup::One(TypeDef {
            params: Vec::new(),
            body,
            ..d.clone()
        })
    }
}

fn render_lookup(l: &Lookup) -> String {
    match l {
        Lookup::External => "(not defined in the crate)".into(),
        Lookup::One(d) => render_def(d),
        Lookup::Many(s) => s.clone(),
    }
}

pub fn side(files: &BTreeMap<String, String>, layout: &Layout, model: &CTypeModel) -> Side {
    let compiled: BTreeSet<&String> = layout.files_of.iter().flatten().collect();
    let mut asts = BTreeMap::new();
    let mut unparsed = Vec::new();
    for f in compiled {
        match syn::parse_file(&files[f]) {
            Ok(ast) => {
                asts.insert(f.clone(), ast);
            }
            Err(_) => unparsed.push(f.clone()),
        }
    }

    // non-generic `type X = Y;` aliases, expanded only when unambiguous
    let mut alias_defs: BTreeMap<String, Vec<syn::Type>> = BTreeMap::new();
    for ast in asts.values() {
        AliasCollector(&mut alias_defs).visit_file(ast);
    }
    let aliases: HashMap<String, syn::Type> = alias_defs
        .into_iter()
        .filter_map(|(name, defs)| {
            let distinct: BTreeSet<String> = defs.iter().map(tokens).collect();
            (distinct.len() == 1).then(|| (name, defs[0].clone()))
        })
        .collect();
    let canon = Canon {
        model,
        aliases: &aliases,
    };

    let mut types: BTreeMap<String, Vec<TypeDef>> = BTreeMap::new();
    let mut exports_of: BTreeMap<&String, Vec<(String, Item)>> = BTreeMap::new();
    for (f, ast) in &asts {
        let mut c = Collector {
            canon: &canon,
            exports: Vec::new(),
            types: Vec::new(),
        };
        c.visit_file(ast);
        for (name, def) in c.types {
            types.entry(name).or_default().push(def);
        }
        exports_of.insert(f, c.exports);
    }
    let mut lib: BTreeMap<String, Vec<Item>> = BTreeMap::new();
    let mut bins = BTreeSet::new();
    for (t, files) in layout.targets.iter().zip(&layout.files_of) {
        for f in files {
            for (sym, item) in exports_of.get(f).into_iter().flatten() {
                match t.kind {
                    Kind::Lib => lib.entry(sym.clone()).or_default().push(item.clone()),
                    Kind::Bin => {
                        bins.insert((t.name.clone(), sym.clone()));
                    }
                }
            }
        }
    }
    Side {
        lib,
        bins,
        types,
        unparsed,
    }
}

// ---- the diff ----------------------------------------------------------------

#[derive(Serialize)]
pub struct AbiDiff {
    /// No exported library symbol removed or re-typed, no breaking layout
    /// change, no duplicate exports, and every compiled file parsed.
    pub ok: bool,
    pub api_restricted: bool,
    pub c_type_model: CTypeModel,
    /// exported library symbols present before, gone after
    pub removed: Vec<String>,
    /// symbols a binary target exported before and no longer does: not
    /// library ABI, so not a break (a later stage un-exporting them is fine)
    pub removed_bin_internal: Vec<String>,
    pub added: Vec<String>,
    /// same symbol, different signature shape
    pub changed: Vec<SigChange>,
    /// crate types, reached from an exported signature, whose definition
    /// changed (named `before -> after` when the stage renamed the type)
    pub types_changed: Vec<TypeChange>,
    /// symbols exported more than once by the library after the stage
    pub duplicate_symbols: Vec<String>,
    /// compiled files that did not parse (their exports are unknown)
    pub unparsed_files: Unparsed,
}

#[derive(Serialize)]
pub struct SigChange {
    pub name: String,
    pub before: String,
    pub after: String,
}

#[derive(Serialize, Clone)]
pub struct TypeChange {
    pub name: String,
    /// "layout": the definition changed. "int_to_enum": an integer became a
    /// fieldless enum with that integer as its `#[repr]` (or back) — the same
    /// in the C ABI, but Rust now assumes C only passes listed values; that is
    /// a contract the tests check, not this tool.
    pub kind: &'static str,
    /// reached by value from an exported signature (C sees its layout)
    pub by_value: bool,
    /// the C headers define its body (`--api` lists it as a `type`)
    pub public_layout: bool,
    /// a layout change that is by value, public, or with no `--api` to tell
    pub breaking: bool,
    pub before: String,
    pub after: String,
}

#[derive(Serialize)]
pub struct Unparsed {
    pub before: Vec<String>,
    pub after: Vec<String>,
}

pub fn diff(before: &Side, after: &Side, api: Option<&Api>, model: CTypeModel) -> AbiDiff {
    let in_api = |s: &String| api.is_none_or(|a| a.symbols.contains(s));
    let lib_b: BTreeMap<&String, &Vec<Item>> =
        before.lib.iter().filter(|(s, _)| in_api(s)).collect();
    let lib_a: BTreeMap<&String, &Vec<Item>> =
        after.lib.iter().filter(|(s, _)| in_api(s)).collect();

    let removed = lib_b
        .keys()
        .filter(|s| !lib_a.contains_key(*s))
        .map(|s| s.to_string())
        .collect();
    let added = lib_a
        .keys()
        .filter(|s| !lib_b.contains_key(*s))
        .map(|s| s.to_string())
        .collect();
    let mut cmp = Cmp {
        b: before,
        a: after,
        api,
        seen: HashSet::new(),
        changes: BTreeMap::new(),
    };
    let mut changed = Vec::new();
    for (sym, b) in &lib_b {
        let Some(a) = lib_a.get(sym) else { continue };
        let same = match (b.as_slice(), a.as_slice()) {
            ([x], [y]) => cmp.item(x, y),
            // several definitions of one symbol: compare as rendered sets
            _ => items(b) == items(a),
        };
        if !same {
            changed.push(SigChange {
                name: sym.to_string(),
                before: items(b),
                after: items(a),
            });
        }
    }
    let removed_bin_internal: BTreeSet<String> = before
        .bins
        .difference(&after.bins)
        .map(|(_, sym)| sym.clone())
        .collect();
    let duplicate_symbols = lib_a
        .iter()
        .filter(|(_, v)| v.len() > 1)
        .map(|(s, _)| s.to_string())
        .collect();

    let mut d = AbiDiff {
        ok: false,
        api_restricted: api.is_some(),
        c_type_model: model,
        removed,
        removed_bin_internal: removed_bin_internal.into_iter().collect(),
        added,
        changed,
        types_changed: cmp.changes.into_values().collect(),
        duplicate_symbols,
        unparsed_files: Unparsed {
            before: before.unparsed.clone(),
            after: after.unparsed.clone(),
        },
    };
    d.ok = d.removed.is_empty()
        && d.changed.is_empty()
        && d.duplicate_symbols.is_empty()
        && d.types_changed.iter().all(|t| !t.breaking)
        && d.unparsed_files.before.is_empty()
        && d.unparsed_files.after.is_empty();
    d
}

fn items(v: &[Item]) -> String {
    let s: BTreeSet<String> = v.iter().map(render_item).collect();
    s.into_iter().collect::<Vec<_>>().join(" | ")
}

/// Structural comparison of a before-type with an after-type. Returns false
/// when their *shape* differs at this level (that makes the enclosing
/// signature or layout differ); differences inside a crate type's definition
/// are recorded as a [`TypeChange`] of that type instead.
struct Cmp<'a> {
    b: &'a Side,
    a: &'a Side,
    api: Option<&'a Api>,
    /// (before, after, by_value) pairs already compared — breaks cycles
    /// through recursive types (`next: *mut Node`)
    seen: HashSet<(String, String, bool)>,
    changes: BTreeMap<String, TypeChange>,
}

impl Cmp<'_> {
    fn item(&mut self, x: &Item, y: &Item) -> bool {
        match (x, y) {
            (Item::Fn(f), Item::Fn(g)) => self.fn_ty(f, g, true),
            (Item::Static { mutable: m, ty: t }, Item::Static { mutable: n, ty: u }) => {
                (m == n) & self.ty(t, u, true)
            }
            _ => false,
        }
    }

    fn fn_ty(&mut self, f: &FnTy, g: &FnTy, by_value: bool) -> bool {
        let mut same =
            f.abi == g.abi && f.variadic == g.variadic && f.params.len() == g.params.len();
        if same {
            for (p, q) in f.params.iter().zip(&g.params) {
                same &= self.ty(p, q, by_value);
            }
        }
        self.ty(&f.ret, &g.ret, by_value) & same
    }

    fn ty(&mut self, x: &Ty, y: &Ty, by_value: bool) -> bool {
        match (x, y) {
            (Ty::Named(..), Ty::Named(..)) => self.named(x, y, by_value),
            (Ty::Prim(p), Ty::Named(n, a)) => {
                let side = self.a;
                self.int_enum(p, side, n, a, by_value, false)
            }
            (Ty::Named(n, a), Ty::Prim(p)) => {
                let side = self.b;
                self.int_enum(p, side, n, a, by_value, true)
            }
            (Ty::Prim(p), Ty::Prim(q)) => p == q,
            (Ty::Ptr { mutable: m, to: t }, Ty::Ptr { mutable: n, to: u })
            | (Ty::Ref { mutable: m, to: t }, Ty::Ref { mutable: n, to: u }) => {
                (m == n) & self.ty(t, u, false)
            }
            (Ty::Array(e, n), Ty::Array(f, m)) => (n == m) & self.ty(e, f, by_value),
            (Ty::Slice(e), Ty::Slice(f)) => self.ty(e, f, by_value),
            (Ty::Tuple(v), Ty::Tuple(w)) => self.all(v, w, by_value),
            // a callback's parameters are not reached by value from here
            (Ty::Fn(f), Ty::Fn(g)) => self.fn_ty(f, g, false),
            (Ty::Other(s), Ty::Other(t)) => s == t,
            _ => false,
        }
    }

    fn all(&mut self, v: &[Ty], w: &[Ty], by_value: bool) -> bool {
        let mut same = v.len() == w.len();
        if same {
            for (x, y) in v.iter().zip(w) {
                same &= self.ty(x, y, by_value);
            }
        }
        same
    }

    fn named(&mut self, x: &Ty, y: &Ty, by_value: bool) -> bool {
        let (Ty::Named(xn, xa), Ty::Named(yn, ya)) = (x, y) else {
            unreachable!()
        };
        let (xs, ys) = (render(x), render(y));
        if !self.seen.insert((xs.clone(), ys.clone(), by_value)) {
            return true; // already compared (or being compared, for recursive types)
        }
        match (self.b.lookup(xn, xa), self.a.lookup(yn, ya)) {
            // external types are compared by name and arguments (`Option<T>`)
            (Lookup::External, Lookup::External) => (xn == yn) & self.all(xa, ya, by_value),
            (Lookup::One(dx), Lookup::One(dy)) => {
                if !self.def(&dx, &dy, by_value) {
                    self.record(
                        &xs,
                        &ys,
                        xn,
                        yn,
                        "layout",
                        by_value,
                        render_def(&dx),
                        render_def(&dy),
                    );
                }
                true
            }
            (lx, ly) => {
                let (rb, ra) = (render_lookup(&lx), render_lookup(&ly));
                if rb != ra || xs != ys {
                    self.record(&xs, &ys, xn, yn, "layout", by_value, rb, ra);
                }
                true
            }
        }
    }

    fn def(&mut self, x: &TypeDef, y: &TypeDef, by_value: bool) -> bool {
        let same_repr = x.reprs == y.reprs;
        let same_body = match (&x.body, &y.body) {
            (Body::Struct(f), Body::Struct(g)) | (Body::Union(f), Body::Union(g)) => {
                self.fields(f, g, by_value)
            }
            (Body::Enum(v), Body::Enum(w)) => {
                let mut same = v.len() == w.len();
                if same {
                    for (p, q) in v.iter().zip(w) {
                        same &= p.name == q.name
                            && p.discriminant == q.discriminant
                            && self.fields(&p.fields, &q.fields, by_value);
                    }
                }
                same
            }
            (Body::Opaque, Body::Opaque) => true,
            _ => false,
        };
        same_repr && same_body
    }

    fn fields(&mut self, f: &Fields, g: &Fields, by_value: bool) -> bool {
        let mut same = f.len() == g.len();
        if same {
            for ((n, t), (m, u)) in f.iter().zip(g) {
                // past a renamed/reordered field the pairing is meaningless:
                // don't compare those types (it would invent spurious changes)
                same &= n == m && self.ty(t, u, by_value);
            }
        }
        same
    }

    /// An integer on one side, a crate type on the other: equal in the C ABI
    /// iff that type is a fieldless enum with this integer as its repr.
    #[allow(clippy::too_many_arguments)]
    fn int_enum(
        &mut self,
        p: &str,
        side: &Side,
        n: &str,
        a: &[Ty],
        by_value: bool,
        enum_before: bool,
    ) -> bool {
        let Lookup::One(d) = side.lookup(n, a) else {
            return false;
        };
        let fieldless = matches!(&d.body, Body::Enum(vs) if vs.iter().all(|v| v.fields.is_empty()));
        if !fieldless || d.int_repr != Some(p) {
            return false;
        }
        let (e, i) = (render_def(&d), p.to_string());
        let (bs, as_, b, a2) = if enum_before {
            (n, p, e, i)
        } else {
            (p, n, i, e)
        };
        self.record(bs, as_, bs, as_, "int_to_enum", by_value, b, a2);
        true
    }

    #[allow(clippy::too_many_arguments)]
    fn record(
        &mut self,
        xs: &str,
        ys: &str,
        xn: &str,
        yn: &str,
        kind: &'static str,
        by_value: bool,
        before: String,
        after: String,
    ) {
        let public_layout = self
            .api
            .is_some_and(|a| a.types.contains(xn) || a.types.contains(yn));
        let name = if xs == ys {
            xs.to_string()
        } else {
            format!("{xs} -> {ys}")
        };
        let no_api = self.api.is_none();
        let e = self.changes.entry(name.clone()).or_insert(TypeChange {
            name,
            kind,
            by_value: false,
            public_layout,
            breaking: false,
            before,
            after,
        });
        e.by_value |= by_value;
        e.breaking = kind == "layout" && (e.by_value || public_layout || no_api);
    }
}

// ---- building types from the syntax tree --------------------------------------

struct Canon<'a> {
    model: &'a CTypeModel,
    aliases: &'a HashMap<String, syn::Type>,
}

impl Canon<'_> {
    fn ty(&self, t: &syn::Type, depth: u32) -> Ty {
        use syn::Type as T;
        let b = |t: &syn::Type| Box::new(self.ty(t, depth));
        match t {
            T::Paren(p) => self.ty(&p.elem, depth),
            T::Group(g) => self.ty(&g.elem, depth),
            T::Ptr(p) => Ty::Ptr {
                mutable: p.mutability.is_some(),
                to: b(&p.elem),
            },
            T::Reference(r) => Ty::Ref {
                mutable: r.mutability.is_some(),
                to: b(&r.elem),
            },
            T::Slice(s) => Ty::Slice(b(&s.elem)),
            T::Array(a) => Ty::Array(b(&a.elem), int_text(&a.len)),
            T::Tuple(tu) => Ty::Tuple(tu.elems.iter().map(|t| self.ty(t, depth)).collect()),
            T::BareFn(f) => Ty::Fn(FnTy {
                abi: abi_name(f.abi.as_ref()),
                params: f.inputs.iter().map(|a| self.ty(&a.ty, depth)).collect(),
                variadic: f.variadic.is_some(),
                ret: Box::new(self.ret(&f.output, depth)),
            }),
            T::Never(_) => Ty::Other("!".into()),
            T::Path(p) if p.qself.is_none() => self.path(&p.path, depth),
            other => Ty::Other(tokens(other)),
        }
    }

    fn path(&self, path: &syn::Path, depth: u32) -> Ty {
        let Some(last) = path.segments.last() else {
            return Ty::Other(tokens(path));
        };
        let name = last.ident.to_string();
        match &last.arguments {
            syn::PathArguments::None => {
                if depth < 32 {
                    if let Some(target) = self.aliases.get(&name) {
                        return self.ty(target, depth + 1);
                    }
                }
                match self.model.primitive(&name) {
                    Some(p) => Ty::Prim(p),
                    None => Ty::Named(name, Vec::new()),
                }
            }
            syn::PathArguments::AngleBracketed(a) => {
                let args = a
                    .args
                    .iter()
                    .filter_map(|g| match g {
                        syn::GenericArgument::Type(t) => Some(self.ty(t, depth)),
                        syn::GenericArgument::Lifetime(_) => None,
                        other => Some(Ty::Other(tokens(other))),
                    })
                    .collect();
                Ty::Named(name, args)
            }
            syn::PathArguments::Parenthesized(_) => Ty::Other(tokens(path)),
        }
    }

    fn ret(&self, out: &syn::ReturnType, depth: u32) -> Ty {
        match out {
            syn::ReturnType::Default => Ty::Tuple(Vec::new()),
            syn::ReturnType::Type(_, t) => self.ty(t, depth),
        }
    }

    /// Calling convention + parameter types + return type. Not part of it:
    /// parameter names and patterns, `unsafe`, `pub`.
    fn sig(&self, s: &syn::Signature) -> FnTy {
        FnTy {
            abi: abi_name(s.abi.as_ref()),
            params: s
                .inputs
                .iter()
                .map(|a| match a {
                    syn::FnArg::Receiver(r) => Ty::Other(tokens(r)),
                    syn::FnArg::Typed(pt) => self.ty(&pt.ty, 0),
                })
                .collect(),
            variadic: s.variadic.is_some(),
            ret: Box::new(self.ret(&s.output, 0)),
        }
    }

    fn fields<'f>(&self, f: impl IntoIterator<Item = &'f syn::Field>) -> Fields {
        f.into_iter()
            .enumerate()
            .map(|(i, f)| {
                (
                    f.ident
                        .as_ref()
                        .map_or_else(|| i.to_string(), |id| id.to_string()),
                    self.ty(&f.ty, 0),
                )
            })
            .collect()
    }

    fn def(&self, attrs: &[syn::Attribute], generics: &syn::Generics, body: Body) -> TypeDef {
        let mut reprs: Vec<String> = Vec::new();
        let mut int_repr = None;
        for a in attrs.iter().filter(|a| a.path().is_ident("repr")) {
            reprs.push(format!("#[{}]", tokens(&a.meta).replace(' ', "")));
            if let Ok(list) =
                a.parse_args_with(Punctuated::<syn::Meta, syn::Token![,]>::parse_terminated)
            {
                for m in list {
                    if let Some(id) = m.path().get_ident() {
                        let p = self.model.primitive(&id.to_string());
                        if p.is_some_and(|p| p.starts_with('i') || p.starts_with('u')) {
                            int_repr = p;
                        }
                    }
                }
            }
        }
        reprs.sort();
        let params = generics
            .params
            .iter()
            .filter_map(|p| match p {
                syn::GenericParam::Type(t) => Some(t.ident.to_string()),
                syn::GenericParam::Const(c) => Some(c.ident.to_string()),
                syn::GenericParam::Lifetime(_) => None,
            })
            .collect();
        TypeDef {
            reprs: reprs.concat(),
            int_repr,
            params,
            body,
        }
    }
}

fn abi_name(abi: Option<&syn::Abi>) -> String {
    match abi {
        None => "Rust".into(),
        Some(a) => a.name.as_ref().map_or_else(|| "C".into(), |s| s.value()),
    }
}

/// An integer literal without its suffix (`3u32` -> `3`, `-1` stays), else
/// the expression's tokens.
fn int_text(e: &syn::Expr) -> String {
    match e {
        syn::Expr::Lit(syn::ExprLit {
            lit: syn::Lit::Int(i),
            ..
        }) => i.base10_digits().to_string(),
        syn::Expr::Unary(syn::ExprUnary {
            op: syn::UnOp::Neg(_),
            expr,
            ..
        }) => format!("-{}", int_text(expr)),
        syn::Expr::Paren(p) => int_text(&p.expr),
        other => tokens(other),
    }
}

/// A token stream as text with canonical spacing (one space between tokens).
fn tokens<T: ToTokens>(t: &T) -> String {
    t.to_token_stream()
        .to_string()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// The symbol an item exports: `#[no_mangle]` (its own name) or
/// `#[export_name = ".."]`, also in the `#[unsafe(..)]` form.
fn export_symbol(ident: &syn::Ident, attrs: &[syn::Attribute]) -> Option<String> {
    for a in attrs {
        let meta = if a.path().is_ident("unsafe") {
            match a.parse_args::<syn::Meta>() {
                Ok(m) => m,
                Err(_) => continue,
            }
        } else {
            a.meta.clone()
        };
        match &meta {
            syn::Meta::Path(p) if p.is_ident("no_mangle") => return Some(ident.to_string()),
            syn::Meta::NameValue(nv) if nv.path.is_ident("export_name") => {
                if let syn::Expr::Lit(syn::ExprLit {
                    lit: syn::Lit::Str(s),
                    ..
                }) = &nv.value
                {
                    return Some(s.value());
                }
            }
            _ => {}
        }
    }
    None
}

struct AliasCollector<'a>(&'a mut BTreeMap<String, Vec<syn::Type>>);

impl<'ast> Visit<'ast> for AliasCollector<'_> {
    fn visit_item_type(&mut self, t: &'ast syn::ItemType) {
        if t.generics.params.is_empty() {
            self.0
                .entry(t.ident.to_string())
                .or_default()
                .push((*t.ty).clone());
        }
    }
}

struct Collector<'a> {
    canon: &'a Canon<'a>,
    exports: Vec<(String, Item)>,
    types: Vec<(String, TypeDef)>,
}

impl<'ast> Visit<'ast> for Collector<'_> {
    fn visit_item_fn(&mut self, f: &'ast syn::ItemFn) {
        if let Some(sym) = export_symbol(&f.sig.ident, &f.attrs) {
            self.exports.push((sym, Item::Fn(self.canon.sig(&f.sig))));
        }
        syn::visit::visit_item_fn(self, f);
    }

    fn visit_impl_item_fn(&mut self, f: &'ast syn::ImplItemFn) {
        if let Some(sym) = export_symbol(&f.sig.ident, &f.attrs) {
            self.exports.push((sym, Item::Fn(self.canon.sig(&f.sig))));
        }
        syn::visit::visit_impl_item_fn(self, f);
    }

    fn visit_item_static(&mut self, s: &'ast syn::ItemStatic) {
        if let Some(sym) = export_symbol(&s.ident, &s.attrs) {
            let mutable = matches!(s.mutability, syn::StaticMutability::Mut(_));
            self.exports.push((
                sym,
                Item::Static {
                    mutable,
                    ty: self.canon.ty(&s.ty, 0),
                },
            ));
        }
        syn::visit::visit_item_static(self, s);
    }

    fn visit_item_struct(&mut self, s: &'ast syn::ItemStruct) {
        let body = Body::Struct(self.canon.fields(&s.fields));
        self.types.push((
            s.ident.to_string(),
            self.canon.def(&s.attrs, &s.generics, body),
        ));
        syn::visit::visit_item_struct(self, s);
    }

    fn visit_item_union(&mut self, u: &'ast syn::ItemUnion) {
        let body = Body::Union(self.canon.fields(&u.fields.named));
        self.types.push((
            u.ident.to_string(),
            self.canon.def(&u.attrs, &u.generics, body),
        ));
        syn::visit::visit_item_union(self, u);
    }

    fn visit_item_enum(&mut self, e: &'ast syn::ItemEnum) {
        let variants = e
            .variants
            .iter()
            .map(|v| Variant {
                name: v.ident.to_string(),
                fields: self.canon.fields(&v.fields),
                discriminant: v.discriminant.as_ref().map(|(_, d)| int_text(d)),
            })
            .collect();
        let def = self.canon.def(&e.attrs, &e.generics, Body::Enum(variants));
        self.types.push((e.ident.to_string(), def));
        syn::visit::visit_item_enum(self, e);
    }

    fn visit_foreign_item_type(&mut self, t: &'ast syn::ForeignItemType) {
        let def = self
            .canon
            .def(&t.attrs, &syn::Generics::default(), Body::Opaque);
        self.types.push((t.ident.to_string(), def));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::layout::layout;

    const MANIFEST: &str = "[package]\nname = \"t\"\n";

    fn side_of(lib: &str) -> Side {
        let files = BTreeMap::from([("src/lib.rs".to_string(), lib.to_string())]);
        let l = layout(MANIFEST, &files).unwrap();
        side(&files, &l, &CTypeModel::host())
    }

    fn diff_of(before: &str, after: &str, api: Option<&Api>) -> AbiDiff {
        diff(&side_of(before), &side_of(after), api, CTypeModel::host())
    }

    fn sig_of(lib: &str, sym: &str) -> String {
        items(&side_of(lib).lib[sym])
    }

    #[test]
    fn param_names_mut_bindings_and_unsafe_are_not_a_change() {
        let a = r#"#[no_mangle] pub unsafe extern "C" fn f(mut heap: *mut u8) {}"#;
        let b = r#"#[no_mangle] pub extern "C" fn f(other: *mut u8) {}"#;
        assert!(diff_of(a, b, None).ok);
        let c = r#"#[no_mangle] pub extern "C" fn f(heap: *const u8) {}"#;
        assert_eq!(diff_of(a, c, None).changed.len(), 1);
    }

    #[test]
    fn c_aliases_and_crate_aliases_resolve_to_the_target_primitive() {
        let m = CTypeModel::host();
        let a = r#"pub type size_t = core::ffi::c_ulong;
                   #[no_mangle] pub extern "C" fn g(n: size_t, x: core::ffi::c_double,
                                                   c: libc::c_char) -> core::ffi::c_int { 0 }"#;
        let b = format!(
            r#"#[no_mangle] pub extern "C" fn g(n: usize, x: f64, c: {}) -> i32 {{ 0 }}"#,
            m.c_char
        );
        assert!(diff_of(a, &b, None).ok);
        let c = r#"#[no_mangle] pub extern "C" fn g(n: usize, x: f32, c: i8) -> i32 { 0 }"#;
        assert_eq!(diff_of(&b, c, None).changed.len(), 1); // f64 -> f32 is real
    }

    #[test]
    fn one_tuple_differs_from_its_element_but_trailing_commas_do_not() {
        let t = r#"#[no_mangle] pub extern "C" fn f(x: (i32,)) {}"#;
        let p = r#"#[no_mangle] pub extern "C" fn f(x: (i32)) {}"#;
        assert_ne!(sig_of(t, "f"), sig_of(p, "f"));
        assert!(!diff_of(t, p, None).ok);
        let cb1 = r#"#[no_mangle] pub extern "C" fn f(cb: Option<unsafe extern "C" fn(*const u8, *const u8,) -> i32,>) {}"#;
        let cb2 = r#"#[no_mangle] pub extern "C" fn f(cb: Option<extern "C" fn(*const u8, *const u8) -> i32>) {}"#;
        assert!(diff_of(cb1, cb2, None).ok);
    }

    #[test]
    fn export_is_no_mangle_or_export_name_not_extern_c() {
        let s = side_of(
            r#"pub extern "C" fn conv_only() {}
               #[unsafe(no_mangle)] pub extern "C" fn a() {}
               #[export_name = "b_sym"] pub extern "C" fn b() {}
               #[no_mangle] pub static mut COUNTER: core::ffi::c_int = 0;"#,
        );
        let syms: Vec<&String> = s.lib.keys().collect();
        assert_eq!(syms, ["COUNTER", "a", "b_sym"]);
        assert_eq!(items(&s.lib["COUNTER"]), "static mut i32");
    }

    #[test]
    fn dropping_extern_c_on_an_export_is_a_change() {
        let a = r#"#[no_mangle] pub extern "C" fn f() {}"#;
        let b = r#"#[no_mangle] pub fn f() {}"#;
        assert_eq!(diff_of(a, b, None).changed.len(), 1);
    }

    const BY_VALUE: &str = r#"#[repr(C)] pub struct Foo { a: i32, b: f64 }
                              #[no_mangle] pub extern "C" fn f(x: Foo) {}"#;

    #[test]
    fn field_order_type_and_repr_changes_are_caught() {
        for after in [
            r#"#[repr(C)] pub struct Foo { b: f64, a: i32 }"#,
            r#"#[repr(C)] pub struct Foo { a: i64, b: f64 }"#,
            r#"#[repr(C, packed)] pub struct Foo { a: i32, b: f64 }"#,
            r#"pub struct Foo { a: i32, b: f64 }"#,
        ] {
            let d = diff_of(
                BY_VALUE,
                &format!("{after} #[no_mangle] pub extern \"C\" fn f(x: Foo) {{}}"),
                None,
            );
            assert!(d.changed.is_empty(), "the signature itself is unchanged");
            let t = &d.types_changed[0];
            assert!(
                !d.ok && t.name == "Foo" && t.by_value && t.breaking,
                "{after}"
            );
        }
    }

    #[test]
    fn renamed_type_with_the_same_layout_keeps_the_abi() {
        // a generics recovery: hand-monomorphized list -> one generic `List<T>`
        let before = r#"#[repr(C)] pub struct list_node_int { data: i32, next: *mut list_node_int }
                        #[repr(C)] pub struct list_int_t { head: *mut list_node_int, size: usize }
                        #[no_mangle] pub extern "C" fn list_int_size(l: *mut list_int_t) -> usize { 0 }"#;
        let after = r#"#[repr(C)] pub struct ListNode<T> { data: T, next: *mut ListNode<T> }
                       #[repr(C)] pub struct List<T> { head: *mut ListNode<T>, size: usize }
                       pub type list_int_t = List<i32>;
                       #[no_mangle] pub extern "C" fn list_int_size(l: *mut list_int_t) -> usize { 0 }"#;
        let d = diff_of(before, after, None);
        assert!(
            d.ok && d.changed.is_empty() && d.types_changed.is_empty(),
            "{:?}",
            d.types_changed.iter().map(|t| &t.name).collect::<Vec<_>>()
        );
        // ...but a different element type is a change
        let wrong = after.replace("List<i32>", "List<i64>");
        let d = diff_of(before, &wrong, None);
        assert!(
            !d.ok
                && d.types_changed
                    .iter()
                    .any(|t| t.name == "list_node_int -> ListNode<i64>")
        );
    }

    #[test]
    fn int_to_repr_enum_is_reported_but_not_breaking() {
        let before =
            r#"pub type cc_stat = u32; #[no_mangle] pub extern "C" fn f() -> cc_stat { 0 }"#;
        let after = r#"#[repr(u32)] pub enum cc_stat { CC_OK = 0u32, CC_ERR = 1 }
                       #[no_mangle] pub extern "C" fn f() -> cc_stat { cc_stat::CC_OK }"#;
        let d = diff_of(before, after, None);
        assert!(d.ok && d.changed.is_empty());
        assert_eq!(
            (d.types_changed[0].kind, d.types_changed[0].breaking),
            ("int_to_enum", false)
        );
        let wide = after.replace("repr(u32)", "repr(u64)");
        assert_eq!(diff_of(before, &wide, None).changed.len(), 1);
    }

    #[test]
    fn layout_change_behind_a_pointer_breaks_only_if_the_header_exposes_it() {
        let before = r#"pub struct List { head: *mut Node } pub struct Node { next: *mut Node }
                        #[no_mangle] pub extern "C" fn len(l: *mut List) -> usize { 0 }"#;
        let after = r#"pub struct List { items: Vec<i32> }
                       #[no_mangle] pub extern "C" fn len(l: *mut List) -> usize { 0 }"#;
        let opaque = Api {
            symbols: ["len".into()].into(),
            types: BTreeSet::new(),
        };
        let d = diff_of(before, after, Some(&opaque));
        let list = d.types_changed.iter().find(|t| t.name == "List").unwrap();
        assert!(!list.by_value && !list.breaking && d.ok);

        let public = Api {
            symbols: ["len".into()].into(),
            types: ["List".into()].into(),
        };
        assert!(!diff_of(before, after, Some(&public)).ok);
        assert!(
            !diff_of(before, after, None).ok,
            "without --api, assume it is exposed"
        );
    }

    #[test]
    fn reordered_fields_report_one_change_not_a_cascade() {
        let before = r#"#[repr(C)] pub struct E { data: *mut u8, next: *mut E }
                        #[no_mangle] pub extern "C" fn f(e: *mut E) {}"#;
        let after = r#"#[repr(C)] pub struct E { next: *mut E, data: *mut u8 }
                       #[no_mangle] pub extern "C" fn f(e: *mut E) {}"#;
        let names: Vec<String> = diff_of(before, after, None)
            .types_changed
            .into_iter()
            .map(|t| t.name)
            .collect();
        assert_eq!(names, ["E"]);
    }

    #[test]
    fn forward_declarations_merge_with_the_definition() {
        let before = r#"extern "C" { pub type Foo; } pub struct Foo { a: i32 }
                        #[no_mangle] pub extern "C" fn f(x: *mut Foo) {}"#;
        let after = r#"pub struct Foo { a: i32 } #[no_mangle] pub extern "C" fn f(x: *mut Foo) {}"#;
        assert!(diff_of(before, after, None).types_changed.is_empty());
    }

    #[test]
    fn api_restricts_symbols() {
        let before =
            r#"#[no_mangle] pub extern "C" fn api() {} #[no_mangle] pub extern "C" fn helper() {}"#;
        let after = r#"#[no_mangle] pub extern "C" fn api() {} fn helper() {}"#;
        assert_eq!(diff_of(before, after, None).removed, ["helper"]);
        let api = Api::parse("symbol api\n").unwrap();
        assert!(diff_of(before, after, Some(&api)).ok);
    }

    #[test]
    fn duplicate_exports_and_unparsed_files_are_not_ok() {
        let dup = r#"#[no_mangle] pub extern "C" fn f() {} mod m { #[no_mangle] pub extern "C" fn f() {} }"#;
        assert_eq!(diff_of("", dup, None).duplicate_symbols, ["f"]);
        let d = diff_of("", "fn {{ broken", None);
        assert!(!d.ok && d.unparsed_files.after == ["src/lib.rs"]);
    }

    #[test]
    fn api_file_format() {
        let a = Api::parse("# header API\nfoo\nsymbol bar\ntype Baz\n\n").unwrap();
        assert_eq!(a.symbols, ["bar".to_string(), "foo".to_string()].into());
        assert_eq!(a.types, ["Baz".to_string()].into());
        assert!(Api::parse("fn foo bar\n").is_err());
    }
}
