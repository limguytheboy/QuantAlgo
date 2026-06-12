import time
import pandas as pd
import numpy as np
import random
from alpha_vantage.fundamentaldata import FundamentalData
from alpha_vantage.timeseries import TimeSeries
import sys
import os
from dotenv import load_dotenv

# ────────────────────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────────────────────

API_KEY = os.getenv("API_KEY")
NUM_STOCKS = 5                    # Start small (5 calls/min limit → ~15–20 min for 5 stocks)
MIN_PERIODS = 5                   # min annual reports
MIN_MONTHS_HISTORY = 24

# Shuffled tickers (same as before)
tickers = [
    'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', 'GOOGL', 'GOOG', 'TSLA', 'BRK-B', 'LLY',
    'AVGO', 'JPM', 'UNH', 'V', 'MA', 'XOM', 'HD', 'PG', 'COST', 'JNJ',
    'MRK', 'ABBV', 'BAC', 'KO', 'WMT', 'CVX', 'CRM', 'AMD', 'NFLX', 'PEP',
    'ADBE', 'QCOM', 'ORCL', 'TMO', 'MCD', 'CSCO', 'INTC', 'ABT', 'DHR', 'PFE',
    'TMUS', 'VZ', 'NEE', 'DIS', 'AMGN', 'GS', 'CAT', 'RTX', 'SPGI', 'ISRG',
    'AXP', 'IBM', 'GE', 'UBER', 'PM', 'NOW', 'UNP', 'COP', 'HON', 'ETN',
    'BKNG', 'SBUX', 'TJX', 'LOW', 'MU', 'LMT', 'SYK', 'BLK', 'MDT', 'CB',
    'ADP', 'REGN', 'PANW', 'KLAC', 'ADI', 'FI', 'INTU', 'DE', 'MMC', 'PLD',
    'BMY', 'GILD', 'BA', 'AMAT', 'CMG', 'SCHW', 'ICE', 'ZTS', 'MDLZ', 'CL',
    'MO', 'T', 'CMCSA', 'EOG', 'DUK', 'SO', 'BDX', 'CI', 'ITW', 'CSX',
    'SHW', 'MPC', 'NOC', 'PGR', 'APH', 'AON'
]

random.seed(42)
random.shuffle(tickers)
stocks = tickers[:NUM_STOCKS]

print(f"Analyzing {len(stocks)} stocks with Alpha Vantage...\n")

fd = FundamentalData(key=API_KEY, output_format='pandas')
ts = TimeSeries(key=API_KEY, output_format='pandas')

all_gaps = []
recovery_months = []
recovered_events = []
analyzed_stocks = 0
total_events = 0

try:
    for idx, ticker in enumerate(stocks, 1):
        try:
            print(f"[{idx:3d}/{len(stocks)}] {ticker:6} ... ", end="", flush=True)

            # 1. Annual balance sheet
            bs_raw, meta = fd.get_balance_sheet_annual(ticker)
            time.sleep(15)

            if bs_raw.empty:
                print("no balance sheet")
                continue

            # Alpha Vantage format: columns = metrics, rows = years + metadata
            # Usually first column = fiscalDateEnding
            if 'fiscalDateEnding' in bs_raw.columns:
                bs = bs_raw.set_index('fiscalDateEnding')
                bs.index = pd.to_datetime(bs.index, errors='coerce')
                bs = bs[bs.index.notna()]
            else:
                # Fallback: transpose and look for date row
                bs = bs_raw.T
                date_col = [c for c in bs.columns if 'fiscal' in c.lower() or 'date' in c.lower()]
                if date_col:
                    bs = bs.set_index(date_col[0])
                    bs.index = pd.to_datetime(bs.index, errors='coerce')
                    bs = bs[bs.index.notna()]
                else:
                    print("no date column found")
                    continue

            if bs.empty:
                print("no valid dates after parsing")
                continue

            # Debug: uncomment to see columns
            # print(f" | Columns: {bs.columns.tolist()[:6]}")

            # Find equity and shares (case-insensitive)
            equity_col = next((c for c in bs.columns if 'equity' in c.lower() and 'total' in c.lower()), None)
            shares_col = next((c for c in bs.columns if 'share' in c.lower() and 'outstanding' in c.lower()), None)

            if equity_col is None or shares_col is None:
                print("missing equity/shares")
                continue

            equity = bs[equity_col].astype(float).dropna()
            shares = bs[shares_col].astype(float).dropna()

            common_dates = equity.index.intersection(shares.index)
            if len(common_dates) < MIN_PERIODS:
                print(f"few years ({len(common_dates)})")
                continue

            bvps_series = equity[common_dates] / shares[common_dates]
            bvps_df = pd.DataFrame({'date': common_dates, 'bvps': bvps_series.values}).sort_values('date')

            # 2. Monthly prices
            daily_data, _ = ts.get_daily_adjusted(ticker, outputsize='full')
            time.sleep(15)

            daily_data.index = pd.to_datetime(daily_data.index)
            monthly_close = daily_data['5. adjusted close'].resample('MS').last().dropna()
            prices = pd.DataFrame({
                'date': monthly_close.index,
                'close': monthly_close.values
            }).reset_index(drop=True)

            if len(prices) < MIN_MONTHS_HISTORY:
                print("short price history")
                continue

            # 3. Avg P/B
            pb_ratios = []
            for _, row in bvps_df.iterrows():
                rep_date = row['date']
                closest = (prices['date'] - rep_date).abs().argmin()
                close_price = prices.loc[closest, 'close']
                if close_price > 0 and row['bvps'] > 0:
                    pb_ratios.append(close_price / row['bvps'])

            if len(pb_ratios) < MIN_PERIODS:
                print("few PB ratios")
                continue

            avg_pb = np.mean(pb_ratios)

            # 4. Merge + ffill
            monthly = pd.merge_asof(
                prices.sort_values('date'),
                bvps_df.sort_values('date'),
                on='date',
                direction='backward'
            )
            monthly['bvps'] = monthly['bvps'].ffill()
            monthly['fair_value'] = monthly['bvps'] * avg_pb
            monthly = monthly.dropna(subset=['bvps', 'fair_value', 'close']).set_index('date')

            if len(monthly) < MIN_MONTHS_HISTORY:
                print("few aligned months")
                continue

            # 5. Event detection
            monthly = monthly.sort_index()
            was_undervalued = False
            local_count = 0

            for i in range(1, len(monthly)):
                price = monthly['close'].iloc[i]
                fair = monthly['fair_value'].iloc[i]
                is_undervalued = price < fair

                if not was_undervalued and is_undervalued:
                    gap_pct = (fair - price) / fair * 100
                    all_gaps.append(gap_pct)

                    recovered = False
                    months_to = None
                    for j in range(i + 1, len(monthly)):
                        if monthly['close'].iloc[j] >= monthly['fair_value'].iloc[j]:
                            recovered = True
                            months_to = j - i
                            break

                    recovered_events.append(recovered)
                    if recovered:
                        recovery_months.append(months_to)

                    total_events += 1
                    local_count += 1

                was_undervalued = is_undervalued

            analyzed_stocks += 1
            print(f"✓ {local_count} events")

        except Exception as e:
            print(f"error: {str(e)[:70]}")
            continue

except KeyboardInterrupt:
    print("\nStopped — partial results")

# SUMMARY
print("\n" + "═" * 70)
print("UNDERVALUATION SUMMARY (Alpha Vantage)")
print("═" * 70)

if total_events == 0:
    print("No events detected.")
    print("Common fixes: check API key limits, try different tickers (AAPL/MSFT often work best).")
else:
    avg_gap = np.mean(all_gaps)
    rec_count = sum(recovered_events)
    pct_rec = (rec_count / total_events) * 100
    pct_fail = 100 - pct_rec
    avg_rec = np.mean(recovery_months) if recovery_months else 0

    print(f"Stocks analysed              : {analyzed_stocks}")
    print(f"Total events                 : {total_events}")
    print(f"Avg gap                      : {avg_gap:.1f}%")
    print(f"Recovered                    : {pct_rec:.1f}%")
    print(f"Never recovered              : {pct_fail:.1f}%")
    print(f"Avg recovery (months)        : {avg_rec:.1f}")

print("\nDone.")