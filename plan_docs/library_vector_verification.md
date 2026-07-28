# Library (cando) vector verification — design

How PROCTOR verifies **library** test cases against TRACTOR's
authoritative vectors, Falco-free. Companion to
`vector_testing_integration_plan.md` (which covered binary cases) and
`falco_integration_notes.md` (the deferred file-change path).

## 1. Why library cases are different

A **binary** case is self-contained: the harness builds the translated
project and runs its `driver` against stdin/stdout vectors. PROCTOR can
verify it in a fabricated one-case corpus (`vector_harness._assemble_corpus`).

A **library** case is **workspace-coupled**. Its verifier is a cando
`runner/` package that:

- is a **member of the corpus Cargo workspace** (the root `Cargo.toml`
  globs `*-Tests/*/*_lib/runner` and `tools/cando*`);
- depends on **`cando2`** by a relative path (`../../../../tools/cando2`);
- loads the translated project as a **cdylib** and compares **library
  state** (`lib_state_in` / `lib_state_out`), not stdout.

So the harness builds it with `cargo rustc -p _<case>_lib_runner
--release` from the **workspace root** — which means the whole workspace
(root manifest, `tools/cando2`, the case's `runner/`) must be present.
A fabricated one-case corpus can't satisfy this.

## 2. Root cause of the earlier "blocker": a version mismatch

Investigating lib support surfaced what looked like a Falco dependency,
but it was self-inflicted. Our two artifacts were pinned to **different
eras of the same repo** (`DARPA-TRACTOR-Program/Test-Corpus`):

| Artifact | Was | Lib runner name | Harness |
|---|---|---|---|
| Vendored `tools/tractor_runtests` | **0319ab0** | `_<case>_runner` | direct, Falco-free |
| Corpus submodule | **3e3b487** (newer) | `_<case>_cando_librunner` | Docker + Falco orchestrator only |

At 3e3b487 the direct harness is gone (replaced by the Falco
`tools/test_runner` orchestrator) and the runner was renamed — so our
vendored direct harness couldn't build the newer lib runners, and the
corpus's own harness needed Falco (blocked by the Docker-29 mount bug;
see `falco_integration_notes.md`).

**Fix: pin the corpus submodule to 0319ab0** — the same era as the
vendored harness. There the direct `runtests.rust` handles library cases
as plain subprocesses (no Docker/Falco/root), the runner is named
`_<case>_runner` (exactly what the harness builds), and case coverage is
identical (85 cases, 42 lib). This is the model the reference
`tractor-crat-dockerfile` used: run the direct harness over a full
corpus checkout. Falco stays deferred to V4 — it's the *newer* corpus's
approach and is only needed for file-change vectors.

## 3. The mechanism: verify in place in a workspace copy

Instead of fabricating a corpus, verify **inside a writable copy of the
real corpus workspace**:

1. **`find_workspace_root(case_dir)`** — walk up to the nearest ancestor
   with a `[workspace]` `Cargo.toml` and a `tools/cando*` crate.
2. **`copy_workspace(root, dest)`** — one writable copy per bench (skips
   `.git`, `target`, and pre-existing `translated_rust`).
3. **`run_vectors_in_place(translated_rust, case_dir, workspace_root, …)`**
   — drop the stage's Rust into the case's `translated_rust` slot inside
   the copy, then run the harness scoped to that case
   (`--root <copy> --subset <case_rel>`) and parse the JUnit.

For **per-stage** comparison, each stage's output is cycled through the
one `translated_rust` slot (drop → verify → next stage overwrites). The
harness only ever looks for a folder literally named `translated_rust`,
so cycling through that single slot is how N stages are fed to it.

`vector_compare` exposes `compare_stages_in_place` /
`verify_final_in_place`; **bench** finds the workspace once, copies it,
and routes every case through the in-place path. The isolated path
(`run_vectors` / `_assemble_corpus`) is kept only as a binary-only
fallback for corpora without a workspace, and for the corpus-free e2e
fixture test.

### Why this over the alternatives

- **Reconstruct a minimal wired workspace per case** — would re-implement
  TRACTOR's workspace layout (glob members, relative cando path,
  `Cargo.lock`) and drift. Rejected: "use their tools, don't reimplement."
- **Use the newer corpus's own harness** — that's the Docker+Falco
  orchestrator (V4), blocked by the mount bug. Deferred.

## 4. Evidence

Spike + e2e on `001_helloworld_lib`, at 0319ab0, in the framework
container and locally:

- crat translates the lib case to a **cdylib already named `hello`**,
  matching the runner's `library: "hello"` — **no `cdylib.py`-style
  rename needed** (this was the last open risk).
- The direct harness builds `cando2` + `_001_helloworld_lib_runner` and
  runs it: **`test1` passes**, Falco-free.
- `tests/e2e/test_vector_lib_e2e.py` reproduces this end-to-end (~22 s,
  builds cando2 + runner) and is green; 9 unit tests cover discovery,
  the slot-drop, and bench routing.

## 5. Operational notes & limits

- **Cost:** one ~340 MB workspace copy per bench, under the bench output
  dir (gitignored). Copying is a one-time per-run cost.
- **Parallelism:** cross-case writes are isolated (each case owns its
  slot). The shared workspace `target` used for cando-runner builds is
  serialized by Cargo's own lock — correct, but lib-runner builds don't
  parallelize. A future optimization could give each case its own
  `CARGO_TARGET_DIR`, but the harness currently expects the runner at
  `<workspace>/target/release`, so that needs harness cooperation.
- **Cleanup:** slots + build artifacts accumulate in the workspace copy;
  it lives under the (disposable) bench output dir.
- **Deferred (V4):** file-change vectors (`setup` + `file_changes`),
  which need the newer Falco orchestrator. See
  `falco_integration_notes.md`.

## 6. Full-corpus result

Whole B01_synthetic (binary **and** library) through c2rust → crat, then
verified in place against the corpus vectors — in `proctor-framework:dev`,
`verify_final`, `--jobs 16`, ~65 s:

| bucket | cases | vector-clean | pass | fail | UB-skip |
|---|---|---|---|---|---|
| binary | 43 | 43/43 | 208 | 0 | 14 |
| library (cando) | 42 | 42/42 | 185 | 0 | 12 |
| **total** | **85** | **85/85** | **393** | **0** | **26** |

**100% of run vectors pass, binary and library, Falco-free.** cando
state comparison works across the whole corpus with no translation
fixups.

One environment fix was needed and is baked into the image: the corpus
workspace's `rust-toolchain.toml` pins **`nightly-2025-11-11`** for
building the cando runners. Without it in the image, 42 parallel runner
builds each raced to auto-install it and corrupted the toolchain
("Missing manifest"); the `Dockerfile` now `rustup toolchain install`s it
up front. (Note: this corpus era's vectors differ slightly from the
newer 3e3b487 pin used for the earlier binary-only run — e.g.
`027_ctype_ascii` passes here.)

## 7. Status

- ✅ Corpus pinned to 0319ab0; in-place verification implemented and
  routed through bench; unit + e2e green; corpus's nightly baked in.
- ✅ Full binary+lib corpus run: 85/85 cases, 393/393 run vectors pass.
- ⏳ **V4 (deferred):** file-change vectors via the newer Falco
  orchestrator — see `falco_integration_notes.md`.
