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
The host launcher forwards EnMaaS only when `ENMAAS_URL` and `ENMAAS_API_KEY`
are both non-empty and the URL uses HTTPS. The `devbox opencode` launch path
validates the full URL before OpenCode starts; malformed endpoints fail closed.
The endpoint must be an ASCII URL with a valid DNS or IP literal hostname.
Numeric or hexadecimal-looking DNS suffixes and scoped IPv6 addresses are
rejected to avoid URL-parser interpretation differences. With the complete
pair, direct provider keys and the Anthropic base URL are removed from the guest
environment. Provider and model settings are user-managed in
`~/.config/opencode/opencode.jsonc`, not generated by devbox. For each provider
routed through EnMaaS, keep the endpoint and gateway key together in its
`settings` object:

```jsonc
"env": [],
"settings": {
  "baseURL": "{env:ENMAAS_URL}",
  "apiKey": "{env:ENMAAS_API_KEY}"
}
```

Devbox validates the forwarded endpoint but does not inspect or rewrite this
user-managed provider mapping. `env: []` disables ordinary provider-key
fallback, so those entries will not use direct provider credentials when the
EnMaaS pair is absent. A project `opencode.json(c)` can override the global
provider endpoint while `{env:ENMAAS_API_KEY}` still resolves from the forwarded
environment, so only use the key with trusted projects. The inline overlay's
baseline policies and permissions remain higher-precedence; provider routing
is intentionally user/project-managed. To keep `do-one-issue` working, define
`rits/zai-org/glm-5-3` under `providers.openai.models` in the global config. The
tested model entry is:

```jsonc
"models": {
  "rits/zai-org/glm-5-3": {
    "name": "GLM 5.3 (curvebender)",
    "limit": { "context": 262000, "output": 128000 },
    "capabilities": {
      "tools": true,
      "input": ["text"],
      "output": ["text"]
    },
    "variants": [
      { "id": "low", "settings": { "effort": "low" } },
      { "id": "high", "settings": { "effort": "high" } },
      { "id": "max", "settings": { "effort": "max" } }
    ]
  }
}
```

After creating and validating the VM, enable optional host-login autostart to
avoid starting it manually after reboot:

```shell
limactl autostart enable devbox
```

OpenCode's managed background service inherits these variables when it
starts. Provisioning intentionally does not pre-start the service without
credentials; the first `devbox opencode` command starts it with the generated
runtime config. A bare `opencode` command from an arbitrary VM shell bypasses
the config generator and guest-side full URL validation, so do not use it to
start the managed service. If you did, stop that service once from the VM
(`opencode service stop`) before retrying with host-side `devbox opencode`.

When the command is `opencode` (including `opencode run`), the helper builds
`OPENCODE_CONFIG_CONTENT` inside the VM immediately before starting the CLI.
The generated overlay keeps the baseline provider-use policies and permission
rules, always enables the local Semble MCP, and gates Context7, The Source,
and Tavily websearch on their complete credential groups. The overlay does not
generate provider or model entries; the EnMaaS pair is still forwarded to
OpenCode when complete and validated before launch. Generated integration
credentials use `{env:NAME}` references only, so secret values stay in the
allowlisted process environment. PriceTag credentials remain available only
for separate utilities such as `list-models.sh` and the opt-in gateway
inference test; they do not generate OpenCode providers. The overlay is
in-memory only and does not edit the host-shared `~/.config/opencode`
configuration.

The dedicated L1 OpenCode state preserves model favorites across VM recreation.
Favorites that refer to the removed `pricetag-hosted` or `octo-open` providers
are not rewritten and will no longer resolve; remove those stale favorites and
select an available provider and model from your user OpenCode configuration.

The Source MCP uses the committed `lima/the-source/uv.lock` dependency graph
and an immutable source commit. Its wrapper installs that locked environment
without credentials, then starts the child with only its own credentials,
`HOME`, and a minimal `PATH`; OpenCode's unrelated provider tokens are not
inherited by that process.

GitHub operations use the forwarded `gh` CLI and host `gh` authentication.
This gh-only policy is recorded in
[issue #275](https://github.com/JohnStrunk/my-sandbox/issues/275).
Before restarting a service from an older devbox release, ensure the global
provider and model entries above are present; the new runtime overlay no longer
supplies them.
OpenCode's one VM-local service loads the runtime config at startup and serves
project sessions across the same-path `~/src` mount. If credentials change while
the service is running, stop and restart the service from a `devbox` shell so it
inherits the updated environment. Changes to the global config or generated
overlay also require a restart for the new settings to take effect. If the
service still has an older generated provider config from a previous devbox
release (PriceTag/OCTO or EnMaaS) loaded, restart it once to discard that
config; this interrupts active sessions:

```shell
devbox bash -lc 'opencode service stop'
devbox opencode
```

### Toggle managed OpenCode debug logging

Use the top-level `devbox` flags before the command to change the persistent
log level on the shared managed service:

```shell
devbox --debug opencode                         # enable debug for all service sessions
devbox --debug opencode run "reproduce the issue" # headless OpenCode run
devbox --no-debug opencode                      # disable debug logging again
```

With no stored preference, debug logging is off by default. `--debug` starts
the managed service with `OPENCODE_LOG_LEVEL=DEBUG`; `--no-debug` starts it
without that override. With neither flag, devbox leaves the preference and
active service unchanged. It refuses to launch if the persisted service config
or an already-running service conflicts with the stored preference, rather than
silently changing the setting or claiming it is active. The preference is
stored in the dedicated devbox OpenCode state directory, not the host-shared
OpenCode configuration, and survives VM recreation. It applies to every project
session served by this VM-local service and is not a host environment variable,
so the strict host-to-VM allowlist is unchanged. If the host-shared OpenCode
service configuration contains a conflicting persisted `OPENCODE_LOG_LEVEL`,
devbox prints the `opencode service unset env OPENCODE_LOG_LEVEL` recovery
command rather than claiming the request took effect.

When an explicit flag does not match the running service process's actual log
level, devbox warns and stops the service; this may interrupt active sessions.
The OpenCode command being launched then starts it with the requested setting.
If the service already has the requested level, it stays running. The
preference persists for later OpenCode invocations until the opposite flag is
used. When a flag is used with a shell or another command, devbox also exports
the selected level into that command's guest environment. A debug-only lifecycle
invocation stores the preference without
opening a shell; a non-OpenCode command does not start the service, so the
setting takes effect on the next OpenCode invocation.

The VM writes logs to `~/.local/share/opencode/log/opencode.log`; this directory
is a writable host mount, so the host-side path is also
`~/.local/share/opencode/log/opencode.log`. Logs remain on the host after debug
logging is disabled or the VM is reset/deleted. `--no-debug` does not erase
existing logs; remove them manually when they are no longer needed. Before
launching OpenCode, devbox restricts the host-shared log directory to mode
`0700` and the log file to `0600`. This keeps logs private even if the managed
daemon creates or replaces a log file with broader default permissions.
OpenCode logs are sensitive at every log level and may include prompts, process
arguments, file paths, and file content; debug mode adds more detail.
Review and redact logs before sharing them; remove credentials, authorization
headers, and other sensitive data.

The schema-drift test runs against the manifest-pinned VM binary and an isolated
home/config directory, leaving the mounted global config untouched:

```shell
cd ~/src/my-sandbox
./scripts/sanitized-test.sh --guest-vm -- \
  env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  uv run --extra test pytest -q tests/unit/test_lima_opencode_config.py
```

The test is skipped outside the provisioned VM; there it asserts that
`opencode --version` matches the tool manifest and that `opencode debug config`
accepts the generated overlay alongside a representative user-managed EnMaaS
provider/model config.

`do-one-issue` keeps its Git and GitHub work in the VM: the host-side script
uses `devbox` only to run `git switch/pull` and the headless `devbox opencode
run --agent build ... --file .opencode/commands/grab-issue.md` command at the
mounted checkout path. The task instructions handle issue assignment, worktree
implementation, PR creation, and the documented CI/merge wait using the VM's
forwarded `gh` authentication. Its headless run selects
`openai/rits/zai-org/glm-5-3`, so that model must be defined in the user-managed
global OpenCode config; devbox no longer supplies it.

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

### Docker CE and Podman runtimes

The VM provisions **Docker CE Engine, its official CLI, Docker's rootless
extras, and `containerd.io`** from Docker's Fedora stable RPM repository. The
manifest pins Docker CE 29.8.2 and containerd 2.3.6 for both supported
architectures. The RPM release suffix is resolved from the official repo for
Fedora 44; package versions and epochs are checked after installation. DNF
verifies repository metadata and package signatures with a committed,
SHA-256-pinned Docker key
(`060A 61C5 1B55 8A7F 742B 77AA C52F EB6B 621E 9F35`, `gpgcheck=1`,
`repo_gpgcheck=1`).
Provisioning skips third-party RPM scriptlets as root. Docker CE is not a Podman
alias, wrapper, or `podman-docker` package.

Rootless mode requires `newuidmap`/`newgidmap`, cgroup v2, and at least 65,536
subordinate UIDs and GIDs. Lima supplies the subordinate-ID ranges and cgroup
delegation; the readiness probe checks the prerequisites. The provisioner writes
Docker's documented per-user systemd unit directly instead of running the
vendor setup utility with the guest's host-mounted credentials. The optional
`docker-buildx-plugin` and `docker-compose-plugin` RPMs are not installed, so
`docker buildx` and `docker compose` are unavailable by default.

Docker uses a rootless **per-user systemd service**. Provisioning enables
`docker.service`; Lima's user lingering lets it start at VM boot and survive
logout. The default `DOCKER_HOST` is
`unix:///run/user/<uid>/docker.sock`. The rootful system `docker.service`,
`docker.socket`, and `containerd.service` remain disabled, and the guest is not
added to a `docker` group.
The daemon runs with the guest user's privileges, not host-root privileges, and
can access files and credentials available to that guest user.

Podman remains a separately supported rootless runtime. Its API socket is
`$XDG_RUNTIME_DIR/podman/podman.sock`; invoking `podman` talks to Podman
directly.
Do not point Docker at that socket or expect Docker to fall back to Podman.
`lima/validate-kind.sh` defaults to the Docker provider and selects the
matching socket when `KIND_EXPERIMENTAL_PROVIDER=podman` is explicitly set.
For direct kind commands, select the provider explicitly; kind's Podman
provider invokes the `podman` CLI directly, while its Docker provider uses the
Docker CLI and the default Docker CE socket.
Docker and Podman keep separate image, container, and network stores; repull or
explicitly save/load images when moving between runtimes.

Verify both runtimes and their separation with:

```shell
devbox-toolchain-check            # checks the pinned Docker CLI version
docker version                    # reports both client and Docker CE server
rpm -qf /usr/bin/docker /usr/bin/dockerd
systemctl --user status docker.service
podman info
```

The readiness probe separately checks the Docker user service/socket and the
manifest-pinned Docker server API version, then pings Podman's own socket. The
provisioned-VM test creates, runs, and removes a container through Docker CE,
then creates and deletes a kind cluster with the explicit Docker provider while
the Podman service and socket are stopped. For troubleshooting, inspect
`systemctl --user status docker.service` and
`journalctl --user -u docker.service`; the Docker and Podman sockets must remain
distinct.

The strict kind/Minikube driver matrix is tracked in
[#319](https://github.com/JohnStrunk/my-sandbox/issues/319), and Minikube
provisioning is tracked in
[#318](https://github.com/JohnStrunk/my-sandbox/issues/318).
Their Docker cases must explicitly select the Docker driver/provider and use
this Docker CE endpoint; their Podman cases select Podman directly. No case may
silently substitute one backend for the other.

Changes to embedded provisioning scripts or `lima/devbox.yaml` require
[recreating the VM](#recreating-the-vm). A manifest-only Docker version bump is
applied on VM restart; user provisioning enables and restarts the Docker service
after root provisioning so its server uses the installed package pin. VMs
created before Docker CE support must be recreated before
`devbox-toolchain-check` can verify the new live manifest; `--reprovision` alone
does not update their embedded scripts or checker.

If provisioning refuses to replace a non-managed
`~/.config/systemd/user/docker.service`, inspect the file and
`systemctl --user cat docker.service` before changing it. If it is safe to give
that unit name to devbox, stop and disable it, remove only the conflicting unit
file, then run `devbox --reprovision` so provisioning can install its managed
rootless unit. Do not overwrite an existing service you still need. VMs created
before Docker CE support must instead be recreated from the current template:
run `devbox --delete`, then `devbox`; `--reprovision` cannot replace their
embedded provisioners or toolchain checker.

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

The production/default template remains on 9p because Lima's Rust `virtiofsd`
exited before guest startup during the direct host-to-VM attempt in this
environment. The #268 benchmark recommends virtiofs for the full Linux/QEMU
template after a direct host-to-VM mixed-write check passes. The command below
runs the #287 disposable SQLite probe with an opt-in mount type; it is not
the full mixed-write benchmark from #268.

Run it on the physical/outer Linux host with Lima/QEMU. Do not run it inside
the devbox L1, where it starts a nested L2 and tests a nested boundary instead
of the physical-host-to-L1 filesystem boundary. Use either invocation:

```shell
DEVBOX_VM_TEST_MOUNT_TYPE=virtiofs scripts/run-vm-ci.sh vm
```

or an optional sanitized-wrapper invocation:

```shell
DEVBOX_VM_TEST_MOUNT_TYPE=virtiofs \
  scripts/sanitized-test.sh -- scripts/run-vm-ci.sh vm
```

This runner-only override applies to the disposable `vm` tier, not the normal
`devbox` launcher. Linux/QEMU virtiofs requires the Rust `virtiofsd`; the
QEMU-packaged `qemu-virtiofsd` is not sufficient. Startup failure is fatal and
never falls back to 9p. `run-vm-ci.sh` validates this non-secret runner control
and unsets it before child/guest commands. The sanitizer preserves it when
wrapping the runner, including a set-empty value.

The override is passed only to the first `limactl start`, when the disposable
instance is created; that create-time mount choice persists when the SQLite
probe stops and restarts the same instance. With the variable unset, the
temporary VM uses the template's 9p default and the runner does not assert a
type (the template test pins 9p). A set-empty value is rejected; only exact
`9p` and `virtiofs` values are accepted, and the `recursive` tier rejects any
set override. For an explicit override, runner output logs the requested type
at VM creation, then records both `requested` and `effective` types after the
first start and after the probe's stop/restart. It queries `findmnt` for the
L1's `~/.local/share/opencode` filesystem type and fails unless it exactly
matches the request. None of this changes `lima/devbox.yaml`, the
production/default mount type, or the CI workflow default.

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
  ./scripts/sanitized-test.sh --guest-vm --vm-lock -- \
    env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
    ./scripts/run-unit-tests.sh
  ```

  See [test-runtime.md](../docs/test-runtime.md) for measured runtime/resource
  baselines and the shared-VM concurrency model.

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
fresh-VM `vm` tier runs a bounded SQLite probe before the VM suite, alongside a
bidirectional file-visibility smoke. It uses a uniquely named test-only WAL
database in the disposable host data directory. On a passing contention check,
the runner stops L1, copies the database and optional WAL into a private
host-only snapshot, and checks exact rows plus `PRAGMA integrity_check` from
both sides.

The 2026-10-02 outer-host run with the default 9p mount failed because the host
held `BEGIN IMMEDIATE` while L1's competing `BEGIN IMMEDIATE` succeeded. The
helper rolled back that unexpected L1 transaction before any additional writes.
No production data was used, and the exact lock-versus-WAL/SHM mechanism remains
unknown. Concurrent host/L1 WAL writers must not be treated as safe or
validated on this 9p stack; the SQLite assertion remains enabled. GitHub's VM
tier was skipped because `/dev/kvm` was unavailable, and CI has no physical-host
integration job for this boundary.

John's manual CodeBurn observation was that a completed L1 session appeared in
CodeBurn on the physical host. That confirms basic completed-session
visibility only; it was not concurrent-read testing, does not automate
CodeBurn's session parsing, and does not prove production SQLite integrity.
Issue #287 remains open/blocked until a successful opt-in virtiofs run or an
explicit policy decision resolves the mount question. Keep the session-data
mount unchanged and do not generalize these disposable checks to production
sessions or other filesystems/mount configurations. The full template now
provisions Semble and its VM-local model cache. TUI session-switching UX and
model-backed conversation resume remain to be validated in the later
integration work.

## What is inside

- **Fedora 44** cloud image, digest-pinned (x86_64 and aarch64).
- **qemu/KVM**, `nestedVirtualization: true`, default `cpuType` (host),
  8 CPUs / 16 GiB RAM / 100 GiB sparse disk.
- **Rootless Docker CE and Podman**: Lima's boot scripts provide static
  `/etc/subuid` and `/etc/subgid` ranges (65,536 IDs), cgroup-v2 delegation,
  and linger. Provisioning installs Docker CE from its official
  signature-checked Fedora repository, separately pins `containerd.io`, and
  enables its rootless user service/socket. Podman/netavark remains
  independently available with its own API socket and bridge configuration;
  neither runtime aliases or falls back to the other.
- **Manifest-pinned tools**: OpenCode, Go + `devbox-go`, uv, Rust, Node/npm,
  Playwright CLI + bundled Chromium, ast-grep + its skills, Semble + prefetched
  model, Repomix, Hadolint, markdownlint-cli2, pre-commit, acli, Google
  Workspace CLI, Antigravity CLI, kind, kubectl, Helm, and Pipenv. Assets
  declaring SHA-256 integrity are verified before installation; version-only
  assets are fetched over HTTPS at their exact pinned versions without a
  manifest-level digest check.
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
integrity policies at runtime; `scripts/validate_tool_versions.py` ensures the
scripts consume every declared Lima tool, and `lima/check_toolchain.py` verifies
the installed versions.

### Tool artifact integrity

The manifest explicitly marks each downloaded tool or skill `sha256` or
`version-only`. Docker CE and containerd use version-only manifest pins, with
their official RPM packages signature-checked by DNF against the pinned Docker
key. Checksum-managed releases are
**Hadolint**, **uv**, **Antigravity CLI**, **limactl**, **kind**, the
**ast-grep release binaries**, and **acli**. Each has separate amd64 and arm64
records; their exact upstream versions and SHA-256 values are verified before
installation. The top-level
`version` remains the provisioning alias, and the validator requires both
artifact versions to normalize to it. The ast-grep agent-skill archive is also
verified against its pinned commit's SHA-256.

The v1 direct-download exceptions are **Node**, **Go**, **rustup**, **Helm**,
and **kubectl**. They retain exact HTTPS version or commit pins but are
intentionally **not SHA-256-verified**. Other npm, Python, and pre-commit
package pins also use `version-only` because the manifest does not carry their
artifact digests. Go retains Renovate's timestamped `golang-version` datasource
and the global 10-day release-age gate. Although go.dev's JSON feed publishes
Linux hashes, it lacks timestamps to join to the version datasource, so Go is
version-only in v1. acli uses Atlassian's official Homebrew formula for its
version and both architecture digests. Its feeds have no release timestamps,
so a scoped `timestamp-optional` rule lets the acli group update without the
global 10-day wait. Other dependencies keep that gate unchanged.

Version-only direct downloads rely on HTTPS and the VM trust store, which
includes the repository's internal Red Hat CA roots as well as public roots;
those internal CAs are part of the TLS integrity boundary for these pins.

The Renovate configuration gives each GitHub release architecture a separate
`github-release-attachments` dependency. For acli, it uses a separate custom
datasource per architecture to extract the Linux download URL's version and
following SHA-256 line from the official formula. Each checksum group requires
three updates, so Renovate waits for the version alias and both architectures
before opening a PR. Offline regression tests cover the regex replacement,
release-attachment mapping, configured acli JSONata transforms against a formula
fixture, and provenance verification. Only a real Mend-hosted Renovate run can
prove the hosted bot creates both architecture updates in one PR.

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
      create/delete cycles with the Docker provider; setting
      `KIND_EXPERIMENTAL_PROVIDER=podman` exercises the separate Podman socket.
- [ ] `docker version` reports the manifest-pinned client and server, and the
      Docker smoke test succeeds while Podman's service/socket are stopped.
- [ ] `~/.agents/skills/devbox-tools/SKILL.md` and the ast-grep skills are
      present; VM-owned files take precedence at those skill names.
- [ ] `HF_HOME` and `SEMBLE_CACHE_LOCATION` point under the VM-local cache,
      and Playwright's bundled Chromium launches without another download.
- [ ] Updating a tool version in the shared manifest and restarting the VM
      changes the stored fingerprint and applies the new version without a
      VM rebuild.
- [ ] Create a worktree and set `UV_PROJECT_ENVIRONMENT` to a unique
       VM-local path before `uv sync --extra test`; run the sanitized unit
       runner through `scripts/sanitized-test.sh --guest-vm --vm-lock` — tests
       pass. Do not reuse that checkout's venv from the host (see the venv
       caveat above).
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
