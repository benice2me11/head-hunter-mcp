from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest

from hh_mcp_server.platform_io import acquire_exclusive_lock


class PlatformIoTests(unittest.TestCase):
    def test_exclusive_lock_rejects_second_holder_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.lock"
            first = acquire_exclusive_lock(path)
            try:
                with self.assertRaises(BlockingIOError):
                    acquire_exclusive_lock(path)
            finally:
                os.close(first)

            second = acquire_exclusive_lock(path)
            os.close(second)

    def test_exclusive_lock_waits_for_other_process_to_release(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.lock"
            ready = Path(folder) / "ready"
            code = (
                "from pathlib import Path; import os, sys, time; "
                "from hh_mcp_server.platform_io import acquire_exclusive_lock; "
                "fd = acquire_exclusive_lock(Path(sys.argv[1])); "
                "Path(sys.argv[2]).write_text('ready', encoding='utf-8'); "
                "time.sleep(0.4); os.close(fd)"
            )
            process = subprocess.Popen([sys.executable, "-c", code, str(path), str(ready)])
            try:
                deadline = time.monotonic() + 2.0
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "child process did not acquire the lock")

                started = time.monotonic()
                fd = acquire_exclusive_lock(path, wait_seconds=2.0, poll_interval=0.02)
                elapsed = time.monotonic() - started
                try:
                    self.assertGreaterEqual(elapsed, 0.15)
                finally:
                    os.close(fd)
            finally:
                process.wait(timeout=3)

    def test_exclusive_lock_times_out_after_bounded_wait(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.lock"
            first = acquire_exclusive_lock(path)
            try:
                started = time.monotonic()
                with self.assertRaises(BlockingIOError):
                    acquire_exclusive_lock(path, wait_seconds=0.15, poll_interval=0.02)
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, 0.10)
                self.assertLess(elapsed, 1.0)
            finally:
                os.close(first)
