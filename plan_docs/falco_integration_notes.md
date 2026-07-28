# Falco / Full-Harness Integration — Deferred: Blockers & Setup Notes

Status: **deferred.** Binary + library vectors are verified now via the
old direct harness (`vector_testing_integration_plan.md`). This document
captures what the **new** Falco-based orchestrator adds, exactly what
blocks it today, and everything learned so re-setup is fast.

## 1. What Falco adds

The new `runtests.orchestrator` runs each vector in a Docker container and
uses **Falco** (syscall monitoring, `modern_ebpf` engine) to capture the
program's **filesystem side-effects** — files created / modified /
deleted — and compares them against the vector's recorded
`file_changes.tar.gz`.

This is the **only** capability the old direct harness lacks: **file-change
vectors** (directory vectors carrying a `setup` script and/or
`file_changes.tar.gz`). Binary (stdout/stderr/rc) and library (cando
lib-state) vectors do **not** need Falco.

## 2. How the new flow works (for when we return)

Their blessed invocation (from the corpus CI, `.github/workflows/ci.yml`):

```
nix run ./tools/test_runner -- --rust --subset <case> --junit-xml out.xml --keep-going --asan
```

That single command:
1. Provisions cmake/ninja/cargo via the Nix flake (`tools/test_runner/flake.nix`).
2. Builds the `exec_test_vector` Docker image (`dockerTools.buildLayeredImage`) and `docker load`s it.
3. Starts a **Falco container** (`falcosecurity/falco:0.43.1`, digest
   `sha256:b4166a61…`) with `cap_add=[SYS_ADMIN, SYS_RESOURCE, SYS_PTRACE]`,
   mounting `/sys/kernel/tracing`, host `/proc`, host `/etc`, the docker
   socket, and its config `falco.yaml` → `/etc/falco/falco.yaml`.
4. Runs **one container per test vector** (`exec_test_vector` image), which
   runs cando and reads the Falco log to diff filesystem changes.

So the runner is itself a **Docker + Falco orchestrator** — it must run at
**host level** (or a machine with real Nix + Docker), never nested inside
the framework container.

## 3. What blocks it right now

### 3.1 Primary blocker — `falco.yaml` bind-mount fails on Docker 29.x

Starting the Falco container fails with runc:

```
error mounting ".../falco_configs/falco.yaml" to rootfs at "/etc/falco/falco.yaml":
flags=MS_BIND|MS_REC: not a directory:
Are you trying to mount a directory onto a file (or vice-versa)?
```

Both source and destination are **regular files** (verified), so this is
anomalous. It reproduces with **Docker 29.3.1** on this host but works on
TRACTOR's CI runner (older Docker). Leading hypothesis: Docker 29.x
changed `MS_REC`-on-a-single-file bind handling.

Full write-up + suggested fixes + a `--no-falco` feature request:
`../notes/tractor-test-runner-falco-mount-bug.md` (to file with TRACTOR).

### 3.2 Falco is mandatory (no opt-out)

`orchestrator/__main__.py` calls `FalcoManager().start()` unconditionally,
and `exec_test_vector/__main__.py` calls `get_filesystem_changes()`
unconditionally. There is **no `--no-falco` flag** — so even a pure-stdout
case can't run without Falco (and thus can't dodge §3.1). Raising a
`--no-falco` request with TRACTOR is the cleanest unblock; do **not** patch
their harness ourselves.

### 3.3 Infrastructure requirements (present but heavy)

- **Docker** at host level (the runner spins containers via the SDK).
- **Falco** needs privileged caps + `/sys/kernel/tracing` + host `/proc`
  + eBPF (kernel ≥ ~5.8; this host's 6.8 is fine).
- **Nix** to build the `exec_test_vector` image (see §4).

## 4. What we PROVED works (so re-setup is fast)

On this host (Ubuntu 24.04.3, kernel 6.8, Docker 29.3.1), everything below
already works — only §3.1 remains:

- **Unprivileged userns**: `kernel.unprivileged_userns_clone = 1`.
- **Rootless Nix, two ways**:
  - `nix-portable` (no `/nix`, no sudo) — bootstraps Nix, **builds and
    `docker load`s the `exec_test_vector` image successfully**. But its
    `/nix` store is virtual (namespace-only), so containers launched by the
    host daemon can't mount `/nix` paths → the falco.yaml mount fails there
    *and* for that reason.
  - **Real single-user Nix** (chosen): one-time `sudo mkdir -m 0755 /nix &&
    sudo chown $USER /nix`, then the official installer `--no-daemon`.
    Nix 2.35.1 installed; **every `nix run` since is rootless** (the one
    sudo was only to create the `/nix` mountpoint at `/`). With a real
    `/nix`, store paths are host-visible — this removes the *virtual-store*
    cause, leaving only the Docker-29 `MS_REC` mount bug (§3.1).
- **Falco + Docker**: the Falco container starts/stops cleanly on this host
  when the mount succeeds (proven in a hybrid run); cando executes vectors
  inside the `exec_test_vector` container.
- **Build phase**: cmake/ninja/cargo provisioned; the corpus's cando
  runners build.

### Hybrid that got furthest (diagnostic only, not the target)

Running the **orchestrator from a pip install** (real host paths) while
using the **Nix-built image** got past the falco.yaml mount (real pip path,
not `/nix`), started Falco, and **executed cando** — failing later only on
env-detail mismatches (falco log path) that arise because the hybrid isn't
their blessed flow. Do **not** productionize the hybrid; it means
reverse-engineering their harness. It only confirmed the stack runs here.

## 5. To set it up again (checklist)

1. Ensure `/nix` exists and is user-owned (one-time sudo), single-user Nix
   installed (`. ~/.nix-profile/etc/profile.d/nix.sh`). Rootless thereafter.
2. Ensure Docker works for the user, unprivileged userns on, kernel ≥ 5.8.
3. Lay the case out under `<corpus>/Public-Tests/<Bxx>/<case>/` with
   `test_case/`, `test_vectors/`, `translated_rust/` (the stage output),
   and `runner/` for library cases.
4. Run their command from the corpus root:
   `nix run --extra-experimental-features "nix-command flakes"
   ./tools/test_runner -- --rust --subset <rel> --junit-xml out.xml --keep-going`
5. **If §3.1 still bites** (Docker 29.x): confirmed blocker → needs TRACTOR
   fix or `--no-falco`, or run on a host with an older Docker/runc where
   the file bind-mount succeeds. Check whether a newer/patched
   `tools/test_runner` has resolved it before retrying.

## 6. When to revisit

Trigger a return to the full flow when **any** of:
- TRACTOR fixes the `falco.yaml` mount (or the corpus bumps to a Docker
  version where it works), or
- TRACTOR ships `--no-falco` (then binary/library file-change-free cases
  run containerized without the mount at all), or
- we run on a machine matching their CI (older Docker + real Nix), or
- we specifically need **file-change vector** verification (the only gap of
  the old direct harness).

At that point the framework swap is small: `vector_harness.py` gains a
second engine that calls `nix run ./tools/test_runner` instead of
`runtests.rust`, same corpus-staging and JUnit-parsing
(`vector_testing_integration_plan.md` §3.1, milestone V4).
