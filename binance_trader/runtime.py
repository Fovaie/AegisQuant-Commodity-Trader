"""One process, separate ledgers and explicit paper/live selection.

Switching the displayed environment never grants a client exchange write access.
Only the live credentials form installs an explicitly live-capable client.
"""
import json
import threading
import uuid
from pathlib import Path
from .api import Binance
from .engine import Engine


class Runtime:
    def __init__(self, directory, readonly=None, initial_mode=None, engines=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.readonly = readonly
        self.engines = engines or {
            "paper": Engine(Binance(), self.directory/"paper.sqlite", "paper"),
            "live": Engine(Binance(live_trading=True), self.directory/"live.sqlite", "live"),
        }
        saved = "paper"
        config = self.directory/"runtime.json"
        if config.exists():
            value = json.loads(config.read_text())
            saved = value.get("mode", "paper")
        requested = initial_mode or saved
        if requested not in self.engines:
            raise ValueError("运行环境配置无效")
        self._mode = requested
        # An existing exposure must remain visible after restart, even if a
        # stale window preference says otherwise. Credentials remain absent.
        occupied = [name for name, e in self.engines.items() if e.s["position"] or e.s["pending"]]
        if len(occupied) > 1:
            raise ValueError("多个环境有待处理持仓；请分别核对账本，禁止隐藏持仓")
        if occupied:
            self._mode = occupied[0]
        self.context_id = uuid.uuid4().hex
        self.analysis = None
        self.engines["paper"].leverage_limits = self.paper_limits
        self.engines["paper"].fee_rates = self.paper_fees

    @property
    def engine(self):
        return self.engines[self._mode]

    @property
    def mode(self):
        return self._mode

    @property
    def api(self):
        return self.engine.api

    @property
    def s(self):
        return self.engine.s

    def paper_client(self):
        if self.readonly is None:
            raise ValueError("请先连接模拟环境的只读API，以读取账户费率和保证金档位")
        return self.readonly

    def paper_limits(self, symbol, force=False):
        return self.paper_client().leverage_limits(symbol, force=force)

    def paper_fees(self, symbol, force=False):
        return self.paper_client().commission_rates(symbol, force=force)

    def set_analysis(self, analysis):
        self.analysis = analysis
        for engine in self.engines.values():
            engine.news_provider = analysis.decision
            engine.candidate_provider = analysis.candidates

    def validate_context(self, body):
        if body.get("context_mode") != self.mode or body.get("context_id") != self.context_id:
            raise ValueError("页面环境已改变，请刷新后重新操作；未提交交易")

    def rotate_context(self):
        self.context_id = uuid.uuid4().hex

    def switch_mode(self, mode):
        if mode not in self.engines:
            raise ValueError("只支持本地模拟与实盘环境")
        if mode == self.mode:
            return
        for engine in self.engines.values():
            if engine.s["enabled"] or engine.s["position"] or engine.s["pending"]:
                raise ValueError("请先暂停并核对当前持仓与订单，再切换环境")
        temp = self.directory/"runtime.json.tmp"
        temp.write_text(json.dumps({"mode": mode}))
        temp.replace(self.directory/"runtime.json")
        self._mode = mode
        self.rotate_context()

    def snapshot(self):
        with self.lock:
            value = self.engine.snapshot()
            value.update(application="commodity-strategy-terminal", available_modes=list(self.engines),
                         context_id=self.context_id, readonly_connected=self.readonly is not None,
                         live_connected=bool(getattr(self.engines["live"], "live_connected", False)))
            return value

    def tick(self):
        with self.lock:
            self.engine.tick()

    def close_databases(self):
        for engine in self.engines.values():
            engine.db.close()
