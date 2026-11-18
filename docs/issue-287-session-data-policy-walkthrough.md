# Issue 287: OpenCode session-data sharing policy

<!-- Captured with Showboat 0.6.1 on 2026-11-18 -->
<!-- showboat-id: 6a110579-7e9b-4e9e-b7d3-ef7ae6ed4449 -->

Why: Issue #287 asked how the host and the L1 devbox VM may share the OpenCode
session-data directory `~/.local/share/opencode`. The host needs read access
to it for session history, logs, and CodeBurn. The evidence is limited. A 9p
contention probe failed on 2026-10-02: the host held a SQLite
`BEGIN IMMEDIATE` transaction, and the L1 still acquired the same lock. So
concurrent host and L1 writers are not validated on 9p. Virtiofs is not an
option for this mount. PR #512 recorded the #317 gid-mapping blocker and kept
9p. The one physical-host virtiofs attempt failed with `EINVAL` on guest
creates before any SQLite probe ran. So there is no virtiofs SQLite result.
The user decided the policy: keep the read-write mount; the L1 `devbox
opencode` service is the only writer; the host is read-only at all times;
manual log removal is the only host write; the rule is a convention, and the
mount does not enforce it.

What changed: `lima/mount-policy.md` gains the section `## Session-data
sharing policy (#287)`. It records the rule, the evidence, and the limits.
`lima/README.md` records the decision in the mount table and replaces the old
open/blocked note. `lima/devbox.yaml` states the rule in a comment above the
mount. The mount itself is unchanged. Two older walkthroughs now cite
sections by name instead of line numbers. New unit tests pin the policy text.
No mount setting changed. No runtime behavior changed.

## Source map

- Policy section: `lima/mount-policy.md`, section
  `## Session-data sharing policy (#287)`.
- README decision: `lima/README.md`, sections `## Shared vs VM-local state`
  and `### OpenCode data-sharing checkpoint (#269)`.
- Template comment and mount entry: `lima/devbox.yaml:105-110`.
- Unit pins: `test_mount_policy_decides_shared_session_data_policy` and
  `test_lima_readme_and_template_state_the_session_data_single_writer_rule`
  in `tests/unit/test_lima_template.py`.
- Older walkthroughs with name-based citations:
  `docs/issue-317-virtiofs-gid-walkthrough.md`,
  `docs/issue-486-virtiofs-symlink-walkthrough.md`.

All commands below run from the repository root of the worktree on branch
`issue-287-opencode-session-data-policy`. The next command prints the branch
base.

```bash
git rev-parse --short origin/main
```

```output
cc69102
```

This is the base commit. Commits on this branch do not move `origin/main`,
so this output stays stable.

```bash
git diff --stat origin/main...HEAD -- lima tests
```

```output
 lima/README.md                   | 11 ++++---
 lima/devbox.yaml                 |  8 +++--
 lima/mount-policy.md             | 68 ++++++++++++++++++++++++++++++++++----
 tests/unit/test_lima_template.py | 71 ++++++++++++++++++++++++++++++++++++++++
 4 files changed, 146 insertions(+), 12 deletions(-)
```

The `-- lima tests` path limit excludes this walkthrough document, so the
block stays stable after the walkthrough commit.

The policy section in `lima/mount-policy.md` is the core of the change. This
command prints the section from its heading to the next `##` heading.

```bash
awk '/^## Session-data sharing policy \(#287\)$/{found=1} found && /^## / && !/^## Session-data sharing policy \(#287\)$/{exit} found' lima/mount-policy.md
```

```output
## Session-data sharing policy (#287)

`~/.local/share/opencode` holds the session database, snapshots, and logs.
The host needs read visibility into it for session history and CodeBurn.
Issue #287 asked how the host and the L1 may share it. The decision: keep
the writable host-to-L1 mount at `~/.host-config/local/share/opencode`, and
add one rule for the host.

**Single-writer rule.** Only one OpenCode writer exists: the L1 `devbox
opencode` service. The host treats `~/.local/share/opencode` as read-only
at all times: session inspection, logs, and CodeBurn only. Do not run a
host-side OpenCode writer against the shared database. Run only the managed
`devbox opencode` service in the L1. Do not start a second OpenCode service
in the guest or on the host against the shared database. The host may
remove old log files manually; that is the only host-side write. The mount
stays writable because the L1 is the writer and the host reads. The mount
does not enforce the host's read-only side; the rule is a convention.

The evidence behind the rule:

- The 9p SQLite contention probe failed on 2026-10-02: the host held
  `BEGIN IMMEDIATE` while the L1's competing `BEGIN IMMEDIATE` succeeded.
  Concurrent host/L1 WAL writers are not validated on 9p.
- Virtiofs is not an option for this mount today. The #317 gid-mapping
  blocker, recorded by PR #512, keeps the template on 9p. The one
  physical-host virtiofs attempt failed with `EINVAL` on guest creates
  before the SQLite probe ran, so there is no virtiofs SQLite result.

The evidence does not establish that production session data stays intact
under concurrent writers. It does not establish that host reads during L1
writes are safe. It does not automate CodeBurn session parsing. No test
proves these.

The rule restrains the host, but the risk runs in both directions. The L1
holds the mount writable, so it can read, write, and delete the host
session data at all times, even when no host process runs. Back up the
host directory if the session history matters.

A host writer plus the running L1 service is an unsupported combination.
A violation can corrupt or lose session data. The user accepted this risk
for read-only host use.

Consequences: the L1 workflow keeps using `devbox opencode`; host session
history and CodeBurn stay available through the mount.

The readiness probe covers the mount's presence, the guest symlink shape,
and the guest-only parent ACL. It does not validate the host source path,
writability, or concurrency. The probe checks the guest link, not the host
source path. A symlinked host location is followed on 9p (see #486).

Revisit this policy when Lima supports gid mapping or passes the virtiofsd
translate flags, a physical-host virtiofs SQLite probe succeeds (#317), and
the host mount location resolves to a non-symlink path (#486).

```

The rule appears in the three files that govern the mount: the policy, the
README, and the template comment.

```bash
grep -n "single writer\|single-writer\|read-only at all times" lima/mount-policy.md lima/README.md lima/devbox.yaml
```

```output
lima/mount-policy.md:18:| `~/.local/share/opencode` | `~/.host-config/local/share/opencode`, RW | Keep. Contains the session database and snapshots used for host-visible session history and CodeBurn usage metrics. The L1 service is the single writer; the host is read-only at all times (convention, not enforced by the mount). See "Session-data sharing policy (#287)" below. | Real L1 data is not mounted. Ordinary L2 tests keep data on the VM-local disk; full-provisioning smokes use a disposable temporary source. |
lima/mount-policy.md:333:  stays the only writer, and the host stays read-only at all times.
lima/README.md:802:| `~/.local/share/opencode` | 9p, RW (virtiofs blocked: #317 gid mapping) | Mounted under `~/.host-config/local/share/opencode`; the L1 service is the single writer, and the host must not write it (convention, not enforced by the mount; manual log removal is the only host write). See "Session-data sharing policy (#287)" in `mount-policy.md` |
lima/README.md:988:the L1 `devbox opencode` service is the single writer, and the host treats
lima/README.md:989:the directory as read-only at all times. The rule, its evidence, and its
lima/devbox.yaml:105:  # #287 single-writer rule: the L1 devbox opencode service is the only writer
lima/devbox.yaml:106:  # of the shared session data; the host is read-only at all times
```

The mount entry itself is unchanged. This check runs the project Python
environment and parses `lima/devbox.yaml` with PyYAML. It prints the
`~/.local/share/opencode` mount entry. It proves that `writable` is still
`true` and that the mount point is unchanged.

```bash
env UV_PROJECT_ENVIRONMENT="$HOME/.venvs/my-sandbox-issue-287" uv run python - <<'PY'
import yaml

with open("lima/devbox.yaml") as f:
    template = yaml.safe_load(f)

entry = [m for m in template["mounts"] if m["location"] == "~/.local/share/opencode"]
assert len(entry) == 1, f"expected 1 entry, found {len(entry)}"
mount = entry[0]
print("location:  ", mount["location"])
print("mountPoint:", mount["mountPoint"])
print("writable:  ", mount["writable"])
assert mount["writable"] is True
assert mount["mountPoint"] == "{{.Home}}/.host-config/local/share/opencode"
print("mount unchanged: writable stays true, mount point stays the same")
PY
```

```output
location:   ~/.local/share/opencode
mountPoint: {{.Home}}/.host-config/local/share/opencode
writable:   True
mount unchanged: writable stays true, mount point stays the same
```

The old wording kept issue #287 open and blocked. The decision replaces it.
This check fails if the old wording still appears in the repository
documentation. It excludes this walkthrough, which quotes the old wording.

```bash
if grep -rn --exclude="issue-287-session-data-policy-walkthrough.md" "remains open/blocked" lima/ docs/; then echo "UNEXPECTED: old wording still present"; exit 1; else echo "OK: the old open/blocked wording no longer appears in lima/ or docs/"; fi
```

```output
OK: the old open/blocked wording no longer appears in lima/ or docs/
```

The unit tests pin the policy text. This run covers the Lima template suite
and the two suites that guard the session-data mount checks. The pytest
timing is nondeterministic, so the command strips it with
`sed -E "s/ in [0-9.]+s//"`. Nothing else is filtered.

```bash
set -o pipefail
env UV_PROJECT_ENVIRONMENT="$HOME/.venvs/my-sandbox-issue-287" \
  uv run --extra test pytest -q \
  tests/unit/test_lima_template.py \
  tests/unit/test_run_vm_ci_mount_type.py \
  tests/unit/test_host_sqlite_mount_validation.py 2>&1 |
  sed -E "s/ in [0-9.]+s//"
```

```output
........................................................................ [ 54%]
............................................................             [100%]
132 passed
```

Pre-commit passes on every file changed by the commit.

```bash
pre-commit run --files lima/mount-policy.md lima/README.md lima/devbox.yaml docs/issue-317-virtiofs-gid-walkthrough.md docs/issue-486-virtiofs-symlink-walkthrough.md tests/unit/test_lima_template.py
```

```output
check for added large files..............................................Passed
check json...........................................(no files to check)Skipped
check for merge conflicts................................................Passed
check for broken symlinks............................(no files to check)Skipped
check toml...........................................(no files to check)Skipped
check xml............................................(no files to check)Skipped
debug statements (python)................................................Passed
fix end of files.........................................................Passed
fix utf-8 byte order marker..............................................Passed
trim trailing whitespace.................................................Passed
no unicode replacement chars.............................................Passed
yamllint.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
markdownlint-cli2........................................................Passed
Validate Mergify configuration location..................................Passed
Validate Mergify configuration.......................(no files to check)Skipped
renovate-config-validator............................(no files to check)Skipped
shellcheck...........................................(no files to check)Skipped
Detect secrets...........................................................Passed
check json5..........................................(no files to check)Skipped
zizmor...............................................(no files to check)Skipped
Validate canonical tool versions.....................(no files to check)Skipped
Test Renovate JSONata datasource transforms..........(no files to check)Skipped
```

## Limits

No runtime behavior changed. The change is documentation, a template
comment, and unit tests. So this walkthrough has no interactive demo. The
recursive L2 test is not run here; CI runs the recursive tier. The policy is
a convention. The mount stays writable, and the mount does not enforce the
host's read-only side. The evidence limits in the policy section still hold:
no test validates concurrent host and L1 writers, and there is no virtiofs
SQLite result.

## Reproduce

From a checkout of commit `3777425` on branch
`issue-287-opencode-session-data-policy`, run the recorded commands from the
repository root. The recorded outputs were captured at commit `3777425`,
before this walkthrough was committed. The `uv` commands require
`UV_PROJECT_ENVIRONMENT` set to a VM-local environment path, as shown in
each command. Bootstrap that environment once with `uv sync --extra test`.
Re-run all recorded commands with `uvx showboat verify
docs/issue-287-session-data-policy-walkthrough.md` from the repository root.
