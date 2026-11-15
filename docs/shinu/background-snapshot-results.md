# Background snapshot verification results

Experiment target: host 109, Debian kernel `6.12.116+deb13-cloud-amd64`, nested
x86_64 KVM, rotational QEMU disk. Firecracker base `v1.18.0`, format `12.0.0`.
All host files/processes belong to `/root/fcexp-p3-bg`; no shinud replacement,
production restart, store mount change, or write to the published checkpoint.
The checkpoint was opened read-only for a sparse copy to the host root filesystem.

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
  `x86_64-linux-musl-gcc` (44.08 s for the final live-proof binary build).
- This workstation has only the versioned native libseccomp shared library. A
  scratch `/tmp/fc-bg-native/libseccomp.so` symlink to `/usr/lib/libseccomp.so.2`
  plus `LIBRARY_PATH=/tmp/fc-bg-native` supplied the build-script link dependency;
  no system packages were installed. The initial native link failure and an
  initial Rust constructor compile error were fixed before the successful build.
- Rust sources were formatted with the pinned toolchain's rustfmt. Stable
  rustfmt warned that this repository's nightly-only comment/import settings
  were ignored; this is not a claim that the complete upstream style pipeline ran.
- Black/isort formatted the three new Python files. `py_compile` succeeded for
  the direct harness, capability probe, standard integration test and HTTP helper.
- `LIBRARY_PATH=/tmp/fc-bg-native cargo test --locked --workspace --lib --bins`
  was attempted. Firecracker's library tests passed **84/84**, binary tests passed
  **88/88**, including `test_background_capture_and_abort_requests` and the updated
  response serialization test. Other completed crate groups passed
  22, 40, 34, 3, 4, and 16 tests respectively.
- **The full Rust suite did not pass.** During the 902-test VMM group, observed
  failures were `device_manager::persist::tests::test_device_manager_persistence`,
  `devices::virtio::mem::device::tests::{test_plug_request_success,test_plug_request_too_big}`
  and `devices::virtio::net::device::tests::test_read_tap_fail_event_handler`; the
  process then aborted with `IO Safety violation: owned file descriptor already
  closed` (SIGABRT). These paths were not changed by this slice, but a baseline
  comparison was not run, so unrelatedness is [INFERENCE], not a verified claim.
  The suite was not rerun to conceal that result.
- The added upstream pytest integration file has not been executed in the local
  harness: `requests_unixsocket` is missing and the upstream guest artifact
  catalogue is not populated. The standalone 109 harness does not require those
  dependencies. No pytest pass is claimed.

## Review

Two independent read-only reviews covered documented standards and snapshot
semantics. Their actionable implementation findings were addressed: partial
state-file cleanup, cleanup-error reporting, centralized output removal, explicit
panic=abort status-lock assumptions, parser/integration coverage, and a full
memory oracle instead of only a checksum of a separate immutable file. A further
lifecycle fix detaches old output ownership before validating a new request, so
aborting a rejected new request cannot delete a previously completed generation.

The experiment uses an immutable read-only scratch disk. It is deliberately not
a writable-disk reflink latency or multi-application consistency test. The design
requires shinu to perform writable disk cuts before resume and to wait for Complete
before generation publication.
