# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Background capture through the same interface used by snapshot callers."""

import dataclasses
import filecmp
import platform
import time
from pathlib import Path

import pytest

from framework.artifacts import GUEST_KERNEL_DEFAULT, pin_guest_kernel
from framework.utils_hugepages import HugePagesConfig
from framework.vm_backend import VM_BACKEND_KVM, pin_vm_backend

pytestmark = [
    pin_vm_backend(VM_BACKEND_KVM),
    pin_guest_kernel(GUEST_KERNEL_DEFAULT),
    pytest.mark.skipif(
        platform.machine() != "x86_64"
        or tuple(map(int, platform.release().split(".")[:2])) < (6, 12),
        reason="Background prototype targets x86_64 Linux 6.12+ with WP_UNPOPULATED",
    ),
]


def wait_complete(vm):
    """Only a terminal, successful capture is a loadable snapshot."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        status = vm.api.snapshot_status.get().json()
        if status["state"] != "Running":
            assert status["state"] == "Complete", status
            assert status["copied_bytes"] == status["total_bytes"]
            return status
        time.sleep(0.01)
    pytest.fail("Background capture did not complete")


def boot(vm):
    """Explicit experimental opt-in; do not weaken default seccomp filters."""
    vm.jailer.extra_args["no-seccomp"] = None
    vm.spawn()
    vm.basic_config(
        vcpu_count=1,
        mem_size_mib=256,
        track_dirty_pages=True,
        huge_pages=HugePagesConfig.NONE,
        rootfs_io_engine="Sync",
    )
    vm.add_net_iface()
    vm.start()


def test_background_preserves_cut_under_guest_writes(uvm, microvm_factory):
    """All memory equals a stopped Full oracle despite post-cut guest writes."""
    vm = uvm
    boot(vm)
    vm.ssh.check_output("dd if=/dev/urandom of=/dev/shm/proof bs=1M count=8")
    _, checksum, _ = vm.ssh.check_output("sha256sum /dev/shm/proof")
    vm.ssh.check_output(
        "(while :; do dd if=/dev/zero of=/dev/shm/writer bs=1M count=32; done) "
        ">/dev/null 2>&1 </dev/null &"
    )
    reference = vm.snapshot_full(mem_path="oracle.mem", vmstate_path="oracle.state")
    vm.api.snapshot_create.put(
        snapshot_type="Background",
        mem_file_path="background.mem",
        snapshot_path="background.state",
    )
    assert vm.state == "Paused"
    vm.resume()
    wait_complete(vm)
    root = Path(vm.chroot())
    assert filecmp.cmp(reference.mem, root / "background.mem", shallow=False)
    vm.ssh.check_output("true")
    snapshot = dataclasses.replace(
        reference, mem=root / "background.mem", vmstate=root / "background.state"
    )
    vm.kill()
    restored = microvm_factory.build()
    restored.spawn()
    restored.restore_from_snapshot(snapshot, resume=True)
    _, restored_checksum, _ = restored.ssh.check_output("sha256sum /dev/shm/proof")
    assert restored_checksum == checksum


@pytest.mark.parametrize("resume_before_abort", [False, True])
def test_background_abort_releases_guest(uvm, resume_before_abort):
    """Abandoning the disk cut or an active capture leaves no partial outputs."""
    vm = uvm
    boot(vm)
    vm.pause()
    vm.api.snapshot_create.put(
        snapshot_type="Background",
        mem_file_path="abort.mem",
        snapshot_path="abort.state",
    )
    if resume_before_abort:
        vm.resume()
    vm.api.snapshot_abort.put()
    assert vm.api.snapshot_status.get().json()["state"] == "Aborted"
    assert not (Path(vm.chroot()) / "abort.mem").exists()
    assert not (Path(vm.chroot()) / "abort.state").exists()
    if not resume_before_abort:
        vm.resume()
    vm.ssh.check_output("true")
