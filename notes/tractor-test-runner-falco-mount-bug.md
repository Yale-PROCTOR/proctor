# Bug: `runtests` orchestrator fails to start Falco — `falco.yaml` bind-mount rejected by runc ("not a directory")

**Repo:** `tools/test_runner` (runtests) in DARPA-TRACTOR-Program/Test-Corpus
**Severity:** blocks all Rust test-vector runs on this host (Falco is started unconditionally)

## Summary

`nix run ./tools/test_runner -- --rust --subset <case>` builds everything
successfully (provisions the toolchain, builds+loads the `exec_test_vector`
image, builds the cando runners), then **fails when starting the Falco
container**. The orchestrator bind-mounts the packaged `falco.yaml` config into
the Falco container and runc rejects the mount with:

```
400 Client Error ... /containers/<id>/start: Bad Request
("failed to create task for container: failed to create shim task:
 OCI runtime create failed: runc create failed: unable to start container
 process: error during container init:
 error mounting
   "/nix/store/…-python3-3.14.4-env/lib/python3.14/site-packages/runtests/orchestrator/falco_configs/falco.yaml"
 to rootfs at "/etc/falco/falco.yaml":
 mount src=…/falco.yaml, dst=/etc/falco/falco.yaml, dstFd=/proc/thread-self/fd/11,
 flags=MS_BIND|MS_REC: not a directory:
 Are you trying to mount a directory onto a file (or vice-versa)?")
```

The failing mount is defined in `orchestrator/falco.py`:

```python
str(Path(__file__).parent / "falco_configs" / "falco.yaml"): {
    "bind": "/etc/falco/falco.yaml",
    "mode": "ro",
},
```

## Why this is anomalous

Both ends of the mount are **regular files**, so a file→file bind should succeed:

- **Source** `…/falco_configs/falco.yaml`: `regular file`, 2486 bytes, world-readable (`-r--r--r--`).
- **Destination** `/etc/falco/falco.yaml` inside the pinned Falco image
  (`falcosecurity/falco@sha256:b4166a61f41e2fa638c041cac881d8bb32c284e3aaf282fdfed80f15f6eb55e3`,
  v0.43.1): `regular file` (verified via
  `docker run --rm --entrypoint stat <image> -c '%F' /etc/falco/falco.yaml`).

runc's `MS_BIND|MS_REC` recursive bind is being applied to a single file, and
the "not a directory" error suggests the recursive bind flag (`MS_REC`) is being
mishandled for a file source on this Docker/runc version.

This succeeds on TRACTOR's CI runner, so it appears **specific to a newer
Docker/runc** than CI uses (see environment below).

## Environment

| | |
|---|---|
| OS | Ubuntu 24.04.3 LTS |
| Kernel | 6.8.0-124-generic |
| Docker | **29.3.1** (bundled runc, default runtime) |
| Nix | 2.35.1, single-user, rootless (store at real `/nix`) |
| Falco image | `falcosecurity/falco@sha256:b4166a61…` (0.43.1), `modern_ebpf` engine |
| Invocation | `nix run ./tools/test_runner -- --rust --subset Public-Tests/B01_synthetic/001_helloworld --junit-xml … --keep-going` |

Leading hypothesis: **Docker 29.x** is newer than the CI's Docker, and its
handling of `MS_REC` on single-file bind mounts changed. A downgrade or a
targeted fix in `falco.py` (e.g. mounting the *directory* `falco_configs/` to a
dir dest, or dropping `MS_REC` for the config files) would likely resolve it.

## Reproduction

1. Host with Docker 29.3.1, rootless single-user Nix, unprivileged userns on.
2. A corpus case laid out as `Public-Tests/<Bxx>/<case>/{test_case,test_vectors,translated_rust}`.
3. `nix run ./tools/test_runner -- --rust --subset Public-Tests/<Bxx>/<case> --junit-xml out.xml --keep-going --log-level INFO`
4. Observe the runc mount error above; no vectors run.

## Suggested fixes

- Mount the whole `falco_configs/` directory to a directory destination, or add
  `bind-propagation`/`bind-nonrecursive` so runc doesn't apply `MS_REC` to a
  file source; or pin/support the newer Docker/runc.

---

# Related feature request: allow running without Falco (`--no-falco`)

**Falco is currently mandatory even for vectors that have no filesystem
expectations.** `orchestrator/__main__.py` calls `FalcoManager().start()`
unconditionally, and `exec_test_vector/__main__.py` calls
`get_filesystem_changes()` (reads the Falco log) unconditionally. There is no
flag or conditional to skip it.

But Falco is only needed for vectors carrying `file_changes.tar.gz` / `setup`
(filesystem-effect checks). Pure `stdout`/`stderr`/`rc` (binary) and
`lib_state_*` (library) vectors don't need it.

A `--no-falco` mode (skip the Falco container; skip `get_filesystem_changes`;
treat any vector that *requires* file-change comparison as SKIP) would:

- **sidestep the mount bug above** for the common case (most B01_synthetic
  vectors have no file-change component), and
- **drop the infrastructure requirement** — Falco needs a privileged container
  (`SYS_ADMIN`, `/sys/kernel/tracing`, host `/proc`, docker socket), which is a
  significant barrier on locked-down / non-CI hosts. Binary + library vectors
  would then run with only Docker (no privileged Falco).

This would make the harness usable in far more environments while still doing
full filesystem verification wherever Falco is available.
