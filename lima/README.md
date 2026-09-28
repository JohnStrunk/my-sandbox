# devbox Lima VM

This directory holds the Lima template for the **devbox VM**: a minimal,
VM-native development environment for my-sandbox. A single Fedora guest
runs OpenCode directly inside it, with Podman available as a project
tool, nested virtualization always enabled (for L2 test VMs, minikube,
kind, and iterating on my-sandbox itself), and the host directories that
matter shared in at the same paths.

The container devbox (`../devbox`) remains fully supported; the VM is the
path to full toolchain parity with nested-VM support, and provisioning
parity beyond the minimal set here is tracked separately in
[issue #270](https://github.com/JohnStrunk/my-sandbox/issues/270).

## Why a VM

The container devbox cannot run VMs: there is no `/dev/kvm` inside a
rootless container, and nested rootless kind was removed as unreliable.
The decided replacement (2026-09-28) is a VM-native devbox. This
bootstrap gets a working VM in place as quickly as possible so agent
sessions can run _inside_ the target environment.

## One-time host preparation (Fedora)

1. **Lima ≥ 2.1.3** (the floor for the CVE-2026-53657 guest-agent fix,
   GHSA-2j9v-p4xj-cjw2). Lima is not packaged for Fedora; install the
   upstream release tarball (adjust the version as long as it is ≥ 2.1.3):

   ```shell
   curl -fsSL \
     "https://github.com/lima-vm/lima/releases/download/v2.2.0/lima-2.2.0-Linux-$(uname -m).tar.gz" \
     -o /tmp/lima.tgz
   sudo tar -C /usr/local -xzf /tmp/lima.tgz
   ```

2. **QEMU and UEFI firmware** (Lima's qemu driver does not bundle them):

   ```shell
   sudo dnf install -y qemu-kvm edk2-ovmf
   ```

3. **`/dev/kvm` access**: add yourself to the `kvm` group
   (`sudo usermod -aG kvm "$USER"`, then log out/in).

4. **Nested virtualization check** (required: the template sets
   `nestedVirtualization: true`, and Lima fails fast when the host KVM
   module has nesting disabled):

   ```shell
   cat /sys/module/kvm_intel/parameters/nested   # Intel: expect Y or 1
   cat /sys/module/kvm_amd/parameters/nested     # AMD: expect 1
   ```

   If it is off, enable it and reboot (Intel example):

   ```shell
   echo "options kvm-intel nested=1" | sudo tee /etc/modprobe.d/kvm-intel.conf
   ```

5. **Kernel update discipline**: nested virtualization is always on, so a
   stale host kernel is a standing exposure. Keep unattended upgrades
   (for example `dnf-automatic`) enabled; reboots to apply a new kernel
   also restart the VM, which stop/start handles cleanly.

6. **Host directories**: the mounts below must exist on the host (Lima
   creates them if missing). Make sure the essentials are in place:

   ```shell
   mkdir -p ~/src ~/kb
   git clone git@github.com:JohnStrunk/my-sandbox.git ~/src/my-sandbox
   # Knowledge base at ~/kb (see the knowledge-base skill for bootstrap)
   ```

   The host `~/.config/{gh,gcloud,acli,gws,opencode}`,
   `~/.local/share/opencode`, and `~/.agents` are shared as-is if
   present.

## Creating the VM

One command creates and boots the instance (name `devbox`, derived from
the template filename). Pass the host Git identity once so provisioning
can seed the VM's global Git config:

```shell
limactl start ~/src/my-sandbox/lima/devbox.yaml \
  --param "GitUserName=$(git config --global user.name)" \
  --param "GitUserEmail=$(git config --global user.email)"
```

The first boot downloads the Fedora 44 cloud image, installs packages,
and runs the readiness probe; expect several minutes. Subsequent starts
are much faster (provisioning is idempotent and re-runs on every start).

**Note:** Lima embeds the template and its `provision`/`probes` scripts
into the instance at create time. Later changes to `lima/*.sh` or
`lima/devbox.yaml` do **not** propagate to an existing instance; recreate
it to pick them up (see [Recreating the VM](#recreating-the-vm)).

## Using the VM

```shell
limactl shell devbox              # log in (login shell, ~ = guest home)
```

Inside the VM the guest home (`/home/<user>.guest`) is VM-local, with
symlinks for the shared paths, so everything works from `~`:

```shell
cd ~/src/my-sandbox && opencode   # start an agent session
```

The existing worktree workflow carries over unchanged: `.worktrees/`
under the repo works inside the VM because the same-path mounts make the
worktree `.git` pointers (which reference host-absolute paths) resolve
identically. `uv sync --extra test` inside a worktree creates a VM-local
`.venv`.

Files created through the mounts are owned by your host uid (guest user
mirrors the host user), so edits made in the VM appear on the host and
vice versa.

## Stop/start

```shell
limactl stop devbox
limactl start devbox
```

Everything persists: VM-local state (the guest home, VM-local caches,
nested Podman storage, OpenCode state) lives on the VM disk, and the
shared paths are host directories. Provisioning re-runs on every start
and is idempotent.

After the VM is known-good, protect it against accidental deletion:

```shell
limactl protect devbox
```

(`limactl delete` then requires `--force`.)

## Recreating the VM

To pick up template or provisioning changes (including version-pin
bumps), recreate the instance. Host-side data (`~/src`, `~/kb`, and the
other mounts) is untouched; only VM-local state (guest home, VM-local
caches, nested Podman storage, OpenCode state) is lost:

```shell
limactl delete --force devbox     # --force is needed when protected
limactl start ~/src/my-sandbox/lima/devbox.yaml \
  --param "GitUserName=$(git config --global user.name)" \
  --param "GitUserEmail=$(git config --global user.email)"
```

## Shared vs VM-local state

| Path                        | Shared?  | Notes                            |
| --------------------------- | -------- | -------------------------------- |
| `~/src`                     | 9p, RW   | Projects root, worktrees         |
| `~/kb`                      | 9p, RW   | Knowledge base (`kbase.py sync`) |
| `~/.agents`                 | 9p, RW   | Agent skills (devbox-tools)      |
| `~/.config/opencode`        | 9p, RW   | OpenCode config                  |
| `~/.local/share/opencode`   | 9p, RW   | Session data                     |
| `~/.config/gh`              | 9p, RW   | gh auth state                    |
| `~/.config/gcloud`          | 9p, RW   | gcloud ADC and config            |
| `~/.config/acli`            | 9p, RW   | Atlassian CLI config             |
| `~/.config/gws`             | 9p, RW   | Google Workspace CLI             |
| `~/.local/state/opencode`   | VM-local | Single service owner             |
| `~/.gitconfig`              | VM-local | Git identity, HTTPS rewrite      |
| uv/pre-commit/Podman caches | VM-local | Rebuilt on demand                |

The guest home directory itself is VM-local (Lima's default
`/home/<user>.guest`); `provision-user.sh` symlinks the shared paths into
it. `$HOME` is never mounted wholesale.

## What is inside

- **Fedora 44** cloud image, digest-pinned (x86_64 and aarch64).
- **qemu/KVM**, `nestedVirtualization: true`, default `cpuType` (host),
  8 CPUs / 16 GiB RAM / 100 GiB sparse disk.
- **Podman** (rootless): Lima's boot scripts provide the base
  (`/etc/subuid` + `/etc/subgid`, cgroup delegation, linger);
  provisioning installs Podman and its networking/storage stack.
- **OpenCode** (pinned from `container/tool-versions.json`), **git**,
  **gh**, **uv** (pinned), **jq**, **Node.js/npm**.
- **limactl** (pinned) plus `qemu-kvm`/`edk2-ovmf` inside the guest, for
  nested L2 VMs.
- Git configured for GitHub over HTTPS (SSH remotes rewritten, `gh` as
  the credential helper), identity seeded once from the host.

Version pins in `lima/*.sh` carry `# renovate:` comments and are kept in
sync with `container/tool-versions.json` by
`scripts/validate_tool_versions.py` (the `lima` consumer).

## Deviations from the issue text

- **gcloud ADC**: the issue asks to mount the ADC _file_; Lima only
  mounts directories, so `~/.config/gcloud` (the parent) is mounted.
  Same credential passthrough.
- **Rootless Podman base**: the issue lists static subuid/subgid, cgroup
  delegation, and linger as provisioning work. Lima's own per-boot
  script (20-rootless-base.sh) already provides all three before
  provisioning runs, so the scripts here do not duplicate them; the
  readiness probe verifies them instead.
- **Guest home**: the guest home stays at Lima's default
  `/home/<user>.guest` rather than `/home/<user>`. Cloud-init creates
  mount points (and their parents) before creating the user, so pointing
  the guest home at a mount-point parent would break `useradd -m`
  (skeleton + ownership). Same-path semantics are preserved with
  symlinks instead.

## Deferred

- 9p vs virtiofs mount performance:
  [issue #268](https://github.com/JohnStrunk/my-sandbox/issues/268)
- OpenCode single-instance validation (host + VM sharing config/data):
  [issue #269](https://github.com/JohnStrunk/my-sandbox/issues/269)
- Full toolchain parity with the container devbox:
  [issue #270](https://github.com/JohnStrunk/my-sandbox/issues/270)

## Acceptance checklist

After the one-time `limactl start` succeeds and the readiness probe
passes, verify from inside the VM (`limactl shell devbox`):

- [ ] `cd ~/src/my-sandbox && git status` sees the host checkout.
- [ ] Create a worktree, `uv sync --extra test`, run
      `uv run --extra test pytest -m unit` — tests pass.
- [ ] Commit and push via `gh`/git from inside the VM.
- [ ] Edits made in the VM are visible on the host, owned by your uid.
- [ ] `~/kb/kbase.py sync` round-trips (knowledge base usable).
- [ ] `cd ~/src/my-sandbox && opencode` starts a session using the
      host-shared config; the background service persists across TUI
      sessions.
- [ ] Nested virtualization: start a small throwaway L2 VM
      (for example `limactl start template://alpine`), confirm it boots,
      then `limactl delete` it.
- [ ] `limactl stop devbox && limactl start devbox` — everything above
      still works.
