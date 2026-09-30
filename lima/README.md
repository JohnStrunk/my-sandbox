# devbox Lima VM

This directory holds the Lima template for the **VM-native devbox**. A single
Fedora guest runs OpenCode directly inside it, with the full manifest-pinned
toolchain, rootless Podman available as a project tool, and nested
virtualization enabled for L2 test VMs, kind, and minikube. Project files are
shared at their host paths inside the VM. Host-shared directories are protected
from the package builder; configuration and credentials are mounted behind a
root-owned parent and exposed only to the guest user.

Issue [#267](https://github.com/JohnStrunk/my-sandbox/issues/267) provides the
minimal bootstrap environment; this full template is the foundation for the
VM-native migration tracked by
[#280](https://github.com/JohnStrunk/my-sandbox/issues/280).
The container devbox remains available during the migration.

## Why a VM

The container devbox cannot run VMs: there is no `/dev/kvm` inside a
rootless container, and nested rootless kind was removed as unreliable.
The decided replacement (2026-09-28) is a VM-native devbox. The minimal
bootstrap got a working VM in place quickly so agent sessions could run
_inside_ the target environment. This full template adds the pinned toolchain
and operator/Kubernetes profile needed to replace the container path.

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
   sudo dnf install -y qemu-kvm qemu-img edk2-ovmf
   ```

3. **`/dev/kvm` access**: add yourself to the `kvm` group
   (`sudo usermod -aG kvm "$USER"`, then log out/in).

4. **Nested virtualization check** (required: L2 test VMs, minikube, and
   kind inside the devbox need it). Lima ≤ 2.2 does **not** fail fast
   when the host KVM module has nesting disabled — under the qemu driver
   the template's `nestedVirtualization: true` is inert there (qemu
   support for the flag lands after v2.2.0). Guest CPU nesting comes
   from Lima's default host-CPU passthrough, so this manual check and
   the readiness probe's VMX/SVM check are the actual guards:

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

From a checkout under the shared `~/src`, create the instance (name `devbox`,
derived from the template filename). `RepoPath` must be the guest-visible path
to that checkout; the translation below also handles hosts where `~/src` is a
symlink. Pass the host Git identity so provisioning can seed the VM's global
Git config:

```shell
cd /path/to/my-sandbox
src_path="$(readlink -f "$HOME/src")"
repo_path="$(pwd -P)"
kb_path="$(readlink -f "$HOME/kb")"
case "$repo_path" in
  "$src_path"/*) ;;
  *) echo "checkout must be under ~/src" >&2; exit 1 ;;
esac
limactl start "$repo_path/lima/devbox.yaml" \
  --param "SrcPath=$src_path" \
  --param "RepoPath=$repo_path" \
  --param "KbPath=$kb_path" \
  --param "GitUserName=$(git config --global user.name)" \
  --param "GitUserEmail=$(git config --global user.email)"
```

The first boot downloads the Fedora 44 cloud image, installs the full toolchain,
prefetches the Playwright browser and Semble model, and runs the readiness
probe; expect several minutes. Subsequent starts are much faster. Provisioning
re-runs idempotently on every start and reads `container/tool-versions.json`
from the shared checkout. Third-party npm/Python packages, browser/model
prefetch, and automatic version checks run as `devbox-toolbuilder`, a separate
unprivileged account with no access to host mounts. The readiness probe checks
a root-owned stamp from the builder's manifest check rather than running
package commands as the credential-capable guest.

**Note:** Lima embeds the template and its provisioning scripts into the
instance at create time. Readiness probes must be inline
`probes[].script` entries with a `#!` line; unlike provisioning, a local
`probes[].file` path is treated as a URL locator. The inline script is
kept in sync with `probe-readiness.sh`, and a unit test checks the copy.
Later changes to the embedded `lima/provision-system.sh`,
`lima/provision-user.sh`, or `lima/devbox.yaml` do **not** propagate to an
existing instance; recreate it to pick them up (see
[Recreating the VM](#recreating-the-vm)). `lima/provision-tools.sh`, the tool
manifest, and the root-owned tool assets are read from the checkout at every
start. Version-only changes in the live tool manifest are applied on
stop/start without recreating the VM.

## Using the VM

```shell
cd ~/src/my-project
devbox                     # shell at the same project path in the VM
devbox opencode            # start OpenCode in the current project
```

The top-level [`devbox`](../devbox) launcher is VM-native by default. It
validates that the current directory is in a mounted host path, ensures the VM
is running, and uses Lima's `--preserve-env` with a strict credential/provider
allowlist. Unrelated host environment variables are not forwarded. The
allowlist mirrors the transitional container launcher's provider names and
credential-group rules. For a low-level host-side shell, `lima/devbox-shell`
uses the same filtered environment and starts the VM if needed.

After creating and validating the VM, enable optional host-login autostart to
avoid starting it manually after reboot:

```shell
limactl autostart enable devbox
```

OpenCode's managed background service inherits these variables when it
starts. Provisioning intentionally does not pre-start the service without
credentials; the first OpenCode command in the forwarded shell starts it.
If you previously started OpenCode from a plain `limactl shell`, stop that
service once (`opencode service stop` from the VM) before retrying with the
helper.

Inside the VM the guest home (`/home/<user>.guest`) is VM-local, with
symlinks for the shared paths, so everything works from `~`:

```shell
repo="$(dirname "$(dirname "$(readlink -f /etc/devbox/tool-versions.json)")")"
cd "$repo" && opencode   # start an agent session
```

The existing worktree workflow carries over unchanged: `.worktrees/`
under the repo works inside the VM because the same-path `~/src` mount makes
the worktree `.git` pointers (which reference host-absolute paths) resolve
identically. The `~/kb` mount has a separate same-path alias for its worktree
metadata. One caveat: `uv sync` in a checkout or worktree puts
`.venv` on the shared `~/src` mount — see
[`.venv` lives on the shared mount](#venv-lives-on-the-shared-mount)
below.

Files created through the mounts are owned by your host uid (guest user
mirrors the host user), so edits made in the VM appear on the host and
vice versa.

## Stop/start

```shell
devbox --stop          # graceful stop
devbox                 # start on demand, then enter the current directory
devbox --reprovision   # stop/start and re-run the embedded provisioners
devbox --reset         # factory-reset, then start and provision again
```

Everything persists: VM-local state (the guest home, VM-local caches,
nested Podman storage, OpenCode state) lives on the VM disk, and the
shared paths are host directories. Provisioning re-runs on every start and is
idempotent. Package installs and automatic version checks use the isolated
`devbox-toolbuilder` account; it cannot traverse the root-owned `~/.host-config`
parent or the protected parent of `~/src`. The KB alias points into the
protected mount tree. Provisioning verifies these boundaries directly and
refuses to protect a `SrcPath` parent inside the guest home.

`devbox` compares a running VM's provisioning fingerprint with the current
checkout and warns when they differ; use `devbox --reprovision` to apply the
current manifest and update the stamp. A stopped VM re-runs provisioning as it
starts. `--reset` is destructive to VM-local state but preserves host-mounted
projects and configuration. Edits to `lima/devbox.yaml` or embedded
provisioning scripts still require the recreation procedure below; reset and
reprovision operate on the existing instance's embedded template.

### Tool-version updates and drift

Provisioning reads `container/tool-versions.json` from the shared checkout on
every start. It stores the root-owned system-script digest in
`/var/lib/devbox-vm/system-provision.sha256` and the combined manifest,
script, and tool-asset fingerprint in the VM-local
`~/.local/share/devbox-toolchain/provisioning.fingerprint`.
When the manifest changes, provisioning reports the drift, installs any new
pins, runs `devbox-toolchain-check` as the isolated builder, then records the
new fingerprint and a root-owned manifest stamp. The readiness probe checks
that stamp and does not execute third-party package commands with host
credentials mounted.
The `devbox-toolchain-check` command remains available for manual use as the
current guest user. Apply a version-only bump without rebuilding the VM:

```shell
repo="$(dirname "$(dirname "$(readlink -f /etc/devbox/tool-versions.json)")")"
git -C "$repo" pull --ff-only
limactl stop devbox
limactl start devbox
```

Changes to `lima/provision-system.sh`, `lima/provision-user.sh`, or
`lima/devbox.yaml` are embedded at VM creation and still require the
[recreate procedure](#recreating-the-vm). The non-embedded
`lima/provision-tools.sh` helper is refreshed on each start. The
`devbox-toolchain-check` command reports every manifest-declared Lima tool at
its exact version, plus `make`, Python/pip, and ShellCheck.

The rootless Podman Docker-compatible API is enabled at
`$XDG_RUNTIME_DIR/podman/podman.sock` and exported through `DOCKER_HOST` for
Docker API clients. Kind uses its explicit experimental Podman provider; ten
consecutive create/delete cycles passed, so Docker CE is not installed.
`lima/validate-kind.sh` repeats that acceptance check.
The VM keeps `net.ipv4.conf.default.route_localnet=0` to preserve the loopback
routing boundary; a rootless Podman published-port smoke test passed with it
disabled.

After the VM is known-good, protect it against accidental deletion:

```shell
limactl protect devbox
```

(`limactl delete` then requires `--force`.)

## Recreating the VM

To pick up template or provisioning-script changes, recreate the instance.
Version-only tool pin changes do not require a recreate; use the
[drift/re-provision procedure](#tool-version-updates-and-drift). Host-side
data (`~/src`, `~/kb`, and the
other mounts) is untouched; only VM-local state (guest home, VM-local
caches, nested Podman storage, OpenCode state) is lost:

```shell
cd /path/to/my-sandbox
src_path="$(readlink -f "$HOME/src")"
repo_path="$(pwd -P)"
kb_path="$(readlink -f "$HOME/kb")"
limactl delete --force devbox     # --force is needed when protected
limactl start "$repo_path/lima/devbox.yaml" \
  --param "SrcPath=$src_path" \
  --param "RepoPath=$repo_path" \
  --param "KbPath=$kb_path" \
  --param "GitUserName=$(git config --global user.name)" \
  --param "GitUserEmail=$(git config --global user.email)"
```

## Shared vs VM-local state

| Path | Shared? | Notes |
| --- | --- | --- |
| `~/src` | 9p, RW (virtiofs after host validation) | Same-path projects root and worktrees; its guest-side parent is accessible only to the guest UID |
| `<repo>/.venv` | 9p, RW (virtiofs after host validation) | Under `~/src`; venv caveat below |
| `~/kb` | 9p, RW (virtiofs after host validation) | Mounted at `~/.host-config/kb`; a same-path alias preserves absolute worktree pointers |
| Host `~/.agents` → guest `~/.host-config/agents` | 9p, RO | Root-owned parent grants traversal only to guest UID; image skills win in guest-local `~/.agents` |
| `~/.config/opencode` | 9p, RW (virtiofs after host validation) | Mounted under `~/.host-config/config/opencode`, linked into guest config |
| `~/.local/share/opencode` | 9p, RW (virtiofs after host validation) | Mounted under `~/.host-config/local/share/opencode` |
| `~/.config/gh` | 9p, RW (virtiofs after host validation) | Host credentials, protected from package builder |
| `~/.config/gcloud` | 9p, RW (virtiofs after host validation) | gcloud ADC/config, protected from package builder |
| `~/.config/acli` | 9p, RW (virtiofs after host validation) | Atlassian CLI config, protected from package builder |
| `~/.config/gws` | 9p, RW (virtiofs after host validation) | Google Workspace CLI config, protected from package builder |
| `~/.local/state/opencode` | VM-local | Single service owner |
| `/var/lib/devbox-toolbuilder` | VM-local | npm/uv/Rust installs, Playwright browser, Semble model; guest can use installed binaries but cannot modify packages |
| `~/.cache/{go,uv,semble/index}` | VM-local | Guest-writable Go, uv, and Semble index caches |
| `/usr/local/node` | VM-local | Manifest-pinned Node.js and npm runtime |
| `~/.cargo` | VM-local | Guest-local Cargo cache; Rust toolchain is read from builder install |
| `~/.local/share/kubebuilder-envtest` | VM-local | `setup-envtest` default asset store |
| `~/.gitconfig` | VM-local | Git identity, HTTPS rewrite |

The template remains on 9p because Lima's `virtiofsd` exited before guest
startup during the direct host-to-VM attempt in this environment. The #268
benchmark recommends virtiofs for the full Linux/QEMU template after a direct
host-to-VM mixed-write check passes. On a host where virtiofsd starts, switch
`mountType` to `virtiofs` only after running that check; otherwise keep 9p.

The guest home directory itself is VM-local (Lima's default
`/home/<user>.guest`); `provision-user.sh` symlinks the shared paths into
it. `$HOME` is never mounted wholesale.

### `.venv` lives on the shared mount

`uv sync` creates `<repo>/.venv` inside the checkout, and checkouts
(including `.worktrees/`) live under the shared `~/src` mount — so a
`.venv` is shared state, not VM-local storage. A virtual environment is
bound to the interpreter and uv cache that built it, and the host and
the guest are different systems, so one `.venv` must not be used from
both sides (the host-side `uv` and the VM-side `uv` will fight over it
and can corrupt it). Pick one side per checkout:

- Use a given checkout from the host **or** from the VM, not both.
- **VM default:** keep the environment off the shared tree with
  `UV_PROJECT_ENVIRONMENT`. Use a unique VM-local path per checkout or
  worktree (the guest home is not mounted):

  ```shell
  export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/my-sandbox-issue269"
  uv sync --extra test
  uv run --extra test pytest -m unit
  ```

  Issue #269 validation on 2026-09-29 passed all 317 unit tests with both
  placements. The shared 9p run took 165.73s; its cache/resource conditions
  were not controlled. The VM-local warm-cache run took 99.49s. The initial
  shared install also fell back from hardlinks to copies. These timings are
  indicative rather than a controlled benchmark, but support the VM-local
  default. The test checkout itself remains on the shared mount.

### OpenCode data-sharing checkpoint (#269)

On 2026-09-29, one isolated OpenCode 2.0.16 service demonstrated CLI/API
visibility of sessions from two project directories under `~/src`;
`opencode session list` in each project showed its session, and the session
metadata API returned the corresponding project location. Conversation
resume was not tested. A separate 10-minute soak used two test services with
VM-local state directories and one disposable shared 9p data directory: they
created 465 and 518 sessions, saw each other's project sessions, and the
database passed `PRAGMA integrity_check` with no SQLite lock/corruption or
service-registration replacement errors. No production session data was used.

This is guest-to-guest evidence only; it does **not** prove host-to-VM
filesystem coherency for OpenCode's SQLite data. The host OpenCode process and
CodeBurn could not be exercised from
the guest, so keep host-shared session data provisional until a host writer
and host CodeBurn read are verified. The full template now provisions Semble
and its VM-local model cache. GitHub MCP is intentionally not installed: the
decision in #275 makes the `gh` CLI canonical. TUI session-switching UX and
model-backed conversation resume remain to be validated in the later
integration work.

## What is inside

- **Fedora 44** cloud image, digest-pinned (x86_64 and aarch64).
- **qemu/KVM**, `nestedVirtualization: true`, default `cpuType` (host),
  8 CPUs / 16 GiB RAM / 100 GiB sparse disk.
- **Rootless Podman**: Lima's boot scripts provide static `/etc/subuid` and
  `/etc/subgid`, cgroup-v2 delegation, and linger; provisioning installs
  Podman/netavark, configures the Docker-compatible socket, and sets bridge
  sysctls as root. Docker CE is not installed.
- **Manifest-pinned tools**: OpenCode, Go + `devbox-go`, uv, Rust, Node/npm,
  Playwright CLI + bundled Chromium, ast-grep + its skills, Semble + prefetched
  model, Repomix, Hadolint, markdownlint-cli2, pre-commit, acli, Google
  Workspace CLI, Antigravity CLI, kind, kubectl, Helm, and Pipenv. Release
  assets with manifest checksums are verified before installation.
- **Operator profile**: GNU make, kind, kubectl, Helm, Python/pip, Pipenv, and
  a VM-local `~/.local/share/kubebuilder-envtest` asset-store location.
- **Additional CLIs/utilities**: `gh`, `glab`, `gcloud`, `gws`, ShellCheck,
  `tokei`, `just`, `difft`, `hyperfine`, `fd`, `file`, `diff`, and `patch`.
- **Nested VMs**: pinned `limactl` plus `qemu-kvm`, `qemu-img`, and
  `edk2-ovmf` inside the guest.
- Git configured for GitHub over HTTPS (SSH remotes rewritten, `gh` as
  the credential helper), identity seeded once from the host.

Every tool installed at a manifest-pinned version declares a `lima` consumer
in `container/tool-versions.json`. Provisioning reads those versions and
checksummed asset digests at runtime; `scripts/validate_tool_versions.py`
ensures the scripts consume every declared Lima tool, and
`lima/check_toolchain.py` verifies the installed versions.

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

The completed directory-sharing evaluation and conditional virtiofs
recommendation from
[issue #268](https://github.com/JohnStrunk/my-sandbox/issues/268)
are recorded in
[directory-sharing-benchmark.md](directory-sharing-benchmark.md).

## Acceptance checklist

After the one-time `limactl start` succeeds and the readiness probe
passes, verify from inside the VM (opened with
`~/src/my-sandbox/lima/devbox-shell`):

- [ ] `cd ~/src/my-sandbox && git status` sees the host checkout.
- [ ] `devbox-toolchain-check` reports the pinned manifest versions and
      operator tools; `make --version`, `kind version`, `kubectl version
      --client`, `helm version`, and `pipenv --version` all succeed.
- [ ] `~/src/my-sandbox/lima/validate-kind.sh` completes ten consecutive
      create/delete cycles using rootless Podman's Docker-compatible socket.
- [ ] `~/.agents/skills/devbox-tools/SKILL.md` and the ast-grep skills are
      present; image-owned files take precedence at those skill names.
- [ ] `HF_HOME` and `SEMBLE_CACHE_LOCATION` point under the VM-local cache,
      and Playwright's bundled Chromium launches without another download.
- [ ] Updating a tool version in the shared manifest and restarting the VM
      changes the stored fingerprint and applies the new version without a
      VM rebuild.
- [ ] Create a worktree and set `UV_PROJECT_ENVIRONMENT` to a unique
      VM-local path before `uv sync --extra test`; run
      `uv run --extra test pytest -m unit` — tests pass. Do not reuse that
      checkout's venv from the host (see the venv caveat above).
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
