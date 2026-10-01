# devbox Lima VM

This directory holds the Lima template for the **VM-only devbox**. A single
Fedora guest runs OpenCode directly inside it, with the full manifest-pinned
toolchain, rootless Podman available as a project tool, and nested
virtualization enabled for L2 test VMs, kind, and minikube. Project files are
shared at their host paths inside the VM. Host-shared directories are protected
from the package builder; configuration and credentials are mounted behind a
root-owned parent and exposed only to the guest user.

Issue [#267](https://github.com/JohnStrunk/my-sandbox/issues/267) provides the
minimal bootstrap environment; this full template completes the VM-native
design tracked by [#280](https://github.com/JohnStrunk/my-sandbox/issues/280).

## Why a VM

The VM-native design supports nested virtualization for L2 tests and project
tools while keeping OpenCode and the development toolchain in one guest. The
minimal bootstrap established the VM; this full template provisions the pinned
toolchain and operator profile in that same environment.

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
   (`sudo usermod -aG kvm "$USER"`, then log out/in). Keep the device owned
   by `root:kvm` with mode `0660`; do not make it world-writable.

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

The first invocation of `devbox` automatically creates the `devbox` instance
from this checkout's `devbox.yaml`, then starts it. Keep the `my-sandbox`
checkout under the shared `~/src` mount so the VM can read its template,
provisioning scripts, and tool manifest. The launcher passes the resolved
`~/src` and checkout paths, the `~/kb` path, and the host's global Git identity
to Lima; no separate `limactl start` command is needed. Host directories under
`~/src` and `~/kb` are created by Lima when they do not already exist.

The first boot downloads the Fedora 44 cloud image, installs the full toolchain,
prefetches the Playwright browser and Semble model, and runs the readiness
probe; expect several minutes. Subsequent starts are much faster. Provisioning
re-runs idempotently on every start and reads `lima/tool-versions.json`
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
allowlist contains the supported provider names and credential-group rules.
For a low-level host-side shell, `lima/devbox-shell`
uses the same filtered environment and starts the VM if needed.

After creating and validating the VM, enable optional host-login autostart to
avoid starting it manually after reboot:

```shell
limactl autostart enable devbox
```

OpenCode's managed background service inherits these variables when it
starts. Provisioning intentionally does not pre-start the service without
credentials; the first `devbox opencode` command starts it with the generated
runtime config. A bare `opencode` command from an arbitrary VM shell bypasses
the config generator, so do not use it to start the managed service. If you did,
stop that service once from the VM (`opencode service stop`) before retrying
with host-side `devbox opencode`.

When the command is `opencode` (including `opencode run`), the helper builds
`OPENCODE_CONFIG_CONTENT` inside the VM immediately before starting the CLI.
The generated overlay keeps the baseline provider-use policies and permission
rules, always enables the local Semble MCP, and gates Context7, The Source,
Tavily websearch, OCTO Open, and PriceTag on their complete credential groups.
Provider credentials appear only as `{env:NAME}` references in the JSON; the
secret values stay in the allowlisted process environment. PriceTag's built-in
OpenAI and Anthropic overrides keep `"env": []` so direct-provider keys cannot
take precedence over the gateway key. The overlay is in-memory only and does
not edit the host-shared `~/.config/opencode` configuration.

The Source MCP uses the committed `lima/the-source/uv.lock` dependency graph
and an immutable source commit. Its wrapper installs that locked environment
without credentials, then starts the child with only its own credentials,
`HOME`, and a minimal `PATH`; OpenCode's unrelated provider tokens are not
inherited by that process.

GitHub operations use the forwarded `gh` CLI and host `gh` authentication.
This gh-only policy is recorded in
[issue #275](https://github.com/JohnStrunk/my-sandbox/issues/275).
OpenCode's one VM-local service loads the runtime config at startup and serves
project sessions across the same-path `~/src` mount. If credentials change while
the service is running, stop and restart the service from a `devbox` shell so it
inherits the updated environment.

The schema-drift test runs against the manifest-pinned VM binary and an isolated
home/config directory, leaving the mounted global config untouched:

```shell
cd ~/src/my-sandbox
uv run --extra test pytest -q tests/unit/test_lima_opencode_config.py
```

The test is skipped outside the provisioned VM; there it asserts that
`opencode --version` matches the tool manifest and that `opencode debug config`
accepts and preserves the generated overlay.

`do-one-issue` keeps its Git and GitHub work in the VM: the host-side script
uses `devbox` only to run `git switch/pull` and the headless `devbox opencode
run --agent build ... --file .opencode/commands/grab-issue.md` command at the
mounted checkout path. The task instructions handle issue assignment, worktree
implementation, PR creation, and the documented CI/merge wait using the VM's
forwarded `gh` authentication.

Inside the VM the guest home (`/home/<user>.guest`) is VM-local, with
symlinks for the shared paths, so ordinary commands work from `~`:

```shell
repo="$(dirname "$(dirname "$(readlink -f /etc/devbox/tool-versions.json)")")"
cd "$repo" && git status
```

Launch OpenCode from the host with `devbox opencode` so the allowlisted
credentials and generated runtime configuration are applied to the shared
service.

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
devbox --reset -- git status       # run a command after the reset
devbox --reprovision -- opencode  # run a command after reprovisioning
devbox --delete        # unprotect and remove the VM; do not recreate it
```

VM-local state (the guest home, VM-local caches, and nested Podman storage)
lives on the VM disk; scoped host mounts, including the dedicated OpenCode L1
state directory, persist independently. Provisioning re-runs on every start
and is idempotent. Third-party package installs and automatic version checks
run as the isolated `devbox-toolbuilder` account; it cannot traverse the
root-owned `~/.host-config` parent or the protected parent of `~/src`. The KB
alias points into the protected mount tree. Provisioning verifies these
boundaries directly and refuses to protect a `SrcPath` parent inside the guest
home.

When `--reset` or `--reprovision` is followed by a command, `devbox` validates
that the caller's current directory is mounted before changing the VM, completes
the lifecycle action under the shared lock, then runs the exact command argv in
the VM from that mapped directory. It uses the ordinary `devbox` command path,
including its filtered host environment and OpenCode runtime configuration,
and returns the command's exit status. If no command is supplied, only the
lifecycle handling runs; `devbox` does not open the default interactive shell
(the reprovision fingerprint check still runs). Put `--` before a command whose
first argument begins with an option.
After the lock is released, a concurrent lifecycle action may win, so the
follow-on command can fail rather than restarting the VM.

`devbox` compares a running VM's provisioning fingerprint with the current
checkout and warns when they differ; use `devbox --reprovision` to apply the
current manifest and update the stamp. A stopped VM re-runs provisioning as it
starts. `--reset` is destructive to VM-local state but preserves host-mounted
projects, configuration, and the dedicated OpenCode L1 state. Edits to
`lima/devbox.yaml` or embedded
provisioning scripts still require the recreation procedure below; reset and
reprovision operate on the existing instance's embedded template.

`devbox -d` / `devbox --delete` removes the configured Lima instance without
starting or recreating it; running it again when the instance is absent is
successful. This explicit destructive option also removes Lima's protection
before deleting. The VM disk and all guest-local state are lost, but files on
host-mounted paths (including `~/src`, `~/kb`, host configuration, and the
dedicated OpenCode L1 state) survive. A later ordinary `devbox` invocation can
create a fresh instance from the current checkout's template.

### Tool-version updates and drift

Provisioning reads `lima/tool-versions.json` from the shared checkout on
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

`limactl delete --force devbox` alone does not remove this protection; first
run `limactl unprotect devbox`. The explicit `devbox --delete` operation does
both. `devbox --reset` does not remove protection either; with pinned Lima 2.2,
manually run `limactl unprotect <instance>` before resetting a protected VM,
then run `limactl protect <instance>` again after reset if the instance still
exists. Unlike `--reset`, `--delete` checks the original state and attempts to
restore protection if deletion fails.

## Recreating the VM

To pick up template or provisioning-script changes, recreate the instance.
Version-only tool pin changes do not require a recreate; use the
[drift/re-provision procedure](#tool-version-updates-and-drift). Host-side
data (`~/src`, `~/kb`, and the other mounts) is untouched; only VM-local state
(guest home, VM-local caches, nested Podman storage, and other guest-local
files) is lost. OpenCode's dedicated `~/.local/state/devbox-opencode` host
directory survives recreation. The host's default `~/.local/state/opencode`
is only a read-only, one-time import source:

If the existing VM has OpenCode preferences or prompt history that were
created only in its VM-local state directory, preserve them before deleting
the VM. Stop the OpenCode service, then run this from the checkout on the
host; it copies only safe preference/history files into the dedicated host
directory and excludes registrations, locks, temporary files, and symlinks.
The helper refuses to copy while the managed service is running. Existing L1
files take precedence over the host's initial seed:

```shell
limactl shell devbox -- opencode service stop
./lima/migrate-opencode-state.sh devbox
```

Skip this migration when the VM has no state to preserve. The destination
`~/.local/state/devbox-opencode` is private to this L1 and persists across
`devbox --reset`, `devbox --delete`, and manual recreation.

```shell
cd /path/to/my-sandbox
src_path="$(readlink -f "$HOME/src")"
repo_path="$(pwd -P)"
kb_path="$(readlink -f "$HOME/kb")"
limactl unprotect devbox          # remove protection before deleting
limactl delete --force devbox
limactl start "$repo_path/lima/devbox.yaml" \
  --param "SrcPath=$src_path" \
  --param "RepoPath=$repo_path" \
  --param "KbPath=$kb_path" \
  --param "GitUserName=$(git config --global user.name)" \
  --param "GitUserEmail=$(git config --global user.email)"
```

After the new instance boots and passes readiness checks, restore deletion
protection with `limactl protect devbox` if you use that safeguard.

## Shared vs VM-local state

The [mount decision matrix](mount-policy.md) records each mount's consumers,
access mode, and L1/L2 policy. Use `~/kb` as the canonical knowledge-base path
in guest shells and file searches; the repository does not provide a
checkout-relative `knowledge-base/` symlink.

| Path | Shared? | Notes |
| --- | --- | --- |
| `~/src` | 9p, RW (virtiofs after host validation) | Same-path projects root and worktrees; its guest-side parent is accessible only to the guest UID |
| `<repo>/.venv` | 9p, RW (virtiofs after host validation) | Under `~/src`; venv caveat below |
| `~/kb` | 9p, RW (virtiofs after host validation) | Mounted at `~/.host-config/kb`; a same-path alias preserves absolute worktree pointers |
| Host `~/.agents` → guest `~/.host-config/agents` | 9p, RO | Root-owned parent grants traversal only to guest UID; VM-owned skills win in guest-local `~/.agents` |
| `~/.config/opencode` | 9p, RW (virtiofs after host validation) | Mounted under `~/.host-config/config/opencode`, linked into guest config |
| `~/.local/share/opencode` | 9p, RW (virtiofs after host validation) | Mounted under `~/.host-config/local/share/opencode` |
| Host `~/.local/state/opencode` | 9p, RO | Trusted L1 can read the source; only allowlisted preferences/history are copied. Registrations, locks, temp files, symlinks, and unknown files are not imported |
| Host `~/.local/state/devbox-opencode` → guest `~/.local/state/opencode` | 9p, RW | Dedicated L1 state, mode `0700`; preserves model favorites, TUI settings/history, and the L1 service registration across recreation without sharing the host's active registration |
| `~/.config/gh` | 9p, RW (virtiofs after host validation) | Host credentials, protected from package builder |
| `~/.config/gcloud` | 9p, RW (virtiofs after host validation) | gcloud ADC/config, protected from package builder |
| `~/.config/acli` | 9p, RW (virtiofs after host validation) | Atlassian CLI config, protected from package builder |
| `~/.config/gws` | 9p, RW (virtiofs after host validation) | Google Workspace CLI config, protected from package builder |
| `/var/lib/devbox-toolbuilder` | VM-local | npm/uv/Rust installs, Playwright browser, Semble model; guest can use installed binaries but cannot modify packages |
| `~/.cache/{go,uv,semble/index}` | VM-local | Guest-writable Go, uv, and Semble index caches |
| `/usr/local/node` | VM-local | Manifest-pinned Node.js and npm runtime |
| `~/.cargo` | VM-local | Guest-local Cargo cache; Rust toolchain is read from builder install |
| `~/.local/share/kubebuilder-envtest` | VM-local | `setup-envtest` default asset store |
| `~/.local/state/devbox-toolchain` | VM-local | The Source MCP server virtualenv |
| `~/.local/share/devbox-toolchain` | VM-local | Provisioning metadata and fingerprints |
| `~/.gitconfig` | VM-local | Git identity, HTTPS rewrite |
| `/tmp/opencode` | VM-local | `root:root` `01777` scratch; tasks `0700` |

### Temporary task scratch

`/tmp/opencode` is a guest-local, disposable scratch parent. Its root-owned
`01777` mode lets the guest and isolated toolbuilder create separate entries;
the sticky bit prevents one account from removing or renaming entries owned by
the other. Provisioning normalizes the parent directory's owner and mode without
changing existing children. Keep each task directory private:

```shell
task_dir="$(mktemp -d /tmp/opencode/task.XXXXXXXX)"
chmod 0700 "$task_dir"
```

Entry names are visible to local accounts, and mode `0700` does not isolate
concurrent processes running as the same guest user. Keep sensitive data out of
directory names and do not treat task directories as a cross-session boundary.

This path is not host-mounted or persistent. Treat its contents as temporary;
do not put project files, caches, or other data there that must survive VM
cleanup, reset, or recreation.

OpenCode's state directory combines TUI/model/history data with the
single-owner service registration, so those files cannot be mounted
independently. Provisioning seeds the dedicated L1 host directory once from
an explicit allowlist of preference/history files in the read-only host state
mount and never overwrites existing L1 data. The trusted L1 user can read the
source mount, including the host's live service registration, but that file is
not copied. The seed is one-way; later host preference changes do not flow
into L1, and L1 state is not shared with nested L2s. Remove
`~/.local/state/devbox-opencode` only when intentionally resetting that
persistent L1 OpenCode state. The complete policy and validation limits are in
[`mount-policy.md`](mount-policy.md).

The default deployment uses one L1 per host home. `DEVBOX_LIMA_INSTANCE`
changes the Lima instance name but does not namespace the persistent state
directory; do not run multiple L1 instances concurrently from the same host
home.

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
filesystem coherency for OpenCode's production SQLite data. The issue #287
fresh-VM CI test runs the bounded SQLite probe before the VM suite, alongside a
bidirectional file-visibility smoke. It uses a uniquely named test-only WAL
database in the disposable host data directory. L1 checks the mounted database
first; CI stops L1, copies the database and optional WAL into a private
host-only snapshot, and checks exact rows plus `PRAGMA integrity_check` from
both sides. This exercises disposable cross-boundary SQLite concurrency only;
it does not establish production database integrity, verify CodeBurn's
end-to-end session parsing, or validate other mount types. John manually
reported that CodeBurn running on the physical host displayed the current L1
session, confirming basic manual session visibility. CI does not launch
CodeBurn or automate session parsing, and it uses only disposable data. Keep
the session data mount unchanged and do not generalize the probe to production
sessions or other filesystems/mount configurations. The full template now
provisions Semble and its VM-local model cache. TUI session-switching UX and
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
- **Red Hat internal TLS trust**: three CA roots are SHA-256 pinned in the
  manifest and embedded provisioner, then installed into the VM trust store
  for system tools, Node.js, and The Source.
- **Nested VMs**: pinned `limactl` plus `qemu-kvm`, `qemu-img`, and
  `edk2-ovmf` inside the guest.
- Git configured for GitHub over HTTPS (SSH remotes rewritten, `gh` as
  the credential helper), identity seeded once from the host.

Every tool installed at a manifest-pinned version declares a `lima` consumer
in `lima/tool-versions.json`. Provisioning reads those versions and
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
      present; VM-owned files take precedence at those skill names.
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
- [ ] From the host, `cd ~/src/my-sandbox && devbox opencode` starts a
      session using the generated runtime overlay; the background service
      persists across project sessions. Do not start it with bare `opencode`
      from a VM shell.
- [ ] Nested virtualization: start a small throwaway L2 VM
      (for example `limactl start template://alpine`), confirm it boots,
      then `limactl delete` it.
- [ ] `limactl stop devbox && limactl start devbox` — everything above
      still works.
