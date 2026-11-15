#!/usr/bin/env python3
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Direct Firecracker interface test; no shinud, jailer, TAP, or production disk writes.

Run as root on 109 only after preparing a PRIVATE rootfs copy with --prepare-rootfs.
The caller must hold flock /root/fcexp.lock throughout. Refuses nightly overlap and
low host memory; uses one VM at a time, 1 GiB / 2 vCPU by default.

python3 background_snapshot.py --prepare-rootfs /root/fcexp-p3-bg/rootfs.ext4
flock /root/fcexp.lock python3 background_snapshot.py --binary ./firecracker \
    --kernel ./vmlinux --rootfs ./rootfs.ext4 --workdir ./run --failure-dir ./small

--failure-dir must be a caller-owned, empty, small tmpfs (e.g. size=8m).
Output JSON includes create-call latency, full pause window, completion latency,
restore-to-checksum latency, fault counts, abort/failure evidence, and free -g.
"""

import argparse
import hashlib
import http.client
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from pathlib import Path

GUEST = r"""#!/bin/sh
mount -t proc proc /proc
mount -t sysfs sysfs /sys
mount -t devtmpfs devtmpfs /dev
mkdir -p /dev/shm
mount -t tmpfs -o size=512m tmpfs /dev/shm
exec /usr/bin/python3 -u -c '
import hashlib, mmap, os, struct, threading, time
path="/dev/shm/bg-proof"
data=bytes(range(256))*32768
with open(path,"wb") as f: f.write(data)
expected=hashlib.sha256(data).hexdigest()
del data
ram=mmap.mmap(-1,128*1024*1024)
# Touch every page before READY; KVM must invalidate already writable mappings.
ram[:]=b"X"*len(ram)
def writer():
    tick=0
    while True:
        tick+=1
        payload=bytes([tick%256])*4096
        for pos in range(0,len(ram),4096): ram[pos:pos+4096]=payload
threading.Thread(target=writer,daemon=True).start()
print("BG_READY checksum="+expected,flush=True)
tick=0
while True:
    with open(path,"rb") as f: actual=hashlib.sha256(f.read()).hexdigest()
    assert actual==expected
    tick+=1
    # Root disk reads exercise virtio-blk host writes to guest buffers too.
    with open("/usr/bin/python3","rb") as f: f.read(1024*1024)
    print("BG_ALIVE checksum="+actual+" tick="+str(tick),flush=True)
    time.sleep(0.25)
'
"""


class UnixHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.host)


def request(sock, method, path, data=None, expected=204):
    connection = UnixHTTP(str(sock), timeout=180)
    body = None if data is None else json.dumps(data)
    connection.request(method, path, body, {"Content-Type": "application/json"})
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    assert response.status == expected, (method, path, response.status, raw.decode())
    return json.loads(raw) if raw else None


def wait_for(predicate, timeout=180):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out waiting for guest/capture")


def alive(log, offset=0):
    text = log.read_text(errors="replace")[offset:] if log.exists() else ""
    matches = re.findall(r"BG_ALIVE checksum=([0-9a-f]{64}) tick=(\d+)", text)
    return matches[-1] if matches else None


class VM:
    def __init__(self, binary, directory):
        self.directory = directory
        directory.mkdir()
        self.sock = directory / "api.sock"
        self.log = directory / "serial.log"
        self.output = self.log.open("wb")
        self.process = subprocess.Popen(
            [str(binary), "--no-seccomp", "--api-sock", str(self.sock)],
            stdin=subprocess.DEVNULL,
            stdout=self.output,
            stderr=subprocess.STDOUT,
        )
        wait_for(lambda: self.sock.exists() or self.process.poll() is not None, 20)
        assert self.process.poll() is None, self.log.read_text(errors="replace")

    def call(self, method, path, data=None, expected=204):
        return request(self.sock, method, path, data, expected)

    def stop(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.output.close()


def safety():
    state = subprocess.run(
        ["systemctl", "is-active", "leap109-nightly.service"],
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert state not in ("active", "activating"), f"nightly is {state}; do not boot"
    available = int(
        re.search(r"MemAvailable:\s+(\d+)", Path("/proc/meminfo").read_text())[1]
    )
    assert available >= 6 * 1024 * 1024, "host has less than 6 GiB available"
    return subprocess.check_output(["free", "-g"], text=True)


def prepare_rootfs(path):
    assert str(path).startswith("/root/fcexp-"), "only a scratch rootfs copy is allowed"
    assert path.is_file()
    with tempfile.TemporaryDirectory(prefix="bg-rootfs-") as mount:
        subprocess.run(["mount", "-o", "loop", str(path), mount], check=True)
        try:
            script = Path(mount) / "sbin/shinu-bg-proof"
            script.write_text(GUEST)
            script.chmod(0o755)
        finally:
            subprocess.run(["umount", mount], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-rootfs", type=Path)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--kernel", type=Path)
    parser.add_argument("--rootfs", type=Path)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--failure-dir", type=Path)
    parser.add_argument("--mem-mib", type=int, default=1024)
    args = parser.parse_args()
    if args.prepare_rootfs:
        prepare_rootfs(args.prepare_rootfs.resolve())
        return
    assert 512 <= args.mem_mib <= 4096
    for name in ("binary", "kernel", "rootfs", "workdir"):
        assert getattr(args, name), f"--{name} required"
        setattr(args, name, getattr(args, name).resolve())
    assert str(args.rootfs).startswith("/root/fcexp-")
    args.workdir.mkdir()
    results = {"free_before": safety(), "mem_size_mib": args.mem_mib, "vcpu_count": 2}
    expected = hashlib.sha256(bytes(range(256)) * 32768).hexdigest()
    vm = VM(args.binary, args.workdir / "source")
    snapshots = []
    try:
        vm.call(
            "PUT",
            "/machine-config",
            {"vcpu_count": 2, "mem_size_mib": args.mem_mib, "track_dirty_pages": True},
        )
        vm.call(
            "PUT",
            "/boot-source",
            {
                "kernel_image_path": str(args.kernel),
                "boot_args": "console=ttyS0 reboot=k panic=1 pci=off root=/dev/vda ro init=/sbin/shinu-bg-proof",
            },
        )
        vm.call(
            "PUT",
            "/drives/root",
            {
                "drive_id": "root",
                "path_on_host": str(args.rootfs),
                "is_root_device": True,
                "is_read_only": True,
                "io_engine": "Sync",
            },
        )
        vm.call("PUT", "/actions", {"action_type": "InstanceStart"})
        assert wait_for(lambda: alive(vm.log))[0] == expected
        # Failure and abort run first, then a usable Full and Background from this same VM.
        cases = [("abort", args.workdir)]
        if args.failure_dir:
            cases.append(("failure", args.failure_dir.resolve()))
        cases += [
            ("consistency", args.workdir),
            ("Full", args.workdir),
            ("Background", args.workdir),
        ]
        for kind, directory in cases:
            state = args.workdir / f"{kind}.state"
            memory = directory / f"{kind}.mem"
            started = time.monotonic()
            vm.call("PATCH", "/vm", {"state": "Paused"})
            if kind == "consistency":
                # Independent oracle at the same paused cut, then write to the very
                # pages being copied by Background and compare the entire output.
                vm.call(
                    "PUT",
                    "/snapshot/create",
                    {
                        "snapshot_type": "Full",
                        "snapshot_path": str(args.workdir / "oracle.state"),
                        "mem_file_path": str(args.workdir / "oracle.mem"),
                    },
                )
            call_start = time.monotonic()
            vm.call(
                "PUT",
                "/snapshot/create",
                {
                    "snapshot_type": "Full" if kind == "Full" else "Background",
                    "snapshot_path": str(state),
                    "mem_file_path": str(memory),
                    "sync_snapshot_files": True,
                },
            )
            call_ms = (time.monotonic() - call_start) * 1000
            assert vm.call("GET", "/", expected=200)["state"] == "Paused"
            # Root is read-only: the immutable scratch disk is this generation's
            # stable disk root. Real shinu cuts writable roots HERE before resume.
            if kind == "abort":
                vm.call("PUT", "/snapshot/abort", {})
                status = vm.call("GET", "/snapshot/status", expected=200)
                assert status["state"] == "Aborted", status
                assert not state.exists() and not memory.exists()
            before_resume = vm.log.stat().st_size
            vm.call("PATCH", "/vm", {"state": "Resumed"})
            pause_ms = (time.monotonic() - started) * 1000
            if kind != "Full":
                status = wait_for(lambda: terminal_status(vm))
                assert (
                    status["state"]
                    == {
                        "abort": "Aborted",
                        "failure": "Failed",
                        "consistency": "Complete",
                        "Background": "Complete",
                    }[kind]
                ), status
                if kind == "failure":
                    assert not state.exists() and not memory.exists()
                if kind in ("consistency", "Background"):
                    assert (
                        status["wp_faults"] > 0
                    ), "load did not exercise KVM WP faults"
                    assert status["copied_bytes"] == status["total_bytes"]
            else:
                status = {"state": "Complete"}
            completion_ms = (time.monotonic() - started) * 1000
            assert wait_for(lambda: alive(vm.log, before_resume))[0] == expected
            results[kind] = {
                "create_call_ms": call_ms,
                "pause_ms": pause_ms,
                "total_completion_ms": completion_ms,
                "status": status,
            }
            if kind == "consistency":
                with (args.workdir / "oracle.mem").open("rb") as reference, memory.open(
                    "rb"
                ) as actual:
                    while True:
                        chunk = reference.read(8 * 1024 * 1024)
                        assert (
                            actual.read(len(chunk)) == chunk
                        ), "Background differs from Full at the same capture cut"
                        if not chunk:
                            assert not actual.read(1)
                            break
                results[kind]["full_memory_bytes_equal"] = True
            if kind in ("Full", "Background"):
                snapshots.append((kind, state, memory))
        # A rejected new request must not give abort ownership of an old generation.
        vm.call(
            "PUT",
            "/snapshot/create",
            {
                "snapshot_type": "Background",
                "snapshot_path": str(args.workdir / "rejected.state"),
                "mem_file_path": str(args.workdir / "rejected.mem"),
            },
            expected=400,
        )
        vm.call("PUT", "/snapshot/abort", {})
        assert snapshots[-1][1].exists() and snapshots[-1][2].exists()
    finally:
        vm.stop()
    for kind, state, memory in snapshots:
        results[f"free_before_restore_{kind}"] = safety()
        restored = VM(args.binary, args.workdir / f"restore-{kind}")
        try:
            started = time.monotonic()
            restored.call(
                "PUT",
                "/snapshot/load",
                {
                    "snapshot_path": str(state),
                    "mem_backend": {
                        "backend_type": "File",
                        "backend_path": str(memory),
                    },
                    "resume_vm": True,
                },
            )
            assert wait_for(lambda: alive(restored.log))[0] == expected
            results[kind]["restore_to_checksum_ms"] = (
                time.monotonic() - started
            ) * 1000
            results[kind]["restored_checksum"] = expected
            restored.call("PATCH", "/vm", {"state": "Paused"})
            rejected_state = args.workdir / f"rejected-{kind}.state"
            rejected_mem = args.workdir / f"rejected-{kind}.mem"
            restored.call(
                "PUT",
                "/snapshot/create",
                {
                    "snapshot_type": "Background",
                    "snapshot_path": str(rejected_state),
                    "mem_file_path": str(rejected_mem),
                },
                expected=400,
            )
            assert not rejected_state.exists() and not rejected_mem.exists()
            offset = restored.log.stat().st_size
            restored.call("PATCH", "/vm", {"state": "Resumed"})
            assert wait_for(lambda: alive(restored.log, offset))[0] == expected
            results[kind]["file_restore_background_rejected_cleanly"] = True
        finally:
            restored.stop()
    results["free_after"] = subprocess.check_output(["free", "-g"], text=True)
    (args.workdir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    print(json.dumps(results, indent=2))


def terminal_status(vm):
    status = vm.call("GET", "/snapshot/status", expected=200)
    return status if status["state"] != "Running" else None


if __name__ == "__main__":
    main()
