# Vendored: TRACTOR `runtests.rust` (direct harness)

Unmodified copy of the TRACTOR test runner from
**DARPA-TRACTOR-Program/Test-Corpus**, path
`deployment/scripts/github-actions/runtests`, at commit **0319ab0**.

This is the pre-Falco harness: it runs test vectors by executing the
translated `driver` (binary cases) or the case's cando `runner` binary
(library cases) directly as subprocesses — no Docker, no Falco, no root.
Pure Python stdlib.

We drive it via `proctor.testing.vector_harness` to verify translations
against the corpus test vectors at each pipeline stage. See
`plan_docs/vector_testing_integration_plan.md`. The newer Falco-based
`tools/test_runner` orchestrator is deferred — see
`plan_docs/falco_integration_notes.md`.

License: MIT (© Massachusetts Institute of Technology), as in the source.
Do not modify — to update, re-vendor from the corpus at a chosen commit.
