# Shinu background snapshots: design and Linux 6.12 prototype

Status: experimental; **not a production application-consistency claim**. Base:
Firecracker `v1.18.0` (`eaf83bb`), snapshot format `12.0.0`. This mode does not
change serialized device/vCPU state or memory-file layout.

## Invariant and seam

A **snapshot generation** binds VM state, point-in-time guest memory, a disk root,
and metadata. A **capture cut** is the stopped-world instant represented by that
generation. **Completion** means every selected byte has been captured and the
requested file synchronization has succeeded; acceptance is not completion.

The snapshot module's interface should give callers leverage: one accepted
capture, one retained status, and one abort operation hide registration, page
ownership, fault prioritization, and cleanup. Locality matters more than exposing
individual UFFD operations. Callers and the manual test cross the same HTTP seam.
There is one capture implementation, not a speculative interchangeable adapter.

For every page at the capture cut, either it is still write-protected, or the
output already owns its old bytes. A writer is released **only after** those old
bytes have been copied to an independent buffer and accepted by the output.
No completed generation mixes pre-cut and post-cut memory or disk contents.

## Existing Firecracker path

Read these functions together; pause alone is not a block-I/O fence:

- [`persist::create_snapshot`](../../src/vmm/src/persist.rs) calls `Vmm::save_state`,
  serializes state, then calls `KvmVm::snapshot_memory_to_file` synchronously.
- [`Vmm::save_state`](../../src/vmm/src/lib.rs) saves devices **before** vCPU/KVM
  state. The device manager calls `prepare_save` before saving each transport.
  Block prepare drains asynchronous requests, handles completions, and flushes;
  saving interrupts first would lose an interrupt generated during that drain.
- [`snapshot_memory_to_file`](../../src/vmm/src/vstate/vm.rs) writes all plugged
  slots for Full; Diff obtains KVM dirty logs or a mincore overapproximation and
  unions device dirty bits. Successful synchronous dumps reset the dirty epoch;
  queues are marked dirty again afterwards. Existing same-sized files can be
  overwritten/merged, including the private-mmap restore backing file.
- [`GuestMemoryExtension`](../../src/vmm/src/vstate/memory.rs) preserves the
  concatenated region/slot memory-file layout. Holes in a Diff represent inherit,
  not zero; a dirty page containing zero must still overwrite its parent.

`prepare_save` is not itself an application freeze. In this base, error handling
in block drain/flush must also be considered before asserting production
application consistency. The prototype rejects Async block IO and uses the
existing Sync device save path; it does not turn a logged device flush failure
into a trustworthy durable generation. Host storage errors and external volumes
need their own checked cut protocol.

## Prior art and feasibility

### QEMU

QEMU v10.0 implements this approach, not iterative pre-copy:

- [`bg_migration_thread`](https://github.com/qemu/qemu/blob/v10.0.0/migration/migration.c#L3770-L3899)
  prepares tracking, stops the VM, saves non-RAM state, arms write tracking, then
  schedules restart separately. Restart callbacks can themselves write queues;
  a fault handler must not need the lock held by restart.
- [`ram_write_tracking_prepare/start/stop` and `poll_fault_page`](https://github.com/qemu/qemu/blob/v10.0.0/migration/ram.c#L1370-L1705)
  populate RAM, register/protect, prioritize faults, and release protection only
  after asynchronous output owns the page. QEMU explicitly probes both kernel
  features and actual RAM-block compatibility.
- [`uffd_open`](https://github.com/qemu/qemu/blob/v10.0.0/util/userfaultfd.c)
  prefers `/dev/userfaultfd` and falls back to the syscall.

### Kernel writers: why protecting the host mapping works for KVM

KVM does not simply write through the userspace virtual address on every guest
store. It installs secondary mappings. Linux's
[`uffd_wp_range`](https://github.com/torvalds/linux/blob/v6.12/mm/userfaultfd.c)
uses `change_protection`, whose
[MMU-notifier invalidation](https://github.com/torvalds/linux/blob/v6.12/mm/mprotect.c)
invalidates KVM mappings. On a subsequent writable translation, KVM's
[`hva_to_pfn_slow`](https://github.com/torvalds/linux/blob/v6.12/virt/kvm/kvm_main.c)
uses writable GUP; `__kvm_write_guest_page` also performs kernel-originated
`copy_to_user`. The handler must receive **kernel faults**, not just userspace
faults. With `UFFD_USER_MODE_ONLY`,
[`handle_userfault`](https://github.com/torvalds/linux/blob/v6.12/fs/userfaultfd.c)
can return SIGBUS for those faults. We explicitly use `user_mode_only(false)`.

Virtio writes made through Firecracker's registered host mapping, including
kernel copies into buffers for Sync IO, participate in this protection. A
retained writable physical-page pin, DMA, VFIO, vhost or a second unregistered
shared alias is **not** covered just because registration succeeded. The
[pinning contract](https://github.com/torvalds/linux/blob/v6.12/Documentation/core-api/pin_user_pages.rst)
requires notifier-driven invalidation and synchronized device access. Such
writers are outside this prototype; Async IO is rejected rather than assuming
io_uring pins obey the needed cut.

### Anonymous versus File restore

Linux 6.12's
[`vma_can_userfault`](https://github.com/torvalds/linux/blob/v6.12/include/linux/userfaultfd_k.h#L215-L247)
accepts anonymous, shmem, or hugetlb mappings for **synchronous** WP. Ordinary
regular-file `MAP_PRIVATE` RAM used by Firecracker File restore is not an
anonymous VMA. The any-memory-type exception is **async WP**, which does not
produce blocking preserve-before-write events and cannot implement this cut.
This is a hard compatibility restriction, not a performance fallback.

The prototype therefore accepts cold-boot anonymous RAM, and explicitly rejects
File-restored RAM. It also cannot register a second UFFD over regions already
owned by an external MISSING pager. The intended combined solution is below.
Do not change to async WP or silently fall back to an inconsistent dump.

[Kernel 6.12 documentation](https://docs.kernel.org/6.12/admin-guide/mm/userfaultfd.html)
requires CAP_SYS_PTRACE or `vm.unprivileged_userfaultfd=1` for kernel-fault syscall
UFFDs; `/dev/userfaultfd` access instead follows device permissions. Feature
negotiation and registration still have to succeed under the actual process
credentials. Our required `UFFD_FEATURE_WP_UNPOPULATED` (bit 13) protects absent
anonymous PTEs too; without it the kernel can silently skip absent pages. We
require it rather than pre-touch all RAM in the pause window. Linux 6.12 version
alone is not evidence that a particular distro build/credential supports it.

[CRIU lazy restore](https://criu.org/Userfaultfd) uses MISSING/post-copy;
[CRIU memory-change tracking](https://criu.org/Memory_changes_tracking) uses
soft-dirty for incremental/pre-dump. Neither supplies preserve-before-write by
itself, and soft-dirty/async WP must not be confused with this protocol.

## Implemented experimental interface

Start the fork with `--no-seccomp`. With the default VMM filter, Background is
rejected before any prohibited UFFD/thread syscall rather than causing SIGSYS.
The production default filters are **not** relaxed. A production implementation
needs a dedicated minimal worker filter and explicit UFFD ioctls/thread setup;
this branch does not claim production isolation.

The accepted subset is x86_64, 4 KiB base pages, anonymous RAM, Sync virtio-block,
no balloon, memory hotplug, pmem, vhost-user, or existing external UFFD ownership.
The snapshot and memory output paths must be distinct and **new**. This avoids
corrupting an immutable parent or a live File mapping. There is at most one
active capture, including exclusion against synchronous Full/Diff captures.

```http
PATCH /vm
{"state":"Paused"}

PUT /snapshot/create
{"snapshot_type":"Background",
 "snapshot_path":"/scratch/generation.state",
 "mem_file_path":"/scratch/generation.mem",
 "sync_snapshot_files":true}
```

A 204 means device/vCPU state is saved and all RAM is registered/protected.
**The VM remains Paused.** The background worker may already copy pages. This is
a deliberate correction to the superficially attractive “resume before create
returns” interface: shinu cannot cut its disk after that automatic resume.
Keeping Firecracker's existing caller-controlled resume gives a clean disk seam.

While still paused, shinu cuts every writable disk root, then:

```http
PATCH /vm
{"state":"Resumed"}

GET /snapshot/status
```

Status fields: `state` is Idle, Running, Complete, Failed, or Aborted;
`copied_bytes`, `total_bytes`, `wp_faults`, `elapsed_ms` (worker duration at
termination), and nullable `error`. A terminal status persists until another
accepted capture. There is one generation in flight, so no job-id namespace.
The caller already knows its unique output paths. There is no progress-poll
requirement for worker liveness. Only **Complete** is loadable, after memory
fsync when requested; state fsync is done before create returns. Parent directory
fsync and atomic generation publication remain shinu's responsibility.

```http
PUT /snapshot/abort
{}
```

Abort joins the independent worker after requesting cancellation, closes UFFD
(which unregisters and wakes blocked writers), and removes both owned outputs.
It is valid before resume, while running, or after Complete if the caller abandons
the disk cut. No-op when no capture exists. Inspect status for cleanup failures;
204 alone does not mean the filesystem accepted every unlink. Aborting does not
resume or thaw the guest: shinu always performs its own recovery sequence.

### Implementation and concurrency

[`background_snapshot.rs`](../../src/vmm/src/background_snapshot.rs) owns UFFD,
shared mapping references, output, a per-page bitset, one worker, cancellation,
and retained status. Setup registers every region, checks WRITEPROTECT support,
and arms every region before returning. Failed partial setup closes UFFD and
removes the memory output. A worker startup failure also releases protection.

One worker services up to 64 nonblocking events, then advances a 64 KiB scanner
batch. It copies pending bytes through vm-memory's volatile reads into an owned
buffer, writes them with positional IO, marks them captured, and unprotects.
Faulting pages are copied first; duplicate events cannot rewrite already-saved
pages. Bitsets are compact; there is one reusable 64 KiB buffer, not a second
RAM-sized allocation. The worker never takes the VMM/device lock. A slow output
can still stall a writer until its page is captured: low pause is not zero guest
latency or a bounded tail-latency guarantee.

File/registration/unsupported-event errors fail the capture and delete output;
no silent downgrade. UFFD is closed before status becomes terminal. Firecracker
uses panic=abort: a worker panic kills this experimental process, not a silently
hung apparently healthy VM. VMM teardown cancels/joins before joining potentially
faulting vCPUs and before releasing guest mappings. Successful outputs survive
ordinary VM teardown; explicit abort removes them. Cancellation is checked
between bounded batches; an uninterruptible host write/fsync can delay abort.
Process/host death can leave partial files; shinu must never infer completion
from pathname existence and must garbage-collect uncommitted generations.

## Diff, dirty tracking, hugepages, and discard

Implemented Background means **Full**, not background Diff. Existing synchronous
Diff remains unchanged. Background does not reset either KVM or device dirty
bitmap, so the next synchronous Diff conservatively includes the previous epoch.
Resetting a bitmap in the worker after resume would lose post-cut guest writes.

A future Background+Diff needs an independent selected-page bitmap obtained while
paused: union KVM and device dirty bits, then arm only those pages. Reset tracking
at the cut while still paused; queue pages are re-marked for the new epoch.
Capture failure must OR the old selected set back into the new dirty epoch while
paused, or invalidate the incremental chain and force Full. Never reset the new
epoch on completion. With no precise tracking, a mincore approximation retains
its existing no-swap requirements and does not prove a precise dirty cut. Diff
holes mean INHERIT; selected all-zero pages must be explicitly recorded. Shinu's
64 KiB chunk store must merge partial dirty chunks with the parent, not discard
unchanged sibling pages.

Hugetlb requires actual feature probing, hugepage-aligned registration/unprotect,
and preservation of an entire host hugepage before release. Transparent hugepages
are distinct and can split. The prototype rejects both rather than pretending
4 KiB state is sufficient. Balloon inflate/free-page reporting/hinting can discard
RAM or replace a File VMA; memory hotplug changes registrations/slots. These are
rejected for the VM's lifetime in this prototype. Future support must capture
old bytes before discard/remap and coordinate REMOVE/UNMAP events with the page
state machine, or suspend these features until capture completes. Merely handling
write faults is not enough.

## P2 MISSING pager and WP: recommended combined ownership

P2's UFFD restore gives the guest anonymous mappings, removing the ordinary-file
restriction, **but not the ownership problem**. Two independent descriptors cannot
register overlapping regions (EBUSY), and passing duplicates of one UFFD to two
independent readers is unsafe: either reader can consume the other's event.
Installing a second WP handler alongside today's external MISSING reader is not
a valid integration.

**Recommendation [INFERENCE design, not implemented here]: Firecracker owns one
UFFD and is its sole event reader.** Register MISSING|WP once during restore.
External P2 retains the immutable manifest/object lookup and decompression module,
but no longer receives a readable UFFD descriptor. Replace, do not layer over,
the old external-fault-handler protocol for this negotiated mode:

1. FC sends a versioned `Missing {request_id, guest_offset, length, generation}`
   message. P2 resolves immutable parent bytes and responds with an owned byte
   payload or a sealed read-only memfd range plus request_id. Transport loss fails
   the VM explicitly; never return zero on a missing/corrupt object.
2. FC alone issues `UFFDIO_COPY`. If the page is pending in an active cut, install
   with `UFFDIO_COPY_MODE_WP`; otherwise install without WP. Pre-cut unresolved
   pages still refer to the immutable parent. A post-cut writer cannot run past
   MISSING resolution and WP preservation. The parent generation is pinned until
   all such dependencies are discharged.
3. A **dedicated dispatcher** remains runnable independently of the snapshot
   copier. This is essential: a copier reading an unpopulated MISSING page can
   itself fault. If that same thread is the only reader, it deadlocks waiting
   for itself. Dispatcher handles MISSING without reading guest RAM; P2 reads
   immutable objects, never this VM's pending pages. A separate copier handles
   prioritized WP and scanning. Alternatively inherit untouched absent pages
   directly from the manifest, but only after a precise resident/cut-state proof;
   do not use a racy mincore check as that proof.
4. Per-page states are Pending → Copying → Captured, plus residency/in-flight
   missing-fill state. Scanner and WP work claim the same page with one atomic
   transition. Only the winner copies; losers wait without holding shared locks.
   Captured pages are never overwritten. Fault queues are bounded and preserve
   forward progress for MISSING while WP/output is slow.
5. Lock order: capture-admission/generation lock, then short per-page state lock.
   Release **both** before waiting for pager IO, file IO, page faults, or guest
   writers. Dispatcher never takes VMM or device locks. Device/VMM threads never
   wait while holding a lock needed by dispatcher/copier/pager. Publish a copied
   buffer before unprotect+wake. Cancellation closes registration only after
   coordinating outstanding fills; no late `COPY_MODE_WP` may re-arm an aborted
   generation. The dispatcher remains alive until all faulting writers and fill
   responses have been drained/cancelled.
6. Chunk prefetch must coalesce under the same per-page state machine. A 64 KiB
   object does not authorize overwriting all sixteen pages after one fault:
   writes may already have changed some; EEXIST is not a successful replacement.

This ownership change gives shinu a deep restore/capture module: callers need
one generation and status rather than knowing which process currently consumes
a given fault kind. P2 object lookup and FC kernel interaction are two real
roles, not two competing readers. The current prototype deliberately fails the
existing overlapping registration instead of claiming this design is done.

## Shinu call order and publication

Extend `vm::snapshot(dir, kind, disk_cut_closure)` with Background, but keep its
existing cleanup ownership:

1. Run `/etc/shinu/freeze.d/* freeze` with per-command deadlines; freeze writable
   ext4 mounts, root last. If this fails, fail; never label crash consistency as
   application consistency.
2. Pause; `create Background`; state capture drains devices and WP is armed.
3. While still paused, create btrfs reflinks (or an atomic block root) for **all**
   disks. The published generation refers to those roots, not mutable originals.
4. Resume; unfreeze filesystems and invoke hooks with `thaw` in reverse order,
   ALWAYS including failure cases. Hook/freeze time is distinct from FC pause.
5. Poll status with a deadline. On Failed, timeout, or disk-cut failure, abort;
   ensure resume/thaw even if abort/file cleanup itself fails. On abort that
   cannot complete because the host IO is stuck, do not claim a usable healthy
   VM; surface the failure and preserve recovery ownership.
6. On Complete only, bind `{fc_build_id, snapshot_format, parent_generation,
   consistency, created_unix_ns, pause_ms}` with state, memory/manifest, and disk
   roots. Sync files/directories as required and atomically publish metadata.
   Compression/chunking may follow the captured immutable memory; publication
   must still wait for every referenced object and manifest to be durable.

`create_call_ms` is **not** total pause. Measure pause request → device/state
save → WP arm → disk cut → resume. The manual experiment additionally reports
worker completion and restore-to-guest-checksum, not just HTTP load latency.

## Verification and reproduction

The executable regression harness is
[`tests/manual/background_snapshot.py`](../../tests/manual/background_snapshot.py).
It boots a scratch checkpoint copy with a minimal init, keeps an 8 MiB checksum
file on guest tmpfs, writes a pre-populated 128 MiB anonymous region continuously,
and performs virtio root-disk reads. It tests abort before resume, optional ENOSPC
with `--failure-dir` pointing to a small scratch tmpfs, then Full and Background
on the **same VM**, and restores each in a fresh process. An additional capture
is compared byte-for-byte against a synchronous Full oracle at the same paused
cut, while the guest writes after resume: this checks the actively modified
region, not just the separate immutable checksum file. It also checks retained
Complete status, copied byte count, nonzero WP faults, checksum agreement, guest
heartbeat, and clean rejection of Background on File-restored RAM. It uses no
TAP and never starts/restarts shinud. It does not test the LeapCore application
stack or claim multi-application freeze-hook coverage.

`tests/integration_tests/functional/test_snapshot_background.py` adds standard
harness coverage for the same Full-oracle equality/restore seam and abort before
or after resume. The direct harness above supplies the host-109 acceptance
experiment without requiring the upstream artifact catalogue and Python stack.

Hold `flock /root/fcexp.lock` across the entire experiment. Do not start when
`leap109-nightly.service` is active/activating, or MemAvailable is below 6 GiB.
The harness repeats those checks before boot/restore; use at most 4 GiB/2 vCPU.
Prepare only `/root/fcexp-*/rootfs.ext4`, never the published checkpoint itself.
The scratch root disk is exposed read-only (guest writes are tmpfs), so its
immutable disk root is stable for this memory experiment; actual writable-disk
btrfs CoW latency is not included and must not be inferred from these numbers.

Build on the development workstation:

```sh
CC_x86_64_unknown_linux_musl=x86_64-linux-musl-gcc \
CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER=x86_64-linux-musl-gcc \
cargo build --locked --release --target x86_64-unknown-linux-musl -p firecracker
```

Record live measurements and observed limitations in
[`background-snapshot-results.md`](background-snapshot-results.md). These results
are for the explicit small scratch VM, not a 14 GiB production world. Kernel/KVM,
feature support, Full comparison, faults, restore usability, cleanup and memory
availability must be observed, not extrapolated to the approximately 1 s target.
