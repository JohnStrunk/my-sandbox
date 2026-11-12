# PATH-independent tool availability checks

<!-- Captured with Showboat 0.6.1 on 2026-11-12 -->
<!-- showboat-id: 76af8c9e-2952-4a1b-b700-5dc0591c64e0 -->

Why: A subagent reported that golangci-lint was not installed on the devbox VM.
Another subagent ran the same tool in the same session. The checker resolved
commands through the inherited PATH, which a non-login shell does not populate,
and no repository rule required proof before a claim of unavailability.

What changed: the checker now searches the canonical devbox directories
($KREW_ROOT/bin, ~/.local/bin, $CARGO_HOME/bin, the toolbuilder bin directories,
the Node and Go installs, $GOPATH/bin, the inherited PATH, then the system
directories). System provisioning also writes a root-owned
/usr/local/bin/devbox-toolchain-check wrapper, which the minimal PATH always
contains. Agents get a single-tool query (--tool), a machine-readable inventory
(--json), and a written rule that forbids an unproven claim.

Source map: path repair at lima/check_toolchain.py:144. CLI at
lima/check_toolchain.py:452-486. Wrapper at lima/provision-system.sh:951-962.
Readiness at lima/probe-readiness.sh:230. Agent rule at AGENTS.md:137. Skill
rule at lima/agent-skills/devbox-tools/SKILL.md:22. VM coverage at
tests/vm/test_provisioned_vm.py:84.

See it work. The first command shows the failure mode: a minimal PATH has no
toolbuilder bin directory, so a bare lookup fails. The second command uses the
same minimal environment and still reports the pinned version.

```bash
env -i PATH=/usr/local/bin:/usr/bin bash -c "command -v golangci-lint || echo golangci-lint-is-not-on-the-minimal-PATH"
```

```output
golangci-lint-is-not-on-the-minimal-PATH
```

```bash
env -i PATH=/usr/local/bin:/usr/bin /usr/bin/python3 lima/check_toolchain.py --manifest lima/tool-versions.json --tool golangci-lint
```

```output
OK: golangci_lint: 2.14.0
```

The second tool from issue #390.

```bash
env -i PATH=/usr/local/bin:/usr/bin /usr/bin/python3 lima/check_toolchain.py --manifest lima/tool-versions.json --tool gocyclo
```

```output
OK: gocyclo: 0.6.0
```

The machine-readable record names the binary that answered, so a report can cite
it.

```bash
env -i PATH=/usr/local/bin:/usr/bin /usr/bin/python3 lima/check_toolchain.py --manifest lima/tool-versions.json --tool golangci-lint --json | jq -c ".tools[0] | {name,pinned,installed,path,status}"
```

```output
{"name":"golangci_lint","pinned":"2.14.0","installed":"2.14.0","path":"/var/lib/devbox-toolbuilder/.local/bin/golangci-lint","status":"ok"}
```

Observed in a fresh L2 provisioned from this worktree. The task-local source
mount was read-only. The config and credential mount sources were empty, and no
L1 credentials entered the L2. The task root and the L2 were deleted after the
run. These lines come from that run and do not replay after cleanup.

The L2 reports its level, carries a root-owned wrapper, and resolves both #390
tools from a minimal environment:

```output
$ limactl shell issue-494-l2 -- cat /etc/devbox/vm-level
L2
$ limactl shell issue-494-l2 -- ls -l /usr/local/bin/devbox-toolchain-check
-rwxr-xr-x. 1 root root 282 Nov 12 16:46 /usr/local/bin/devbox-toolchain-check
$ limactl shell issue-494-l2 -- bash -ceu \
    'env -i PATH=/usr/local/bin:/usr/bin bash -c "command -v golangci-lint || echo golangci-lint-is-not-on-the-minimal-PATH"'
golangci-lint-is-not-on-the-minimal-PATH
$ limactl shell issue-494-l2 -- bash -ceu \
    'env -i PATH=/usr/local/bin:/usr/bin devbox-toolchain-check --tool golangci-lint'
OK: golangci_lint: 2.14.0
$ limactl shell issue-494-l2 -- bash -ceu \
    'env -i PATH=/usr/local/bin:/usr/bin devbox-toolchain-check --tool gocyclo'
OK: gocyclo: 0.6.0
```

The wrapper content shows the two properties that make the result trustworthy.
It runs Python in isolated mode, and it reads the root-owned manifest snapshot
rather than the guest-writable /etc/devbox symlink:

```output
$ limactl shell issue-494-l2 -- cat /usr/local/bin/devbox-toolchain-check
#!/bin/sh
# PATH-independent availability check for agents (issue #494).
# Read the root-owned manifest snapshot, not the guest-writable symlink.
exec /usr/bin/python3 -I -S /var/lib/devbox-vm/tool-assets/check_toolchain.py \
  --manifest /var/lib/devbox-vm/tool-versions.json "$@"
```

The JSON record from the same L2 run. The inherited PATH has /usr/bin and
/usr/local/bin only. The record still names the toolbuilder path.

```output
$ limactl shell issue-494-l2 -- bash -ceu \
    'env -i PATH=/usr/local/bin:/usr/bin devbox-toolchain-check --tool golangci-lint --json'
{
  "search_path": "/home/jstrunk.guest/.krew/bin:/home/jstrunk.guest/.local/bin:/home/jstrunk.guest/.cargo/bin:/var/lib/devbox-toolbuilder/.local/bin:/var/lib/devbox-toolbuilder/.cargo/bin:/usr/local/node/bin:/usr/local/go/bin:/home/jstrunk.guest/.cache/go/bin:/usr/local/bin:/usr/bin:/usr/local/sbin:/usr/sbin:/sbin:/bin",
  "inherited_path": "/usr/local/bin:/usr/bin",
  "missing_from_inherited_path": [
    "/home/jstrunk.guest/.krew/bin",
    "/home/jstrunk.guest/.local/bin",
    "/home/jstrunk.guest/.cargo/bin",
    "/var/lib/devbox-toolbuilder/.local/bin",
    "/var/lib/devbox-toolbuilder/.cargo/bin",
    "/usr/local/node/bin",
    "/usr/local/go/bin",
    "/home/jstrunk.guest/.cache/go/bin",
    "/usr/local/sbin",
    "/usr/sbin",
    "/sbin",
    "/bin"
  ],
  "tools": [
    {
      "name": "golangci_lint",
      "command": [
        "golangci-lint",
        "--version"
      ],
      "pinned": "2.14.0",
      "installed": "2.14.0",
      "path": "/var/lib/devbox-toolbuilder/.local/bin/golangci-lint",
      "status": "ok",
      "detail": "2.14.0"
    }
  ]
}
```

Agent behavior check. A fresh subagent received the question "Is golangci-lint
installed in this VM?" and the repository rule. It ran the documented steps, hit
the old checker on the pre-change L1, fell back as the rule states, and reported
the version and the binary path. Captured output, not replayable:

```output
$ devbox-toolchain-check --tool golangci-lint
devbox-toolchain-check: error: unrecognized arguments: --tool golangci-lint
$ command -v golangci-lint
/var/lib/devbox-toolbuilder/.local/bin/golangci-lint
$ golangci-lint version
golangci-lint has version 2.14.0 built with go1.27.0 from 114493f9 on 2026-09-24T11:07:15Z
```

The L2 experiment used a read-only source mount and empty credential mounts. A
VM created before this change has no /usr/local/bin wrapper until devbox
--recreate. Restart does refresh the user-level copy and the skill text. The
documented fallback covers that case.

Evidence: 246 targeted unit tests and the full unit suite pass (980 plus 11
unit_serial). pre-commit passes on all 12 changed files. The fresh L2
provisioned from this commit and answered the probe. The VM tier of
scripts/run-vm-ci.sh did not run: the pre-existing host SQLite mount probe timed
out before the test harness started. CI runs the VM tier.

Known environment issue, not caused by this change: `coderabbit --version`
exits 1 on this L1 because the CLI rejects a symlinked log directory. The
full toolchain check therefore reports one ERROR on L1 and on the fresh L2,
with both the old and the new checker. The single-tool queries and the
version records above are unaffected.

Reproduce: run the commands in "See it work" from the repository root. For
the L2 check, follow the fresh-L2 recipe in `AGENTS.md` with `VmLevel=L2`,
then run the `limactl shell` commands above. Cleanup: stop and delete the L2,
then remove the task root.
