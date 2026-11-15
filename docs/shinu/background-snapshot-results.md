# Background snapshot verification results

Experiment target: host 109, Debian kernel `6.12.116+deb13-cloud-amd64`, nested
x86_64 KVM, rotational QEMU disk. Firecracker base `v1.18.0`, format `12.0.0`.
All P3-owned host files/processes belong to `/root/fcexp-p3-bg`; no shinud
replacement, production restart, store mount change, or write to a published
checkpoint. The final harness uses P2's shared read-only scratch checkpoint
`/root/fcexp-shared/23a37f29.ext4` and a private initrd, with no per-experiment
disk copy or image injection. An earlier sparse copy of retired `51e9bf3b` was
stopped/deleted under host IO coordination; its cp exited and the requested
`/proc/*/fd` deleted-shinu-handle check had no output.

## Observed kernel feasibility (no VM)

Command (2026-11-15):

```sh
timeout 20 python3 /root/fcexp-p3-bg/uffd_wp_probe.py /root/fcexp-p3-bg
```

Observed output:

```json
{
  "kernel": "6.12.116+deb13-cloud-amd64",
  "userspace_write": {
    "features": 131071,
    "event_flags": 3,
    "old_byte": "A",
    "new_byte": "B"
  },
  "kernel_read_into_wp": {
    "features": 131071,
    "event_flags": 3,
    "old_byte": "A",
    "new_byte": "B"
  },
  "unpopulated_userspace_write": {
    "features": 131071,
    "event_flags": 3,
    "old_byte": "\u0000",
    "new_byte": "B"
  },
  "unpopulated_kernel_write": {
    "features": 131071,
    "event_flags": 3,
    "old_byte": "\u0000",
    "new_byte": "B"
  },
  "private_file_register_errno": 22
}
```

Both a userspace `memset` and kernel `read(2)` into a protected anonymous page
blocked for WP, let the handler read old `A`, and continued to write `B` after
unprotect. The negotiated mask includes PAGEFAULT_FLAG_WP and WP_UNPOPULATED.
Synchronous WP registration of an ordinary-file MAP_PRIVATE mapping actually
returned EINVAL (22), confirming the documented File-restore incompatibility.
This probe alone is **not** a KVM guest-store proof or a snapshot restore proof.

## Build and code checks

- Musl release build of `firecracker` succeeded with Rust 1.97.0 and
  `x86_64-linux-musl-gcc` (1 min 03 s for the final live-proof binary build).
- This workstation has only the versioned native libseccomp shared library. A
  scratch `/tmp/fc-bg-native/libseccomp.so` symlink to
  `/usr/lib/libseccomp.so.2` plus `LIBRARY_PATH=/tmp/fc-bg-native` supplied the
  build-script link dependency; no system packages were installed. The initial
  native link failure and an initial Rust constructor compile error were fixed
  before the successful build. The measured binary SHA-256 is
  `77d478c768d41e951653f34e18da51cf518a136dce8c0e493bbb3c9ce7bfd8c7`. The static
  initrd launcher also compiled with `-Wall -Wextra -Werror`; its uncompressed
  newc archive is 51 512-byte blocks (26,112 bytes).
- Rust sources were formatted with the pinned toolchain's rustfmt. Stable
  rustfmt warned that this repository's nightly-only comment/import settings
  were ignored; this is not a claim that the complete upstream style pipeline
  ran.
- Black/isort formatted the three new Python files. `py_compile` succeeded for
  the direct harness, capability probe, standard integration test and HTTP
  helper.
- `LIBRARY_PATH=/tmp/fc-bg-native cargo test --locked --workspace --lib --bins`
  was attempted. Firecracker's library tests passed **84/84**, binary tests
  passed **88/88**, including `test_background_capture_and_abort_requests` and
  the updated response serialization test. Other completed crate groups passed
  22, 40, 34, 3, 4, and 16 tests respectively.
- **The initial parallel invocation failed.** During the 902-test VMM group,
  observed failures were
  `device_manager::persist::tests::test_device_manager_persistence`,
  `devices::virtio::mem::device::tests::{test_plug_request_success,test_plug_request_too_big}`
  and `devices::virtio::net::device::tests::test_read_tap_fail_event_handler`;
  the process then aborted with
  `IO Safety violation: owned file descriptor already closed` (SIGABRT). At that
  stage there was no baseline comparison. The controlled post-rebase comparison
  below now establishes existing parallel-test interference and passing upstream
  CI settings on both branches; this initial negative evidence is retained
  rather than relabelled as a pass.
- The added upstream pytest integration file has not been executed in the local
  harness: `requests_unixsocket` is missing and the upstream guest artifact
  catalogue is not populated. The standalone 109 harness does not require those
  dependencies. No pytest pass is claimed.

## Review

Two independent read-only reviews covered documented standards and snapshot
semantics. Their actionable implementation findings were addressed: partial
state-file cleanup, cleanup-error reporting, centralized output removal,
explicit panic=abort status-lock assumptions, parser/integration coverage, and a
full memory oracle instead of only a checksum of a separate immutable file. A
further lifecycle fix detaches old output ownership before validating a new
request, so aborting a rejected new request cannot delete a previously completed
generation.

The experiment uses an immutable read-only scratch disk. It is deliberately not
a writable-disk reflink latency or multi-application consistency test. The
design requires shinu to perform writable disk cuts before resume and to wait
for Complete before generation publication.

## Safety regression checks

`python3 -m unittest discover -s tests/manual -p test_background_snapshot_preflight.py -v`
passed: one test with three CLI subcases in 0.201 s. Empty/error nightly
queries, `activating`, and a contradictory nonzero query result with `inactive`
output all refused to start the Firecracker executable. The external systemctl
command is the substituted system seam; no VM is mocked in the actual snapshot
proof.

A preliminary live run deliberately exposed a real default-filter failure in the
earlier binary (`ccd525ce...`): Background rejection happened after creating the
state file, and failure cleanup attempted `unlink` under the default VMM filter.
The HTTP client got `RemoteDisconnected`; the log at `2026-11-15T14:37:39Z`
recorded signal 31 and **bad syscall 87**. The fix checks Background capability
before any state-file creation, and the live harness now requires a 400
response, zero output files, and a working resumed guest under the default
filter. This is not a reason to loosen that filter.

Final source review also removed the paused-path `full_config()`
materialization: eligibility now inspects only the actual relevant device
types/IO engines, without allocating owned unrelated configurations. Required
ENOSPC injection now validates an empty small tmpfs and checks `os error 28`,
rather than accepting any failure.

## Successful live VM proof

The final proof used binary SHA-256
`77d478c768d41e951653f34e18da51cf518a136dce8c0e493bbb3c9ce7bfd8c7`, kernel
6.1.155 FC-CI, **1024 MiB / 2 vCPU**, and the shared new checkpoint `23a37f29`
as a read-only root. A 26,112-byte private initrd ran the proof without changing
the base. Guest load continuously rewrote 128 MiB of pre-touched memory; an 8
MiB tmpfs file supplied a pre-cut/restored checksum, and virtio-blk reads
exercised device-to-guest writes. Every VM was stopped before the next started.
The complete machine-readable output is
[`background-snapshot-results.json`](background-snapshot-results.json).

| Same-VM capture mode                   | create-call ms | pause→resume ms | total completion ms | restore→checksum ms |
| -------------------------------------- | -------------: | --------------: | ------------------: | ------------------: |
| Full, unchanged stock synchronous path |        2816.74 |         2818.37 |             2818.37 |              606.42 |
| Background                             |          74.04 |           75.34 |             4275.77 |              705.53 |

This single trial reduced the measured pause by **37.41× / 97.33%**, but
increased completion time: Background moved work out of the pause, not out of
existence. Its worker took 4172 ms, captured exactly 1,073,741,824 bytes, and
serviced **3051 WP events**. Full is the existing synchronous implementation in
the same fork binary and the same VM, not a separate stock-binary experiment.
Captures used `sync_snapshot_files=true`. The earlier oracle capture warms
memory; no cold-cache, p50/p95, or 14 GiB performance claim follows from this
one experiment.

Additional observed assertions passed:

- Same-cut oracle: the entire 1 GiB Background memory file was **byte-for-byte
  equal** to a stopped Full capture even though the guest resumed its writer.
  This run serviced 3473 WP events. Its 5597.99 ms pause includes creating the
  Full oracle and is **not** the dedicated Background pause measurement.
- Abort before resume: Aborted after 4 ms worker time, both outputs removed;
  guest resumed and produced a valid checksum heartbeat.
- ENOSPC: 8 MiB private tmpfs caused `No space left on device (os error 28)`;
  status Failed, 74 WP events had been handled, incomplete files removed, and
  the guest continued. This checks release of protection on failure.
- Both Full and Background restored and produced checksum
  `7d212b9c884f5c77896de960ae17cc341cda43b14d6a971f34ca29ebd4badf7f`. Their
  File-restored worlds rejected a new Background request cleanly, removed its
  outputs, then resumed and kept producing valid checksum heartbeats.
- The **default seccomp profile** now returned the expected 400 for Background,
  created no state/memory outputs, and left the restored guest resumable and
  healthy. The earlier syscall-87 regression no longer occurred in this run.
- A rejected new capture followed by abort did not delete the earlier completed
  generation's files.
- Required host gate was inactive and the experiment held `/root/fcexp.lock`.
  `free -g` before: total 23, used 0, free 18, cache 4, **available 22 GiB**;
  after: used 1, free 14, cache 8, **available 22 GiB**, swap 0 throughout.
- The separate absent-page probe also passed for userspace and kernel writes:
  old zero was captured before first write of `B`, with WP flags 3. This
  directly checks the negotiated WP_UNPOPULATED behavior, not only pre-populated
  pages.

Restore-to-checksum is a usable guest/Python proof, **not** LeapCore application
readiness. This proves the anonymous-RAM snapshot protocol on the stated host
and small configuration; it does not prove the general application-freeze
contract, writable-disk CoW latency, external pager composition, or the
production 14 GiB world's one-second restore goal. Those limitations are
explicit in the design.

## Cleanup

After archiving the JSON and regression log, the cleanup runner acquired the
same flock, found no owned Firecracker process, unmounted any owned small tmpfs,
and removed **only** `/root/fcexp-p3-bg`. It reported that the directory was
gone and the shared base was untouched. No TAP was created. The owned copy
process and orphan lock waiter had already exited; no deleted shinu checkpoint
handle remained. After cleanup `free -g` showed used 0, free 15, cache 7,
available 22 GiB, swap 0. All lock holders from this slice have exited.

## Branch provenance and production limits

The branch now rebases onto `shinu/v1.18`
`3de1de497e3e0ff2d8e7488714ff819d07d1432a`, including the fail-closed block
preparation patch. The current FC label is `Firecracker v1.18.0-shinu.1-bg`; the
baseline label is `Firecracker v1.18.0-shinu.1`. The jailer and persisted
`DevicesState` layout remain unchanged, and snapshot format is still `12.0.0`.
Background reaches the inherited `DeviceManager::save_state()` seam through
`Vmm::save_state()`, so a drain/flush failure prevents state-file creation here
as well as in synchronous capture. The old pending-integration risk is resolved.

The recorded 109 performance data remains historical evidence from binary
`77d478c7...` before this runtime rebase; it has not been relabelled as a fresh
measurement of the new build. No additional 109 experiment was performed. The
new upstream pytest cases remain scoped to x86_64 Linux 6.12+.

This still does not prove the whole application consistency contract: guest
hooks, checked writable-disk cuts, dedicated worker seccomp and a single-owner
MISSING|WP broker remain outside this prototype's proven subset. Independent
review was source/file-scoped; a complete branch-wide independent review is not
claimed.

## Rebased test comparison and current build

The earlier unqualified parallel command was **not** the upstream test runner.
The authoritative wrapper is
[`tests/host_tools/cargo_build.py:49-64`](../../tests/host_tools/cargo_build.py),
called by
[`tests/integration_tests/build/test_unittests.py:18-26`](../../tests/integration_tests/build/test_unittests.py).
It sets `RUST_TEST_THREADS=1`, `RUST_BACKTRACE=1`, and (on x86_64) empty
`RUSTFLAGS`, then runs all musl tests/doc tests and a separate examples pass.
The controlled comparison used this same selection and isolation, adding only
`--locked` and a local cross-linker search path for the required static library:

```sh
export RUST_TEST_THREADS=1 RUST_BACKTRACE=1 RUSTFLAGS=''
cargo test --locked --target x86_64-unknown-linux-musl --all --no-fail-fast -- --nocapture
cargo test --locked --target x86_64-unknown-linux-musl --all --no-fail-fast --examples -- --nocapture
```

| Checkout                               | Main suite passed / failed | Existing ignored doc test | VMM unit tests |     Examples |
| -------------------------------------- | -------------------------: | ------------------------: | -------------: | -----------: |
| Unmodified `shinu/v1.18` at `3de1de49` |                   1235 / 0 |                         1 |      903 / 903 | 3 / 0 failed |
| Rebased Background branch              |                   1237 / 0 |                         1 |      903 / 903 | 3 / 0 failed |

All four originally reported cases passed in **both** serial runs. There were no
test filters, new ignores, or changed failing tests. The two additional passing
tests are the Background request parser case compiled into the FC library and
binary test targets. The comparison also ran the VMM Rust integration tests
(11/11 each), including create/load snapshot. Existing tests which print that no
free 2 MiB hugepages are available retain their upstream conditional behavior;
this does not add hugepage support to the prototype.

The same original parallel command was also executed on the unmodified baseline
and the rebased branch. Baseline reproduced the same owned-FD IO Safety SIGABRT;
the individual failing metric/TAP cases varied with scheduling. The rebased
parallel run had six assertion failures but no abort in that particular run. The
full observed failure lists, original-four-case verdicts and log hashes are in
[`background-snapshot-test-comparison.json`](background-snapshot-test-comparison.json).

The causes have locality in the existing tests, not in the Background module:

- Device-manager persistence tests reuse a TAP named `hostname`; concurrent
  tests observed `TapOpen(... ResourceBusy ..., "hostname")` / EBUSY.
- Virtio-mem uses a process-global `METRICS`; the plug tests compare global
  counters to a previously read value plus one. Other parallel tests can update
  those counters between read and assertion. Block/rng/vsock showed analogous
  counter drift. Net's metrics registry also reuses a global entry per iface ID.
- `net::device::tests::test_read_tap_fail_event_handler` deliberately closes the
  raw TAP FD at line 2196, then runs metric assertions, and only at its
  successful end calls `mem::forget(th)` to prevent a second close (line 2225).
  **[INFERENCE: unwind mechanism]** A metric assertion panic bypasses that final
  forget, so unwinding drops the still-owning FD wrapper and can trigger the
  observed IO Safety abort. The direct evidence is that the unmodified baseline
  reproduces the abort and both branches pass this case under upstream
  isolation.

This establishes the reported failures as existing parallel-test interference;
no Background-specific isolated failure was found. The tests were not patched to
hide the unsupported parallel run. The upstream-scoped run is the passing
acceptance command, rather than a claim that arbitrary parallel execution is
safe.

For musl test linking, the workstation lacked static libseccomp. The same
`v2.6.1` pinned by `tools/devctr/Dockerfile:71-90` was built under `/tmp` with
`x86_64-linux-musl-gcc -static`, shared libraries disabled, and gperf 3.1. A
small linker wrapper added its library directory without changing the CI Rust
flags. The native build-script libseccomp linker-name symlink remained separate.
No system package or seccomp profile was changed, and no command touched host
109\.

The post-rebase musl release build succeeded, with observed output:

```text
Firecracker v1.18.0-shinu.1-bg
v12.0.0
Jailer v1.18.0
```

Current FC binary SHA-256:
`06cce24a4441b2b690647f27a2b04ccfe205ef0a3dd8c829e177f65ff199bd9f`. Jailer
SHA-256 (unchanged from P1):
`6b57d555128e7a301c949d6c79c0983406b3205ac36ea0fcd076d0a3bb928fd5`. shinu must
explicitly allow `Firecracker v1.18.0-shinu.1-bg` in addition to its baseline
label and pin this variant's archive digests. The old 109 timing table still
names its historical binary; these local post-rebase checks do not constitute a
new live timing measurement.
