# virtiofsd gid mapping constraint and direct 9p baseline walkthrough

<!-- Captured with Showboat 0.6.1 on 2026-11-17 -->

Why: Issue #317 asked whether to change the Lima template from 9p to
virtiofs. On the physical host, virtiofs mounts and host-to-guest reads succeed,
but guest file creates and writes fail with `EINVAL` (`Invalid argument`). This
failure occurs because of a gid-mapping limit in virtiofsd. The default
`--sandbox=namespace` option in virtiofsd maps one uid and one gid. Lima passes
only `--socket-path` and `--shared-dir`, and Lima has no `user.gid` setting.
Upstream issue lima-vm/lima#2152 records this failure and remains open.

What changed: A recursive-tier regression test records the virtiofsd `/proc` id
maps, reproduces the `EINVAL` failure, and proves the guest-gid alignment
workaround. Documentation updates in `lima/mount-policy.md`, `lima/README.md`,
`lima/devbox.yaml`, and `lima/directory-sharing-benchmark.md` record the root
cause, the benchmark baseline, and the decision to keep 9p. The template stays
on 9p.

## Source map

- Template record: `lima/devbox.yaml:71-75`
- Mount policy: `lima/mount-policy.md:132-217`
- Benchmark follow-up: `lima/directory-sharing-benchmark.md:15-18,185-225`
- README rationale: `lima/README.md:797-809,867-883`
- Recursive test: `tests/vm/test_recursive_vm.py:264-547`
- Unit tests: `tests/unit/test_lima_template.py:851,855-893`,
  `tests/unit/test_vm_level_marker.py:567`

## Host environment and daemon versions

The L1 devbox environment provides `limactl` 2.2.1, QEMU 10.2.2, and `virtiofsd`
1.14.0.

```bash
id && limactl --version && qemu-system-x86_64 --version | head -n 1 && rpm -q virtiofsd && /usr/libexec/virtiofsd --version && uname -r
```

```output
uid=101587(jstrunk) gid=1000(jstrunk) groups=1000(jstrunk),36(kvm),983(docker),987(libvirt) context=unconfined_u:unconfined_r:unconfined_t:s0-s0:c0.c1023
limactl version 2.2.1
QEMU emulator version 10.2.2 (qemu-10.2.2-1.fc44)
virtiofsd-1.14.0-1.fc44.x86_64
virtiofsd 1.14.0
6.19.10-300.fc44.x86_64
```

## Production mount baseline

The devbox L1 uses 9p for its shared source mount from the physical host.

```bash
findmnt -T ~/src -o TARGET,FSTYPE,OPTIONS
```

```output
TARGET            FSTYPE OPTIONS
/home/jstrunk/src 9p     rw,relatime,cache=0x5,access=client,msize=131072,trans=virtio
```

## See it work: recursive virtiofs gid reproduction and workaround

The test `test_virtiofs_guest_gid_must_match_host_gid` runs in a nested L2 VM.
In this nested check, the term host refers to the L1 devbox VM. The test
records `/proc/<pid>/uid_map` and `/proc/<pid>/gid_map` from the virtiofsd
process. The map files show `101587 101587 1` for uid and `1000 1000 1` for gid.
When the guest primary gid is 4242, file creates fail with `Invalid argument`.
When the guest primary gid aligns with host gid 1000, file creates succeed,
and the L1 host observes gid 1000.

```bash
env UV_PROJECT_ENVIRONMENT="$HOME/.venvs/my-sandbox-issue-317" bash -c '
  set -o pipefail
  output="$(./scripts/sanitized-test.sh --guest-vm --require-vm -- \
    env MY_SANDBOX_VM_TEST_FRESH=1 DEVBOX_VM_START_TIMEOUT=3600 \
      UV_PROJECT_ENVIRONMENT="$HOME/.venvs/my-sandbox-issue-317" \
      uv run --extra test pytest tests/vm/test_recursive_vm.py \
        -m recursive -k gid -x -s 2>&1)"
  status=$?
  printf "%s\n" "$output" | grep -o "virtiofs-gid-evidence:.*"
  printf "pytest_exit_status=%s\n" "$status"
  exit "$status"
'
```

```output
virtiofs-gid-evidence: host uid=101587 gid=1000
virtiofs-gid-evidence: virtiofsd uid_map=[    101587     101587          1]
virtiofs-gid-evidence: virtiofsd gid_map=[      1000       1000          1]
virtiofs-gid-evidence: guest user=jstrunk id=[uid=101587(jstrunk) gid=1000(jstrunk) groups=1000(jstrunk) context=unconfined_u:unconfined_r:unconfined_t:s0-s0:c0.c1023]
virtiofs-gid-evidence: guest mount fstype=[virtiofs] shared stat=[101587:1000]
virtiofs-gid-evidence: guest default gid=1000
virtiofs-gid-evidence: aligned guest gid=1000
virtiofs-gid-evidence: mismatch guest gid=4242
virtiofs-gid-evidence: mismatch session gid=1000 (pinned at boot)
virtiofs-gid-evidence: mismatch write stderr: bash: line 1: /workspace/shared/from-mismatch.txt: Invalid argument
virtiofs-gid-evidence: restored guest gid=1000
virtiofs-gid-evidence: restored file host gid=1000
pytest_exit_status=0
```

## Mount policy documentation update

The mount policy documentation records the root cause, the upstream issue,
reproduction data, and workarounds.

```bash
sed -n '/^## Guest gid must match the host gid/,/^## Validation notes/p' lima/mount-policy.md | head -n -1
```

````output
## Guest gid must match the host gid (#317)

On the physical host, the failure of guest create/write operations under
virtiofs is attributed to a virtiofsd gid-mapping limit. Virtiofs mounts and
host-to-guest reads succeed, but guest create/write operations fail with
`EINVAL` (`Invalid argument`). The physical boundary was not re-run in this
validation, but virtiofs mounts started reliably in the nested runs, and the
recorded physical-host attempt served reads before its creates failed. The
failure has a three-part cause:

1. Lima v2.2.1 `VirtiofsdCmdline` (`pkg/driver/qemu/qemu.go`) passes only
   `--socket-path` and `--shared-dir` to virtiofsd.
2. virtiofsd 1.14.0 (`/usr/libexec/virtiofsd`, RPM
   `virtiofsd-1.14.0-1.fc44.x86_64`) uses its default `--sandbox=namespace`.
   That user namespace maps exactly one uid and one gid: the identity of the
   user that started `limactl`.
3. Lima sets the guest user's uid from the host, but `limatype.User` has no gid
   field. The guest keeps the image default gid, and Lima cannot set the guest
   primary gid from a template.

Upstream issue lima-vm/lima#2152 records this failure and remains open.

The #317 triage update on 2026-11-12 recorded physical-host values: host user
uid 101587 and primary gid 101587, but L1 guest user uid 101587 and primary gid
1000. Testing inside the L1 on 2026-11-17 with limactl 2.2.1, QEMU 10.2.2, and
virtiofsd 1.14.0 reproduced this behavior. In this nested check, "host" means
the L1. A nested L2 virtiofsd process reported that `/proc/<pid>/uid_map`
contained `101587 101587 1` and `/proc/<pid>/gid_map` contained `1000 1000 1`:

```text
101587 101587 1
1000 1000 1
```

When the primary gid of the L2 guest user was 4242, guest creates in the
virtiofs mount failed with `Invalid argument` and created no host file. When the
guest primary gid matched the L1 host's gid (1000), guest creates succeeded,
and the host observed the file with gid 1000.

The new recursive-tier test `test_virtiofs_guest_gid_must_match_host_gid` in
`tests/vm/test_recursive_vm.py` records `/proc/<pid>/uid_map` and `gid_map`. It
reproduces the `EINVAL` failure and proves the guest-gid alignment workaround.
The test passed in the L1 on 2026-11-17 (`1 passed`, 35.69 s).

Two workarounds were tested on 2026-11-17 in a nested L2:

- **Guest gid alignment at provision time:** A system provision script can
  create a group with the host gid and run `usermod -g` with that group. In
  tests, a user-mode provision script was measured to run with the new gid in
  the same boot. An aligned primary gid then uses the mapped gid, and the nested
  check proves that an aligned gid makes a guest create succeed. However, Lima
  SSH session machinery resolves credentials before boot scripts run.
  Interactive `limactl shell` sessions keep the gid that the persistent SSH
  session resolved at boot, and a VM restart refreshes it. Refreshing a primary
  gid by terminating the guest user session was not tested. Because Lima cannot
  set the primary gid from the template, this approach requires plumbing in the
  host gid and restarting the VM after first boot.
- **Virtiofsd translate flags:** `--translate-uid` and `--translate-gid` work
  while `--sandbox=namespace` remains active. Lima v2.2.1 reads only `type` and
  `binary` from the vhost-user JSON description in
  `~/.local/share/qemu/vhost-user/*.json` (which Lima searches before
  `/usr/share/qemu/vhost-user`). The tested override pointed `binary` at a
  wrapper script that execs `/usr/libexec/virtiofsd` with
  `--translate-uid squash-guest:0:<host uid>:4294967295` and
  `--translate-gid squash-guest:0:<host gid>:4294967295`. A guest create with
  gid 4242 succeeded without virtiofsd warnings, and both sides saw
  `<host uid>:<host gid>`. Lima does not pass these flags, and the override
  affects every Lima VM that the host user starts. The repository does not
  adopt this override.

The decision is to keep 9p. No supported template or launcher change fixes the
physical-host failure. Revisit virtiofs when Lima supports gid mapping or passes
the translate flags. A direct host-to-L1 performance comparison is not relevant
to the decision until guest create/write operations succeed on that boundary.
Validating that boundary still requires a run on a supported physical host.

Direct host-to-L1 9p baseline measurements recorded on 2026-11-17 show that the
direct 9p baseline is adequate for the measured workloads. For the 1,500-file
workload, direct 9p achieved a median of 5.668 s compared to 4.834 s for nested
virtiofs. For the 200-transaction loop, direct 9p achieved a median of 0.248 s
compared to 0.229 s for nested virtiofs. The large gap in the 2026-09-29
benchmark came from stacking 9p on 9p in a nested L2 VM. A direct virtiofs
comparison stays blocked until guest writes succeed. See
[`directory-sharing-benchmark.md`](directory-sharing-benchmark.md) for details
and comparison data.

````

## Benchmark documentation update

The benchmark documentation records baseline measurements for the direct
host-to-L1 9p mount and compares them against prior nested benchmark results.

```bash
sed -n '/^## Follow-up: direct host-to-L1 9p baseline/,/^## Inotify/p' lima/directory-sharing-benchmark.md | head -n -1
```

```output
## Follow-up: direct host-to-L1 9p baseline (2026-11-17)

On 2026-11-17, baseline measurements were recorded inside the production L1
devbox VM. The `~/src` path was the physical host's 9p mount
(`findmnt -T ~/src`:
`9p rw,relatime,cache=0x5,access=client,msize=131072,trans=virtio`). The VM ran
kernel `6.19.10-300.fc44.x86_64`.

The harness `lima/benchmark_sessiondb.py` ran with default settings (1,500
files, 200 transactions, and a 250 ms lock hold). It ran across three fresh
output directories on the 9p mount: `~/src/.issue-317-bench/run-1`,
`run-2`, and `run-3`. All runs used WAL journal mode and reported no errors.

Across the three harness runs, file workload times were 5.616 s, 5.683 s, and
5.668 s. Database end-to-end times were 0.660 s, 0.636 s, and 0.640 s. The
200-transaction loop times were 0.251 s, 0.248 s, and 0.245 s. Lock wait times
were 0.336 s, 0.336 s, and 0.337 s.

In the issue-317 worktree, `git status` ran three times with a warm cache. The
median elapsed time was 0.151 s (range 0.149–0.154 s, with runs of 0.151 s,
0.154 s, and 0.149 s).

| Workload | Direct host-to-L1 9p | Prior nested 9p | Prior nested virtiofs |
| --- | ---: | ---: | ---: |
| File workload (seconds) | median 5.668 (range 5.616–5.683, 3 runs) | median 217.382 (range 185.620–221.786, 4 runs) | median 4.834 (range 4.414–5.914, 13 runs) |
| 200-transaction loop (seconds) | median 0.248 (range 0.245–0.251, 3 runs) | 0.651 | 0.229 |
| Database end-to-end (seconds) | median 0.640 (range 0.636–0.660, 3 runs) | median 1.347 (range 1.239–1.406) | median 0.600 (range 0.590–0.625) |
| Observed writer lock wait (seconds) | median 0.336 (range 0.336–0.337) | 0.268–0.277 | 0.331–0.332 |
| `git status` (warm cache, seconds) | median 0.151 (range 0.149–0.154, 3 runs) | 2.438 | 0.078 |

The direct host-to-L1 9p path performs in the same range as the nested virtiofs
measurements from the 2026-09-29 benchmark. The large performance gap in that
earlier benchmark resulted from stacking 9p on 9p in a nested L2 VM. That
configuration is not the production path. The performance argument for
switching the production L1 devbox to virtiofs is therefore not supported by
these measurements.

These measurements are indicative, not a controlled comparison. They were
recorded on different virtual machines and dates, and the `git status` runs
used a warm cache.

```

## Changed files summary

The changes update documentation, unit test expectations, and recursive VM
tests.

```bash
git diff --stat
```

```output
 lima/README.md                      |  37 +++--
 lima/devbox.yaml                    |   5 +
 lima/directory-sharing-benchmark.md |  45 ++++++
 lima/mount-policy.md                |  88 ++++++++++-
 tests/unit/test_lima_template.py    |  25 +++-
 tests/unit/test_vm_level_marker.py  |   2 +-
 tests/vm/test_recursive_vm.py       | 286 ++++++++++++++++++++++++++++++++++++
 7 files changed, 466 insertions(+), 22 deletions(-)
```

```bash
git status --short
```

```output
 M lima/README.md
 M lima/devbox.yaml
 M lima/directory-sharing-benchmark.md
 M lima/mount-policy.md
 M tests/unit/test_lima_template.py
 M tests/unit/test_vm_level_marker.py
 M tests/vm/test_recursive_vm.py
?? docs/issue-317-virtiofs-gid-walkthrough.md
```

## Evidence and verification

The investigation confirmed several facts about virtiofs and Lima:

1. Lima v2.2.1 passes only `--socket-path` and `--shared-dir` to virtiofsd
   in `VirtiofsdCmdline` (`pkg/driver/qemu/qemu.go`).
2. virtiofsd 1.14.0 uses `--sandbox=namespace` by default. That namespace maps
   one uid and one gid. Lima sets the guest uid from the host and has no gid
   field.
3. Upstream issue lima-vm/lima#2152 records this failure and remains open.
4. The issue #317 triage update recorded physical-host values of host uid
   101587, host gid 101587, and L1 guest gid 1000. In the nested reproduction
   test, host refers to the L1. The test recorded `uid_map` as `101587 101587 1`
   and `gid_map` as `1000 1000 1`. A guest create with gid 4242 failed with
   `Invalid argument`. A guest create with gid 1000 succeeded, and the host
   observed gid 1000.
5. Two workarounds were tested in a nested L2. First, provision-time
   `usermod -g` runs with the new gid in the same boot for provision scripts.
   However, interactive `limactl shell` sessions keep the boot-time gid until
   the VM restarts. Second, virtiofsd `--translate-uid` and `--translate-gid`
   work under `--sandbox=namespace` when injected through a user-level
   vhost-user override. However, Lima does not pass those flags, and the
   override affects every Lima VM that the host user starts.
6. Direct host-to-L1 9p baseline measurements from 2026-11-17 used
   `benchmark_sessiondb.py` defaults. The runs showed a 1,500-file median of
   5.668 s and a 200-transaction loop median of 0.248 s. The database
   end-to-end time was 0.640 s, and the `git status` median was 0.151 s.
   Prior nested results showed 217.382 s and 0.651 s for 9p-over-9p. Nested
   virtiofs achieved 4.834 s and 0.229 s. The direct production path performs
   in the same range as the nested virtiofs numbers. The earlier performance gap
   resulted from stacking 9p on 9p in an L2 VM.
7. Decision: Keep 9p. Revisit virtiofs when Lima can set the guest gid or
   passes the translate flags. Direct physical-host validation still requires
   a run on a supported physical host.

No physical-host run occurred during this validation. The direct virtiofs
comparison was not measured. The nested runs reproduced the mechanism inside the
L1 virtual machine. In those nested runs, virtiofs mounts started reliably. The
recorded physical-host attempt served reads before its creates failed.

The captured test evidence block contains twelve deterministic evidence lines
and one exit status line (`pytest_exit_status=0`). The evidence lines confirm
the host identity, the virtiofsd user namespace maps, and the failure with
mismatched guest gid 4242. They also confirm successful file creation after
aligning the guest primary gid to host gid 1000.

## Reproduce and clean up

To run the recursive test inside a provisioned L1 devbox VM, execute this
command:

> ```bash
> env UV_PROJECT_ENVIRONMENT="$HOME/.venvs/my-sandbox-issue-317" \
>   ./scripts/sanitized-test.sh --guest-vm --require-vm -- \
>   env MY_SANDBOX_VM_TEST_FRESH=1 DEVBOX_VM_START_TIMEOUT=3600 \
>   UV_PROJECT_ENVIRONMENT="$HOME/.venvs/my-sandbox-issue-317" \
>   uv run --extra test pytest tests/vm/test_recursive_vm.py -m recursive -k gid -x -s
> ```

The test creates a temporary task directory under `/var/tmp` and starts a
nested L2 VM. It verifies the virtiofs id maps and write permissions. The test
deletes the nested VM and temporary directory during cleanup.
