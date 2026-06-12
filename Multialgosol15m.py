from AlgorithmImports import *
from datetime import timedelta
from collections import deque


# ─────────────────────────────────────────────────────────────────────────────
#  REGIME IDs
# ─────────────────────────────────────────────────────────────────────────────
REGIME_TRENDING    = "TRENDING"
REGIME_RANGING     = "RANGING"
REGIME_PANIC       = "PANIC"
REGIME_UNKNOWN     = "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────────
#  DEFAULTS  — all tuneable via QC parameter optimiser
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULTS = {
    # Symbol / TF
    "symbol":                   "SOLUSDT",
    "timeframe_minutes":        15,

    # ── Risk / Sizing ─────────────────────────────────────────────────────────
    "risk_pct":                 0.01,       # 1% risk per trade — adjust later
    "max_pos_pct":              0.15,       # max 15% equity in one position
    "max_margin_pct":           0.40,       # total margin cap
    "min_rr":                   1.5,        # minimum reward:risk to take a trade

    # ── Drawdown protection ───────────────────────────────────────────────────
    "max_drawdown_pct":         0.10,       # halt if DD from peak > 10%
    "loss_streak_n":            3,          # consecutive losses before scaling
    "loss_streak_scale":        0.5,        # scale risk by this on streak
    "loss_streak_floor":        0.5,        # never go below risk_pct * floor

    # ── ATR ───────────────────────────────────────────────────────────────────
    "atr_period":               14,
    "sl_buffer_atr":            0.4,

    # ── REGIME DETECTOR (ADX + Volatility) ───────────────────────────────────
    "adx_period":               14,
    "adx_trend_thresh":         25,         # ADX > 25  → trending
    "adx_range_thresh":         18,         # ADX < 18  → ranging
    "volatility_lookback":      20,
    "panic_vol_mult":           2.5,        # vol > N x rolling avg → panic

    # ── STRATEGY 1: SMC / BOS + Retest ───────────────────────────────────────
    "smc_pivot_left":           4,
    "smc_pivot_right":          4,
    "smc_bos_atr_min_mult":     0.05,
    "smc_vol_spike_mult":       1.5,
    "smc_vol_lookback":         20,
    "smc_retest_zone_pct":      0.015,
    "smc_vp_lookback":          40,
    "smc_vp_bins":              24,
    "smc_vp_zone_pct":          0.025,
    "smc_fib_mult":             1.618,      # TP = 1.618 extension of swing
    "smc_max_open_bars":        48,
    "smc_bos_hold_bars":        1,
    "smc_conf_body_atr_mult":   0.3,
    "smc_min_swing_atr_mult":   1.5,

    # ── STRATEGY 2: Momentum / Trend Following ────────────────────────────────
    "mom_fast_ema":             9,
    "mom_slow_ema":             21,
    "mom_signal_ema":           5,          # EMA of (fast-slow) as momentum signal
    "mom_adx_min":              25,         # only trade momentum when ADX strong
    "mom_sl_atr_mult":          1.5,
    "mom_tp_atr_mult":          3.0,

    # ── STRATEGY 3: Mean Reversion (RSI + Bollinger) ──────────────────────────
    "mr_rsi_period":            14,
    "mr_rsi_oversold":          30,
    "mr_rsi_overbought":        70,
    "mr_bb_period":             20,
    "mr_bb_std":                2.0,
    "mr_sl_atr_mult":           1.2,
    "mr_tp_atr_mult":           2.0,

    # ── STRATEGY 4: Sentiment / Funding Rate Fade ─────────────────────────────
    # Crypto-specific: extreme negative funding = shorts crowded = buy signal
    # extreme positive funding = longs crowded = sell signal
    # We proxy funding sentiment via RSI extremes + vol spike + price vs VWAP
    "sent_rsi_period":          7,          # short RSI to catch fast extremes
    "sent_oversold":            20,         # panic sell threshold
    "sent_overbought":          80,         # euphoria threshold
    "sent_vol_spike_mult":      2.0,        # vol must spike on sentiment extreme
    "sent_vwap_lookback":       96,         # bars for rolling VWAP (96 x 15m = 24h)
    "sent_sl_atr_mult":         2.0,        # wider SL — these are volatile entries
    "sent_tp_atr_mult":         3.5,
}


_TF_MAP = {
    1: Resolution.MINUTE, 5: Resolution.MINUTE, 15: Resolution.MINUTE,
    30: Resolution.MINUTE, 60: Resolution.HOUR
}


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN ALGORITHM
# ─────────────────────────────────────────────────────────────────────────────
class MultiAlgoSOL(QCAlgorithm):
    """
    Multi-algorithm regime-routed strategy for SOLUSDT 15m on Binance.

    Architecture:
      ┌─ Regime Detector (ADX + Volatility) ─────────────────┐
      │  TRENDING  → Strategy 1 (SMC/BOS) + Strategy 2 (Mom) │
      │  RANGING   → Strategy 3 (Mean Reversion)              │
      │  PANIC     → Strategy 4 (Sentiment Fade)              │
      └───────────────────────────────────────────────────────┘
      All routed through unified Kelly-based position sizer
      and shared drawdown / loss-streak protection.
    """

    def initialize(self):
        self.set_start_date(2023, 1, 1)
        self.set_end_date(2026, 2, 1)
        self.set_account_currency("USDT")
        self.set_cash("USDT", 50_000)
        self.set_brokerage_model(BrokerageName.BINANCE, AccountType.MARGIN)

        # ── Symbol ────────────────────────────────────────────────────────────
        contract      = self.add_crypto("SOLUSDT", Resolution.MINUTE, Market.BINANCE)
        self._sym     = contract.symbol
        self._tf_mins = int(self._p("timeframe_minutes", int))

        # ── Core Indicators (shared) ──────────────────────────────────────────
        self._atr = self.atr(self._sym, self._p("atr_period", int), MovingAverageType.WILDERS)
        self._adx = self.adx(self._sym, self._p("adx_period", int))

        # ── Strategy 2: Momentum EMAs ─────────────────────────────────────────
        self._ema_fast = self.ema(self._sym, self._p("mom_fast_ema", int))
        self._ema_slow = self.ema(self._sym, self._p("mom_slow_ema", int))

        # ── Strategy 3: Mean Reversion ────────────────────────────────────────
        self._rsi_mr  = self.rsi(self._sym, self._p("mr_rsi_period", int))
        self._bb      = self.bb(self._sym, self._p("mr_bb_period", int), self._p("mr_bb_std"))

        # ── Strategy 4: Sentiment ─────────────────────────────────────────────
        self._rsi_sent = self.rsi(self._sym, self._p("sent_rsi_period", int))

        # ── Bar buffer (all strategies share it) ──────────────────────────────
        buf = max(
            self._p("smc_vp_lookback", int),
            self._p("smc_vol_lookback", int),
            self._p("sent_vwap_lookback", int),
            self._p("volatility_lookback", int),
            self._p("smc_pivot_left", int) + self._p("smc_pivot_right", int) + 10,
        ) + 20
        self._bars = deque(maxlen=buf)

        # ── Consolidator ──────────────────────────────────────────────────────
        self.consolidate(self._sym, timedelta(minutes=self._tf_mins), self._on_bar)

        # ── Shared state ──────────────────────────────────────────────────────
        self._bar_idx        = 0
        self._regime         = REGIME_UNKNOWN
        self._peak_equity    = 50_000.0
        self._consec_losses  = 0
        self._trading_paused = False
        self._position_active= False
        self._sl             = 0.0
        self._tp             = 0.0
        self._active_strategy= None

        # ── SMC state ─────────────────────────────────────────────────────────
        self._smc_pivot_highs       = deque(maxlen=30)
        self._smc_pivot_lows        = deque(maxlen=30)
        self._smc_bos_active        = False
        self._smc_bos_dir           = None
        self._smc_bos_level         = None
        self._smc_bos_sl_ref        = None
        self._smc_bos_origin        = None
        self._smc_bos_bar_idx       = -1
        self._smc_bos_held          = False
        self._smc_retest_logged     = False

        # ── Stats ─────────────────────────────────────────────────────────────
        self.stats = {
            # Regime
            "regime_trending":          0,
            "regime_ranging":           0,
            "regime_panic":             0,
            # Per-strategy trades
            "smc_trades":               0,
            "mom_trades":               0,
            "mr_trades":                0,
            "sent_trades":              0,
            # Outcomes
            "closed_tp":                0,
            "closed_sl":                0,
            # Filters
            "paused_dd":                0,
            "scaled_risk":              0,
            "skipped_rr":               0,
            "skipped_lotsize":          0,
            # SMC detail
            "smc_bos_activated":        0,
            "smc_bos_expired":          0,
            "smc_bos_no_hold":          0,
            "smc_entry_no_retest":      0,
            "smc_entry_vp_miss":        0,
            "smc_entry_no_confirm":     0,
        }

        self.log(
            f"=== MultiAlgoSOL INIT | SOLUSDT {self._tf_mins}m | "
            f"2023-01-01 → 2026-02-01 | Capital=50,000 USDT ==="
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  PARAMETER HELPER
    # ─────────────────────────────────────────────────────────────────────────
    def _p(self, key, cast=float):
        v = self.get_parameter(key)
        return cast(v) if v is not None else cast(_DEFAULTS[key])

    # ─────────────────────────────────────────────────────────────────────────
    #  MAIN BAR HANDLER
    # ─────────────────────────────────────────────────────────────────────────
    def _on_bar(self, bar: TradeBar):
        self._bar_idx += 1
        self._bars.append(bar)

        if not self._core_ready():
            return

        # ── Drawdown circuit breaker ──────────────────────────────────────────
        equity = self.portfolio.total_portfolio_value
        if equity > self._peak_equity:
            self._peak_equity    = equity
            self._trading_paused = False
        dd = (self._peak_equity - equity) / self._peak_equity
        if dd >= self._p("max_drawdown_pct"):
            if not self._trading_paused:
                self.log(f"[PAUSED] DD={dd:.2%}  equity={equity:.2f}")
                self._trading_paused = True
                self.stats["paused_dd"] += 1
            self._smc_reset()
            return

        # ── Manage open position first ────────────────────────────────────────
        if self._position_active:
            self._manage_position(bar.close)
            return                          # one position at a time

        # ── Detect regime ─────────────────────────────────────────────────────
        self._detect_regime(bar)

        # ── Route to strategies based on regime ───────────────────────────────
        #
        #  TRENDING → SMC/BOS  AND  Momentum   (both can trigger, first wins)
        #  RANGING  → Mean Reversion
        #  PANIC    → Sentiment Fade
        #
        if self._regime == REGIME_TRENDING:
            if not self._smc_entry(bar):       # SMC gets first priority
                self._momentum_entry(bar)
        elif self._regime == REGIME_RANGING:
            self._mean_reversion_entry(bar)
        elif self._regime == REGIME_PANIC:
            self._sentiment_entry(bar)

    # ─────────────────────────────────────────────────────────────────────────
    #  CORE READINESS CHECK
    # ─────────────────────────────────────────────────────────────────────────
    def _core_ready(self):
        return (
            self._atr.is_ready and
            self._adx.is_ready and
            self._ema_fast.is_ready and
            self._ema_slow.is_ready and
            self._rsi_mr.is_ready and
            self._bb.is_ready and
            self._rsi_sent.is_ready and
            len(self._bars) >= self._p("smc_pivot_left", int) + self._p("smc_pivot_right", int) + 5
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  REGIME DETECTOR  (ADX + Volatility)
    # ─────────────────────────────────────────────────────────────────────────
    def _detect_regime(self, bar: TradeBar):
        adx_val  = self._adx.current.value
        vol_lb   = self._p("volatility_lookback", int)
        bars_lst = list(self._bars)

        # Rolling volatility = std of close-to-close returns
        if len(bars_lst) >= vol_lb + 1:
            closes  = [b.close for b in bars_lst[-(vol_lb + 1):]]
            returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
            avg_ret = sum(abs(r) for r in returns) / len(returns)
            curr_ret= abs((bar.close - bars_lst[-2].close) / bars_lst[-2].close) if len(bars_lst) >= 2 else 0
            vol_ratio = curr_ret / avg_ret if avg_ret > 0 else 1.0
        else:
            vol_ratio = 1.0

        prev_regime = self._regime

        # PANIC: current bar vol is N x the rolling average
        if vol_ratio >= self._p("panic_vol_mult"):
            self._regime = REGIME_PANIC
            self.stats["regime_panic"] += 1
        # TRENDING: ADX strong
        elif adx_val >= self._p("adx_trend_thresh"):
            self._regime = REGIME_TRENDING
            self.stats["regime_trending"] += 1
        # RANGING: ADX weak
        elif adx_val <= self._p("adx_range_thresh"):
            self._regime = REGIME_RANGING
            self.stats["regime_ranging"] += 1
        else:
            # In between — keep previous regime for stability
            pass

        if self._regime != prev_regime and prev_regime != REGIME_UNKNOWN:
            self.log(f"[REGIME] {prev_regime} → {self._regime}  ADX={adx_val:.1f}  vol_ratio={vol_ratio:.2f}")

    # =========================================================================
    #  STRATEGY 1: SMC / BOS + RETEST
    # =========================================================================
    def _smc_entry(self, bar: TradeBar) -> bool:
        """Returns True if a trade was placed."""
        self._smc_detect_pivots()
        self._smc_check_bos(bar)

        if self._smc_bos_active:
            bars_since = self._bar_idx - self._smc_bos_bar_idx
            if bars_since > self._p("smc_max_open_bars", int):
                self.stats["smc_bos_expired"] += 1
                self._smc_reset()
                return False
            return self._smc_try_entry(bar)
        return False

    # ── Pivot detection ───────────────────────────────────────────────────────
    def _smc_detect_pivots(self):
        pl = self._p("smc_pivot_left", int)
        pr = self._p("smc_pivot_right", int)
        buf = list(self._bars)
        n   = len(buf)
        ci  = n - pr - 1
        if ci < pl:
            return
        pivot = buf[ci]
        left  = buf[ci - pl : ci]
        right = buf[ci + 1 : ci + 1 + pr]
        if len(left) < pl or len(right) < pr:
            return
        if all(pivot.high >= b.high for b in left) and all(pivot.high >= b.high for b in right):
            self._smc_pivot_highs.append((self._bar_idx, pivot.high))
        if all(pivot.low <= b.low for b in left) and all(pivot.low <= b.low for b in right):
            self._smc_pivot_lows.append((self._bar_idx, pivot.low))

    # ── BOS check ─────────────────────────────────────────────────────────────
    def _smc_check_bos(self, bar: TradeBar):
        if self._smc_bos_active:
            return
        if not self._smc_pivot_highs or not self._smc_pivot_lows:
            return

        atr      = self._atr.current.value
        min_move = atr * self._p("smc_bos_atr_min_mult")
        _, last_high = self._smc_pivot_highs[-1]
        _, last_low  = self._smc_pivot_lows[-1]

        if bar.close > last_high + min_move:
            if self._smc_vol_spike():
                self._smc_activate("bullish", last_high, last_low, last_low)
        elif bar.close < last_low - min_move:
            if self._smc_vol_spike():
                self._smc_activate("bearish", last_low, last_high, last_high)

    def _smc_vol_spike(self) -> bool:
        lb  = self._p("smc_vol_lookback", int)
        buf = list(self._bars)
        if len(buf) < lb + 1:
            return True
        avg = sum(b.volume for b in buf[-(lb+1):-1]) / lb
        return buf[-1].volume > avg * self._p("smc_vol_spike_mult")

    def _smc_activate(self, direction, bos_level, sl_ref, origin):
        self._smc_bos_active    = True
        self._smc_bos_dir       = direction
        self._smc_bos_level     = bos_level
        self._smc_bos_sl_ref    = sl_ref
        self._smc_bos_origin    = origin
        self._smc_bos_bar_idx   = self._bar_idx
        self._smc_bos_held      = (self._p("smc_bos_hold_bars", int) == 0)
        self._smc_retest_logged = False
        self.stats["smc_bos_activated"] += 1
        self.log(f"[SMC BOS {direction.upper()}] level={bos_level:.2f}  sl={sl_ref:.2f}")

    # ── Entry attempt ─────────────────────────────────────────────────────────
    def _smc_try_entry(self, bar: TradeBar) -> bool:
        c   = bar.close
        atr = self._atr.current.value

        # Minimum swing size
        swing = abs(self._smc_bos_level - self._smc_bos_origin)
        if swing < atr * self._p("smc_min_swing_atr_mult"):
            return False

        # Hold check
        hold_bars  = self._p("smc_bos_hold_bars", int)
        bars_since = self._bar_idx - self._smc_bos_bar_idx
        if not self._smc_bos_held:
            if bars_since <= hold_bars:
                if self._smc_bos_dir == "bullish" and c < self._smc_bos_level:
                    self.stats["smc_bos_no_hold"] += 1
                    self._smc_reset()
                    return False
                elif self._smc_bos_dir == "bearish" and c > self._smc_bos_level:
                    self.stats["smc_bos_no_hold"] += 1
                    self._smc_reset()
                    return False
                return False
            else:
                self._smc_bos_held = True

        # Retest check
        if self._smc_bos_dir == "bullish":
            retested = (c <= self._smc_bos_level) and (c >= self._smc_bos_level * (1 - self._p("smc_retest_zone_pct")))
        else:
            retested = (c >= self._smc_bos_level) and (c <= self._smc_bos_level * (1 + self._p("smc_retest_zone_pct")))

        if not retested:
            if not self._smc_retest_logged:
                self.stats["smc_entry_no_retest"] += 1
                self._smc_retest_logged = True
            return False

        # Confirmation candle
        body     = bar.close - bar.open
        body_abs = abs(body)
        min_body = atr * self._p("smc_conf_body_atr_mult")
        if self._smc_bos_dir == "bullish" and not (body > 0 and body_abs >= min_body):
            self.stats["smc_entry_no_confirm"] += 1
            return False
        if self._smc_bos_dir == "bearish" and not (body < 0 and body_abs >= min_body):
            self.stats["smc_entry_no_confirm"] += 1
            return False

        # Volume Profile confluence
        vp = self._get_vpoc()
        if vp is not None:
            if abs(vp - self._smc_bos_level) / self._smc_bos_level > self._p("smc_vp_zone_pct"):
                self.stats["smc_entry_vp_miss"] += 1
                return False

        # SL / TP
        if self._smc_bos_dir == "bullish":
            sl = self._smc_bos_sl_ref - atr * self._p("sl_buffer_atr")
            tp = self._smc_bos_level + swing * self._p("smc_fib_mult")
        else:
            sl = self._smc_bos_sl_ref + atr * self._p("sl_buffer_atr")
            tp = self._smc_bos_level - swing * self._p("smc_fib_mult")

        placed = self._place_trade(c, sl, tp, self._smc_bos_dir, "SMC")
        if placed:
            self.stats["smc_trades"] += 1
            self._smc_reset()
        return placed

    # ── VP (VPOC) ─────────────────────────────────────────────────────────────
    def _get_vpoc(self):
        lb   = self._p("smc_vp_lookback", int)
        bins = self._p("smc_vp_bins", int)
        bars = list(self._bars)[-lb:]
        if len(bars) < 5:
            return None
        lo = min(b.low  for b in bars)
        hi = max(b.high for b in bars)
        if hi <= lo:
            return None
        bsz  = (hi - lo) / bins
        vols = [0.0] * bins
        for b in bars:
            b_lo = max(0, min(int((b.low  - lo) / bsz), bins - 1))
            b_hi = max(0, min(int((b.high - lo) / bsz), bins - 1))
            span = b_hi - b_lo + 1
            for i in range(b_lo, b_hi + 1):
                vols[i] += b.volume / span
        mids = [lo + (i + 0.5) * bsz for i in range(bins)]
        return mids[vols.index(max(vols))]

    def _smc_reset(self):
        self._smc_bos_active    = False
        self._smc_bos_dir       = None
        self._smc_bos_level     = None
        self._smc_bos_sl_ref    = None
        self._smc_bos_origin    = None
        self._smc_bos_bar_idx   = -1
        self._smc_bos_held      = False
        self._smc_retest_logged = False

    # =========================================================================
    #  STRATEGY 2: MOMENTUM / TREND FOLLOWING
    # =========================================================================
    def _momentum_entry(self, bar: TradeBar):
        """
        Entry: fast EMA crosses above/below slow EMA
               AND ADX confirms trend strength
               AND bar closes in direction of cross
        """
        fast = self._ema_fast.current.value
        slow = self._ema_slow.current.value
        adx  = self._adx.current.value
        atr  = self._atr.current.value
        c    = bar.close

        if adx < self._p("mom_adx_min"):
            return

        sl_dist = atr * self._p("mom_sl_atr_mult")
        tp_dist = atr * self._p("mom_tp_atr_mult")

        if fast > slow and bar.close > bar.open:       # bullish momentum
            sl = c - sl_dist
            tp = c + tp_dist
            if self._place_trade(c, sl, tp, "bullish", "MOM"):
                self.stats["mom_trades"] += 1

        elif fast < slow and bar.close < bar.open:     # bearish momentum
            sl = c + sl_dist
            tp = c - tp_dist
            if self._place_trade(c, sl, tp, "bearish", "MOM"):
                self.stats["mom_trades"] += 1

    # =========================================================================
    #  STRATEGY 3: MEAN REVERSION  (RSI + Bollinger Bands)
    # =========================================================================
    def _mean_reversion_entry(self, bar: TradeBar):
        """
        Entry: RSI oversold/overbought AND price outside Bollinger Band
        TP: middle Bollinger Band (mean reversion target)
        SL: ATR-based beyond the band extreme
        """
        rsi  = self._rsi_mr.current.value
        atr  = self._atr.current.value
        c    = bar.close
        bb_upper = self._bb.upper_band.current.value
        bb_lower = self._bb.lower_band.current.value
        bb_mid   = self._bb.middle_band.current.value

        sl_dist = atr * self._p("mr_sl_atr_mult")

        # Long: oversold + below lower band
        if rsi < self._p("mr_rsi_oversold") and c < bb_lower:
            sl = c - sl_dist
            tp = bb_mid
            if tp > c and (tp - c) / (c - sl) >= self._p("min_rr"):
                if self._place_trade(c, sl, tp, "bullish", "MR"):
                    self.stats["mr_trades"] += 1

        # Short: overbought + above upper band
        elif rsi > self._p("mr_rsi_overbought") and c > bb_upper:
            sl = c + sl_dist
            tp = bb_mid
            if tp < c and (c - tp) / (sl - c) >= self._p("min_rr"):
                if self._place_trade(c, sl, tp, "bearish", "MR"):
                    self.stats["mr_trades"] += 1

    # =========================================================================
    #  STRATEGY 4: SENTIMENT / FUNDING RATE FADE
    # =========================================================================
    def _sentiment_entry(self, bar: TradeBar):
        """
        Fires ONLY in PANIC regime (extreme vol spike).
        Edge: Loss Aversion + Capitulation — markets overshoot on panic.

        Signals:
          - Short RSI at extreme (< 20 or > 80)
          - Volume spike (current bar vs rolling average)
          - Price far from rolling VWAP

        Behavioral concept: when everyone is panic selling/buying,
        the move is already exhausted — fade it for the snap-back.
        """
        rsi  = self._rsi_sent.current.value
        atr  = self._atr.current.value
        c    = bar.close

        # Volume spike check
        vol_lb = self._p("volatility_lookback", int)
        bars   = list(self._bars)
        if len(bars) < vol_lb + 1:
            return
        avg_vol = sum(b.volume for b in bars[-(vol_lb+1):-1]) / vol_lb
        if bars[-1].volume < avg_vol * self._p("sent_vol_spike_mult"):
            return     # not a real panic spike

        # Rolling VWAP
        vwap_lb = self._p("sent_vwap_lookback", int)
        vwap    = self._rolling_vwap(vwap_lb)
        if vwap is None:
            return

        sl_dist = atr * self._p("sent_sl_atr_mult")
        tp_dist = atr * self._p("sent_tp_atr_mult")

        # Panic buy fade (overbought + price >> VWAP) → SHORT
        if rsi > self._p("sent_overbought") and c > vwap * 1.01:
            sl = c + sl_dist
            tp = c - tp_dist
            if self._place_trade(c, sl, tp, "bearish", "SENT"):
                self.stats["sent_trades"] += 1

        # Panic sell fade (oversold + price << VWAP) → LONG
        elif rsi < self._p("sent_oversold") and c < vwap * 0.99:
            sl = c - sl_dist
            tp = c + tp_dist
            if self._place_trade(c, sl, tp, "bullish", "SENT"):
                self.stats["sent_trades"] += 1

    def _rolling_vwap(self, lookback):
        bars = list(self._bars)[-lookback:]
        if len(bars) < 5:
            return None
        total_pv  = sum(((b.high + b.low + b.close) / 3) * b.volume for b in bars)
        total_vol = sum(b.volume for b in bars)
        return total_pv / total_vol if total_vol > 0 else None

    # =========================================================================
    #  UNIFIED POSITION SIZER + ORDER PLACER
    # =========================================================================
    def _place_trade(self, price, sl, tp, direction, strategy_name) -> bool:
        """
        Unified entry — all strategies go through here.
        Returns True if order was actually placed.
        """
        sl_dist = abs(price - sl)
        tp_dist = abs(tp - price)
        if sl_dist <= 0 or tp_dist <= 0:
            return False

        # R:R gate
        rr = tp_dist / sl_dist
        if rr < self._p("min_rr"):
            self.stats["skipped_rr"] += 1
            return False

        # Direction sanity
        if direction == "bullish" and tp <= price:
            return False
        if direction == "bearish" and tp >= price:
            return False

        equity = self.portfolio.total_portfolio_value

        # Loss streak scaling with floor
        effective_risk = self._p("risk_pct")
        if self._consec_losses >= self._p("loss_streak_n", int):
            scaled         = effective_risk * self._p("loss_streak_scale")
            floor          = effective_risk * self._p("loss_streak_floor")
            effective_risk = max(scaled, floor)
            self.stats["scaled_risk"] += 1

        # Quantity calculation
        risk_qty   = (equity * effective_risk) / sl_dist
        pos_qty    = (equity * self._p("max_pos_pct")) / price
        margin_used= self.portfolio.total_margin_used
        margin_cap = max(0, equity * self._p("max_margin_pct") - margin_used)
        margin_qty = margin_cap / price
        qty        = min(risk_qty, pos_qty, margin_qty)

        if qty <= 0:
            return False

        # Lot size check
        lot_size = self.securities[self._sym].symbol_properties.lot_size
        if qty < lot_size:
            self.stats["skipped_lotsize"] += 1
            return False

        signed_qty = qty if direction == "bullish" else -qty
        self.market_order(self._sym, signed_qty)

        self._sl              = sl
        self._tp              = tp
        self._position_active = True
        self._active_strategy = strategy_name

        side = "LONG" if direction == "bullish" else "SHORT"
        self.log(
            f"[{strategy_name} {side}] regime={self._regime}  qty={qty:.4f} @ {price:.2f}"
            f"  SL={sl:.2f}  TP={tp:.2f}  R:R={rr:.2f}"
            f"  risk={effective_risk:.3f}  streak={self._consec_losses}"
        )
        return True

    # =========================================================================
    #  POSITION MANAGEMENT
    # =========================================================================
    def _manage_position(self, c):
        if not self.portfolio[self._sym].invested:
            self.log(f"[EXTERNAL CLOSE] strategy={self._active_strategy}")
            self._consec_losses += 1
            self._position_active = False
            self._smc_reset()
            return

        is_long = self.portfolio[self._sym].is_long
        hit_tp  = (is_long and c >= self._tp) or (not is_long and c <= self._tp)
        hit_sl  = (is_long and c <= self._sl) or (not is_long and c >= self._sl)

        if hit_tp or hit_sl:
            pnl = self.portfolio[self._sym].unrealized_profit_percent
            self.liquidate(self._sym)
            if hit_tp:
                self.stats["closed_tp"] += 1
                self._consec_losses = 0
                self.log(f"[TP] {self._active_strategy}  PnL={pnl:.2%}")
            else:
                self.stats["closed_sl"] += 1
                self._consec_losses += 1
                self.log(f"[SL] {self._active_strategy}  PnL={pnl:.2%}  streak={self._consec_losses}")
            self._position_active = False
            self._active_strategy = None
            self._smc_reset()

    # =========================================================================
    #  END OF ALGORITHM REPORT
    # =========================================================================
    def on_end_of_algorithm(self):
        self.log("=" * 65)
        self.log("FINAL REPORT — MultiAlgoSOL 15m  |  SOLUSDT  |  2023–2026")
        self.log("=" * 65)

        # Regime distribution
        total_bars = self.stats["regime_trending"] + self.stats["regime_ranging"] + self.stats["regime_panic"]
        if total_bars > 0:
            self.log(f"  REGIME DISTRIBUTION ({total_bars} classified bars)")
            self.log(f"    Trending : {self.stats['regime_trending']:>6}  ({self.stats['regime_trending']/total_bars:.1%})")
            self.log(f"    Ranging  : {self.stats['regime_ranging']:>6}  ({self.stats['regime_ranging']/total_bars:.1%})")
            self.log(f"    Panic    : {self.stats['regime_panic']:>6}  ({self.stats['regime_panic']/total_bars:.1%})")

        self.log("")
        self.log("  TRADES BY STRATEGY")
        total_trades = (self.stats["smc_trades"] + self.stats["mom_trades"] +
                        self.stats["mr_trades"]  + self.stats["sent_trades"])
        self.log(f"    SMC/BOS      : {self.stats['smc_trades']}")
        self.log(f"    Momentum     : {self.stats['mom_trades']}")
        self.log(f"    Mean Rev     : {self.stats['mr_trades']}")
        self.log(f"    Sentiment    : {self.stats['sent_trades']}")
        self.log(f"    TOTAL        : {total_trades}")

        self.log("")
        self.log("  OUTCOMES")
        tp = self.stats["closed_tp"]
        sl = self.stats["closed_sl"]
        self.log(f"    TP closes    : {tp}")
        self.log(f"    SL closes    : {sl}")
        if (tp + sl) > 0:
            self.log(f"    Win Rate     : {tp/(tp+sl)*100:.1f}%")

        self.log("")
        self.log("  RISK / FILTERS")
        self.log(f"    DD pauses    : {self.stats['paused_dd']}")
        self.log(f"    Scaled risk  : {self.stats['scaled_risk']}")
        self.log(f"    Skipped R:R  : {self.stats['skipped_rr']}")
        self.log(f"    Skipped lot  : {self.stats['skipped_lotsize']}")

        self.log("")
        self.log("  SMC DETAIL")
        self.log(f"    BOS activated: {self.stats['smc_bos_activated']}")
        self.log(f"    BOS expired  : {self.stats['smc_bos_expired']}")
        self.log(f"    No hold      : {self.stats['smc_bos_no_hold']}")
        self.log(f"    No retest    : {self.stats['smc_entry_no_retest']}")
        self.log(f"    VP miss      : {self.stats['smc_entry_vp_miss']}")
        self.log(f"    No confirm   : {self.stats['smc_entry_no_confirm']}")
        self.log("=" * 65)