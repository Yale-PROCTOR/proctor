# Original: `diff_metrics`

Written for PROCTOR, not vendored. `tools/measure_unsafety` and
`tools/measure_idiomaticity` are third-party tools we keep unmodified; this
crate is ours and can be edited freely.

## What it is

PROCTOR runs a pipeline of stages (c2rust, CRAT, abstraction recovery, …), and
each stage rewrites a Rust crate. To compare stages fairly, every stage must be
measured the same way, independent of what the stage reports about itself.
`diff_metrics` does that. It takes a stage's input crate (the previous stage's
`out/rust`) and its output crate, and reports two things: how much code the
stage changed, and whether the stage kept the library's C ABI.

The tool reads source code only. It does not build the crate, resolve names or
infer types, so it also works on a crate that does not compile.

If it cannot read an input, it stops with an error message and exit code 2. It
never reports "nothing changed" because a directory, a file, `Cargo.toml` or
the `--api` file was missing or unreadable.

The JSON output (`schema_version: 3`) contains:

- **totals**: `files_changed`, `lines_added`, `lines_removed`, `touched_files`;
- **files**: for each changed file, its `status`, `lines_added`,
  `lines_removed` and `rustfmt_ok`;
- **targets**: for each crate, its Cargo targets and the `unreachable_files`
  that no target compiles;
- **abi**: exported symbols that were `removed`, `added` or `changed`,
  `removed_bin_internal`, `types_changed`, `duplicate_symbols`,
  `unparsed_files`, the `c_type_model` used, and an overall `ok`.

## How lines are counted (`src/churn.rs`)

We want to know how much code a stage actually rewrote. A stage that only
reformats code or moves functions around has not rewritten anything, so that
should not count as change.

The tool first runs `rustfmt` on both versions of each file whose text differs,
so formatting differences disappear. It then strips leading and trailing
whitespace from every line and drops blank lines. Finally it compares the two
versions as unordered collections of lines:

- `lines_added` is the number of lines in the new version that the old version
  does not have;
- `lines_removed` is the number of lines in the old version that the new
  version does not have.

Because order is ignored, moving a line is not counted as a change. This is
called churn. It is not an edit distance like `git diff`, which would count a
moved line once as removed and once as added.

A file whose lines are all the same but in a different order gets the status
`reordered`. It counts towards `files_changed`, with 0 lines added and 0
removed. The other statuses are `modified`, `added` and `removed`.

If `rustfmt` is not installed or fails on a file, the tool compares the raw
text instead and marks that file with `rustfmt_ok: false`. `--no-rustfmt`
always compares raw text.

## Which files belong to which target (`src/layout.rs`)

A crate can build several artifacts, which Cargo calls targets: one library and
any number of binaries. The library's exported symbols are the ABI that C code
links against. A binary's exported symbols are not anyone's API. So when a
stage stops exporting a symbol, it is a break only if the library exported it.
A file that no target compiles exports nothing at all. The tool therefore needs
to know which targets compile each file.

It finds this the way Cargo and rustc do:

1. It reads each target's root file from `Cargo.toml`: the `path` of `[lib]`
   and of each `[[bin]]`. Without an explicit path it uses Cargo's defaults:
   `src/lib.rs` for the library, and `src/main.rs` and `src/bin/*.rs` for
   binaries (unless `autobins = false`).
2. From each root file it follows the `mod` declarations to every file that
   target compiles. `mod x;` loads `x.rs` or `x/mod.rs`. Inline
   `mod x { ... }` blocks and `#[path = "..."]` attributes are followed too.
3. Every `.rs` file that no target reaches is listed in `unreachable_files`,
   and its exports are ignored.

This means a library file that happens to contain a `fn main`, such as c2rust's
`src/main.rs`, is still library code. A binary's helper file without `main` is
still binary code.

The tool does not evaluate `#[cfg(...)]`; it follows every `mod` declaration.
It also does not follow `include!`.

## How the ABI is compared (`src/abi.rs`)

C code calls the Rust library through the symbols it exports. A stage keeps the
ABI only if C code compiled against the original header still works with the
new library. The tool checks three things, in order: which symbols are
exported, the signature of each exported symbol, and the layout of the types
those signatures use.

### Exported symbols

C code can only call what the library exports. If a stage stops exporting a
function that C code calls, the program no longer links. So the tool first
finds every exported symbol in both crates and compares the two sets.

A function or static is exported when it has `#[no_mangle]` or
`#[export_name = "name"]` (or the `#[unsafe(no_mangle)]` form). The tool tracks
each export by its symbol name, which is the function's own name for
`#[no_mangle]` and the given name for `#[export_name]`.

`extern "C"` on its own does not export anything. It only sets the calling
convention. The tool records it as part of the signature, so a stage that
removes `extern "C"` from an exported function changes that function's
signature.

Only the library's exports are ABI. If a binary target stops exporting a
symbol, the tool lists it under `removed_bin_internal` and does not count it as
a break.

### Signatures

C code calls an exported function exactly as the header declares it. If a
stage changes a parameter or return type, C still passes the old types, and the
function reads the wrong values. So for every symbol both crates export, the
tool compares the signatures.

For each exported function the tool compares the calling convention, the
parameter types, whether it is variadic, and the return type. For an exported
static it compares the static's type and whether it is `mut`.

These differences do not change the ABI, so the tool ignores them:

- parameter names and patterns: `fn f(mut x: i32)` and `fn f(y: i32)` match;
- `unsafe`: it is a promise inside Rust, not part of the call;
- lifetimes, and `-> ()` versus no return type;
- spacing and trailing commas.

The tool compares parsed types, not text. So `(T,)`, a one-element tuple, is
still different from `(T)`, which is just `T`.

Two kinds of type names are resolved before comparing:

- **Type aliases in the crate.** `type size_t = c_ulong;` is replaced by
  `c_ulong`. Only aliases without generic parameters are expanded, and only when
  the crate defines the name one way.
- **C type names.** `size_t`, `c_int`, `c_long`, `c_char`, `int32_t` and similar
  names, as well as `usize` and `isize`, are replaced by the fixed-size Rust
  integer they stand for. So a stage that rewrites `c_int` as `i32` does not
  change the signature.

Some C types have a different size on different machines. `c_long` is 64 bits on
Linux x86-64 and 32 bits on Windows, and `c_char` is signed on x86-64 Linux but
unsigned on ARM Linux. The tool uses the sizes of the machine it was compiled
on, which is the machine that builds and tests the crate. The JSON output lists
the sizes it used under `abi.c_type_model`.

### Type layouts

A function's signature can stay the same while the struct it takes changes.
C code reads a struct's fields at fixed byte offsets, so swapping two fields
breaks it even though the signature looks identical. The tool therefore also
compares every crate type that an exported signature uses, either directly or
through fields, pointers, arrays and generic arguments.

C only sees bytes, not Rust type names. So the tool compares two types by their
layout:

- the `#[repr(...)]` attributes;
- for a struct or union, the field names, their order and their types;
- for an enum, the variants, their fields and their discriminant values.

Generic arguments are filled in before comparing. A stage that replaces
`struct list_int_t { head: *mut node_int, len: usize }` with a generic
`List<i32>` that has the same fields therefore keeps the ABI. Recursive types
such as `struct Node { next: *mut Node }` are handled by remembering which pairs
of types have already been compared.

Each type whose layout differs is listed under `abi.types_changed`. If the stage
also renamed the type, the entry is named `old -> new`.

Two special cases:

- **Integer to enum.** CRAT sometimes turns a `u32` status code into a
  `#[repr(u32)]` enum with no fields. C passes both the same way, so this is
  reported with kind `int_to_enum` and is not a break. The Rust side does now
  assume C only passes values the enum lists; the tests check that, this tool
  does not.
- **Forward declarations.** c2rust emits `extern "C" { type T; }` for a struct
  it only sees declared, next to the full `struct T` from another file. The tool
  treats the declaration and the definition as one type.

### When a layout change is a break

C code can only depend on a layout it can see. A layout change is a break when:

- the type is passed or returned by value, or is a field of such a type; or
- the C header defines the type's body, so C code may read its fields. You tell
  the tool this with a `type NAME` line in the `--api` file; or
- no `--api` file was given, so the tool cannot tell and assumes the worst.

A type that C only handles through a pointer, and that the header leaves
opaque (`typedef struct list list;`), can change freely. Abstraction recovery
relies on this, for example to replace a linked list's fields with a `Vec`.
The tool reports such a change but does not count it as a break.

`abi.ok` is false when an exported library symbol was removed or its signature
changed, a layout change is a break, the library exports the same symbol twice,
or a file the crate compiles did not parse.

### The `--api` file

c2rust exports every C function that is not `static`, including helpers the
header never declares. A later stage such as CRAT makes those helpers private,
which is not a break. The `--api` file limits the check to the real public API.
It has one entry per line:

- `symbol NAME` (or just `NAME`): an exported function or global the header
  declares;
- `type NAME`: a type whose body the header defines.

Blank lines and lines starting with `#` are ignored.

### Limits

The tool reads source code only. It does not evaluate `#[cfg]`, does not
resolve paths (it identifies a type by its last path segment, so two types with
the same name in different modules are treated as one), does not compute sizes
or alignment, and does not look at what a function does with its arguments. It
reports changes that may break the ABI. It does not prove that the ABI is
compatible.

## Build and run

Python callers (`proctor.testing.diff_eval`) build the tool automatically the
first time they need it. To build it by hand, run `cargo build --release`; the
binary is `target/release/diff_metrics`. It depends on the same crates as
`measure_unsafety` plus `toml`, which the first build may download.

Run it as:

    diff_metrics --before <crate dir> --after <crate dir> [--api <file>] [--no-rustfmt]

It prints the JSON report on stdout. On an error it prints a message on stderr
and exits with code 2.

`cargo test` runs the unit tests. They cover input errors, line counting
(including `reordered` and the rustfmt fallback), finding targets and following
the module tree (including c2rust's layout), detecting exports, comparing
signatures (aliases, one-element tuples, trailing commas, `unsafe`), comparing
layouts (field order, field type and `#[repr]` changes, renamed generic types,
`int_to_enum`, opaque and public types behind pointers, forward declarations),
and reading the `--api` file.
