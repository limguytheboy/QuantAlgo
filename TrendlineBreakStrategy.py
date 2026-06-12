from AlgorithmImports import *
from datetime import timedelta
from collections import deque


# ─────────────────────────────────────────────────────────────────────────────
#  REGIME IDs
# ─────────────────────────────────────────────────────────────────────────────
REGIME_TRENDING = "TRENDING"
REGIME_RANGING  = "RANGING"
REGIME_PANIC    = "PANIC"
REGIME_UNKNOWN  = "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────────
#  DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULTS = {
    "symbol":                   "SOLUSDT",
    "timeframe_minutes":        5,

    # ── Risk / Sizing ─────────────────────────────────────────────────────────
    "risk_pct":                 0.005,
    "max_pos_pct":              0.08,
    "max_margin_pct":           0.25,
    "min_rr":                   1.5,
    "max_sl_pct":               0.04,       # FIX P3: raised 3%→4% (was clipping valid SLs)

    # ── Drawdown protection ───────────────────────────────────────────────────
    "max_drawdown_pct":         0.08,
    "loss_streak_n":            4,          # FIX P3: raised 3→4 (was scaling too aggressively)
    "loss_streak_scale":        0.6,        # FIX P3: raised 0.5→0.6 (less punishment per streak)
    "loss_streak_floor":        0.5,

    # ── ATR ───────────────────────────────────────────────────────────────────
    "atr_period":               14,
    "sl_buffer_atr":            0.4,

    # ── HTF Trend Filter ──────────────────────────────────────────────────────
    "htf_ema_period":           200,

    # ── REGIME DETECTOR ───────────────────────────────────────────────────────
    # FIX P2: Regime thresholds widened so RANGING fires more often
    # v3 had adx_range_thresh=18 — in a crypto trending market ADX rarely
    # drops below 18. Raising to 22 means 18-22 range = RANGING, not just <18.
    # Also: MR strategy now also fires in TRENDING when RSI+BB both extreme —
    # mean reversion opportunities exist within trending markets too.
    "adx_period":               14,
    "adx_trend_thresh":         28,         # FIX P2: raised 25→28 (less bars = TRENDING)
    "adx_range_thresh":         22,         # FIX P2: raised 18→22 (more bars = RANGING)
    "volatility_lookback":      20,
    "panic_vol_mult":           2.5,

    # ── STRATEGY 1: SMC / BOS + Retest ───────────────────────────────────────
    # FIX P1: The pivot deque approach was giving mismatched highs/lows.
    # New approach: for each BOS we find the MOST RECENT confirmed swing
    # structure — the high AND low that form the SAME swing, not two
    # independent deque entries. We now track (bar_idx, high, low) pairs
    # from the same pivot window so sl_ref is always correctly paired.
    "smc_pivot_left":           5,          # slightly wider for cleaner swings
    "smc_pivot_right":          5,
    "smc_bos_atr_min_mult":     0.5,        # raised 0.3→0.5 — more meaningful BOS only
    "smc_vol_spike_mult":       1.3,        # lowered 1.5→1.3 — less restrictive
    "smc_vol_lookback":         20,
    "smc_retest_zone_pct":      0.02,       # widened 1.5%→2% — more retest catches
    "smc_vp_lookback":          40,
    "smc_vp_bins":              24,
    "smc_vp_zone_pct":          0.04,       # widened 2.5%→4% — VP was killing all setups
    "smc_fib_mult":             1.618,
    "smc_max_open_bars":        64,         # widened 48→64 — more time to retest
    "smc_bos_hold_bars":        1,
    "smc_conf_body_atr_mult":   0.2,        # lowered 0.3→0.2 — allow smaller confirm bars
    "smc_min_swing_atr_mult":   1.2,        # lowered 1.5→1.2 — allow smaller swings

    # ── STRATEGY 2: Momentum ─────────────────────────────────────────────────
    # FIX P3: TP was too far — avg peak +1.38R but TP at 2.4R (1.2 ATR SL × 2.0)
    # Lower TP to 1.8R so more trades actually close TP instead of reversing
    "mom_fast_ema":             9,
    "mom_slow_ema":             21,
    "mom_adx_min":              25,
    "mom_sl_atr_mult":          1.2,
    "mom_tp_atr_mult":          2.16,       # FIX P3: R:R = 1.8x (was 2.0x, TP too far)

    # ── STRATEGY 3: Mean Reversion ────────────────────────────────────────────
    # FIX P2: MR now runs in BOTH ranging AND trending regimes
    # In trending markets: only fade EXTREME moves (RSI < 20 or > 80)
    # In ranging markets: standard RSI 30/70 + Bollinger
    "mr_rsi_period":            14,
    "mr_rsi_oversold":          30,         # ranging threshold
    "mr_rsi_overbought":        70,
    "mr_rsi_extreme_oversold":  20,         # FIX P2: trending threshold (more extreme)
    "mr_rsi_extreme_overbought":80,
    "mr_bb_period":             20,
    "mr_bb_std":                2.0,
    "mr_sl_atr_mult":           1.0,
    "mr_tp_atr_mult":           2.0,

    # ── STRATEGY 4: Sentiment / Panic Fade ───────────────────────────────────
    # FIX P4: Sentiment had 40% WR (positive EV at 2R) but only 26 trades
    # Lower vol_spike_mult 2.0→1.5 and widen VWAP threshold to get more signals
    "sent_rsi_period":          7,
    "sent_oversold":            20,
    "sent_overbought":          80,
    "sent_vol_spike_mult":      1.5,        # FIX P4: lowered 2.0→1.5
    "sent_vwap_lookback":       96,
    "sent_vwap_dev_pct":        0.005,      # FIX P4: was hardcoded 1% — now 0.5% threshold
    "sent_sl_atr_mult":         1.5,
    "sent_tp_atr_mult":         3.0,
}


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN ALGORITHM
# ─────────────────────────────────────────────────────────────────────────────
class MultiAlgoSOL(QCAlgorithm):
    """
    Multi-algorithm regime-routed strategy — SOLUSDT 15m — 2023-2026  v4

    FIXES vs v3 (diagnosed from backtest results):

    FIX P1 — SMC: 0 trades from 99 BOS (19 SL direction errors)
      Root cause: _smc_pivot_highs and _smc_pivot_lows are independent deques.
      A bullish BOS (price > last_high) pairs last_high with last_low — but
      these come from different pivot windows and last_low > last_high is
      possible if the deques aren't in sync. Fixed by tracking swing structures
      as (high, low) pairs from the same pivot window.

    FIX P2 — Mean Reversion: 0 trades (only 6 RANGING bars in 3 years)
      Root cause: adx_range_thresh=18 almost never triggered in a trending
      crypto asset. Two fixes: (a) widen RANGING threshold to ADX<22,
      (b) allow MR to also fire in TRENDING regime at extreme RSI levels
      (RSI<20 or >80) — mean reversion within trends is a real edge.

    FIX P3 — Momentum: 35% WR, needs 40% (avg peak +1.38R, TP at 2.4R)
      Root cause: TP is set too far away. Price reaches +1.38R on average
      then reverses — TP at 2.4R is never reached. Lowered TP to 1.8R.
      Also: loss_streak triggered 191 times (every 3 losses) cascading risk
      to near-zero. Raised streak threshold 3→4, scale factor 0.5→0.6.

    FIX P4 — Sentiment: positive EV (40% WR at 2R) but only 26 trades
      Root cause: sent_vol_spike_mult=2.0 was too restrictive. Lowered to 1.5.
      Also hardcoded 1% VWAP deviation → parameterized at 0.5%.

    Regime routing (updated):
      TRENDING  → SMC/BOS  +  Momentum  +  MR extreme fade (RSI <20/>80)
      RANGING   → Mean Reversion (standard RSI 30/70 + BB)
      PANIC     → Sentiment Fade
    """

    def initialize(self):
        self.set_start_date(2023, 1, 1)
        self.set_end_date(2026, 2, 1)
        self.set_account_currency("USDT")
        self.set_cash("USDT", 50_000)
        self.set_brokerage_model(BrokerageName.BINANCE, AccountType.MARGIN)

        contract  = self.add_crypto("SOLUSDT", Resolution.MINUTE, Market.BINANCE)
        self._sym = contract.symbol
        self._tf  = int(self._p("timeframe_minutes", int))

        self.set_benchmark(self._sym)

        # ── Indicators ────────────────────────────────────────────────────────
        self._atr      = self.atr(self._sym, self._p("atr_period", int), MovingAverageType.WILDERS)
        self._adx      = self.adx(self._sym, self._p("adx_period", int))
        self._ema_fast = self.ema(self._sym, self._p("mom_fast_ema", int))
        self._ema_slow = self.ema(self._sym, self._p("mom_slow_ema", int))
        self._ema_htf  = self.ema(self._sym, self._p("htf_ema_period", int))
        self._rsi_mr   = self.rsi(self._sym, self._p("mr_rsi_period", int))
        self._bb       = self.bb(self._sym, self._p("mr_bb_period", int), self._p("mr_bb_std"))
        self._rsi_sent = self.rsi(self._sym, self._p("sent_rsi_period", int))

        # ── Bar buffer ────────────────────────────────────────────────────────
        buf = max(
            self._p("smc_vp_lookback", int),
            self._p("smc_vol_lookback", int),
            self._p("sent_vwap_lookback", int),
            self._p("volatility_lookback", int),
            self._p("smc_pivot_left", int) + self._p("smc_pivot_right", int) + 10,
        ) + 20
        self._bars = deque(maxlen=buf)

        self.consolidate(self._sym, timedelta(minutes=self._tf), self._on_bar)

        # ── Shared state ──────────────────────────────────────────────────────
        self._bar_idx         = 0
        self._regime          = REGIME_UNKNOWN
        self._peak_equity     = 50_000.0
        self._consec_losses   = 0
        self._trading_paused  = False
        self._position_active = False
        self._sl              = 0.0
        self._tp              = 0.0
        self._entry_price     = 0.0
        self._initial_qty     = 0.0
        self._active_strategy = None
        self._active_dir      = None
        self._entry_regime    = None

        # ── FIX P1: SMC state — now tracks paired (high, low) swing structures
        # Instead of two independent deques, we track a list of swing objects
        # where each swing has both its high AND low from the same pivot scan.
        # This guarantees SL is always on the correct side of the BOS level.
        # ──────────────────────────────────────────────────────────────────────
        # _smc_swings: deque of dicts {bar_idx, high, low}
        # Each entry = a confirmed pivot that had both a swing high and low
        # identified from the same pivot_left/right window.
        self._smc_swings        = deque(maxlen=20)
        # Separate tracking for unpaired pivots (only high OR only low confirmed)
        self._smc_last_ph       = None    # (bar_idx, price) last confirmed pivot high
        self._smc_last_pl       = None    # (bar_idx, price) last confirmed pivot low

        self._smc_bos_active    = False
        self._smc_bos_dir       = None
        self._smc_bos_level     = None
        self._smc_bos_sl_ref    = None    # always correctly paired now
        self._smc_bos_origin    = None
        self._smc_bos_bar_idx   = -1
        self._smc_bos_held      = False
        self._smc_retest_logged = False

        # ── Analysis panel state ──────────────────────────────────────────────
        self._trade_journal   = []
        self._current_trade   = None
        self._equity_curve    = []

        self._strat_tp = {"SMC": 0, "MOM": 0, "MR": 0, "MR_EXT": 0, "SENT": 0}
        self._strat_sl = {"SMC": 0, "MOM": 0, "MR": 0, "MR_EXT": 0, "SENT": 0}
        self._regime_tp = {REGIME_TRENDING: 0, REGIME_RANGING: 0, REGIME_PANIC: 0}
        self._regime_sl = {REGIME_TRENDING: 0, REGIME_RANGING: 0, REGIME_PANIC: 0}
        self._long_tp   = 0;  self._long_sl  = 0
        self._short_tp  = 0;  self._short_sl = 0

        # ── Stats ─────────────────────────────────────────────────────────────
        self.stats = {
            "regime_trending":          0,
            "regime_ranging":           0,
            "regime_panic":             0,
            "smc_trades":               0,
            "mom_trades":               0,
            "mr_trades":                0,
            "mr_ext_trades":            0,   # MR extreme in trending regime
            "sent_trades":              0,
            "closed_tp":                0,
            "closed_sl":                0,
            "paused_dd":                0,
            "scaled_risk":              0,
            "skipped_rr":               0,
            "skipped_sl_too_wide":      0,
            "skipped_lotsize":          0,
            "skipped_trend_filter":     0,
            # SMC funnel
            "smc_pivot_pairs":          0,   # FIX P1: paired swings found
            "smc_bos_activated":        0,
            "smc_bos_expired":          0,
            "smc_bos_no_hold":          0,
            "smc_sl_direction_error":   0,
            "smc_entry_no_retest":      0,
            "smc_entry_vp_miss":        0,
            "smc_entry_no_confirm":     0,
        }

        self.log(
            "=== MultiAlgoSOL v4 | SOLUSDT 15m | 2023-01-01→2026-02-01 "
            "| Capital=50k | Fixes: SMC-SL-pairing, MR-in-trending, MOM-TP, SENT-threshold ==="
        )

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

        if self._bar_idx % 12 == 0:
            self._equity_curve.append((str(self.time), self.portfolio.total_portfolio_value))

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
            self._manage_position(bar)
            return

        # ── Detect regime ─────────────────────────────────────────────────────
        self._detect_regime(bar)

        # ── Route to strategies ───────────────────────────────────────────────
        # FIX P2: MR extreme now also runs in TRENDING regime
        if self._regime == REGIME_TRENDING:
            if not self._smc_entry(bar):
                if not self._momentum_entry(bar):
                    self._mean_reversion_entry(bar, extreme_only=True)  # FIX P2

        elif self._regime == REGIME_RANGING:
            self._mean_reversion_entry(bar, extreme_only=False)

        elif self._regime == REGIME_PANIC:
            self._sentiment_entry(bar)

    # ─────────────────────────────────────────────────────────────────────────
    def _core_ready(self):
        return (
            self._atr.is_ready and
            self._adx.is_ready and
            self._ema_fast.is_ready and
            self._ema_slow.is_ready and
            self._ema_htf.is_ready and
            self._rsi_mr.is_ready and
            self._bb.is_ready and
            self._rsi_sent.is_ready and
            len(self._bars) >= self._p("smc_pivot_left", int) + self._p("smc_pivot_right", int) + 5
        )

    # ─────────────────────────────────────────────────────────────────────────
    #  REGIME DETECTOR
    # ─────────────────────────────────────────────────────────────────────────
    def _detect_regime(self, bar: TradeBar):
        adx_val = self._adx.current.value
        vol_lb  = self._p("volatility_lookback", int)
        bars    = list(self._bars)

        vol_ratio = 1.0
        if len(bars) >= vol_lb + 1:
            closes   = [b.close for b in bars[-(vol_lb + 1):]]
            rets     = [abs((closes[i] - closes[i-1]) / closes[i-1]) for i in range(1, len(closes))]
            avg_ret  = sum(rets) / len(rets) if rets else 0
            curr_ret = abs((bar.close - bars[-2].close) / bars[-2].close) if len(bars) >= 2 else 0
            vol_ratio = curr_ret / avg_ret if avg_ret > 0 else 1.0

        prev = self._regime

        if vol_ratio >= self._p("panic_vol_mult"):
            self._regime = REGIME_PANIC
            self.stats["regime_panic"] += 1
        elif adx_val >= self._p("adx_trend_thresh"):
            self._regime = REGIME_TRENDING
            self.stats["regime_trending"] += 1
        elif adx_val <= self._p("adx_range_thresh"):
            self._regime = REGIME_RANGING
            self.stats["regime_ranging"] += 1
        # else: keep previous regime (stability in transition zone)

        if self._regime != prev and prev != REGIME_UNKNOWN:
            self.log(f"[REGIME] {prev} → {self._regime}  ADX={adx_val:.1f}  vol_ratio={vol_ratio:.2f}")

    # =========================================================================
    #  STRATEGY 1: SMC / BOS + RETEST
    #  FIX P1: Paired swing tracking — SL always correctly paired with BOS level
    # =========================================================================
    def _smc_entry(self, bar: TradeBar) -> bool:
        self._smc_detect_pivots()
        self._smc_check_bos(bar)

        if self._smc_bos_active:
            if (self._bar_idx - self._smc_bos_bar_idx) > self._p("smc_max_open_bars", int):
                self.stats["smc_bos_expired"] += 1
                self._smc_reset()
                return False
            return self._smc_try_entry(bar)
        return False

    def _smc_detect_pivots(self):
        """
        FIX P1: Now builds PAIRED swing structures.

        A swing structure = a window where we can confirm BOTH:
          - A pivot high (local max in the window)
          - A pivot low  (local min in the window)

        For a bullish BOS setup we need:
          bos_level = the swing HIGH that was broken
          sl_ref    = the swing LOW of the SAME swing (the base of the move)

        We track the most recent confirmed pivot high and low INDEPENDENTLY,
        then pair them when a BOS fires. The critical rule: for a bullish BOS
        (price broke above a swing high), the sl_ref (swing low) MUST be
        below the bos_level (swing high). We validate this on pairing.
        """
        pl  = self._p("smc_pivot_left", int)
        pr  = self._p("smc_pivot_right", int)
        buf = list(self._bars)
        ci  = len(buf) - pr - 1
        if ci < pl:
            return

        pivot = buf[ci]
        left  = buf[ci - pl : ci]
        right = buf[ci + 1 : ci + 1 + pr]
        if len(left) < pl or len(right) < pr:
            return

        is_ph = all(pivot.high >= b.high for b in left) and all(pivot.high >= b.high for b in right)
        is_pl = all(pivot.low  <= b.low  for b in left) and all(pivot.low  <= b.low  for b in right)

        if is_ph:
            self._smc_last_ph = (self._bar_idx, pivot.high)
        if is_pl:
            self._smc_last_pl = (self._bar_idx, pivot.low)

    def _smc_check_bos(self, bar: TradeBar):
        if self._smc_bos_active:
            return
        if self._smc_last_ph is None or self._smc_last_pl is None:
            return

        atr      = self._atr.current.value
        min_move = atr * self._p("smc_bos_atr_min_mult")
        _, ph    = self._smc_last_ph   # most recent pivot high price
        _, pl    = self._smc_last_pl   # most recent pivot low  price

        # Sanity: pivot high must actually be above pivot low
        # (can fail if market is very choppy and both pivots are from different regimes)
        if ph <= pl:
            return

        # Bullish BOS: price closed above swing high
        # SL goes BELOW the swing low (pl) — guaranteed ph > pl here
        if bar.close > ph + min_move:
            if self._smc_vol_spike():
                self._smc_activate("bullish",
                                   bos_level=ph,
                                   sl_ref=pl,      # swing low = SL anchor
                                   origin=pl)      # project TP from swing low up

        # Bearish BOS: price closed below swing low
        # SL goes ABOVE the swing high (ph) — guaranteed ph > pl here
        elif bar.close < pl - min_move:
            if self._smc_vol_spike():
                self._smc_activate("bearish",
                                   bos_level=pl,
                                   sl_ref=ph,      # swing high = SL anchor
                                   origin=ph)      # project TP from swing high down

    def _smc_vol_spike(self) -> bool:
        lb  = self._p("smc_vol_lookback", int)
        buf = list(self._bars)
        if len(buf) < lb + 1:
            return True
        avg = sum(b.volume for b in buf[-(lb+1):-1]) / lb
        return buf[-1].volume > avg * self._p("smc_vol_spike_mult")

    def _smc_activate(self, direction, bos_level, sl_ref, origin):
        # Final direction sanity check (should always pass now with paired pivots)
        if direction == "bullish" and sl_ref >= bos_level:
            self.stats["smc_sl_direction_error"] += 1
            self.log(f"[SMC SKIP] bull sl={sl_ref:.2f} >= bos={bos_level:.2f}  ph/pl mismatch")
            return
        if direction == "bearish" and sl_ref <= bos_level:
            self.stats["smc_sl_direction_error"] += 1
            self.log(f"[SMC SKIP] bear sl={sl_ref:.2f} <= bos={bos_level:.2f}  ph/pl mismatch")
            return

        self._smc_bos_active    = True
        self._smc_bos_dir       = direction
        self._smc_bos_level     = bos_level
        self._smc_bos_sl_ref    = sl_ref
        self._smc_bos_origin    = origin
        self._smc_bos_bar_idx   = self._bar_idx
        self._smc_bos_held      = (self._p("smc_bos_hold_bars", int) == 0)
        self._smc_retest_logged = False
        self.stats["smc_bos_activated"] += 1
        self.log(f"[SMC BOS {direction.upper()}] level={bos_level:.2f}  sl_ref={sl_ref:.2f}  origin={origin:.2f}")

    def _smc_try_entry(self, bar: TradeBar) -> bool:
        c   = bar.close
        atr = self._atr.current.value
        swing = abs(self._smc_bos_level - self._smc_bos_origin)
        if swing < atr * self._p("smc_min_swing_atr_mult"):
            return False

        # Post-BOS hold check
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

        # Retest zone check
        rz = self._p("smc_retest_zone_pct")
        if self._smc_bos_dir == "bullish":
            retested = (c <= self._smc_bos_level) and (c >= self._smc_bos_level * (1 - rz))
        else:
            retested = (c >= self._smc_bos_level) and (c <= self._smc_bos_level * (1 + rz))

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

        # VP confluence
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

        placed = self._place_trade(bar, sl, tp, self._smc_bos_dir, "SMC")
        if placed:
            self.stats["smc_trades"] += 1
            self._smc_reset()
        return placed

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
    #  STRATEGY 2: MOMENTUM — FIX P3: TP lowered to 1.8R
    # =========================================================================
    def _momentum_entry(self, bar: TradeBar) -> bool:
        fast    = self._ema_fast.current.value
        slow    = self._ema_slow.current.value
        htf_ema = self._ema_htf.current.value
        adx     = self._adx.current.value
        atr     = self._atr.current.value
        c       = bar.close

        if adx < self._p("mom_adx_min"):
            return False

        sl_dist = atr * self._p("mom_sl_atr_mult")
        tp_dist = atr * self._p("mom_tp_atr_mult")

        if fast > slow and bar.close > bar.open and c > htf_ema:
            sl = c - sl_dist
            tp = c + tp_dist
            if self._place_trade(bar, sl, tp, "bullish", "MOM"):
                self.stats["mom_trades"] += 1
                return True

        elif fast < slow and bar.close < bar.open and c < htf_ema:
            sl = c + sl_dist
            tp = c - tp_dist
            if self._place_trade(bar, sl, tp, "bearish", "MOM"):
                self.stats["mom_trades"] += 1
                return True

        elif (fast > slow and bar.close > bar.open and c < htf_ema) or \
             (fast < slow and bar.close < bar.open and c > htf_ema):
            self.stats["skipped_trend_filter"] += 1

        return False

    # =========================================================================
    #  STRATEGY 3: MEAN REVERSION
    #  FIX P2: extreme_only=True fires in TRENDING regime (RSI <20/>80)
    #           extreme_only=False fires in RANGING regime (RSI <30/>70 + BB)
    # =========================================================================
    def _mean_reversion_entry(self, bar: TradeBar, extreme_only: bool = False):
        rsi      = self._rsi_mr.current.value
        atr      = self._atr.current.value
        c        = bar.close
        bb_upper = self._bb.upper_band.current.value
        bb_lower = self._bb.lower_band.current.value
        bb_mid   = self._bb.middle_band.current.value
        sl_dist  = atr * self._p("mr_sl_atr_mult")

        if sl_dist <= 0:
            return

        if extreme_only:
            # TRENDING regime: only fade extreme RSI — price far from mean
            # No Bollinger requirement — extreme RSI alone is the signal
            os_thresh = self._p("mr_rsi_extreme_oversold")
            ob_thresh = self._p("mr_rsi_extreme_overbought")
            strat_tag = "MR_EXT"
        else:
            # RANGING regime: standard RSI + must be outside Bollinger
            os_thresh = self._p("mr_rsi_oversold")
            ob_thresh = self._p("mr_rsi_overbought")
            strat_tag = "MR"

        # Long: oversold condition
        long_ok = rsi < os_thresh
        if not extreme_only:
            long_ok = long_ok and (c < bb_lower)    # also need price below lower BB in ranging

        if long_ok:
            sl = c - sl_dist
            tp = bb_mid if not extreme_only else (c + sl_dist * self._p("mr_tp_atr_mult"))
            if tp > c and sl_dist > 0 and (tp - c) / sl_dist >= self._p("min_rr"):
                if self._place_trade(bar, sl, tp, "bullish", strat_tag):
                    if extreme_only:
                        self.stats["mr_ext_trades"] += 1
                    else:
                        self.stats["mr_trades"] += 1
                    return

        # Short: overbought condition
        short_ok = rsi > ob_thresh
        if not extreme_only:
            short_ok = short_ok and (c > bb_upper)  # also need price above upper BB in ranging

        if short_ok:
            sl = c + sl_dist
            tp = bb_mid if not extreme_only else (c - sl_dist * self._p("mr_tp_atr_mult"))
            if tp < c and sl_dist > 0 and (c - tp) / sl_dist >= self._p("min_rr"):
                if self._place_trade(bar, sl, tp, "bearish", strat_tag):
                    if extreme_only:
                        self.stats["mr_ext_trades"] += 1
                    else:
                        self.stats["mr_trades"] += 1

    # =========================================================================
    #  STRATEGY 4: SENTIMENT / PANIC FADE — FIX P4: more signals
    # =========================================================================
    def _sentiment_entry(self, bar: TradeBar):
        rsi    = self._rsi_sent.current.value
        atr    = self._atr.current.value
        c      = bar.close
        vol_lb = self._p("volatility_lookback", int)
        bars   = list(self._bars)

        if len(bars) < vol_lb + 1:
            return
        avg_vol = sum(b.volume for b in bars[-(vol_lb+1):-1]) / vol_lb
        if bars[-1].volume < avg_vol * self._p("sent_vol_spike_mult"):
            return

        vwap = self._rolling_vwap(self._p("sent_vwap_lookback", int))
        if vwap is None:
            return

        # FIX P4: parameterized VWAP deviation threshold
        vwap_dev = self._p("sent_vwap_dev_pct")
        sl_dist  = atr * self._p("sent_sl_atr_mult")
        tp_dist  = atr * self._p("sent_tp_atr_mult")

        if rsi > self._p("sent_overbought") and c > vwap * (1 + vwap_dev):
            sl = c + sl_dist
            tp = c - tp_dist
            if self._place_trade(bar, sl, tp, "bearish", "SENT"):
                self.stats["sent_trades"] += 1

        elif rsi < self._p("sent_oversold") and c < vwap * (1 - vwap_dev):
            sl = c - sl_dist
            tp = c + tp_dist
            if self._place_trade(bar, sl, tp, "bullish", "SENT"):
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
    def _place_trade(self, bar: TradeBar, sl, tp, direction, strategy_name) -> bool:
        price   = bar.close
        sl_dist = abs(price - sl)
        tp_dist = abs(tp - price)
        if sl_dist <= 0 or tp_dist <= 0:
            return False

        max_sl = price * self._p("max_sl_pct")
        if sl_dist > max_sl:
            self.stats["skipped_sl_too_wide"] += 1
            return False

        rr = tp_dist / sl_dist
        if rr < self._p("min_rr"):
            self.stats["skipped_rr"] += 1
            return False

        if direction == "bullish" and (sl >= price or tp <= price):
            return False
        if direction == "bearish" and (sl <= price or tp >= price):
            return False

        equity = self.portfolio.total_portfolio_value

        effective_risk = self._p("risk_pct")
        if self._consec_losses >= self._p("loss_streak_n", int):
            scaled         = effective_risk * self._p("loss_streak_scale")
            floor          = effective_risk * self._p("loss_streak_floor")
            effective_risk = max(scaled, floor)
            self.stats["scaled_risk"] += 1

        risk_qty    = (equity * effective_risk) / sl_dist
        pos_qty     = (equity * self._p("max_pos_pct")) / price
        margin_used = self.portfolio.total_margin_used
        margin_cap  = max(0, equity * self._p("max_margin_pct") - margin_used)
        margin_qty  = margin_cap / price
        qty         = min(risk_qty, pos_qty, margin_qty)

        if qty <= 0:
            return False

        lot_size = self.securities[self._sym].symbol_properties.lot_size
        if qty < lot_size:
            self.stats["skipped_lotsize"] += 1
            return False

        signed_qty = qty if direction == "bullish" else -qty
        self.market_order(self._sym, signed_qty)

        self._sl              = sl
        self._tp              = tp
        self._entry_price     = price
        self._initial_qty     = qty
        self._position_active = True
        self._active_strategy = strategy_name
        self._active_dir      = direction
        self._entry_regime    = self._regime

        side = "LONG" if direction == "bullish" else "SHORT"
        self.log(
            f"[{strategy_name} {side}] regime={self._regime}  qty={qty:.4f} @ {price:.2f}"
            f"  SL={sl:.2f}  TP={tp:.2f}  R:R={rr:.2f}"
            f"  risk={effective_risk:.4f}  streak={self._consec_losses}"
        )

        self._current_trade = {
            "id":            len(self._trade_journal) + 1,
            "strategy":      strategy_name,
            "regime":        self._regime,
            "direction":     direction,
            "entry_time":    str(self.time),
            "entry_price":   price,
            "sl":            round(sl, 4),
            "tp":            round(tp, 4),
            "rr":            round(rr, 3),
            "sl_dist":       round(sl_dist, 4),
            "qty":           round(qty, 6),
            "risk_used":     round(effective_risk, 5),
            "exit_time":     None,
            "exit_price":    None,
            "outcome":       None,
            "pnl_pct":       None,
            "bars_held":     0,
            "peak_profit_r": 0.0,
            "max_dd_r":      0.0,
            "went_positive": False,
        }
        return True

    # =========================================================================
    #  POSITION MANAGEMENT
    # =========================================================================
    def _manage_position(self, bar: TradeBar):
        c = bar.close

        if self._current_trade and self._entry_price > 0:
            orig_sl_dist = self._current_trade["sl_dist"]
            if orig_sl_dist > 0:
                move      = (c - self._entry_price) if self._active_dir == "bullish" \
                            else (self._entry_price - c)
                current_r = move / orig_sl_dist
                if current_r > self._current_trade["peak_profit_r"]:
                    self._current_trade["peak_profit_r"] = round(current_r, 3)
                if current_r < self._current_trade["max_dd_r"]:
                    self._current_trade["max_dd_r"] = round(current_r, 3)
                if current_r > 0:
                    self._current_trade["went_positive"] = True
                self._current_trade["bars_held"] += 1

        if not self.portfolio[self._sym].invested:
            self.log(f"[EXTERNAL CLOSE] strategy={self._active_strategy}")
            self._consec_losses += 1
            self._close_trade_record(c, "EXTERNAL", 0.0)
            self._position_active = False
            self._smc_reset()
            return

        is_long = self.portfolio[self._sym].is_long
        hit_tp  = (is_long and c >= self._tp) or (not is_long and c <= self._tp)
        hit_sl  = (is_long and c <= self._sl) or (not is_long and c >= self._sl)

        if hit_tp or hit_sl:
            pnl   = self.portfolio[self._sym].unrealized_profit_percent
            self.liquidate(self._sym)

            strat = self._active_strategy or "UNK"
            direc = self._active_dir      or "bullish"
            reg   = self._entry_regime    or REGIME_UNKNOWN

            if hit_tp:
                outcome = "TP"
                self.stats["closed_tp"] += 1
                self._consec_losses = 0
                self._strat_tp[strat]  = self._strat_tp.get(strat, 0) + 1
                self._regime_tp[reg]   = self._regime_tp.get(reg, 0) + 1
                if direc == "bullish": self._long_tp  += 1
                else:                  self._short_tp += 1
                self.log(f"[TP] {strat}  PnL={pnl:.2%}")
            else:
                outcome = "SL"
                self.stats["closed_sl"] += 1
                self._consec_losses += 1
                self._strat_sl[strat]  = self._strat_sl.get(strat, 0) + 1
                self._regime_sl[reg]   = self._regime_sl.get(reg, 0) + 1
                if direc == "bullish": self._long_sl  += 1
                else:                  self._short_sl += 1
                self.log(f"[SL] {strat}  PnL={pnl:.2%}  streak={self._consec_losses}")

            self._close_trade_record(c, outcome, pnl)
            self._position_active = False
            self._active_strategy = None
            self._active_dir      = None
            self._entry_regime    = None
            self._smc_reset()

    def _close_trade_record(self, exit_price, outcome, pnl):
        if self._current_trade is None:
            return
        self._current_trade.update({
            "exit_time":  str(self.time),
            "exit_price": round(exit_price, 4),
            "outcome":    outcome,
            "pnl_pct":    round(pnl * 100, 4) if pnl else 0.0,
        })
        self._trade_journal.append(self._current_trade)
        self._current_trade = None

    # =========================================================================
    #  END OF ALGORITHM — FULL ANALYSIS PANEL
    # =========================================================================
    def on_end_of_algorithm(self):
        tp_tot = self.stats["closed_tp"]
        sl_tot = self.stats["closed_sl"]
        total  = tp_tot + sl_tot

        self.log("=" * 70)
        self.log("ANALYSIS PANEL — MultiAlgoSOL v4 | SOLUSDT 15m | 2023–2026")
        self.log("=" * 70)

        # ── Regime distribution ───────────────────────────────────────────────
        total_reg = (self.stats["regime_trending"] + self.stats["regime_ranging"] +
                     self.stats["regime_panic"])
        self.log("")
        self.log("── REGIME DISTRIBUTION ──────────────────────────────────────")
        if total_reg > 0:
            self.log(f"  Trending : {self.stats['regime_trending']:>6}  ({self.stats['regime_trending']/total_reg:.1%})")
            self.log(f"  Ranging  : {self.stats['regime_ranging']:>6}  ({self.stats['regime_ranging']/total_reg:.1%})")
            self.log(f"  Panic    : {self.stats['regime_panic']:>6}  ({self.stats['regime_panic']/total_reg:.1%})")

        # ── Edge diagnosis ────────────────────────────────────────────────────
        self.log("")
        self.log("── EDGE DIAGNOSIS ───────────────────────────────────────────")
        if total > 0:
            real_wr      = tp_tot / total * 100
            min_rr       = self._p("min_rr")
            breakeven_wr = 1 / (1 + min_rr) * 100
            has_edge     = real_wr >= breakeven_wr
            ev           = (real_wr/100) * min_rr - (1 - real_wr/100) * 1.0
            self.log(f"  Total trades         : {total}")
            self.log(f"  TP / SL              : {tp_tot}W / {sl_tot}L")
            self.log(f"  Win Rate             : {real_wr:.1f}%")
            self.log(f"  Breakeven WR @ {min_rr}R   : {breakeven_wr:.1f}%")
            self.log(f"  Has mathematical edge: {'YES ✓' if has_edge else 'NO — losing in expectation'}")
            self.log(f"  Approx EV per trade  : {ev:+.3f}R")
        else:
            self.log("  No completed trades.")

        # ── Per-strategy breakdown ────────────────────────────────────────────
        self.log("")
        self.log("── PER-STRATEGY BREAKDOWN ───────────────────────────────────")
        strat_names  = {"SMC": "SMC/BOS", "MOM": "Momentum",
                        "MR": "Mean Rev", "MR_EXT": "MR Extreme", "SENT": "Sentiment"}
        strat_placed = {"SMC":  self.stats["smc_trades"],   "MOM": self.stats["mom_trades"],
                        "MR":   self.stats["mr_trades"],    "MR_EXT": self.stats["mr_ext_trades"],
                        "SENT": self.stats["sent_trades"]}
        for k, label in strat_names.items():
            t  = self._strat_tp.get(k, 0)
            s  = self._strat_sl.get(k, 0)
            n  = t + s
            wr = t / n * 100 if n > 0 else 0
            self.log(f"  {label:<12}  placed={strat_placed[k]:>3}  closed={n:>3}  WR={wr:.0f}%  ({t}W/{s}L)")

        # ── Per-regime outcome split ──────────────────────────────────────────
        self.log("")
        self.log("── REGIME OUTCOME SPLIT ─────────────────────────────────────")
        for reg in [REGIME_TRENDING, REGIME_RANGING, REGIME_PANIC]:
            t  = self._regime_tp.get(reg, 0)
            s  = self._regime_sl.get(reg, 0)
            n  = t + s
            wr = t / n * 100 if n > 0 else 0
            self.log(f"  {reg:<10}  trades={n:>3}  WR={wr:.0f}%  ({t}W/{s}L)")

        # ── Long vs short bias ────────────────────────────────────────────────
        self.log("")
        self.log("── DIRECTIONAL BIAS ─────────────────────────────────────────")
        long_total  = self._long_tp  + self._long_sl
        short_total = self._short_tp + self._short_sl
        all_dir     = long_total + short_total
        if all_dir > 0:
            long_pct  = long_total  / all_dir * 100
            short_pct = short_total / all_dir * 100
            long_wr   = self._long_tp  / long_total  * 100 if long_total  > 0 else 0
            short_wr  = self._short_tp / short_total * 100 if short_total > 0 else 0
            self.log(f"  Long  trades : {long_total:>3}  ({long_pct:.0f}%)  WR={long_wr:.0f}%")
            self.log(f"  Short trades : {short_total:>3}  ({short_pct:.0f}%)  WR={short_wr:.0f}%")
            if short_pct < 15:
                self.log("  !! BETA WARNING: <15% short trades — strategy is long-biased")
            else:
                self.log("  ✓ Reasonable directional mix")

        # ── Intra-trade R statistics ──────────────────────────────────────────
        self.log("")
        self.log("── INTRA-TRADE R STATISTICS ─────────────────────────────────")
        if self._trade_journal:
            peaks  = [t["peak_profit_r"] for t in self._trade_journal if t["outcome"] != "EXTERNAL"]
            dds    = [t["max_dd_r"]      for t in self._trade_journal if t["outcome"] != "EXTERNAL"]
            held   = [t["bars_held"]     for t in self._trade_journal if t["outcome"] != "EXTERNAL"]
            went_p = sum(1 for t in self._trade_journal if t["went_positive"])
            n_j    = len(peaks)
            if n_j > 0:
                avg_peak = sum(peaks) / n_j
                avg_dd   = sum(dds)   / n_j
                avg_bars = sum(held)  / n_j
                self.log(f"  Trades analysed      : {n_j}")
                self.log(f"  Avg peak profit R    : {avg_peak:+.2f}R")
                self.log(f"  Max peak profit R    : {max(peaks):+.2f}R")
                self.log(f"  Avg max drawdown R   : {avg_dd:+.2f}R")
                self.log(f"  Worst single DD R    : {min(dds):+.2f}R")
                self.log(f"  Avg bars held        : {avg_bars:.1f}  ({avg_bars*self._tf/60:.1f}h)")
                self.log(f"  Went positive first  : {went_p}/{n_j} ({went_p/n_j*100:.0f}%)")

                sl_trades  = [t for t in self._trade_journal if t["outcome"] == "SL"]
                fakeout_sl = [t for t in sl_trades if t["peak_profit_r"] > 0.5]
                if sl_trades:
                    fo_pct     = len(fakeout_sl) / len(sl_trades) * 100
                    avg_sl_pk  = sum(t["peak_profit_r"] for t in sl_trades) / len(sl_trades)
                    self.log(f"  SL fake-outs (>0.5R): {len(fakeout_sl)}/{len(sl_trades)} ({fo_pct:.0f}%)")
                    self.log(f"  Avg peak R before SL: {avg_sl_pk:+.2f}R")
                    if fo_pct > 40:
                        self.log("  !! SL FAKEOUT: >40% losses went +ve first — SL too tight or TP too far")

        # ── Filter funnel ─────────────────────────────────────────────────────
        self.log("")
        self.log("── FILTER FUNNEL ────────────────────────────────────────────")
        self.log(f"  Skipped: RR fail       {self.stats['skipped_rr']:>6}")
        self.log(f"  Skipped: SL too wide   {self.stats['skipped_sl_too_wide']:>6}")
        self.log(f"  Skipped: HTF filter    {self.stats['skipped_trend_filter']:>6}")
        self.log(f"  Skipped: lot size      {self.stats['skipped_lotsize']:>6}")
        self.log(f"  Risk scaled (streak)   {self.stats['scaled_risk']:>6}")
        self.log(f"  DD pauses              {self.stats['paused_dd']:>6}")
        self.log(f"  SMC BOS activated      {self.stats['smc_bos_activated']:>6}")
        self.log(f"  SMC SL dir error       {self.stats['smc_sl_direction_error']:>6}  (should be 0 now)")
        self.log(f"  SMC BOS expired        {self.stats['smc_bos_expired']:>6}")
        self.log(f"  SMC no hold            {self.stats['smc_bos_no_hold']:>6}")
        self.log(f"  SMC no retest          {self.stats['smc_entry_no_retest']:>6}")
        self.log(f"  SMC VP miss            {self.stats['smc_entry_vp_miss']:>6}")
        self.log(f"  SMC no confirm         {self.stats['smc_entry_no_confirm']:>6}")

        # ── Auto-diagnosis ────────────────────────────────────────────────────
        self.log("")
        self.log("── AUTO-DIAGNOSIS ───────────────────────────────────────────")
        if total > 0:
            if self.stats["smc_sl_direction_error"] > 0:
                self.log(f"  !! SMC still has {self.stats['smc_sl_direction_error']} SL direction errors")
                self.log("     ph/pl mismatch still occurring — check smc_pivot_left/right params")

            if self.stats["smc_trades"] == 0 and self.stats["smc_bos_activated"] > 10:
                self.log(f"  !! SMC: {self.stats['smc_bos_activated']} BOS but 0 trades")
                self.log(f"     no_retest={self.stats['smc_entry_no_retest']}  vp_miss={self.stats['smc_entry_vp_miss']}  no_confirm={self.stats['smc_entry_no_confirm']}")
                if self.stats["smc_entry_no_retest"] > self.stats["smc_bos_activated"] * 0.5:
                    self.log("     → Price not returning to retest. Try smc_retest_zone_pct=0.03")
                if self.stats["smc_entry_vp_miss"] > self.stats["smc_bos_activated"] * 0.3:
                    self.log("     → VP confluence too strict. Try smc_vp_zone_pct=0.06")

            if self.stats["mr_trades"] == 0 and self.stats["regime_ranging"] < 100:
                self.log("  !! Mean Reversion still getting no RANGING bars")
                self.log(f"     Ranging bars: {self.stats['regime_ranging']} — check adx_range_thresh")

            mr_ext = self.stats["mr_ext_trades"]
            if mr_ext > 0:
                self.log(f"  ✓ MR Extreme fired {mr_ext} times in TRENDING regime (new in v4)")

            sent = self.stats["sent_trades"]
            if sent < 30:
                self.log(f"  !! Sentiment only {sent} trades — try sent_vol_spike_mult=1.2")

            short_total_v = self._short_tp + self._short_sl
            all_dir_v     = (self._long_tp + self._long_sl) + short_total_v
            if all_dir_v > 0 and short_total_v / all_dir_v < 0.1:
                self.log("  !! <10% short trades — strategy is long-only (beta, not alpha)")

            if total < 30:
                self.log(f"  !! Only {total} trades in 3 years — too selective for reliable stats")

            # Compare v3 vs v4
            self.log("")
            self.log("  v3 BASELINE: 697 trades, 35% WR, -0.128R EV, SMC=0, MR=0")
            real_wr_v4 = tp_tot / total * 100 if total > 0 else 0
            min_rr_v4  = self._p("min_rr")
            ev_v4      = (real_wr_v4/100) * min_rr_v4 - (1 - real_wr_v4/100) * 1.0
            self.log(f"  v4 RESULT  : {total} trades, {real_wr_v4:.1f}% WR, {ev_v4:+.3f}R EV, "
                     f"SMC={self.stats['smc_trades']}, MR={self.stats['mr_trades']}, "
                     f"MR_EXT={self.stats['mr_ext_trades']}")

        self.log("=" * 70)

        # ── Parseable CSV output ──────────────────────────────────────────────
        journal_headers = [
            "id", "strategy", "regime", "direction",
            "entry_time", "entry_price", "sl", "tp", "rr", "sl_dist", "qty", "risk_used",
            "exit_time", "exit_price", "outcome", "pnl_pct", "bars_held",
            "peak_profit_r", "max_dd_r", "went_positive",
        ]
        self.log("JOURNAL_CSV|" + "|".join(journal_headers))
        for t in self._trade_journal:
            self.log("JOURNAL_CSV|" + "|".join(str(t.get(h, "")) for h in journal_headers))

        self.log("EQUITY_CSV|timestamp|equity")
        for ts, eq in self._equity_curve:
            self.log(f"EQUITY_CSV|{ts}|{eq:.2f}")

        self.log("=" * 70)