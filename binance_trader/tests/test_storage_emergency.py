"""Post-fill disk-failure risk reduction with fake venue and temp ledger only."""
import sqlite3,tempfile,unittest,json
from unittest.mock import patch
import test_manual_depth as fixture
from binance_trader.api import APIError

class StorageEmergencyTests(unittest.TestCase):
    def run_failure(self,stage,unknown=False,unresolved=False):
        with tempfile.TemporaryDirectory() as directory:
            e,api=fixture.ManualDepthTests().make(directory,'live')
            save=e.save;original=api.request;failed=[False]
            def broken_save():
                trigger=(stage=='filled' and e.s['position'] is not None or stage=='protection' and len(api.algos)==1 or stage=='close' and e.s['pending'] and e.s['pending']['kind']=='close')
                if trigger or failed[0]:
                    failed[0]=True
                    if unresolved and e.s['position']:e.s['position']['entry_unresolved']=True
                    raise sqlite3.OperationalError('disk full')
                save()
            if stage=='close':api.reject_algo=True
            def request(method,path,params=None,signed=False):
                if unknown and method=='POST' and path=='/fapi/v1/order' and params.get('reduceOnly')=='true':
                    api.calls.append((method,path,params));raise APIError('unknown response')
                return original(method,path,params,signed)
            try:
                with patch.object(e,'save',side_effect=broken_save),patch.object(api,'request',side_effect=request):
                    with self.assertRaises(Exception):e.maybe_enter()
                    closes=[p for m,path,p in api.calls if m=='POST' and path=='/fapi/v1/order' and p.get('reduceOnly')=='true']
                    self.assertEqual(len(closes),1)
                    self.assertFalse(e.s['enabled']);self.assertTrue(e.s['halted'])
                    if unknown:
                        self.assertTrue(api.positions);self.assertIsNotNone(e.s['pending'])
                        with self.assertRaises(ValueError):e.storage_emergency_close()
                        e.sync_demo()
                        self.assertEqual(len([1 for m,path,p in api.calls if m=='POST' and path=='/fapi/v1/order' and p.get('reduceOnly')=='true']),1)
                    else:
                        self.assertEqual(api.positions,[])
                        if unresolved:
                            self.assertTrue(api.algos);self.assertEqual(e.s['pending']['kind'],'entry')
                        else:self.assertEqual(api.algos,{})
                persisted=json.loads(e.db.execute('SELECT body FROM state WHERE id=1').fetchone()[0])
                self.assertIsNotNone(persisted['pending'])
            finally:e.db.close()
    def test_disk_full_after_fill_still_reduces_confirmed_exposure(self):self.run_failure('filled')
    def test_disk_full_after_first_protection_still_reduces_exposure(self):self.run_failure('protection')
    def test_disk_full_before_emergency_close_does_not_prevent_reduction(self):self.run_failure('close')
    def test_unknown_emergency_result_is_never_retried(self):self.run_failure('filled',True)
    def test_unresolved_entry_keeps_protection_after_reducing_known_fill(self):self.run_failure('protection',unresolved=True)
