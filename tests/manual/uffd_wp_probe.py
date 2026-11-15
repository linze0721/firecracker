#!/usr/bin/env python3
# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Probe synchronous UFFD-WP on real anonymous/private-file mappings, no VM.

Uses two pages plus a scratch regular file. Includes a kernel read(2) into a
protected anonymous buffer, not just a userspace store. Run under timeout(1).
"""

import ctypes
import errno
import fcntl
import json
import mmap
import os
import select
import struct
import sys
import tempfile
import threading
from pathlib import Path

API = 0xC018AA3F
REGISTER = 0xC020AA00
WRITEPROTECT = 0xC018AA06
WP_UNPOPULATED = 1 << 13
PAGE = os.sysconf("SC_PAGE_SIZE")
LIBC = ctypes.CDLL(None, use_errno=True)


def new_uffd():
    with open("/dev/userfaultfd", "rb", buffering=0) as device:
        fd = fcntl.ioctl(device, 0xAA00, os.O_CLOEXEC | os.O_NONBLOCK)
    api = bytearray(struct.pack("QQQ", 0xAA, 1 | WP_UNPOPULATED, 0))
    fcntl.ioctl(fd, API, api)
    return fd, struct.unpack("QQQ", api)[1]


def register(fd, memory):
    address = ctypes.addressof(ctypes.c_char.from_buffer(memory))
    registration = bytearray(struct.pack("QQQQ", address, PAGE, 2, 0))
    fcntl.ioctl(fd, REGISTER, registration)
    assert struct.unpack("QQQQ", registration)[3] & (1 << 6)
    return address


def copy_fault(kernel_write, populated=True):
    fd, features = new_uffd()
    memory = mmap.mmap(-1, PAGE, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    if populated:
        memory[0] = ord("A")
    result = {"features": features}
    try:
        address = register(fd, memory)
        fcntl.ioctl(fd, WRITEPROTECT, struct.pack("QQQ", address, PAGE, 1))

        def handler():
            assert select.select([fd], [], [], 5)[0], "missing WP event"
            event = os.read(fd, 32)
            flags = struct.unpack_from("Q", event, 8)[0]
            assert event[0] == 0x12 and flags & 2, event.hex()
            result["event_flags"] = flags
            result["old_byte"] = ctypes.string_at(address, 1).decode()
            fcntl.ioctl(fd, WRITEPROTECT, struct.pack("QQQ", address, PAGE, 0))

        thread = threading.Thread(target=handler)
        thread.start()
        if kernel_write:
            read_fd, write_fd = os.pipe()
            try:
                os.write(write_fd, b"B")
                assert LIBC.read(read_fd, ctypes.c_void_p(address), 1) == 1
            finally:
                os.close(read_fd)
                os.close(write_fd)
        else:
            LIBC.memset(ctypes.c_void_p(address), ord("B"), 1)
        thread.join(6)
        assert not thread.is_alive()
        result["new_byte"] = chr(memory[0])
        expected_old = "A" if populated else "\0"
        assert result["old_byte"] == expected_old and result["new_byte"] == "B"
        return result
    finally:
        os.close(fd)
        memory.close()


def main():
    scratch = Path(sys.argv[1]).resolve()
    assert str(scratch).startswith("/root/fcexp-")
    result = {
        "kernel": os.uname().release,
        "userspace_write": copy_fault(False),
        "kernel_read_into_wp": copy_fault(True),
        "unpopulated_userspace_write": copy_fault(False, populated=False),
        "unpopulated_kernel_write": copy_fault(True, populated=False),
    }
    fd, _ = new_uffd()
    try:
        with tempfile.TemporaryFile(dir=scratch) as file:
            file.truncate(PAGE)
            with mmap.mmap(file.fileno(), PAGE, flags=mmap.MAP_PRIVATE) as memory:
                try:
                    register(fd, memory)
                except OSError as error:
                    result["private_file_register_errno"] = error.errno
                    assert error.errno == errno.EINVAL, error
                else:
                    raise AssertionError(
                        "kernel unexpectedly supports private-file synchronous WP"
                    )
    finally:
        os.close(fd)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
