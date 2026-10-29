"""One clock contract for charts, signals, selection and execution."""
INTERVALS = {'1m': 60_000, '3m': 180_000, '5m': 300_000, '15m': 900_000, '1h': 3_600_000}


def timeframe(interval='1h'):
    if not isinstance(interval, str) or interval not in INTERVALS:
        raise ValueError('K线周期仅支持1m、3m、5m、15m、1h')
    duration = INTERVALS[interval]
    return dict(interval=interval, duration_ms=duration,
                label='1小时' if interval == '1h' else interval[:-1]+'分钟',
                entry_window_ms=min(120_000, duration//2), stale_ms=duration+min(60_000,duration//4))


def candles_for(api, symbol, interval='1h'):
    timeframe(interval)
    return api.candles(symbol) if interval == '1h' else api.candles(symbol, interval=interval)
