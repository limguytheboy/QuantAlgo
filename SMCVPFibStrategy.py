from AlgorithmImports import *
from datetime import timedelta
from collections import deque


# ─────────────────────────────────────────────────────────────────────────────
#  DEFAULTS
# ─────────────────────────────────────────────────────────────────────────────
_DEFAULTS = {
    # Pivot detection
    "pivot_left":           4,
    "pivot_right":          4,
    "atr_period":           14,

    # Volume spike on BOS bar (compared to prior bars, not including itself)
    "vol_spike_mult":       1.5,      # BOS bar must be > N x avg of prior bars
    "vol_lookback":         20,

    # Volume Profile confluence
    # vp_mode: 0=VPOC, 1=VAH/VAL midpoint, 2=nearest HVN to BOS level
    "vp_mode":              0,
    "vp_lookback":          40,
    "vp_bins":              24,
    "vp_zone_pct":          0.025,    # VP level must be within 2.5% of BOS level

    # BOS / Entry
    "bos_atr_min_mult":     0.05,
    # Entry zone: price must retrace back INTO this % of the BOS level
    # We enter ONLY when price is below BOS level (bullish) or above (bearish)
    # so we're buying the retest, not chasing the breakout
    "retest_zone_pct":      0.015,    # price must be within 1.5% BELOW bos level (bull)

    # Minimum R:R required before placing a trade
    "min_rr":               1.5,

    # SL / TP
    "sl_buffer_atr":        0.3,
    # fib_tp: 0=0.618ext, 1=1.0ext, 2=1.272ext, 3=1.618ext,
    #          4=2.0ext, 5=2.618ext, 6=0.618retrace, 7=0.786retrace
    "fib_tp":               3,        # default = 1.618 extension

    # HTF filter
    "use_htf_filter":       0.0,
    "htf_ema_period":       50,

    # BOS expiry
    "max_open_bars":        72,

    # Sizing — conservative defaults safe for volatile assets like SOL
    "risk_pct":             0.005,    # 0.5% risk per trade (was 1%)
    "max_pos_pct":          0.10,     # max 10% of equity in one position (was 20%)
    "max_margin_pct":       0.30,     # hard cap: total margin used never exceeds 30% of equity

    # Drawdown protection
    "max_drawdown_pct":     0.08,     # pause trading if drawdown from peak exceeds 8%
    "loss_streak_scale":    0.5,      # reduce risk_pct by this factor after N consecutive losses
    "loss_streak_n":        3,        # how many consecutive losses triggers the scale-down

    # FIX: minimum risk floor — scaled risk never drops below this fraction of base risk_pct
    # Prevents cascading near-zero position sizes during long loss streaks
    "loss_streak_floor":    0.5,      # effective_risk never falls below risk_pct * this value

    # Minimum swing size filter (cuts low-quality tiny-swing trades = fewer fees)
    "min_swing_atr_mult":   1.5,      # swing_move must be >= N x ATR to qualify

    # Post-BOS hold check — "if price can undo it quickly it was liquidity, not direction"
    # The bar immediately after the BOS bar must NOT close back beyond the BOS level.
    # e.g. for bullish BOS: bar[BOS+1] must close ABOVE the BOS level (held the break).
    # Set to 0 to disable.
    "bos_hold_bars":        1,        # how many bars after BOS must hold before retest is valid

    # Confirmation candle check — "entry = reaction, not touch"
    # When price is in the retest zone the current bar must close showing rejection:
    #   bullish: close > open  (buyers stepped in, bar closed up)
    #   bearish: close < open  (sellers stepped in, bar closed down)
    # AND the candle body (|close - open|) must be >= conf_body_atr_mult * ATR
    # to filter doji / indecision bars that look like reactions but aren't.
    "conf_body_atr_mult":   0.3,      # minimum body size as fraction of ATR (0 = direction only)

    # Symbol and dates
    "symbol":               "SOLUSDT",
    "timeframe":            "4h",
}

_TF_MAP     = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "1h": 60, "2h": 120, "4h": 240}

# fib_tp index → (mode, multiplier)
# mode "ext"     = project N x swing_move BEYOND the BOS level (standard extension)
# mode "retrace" = TP is AT (bos_level - swing_move * N) i.e. N retracement of the swing
#                  For bullish: TP = swing_origin + swing_move * N  (below the BOS level)
#                  This targets the 0.618 golden pocket on the way back up
_FIB_TABLE = {
    # idx: (mode,      multiplier,  label)
    0:     ("ext",     0.618,       "0.618 ext"),
    1:     ("ext",     1.0,         "1.0 ext"),
    2:     ("ext",     1.272,       "1.272 ext"),
    3:     ("ext",     1.618,       "1.618 ext"),
    4:     ("ext",     2.0,         "2.0 ext"),
    5:     ("ext",     2.618,       "2.618 ext"),
    6:     ("retrace", 0.618,       "0.618 retrace"),
    7:     ("retrace", 0.786,       "0.786 retrace"),
}


class SMCVPFibStrategy(QCAlgorithm):
    """
    Entry logic (fixed):
      - BOS detected: price closes BEYOND a confirmed swing level with vol spike
      - Retest: price pulls BACK to just below (bull) / above (bear) the BOS level
        This means we're buying support, not chasing a breakout
      - VP confluence: rolling VPOC/VAH-VAL/HVN must be near the BOS level
      - SL: behind the swing that got broken, + ATR buffer
      - TP: Fibonacci extension of swing range from swing origin through BOS level
      - R:R gate: trade rejected if calculated R:R < min_rr
    """

    def initialize(self):
        self.set_start_date(2020, 6, 1)
        self.set_end_date(2026, 2, 1)
        self.set_account_currency("USDT")
        self.set_cash("USDT", 50_000)
        self.set_brokerage_model(BrokerageName.BINANCE, AccountType.MARGIN)

        symbol_str   = _DEFAULTS["symbol"]
        qc_sym       = self.get_parameter("symbol")
        if qc_sym:
            symbol_str = qc_sym
        contract = self.add_crypto(symbol_str, Resolution.MINUTE, Market.BINANCE)
        self._symbol = contract.symbol

        def param(key, cast=float):
            v = self.get_parameter(key)
            return cast(v) if v is not None else cast(_DEFAULTS[key])

        self.pivot_left           = param("pivot_left",         int)
        self.pivot_right          = param("pivot_right",        int)
        atr_period                = param("atr_period",         int)

        self.vol_spike_mult       = param("vol_spike_mult")
        self.vol_lookback         = param("vol_lookback",       int)

        self.vp_mode              = param("vp_mode",            int)
        self.vp_lookback          = param("vp_lookback",        int)
        self.vp_bins              = param("vp_bins",            int)
        self.vp_zone_pct          = param("vp_zone_pct")

        self.bos_atr_min_mult     = param("bos_atr_min_mult")
        self.retest_zone_pct      = param("retest_zone_pct")
        self.min_rr               = param("min_rr")
        self.sl_buffer_atr        = param("sl_buffer_atr")

        fib_idx                   = param("fib_tp",             int)
        fib_entry                 = _FIB_TABLE.get(fib_idx, _FIB_TABLE[3])
        self.fib_mode             = fib_entry[0]    # "ext" or "retrace"
        self.fib_mult             = fib_entry[1]
        self.fib_label            = fib_entry[2]

        self.use_htf_filter       = param("use_htf_filter") > 0.5
        htf_ema_period            = param("htf_ema_period",     int)

        self.max_open_bars        = param("max_open_bars",      int)
        self.risk_pct             = param("risk_pct")
        self.max_pos_pct          = param("max_pos_pct")
        self.max_margin_pct       = param("max_margin_pct")
        self.max_dd_pct           = param("max_drawdown_pct")
        self.loss_streak_scale    = param("loss_streak_scale")
        self.loss_streak_n        = param("loss_streak_n",      int)
        # FIX: risk floor — effective risk never drops below risk_pct * this fraction
        self.loss_streak_floor    = param("loss_streak_floor")
        self.min_swing_atr        = param("min_swing_atr_mult")
        self.bos_hold_bars        = param("bos_hold_bars",       int)
        self.conf_body_atr_mult   = param("conf_body_atr_mult")

        tf_str                    = param("timeframe",          str)
        self._tf_mins             = _TF_MAP.get(tf_str, 60)

        # Indicators
        self._atr     = self.atr(self._symbol, atr_period, MovingAverageType.WILDERS)
        self._ema_htf = self.ema(self._symbol, htf_ema_period, Resolution.HOUR) if self.use_htf_filter else None

        buf_size         = max(self.vp_lookback, self.vol_lookback,
                               self.pivot_left + self.pivot_right + 10) + 10
        self._bar_buffer = deque(maxlen=buf_size)

        self.consolidate(self._symbol, timedelta(minutes=self._tf_mins), self.on_trading_bar)
        if self.use_htf_filter:
            self.consolidate(self._symbol, timedelta(hours=4), self.on_four_hour_bar)

        # State
        self.htf_trend        = None
        self.confirmed_highs  = deque(maxlen=30)
        self.confirmed_lows   = deque(maxlen=30)

        self.bos_active       = False
        self.bos_dir          = None
        self.bos_level        = None
        self.bos_swing_sl     = None
        self.bos_swing_origin = None
        self.bos_bar_idx      = -1
        self.bar_index        = 0

        self.position_active  = False
        self._sl              = 0.0
        self._tp              = 0.0

        # Drawdown and streak tracking
        self._peak_equity     = 50_000.0
        self._consec_losses   = 0
        self._trading_paused  = False

        # FIX: track whether we've already counted a no-retest miss for the
        # current BOS activation, so entry_no_retest increments once per BOS,
        # not once per bar while waiting
        self._bos_retest_logged = False

        # Post-BOS hold: store the BOS bar's close so we can check the next bar held it
        self._bos_bar_close     = None
        self._bos_held          = False   # becomes True once bos_hold_bars bars confirm hold

        self.stats = {
            "bars_processed":    0,
            "pivots_confirmed":  0,
            "bos_candidates":    0,
            "bos_vol_filtered":  0,
            "bos_htf_filtered":  0,
            "bos_activated":     0,
            "bos_expired":       0,
            "entry_no_retest":   0,
            "entry_vp_miss":     0,
            "entry_rr_fail":     0,
            "entry_no_hold":     0,    # BOS not held — break was immediately reversed (liquidity grab)
            "entry_no_confirm":  0,    # retest zone reached but no rejection candle confirmation
            "entry_triggered":   0,
            "trades_placed":     0,
            "trades_skipped_lotsize": 0,   # FIX: new counter for lot-size rejections
            "closed_tp":         0,
            "closed_sl":         0,
            "paused_dd":         0,    # trades blocked by drawdown circuit breaker
            "scaled_risk":       0,    # trades placed at reduced risk due to loss streak
            "swing_too_small":   0,    # trades blocked by min swing filter
        }

        self.log(
            f"=== INIT tf={tf_str} pivot={self.pivot_left}L/{self.pivot_right}R"
            f" vol={self.vol_spike_mult}x/{self.vol_lookback}bars"
            f" vp_mode={self.vp_mode} vp_lb={self.vp_lookback}"
            f" retest={self.retest_zone_pct:.1%} vp_zone={self.vp_zone_pct:.1%}"
            f" fib='{self.fib_label}' min_rr={self.min_rr}"
            f" sl_buf={self.sl_buffer_atr}xATR htf={'on' if self.use_htf_filter else 'off'}"
            f" risk_floor={self.loss_streak_floor:.0%}"
            f" hold={self.bos_hold_bars}bars conf_body={self.conf_body_atr_mult}xATR"
        )

    # ─────────────────────────────────────────────────────────────────────────
    def on_four_hour_bar(self, bar: TradeBar):
        if self._ema_htf is None or not self._ema_htf.is_ready:
            return
        prev           = self.htf_trend
        self.htf_trend = "up" if bar.close > self._ema_htf.current.value else "down"
        if self.htf_trend != prev:
            self.log(f"[HTF] {prev} -> {self.htf_trend}  close={bar.close:.2f}")

    # ─────────────────────────────────────────────────────────────────────────
    def on_trading_bar(self, bar: TradeBar):
        self.bar_index += 1
        if not self._atr.is_ready:
            return

        self.stats["bars_processed"] += 1
        self._bar_buffer.append(bar)

        if len(self._bar_buffer) < self.pivot_left + self.pivot_right + 1:
            return

        self.detect_pivots()

        # ── Drawdown circuit breaker ──────────────────────────────────
        equity = self.portfolio.total_portfolio_value
        if equity > self._peak_equity:
            self._peak_equity    = equity
            self._trading_paused = False   # recover when new peak is made
        current_dd = (self._peak_equity - equity) / self._peak_equity
        if current_dd >= self.max_dd_pct:
            if not self._trading_paused:
                self.log(f"[PAUSED] Drawdown {current_dd:.2%} >= {self.max_dd_pct:.2%}  equity={equity:.2f}")
                self._trading_paused = True
                self.stats["paused_dd"] += 1
            if self.bos_active:
                self.reset_bos()
            return

        self.check_bos(bar)

        if self.bos_active and not self.position_active:
            if (self.bar_index - self.bos_bar_idx) > self.max_open_bars:
                self.stats["bos_expired"] += 1
                self.log(f"[BOS EXPIRED] {self.bos_dir}")
                self.reset_bos()

        if self.bos_active and not self.position_active:
            self.try_entry(bar)

        if self.position_active:
            self.manage_position(bar.close)

    # ─────────────────────────────────────────────────────────────────────────
    def detect_pivots(self):
        buf = list(self._bar_buffer)
        n   = len(buf)
        ci  = n - self.pivot_right - 1      # positive candidate index

        if ci < self.pivot_left:
            return

        pivot = buf[ci]
        left  = buf[ci - self.pivot_left : ci]
        right = buf[ci + 1 : ci + 1 + self.pivot_right]

        if len(left) < self.pivot_left or len(right) < self.pivot_right:
            return

        if (all(pivot.high >= b.high for b in left) and
                all(pivot.high >= b.high for b in right)):
            self.confirmed_highs.append((self.bar_index, pivot.high))
            self.stats["pivots_confirmed"] += 1

        if (all(pivot.low <= b.low for b in left) and
                all(pivot.low <= b.low for b in right)):
            self.confirmed_lows.append((self.bar_index, pivot.low))
            self.stats["pivots_confirmed"] += 1

    # ─────────────────────────────────────────────────────────────────────────
    def has_volume_spike(self):
        buf = list(self._bar_buffer)
        if len(buf) < self.vol_lookback + 1:
            return True
        bos_bar   = buf[-1]
        prior     = buf[-(self.vol_lookback + 1):-1]
        avg_vol   = sum(b.volume for b in prior) / len(prior)
        return bos_bar.volume > avg_vol * self.vol_spike_mult

    # ─────────────────────────────────────────────────────────────────────────
    def check_bos(self, bar: TradeBar):
        if self.bos_active or self.position_active:
            return
        if not self.confirmed_highs or not self.confirmed_lows:
            return

        atr_val  = self._atr.current.value
        min_move = atr_val * self.bos_atr_min_mult

        _, last_high = self.confirmed_highs[-1]
        _, last_low  = self.confirmed_lows[-1]

        if bar.close > last_high + min_move:
            self.stats["bos_candidates"] += 1
            if not self.has_volume_spike():
                self.stats["bos_vol_filtered"] += 1; return
            if self.use_htf_filter and self.htf_trend == "down":
                self.stats["bos_htf_filtered"] += 1; return
            self._activate_bos("bullish", last_high, last_low, last_low)

        elif bar.close < last_low - min_move:
            self.stats["bos_candidates"] += 1
            if not self.has_volume_spike():
                self.stats["bos_vol_filtered"] += 1; return
            if self.use_htf_filter and self.htf_trend == "up":
                self.stats["bos_htf_filtered"] += 1; return
            self._activate_bos("bearish", last_low, last_high, last_high)

    def _activate_bos(self, direction, bos_level, sl_ref, swing_origin):
        self.bos_active         = True
        self.bos_dir            = direction
        self.bos_level          = bos_level
        self.bos_swing_sl       = sl_ref
        self.bos_swing_origin   = swing_origin
        self.bos_bar_idx        = self.bar_index
        # FIX: reset the per-BOS retest flag so we count no-retest misses once per activation
        self._bos_retest_logged = False
        # Store the BOS bar's close to validate hold on subsequent bars
        buf = list(self._bar_buffer)
        self._bos_bar_close = buf[-1].close if buf else bos_level
        # Hold is confirmed once bos_hold_bars bars after the BOS bar all close on the right side.
        # If bos_hold_bars == 0 we skip the check entirely (pre-set to True).
        self._bos_held      = (self.bos_hold_bars == 0)
        self.stats["bos_activated"] += 1
        self.log(f"[BOS {direction.upper()}] level={bos_level:.2f}  sl_ref={sl_ref:.2f}  origin={swing_origin:.2f}")

    # ─────────────────────────────────────────────────────────────────────────
    def get_vp_level(self):
        bars = list(self._bar_buffer)[-self.vp_lookback:]
        if len(bars) < 5:
            return None
        lo = min(b.low  for b in bars)
        hi = max(b.high for b in bars)
        if hi <= lo:
            return None

        bin_size = (hi - lo) / self.vp_bins
        vol_bins = [0.0] * self.vp_bins
        for b in bars:
            b_lo = max(0, min(int((b.low  - lo) / bin_size), self.vp_bins - 1))
            b_hi = max(0, min(int((b.high - lo) / bin_size), self.vp_bins - 1))
            span = b_hi - b_lo + 1
            for i in range(b_lo, b_hi + 1):
                vol_bins[i] += b.volume / span

        mids = [lo + (i + 0.5) * bin_size for i in range(self.vp_bins)]

        if self.vp_mode == 0:
            return mids[vol_bins.index(max(vol_bins))]
        elif self.vp_mode == 1:
            total  = sum(vol_bins)
            target = total * 0.70
            ranked = sorted(range(self.vp_bins), key=lambda i: vol_bins[i], reverse=True)
            va, cum = [], 0.0
            for i in ranked:
                va.append(i); cum += vol_bins[i]
                if cum >= target: break
            return (mids[max(va)] + mids[min(va)]) / 2.0
        else:
            ref    = self.bos_level if self.bos_level else (hi + lo) / 2
            top_n  = max(1, self.vp_bins // 3)
            ranked = sorted(range(self.vp_bins), key=lambda i: vol_bins[i], reverse=True)
            return min([mids[i] for i in ranked[:top_n]], key=lambda p: abs(p - ref))

    # ─────────────────────────────────────────────────────────────────────────
    def try_entry(self, bar: TradeBar):
        c   = bar.close
        atr = self._atr.current.value

        # ── 0. Minimum swing size filter ─────────────────────────────
        swing_move = abs(self.bos_level - self.bos_swing_origin)
        if swing_move < atr * self.min_swing_atr:
            self.stats["swing_too_small"] += 1
            return

        # ── 1. Post-BOS hold check ────────────────────────────────────
        # "If price can undo it quickly it was liquidity, not direction."
        # For the first bos_hold_bars bars after the BOS bar, verify each bar
        # closes on the correct side of the BOS level — confirming the break was
        # real displacement and not a wick/fake-out that immediately reversed.
        #   Bullish: every hold bar must close ABOVE the BOS level
        #   Bearish: every hold bar must close BELOW the BOS level
        # Once all hold bars pass we set _bos_held = True and never re-check.
        bars_since_bos = self.bar_index - self.bos_bar_idx
        if not self._bos_held:
            if bars_since_bos <= self.bos_hold_bars:
                # Still in the hold window — check this bar
                if self.bos_dir == "bullish" and c < self.bos_level:
                    self.stats["entry_no_hold"] += 1
                    self.log(f"[NO HOLD] bullish BOS reversed  c={c:.2f} < bos={self.bos_level:.2f}")
                    self.reset_bos()
                    return
                elif self.bos_dir == "bearish" and c > self.bos_level:
                    self.stats["entry_no_hold"] += 1
                    self.log(f"[NO HOLD] bearish BOS reversed  c={c:.2f} > bos={self.bos_level:.2f}")
                    self.reset_bos()
                    return
                # Bar held — but we're still inside the hold window, don't enter yet
                return
            else:
                # Passed all hold bars without reversal — mark as held
                self._bos_held = True

        # ── 2. Retest check ───────────────────────────────────────────
        # Only count the miss once per BOS activation (not once per bar).
        if self.bos_dir == "bullish":
            below_bos = c <= self.bos_level
            in_zone   = c >= self.bos_level * (1 - self.retest_zone_pct)
            retested  = below_bos and in_zone
        else:
            above_bos = c >= self.bos_level
            in_zone   = c <= self.bos_level * (1 + self.retest_zone_pct)
            retested  = above_bos and in_zone

        if not retested:
            if not self._bos_retest_logged:
                self.stats["entry_no_retest"] += 1
                self._bos_retest_logged = True
            return

        # ── 3. Confirmation candle check ──────────────────────────────
        # "Entry = reaction, not touch."
        # Price is in the retest zone — but we only enter if this bar itself
        # shows a clear rejection reaction: closes in the bias direction AND
        # has a body >= conf_body_atr_mult * ATR (filters doji / indecision).
        #   Bullish confirmation: bar closes UP  (close > open) with body >= threshold
        #   Bearish confirmation: bar closes DOWN (close < open) with body >= threshold
        body          = bar.close - bar.open          # positive = bullish bar, negative = bearish bar
        body_abs      = abs(body)
        min_body      = atr * self.conf_body_atr_mult
        bull_confirm  = (self.bos_dir == "bullish") and (body > 0) and (body_abs >= min_body)
        bear_confirm  = (self.bos_dir == "bearish") and (body < 0) and (body_abs >= min_body)
        confirmed     = bull_confirm or bear_confirm

        if not confirmed:
            self.stats["entry_no_confirm"] += 1
            return

        # ── 4. VP confluence check ────────────────────────────────────
        vp_lvl = self.get_vp_level()
        if vp_lvl is not None:
            vp_dist = abs(vp_lvl - self.bos_level) / self.bos_level
            if vp_dist > self.vp_zone_pct:
                self.stats["entry_vp_miss"] += 1
                return

        # ── 5. Compute SL and TP, then check R:R ─────────────────────
        if self.bos_dir == "bullish":
            sl = self.bos_swing_sl - atr * self.sl_buffer_atr
        else:
            sl = self.bos_swing_sl + atr * self.sl_buffer_atr

        sl_dist = abs(c - sl)
        if sl_dist <= 0:
            return

        if self.fib_mode == "ext":
            fib_dist = swing_move * self.fib_mult
            tp = (self.bos_level + fib_dist) if self.bos_dir == "bullish" else (self.bos_level - fib_dist)
        else:
            fib_dist = swing_move * self.fib_mult
            if self.bos_dir == "bullish":
                tp = self.bos_swing_origin + fib_dist
            else:
                tp = self.bos_swing_origin - fib_dist

        if self.bos_dir == "bullish" and tp <= c:
            self.stats["entry_rr_fail"] += 1
            self.log(f"[RR FAIL] bull tp={tp:.2f} <= entry={c:.2f} — swing too small")
            return
        if self.bos_dir == "bearish" and tp >= c:
            self.stats["entry_rr_fail"] += 1
            self.log(f"[RR FAIL] bear tp={tp:.2f} >= entry={c:.2f} — swing too small")
            return

        rr = abs(tp - c) / sl_dist
        if rr < self.min_rr:
            self.stats["entry_rr_fail"] += 1
            self.log(f"[RR FAIL] rr={rr:.2f} < min={self.min_rr}")
            return

        self.stats["entry_triggered"] += 1
        self.log(
            f"[ENTRY] {self.bos_dir}  c={c:.2f}  bos={self.bos_level:.2f}"
            f"  SL={sl:.2f}  TP={tp:.2f}  R:R={rr:.2f}"
            f"  body={body_abs:.2f}  vp={f'{vp_lvl:.2f}' if vp_lvl else 'N/A'}"
        )
        self.enter_position(c, sl, tp)

    # ─────────────────────────────────────────────────────────────────────────
    def enter_position(self, price, sl, tp):
        sl_dist = abs(price - sl)
        equity  = self.portfolio.total_portfolio_value

        # FIX: risk floor — effective risk never falls below risk_pct * loss_streak_floor.
        # Without this, a long loss streak cascades the position size toward zero, meaning
        # even when the strategy eventually wins it barely recovers what was lost.
        effective_risk = self.risk_pct
        if self._consec_losses >= self.loss_streak_n:
            scaled         = self.risk_pct * self.loss_streak_scale
            floor          = self.risk_pct * self.loss_streak_floor
            effective_risk = max(scaled, floor)
            self.stats["scaled_risk"] += 1
            self.log(f"[SCALED RISK] consec_losses={self._consec_losses} risk={effective_risk:.3f}")

        risk_qty    = (equity * effective_risk) / sl_dist
        pos_qty     = (equity * self.max_pos_pct) / price
        margin_used = self.portfolio.total_margin_used
        margin_left = max(0, equity * self.max_margin_pct - margin_used)
        margin_qty  = margin_left / price
        qty         = min(risk_qty, pos_qty, margin_qty)

        if qty <= 0:
            self.log(f"[SKIP] qty=0 margin_left={margin_left:.2f}")
            return

        # FIX: check lot size before submitting to avoid brokerage rejection errors.
        # Retrieve the minimum order size from the security's symbol properties and
        # skip gracefully rather than firing an error into the log.
        security   = self.securities[self._symbol]
        lot_size   = security.symbol_properties.lot_size
        if qty < lot_size:
            self.stats["trades_skipped_lotsize"] += 1
            self.log(
                f"[SKIP LOT] qty={qty:.6f} < lot_size={lot_size}  "
                f"equity={equity:.2f}  sl_dist={sl_dist:.2f}"
            )
            return

        signed_qty = qty if self.bos_dir == "bullish" else -qty
        self.market_order(self._symbol, signed_qty)

        self._sl             = sl
        self._tp             = tp
        self.position_active = True
        self.stats["trades_placed"] += 1

        direction = "LONG" if self.bos_dir == "bullish" else "SHORT"
        self.log(
            f"[TRADE {direction}] qty={qty:.4f} @ {price:.2f}"
            f"  SL={sl:.2f}  TP={tp:.2f}  R:R={abs(tp-price)/sl_dist:.2f}"
            f"  risk_usd={sl_dist*qty:.2f}  margin_used={margin_used:.2f}"
        )

    # ─────────────────────────────────────────────────────────────────────────
    def manage_position(self, c):
        if not self.portfolio[self._symbol].invested:
            # FIX: position was closed externally (e.g. margin call, manual liquidation)
            # without hitting TP or SL — treat as a loss for streak tracking purposes
            # so the streak counter stays consistent with actual trade outcomes.
            self.log(f"[CLOSE EXTERNAL] position closed outside TP/SL logic")
            self._consec_losses += 1
            self.position_active = False
            self.reset_bos()
            return

        is_long = self.portfolio[self._symbol].is_long
        hit_tp  = (is_long and c >= self._tp) or (not is_long and c <= self._tp)
        hit_sl  = (is_long and c <= self._sl) or (not is_long and c >= self._sl)

        if hit_tp or hit_sl:
            pnl = self.portfolio[self._symbol].unrealized_profit_percent
            self.liquidate(self._symbol)
            if hit_tp:
                self.stats["closed_tp"] += 1
                self._consec_losses = 0          # reset streak on win
                self.log(f"[CLOSE TP] PnL={pnl:.2%}")
            else:
                self.stats["closed_sl"] += 1
                self._consec_losses += 1         # increment streak on loss
                self.log(f"[CLOSE SL] PnL={pnl:.2%}  streak={self._consec_losses}")
            self.position_active = False
            self.reset_bos()

    # ─────────────────────────────────────────────────────────────────────────
    def reset_bos(self):
        self.bos_active         = False
        self.bos_dir            = None
        self.bos_level          = None
        self.bos_swing_sl       = None
        self.bos_swing_origin   = None
        self.bos_bar_idx        = -1
        # FIX: clear the retest flag so it's fresh for the next BOS activation
        self._bos_retest_logged = False
        self._bos_bar_close     = None
        self._bos_held          = False

    # ─────────────────────────────────────────────────────────────────────────
    def on_end_of_algorithm(self):
        self.log("=" * 60)
        self.log("FINAL STATS")
        self.log("=" * 60)
        for k, v in self.stats.items():
            self.log(f"  {k:<30} {v}")
        t  = self.stats["trades_placed"]
        tp = self.stats["closed_tp"]
        sl = self.stats["closed_sl"]
        if (tp + sl) > 0:
            self.log(f"  win_rate                       {tp/(tp+sl)*100:.1f}%  ({tp}W/{sl}L)")
        self.log("=" * 60)