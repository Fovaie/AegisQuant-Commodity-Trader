import errno
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from binance_trader.engine import Engine
from binance_trader.news_risk import RiskLedger
from test_trader import Fake


class StorageRecoveryTests(unittest.TestCase):
    def test_double_save_failure_does_not_kill_tick_or_rearm_after_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            engine=Engine(Fake(),Path(folder)/'paper.sqlite','paper')
            engine.s['enabled']=True
            try:
                with patch.object(engine,'save',side_effect=sqlite3.OperationalError('database or disk is full')), patch('traceback.print_exc'):
                    engine.tick()
                self.assertFalse(engine.s['enabled'])
                self.assertEqual(engine.decision['source'],'storage')
                self.assertFalse(engine.risk_preview['ok'])
                engine.tick()
                self.assertFalse(engine.s['enabled'])
                self.assertNotIn('交易账本保存失败',engine.error)
            finally:engine.db.close()

    def test_news_io_failure_retries_without_dropping_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'news.json';ledger=RiskLedger(path)
            with patch('binance_trader.news_risk.os.replace',side_effect=OSError(errno.ENOSPC,'full')):
                ledger.save()
            self.assertTrue(ledger.error);self.assertTrue(ledger._retryable_write_error)
            ledger.update('XAUUSDT',[],1)
            self.assertFalse(ledger.error);self.assertFalse(ledger._retryable_write_error)
            self.assertTrue(path.exists());self.assertFalse(RiskLedger(path).error)

    def test_corrupt_ledger_is_not_overwritten_by_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'news.json';path.write_text('corrupt')
            ledger=RiskLedger(path);ledger.update('XAUUSDT',[],1)
            self.assertTrue(ledger.error);self.assertFalse(ledger._retryable_write_error)
            self.assertEqual(path.read_text(),'corrupt')
