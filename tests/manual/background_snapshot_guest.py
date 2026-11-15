# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Runs inside the scratch VM, with the shared checkpoint mounted read-only."""

import hashlib
import mmap
import threading
import time

path = "/dev/shm/bg-proof"
data = bytes(range(256)) * 32768
with open(path, "wb") as output:
    output.write(data)
expected = hashlib.sha256(data).hexdigest()
del data
ram = mmap.mmap(-1, 128 * 1024 * 1024)
# Touch pages before READY, so WP must invalidate existing writable KVM mappings.
ram[:] = b"X" * len(ram)


def writer():
    tick = 0
    while True:
        tick += 1
        payload = bytes([tick % 256]) * 4096
        for position in range(0, len(ram), 4096):
            ram[position : position + 4096] = payload


threading.Thread(target=writer, daemon=True).start()
print("BG_READY checksum=" + expected, flush=True)
tick = 0
while True:
    with open(path, "rb") as source:
        actual = hashlib.sha256(source.read()).hexdigest()
    assert actual == expected
    tick += 1
    # Also exercise host virtio-blk writes into guest buffers.
    with open("/usr/bin/python3", "rb") as source:
        source.read(1024 * 1024)
    print("BG_ALIVE checksum=" + actual + " tick=" + str(tick), flush=True)
    time.sleep(0.25)
