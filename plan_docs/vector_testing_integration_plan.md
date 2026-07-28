# Vector-Testing Integration Plan

How the orchestration framework verifies translations against the TRACTOR
test vectors, at **each pipeline stage**, by **driving TRACTOR's own
authoritative harness** — never reimplementing their comparison logic.

Supersedes the earlier converter approach in
`test_vector_integration_plan.md` (which reimplemented binary-vector
comparison in Python). See §"Why not our own converter" below.

## 1. Principle

TRACTOR ships the authoritative test runner (`runtests`). We **use their
tool as given** and only feed it our translated output. If their tool has
a limitation on our host, we **report it to TRACTOR and discuss**, rather
than reimplement or hack around it.

## 2. Two harness versions — use the direct one now, the Falco one later

The corpus's `runtests` runner exists in two forms across its history:

| | Old (`runtests.rust`, e.g. commit `0319ab0`) | New (`runtests.orchestrator`) |
|---|---|---|
| Execution | **direct subprocess** — runs `driver` / the cando `runner` binary on the host | each vector in its own **Docker container** |
| Falco | none | **required** (filesystem-change monitoring) |
| Deps | **pure Python stdlib** + cargo/cmake/ninja | Docker + Falco + Nix + privileged caps |
| Vector coverage | binary + library (stdout/stderr/rc, cando lib-state) | + file-change vectors |
| Authoritative? | yes (library goes through real cando) | yes |

**Decision: integrate the OLD `runtests.rust` now.** It is authoritative
(library vectors use the case's real cando runner), covers binary +
library vectors, needs no Docker/Falco/root, and — verified in the spike —
runs against the **current** corpus's cases unchanged (layout and vector
format are compatible). File-change vectors are deferred with the whole
Falco flow (see `falco_integration_notes.md`).

### Spike result (proven)

`python3 -m runtests.rust --root <corpus> --subset
Public-Tests/B01_synthetic/001_helloworld --junit-xml out.xml` against our
`c2rust → crat` translation:

```
Test Vectors Passed: 3   Failed: 0   (build + test1 + test2 + test3)
```

~1 s, fully rootless, no Docker/Falco/`/nix`.

## 3. Architecture

The framework **produces** translated Rust at every stage; the harness
**verifies** it. Nothing about the harness changes per stage — we point it
at whichever stage output we want scored.

```
c2rust ─▶ crat ─▶ abstraction_recovery ─▶ discipline_repair ─▶ ...
            │            │
            ▼            ▼         each stage's out/rust is fed to the harness
      runtests.rust  runtests.rust
            │            │
            ▼            ▼
     JUnit(crat)   JUnit(abs_rec)  ──▶ per-stage, per-vector comparison
```

### 3.1 The harness as a framework component

`proctor/testing/vector_harness.py` — a thin driver that, given a
translated Rust project + the case's `test_vectors/` (and `runner/` for
library cases):

1. Assembles a **temporary corpus-shaped dir**:
   `<tmp>/Public-Tests/<Bxx>/<case>/{test_case, test_vectors, runner?, translated_rust}`
   where `translated_rust` = the stage's `out/rust` output. (`test_case`,
   `test_vectors`, `runner` are copied/linked from the source corpus case.)
2. Invokes the vendored harness:
   `python3 -m runtests.rust --root <tmp> --subset <rel> --junit-xml <out> --keep-going`
   with cargo/cmake/ninja on PATH.
3. Parses the JUnit XML into a typed `VectorReport`
   (per-vector PASS/FAIL/SKIP, build ok, messages).

### 3.2 Vendoring the harness

The old `runtests.rust` is pure stdlib and small. Options (decide at
implementation):

- **(a) Pin the harness as a submodule / vendored copy** at the last
  pre-Falco commit, carried under `tools/` — matches the "use their tool"
  principle and is version-pinned/reproducible. *(recommended)*
- (b) Extract it at run time from the corpus submodule's git history.

Either way it is **their code, unmodified**, not a reimplementation.

### 3.3 Where verification plugs in

Two surfaces, mirroring what already exists for the (now-removed) converter:

- **Per-stage gate** (`[stages.<id>] gate_tests` / `[testing]
  after_each_stage`): after a stage produces `rust_project`, run the
  harness on it; a vector failure gates the pipeline (configurable).
- **`proctor bench` reporting**: for each corpus case, run the harness on
  the **final** output (and optionally after each stage) and record
  per-stage per-vector results in `bench.json` for comparison.

### 3.4 Per-stage comparison (the goal)

Because the harness is stateless per `translated_rust`, run it after each
transformation stage and diff the reports:

```
case                    crat    abs_recovery   discipline_repair
B01/003_string_slicing  3/3     2/3 (-1)       3/3 (+1)
```

Recorded per stage in the run/bench record; a small reporter emits the
delta table (which stage fixed/broke which vectors).

## 4. Requirements

- Host (or the framework container) needs **cargo, cmake, ninja** on PATH.
  The framework image already installs all three.
- No Docker/Falco/root for binary + library vectors.
- The source corpus (`tractor-test-corpus/Test-Corpus`) provides
  `test_case/`, `test_vectors/`, and `runner/` per case.

## 5. Scope

**In scope now:** binary vectors (argv/stdin → stdout/stderr/rc, exact +
regex, `has_ub` skip) and library vectors (cando lib-state via the case's
`runner/` crate).

**Deferred (see `falco_integration_notes.md`):** file-change vectors
(`setup` + `file_changes.tar.gz`), which need the new Falco orchestrator.
These are the minority; the harness reports them as SKIP for now.

## 6. Milestones

| # | Contents | Est | Status |
|---|---|---|---|
| V1 | Vendor/pin `runtests.rust`; `vector_harness.py` (assemble corpus dir, invoke, parse JUnit); unit tests on the parser | ~1 d | ✅ done |
| V2 | Wire into `bench` reporting; per-stage comparison; e2e on a real crat output | ~1 d | ✅ done |
| V3 | Corpus-scale `bench` with vector verification over B01_synthetic; report pass rates + per-stage deltas | ~0.5 d | ✅ done |
| V4 (later) | Swap engine to `nix run ./tools/test_runner` for file-change vectors when Falco is unblocked (`falco_integration_notes.md`) | — | deferred |

### V3 result (in-container, `configs/b01_bench_vectors.toml`)

Ran c2rust → crat over the **43 binary** cases of B01_synthetic inside
`proctor-framework:dev`, then verified each against the corpus vectors
with the vendored harness. `--jobs 16`, ~29 s wall.

- **43/43** cases translated (both stages ok).
- **crat vectors: 207 pass, 1 fail, 14 skipped (`has_ub`)** → 207/208 =
  **99.5%** over run vectors; **42/43** cases fully vector-clean.
- Per-stage delta (`verify_all_stages`): the c2rust stage's output is a
  library crate with **no runnable `driver`** (0 runnable vectors); the
  runnable binary and passing vectors first appear at crat's `bin` pass.
- The one real failure — `027_ctype_ascii` test16 — is a genuine
  translation-fidelity gap (stdin `\r`, ctype classification stdout
  mismatch), surfaced only by running the authoritative vectors.

**Not yet covered:** the 42 `_lib` (cando) cases. TRACTOR's library
runner builds `cando` from the corpus *workspace* root; the per-case
assembled corpus doesn't carry it, so library cases can't be verified in
isolation. Wiring cando (mount/build the corpus workspace, or run inside
the corpus checkout) is the next step alongside V4's Falco engine swap.

## 7. Why not our own converter

The earlier `test_vector_integration_plan.md` reimplemented binary-vector
comparison in Python (`vector_runner.py`). Two problems drove the switch
to driving TRACTOR's harness directly:

1. **Regex-flavor drift** — Python `re` ≠ Rust `regex` crate (lookaround,
   backrefs, char-class differences); an `is_regex` vector could pass ours
   and fail theirs.
2. **File-change vectors** — never covered by the reimplementation.

Using their runner eliminates both by construction, and honors the
"don't reimplement their tools" rule.

## 8. Open decisions

- Harness vendoring: submodule-pin (recommended) vs. history-extract (§3.2).
- Run the harness after **every** stage (full comparison, more build/run
  cost) vs. only after selected stages (config-driven).
- Corpus staging by copy vs. symlink of `test_case`/`test_vectors`/`runner`
  (copy is safe; symlink is cheaper — the harness only reads them).
