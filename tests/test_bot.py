import json
import unittest
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from bot import Config, Snapshot, State, USDT, WBNB, apply_decision, decide, parse_snapshot, process_once


def config() -> Config:
    return Config.load(Path(__file__).resolve().parents[1] / "config.example.json")


def snapshot(**updates) -> Snapshot:
    original = Snapshot(
        fetched_at_ms=1_800_000_000_000,
        pair_address="0x" + "1" * 40,
        base_symbol="WBNB",
        quote_symbol="USDT",
        base_usd=100.0,
        quote_usd=1.0,
        quote_per_base=100.0,
        liquidity_usd=1_000_000.0,
        volume_m5_usd=10_000.0,
        change_m5_pct=0.6,
        buys_m5=30,
        sells_m5=10,
        pool_age_hours=48.0,
        bnb_usd=100.0,
        bnb_ema9=101.0,
        bnb_ema21=100.0,
        last_closed_candle_ms=1_799_999_999_999,
    )
    return replace(original, **updates)


class PaperBotTests(unittest.TestCase):
    def test_buy_charges_costs_and_sell_stop_loss(self):
        cfg = config()
        first = snapshot()
        state = State.new(cfg, first)
        self.assertEqual(state.quote_qty, 20.0)
        self.assertAlmostEqual(state.initial_equity_usd, 20.79)
        self.assertEqual(decide(cfg, state, first)[0], "buy")
        buy = apply_decision(cfg, state, first, "buy", "entry")
        self.assertLess(buy["equity_usd"], state.initial_equity_usd)
        self.assertEqual(state.quote_qty, 0.0)
        self.assertAlmostEqual(state.base_qty, 20 * 0.9925 / 100)
        self.assertAlmostEqual(state.gas_bnb_qty, 0.79 / 100 - 0.05 / 100)
        drop = snapshot(fetched_at_ms=first.fetched_at_ms + 60_000,
                        quote_per_base=95.0, base_usd=95.0, bnb_usd=95.0)
        self.assertEqual(decide(cfg, state, drop), ("sell", "paper stop loss"))

    def test_wrong_pool_and_low_liquidity_never_buy(self):
        cfg = config()
        state = State.new(cfg, snapshot())
        self.assertEqual(decide(cfg, state, snapshot(pair_address="0x" + "2" * 40))[0], "halt")
        self.assertEqual(decide(cfg, state, snapshot(liquidity_usd=10))[0], "hold")
        state.base_qty = 0.2
        state.quote_qty = 0.0
        self.assertEqual(decide(cfg, state, snapshot(liquidity_usd=10))[0], "halt")

    def test_parser_rejects_wrong_contract_and_stale_candle(self):
        cfg = config()
        now_ms = 1_800_000_000_000
        pair = {
            "chainId": "bsc", "dexId": "pancakeswap",
            "pairAddress": "0x" + "1" * 40,
            "baseToken": {"address": WBNB, "symbol": "WBNB"},
            "quoteToken": {"address": USDT, "symbol": "USDT"},
            "priceUsd": "100", "priceNative": "100",
            "liquidity": {"usd": 1_000_000},
            "volume": {"m5": 5000},
            "txns": {"m5": {"buys": 20, "sells": 10}},
            "priceChange": {"m5": 0.6},
        }
        candles = [[now_ms - 23 * 60_000 + i * 60_000,
                    "100", "100", "100", "100", "1",
                    now_ms - 22 * 60_000 + i * 60_000 - 1]
                   for i in range(22)]
        self.assertEqual(parse_snapshot([pair], candles, cfg, now_ms).base_symbol, "WBNB")
        wrong = json.loads(json.dumps(pair))
        wrong["baseToken"]["address"] = "0x" + "2" * 40
        with self.assertRaisesRegex(ValueError, "no exact PancakeSwap pool"):
            parse_snapshot([wrong], candles, cfg, now_ms)
        old_candles = [c.copy() for c in candles]
        for candle in old_candles:
            candle[6] -= 10 * 60_000
        with self.assertRaisesRegex(ValueError, "stale"):
            parse_snapshot([pair], old_candles, cfg, now_ms)

    def test_state_persists_without_repeat_buy(self):
        cfg = config()
        test_id = uuid4().hex
        state_path = Path.cwd() / f".test-state-{test_id}.json"
        events_path = Path.cwd() / f".test-events-{test_id}.jsonl"
        try:
            first = process_once(cfg, state_path, events_path, snapshot())
            second = process_once(cfg, state_path, events_path,
                                  snapshot(fetched_at_ms=1_800_000_060_000))
            self.assertEqual(first["action"], "buy")
            self.assertEqual(second["action"], "hold")
            self.assertEqual(len(events_path.read_text().splitlines()), 2)
        finally:
            state_path.unlink(missing_ok=True)
            events_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()

