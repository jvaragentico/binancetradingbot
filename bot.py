"""Read-only BNB Chain market monitor and paper trading simulator.

There is intentionally no wallet signing or transaction submission in this project.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
USDT = "0x55d398326f99059ff775485246999027b3197955"
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
DEX_URL = "https://api.dexscreener.com/token-pairs/v1/bsc/"
BINANCE_URL = (
    "https://data-api.binance.vision/api/v3/klines"
    "?symbol=BNBUSDT&interval=1m&limit=40"
)


def utc_now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def positive_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return number


@dataclass(frozen=True)
class Config:
    base_token_address: str
    quote_token_address: str
    initial_asset: str
    starting_usd: float
    gas_reserve_usd: float
    poll_seconds: int
    min_liquidity_usd: float
    min_volume_m5_usd: float
    min_m5_trades: int
    min_pool_age_hours: int
    fee_pct_per_side: float
    slippage_pct_per_side: float
    gas_usd_per_swap: float
    stop_loss_pct: float
    take_profit_pct: float
    max_drawdown_pct: float
    max_swaps_per_day: int
    cooldown_minutes: int
    min_trade_usd: float

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = json.loads(path.read_text(encoding="utf-8"))
        cfg = cls(**data)
        if not ADDRESS.fullmatch(cfg.base_token_address):
            raise ValueError("base_token_address must be a BNB Chain token contract")
        if not ADDRESS.fullmatch(cfg.quote_token_address):
            raise ValueError("quote_token_address must be a BNB Chain token contract")
        if cfg.base_token_address.lower() == cfg.quote_token_address.lower():
            raise ValueError("base and quote token addresses must differ")
        if cfg.quote_token_address.lower() not in (WBNB, USDT):
            raise ValueError("quote token must be WBNB or Binance-Peg BSC-USD (USDT)")
        if cfg.initial_asset not in ("base", "quote"):
            raise ValueError("initial_asset must be base or quote")
        for name in (
            "starting_usd", "gas_reserve_usd", "poll_seconds",
            "min_liquidity_usd", "min_volume_m5_usd", "min_m5_trades",
            "gas_usd_per_swap", "stop_loss_pct",
            "take_profit_pct", "max_drawdown_pct", "max_swaps_per_day",
            "cooldown_minutes", "min_trade_usd",
        ):
            positive_number(getattr(cfg, name), name)
        if cfg.gas_reserve_usd >= cfg.starting_usd:
            raise ValueError("gas_reserve_usd must be below starting_usd")
        if cfg.min_pool_age_hours < 0:
            raise ValueError("min_pool_age_hours cannot be negative")
        if not 0 <= cfg.fee_pct_per_side < 100:
            raise ValueError("fee_pct_per_side must be between 0 and 100")
        if not 0 <= cfg.slippage_pct_per_side < 100:
            raise ValueError("slippage_pct_per_side must be between 0 and 100")
        if cfg.fee_pct_per_side + cfg.slippage_pct_per_side >= 100:
            raise ValueError("combined per-side costs must be below 100 percent")
        if not 0 < cfg.max_drawdown_pct < 100:
            raise ValueError("max_drawdown_pct must be between 0 and 100")
        return cfg


@dataclass(frozen=True)
class Snapshot:
    fetched_at_ms: int
    pair_address: str
    base_symbol: str
    quote_symbol: str
    base_usd: float
    quote_usd: float
    quote_per_base: float
    liquidity_usd: float
    volume_m5_usd: float
    change_m5_pct: float
    buys_m5: int
    sells_m5: int
    pool_age_hours: float
    bnb_usd: float
    bnb_ema9: float
    bnb_ema21: float
    last_closed_candle_ms: int


def http_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "BNBChainPaperBot/0.1", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(4_000_001)
    if len(raw) > 4_000_000:
        raise ValueError("market data response exceeds 4 MB")
    return json.loads(raw)


def ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError(f"need at least {period} closed candles")
    result = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def parse_snapshot(
    pairs: Any, candles: Any, cfg: Config, now_ms: int | None = None
) -> Snapshot:
    now_ms = now_ms or utc_now_ms()
    if not isinstance(pairs, list):
        raise ValueError("DEX Screener did not return a pair list")
    matches = [
        pair for pair in pairs
        if pair.get("chainId") == "bsc"
        and pair.get("dexId") == "pancakeswap"
        and pair.get("baseToken", {}).get("address", "").lower()
        == cfg.base_token_address.lower()
        and pair.get("quoteToken", {}).get("address", "").lower()
        == cfg.quote_token_address.lower()
    ]
    if not matches:
        raise ValueError("no exact PancakeSwap pool for the configured token pair")
    pair = max(matches, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
    if not isinstance(candles, list):
        raise ValueError("Binance did not return candles")
    closed = [c for c in candles if isinstance(c, list) and len(c) >= 7 and int(c[6]) < now_ms]
    if len(closed) < 21:
        raise ValueError("Binance returned fewer than 21 closed 1m candles")
    last_close_ms = int(closed[-1][6])
    if now_ms - last_close_ms > 180_000 or last_close_ms > now_ms:
        raise ValueError("Binance candles are stale or future-dated")
    closes = [positive_number(c[4], "Binance close") for c in closed]
    bnb_usd = closes[-1]
    base_usd = positive_number(pair.get("priceUsd"), "DEX priceUsd")
    quote_per_base = positive_number(pair.get("priceNative"), "DEX priceNative")
    quote_usd = bnb_usd if cfg.quote_token_address.lower() == WBNB else 1.0
    implied_base_usd = quote_per_base * quote_usd
    if abs(implied_base_usd / base_usd - 1) > 0.03:
        raise ValueError("DEX USD and quote prices disagree by over 3 percent")
    if cfg.base_token_address.lower() == WBNB and abs(base_usd / bnb_usd - 1) > 0.03:
        raise ValueError("DEX WBNB and Binance BNB prices disagree by over 3 percent")
    txns_m5 = (pair.get("txns") or {}).get("m5") or {}
    price_change = pair.get("priceChange") or {}
    liquidity = pair.get("liquidity") or {}
    volume = pair.get("volume") or {}
    created_at = int(pair.get("pairCreatedAt") or 0)
    pool_age_hours = (now_ms - created_at) / 3_600_000 if created_at else 0.0
    return Snapshot(
        fetched_at_ms=now_ms,
        pair_address=pair.get("pairAddress", ""),
        base_symbol=pair["baseToken"]["symbol"],
        quote_symbol=pair["quoteToken"]["symbol"],
        base_usd=base_usd,
        quote_usd=quote_usd,
        quote_per_base=quote_per_base,
        liquidity_usd=float(liquidity.get("usd") or 0),
        volume_m5_usd=float(volume.get("m5") or 0),
        change_m5_pct=float(price_change.get("m5") or 0),
        buys_m5=int(txns_m5.get("buys") or 0),
        sells_m5=int(txns_m5.get("sells") or 0),
        pool_age_hours=pool_age_hours,
        bnb_usd=bnb_usd,
        bnb_ema9=ema(closes, 9),
        bnb_ema21=ema(closes, 21),
        last_closed_candle_ms=last_close_ms,
    )


def fetch_snapshot(cfg: Config) -> Snapshot:
    pairs = http_json(DEX_URL + cfg.base_token_address)
    candles = http_json(BINANCE_URL)
    return parse_snapshot(pairs, candles, cfg)


@dataclass
class State:
    started_at_ms: int
    initial_equity_usd: float
    initial_base_qty: float
    initial_quote_qty: float
    initial_gas_bnb_qty: float
    base_qty: float
    quote_qty: float
    gas_bnb_qty: float
    entry_price_quote: float | None
    last_swap_ms: int | None
    swaps_today: int
    day_utc: str
    halted: bool
    halt_reason: str | None
    pair_address: str

    @classmethod
    def new(cls, cfg: Config, snap: Snapshot) -> "State":
        trade_usd = cfg.starting_usd
        base_qty = trade_usd / snap.base_usd if cfg.initial_asset == "base" else 0.0
        quote_qty = trade_usd / snap.quote_usd if cfg.initial_asset == "quote" else 0.0
        gas_qty = cfg.gas_reserve_usd / snap.bnb_usd
        return cls(
            started_at_ms=snap.fetched_at_ms,
            initial_equity_usd=cfg.starting_usd + cfg.gas_reserve_usd,
            initial_base_qty=base_qty,
            initial_quote_qty=quote_qty,
            initial_gas_bnb_qty=gas_qty,
            base_qty=base_qty,
            quote_qty=quote_qty,
            gas_bnb_qty=gas_qty,
            entry_price_quote=snap.quote_per_base if base_qty else None,
            last_swap_ms=None,
            swaps_today=0,
            day_utc=iso(snap.fetched_at_ms)[:10],
            halted=False,
            halt_reason=None,
            pair_address=snap.pair_address,
        )


def equity(state: State, snap: Snapshot) -> float:
    return (
        state.base_qty * snap.base_usd
        + state.quote_qty * snap.quote_usd
        + state.gas_bnb_qty * snap.bnb_usd
    )


def hodl_equity(state: State, snap: Snapshot) -> float:
    return (
        state.initial_base_qty * snap.base_usd
        + state.initial_quote_qty * snap.quote_usd
        + state.initial_gas_bnb_qty * snap.bnb_usd
    )


def market_gate(cfg: Config, snap: Snapshot) -> str | None:
    if snap.liquidity_usd < cfg.min_liquidity_usd:
        return "liquidity below minimum"
    if snap.volume_m5_usd < cfg.min_volume_m5_usd:
        return "five-minute volume below minimum"
    if snap.buys_m5 + snap.sells_m5 < cfg.min_m5_trades:
        return "too few recent pool trades"
    if cfg.min_pool_age_hours and snap.pool_age_hours < cfg.min_pool_age_hours:
        return "pool too new or age unavailable"
    return None


def decide(cfg: Config, state: State, snap: Snapshot) -> tuple[str, str]:
    if state.pair_address.lower() != snap.pair_address.lower():
        return "halt", "pool changed since paper portfolio initialization"
    if state.halted:
        return "hold", state.halt_reason or "portfolio halted"
    gate = market_gate(cfg, snap)
    if gate:
        return ("halt" if state.base_qty > 0 else "hold"), gate
    pnl_pct = (equity(state, snap) / state.initial_equity_usd - 1) * 100
    if pnl_pct <= -cfg.max_drawdown_pct:
        return ("sell" if state.base_qty > 0 else "halt"), "maximum drawdown reached"
    if state.base_qty > 0:
        if state.entry_price_quote:
            entry_return = (snap.quote_per_base / state.entry_price_quote - 1) * 100
            if entry_return <= -cfg.stop_loss_pct:
                return "sell", "paper stop loss"
            if entry_return >= cfg.take_profit_pct:
                return "sell", "paper take profit"
        if snap.change_m5_pct < -0.5 or snap.sells_m5 > snap.buys_m5 * 1.3:
            return "sell", "DEX short-term momentum turned down"
        if snap.bnb_ema9 < snap.bnb_ema21 * 0.9998:
            return "sell", "BNB market trend turned down"
        return "hold", "holding base token"
    if state.last_swap_ms and snap.fetched_at_ms - state.last_swap_ms < cfg.cooldown_minutes * 60_000:
        return "hold", "cooldown active"
    if state.swaps_today >= cfg.max_swaps_per_day:
        return "hold", "daily swap cap reached"
    if state.quote_qty * snap.quote_usd < cfg.min_trade_usd:
        return "hold", "trade size below minimum"
    if state.gas_bnb_qty * snap.bnb_usd < cfg.gas_usd_per_swap + 0.25:
        return "hold", "BNB gas reserve too low"
    bullish_bnb = snap.bnb_ema9 > snap.bnb_ema21 * 1.0002
    bullish_token = snap.change_m5_pct > 0.3 and snap.buys_m5 >= snap.sells_m5 * 1.2
    if bullish_bnb and bullish_token:
        return "buy", "BNB trend and DEX flow positive"
    return "hold", "entry signal absent"


def apply_decision(cfg: Config, state: State, snap: Snapshot, action: str, reason: str) -> dict:
    event: dict[str, Any] = {
        "time": iso(snap.fetched_at_ms), "mode": "paper", "action": action,
        "reason": reason, "pair": snap.pair_address,
    }
    if action == "halt":
        state.halted = True
        state.halt_reason = reason
    if action in ("buy", "sell"):
        gas_bnb = cfg.gas_usd_per_swap / snap.bnb_usd
        if state.gas_bnb_qty < gas_bnb + 0.25 / snap.bnb_usd:
            event["action"] = "hold"
            event["reason"] = "insufficient simulated BNB gas reserve"
        else:
            haircut = 1 - (cfg.fee_pct_per_side + cfg.slippage_pct_per_side) / 100
            if action == "buy":
                spent = state.quote_qty
                received = spent * haircut / snap.quote_per_base
                state.quote_qty = 0.0
                state.base_qty = received
                state.entry_price_quote = snap.quote_per_base / haircut
                event.update({"spent_quote": spent, "received_base": received})
            else:
                spent = state.base_qty
                received = spent * snap.quote_per_base * haircut
                state.base_qty = 0.0
                state.quote_qty = received
                state.entry_price_quote = None
                event.update({"spent_base": spent, "received_quote": received})
            state.gas_bnb_qty -= gas_bnb
            state.last_swap_ms = snap.fetched_at_ms
            state.swaps_today += 1
            event["gas_usd_assumed"] = cfg.gas_usd_per_swap
    if reason == "maximum drawdown reached":
        state.halted = True
        state.halt_reason = reason
    event.update({
        "equity_usd": round(equity(state, snap), 6),
        "trading_equity_usd": round(
            state.base_qty * snap.base_usd + state.quote_qty * snap.quote_usd, 6
        ),
        "hodl_equity_usd": round(hodl_equity(state, snap), 6),
        "base_qty": state.base_qty,
        "quote_qty": state.quote_qty,
        "gas_bnb_qty": state.gas_bnb_qty,
        "halted": state.halted,
    })
    return event


def save_state(path: Path, state: State) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(asdict(state), indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def process_once(cfg: Config, state_path: Path, events_path: Path, snap: Snapshot) -> dict:
    if state_path.exists():
        state = State(**json.loads(state_path.read_text(encoding="utf-8")))
    else:
        state = State.new(cfg, snap)
    today = iso(snap.fetched_at_ms)[:10]
    if state.day_utc != today:
        state.day_utc = today
        state.swaps_today = 0
    action, reason = decide(cfg, state, snap)
    event = apply_decision(cfg, state, snap, action, reason)
    event.update({
        "base_symbol": snap.base_symbol,
        "quote_symbol": snap.quote_symbol,
        "base_usd": snap.base_usd,
        "bnb_usd": snap.bnb_usd,
        "quote_usd": snap.quote_usd,
        "liquidity_usd": snap.liquidity_usd,
        "volume_m5_usd": snap.volume_m5_usd,
        "change_m5_pct": snap.change_m5_pct,
        "buys_m5": snap.buys_m5,
        "sells_m5": snap.sells_m5,
    })
    save_state(state_path, state)
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    return event


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("once", "run", "status", "report"))
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--state", type=Path, default=Path("state.json"))
    parser.add_argument("--events", type=Path, default=Path("events.jsonl"))
    parser.add_argument("--snapshot", type=Path, help="offline raw JSON with dex_pairs and binance_klines")
    parser.add_argument("--hours", type=float, help="stop a paper run after this many hours")
    args = parser.parse_args()
    if args.hours is not None and (
        args.command != "run" or not math.isfinite(args.hours) or args.hours <= 0
    ):
        parser.error("--hours requires `run` and a positive finite number")
    deadline = time.monotonic() + args.hours * 3600 if args.hours else None
    try:
        if args.command == "status":
            if not args.state.exists():
                print("No paper portfolio yet. Run `python bot.py once`.")
            else:
                print(args.state.read_text(encoding="utf-8"))
            return 0
        if args.command == "report":
            if not args.state.exists() or not args.events.exists():
                print("No paper portfolio yet. Run `python bot.py once`.")
                return 0
            state = State(**json.loads(args.state.read_text(encoding="utf-8")))
            lines = args.events.read_text(encoding="utf-8").splitlines()
            last = json.loads(lines[-1])
            trading = last.get("trading_equity_usd")
            if trading is None:
                trading = last["base_qty"] * last["base_usd"] + last["quote_qty"]
            swaps = sum(json.loads(line).get("action") in ("buy", "sell") for line in lines)
            print(json.dumps({
                "mode": "paper", "started_at": iso(state.started_at_ms),
                "last_market_check": last["time"], "last_action": last["action"],
                "last_reason": last["reason"],
                "starting_trade_usd": Config.load(args.config).starting_usd,
                "trading_equity_usd": round(trading, 4),
                "estimated_total_equity_usd": last["equity_usd"],
                "estimated_hodl_equity_usd": last["hodl_equity_usd"],
                "paper_swaps": swaps, "halted": state.halted,
            }, indent=2))
            return 0
        cfg = Config.load(args.config)
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                print("Paper run duration completed.")
                return 0
            try:
                if args.snapshot:
                    raw = json.loads(args.snapshot.read_text(encoding="utf-8"))
                    snap = parse_snapshot(raw["dex_pairs"], raw["binance_klines"], cfg)
                else:
                    snap = fetch_snapshot(cfg)
                print(json.dumps(process_once(cfg, args.state, args.events, snap), indent=2))
            except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
                print(f"Cycle failed safely: {exc}", file=sys.stderr)
                if args.command == "once":
                    return 1
            if args.command == "once":
                return 0
            wait_seconds = cfg.poll_seconds
            if deadline is not None:
                wait_seconds = min(wait_seconds, max(0, deadline - time.monotonic()))
            time.sleep(wait_seconds)
    except (OSError, ValueError, TypeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


