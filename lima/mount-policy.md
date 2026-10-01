# Lima mount policy

This is the decision matrix for the default L1 devbox and nested L2 test/dev
VMs. The L1 is a trusted, single-user workspace. Third-party package builds
run as a separate account and cannot traverse its protected host mounts. L2s
are disposable test environments: they receive only the inputs needed by a
test, never the L1's credentials, OpenCode data, or state. **Neither policy
mounts all of `$HOME`.**

## Mount decision matrix

| Host path | L1 destination / access | Consumers and decision | L2 policy |
| --- | --- | --- | --- |
| `~/src` | Same absolute path, RW | Keep. Projects and worktrees need same-path absolute `.git` pointers; the host launcher also resolves the caller's work directory here. Parent ACL limits access to the guest UID. | Do not stack this mount. Tests copy the checkout to L2-local disk (see #268); only an individual test input may be mounted read-only. |
| `~/kb` | `~/.host-config/kb`, RW; `~/kb` and the `KbPath` absolute alias point to it | Keep. `kbase.py` and notes are updated from the VM, and KB worktree `.git` pointers use the host absolute path. The canonical shell/file-search path is `~/kb`; the repository does not provide a `knowledge-base/` symlink. Readiness checks the alias and a readable `kbase.py`. | Not mounted. KB notes and worktrees are not needed by nested tests. |
| `~/.agents` | `~/.host-config/agents`, RO | Keep. Host skills are inputs to the guest-local skill overlay. The package-builder account cannot traverse `.host-config`. | Not mounted; L2 tests use only explicitly staged test inputs. |
| `~/.config/opencode` | `~/.host-config/config/opencode`, RW | Keep. Contains shared OpenCode configuration, provider authentication, agents, commands, plugins, and settings. | Not mounted. No host provider/config secrets in test VMs. |
| `~/.local/share/opencode` | `~/.host-config/local/share/opencode`, RW | Keep. Contains the session database and snapshots used for host-visible session history and CodeBurn usage metrics. Host/VM SQLite coherency remains subject to the validation caveat below. | Not mounted. OpenCode data in L2 remains on its VM-local disk. |
| `~/.local/state/opencode` | `~/.host-config/local/state/opencode-seed`, RO | Keep only as a one-time import source for existing host preferences/history. The trusted L1 user can read the source, including its service registration; only the explicit preference/history allowlist is copied, so registrations, locks, temp files, symlinks, and unrecognized future files are not imported. | Not mounted. |
| `~/.local/state/devbox-opencode` | `~/.host-config/local/state/devbox-opencode`, RW, mode `0700` | Keep. This dedicated host-backed directory is the L1's persistent `~/.local/state/opencode` target. It preserves L1 model favorites, TUI settings, prompt history, and its own service registration across VM recreation without sharing the host's default registration. The one-time host seed does not continuously sync back to the host default state directory. | Not mounted. Every L2 keeps its own independent VM-local XDG state. The recursive test starts two actual OpenCode services concurrently and checks that both registrations remain stable. |
| `~/.config/gh` | `~/.host-config/config/gh`, RW | Keep. Authenticated `gh` is the canonical GitHub interface inside the VM. | Not mounted. |
| `~/.config/gcloud` | `~/.host-config/config/gcloud`, RW | Keep. Required by the installed Google Cloud tooling; includes ADC config. | Not mounted. |
| `~/.config/acli` | `~/.host-config/config/acli`, RW | Keep. Required by the installed Atlassian CLI. | Not mounted. |
| `~/.config/gws` | `~/.host-config/config/gws`, RW | Keep. Required by the installed Google Workspace CLI. | Not mounted. |

The paths below remain L1-local and are not host mounts: the toolbuilder and
its package caches, `~/.cache/{go,uv,semble}`, `~/.cargo`,
`~/.local/share/kubebuilder-envtest`, `~/.local/share/devbox-toolchain`, and
`~/.local/state/devbox-toolchain`. Worktree Python environments should use a
unique VM-local `UV_PROJECT_ENVIRONMENT` rather than the shared checkout.

The persistent `~/.local/state/devbox-opencode` mount is for the repository's
single L1 instance per host home. `DEVBOX_LIMA_INSTANCE` changes the Lima name,
not the backing state directory; do not run multiple L1 instances concurrently
with the same host home. Disposable L2 VMs do not receive this mount.

## OpenCode state lifecycle

OpenCode v2 derives a single state root from `XDG_STATE_HOME` and keeps both
the service registration and TUI/model/history state below
`~/.local/state/opencode`. Those files cannot be mounted independently: the
service registration must be single-owner, while user preferences must survive
recreating the VM. The L1 therefore links that standard path to its own
host-backed `~/.local/state/devbox-opencode` directory. It imports files once
from an explicit allowlist: `model.json`, `session.json`,
`prompt-history.jsonl`, `prompt-stash.jsonl`, `kv.json`, `tui.json`,
`frecency.jsonl`,
`latest/tui/tabs.json`, and `latest/tui/plugin.*.json`. Service registrations,
locks, temp files, symlinks, and unrecognized files are never imported.
Existing L1 files are never overwritten and a marker prevents later boots from
reseeding. The state directory is mode
`0700`; the persistent state mount is behind the protected `.host-config`
parent and is not available to the package builder.

The seed is intentionally one-way: edits made later by the host's regular
OpenCode process do not overwrite the L1's preferences, and L1 service
registrations are not shared with that process. The separately mounted data
directory remains the shared location for OpenCode sessions. The dedicated
`~/.local/state/devbox-opencode` directory persists across `devbox --reset`,
`devbox --delete`, and manual Lima recreation; remove that exact directory only
when intentionally resetting the L1's persisted OpenCode preferences/history.

## Validation notes

- Template tests compare the configured mount set with provisioning links and
  readiness checks; the inline Lima readiness probe is kept byte-for-byte in
  sync with `probe-readiness.sh`.
- The provisioned-VM test reads a KB sentinel through the canonical guest
  `~/kb` path, in addition to checking the live mount and absolute worktree
  alias. A missing or unreadable KB mount is a readiness failure.
- The fresh-VM CI tier checks that a sentinel in the host's disposable
  OpenCode data directory is readable in L1 and that an L1 write is visible
  back on the host. It never touches production session data.
- The recursive test mounts only a read-only OpenCode executable into each
  disposable L2. It starts two L2 VMs concurrently, starts a managed OpenCode
  service in each, checks distinct and stable registrations over a 15-second
  soak, and verifies their state files are on each L2's local block filesystem
  rather than a shared host/L1 mount.
- The shared session-data mount is retained for host visibility and CodeBurn.
  Guest-to-guest SQLite and session visibility have prior coverage (#269), and
  CodeBurn's provider reads `~/.local/share/opencode`; this VM-only test
  environment cannot launch the host CodeBurn process or verify its end-to-end
  session parsing. The disposable host/L1 test checks file-level mount
  visibility, not concurrent host/VM SQLite integrity. Do not interpret this
  as evidence that concurrent host/VM SQLite writers are safe.
