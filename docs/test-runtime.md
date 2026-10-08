# Test runtime and concurrency

## Cgroup discovery

With the default cgroup root, `scripts/resource_preflight.py` automatically
uses `/proc/self/cgroup` to locate the process's active cgroup, including nested
paths in unified cgroup v2 and controller mounts in cgroup v1. With a custom
`--cgroup-root`, membership is used only when `--proc-cgroup` is supplied;
otherwise the existing flat-tree lookup is preserved for candidates within the
chosen root. Candidates that resolve outside that root are ignored. In
membership-discovery mode, PID and memory usage is read from active membership
paths, not substituted with an ancestor cgroup's value. If `/proc/self/cgroup`
is unavailable at the default root, or a supplied `--proc-cgroup` override is
missing, unreadable, or yields no valid matching membership, usage remains
unknown rather than falling back to flat-root values. Missing or unreadable
active usage files behave the same way; a known finite limit then constrains
the budget.

For deterministic tests with a synthetic cgroup tree, `--cgroup-root` selects
the tree and `--proc-cgroup` supplies a fixture in `/proc/self/cgroup` format.

## Measurement environment

The #302 baseline was measured inside the Fedora 44 Lima guest on an 8-vCPU,
16-GiB VM. The checkout was on the shared 9p mount; the test environment used
uv 0.12.17, Python 3.14.7, and pytest 9.1.1. The unit selection contained 258
tests with 14 non-unit tests deselected. The VM-local test venv was already
installed. Here, “cold” means a cold pre-commit hook cache, not VM or venv
provisioning. Baseline source was commit
`ff5da09026dabf0690c65a6c6b84af7dcb4c29d3`.

To reproduce the historical baseline, use a detached worktree at that commit
from the main checkout, then create a unique VM-local test environment for it:

```shell
git worktree add --detach .worktrees/issue-302-baseline ff5da09026dabf0690c65a6c6b84af7dcb4c29d3
cd .worktrees/issue-302-baseline
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/my-sandbox-issue-302-baseline"
uv sync --extra test

# Historical serial unit baseline. This revision predates --vm-lock, so run
# this baseline workflow alone on the shared guest.
./scripts/sanitized-test.sh --guest-vm -- \
  env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  uv run --extra test pytest -m unit
```

The remaining commands target the final feature worktree and its current
wrapper. For the historical baseline, use the command above; its wrapper
predates `--vm-lock`.

From the worktree root inside the guest, use this sequence to reproduce the
lint/unit runs and the serial VM-tier selections. The test and lint commands are
wrapped in `sanitized-test.sh`; its private HOME/TMPDIR are per invocation. The
explicit pre-commit cache is guest-local and intentionally reusable:

```shell
export UV_PROJECT_ENVIRONMENT="$HOME/.venvs/my-sandbox-issue-302"
uv sync --extra test

# Full local fast-check; use a fresh PRE_COMMIT_HOME for a cold first run.
./scripts/sanitized-test.sh --guest-vm --vm-lock --resource-preflight -- \
  env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  PRE_COMMIT_HOME="$HOME/.cache/pre-commit" ./scripts/fast-check.sh

# Full unit tier: process/signal tests serially, then parallel-safe tests.
./scripts/sanitized-test.sh --guest-vm --vm-lock -- \
  env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  ./scripts/run-unit-tests.sh

# Serial-only comparison on the final feature worktree.
./scripts/sanitized-test.sh --guest-vm --vm-lock -- \
  env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  uv run --extra test pytest -m unit

# Isolated warm lint run.
./scripts/sanitized-test.sh --guest-vm -- \
  env PRE_COMMIT_HOME="$HOME/.cache/pre-commit" ./.github/lint-all.sh

# Shared-VM tiers; do not add pytest -n to either command.
./scripts/sanitized-test.sh --guest-vm --vm-lock --require-vm -- \
  env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  uv run --extra test pytest -m "vm or e2e_kind"
./scripts/sanitized-test.sh --guest-vm --vm-lock --require-recursive-vm -- \
  env UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  uv run --extra test pytest -m recursive
```

Run `uv sync` before timing; its setup time is not part of the reported test
runtime. For a genuinely cold hook measurement, first use an empty
pre-commit-hook cache; the repeat reuses that cache.

`fast-check.sh` includes `.github/lint-all.sh` followed by the unit suite. A
baseline cgroup-measured unit run used the serial selection; the experimental
xdist runs used pytest-xdist 3.8.0. VM/kind and recursive repetitions used the
optional shared-VM lock. These tiers should remain serial. See the repository
[Testing section](../README.md#testing) for the wrapper behavior.

GNU `time` was installed for measurement only (`sudo dnf install -y time`;
version 1.9-28.fc44). For wall time, CPU time, and GNU time's reported maximum
RSS, wrap the sanitized command with GNU time:

```shell
./scripts/sanitized-test.sh --guest-vm --vm-lock -- \
  /usr/bin/time -v env \
  UV_PROJECT_ENVIRONMENT="$UV_PROJECT_ENVIRONMENT" \
  PRE_COMMIT_HOME="$HOME/.cache/pre-commit" ./scripts/fast-check.sh
```

For aggregate cgroup totals, run the command in a transient user scope and read
its cgroup files from the still-running scope shell before it exits. Set the
guest user-manager environment for non-interactive `devbox` shells, then run the
same sanitized command inside the scope. For example:

```shell
uid="$(id -u)"
XDG_RUNTIME_DIR="/run/user/$uid" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
  systemd-run --user --scope --property=MemoryAccounting=yes -- \
  bash -c '
    set +e
    ./scripts/sanitized-test.sh --guest-vm --vm-lock -- \
      env UV_PROJECT_ENVIRONMENT="$HOME/.venvs/my-sandbox-issue-302" \
      PRE_COMMIT_HOME="$HOME/.cache/pre-commit" \
      ./scripts/fast-check.sh
    status=$?
    cgroup="$(sed -n "s/^0:://p" /proc/self/cgroup)"
    for metric in memory.peak cpu.stat io.stat; do
      printf "%s: \n" "$metric"
      cat "/sys/fs/cgroup${cgroup}/$metric"
    done
    exit "$status"
  '
```

Record the exact command, cache state, wall time, test counts, GNU time output,
and the scope's `memory.peak`, `cpu.stat`, and `io.stat`. A cold hook run should
start without the relevant pre-commit hook environments; a repeat should reuse
those environments and the uv cache. Do not compare the two as if they had the
same setup cost.

## Baseline and profiling results

| Tier or experiment | Result | Resources / notes |
| --- | --- | --- |
| `uv sync`, fresh worktree venv | 1.75s wall; 1.46s user+system; warm repeat 0.28s | Initial setup downloaded CPython 3.14.7 and test packages; repeat checked 13 packages. Guest tool/runtime already provisioned. |
| `fast-check.sh`, cold pre-commit cache at baseline commit | 4:33.17 wall; 258 unit tests passed; unit phase 83.67s | GNU time: 55.47s user + 25.94s system; max RSS 662,704 KiB. |
| `fast-check.sh`, warm baseline at `ff5da09` | 3:13.27 wall; 258 unit tests passed; unit phase 75.25s | Cgroup: 18.30s user + 8.22s system CPU; 218,832,896-byte `memory.peak`. Same warmed pre-commit cache used by the post-change run. |
| Intermediate warm retry | 4:15.86 wall; one unit failure | Existing `test_wrapper_sigint_exits_fast_with_command_cleanup` failed once; it passed in 10 consecutive targeted reruns and later complete runs. |
| Original `.github/lint-all.sh`, warm baseline | 2:24.39 wall; 16.53s user+system CPU; max RSS 146,180 KiB | Old per-file `git check-ignore` implementation. Repeated wall times varied on the shared 9p checkout. |
| Lint A/B on the same updated 93-file tree at `0c67469` | Old per-file scan: 3:19.28; batched scan: 1:55.44 | Both passed all hooks. Batched `git ls-files` saves 83.84s (42%) in this paired run; user+system CPU was 18.10s old and 15.16s new. |
| Serial unit baseline | 258 passed in 70.02s | Cgroup: 5.50s user + 3.40s system CPU, 194,224,128-byte `memory.peak`. Other warm serial runs took 83.67–85.60s. |
| Experimental `pytest -n 4` unit baseline | 258 passed in 25.50s | Cgroup: 9.80s user + 4.30s system CPU, 493,568,000-byte `memory.peak`. |
| Experimental `pytest -n 8` unit baseline | 258 passed in 21.09s | Faster in isolation, but uses all guest CPUs and leaves no CPU headroom for a second worktree. |
| Provisioned-VM/kind baseline | 7 passed, 1 expected skip in 81.37s | Cgroup: 21.13s user + 11.83s system CPU, 1,581,539,328-byte peak; I/O about 56 MB read / 744 MB written. Kind cycle: 68.10s. |
| Recursive VM baseline | 2 passed in 427.61s | Cgroup: 474.31s user + 57.26s system CPU, 5,366,390,784-byte peak, 48 MB read. Keep this tier serial. |

Two unit workers bound process pressure within a single full test run.
Experiments running complete suites in two worktrees at once timed out in
signal/process-group regression tests even with two workers per worktree. Full
`fast-check` commands therefore use `--vm-lock` across worktrees; targeted
filesystem/kind-script tests do not use it and can overlap. Sanitized wrapper
invocations still have private HOME/TMPDIR trees, and each worktree needs its
own VM-local `UV_PROJECT_ENVIRONMENT`.

Do not run full `fast-check`, VM/kind, or recursive tiers concurrently against
the same shared devbox. Pass `--vm-lock` to each `sanitized-test.sh` invocation
for those suites; it locks the stable original-home path
`$HOME/.cache/my-sandbox-vm-tests.lock` across the complete test command. The
lock opener requires `flock` and Python 3, and the wait is bounded at 3600
seconds by default and can be tuned with
`--vm-lock-timeout SECONDS` (maximum 86400). Unit/fast-check commands
can use it for the complete workflow; targeted parallel-safe unit selections
intentionally omit it. CI's VM jobs use their own isolated Lima homes.

`lima/validate-kind.sh` also adds its shell PID to the timestamped cluster
prefix, avoiding same-second name collisions; its cleanup trap only deletes
clusters registered by that invocation.

## Post-change measurements

The feature worktree is based on `2a2bacd` (after PR #321); the original
baseline was `ff5da09`. Upstream merges and the final lock regressions increased
the unit selection from 258 tests to 364 (11 timing-sensitive serial tests plus
353 parallel-safe tests).

| Final-base workflow | Result | Resources / notes |
| --- | --- | --- |
| Warm full `fast-check.sh`, 355-test snapshot | 2:27.72 wall; 355 passed (11 serial + 344 parallel-safe) | Cgroup: 23.82s user + 9.38s system CPU, 287,010,816-byte peak. Warm hook and uv caches. |
| Final reviewed full `fast-check.sh` | 33.44s wall; 364 passed (11 serial + 353 parallel-safe) | Bash `time -p`: 19.01s user + 4.99s system; warm hook and uv caches. All pre-commit hooks passed. |
| Final unit runner, parallel-safe phase | 353 passed in 10.64s with `-n 2` | Full CI/local unit selection also runs the serial phase below. |
| Final unit runner, serial process/signal phase | 11 passed in 13.76s | Includes lifecycle and signal cleanup cases. |
| Cold hook setup, initial post-change base `0c67469` | 3:44.61 wall; 287 passed; unit phase 33.59s | Cold `PRE_COMMIT_HOME`; not repeated after later upstream merges. |
| Latest provisioned-VM/kind tier | 6 passed, 3 expected skips in 42.59s | Run with `--vm-lock`; KVM preflight passed. Skips reflect the existing guest's different system provisioner, stale staged skills, and a disposable-CI-host-only mount probe. |
| Earlier recursive VM tier | 2 passed in 371.98s | Run with `--vm-lock`; cgroup peak 5,463,699,456 bytes, CPU 452.50s, about 62 MB read / 0.3 MB written. |

On the original 258-test baseline selection, the standalone unit experiment went
from 70.02s serial to 25.50s with four workers (64% faster), at a peak-memory
increase from about 185 MiB to 471 MiB. The final runner uses two workers for
the parallel-safe group and keeps process/signal tests serial. The warm baseline
at `ff5da09` ran 258 unit tests in 3:13.27; the warm 355-test snapshot after the
change took 2:27.72. The post-change snapshot therefore covered 97 additional
tests. The latest 364-test run took 33.44s with Bash `time -p`; it is a separate
warm run, not a matched baseline comparison.

The signal/process tests also take a per-UID `flock` in a private mode-0700
directory under `/tmp`, with a bounded 300-second wait. If shared-guest
contention, unsupported filesystem locking, or an unsafe pre-existing lock
path prevents acquisition, the affected tests are reported as infrastructure
skips rather than product failures. This protects direct pytest invocations
across worktrees; the full `fast-check` lock additionally prevents the rest of
the suite from adding shared-guest load while that group runs.

Earlier two-worktree validation ran the 344-test parallel-safe unit selection
from both worktrees at once: 344 passed in 37.31s and 40.33s, with 41.53s
overall wall time and 557,002,752-byte cgroup peak. Each checkout used a
distinct VM-local venv and sanitizer-created HOME/TMPDIR. The final suite adds
lock regression tests; the paired timing above records the earlier 344-test
selection, not those later additions.

Concurrent full-suite experiments timed out in process-signal/lifecycle tests;
the final unit runner separates those cases into a serial phase, and a single
full-worktree fast-check passes. Do not claim or run two full suites in parallel
on this shared guest: use `--vm-lock` so only one complete workflow runs at a
time. The wait is bounded at 3600 seconds (configurable up to 86400). The
parallel-safe unit subset above is the validated concurrent path. VM/kind and
recursive tiers also use this lock. The recursive tier measurement above
predates the final lock-file hardening; the current fast-check and VM/kind tier
exercise the updated lock path, and CI will validate the recursive workflow.
