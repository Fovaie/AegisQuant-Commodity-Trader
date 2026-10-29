"""Process exclusion for temporary ledgers; no exchange/network operations."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from binance_trader.__main__ import acquire_ledger_locks


PROBE = '''
import sys
from binance_trader.__main__ import acquire_ledger_locks
try:
    handles = acquire_ledger_locks(sys.argv[1], sys.argv[2:])
except SystemExit:
    print("locked")
    raise SystemExit(73)
else:
    print("acquired")
    for handle in handles: handle.close()
'''


class RuntimeLockTests(unittest.TestCase):
    def test_same_ledger_excludes_another_process_and_alias_path(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp)/'actual'; directory.mkdir()
            alias=Path(temp)/'alias'; alias.symlink_to(directory,target_is_directory=True)
            handles=acquire_ledger_locks(directory,['runtime','paper','live'])
            try:
                for path in (directory,alias):
                    result=subprocess.run([sys.executable,'-c',PROBE,str(path),'live'],capture_output=True,text=True,timeout=5)
                    self.assertEqual(result.returncode,73,result.stderr)
                    self.assertEqual(result.stdout.strip(),'locked')
                with self.assertRaises(SystemExit):
                    acquire_ledger_locks(alias,['paper'])
            finally:
                for handle in handles: handle.close()
            result=subprocess.run([sys.executable,'-c',PROBE,str(alias),'runtime','paper','live'],capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),'acquired')

    def test_failed_multi_lock_acquisition_releases_earlier_partial_locks(self):
        with tempfile.TemporaryDirectory() as temp:
            held=acquire_ledger_locks(temp,['paper'])
            try:
                with self.assertRaises(SystemExit):
                    acquire_ledger_locks(temp,['runtime','paper','live'])
                # Sorted acquisition obtains live before colliding with paper.
                # A failed start must not accidentally retain that first lock.
                probe=acquire_ledger_locks(temp,['live'])
                for handle in probe: handle.close()
                with self.assertRaises(SystemExit): acquire_ledger_locks(temp,['paper'])
            finally:
                for handle in held: handle.close()

