# Vendored: `measure_unsafety` (TRACTOR pipeline-automation)

Unmodified copy of the unsafe-usage scorer from
**DARPA-TRACTOR-Program/pipeline-automation**, path
`evaluate_unsafe_usage/measure_unsafety` (the program's "Testing infrastructure
for TRACTOR" repo; the files state no author).

## Which tool this is — and which it is NOT

This is a **syntactic** (`syn` AST) scorer. It walks the crate's `*.rs`, counts
unsafe statements / fns / (pub) fns / blocks / impls, and reports a headline
**`unsafe_score = unsafe_statements + unsafe_impls + unsafe_other`** (+1 per
statement under an `unsafe` block/fn, +1 per `unsafe impl`, +1 per other
`unsafe` keyword use). Source-only: no toolchain, runs even if the crate
doesn't build.

**Caveat (doc vs. code).** The in-code comment in `src/stats.rs` describes a
"+1 per `pub`/trait-impl `unsafe fn`" rule that is **not** implemented — the
formula above (read from the actual code) is authoritative, and `pub_fns` is
computed but never added to the score. Also, a redundant `unsafe { }` block
nested inside an `unsafe fn` is counted both as the block statement and for its
inner statements, so `unsafe_score` skews high on heavily-nested C2Rust/CRAT
output. Both are properties of the upstream tool (do not modify); treat the
score as a *relative* syntactic signal, not an exact count.

Per `plan_docs/unsafe_evaluation_plan.md` §3, this is the **syn / "cheapest
signal, no compile"** tier. It is deliberately NOT:

- the plan's **suggested default** — Galois `Tractor-Crisp/tools/find_unsafe2`
  (a **semantic** MIR scorer of unsafe *operations*, run as a cargo subcommand);
- Galois `find_unsafe` (a richer syn scorer than this one);
- the **semantic `UnsafeOpKind`** measure the corpus is scored on.

**Therefore the numbers this tool reports are syntactic `unsafe_score`, not
semantic unsafe-operation counts.** They track the same direction and need no
build, which is why they're wired into `metrics`/`bench` today; but the
semantic per-stage signal (Galois `find_unsafe2`, or DARPA
`evaluate_unsafe_usage`'s UnsafeOpKind mode) is the upgrade path and remains
un-wired. Note also: the plan's §2 survey assumed this DARPA tool was
*semantic*; on inspection it is *syntactic* (see the doc's status header).

## Invocation note

Upstream also ships `unsafety.Dockerfile` + `invoke_unsafety.py` (a pinned-rustc
Docker wrapper). We build and run the `measure_unsafety` binary **directly**;
since it is a source parser, the rustc pin does not change the score.

- Build (stable rust): `cargo build --release` -> `target/release/measure_unsafety`
  (built on demand by `proctor.testing.unsafe_eval`).
- Run: `measure_unsafety <project_root>` -> JSON stats on stdout.

Driven via `proctor.testing.unsafe_eval`. License: as in the source repo.
Do not modify — re-vendor from upstream to update.
