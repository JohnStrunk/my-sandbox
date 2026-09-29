# Directory-sharing benchmark (issue #268)

**Run date:** 2026-09-29

**Status:** completed in the bootstrap Lima VM from issue #267

## Recommendation

- **For the full Linux/QEMU template (#270): conditional GO for virtiofs.** On
  this Fedora 44 / Lima 2.2.0 stack it was much faster than stacked 9p for
  metadata and small-file workloads, and the mixed-write test did not reproduce
  lima-vm/lima#2152. Keep 9p as a fallback until the same write check passes
  against a direct host-to-L1 virtiofs mount; this experiment used an L1 whose
  `~/src` was already a 9p mount.
- **For nested test VMs:** use a minimal mount set and copy the repository to
  the L2's VM-local disk for ordinary tests. Do not mount all of `~/src` over
  another 9p mount: the large-repository `git status` did not finish within 20
  minutes in that configuration.
- **Keep Python environments and caches VM-local.** A mounted uv environment
  was substantially slower on both mount types; a mounted uv cache also added
  measurable overhead.
- **Standardize on editing watched files inside the L2.** `mountInotify` was
  enabled and Lima logged the watched mount, but edits made outside the L2 were
  not delivered to L2 watchers on either mount type. For workflows that must
  observe L1/host-side edits, use a polling watcher instead. Virtiofs did
  deliver inotify events for edits made inside the L2.
- No 9p `msize` or cache tuning was tested, so keep Lima's defaults rather than
  infer a tuning recommendation from this spike. The minimal bootstrap template
  remains on its 9p default; #270 can consume the conditional virtiofs
  recommendation.

## Environment and method

The experiment ran entirely inside the issue #267 bootstrap VM; no physical
host action was needed for the L2 mount and workload tests.

| Layer | Environment |
| --- | --- |
| L1 | Fedora 44, kernel `6.19.10-300.fc44.x86_64`, 8 CPUs / 15 GiB RAM; Lima 2.2.0; QEMU/qemu-img 10.2.2; virtiofsd 1.14.0. |
| L2 | Same Fedora 44 x86_64 cloud image and digest as `lima/devbox.yaml`; 2 CPUs, 4 GiB RAM, 32 GiB disk. Guest kernel `6.19.10-300.fc44.x86_64`. |
| Tools | Git 2.55.0, Go 1.26.8, uv 0.12.17, ast-grep 0.45.3, system Python 3.14.3; uv-managed project Python 3.14.7; SQLite 3.51.2. |

The L1's `~/src` mount was 9p (`msize=131072`, `cache=0x5` / mmap). The three
L2 configurations were:

1. **9p:** Fedora L2 mounting L1's `~/src` with 9p (9p-over-9p).
2. **virtiofs:** the same source tree mounted with Lima's Linux/QEMU virtiofsd.
3. **No shared mount:** `--mount-none`; repository and workload inputs were
   copied into `/var/tmp` on the L2 virtual disk. `/tmp` is a 2 GiB tmpfs in
   this image, so the 2.7 GiB large-repository fixture was intentionally kept
   off `/tmp`.

Each L2 used the same Fedora image and resource settings. The large repository
was `k8s-operatorhub/community-operators`, commit
`5a28c02b24b4c056054bba427578834326a3ee55`, 56,586 tracked files and 2.7 GiB on
the L1. The my-sandbox main checkout was at `220ae4f`.

The Python timing helper used `time.perf_counter()` around each command and
discarded command output. Git and ast-grep commands ran three times. Go cold
builds were one run per configuration after `go clean -cache`; warm builds ran
three times. Uv placement timings are medians of three
`uv sync --locked --extra test --reinstall` runs after populating each cache.
Go's module cache stayed VM-local; builds wrote the binary into the tested
mount or local disk. `L1 direct 9p` is a reference measurement made on the
L1's existing host-shared `~/src` mount, not a fourth L2 configuration.

The uv matrix ran from the issue-268 linked worktree in both shared-mount L2s.
The no-share L2 used a linked worktree created from a VM-local clone at the
same base commit. `UV_PROJECT_ENVIRONMENT` and `UV_CACHE_DIR` were set
independently to guest-local or shared paths for the placement comparisons.

## Git results

Median elapsed seconds (three runs; lower is better):

| Workload | L1 direct 9p | L2 9p | L2 virtiofs | L2 no shared mount |
| --- | ---: | ---: | ---: | ---: |
| my-sandbox `git status` | — | 2.438 | 0.078 | 0.001 |
| my-sandbox `git log -10` | — | 0.560 | 0.021 | 0.001 |
| Large repo `git status` | 26.603 | **>1,200 (timed out)** | 40.017 | 0.278 |
| Large repo `git log -10` | — | 0.855 | 0.036 | 0.003 |

Raw status runs for the large repository were `211.807 / 26.421 / 26.603` on
L1 direct 9p, `121.731 / 40.017 / 39.403` on L2 virtiofs, and
`0.411 / 0.278 / 0.223` on the L2-local copy. The nested-9p command was still
running after 1,200 seconds and was stopped; treat that result as a lower bound,
not a completed timing. The large-repository `git log` timings were
`0.871 / 0.855 / 0.811` (9p), `0.040 / 0.036 / 0.029` (virtiofs), and
`0.008 / 0.003 / 0.002` (local).

The main-checkout status runs were `3.906 / 2.429 / 2.438` (9p),
`0.202 / 0.078 / 0.073` (virtiofs), and `0.005 / 0.001 / 0.001` (local).
Main-checkout `git log -10` runs were `0.547 / 0.560 / 0.560` (9p),
`0.034 / 0.021 / 0.019` (virtiofs), and `0.003 / 0.001 / 0.001` (local).

For the large-repository status workload, stacked 9p was not usable at the
20-minute cutoff. Virtiofs was about 1.5x the L1's direct 9p warm time and about
144x the VM-local copy's warm time; its first run was slower while the guest
cache warmed.

## Build, scan, and uv results

| Workload | L2 9p | L2 virtiofs | L2 no shared mount |
| --- | ---: | ---: | ---: |
| Go cold build (seconds) | 8.161 | 8.249 | 7.363 |
| Go warm build median (seconds) | 0.287 | 0.061 | 0.042 |
| ast-grep Python scan median (seconds) | 0.501 | 0.071 | 0.030 |
| uv local env + local cache median (seconds) | 0.248 | 0.194 | 0.164 |

The Go workload was a small module using `github.com/google/uuid` and
`golang.org/x/text`; module downloads were prewarmed and the output binary was
written into the tested filesystem. Warm Go build runs were `0.290 / 0.287 /
0.271` (9p), `0.061 / 0.064 / 0.059` (virtiofs), and `0.043 / 0.038 / 0.042`
(local).

The ast-grep scan used:

```shell
ast-grep run --lang python --pattern 'print($ARG)' \
  --files-with-matches <my-sandbox-checkout>
```

Runs were
`0.493 / 0.501 / 0.560` (9p), `0.105 / 0.071 / 0.070` (virtiofs), and
`0.048 / 0.030 / 0.027` (local). An exploratory YAML scan over the entire
large repository did not finish within 600 seconds on 9p and was terminated;
the comparable scan across all three configurations therefore uses
my-sandbox.

For uv, the project environment and cache were independently placed on the
guest-local disk or the shared mount. Values below are medians of three warm
reinstall runs, seconds:

| `UV_PROJECT_ENVIRONMENT` | `UV_CACHE_DIR` | L2 9p | L2 virtiofs | L2 no shared mount |
| --- | --- | ---: | ---: | ---: |
| local | local | 0.248 | 0.194 | 0.164 |
| local | mount | 5.858 | 0.872 | n/a |
| mount | local | 37.218 | 6.401 | n/a |
| mount | mount | 37.652 | 3.315 | n/a |

The first local-env/local-cache syncs took 1.612 s (9p), 1.855 s (virtiofs),
and 1.657 s (local). The first mount-env/mount-cache sync took 29.480 s (9p)
and 2.916 s (virtiofs). For the full uv matrix, 9p local-env/mount-cache runs
were `5.817 / 5.858 / 5.920`; mount-env/local-cache runs were
`37.153 / 37.218 / 37.978`. Virtiofs local-env/mount-cache runs were
`0.949 / 0.843 / 0.872`; mount-env/local-cache runs were
`7.234 / 6.401 / 6.265`. The environment location dominated; placing only the
cache on virtiofs was also slower than keeping both paths local.

## Small-file, SQLite, and virtiofs write validation

The executable harness is `lima/benchmark_sessiondb.py`. From the repository
root inside each L2, run:

```shell
./lima/benchmark_sessiondb.py /path/to/new/output-directory
```

The output path must not already exist. It creates, rewrites, and reads 1,500
small JSON files, then performs 200 one-row SQLite WAL transactions using the
SQLite default synchronous setting. A second process holds `BEGIN IMMEDIATE`
for 250 ms while a contender records the lock wait. The report contains four
9p runs, 13 virtiofs runs, and five VM-local runs. The latest run in each mode
used the checked-in harness and supplies the transaction-loop-only timing;
earlier runs used an equivalent temporary script.

| Result | L2 9p | L2 virtiofs | L2 no shared mount |
| --- | ---: | ---: | ---: |
| File workload (seconds) | median 217.382 (range 185.620–221.786, 4 runs) | median 4.834 (range 4.414–5.914, 13 runs) | median 0.139 (range 0.132–0.156, 5 runs) |
| DB end-to-end: setup, transactions, and lock (seconds) | median 1.347 (range 1.239–1.406) | median 0.600 (range 0.590–0.625) | median 1.612 (range 1.481–1.720) |
| 200-transaction loop only (latest harness run; seconds) | 0.651 | 0.229 | 1.056 |
| Journal mode / errors | WAL / 0 | WAL / 0 | WAL / 0 |
| Observed writer lock wait (seconds) | 0.268–0.277 | 0.331–0.332 | 0.330–0.331 |

All 13 virtiofs mixed-write runs completed; none raised `EINVAL` or another
write error, WAL mode stayed enabled, and the competing writer waited for the
held lock. The virtiofs instance's Lima host-agent/serial logs also contained
no `EINVAL` or `Invalid argument` text. This rules out the reported failure for
this workload and stack, but does not close or invalidate Lima issue #2152.

## Inotify and iteration loop

`mountInotify` was enabled for the 9p and virtiofs L2s; Lima logged that it
enabled inotify for the writable mounts. With the real nested source (`~/src`,
which is itself an outer 9p mount), L1-side create/write/append, nested
directory creation, rename, and delete operations produced **no** events in
the L2 watcher on either mount type. An L1-local btrfs source mounted through
9p also produced no L1-to-L2 events. Guest-local writes to that L1-local 9p
mount did produce `CREATE` and `CLOSE_WRITE` events. On virtiofs, L2-local
create/modify/close-write, nested-directory, rename, and delete operations all
produced their expected events.

This run could not issue a write from a shell on the physical host outside L1.
Thus physical-host-to-L1-to-L2 event propagation remains unverified; the tested
L1-to-L2 relay is not reliable enough to standardize on `mountInotify`.

The template iteration check used
`~/src/.issue268-bench/l2-iteration.yaml`: Lima started a 9p L2, the template
was edited in L1 to select virtiofs, and a second L2 started from the same path.
`findmnt` and `limactl list` reported the expected mount type for each; both
instances were then stopped and deleted. The edit/start/verify/teardown loop
works, but the large-repository timings favor a minimal-mount template plus a
VM-local repository copy for future L2 test harnesses.

## Bootstrap prerequisite found

The first nested L2 start failed because the bootstrap VM had `qemu-kvm` but
not `qemu-img`, which Lima needs to inspect and resize L2 disks. Installing
Fedora's `qemu-img` 10.2.2 package allowed L2 creation. The provisioning script
and Lima README now include that package, with a unit test guarding the package
list.
