# Copyright 2026 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""CLI safety checks without starting Firecracker or requiring KVM/systemd."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SnapshotPreflightTest(unittest.TestCase):
    def test_unconfirmed_nightly_state_never_starts_firecracker(self):
        """A query error is not evidence that nightly is inactive."""
        harness = Path(__file__).with_name("background_snapshot.py")
        for state, exit_code in [("", 1), ("activating", 0), ("inactive", 1)]:
            with self.subTest(
                state=state, exit_code=exit_code
            ), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / "systemctl").write_text(
                    f"#!/bin/sh\nprintf '%s\\n' '{state}'\nexit {exit_code}\n"
                )
                (root / "systemctl").chmod(0o755)
                marker = root / "started"
                binary = root / "firecracker"
                binary.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
                binary.chmod(0o755)
                environment = {**os.environ, "PATH": f"{root}:/usr/bin:/bin"}
                environment.pop("PYTHONOPTIMIZE", None)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(harness),
                        "--binary",
                        str(binary),
                        "--kernel",
                        str(root / "kernel"),
                        "--initrd",
                        str(root / "initrd"),
                        "--rootfs",
                        "/root/fcexp-shared/test.ext4",
                        "--workdir",
                        str(root / "run"),
                        "--failure-dir",
                        "/root/fcexp-test/small",
                    ],
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("nightly query must confirm inactive", result.stderr)
                self.assertFalse(
                    marker.exists(), "unsafe preflight spawned Firecracker"
                )


if __name__ == "__main__":
    unittest.main()
