# virtiofsd symlink constraint and the nested virtiofs check

<!-- Captured with Showboat 0.6.1 on 2026-11-12 -->
<!-- showboat-id: 772908c5-7b59-412e-8b7a-ad1560087f8a -->

Why: the repository recorded that "virtiofsd could not start in this
environment". That claim over-generalized the real root cause. Rust
virtiofsd rejects a shared directory whose final path component is a
symlink. The failure happens before QEMU starts, and the `limactl` output
never mentions symlinks; the real error appears only in
`$LIMA_HOME/<instance>/ha.stderr.log`. In the devbox L1, the shared paths
are symlinks by design, so a nested virtiofs VM with a `~/...` location
fails at startup with a cryptic EINVAL.

What changed: `lima/devbox.yaml`, `lima/README.md`, and
`lima/mount-policy.md` name the symlink constraint and record the
resolution pattern — resolve every host mount location with `readlink -f`
before `limactl create`/`start`, or keep mount locations as real
directories. 9p stays the template default because it tolerates symlinks.
The Rust `virtiofsd` RPM joins the VM package set so fresh VMs can run
virtiofs mounts, and a recursive-tier check encodes the whole behavior.

## Source map

- Corrected template record: `lima/devbox.yaml:56-82`.
- Mount policy constraint and resolution pattern:
  `lima/mount-policy.md:85-134`.
- README rationale and package bullet: `lima/README.md:841-856,1006-1007`.
- Package set and embedded digest: `lima/fedora-packages.sh:91-96`,
  `lima/provision-system.sh:500`.
- Recursive check: `tests/vm/test_recursive_vm.py:129-253`.
- Unit pins: `tests/unit/test_lima_template.py:724-762,909-920`,
  `tests/unit/test_vm_level_marker.py:567`.

## The constraint trigger: the L1's shared paths are symlinks

```bash
for p in "$HOME/src" "$HOME/kb" "$HOME/.config/opencode" "$HOME/.local/share/opencode"; do printf "%-32s " "$p"; if [[ -L "$p" ]]; then echo "symlink -> $(readlink "$p")"; else echo "directory"; fi; done
```

```output
/home/jstrunk.guest/src          symlink -> /home/jstrunk/src
/home/jstrunk.guest/kb           symlink -> /home/jstrunk.guest/.host-config/kb
/home/jstrunk.guest/.config/opencode symlink -> /home/jstrunk.guest/.host-config/config/opencode
/home/jstrunk.guest/.local/share/opencode symlink -> /home/jstrunk.guest/.host-config/local/share/opencode
```

## The daemon the package set now installs

```bash
rpm -q virtiofsd && /usr/libexec/virtiofsd --version
```

```output
virtiofsd-1.14.0-1.fc44.x86_64
virtiofsd 1.14.0
```

## The corrected template record

```bash
sed -n '56,78p' lima/devbox.yaml
```

```output
# #268 favors virtiofs for the full VM, but it requires a direct host-to-VM
# mixed-write check first (#317); keep 9p until a supported host passes it.
# The earlier "virtiofsd could not start in this environment" record has a
# narrower cause: the Rust virtiofsd rejects a shared directory whose final
# path component is a symlink ("Error entering sandbox: BindMountSharedDir"
# with EINVAL, before QEMU starts). The backing filesystem does not matter.
# In the L1 guest, ~/src, ~/kb, and the linked config paths are symlinks by
# design, so a nested virtiofs VM whose template passes such a location
# fails at virtiofsd startup. Lima expands a "~/..." mount location against
# the host HOME without resolving symlinks: resolve every host mount
# location (readlink -f) before limactl create/start, or keep mount
# locations as real directories. 9p tolerates symlinked locations, so the
# "~/..." locations below are safe with this default. lima/mount-policy.md
# records the constraint and the repeatable nested-L1 check.
mountType: "9p"
```

## Observed behavior (captured 2026-11-12 inside the devbox L1)

A minimal nested VM with `mountType: virtiofs` and a symlinked location
failed in about 10 seconds, before QEMU started. The `limactl` stdout
showed only `level=fatal msg="exiting, status={Running:false ...}"`. The
hostagent log carried the real cause:

```text
virtiofsd-0[stderr]: ... ERROR virtiofsd] Error entering sandbox: BindMountSharedDir(Os { code: 22, kind: InvalidInput, message: "Invalid argument" })
```

The same VM shape with the resolved directory started; `findmnt` reported
FSTYPE `virtiofs` for the mount; the provisioner wrote the L2 marker; the
guest read a host-written file; the guest created and read back its own
file; and the guest write was visible on the L1 side.

## Verification

Run on 2026-11-12, inside the devbox L1 from the feature worktree:

- The full unit suite passed (962 passed, 35 deselected after the rebase).
- All pre-commit hooks passed, including the sanitized fast tier
  (`scripts/sanitized-test.sh --guest-vm --vm-lock --resource-preflight`).
- The recursive check
  `tests/vm/test_recursive_vm.py::test_virtiofs_shared_dir_requires_a_resolved_location`
  passed (1 passed in 37.84s), including its assertion that the guest wrote
  `/etc/devbox/vm-level` as `L2`. Lima does not fail `limactl start` when a
  provision script fails — cloud-init records the error while the start
  completes — so that marker assertion is what proves the provisioner ran.
- A fresh fully provisioned L2 built from this worktree reached readiness,
  answered `limactl shell <name> -- echo hello` with `hello`, reported
  `/etc/devbox/vm-level` as `L2`, contained `virtiofsd-1.14.0-1.fc44`, and
  kept the 9p default for its own source mount. The staged source mount was
  read-only; config and credential mount sources were empty, and no L1
  credentials were passed into the L2. The task root and L2 were deleted
  after the run.

## Reproduce

Inside a provisioned L1, from a checkout of this commit:

```shell
./scripts/sanitized-test.sh --guest-vm --require-recursive-vm -- \
  env UV_PROJECT_ENVIRONMENT="$HOME/.cache/my-sandbox-issue486-recursive-venv" \
  uv run --extra test pytest -q \
  tests/vm/test_recursive_vm.py::test_virtiofs_shared_dir_requires_a_resolved_location
```

CI runs the same check in the recursive tier (`run-vm-ci.sh recursive`),
which creates a fresh VM from the updated package set. Cleanup is
automatic: the check deletes its nested VM and removes its task root under
`/var/tmp`.
