# Evaluating idiomaticity after each translation stage — plan

Measure how *idiomatic* the Rust is **after every Rust-producing stage**
(c2rust → crat → abstraction_recovery → …), the third axis alongside
**correctness** (vectors) and **safety** (unsafe ops). c2rust output is
maximally non-idiomatic (raw-pointer, C-shaped); crat and abstraction
recovery should each push it toward idiomatic Rust.

## 1. The existing tool (use it)

Yale already has it, on the `automation` branch:
`automation/measurements/measure_idiomaticity/`.

- **`measure_idiomaticity.py`** runs `cargo clippy --message-format json
  --workspace --all-targets -- -W clippy::all -D clippy::correctness`,
  parses the JSON diagnostics, and buckets each lint by its **clippy
  group** (style / complexity / perf / pedantic / nursery / …). Output
  `idiomaticity.json`: `{clippy: {group: {lint: count}}, rustc: {…},
  cyclomatic_complexity_counts: {complexity: count}}`.
- **`clippy_lint_map.py`** builds the lint→group map (scraped from the
  clippy docs, cached to JSON).
- **`clippy.toml`** sets `cognitive-complexity-threshold = 0` so clippy
  reports every function's cognitive complexity (a distribution, not
  just a pass/fail).

So idiomaticity = **clippy lint density by group + cognitive-complexity
distribution.** Lower counts (especially in the style/complexity/idiom
groups) = more idiomatic. This is the right engine — clippy *is* the
Rust idiom authority; don't reimplement it. Recommendation: **drive this
tool**, same as we drive TRACTOR's harness for vectors.

Note: the same `automation/measurements/` folder also ships
`measure_unsafety/` (a Rust program) — cross-reference for
`unsafe_evaluation_plan.md`; we should reconcile whether to use Yale's
`measure_unsafety` or DARPA's `evaluate_unsafe_usage` (both measure the
same rustc `UnsafeOpKind`).

## 2. What we can improve

The tool measures **one** project and emits **raw counts**. For per-stage
pipeline evaluation, add:

1. **Per-stage delta + a headline score.** Run it after each stage and
   report the reduction (c2rust → crat → absrec). Raw counts scale with
   program size, so also report **normalized** figures: lints per KLOC
   and/or **% reduction vs the c2rust baseline**. A single idiomaticity
   score (e.g. weighted lint density) makes stages comparable at a glance
   while keeping the per-group breakdown.
2. **Weight/curate the groups.** Not all lints speak to "idiomaticity":
   weight the idiom-bearing groups (style, complexity, pedantic) above
   perf/correctness, and **exclude unsafe-related lints** so idiomaticity
   and the unsafe metric don't double-count the same thing.
3. **Toolchain robustness.** Clippy must exist for the crate's *pinned*
   toolchain — c2rust/crat output carries its own `rust-toolchain` (a
   nightly). The current script does `rustup component add clippy` on the
   default toolchain; it should add clippy for the crate's toolchain and
   handle a crate that doesn't fully build under clippy (partial results
   rather than zero).
4. **Offline lint map.** `clippy_lint_map.py` fetches from the clippy
   website; an in-container/hermetic run must use the **cached** JSON (no
   network). Ship the cached map with the stage.
5. **Cognitive complexity as a first-class number.** Summarize the
   `cyclomatic_complexity_counts` distribution (mean/max/histogram) — a
   clean single signal of how "un-C-shaped" the code is getting.

## 3. Integration (mirrors the vector / unsafe harnesses)

Add `proctor/testing/idiomaticity_eval.py`, analogous to
`vector_harness.py` / `unsafe_eval.py`:

- `measure_idiomaticity(rust_project, *, workdir) -> IdiomReport` — drive
  `measure_idiomaticity.py` on the stage's `out/rust`, read
  `idiomaticity.json`, return grouped counts + complexity summary +
  normalized score.
- A per-stage comparison (like `VectorComparison`): run on **each**
  stage's `out/rust`, assemble an **idiomaticity-improvement delta
  table**.
- Wire into `bench` behind `[bench] measure_idiomaticity = true`, next to
  `verify_vectors` and `measure_unsafe`, so one bench run reports all
  three axes per stage into `bench.json`.

## 4. Caveats

- Clippy needs the crate to compile; run it after a successful build.
  Give it the crate's toolchain (see improvement 3).
- Version pinning: clippy lints change across Rust versions — pin the
  clippy/toolchain used for measurement so scores are comparable across
  runs, and record it in the report.

## 5. Milestones

| # | Contents |
|---|---|
| I1 | `idiomaticity_eval.py` drives `measure_idiomaticity.py` on one stage output; parse `idiomaticity.json`; unit-test the parser; ship the cached lint map + per-crate clippy toolchain handling |
| I2 | Per-stage comparison + host-side pass over a run's `stages/*/out/rust`; normalized score + delta table |
| I3 | Wire into `bench` (`[bench] measure_idiomaticity`); report idiomaticity + unsafe + vectors together; corpus run + per-stage reduction |

## 6. The three axes, together

A translation stage is only a genuine win if it **improves idiomaticity
and reduces unsafe without breaking the vectors.** Vectors (the vector
harness, `proctor/testing/`; deferred work in
`falco_integration_notes.md`), unsafe (`unsafe_evaluation_plan.md`), and
idiomaticity (this doc) are the same per-stage pattern — drive the
authoritative tool, compare across stages —
and should share the bench plumbing and `bench.json` schema.

## 7. Tools (links)

- Yale `measure_idiomaticity` (clippy-based; the tool to drive) —
  https://github.com/Yale-PROCTOR/proctor/tree/automation/automation/measurements/measure_idiomaticity
- clippy lint groups (lint→group source) —
  https://rust-lang.github.io/rust-clippy/master/index.html
- Adapted from crat's agent `clippy.py` (same repo, `agent/src/agent/`).

Unsafe/safety tools are catalogued in `unsafe_evaluation_plan.md`
(`measure_unsafety` lives in the same Yale `automation/measurements`
suite as `measure_idiomaticity`).
