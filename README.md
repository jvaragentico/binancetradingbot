# BNB Chain paper trading bot

This project watches a PancakeSwap WBNB/USDT pool and Binance's BNB/USDT spot market, then simulates swaps. It **never connects to a wallet, requests a private key, signs a transaction, or places a real order**.

The example configuration models **$20 USDT to trade plus $0.79 of BNB for gas**, matching the requested trading budget and approximately the BNB gas balance shown in the wallet screenshot. The wallet address and its holdings are not stored in this repository. This is a separate paper portfolio, not a live wallet balance mirror.

## Run on Windows

Python 3.11 or newer is required. There are no third-party Python packages and Docker is not needed.

```powershell
Copy-Item config.example.json config.json
python bot.py once
python bot.py run
```

`once` fetches one market snapshot and makes one paper decision. `run` repeats every 60 seconds until Ctrl+C. `python bot.py status` prints the saved paper portfolio; `python bot.py report` summarizes estimated equity and swaps. `state.json` and `events.jsonl` are created locally and ignored by Git. Running `once` again uses the existing state; to start a **new** simulation, save or remove those two local files first. Keep the configuration fixed during a simulation.

The default configuration starts in USDT. It monitors WBNB/USDT; it does **not** automatically buy WBNB on startup. An entry requires both a rising Binance BNB trend and positive five-minute DEX price and buy/sell flow. The bot can hold USDT for long periods.

## How decisions are made

- DEX Screener supplies an exact BNB Chain PancakeSwap pool match for the configured WBNB and USDT contract addresses. Among matching pools, the bot selects the one with the highest reported liquidity. It reads price, liquidity, five-minute volume, price change, and trade counts.
- Binance's public market-data API supplies one-minute BNB/USDT candles. Only closed candles are used, and candles older than three minutes stop that cycle.
- An entry needs a 9-period EMA above the 21-period EMA by at least 0.02%, DEX price change above 0.3% over five minutes, and at least 20% more buys than sells. A falling DEX price/flow or Binance trend can trigger a simulated sale. This rule set is an **untested starter strategy**, not evidence of an edge.
- The bot skips entries when reported pool liquidity is under $100,000, five-minute volume is under $1,000, or fewer than five trades were reported. If any of those conditions appear while holding WBNB, it halts the paper portfolio for review because a fill cannot be trusted. It also blocks entries when simulated gas is too low, the daily eight-swap cap was hit, or the cooldown is active. It has a 4% paper stop loss, 6% paper take profit, and 10% portfolio drawdown halt.
- A paper swap assumes a 0.25% pool/route fee, 0.5% slippage, and $0.05 gas **per side**. These are model inputs, not live PancakeSwap quotes. For a $20 round trip, the assumed fee and slippage total roughly $0.30, plus $0.10 gas; the price must move about 2% in your favor just to offset these assumptions.

`events.jsonl` records every decision with estimated portfolio value and a buy-and-hold baseline. `state.json` stores quantities and the halt flag. The bot refuses to continue if the highest-liquidity matching pool changes after initialization, to avoid silently switching price sources.

## Limits before any live trading work

This poller is **near real time**, not a low-latency trading engine. DEX Screener's pair response does not provide a guaranteed quote timestamp or executable swap quote. Paper fills use its reported price and fixed costs; actual swaps can differ because of routing, changing liquidity, token taxes, failed transactions, approval costs, MEV, and gas. The paper stop loss works only while the process and both feeds are working. The bot does not validate whether a custom token can be sold.

The requested $20-to-$100 outcome is a **400% gain in three days** (about 71% compounded per day). No strategy here is validated to produce that return. Run the paper portfolio, inspect the trade log and costs, and judge results against simply holding USDT or BNB before considering live execution. Do not put a seed phrase or private key in `config.json` or this repository.

## Sources

- [DEX Screener pair API](https://docs.dexscreener.com/api/reference)
- [Binance public market data API](https://developers.binance.com/en/docs/products/spot/rest-api)
- [BNB Chain RPC documentation](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/)
- [PancakeSwap routes and fees](https://docs.pancakeswap.finance/trade/pancakeswap-exchange/fees-and-routes)

Run `python -m unittest discover -s tests -v` to check the parser and paper risk logic.

