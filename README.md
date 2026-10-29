# AegisQuant Commodity Trader

**English** · [简体中文](README.zh-CN.md)

A local Python strategy terminal and optional macOS desktop app for **Binance USDⓈ-M commodity perpetual contracts**. This repository contains only `binance_trader/`; the separate moomoo/US/HK equities project is not included.

## Copyright, commercial use & attribution / 版权、商业使用与来源标注

**Copyright © 2026 Frankie (GitHub: Ffffsss123).** Rights in original project code are reserved except as expressly licensed in [LICENSE](LICENSE).

Personal use, modification, commercial use and profit-making use are permitted **only with the required attribution**. Preserve the copyright notice and license; clearly state that the original project comes from **[Ffffsss123/AegisQuant-Commodity-Trader](https://github.com/Ffffsss123/AegisQuant-Commodity-Trader)**. Modified versions must identify their changes and must not claim to be the original author's release. Distributed products and services must display this credit and link in their README/documentation and user-accessible About/Credits page (or equivalent). Internal/private users must retain the notice and source link in their copy. Attribution does not imply endorsement. Unauthorized removal of attribution or other license violations may result in legal action; the author reserves the right to seek remedies available under applicable law.

**版权所有 © 2026 Frankie（GitHub：Ffffsss123）。** 原创项目代码除 [LICENSE](LICENSE) 明确授予的权利外，其余权利保留。

允许个人使用、个人改写、商业使用及盈利使用，**前提是必须保留版权和许可证，明确注明原项目来自 [Ffffsss123/AegisQuant-Commodity-Trader](https://github.com/Ffffsss123/AegisQuant-Commodity-Trader) 并附可访问链接**。修改版必须说明修改情况，不得冒充原作者发布。对外分发的产品或服务，须在 README／说明文档和用户可见的“关于／致谢”或同等页面标注来源；内部或私人使用也须在副本中保留声明和来源链接。标注来源不代表作者背书。擅自删除署名、隐瞒来源或违反许可证的，作者保留依法追究责任、提起诉讼并寻求法律救济的权利。

This is a custom attribution license; third-party dependencies retain their own licenses. / 本项目采用自定义署名许可证；第三方依赖仍适用各自许可证。

## Supported markets

| Symbol | Market |
| --- | --- |
| XAUUSDT | Gold |
| CLUSDT | WTI crude oil |
| BZUSDT | Brent crude oil |
| XAGUSDT | Silver |
| NATGASUSDT | Natural gas |

These are exchange-listed USDT-margined derivatives, not physical commodity purchases. Availability and limits depend on Binance, account eligibility and region; the program validates exchange metadata.

## Run locally

Use Python 3.11 or later and a virtual environment:

```sh
git clone https://github.com/Ffffsss123/AegisQuant-Commodity-Trader.git
cd AegisQuant-Commodity-Trader
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m binance_trader
```

Open **http://127.0.0.1:8766**. First launch uses a local paper ledger and public market data; Binance credentials are not required for paper mode. The interface supports English and Chinese. Configure settings before starting a strategy. Closing the browser does not stop the backend; use Ctrl+C in the terminal. Do not run two processes against the same ledger.

Runtime state defaults to `data/binance/`. Set `BINANCE_TRADER_DATA` to use another private directory. Keep this directory out of Git. API keys are entered locally; normal launches keep them in memory. Optional macOS Keychain persistence requires explicit opt-in (`COMMODITY_SAVE_CREDENTIALS=1` or the desktop preference). ChatGPT OAuth has its own local token cache. No credentials, balances, trade histories or downloaded news are distributed here.

## How it runs

1. Fetch Binance quotes, exchange rules, mark prices and candles; maintain per-symbol analysis.
2. Collect public news and available official statistics. Polling targets are not guaranteed delivery latency. The UI can refresh each second without creating a trading signal each second.
3. Deduplicate news IDs/content versions and statistic revisions. Call the optional configured AI service only for new/revised inputs. With unchanged inputs and healthy sources, retain the original analysis, direction and timestamp without a fixed 15-minute expiry or repeated model charge. Changed inputs or failed source checks invalidate use of the old direction.
4. In automatic selection mode compare the five instruments, choose at most one eligible candidate, and stay flat if none qualify. Candidate ranking uses planned net reward/risk after cost estimates, not a probability of profit.
5. With a position, focus routine AI work on the held instrument. New major events elsewhere may generate opportunity notices; they do not force rotation. Resume selection after exiting under the existing position rules.
6. Persist state, reconcile uncertain orders and retain risk locks. Restart keeps new entries paused. Optional ChatGPT chat and structured strategy AI are separate connections; a chat response alone does not place an order.

## Trading rules

- Signals use **closed candles**: EMA20 above EMA60 plus a close above the preceding 20-candle high for a long; the inverse for a short. At least 80 closed candles are required. Stale, malformed or discontinuous data is rejected.
- Available periods: 1m, 3m, 5m, 15m and 1h. Entry windows are respectively 30, 90, 120, 120 and 120 seconds after candle close. The current fresh-ledger default is 1h. One-second chart refresh does not bypass these windows.
- A valid overall AI direction filters for a matching candle breakout. Mixed, unavailable or unconfirmed news/AI direction can use the healthy K-line fallback. Source failures, stale inputs, acute-event cooldowns and account/risk failures still restrict new entries. Long-running risk background alone does not prohibit trading indefinitely.
- Stop distance is the greater of 2×ATR14 and 1% of price; distances above 8% are rejected. The initial target is twice the stop distance. Exits include stop/target protection and qualifying opposite signals; there is no averaging down.
- Quantity is sized from the planned loss budget and rounded down to exchange precision, including taker-fee, adverse-slippage and funding reserves. Current code limits notional to 150 USDT and required margin plus reserves to 20% of available strategy funds. Leverage must pass current account brackets and liquidation-buffer checks; selecting leverage does not commit all wallet funds.
- A fresh paper ledger starts at 300 USDT, 2× leverage and 1.5 USDT planned loss per trade. Existing ledger settings are restored. Fresh live trial capital is capped at 100 USDT; daily and cumulative loss limits are 2% and 15% of the applicable budget. Crossing a loss threshold stops new entries and attempts to exit, subject to connectivity and exchange acceptance.
- Live mode requires a separately connected account and explicit start confirmation. The current risk model requires single-asset margin and one-way positions, with isolated-margin handling. Unknown order outcomes lock execution for reconciliation rather than blind retries.

The strategy is experimental. Planned reward/risk is not realized profit, stop execution is not a guaranteed maximum loss, and leverage can magnify losses. Model interpretation is not independently verified prediction accuracy. Automated tests do not establish profitability or prove live exchange fills/protection under every failure.

## Development and macOS packaging

```sh
python -m unittest discover -s binance_trader/tests
# Optional frontend tests; requires Node.js
node --test binance_trader/tests/test_*_ui.js
```

To build the macOS app, install Xcode Command Line Tools and PyInstaller in the environment, then run:

```sh
python -m pip install pyinstaller
BUILD_PYTHON="$PWD/.venv/bin/python" zsh binance_trader/desktop/build.sh
```

The script creates `AegisQuant Commodity Trader.app`; generated apps and runtime directories are intentionally excluded from this source repository. Local ad-hoc signing does not constitute Apple notarization.

## Project layout

- `binance_trader/core.py`, `strategy.py`, `selection.py`: closed-candle signals, eligibility and ranking.
- `engine.py`, `runtime.py`, `api.py`, `risk.py`: execution, account reconciliation and risk sizing.
- `news.py`, `news_risk.py`, `fundamentals.py`, `commodity_sources.py`: news/background and statistics.
- `ai_service.py`, `ai_policy.py`, `ai_news.py`: deduplication, structured interpretation and direction checks.
- `static/`: local web interface; `desktop/`: macOS launcher and packaging; `tests/`: isolated regression tests.

## Third-party services

Binance, news publishers, official data providers and optional model services have their own availability, access rules and terms. This repository does not grant rights over their content, APIs, trademarks or dependencies and is not an official Binance product. Contact the author through this repository's Issues for attribution or licensing questions.
