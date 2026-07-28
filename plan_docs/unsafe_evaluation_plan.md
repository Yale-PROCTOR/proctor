# Evaluating `unsafe` usage after each translation stage — plan

Measure how much `unsafe` a translation carries, **after every
Rust-producing stage** (c2rust → crat → abstraction_recovery → …), so we
can track the reduction each stage achieves — the safety analogue of the
per-stage vector-correctness delta. c2rust output is maximally unsafe
(raw pointer translation); crat and abstraction recovery should each
drive the count down.

## 1. What "unsafe usage" should mean

Not `unsafe` *keywords* or *blocks* (text/syn counting is imprecise and
drifts from the language). The authoritative definition, used by all of
TRACTOR's own tooling, is **rustc's set of operations that require an
unsafe context** — the `UnsafeOpKind` enum from
`rustc_mir_build`'s `check_unsafety`:

`CallToUnsafeFunction`, `UseOfInlineAssembly`, `InitializingTypeWith`,
`UseOfMutableStatic`, `UseOfExternStatic`, `DerefOfRawPointer`,
`AccessToUnionField`, `MutationOf/BorrowOfLayoutConstrainedField`,
`CallToFunctionWith`, `UnsafeBinderCast`, … (it deliberately does **not**
count unsafe attrs/traits/impls). Counting these ops is stable and
matches how the corpus is scored.

## 2. The three tools (surveyed)

**Two definitions are in play** — pick deliberately:

- **Semantic (rustc `UnsafeOpKind`)** — the *operations* that require an
  unsafe context. This is what the corpus is scored on. Precise, but
  needs the crate to compile under a (patched) rustc.
- **Syntactic (syn AST)** — counts of unsafe *syntax* (`unsafe` blocks,
  fns, impls, traits). Cheap (parse only, no build), but a coarser proxy.

| tool | definition | mechanism | fit |
|---|---|---|---|
| **`pipeline-automation/evaluate_unsafe_usage`** (DARPA) | semantic `UnsafeOpKind` | `unsafety.Dockerfile` builds an image (specific rustc + entrypoint); `invoke_unsafety.py <rust_project> out.json` | **authoritative** (corpus scoring), needs Docker + a compile |
| **`Test-Corpus/tools/rust_eval/unsafe_ops`** | semantic `UnsafeOpKind` | rustc **patch** — `requires_unsafe` prints `TRACTOR_UNSAFE_OPS {kind,span}` (env-gated) | the engine the DARPA harness is built on |
| **`crat/crates/finders/src/unsafe_finder.rs`** | semantic `UnsafeOpKind` | same `check_unsafety` via crat's rustc driver | same analysis, but only crat's stage/toolchain |
| **Yale `automation/measurements/measure_unsafety`** | **syntactic** (syn) | Rust program: `walkdir` over `src/*.rs`, `syn` visitor counts unsafe syntax | **lightweight**, no build/compiler, pairs with `measure_idiomaticity` |
| **Galois `Tractor-Crisp/tools/find_unsafe`** | **syntactic** (syn) | `syn` visitor over `ExprUnsafe` / unsafe fns / statics / impls / traits / macros | lightweight, **more comprehensive** syntax coverage than Yale's |
| **Galois `Tractor-Crisp/tools/find_unsafe2`** | **semantic** (MIR) | `rustc_public::mir` visitor run as a **cargo subcommand** | semantic like `UnsafeOpKind` but **no patched rustc** — just a compile |

Two definitions, several tools each:
- **Semantic** (rustc, actual unsafe operations): DARPA
  `evaluate_unsafe_usage` and the corpus `unsafe_ops` patch are the
  authoritative corpus-scoring engine; crat's `unsafe_finder` is the same
  embedded in crat; **Galois `find_unsafe2`** gets the same class of
  answer via the stable-MIR API as a cargo subcommand (no patched-rustc
  Docker build — the practical semantic option).
- **Syntactic** (syn AST, unsafe syntax): Yale `measure_unsafety` (simple)
  and Galois `find_unsafe` (richer — also counts unsafe impls/traits/
  macros that the semantic `UnsafeOpKind` deliberately ignores).

## 3. Recommendation

Don't reimplement — drive existing tools. Three tiers, pick per need:

- **Every-run per-stage semantic signal → Galois `find_unsafe2`.** MIR
  (`rustc_public`) so it counts real unsafe *operations*, but it's a
  plain **cargo subcommand** — no patched-rustc Docker build, just needs
  the crate to compile. The most practical semantic option for wiring
  into every bench.
- **Authoritative / corpus-scored check → DARPA `evaluate_unsafe_usage`.**
  The performer-facing harness ("performers are not expected to modify
  them"), reporting the exact `UnsafeOpKind` the corpus is scored on. Run
  it at milestones to confirm `find_unsafe2` agrees; cost is the
  patched-rustc Docker build.
- **Cheapest signal, no compile → syn tools** (Yale `measure_unsafety`,
  Galois `find_unsafe`). Source-only, so they run even if the crate
  doesn't build, and Yale's pairs with `measure_idiomaticity` in one
  suite. Coarser (syntax, not operations).

**Suggested default:** wire **`find_unsafe2`** into bench for the
per-stage semantic delta, cross-check against DARPA
`evaluate_unsafe_usage` at milestones, and keep a syn tool as a
build-independent fallback. crat's `unsafe_finder` is an extra
crat-stage cross-check.

- **Do not** hand-roll unsafe counting — these cover both the syntactic
  and semantic definitions already.

## 4. Integration design (mirrors the vector harness)

Add `proctor/testing/unsafe_eval.py`, analogous to `vector_harness.py`:

- `measure_unsafe(rust_project, *, workdir) -> UnsafeReport` — run the
  `tractor/unsafety` image on the stage's `out/rust`, read `out.json`,
  return total ops + a per-`UnsafeOpKind` breakdown.
- `UnsafeReport` with `.total`, `.by_kind`, and a `.summary()`.
- A per-stage comparison (like `VectorComparison`): run it on **each**
  stage's `out/rust` and assemble an **unsafe-reduction delta table**
  (c2rust N → crat M → abstraction_recovery K).

Wire it into `bench` behind a `[bench] measure_unsafe = true` flag, next
to `verify_vectors`, so one bench run reports both **correctness**
(vectors) and **safety** (unsafe ops) per stage. Store the counts in
`bench.json` alongside the vector results.

## 5. Open questions / caveats

- **Toolchain match.** The image installs "a specific version of the
  Rust compiler." It must be able to *compile* the stage output —
  c2rust/crat output uses nightly features (`c_variadic`, `extern_types`,
  …). Confirm the harness's rustc accepts them (it is built to compile
  corpus translations, so it should); if not, that's a report-to-TRACTOR
  issue, not a reimplement.
- **Docker-in-Docker.** `evaluate_unsafe_usage` is itself a Docker image.
  Our pipeline already runs in a container, so measuring in-run means
  nested Docker (or running the measurement as a separate host-side pass
  over the run's `stages/*/out/rust`). Simplest first cut: a **host-side
  pass** after a bench (like the manual `verify_stages.py`), then wire it
  into bench once the DinD story is settled.
- **Build the image once.** `docker build -t tractor/unsafety -f
  unsafety.Dockerfile .` (builds a patched rustc — slow, cached).
- **Normalization.** Raw op counts scale with program size; also report
  ops **per stage relative to c2rust** (reduction %) so cases are
  comparable, and keep the per-kind breakdown (e.g. `DerefOfRawPointer`
  is the interesting one for abstraction recovery).

## 6. Milestones

| # | Contents |
|---|---|
| U1 | Build `tractor/unsafety`; `unsafe_eval.py` drives `invoke_unsafety.py` on one stage output; parse `out.json`; unit-test the parser |
| U2 | Per-stage comparison + host-side pass over a run's `stages/*/out/rust`; unsafe-reduction delta table |
| U3 | Wire into `bench` (`[bench] measure_unsafe`), report unsafe + vectors together in `bench.json`; run over a corpus and report per-stage reduction |
| U4 | (optional) cross-check crat's stage against `crat`'s own `unsafe_finder` |

## 7. Why this pairs with vector testing

Together they give the two axes TRACTOR cares about per stage:
**correctness** (does it still pass the vectors?) and **safety** (how
much `unsafe` remains?). A stage is only a win if it reduces unsafe
**without** breaking vectors — this makes that measurable per stage.

## 8. Tools (links)

Semantic (rustc / MIR — actual unsafe operations):

- DARPA `evaluate_unsafe_usage` — https://github.com/DARPA-TRACTOR-Program/pipeline-automation/tree/main/evaluate_unsafe_usage
- corpus `unsafe_ops` (rustc patch) — https://github.com/DARPA-TRACTOR-Program/Test-Corpus/tree/main/tools/rust_eval/unsafe_ops
- crat `unsafe_finder.rs` — https://github.com/Yale-PROCTOR/crat/blob/master/crates/finders/src/unsafe_finder.rs
- Galois `find_unsafe2` (stable-MIR cargo subcommand) — https://github.com/GaloisInc/Tractor-Crisp/tree/main/tools/find_unsafe2

Syntactic (syn AST — unsafe syntax):

- Yale `measure_unsafety` — https://github.com/Yale-PROCTOR/proctor/tree/automation/automation/measurements/measure_unsafety
- Galois `find_unsafe` — https://github.com/GaloisInc/Tractor-Crisp/tree/main/tools/find_unsafe

Related: idiomaticity — `idiomaticity_evaluation_plan.md`.
