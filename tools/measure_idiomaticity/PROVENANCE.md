# Vendored: Yale `measure_idiomaticity`

Copy of the idiomaticity measurement from **Yale-PROCTOR/proctor**, branch
`automation`, path `automation/measurements/measure_idiomaticity`.

Drives `cargo clippy --message-format json --workspace --all-targets` and
buckets each lint by its **clippy group** (style / complexity / perf /
pedantic / …) using `.cache/clippy_lint_to_group.json`. Output
`idiomaticity.json`:
`{clippy: {group: {lint: count}}, rustc: {…}, cyclomatic_complexity_counts: {…}}`.

Driven via `proctor.testing.idiomaticity_eval`. Needs `cargo` + `clippy` for
the crate's own toolchain, and the crate must build (clippy compiles it).

Vendored change: `clippy_lint_map.py` imports `requests`/`bs4` **lazily** (only
needed to *regenerate* the lint→group map from the clippy website). The map is
committed under `.cache/`, so loading it — the normal path — needs neither
dependency and runs offline. To refresh the map:
`CLIPPY_CONF_DIR=.cache python -c "from clippy_lint_map import ClippyLintMap;
ClippyLintMap().create_clippy_maps()"` (needs `requests` + `beautifulsoup4`).

Vendored change: on a clippy/compile failure, `main()` now records the failure
string as a `build_error` key in the output JSON (it was only logged before).
This lets `proctor.testing.idiomaticity_eval` skip a non-building crate instead
of misreading its empty lint set as "0 lints / maximally idiomatic."
