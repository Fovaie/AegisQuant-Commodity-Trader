# AegisQuant 商品永续交易器

[English](README.md) · **简体中文**

专用于币安 USDT 本位商品永续合约的本地 Python 策略终端，可选 macOS 独立 App。只包含 `binance_trader/`，不包含 moomoo、美股或港股项目。

## Copyright, commercial use & attribution / 版权、商业使用与来源标注

**Copyright © 2026 Frankie (GitHub: Ffffsss123).** Rights in original project code are reserved except as expressly licensed in [LICENSE](LICENSE).

Personal use, modification, commercial use and profit-making use are permitted **only with the required attribution**. Preserve the copyright notice and license; clearly state that the original project comes from **[Ffffsss123/AegisQuant-Commodity-Trader](https://github.com/Ffffsss123/AegisQuant-Commodity-Trader)**. Modified versions must identify their changes and must not claim to be the original author's release. Distributed products and services must display this credit and link in their README/documentation and user-accessible About/Credits page (or equivalent). Internal/private users must retain the notice and source link in their copy. Attribution does not imply endorsement. Unauthorized removal of attribution or other license violations may result in legal action; the author reserves the right to seek remedies available under applicable law.

**版权所有 © 2026 Frankie（GitHub：Ffffsss123）。** 原创项目代码除 [LICENSE](LICENSE) 明确授予的权利外，其余权利保留。

允许个人使用、个人改写、商业使用及盈利使用，**前提是必须保留版权和许可证，明确注明原项目来自 [Ffffsss123/AegisQuant-Commodity-Trader](https://github.com/Ffffsss123/AegisQuant-Commodity-Trader) 并附可访问链接**。修改版必须说明修改情况，不得冒充原作者发布。对外分发的产品或服务，须在 README／说明文档和用户可见的“关于／致谢”或同等页面标注来源；内部或私人使用也须在副本中保留声明和来源链接。标注来源不代表作者背书。擅自删除署名、隐瞒来源或违反许可证的，作者保留依法追究责任、提起诉讼并寻求法律救济的权利。

This is a custom attribution license; third-party dependencies retain their own licenses. / 本项目采用自定义署名许可证；第三方依赖仍适用各自许可证。

## 运行方法

建议 Python 3.11 及以上：

```sh
git clone https://github.com/Ffffsss123/AegisQuant-Commodity-Trader.git
cd AegisQuant-Commodity-Trader
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m binance_trader
```

打开 http://127.0.0.1:8766 。首次启动为本地模拟，使用公开行情，无需币安密钥；界面支持中英文。关闭网页不会关闭后台，终端按 Ctrl+C 停止。账本默认保存在 `data/binance/`，可通过 `BINANCE_TRADER_DATA` 指定目录，不能上传到 Git。API 密钥正常情况下仅存内存；macOS 钥匙串保存为可选功能。ChatGPT 登录另有本地令牌缓存。

## 品种范围

| 交易对 | 商品 |
| --- | --- |
| XAUUSDT | 黄金 |
| CLUSDT | WTI 原油 |
| BZUSDT | 布伦特原油 |
| XAGUSDT | 白银 |
| NATGASUSDT | 天然气 |

这是以 USDT 为保证金的衍生品交易，不是购买实物黄金或原油。能否交易及限额以币安、账户、地区和实时交易规则为准。

## 基本运行逻辑

1. 采集报价、标记价格、K 线、交易精度、费率和保证金档位。
2. 采集新闻及已接入的官方统计，按新闻 ID、正文版本和统计修订去重。轮询目标不等于新闻到达时延保证。
3. 新增或修订材料才调用可选 AI 接口。无新增且来源健康时，保留原分析、方向和原始时间，不因超过 15 分钟自动失效，也不重复调用；材料改变或来源异常时重新校验。聊天连接和策略 AI 连接各自独立，普通聊天回复不会直接下单。
4. 自动模式同时比较五个品种，合格候选按扣除预估成本后的计划盈亏比排序，最多持有一个；全部不合格就空仓。计划盈亏比不是盈利概率。
5. 有持仓时聚焦持有品种的常规模型分析；其他品种新重大事件只提示机会，不强制平仓换仓。原仓位按自身止盈、止损及合格反向信号退出，空仓后重新选品。
6. 保存账本和防重复记录，订单结果不明时锁定核对。重启保持新开仓暂停。

## 交易逻辑与风控

- 只用已收盘 K 线：EMA20 高于 EMA60 且收盘突破此前 20 根最高价时做多；反向条件做空。至少需要 80 根已收盘 K 线，拒绝过期、不连续或异常行情。
- 支持 1m、3m、5m、15m、1h，收盘后的入场窗口分别为 30、90、120、120、120 秒。新账本默认 1h；每秒刷新界面不会绕过窗口。
- 有效 AI 综合方向必须与收盘突破同向。新闻不足、方向混合或模型不可用时，可使用健康 K 线备用路径；来源中断、过期数据、突发事件冷静期及账户风控仍会限制开仓。长期风险背景本身不会无限期禁仓。
- 止损距离取 2×ATR14 和价格 1% 中较大者，超过 8% 则跳过；初始止盈距离为止损的两倍。不摊平加仓。
- 按计划损失预算计算数量，包含手续费、滑点和资金费预留，按交易步长向下取整。当前单笔名义价值上限 150 USDT，保证金和预留不超过可用策略资金 20%，且须通过交易所杠杆档位和强平缓冲核验。
- 新模拟账本为 300 USDT、2×、单笔计划损失 1.5 USDT；已有账本恢复原配置。新实盘试运行预算上限 100 USDT，每日亏损阈值为适用预算 2%，累计为 15%。触发后停止新开仓并尝试平仓，实际执行取决于连接及交易所接受情况。
- 实盘需单独连接并确认启动；当前支持单币保证金、单向持仓及逐仓风控。结果不明的订单不盲目重试。暂停新开仓时继续保护已有持仓；退出后台后本机监控停止。

这是实验性策略，不承诺收益；止损不保证实际损失严格受限，杠杆可能放大亏损。AI 解读不能视为已验证的预测准确率；自动化测试不等于实盘盈利或所有故障下成交保护均已验收。

## 测试与打包

```sh
python -m unittest discover -s binance_trader/tests
node --test binance_trader/tests/test_*_ui.js
```

界面测试需要 Node.js。macOS 打包需要 Xcode Command Line Tools 和 PyInstaller：

```sh
python -m pip install pyinstaller
BUILD_PYTHON="$PWD/.venv/bin/python" zsh binance_trader/desktop/build.sh
```

输出为 `商品策略交易器.app`，构建产物不随源码发布。本地签名不代表已完成 Apple 公证。详细文件结构见 [英文主页](README.md#project-layout)。第三方依赖、新闻与数据服务仍受其自身许可证和服务条款约束；本项目不是币安官方产品。
