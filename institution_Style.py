# ============================================================
#  INSTITUTIONAL MOMENTUM BREAKOUT STRATEGY
#  Engine : QuantConnect LEAN (local, free)
#  Universe: US Equities (daily bars)
#
#  LOGIC
#  ─────
#  Phase 1 – "Sleeping" filter
#    • Over the past LOOKBACK_WEAK candles the stock must have
#      been LOW-momentum: ADX < ADX_WEAK_THRESH
#      AND  abs(price change) < WEAK_MOVE_PCT
#
#  Phase 2 – "Ignition" trigger (last N candles)
#    • Most-recent IGNITION_CANDLES candles show a strong
#      momentum surge: ADX > ADX_STRONG_THRESH
#      AND  price change > STRONG_MOVE_PCT  (bullish burst)
#
#  Entry  : Market open next day after signal fires
#  Target : Entry × (1 + TAKE_PROFIT_PCT)
#  Stop   : Entry × (1 - STOP_LOSS_PCT)
#  Max hold: MAX_HOLD_DAYS candles → forced exit at market
# ============================================================

from AlgorithmImports import *

# ── TUNABLE PARAMETERS (edit these freely) ─────────────────
UNIVERSE_SIZE       = 100    # top N stocks by dollar volume to scan
LOOKBACK_WEAK       = 20     # candles to measure "quiet" phase
WEAK_MOVE_PCT       = 0.05   # max allowed price move in quiet phase (5 %)
ADX_WEAK_THRESH     = 20     # ADX must be BELOW this to be "weak"

IGNITION_CANDLES    = 3      # candles that show the momentum burst
STRONG_MOVE_PCT     = 0.04   # min price move in ignition window (4 %)
ADX_STRONG_THRESH   = 25     # ADX must be ABOVE this in ignition window

TAKE_PROFIT_PCT     = 0.08   # 8 % take-profit
STOP_LOSS_PCT       = 0.04   # 4 %  stop-loss  → R:R = 2 : 1
MAX_HOLD_DAYS       = 10     # force-exit after 10 trading days
MAX_POSITIONS       = 5      # max concurrent trades
POSITION_SIZE_PCT   = 0.18   # 18 % of portfolio per position
# ───────────────────────────────────────────────────────────


class InstitutionalMomentumBreakout(QCAlgorithm):

    def Initialize(self):
        # ── Back-test window ───────────────────────────────
        self.SetStartDate(2019, 1, 1)
        self.SetEndDate(2024, 1, 1)
        self.SetCash(100_000)

        self.SetBrokerageModel(BrokerageName.InteractiveBrokersBrokerage,
                               AccountType.Margin)

        # ── Universe selection ─────────────────────────────
        self.AddUniverse(self.SelectUniverse)

        # ── Indicator store ────────────────────────────────
        # symbol → { "adx": ADX, "closes": RollingWindow, "entry": float,
        #             "stop": float, "target": float, "hold": int }
        self.data = {}

        # ── Scheduling ─────────────────────────────────────
        self.Schedule.On(
            self.DateRules.EveryDay(),
            self.TimeRules.AfterMarketOpen("SPY", 5),
            self.ScanAndTrade
        )

        self.SetWarmUp(LOOKBACK_WEAK + IGNITION_CANDLES + 5)

        # ── Benchmark ──────────────────────────────────────
        self.SetBenchmark("SPY")

        # ── Logging helpers ────────────────────────────────
        self._trade_log = []


    # ── Universe filter ────────────────────────────────────
    def SelectUniverse(self, fundamentals):
        # Keep only common US equities with real dollar volume
        filtered = [
            f for f in fundamentals
            if f.HasFundamentalData
            and f.SecurityReference.SecurityType == "ST00000001"   # common stock
            and f.MarketCap > 500_000_000          # > $500 M market cap
            and f.EarningReports.BasicAverageShares.ThreeMonths > 0
        ]
        # Sort by 3-month average dollar volume descending
        sorted_by_vol = sorted(
            filtered,
            key=lambda f: f.DollarVolume,
            reverse=True
        )
        return [f.Symbol for f in sorted_by_vol[:UNIVERSE_SIZE]]


    # ── Called when a new security enters the universe ─────
    def OnSecuritiesChanged(self, changes: SecurityChanges):
        for sec in changes.AddedSecurities:
            sym = sec.Symbol
            if sym not in self.data:
                adx = self.ADX(sym, 14, Resolution.Daily)
                closes = RollingWindow[float](LOOKBACK_WEAK + IGNITION_CANDLES + 2)
                self.data[sym] = {
                    "adx":    adx,
                    "closes": closes,
                    "entry":  None,
                    "stop":   None,
                    "target": None,
                    "hold":   0
                }
                # Warm up history
                hist = self.History(sym,
                                    LOOKBACK_WEAK + IGNITION_CANDLES + 30,
                                    Resolution.Daily)
                for bar in hist.itertuples():
                    closes.Add(float(bar.close))

        for sec in changes.RemovedSecurities:
            sym = sec.Symbol
            if sym in self.data and self.Portfolio[sym].Invested:
                self.Liquidate(sym, "Universe removed")
            self.data.pop(sym, None)


    # ── Daily bar update ───────────────────────────────────
    def OnData(self, data: Slice):
        for sym, d in self.data.items():
            if data.Bars.ContainsKey(sym):
                d["closes"].Add(float(data.Bars[sym].Close))


    # ── Core scan – runs each morning ─────────────────────
    def ScanAndTrade(self):
        if self.IsWarmingUp:
            return

        open_positions = [s for s in self.Portfolio.Keys
                          if self.Portfolio[s].Invested]

        # ── 1. Manage existing positions ──────────────────
        for sym in list(open_positions):
            d = self.data.get(sym)
            if d is None or d["entry"] is None:
                continue

            price = self.Securities[sym].Price
            d["hold"] += 1

            hit_target = price >= d["target"]
            hit_stop   = price <= d["stop"]
            timed_out  = d["hold"] >= MAX_HOLD_DAYS

            if hit_target or hit_stop or timed_out:
                reason = ("TARGET" if hit_target else
                          "STOP"   if hit_stop   else "TIMEOUT")
                pnl_pct = (price / d["entry"] - 1) * 100
                self.Log(f"EXIT {sym} | {reason} | "
                         f"entry={d['entry']:.2f} exit={price:.2f} "
                         f"pnl={pnl_pct:.1f}%")
                self._trade_log.append({
                    "symbol": str(sym), "reason": reason,
                    "entry": d["entry"], "exit": price,
                    "pnl_pct": round(pnl_pct, 2)
                })
                self.Liquidate(sym, reason)
                d["entry"] = d["stop"] = d["target"] = None
                d["hold"]  = 0

        # ── 2. Look for new entries ────────────────────────
        open_positions = [s for s in self.Portfolio.Keys
                          if self.Portfolio[s].Invested]
        if len(open_positions) >= MAX_POSITIONS:
            return

        candidates = []
        for sym, d in self.data.items():
            if self.Portfolio[sym].Invested:
                continue
            if not d["adx"].IsReady:
                continue
            if not d["closes"].IsReady:
                continue

            closes = [d["closes"][i]
                      for i in range(d["closes"].Count - 1, -1, -1)]
            # closes[0] = oldest, closes[-1] = most recent

            if len(closes) < LOOKBACK_WEAK + IGNITION_CANDLES:
                continue

            # ── Quiet phase check ──────────────────────────
            quiet_closes = closes[:LOOKBACK_WEAK]
            quiet_move   = abs(quiet_closes[-1] / quiet_closes[0] - 1)
            adx_val      = d["adx"].Current.Value

            quiet_ok = (quiet_move < WEAK_MOVE_PCT and
                        adx_val < ADX_WEAK_THRESH)
                    # ADX check at signal bar

            # ── Ignition phase check ───────────────────────
            ignition_closes = closes[LOOKBACK_WEAK:]
            ignition_move   = (ignition_closes[-1] / ignition_closes[0] - 1)

            ignition_ok = (ignition_move > STRONG_MOVE_PCT and
                            adx_val > ADX_STRONG_THRESH)

            if quiet_ok and ignition_ok:
                # Score by ignition strength
                candidates.append((sym, ignition_move, adx_val))

        # Sort strongest ignition first
        candidates.sort(key=lambda x: x[1], reverse=True)

        slots = MAX_POSITIONS - len(open_positions)
        for sym, move, adx_v in candidates[:slots]:
            price  = self.Securities[sym].Price
            if price <= 0:
                continue

            target = price * (1 + TAKE_PROFIT_PCT)
            stop   = price * (1 - STOP_LOSS_PCT)
            alloc  = POSITION_SIZE_PCT

            self.SetHoldings(sym, alloc)
            d = self.data[sym]
            d["entry"]  = price
            d["stop"]   = stop
            d["target"] = target
            d["hold"]   = 0

            self.Log(
                f"ENTRY {sym} | price={price:.2f} | "
                f"move={move*100:.1f}% | ADX={adx_v:.1f} | "
                f"target={target:.2f} | stop={stop:.2f}"
            )


    # ── End-of-algo summary ───────────────────────────────
    def OnEndOfAlgorithm(self):
        wins   = [t for t in self._trade_log if t["pnl_pct"] > 0]
        losses = [t for t in self._trade_log if t["pnl_pct"] <= 0]
        total  = len(self._trade_log)

        self.Log("=" * 60)
        self.Log("STRATEGY SUMMARY")
        self.Log(f"  Total trades  : {total}")
        if total:
            self.Log(f"  Win rate      : {len(wins)/total*100:.1f}%")
            avg_win  = sum(t['pnl_pct'] for t in wins)  / max(len(wins), 1)
            avg_loss = sum(t['pnl_pct'] for t in losses)/ max(len(losses), 1)
            self.Log(f"  Avg win       : {avg_win:.2f}%")
            self.Log(f"  Avg loss      : {avg_loss:.2f}%")
            self.Log(f"  Expected R:R  : {abs(avg_win/avg_loss):.2f}")
        self.Log("=" * 60)