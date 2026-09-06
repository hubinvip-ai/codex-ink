import ctypes
import errno
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from tools.companion_setup import MacSystem, SetupError


@unittest.skipUnless(sys.platform == 'darwin', 'Darwin process inspection')
class ProcessInspectionTests(unittest.TestCase):
    def test_real_zombie_does_not_block_inspection(self):
        child = os.fork()
        if child == 0:
            os._exit(0)
        try:
            for _ in range(40):
                state = subprocess.check_output(['/bin/ps', '-p', str(child), '-o', 'stat='], text=True).strip()
                if state.startswith('Z'): break
                time.sleep(.025)
            self.assertTrue(state.startswith('Z'))
            adapter = MacSystem()
            real_run = adapter.run
            def owned_process_listing(argv):
                # Exercise the real zombie/sysctl path without inspecting unrelated
                # hosted-runner services whose process arguments may be protected.
                if argv == ['/bin/ps', '-axo', 'pid=,uid=']:
                    return real_run(['/bin/ps', '-p', str(child), '-o', 'pid=,uid='])
                return real_run(argv)
            with patch.object(adapter, 'run', side_effect=owned_process_listing):
                rows = adapter.processes()
            self.assertNotIn(child, [r['pid'] for r in rows])
        finally:
            os.waitpid(child, 0)

    def test_unreadable_live_pid_is_not_ignored(self):
        target = os.getppid()
        def denied(*args):
            ctypes.set_errno(errno.EPERM)
            return -1
        class Lib:
            sysctl = staticmethod(denied)
        adapter = MacSystem()
        with patch.object(adapter, 'run', side_effect=[
            subprocess.CompletedProcess([], 0, f'{target} {os.getuid()}\n'.encode(), b''),
            subprocess.CompletedProcess([], 0, f'{target} {os.getuid()} S\n'.encode(), b''),
        ]), patch('tools.companion_setup.ctypes.CDLL', return_value=Lib()):
            with self.assertRaises(SetupError): adapter.processes()
