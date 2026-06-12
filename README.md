# 📈 Quantitative Trading Backtest Sandbox

A personal sandbox repository for testing and validating various trading strategies. The main goal of this project is to take interesting ideas found on the internet or custom hypotheses and run them through historical data to see if they actually work.

---

## 📂 Project Focus & Setup

### 1. Backtesting Environment (Docker & LEAN)
* **Local LEAN Engine:** Deployed the QuantConnect LEAN engine locally using **Docker** and the LEAN CLI to run free, unrestricted backtests without cloud limitations.
* **Data Sources:** Connected to the Coinbase API to fetch historical market data for Solana ($SOL) and utilized `yfinance` to pull historical charts for large-cap stocks.

### 2. Strategy Testing & Optimization
* **Multi-Timeframe Testing:** Tested strategies across various intervals, ranging from 1-minute to 4-hour candles, to find the most optimal timeframe for each setup.
* **Parameter Optimization:** Ran optimization loops to tweak variables and find the best-performing combinations for specific market conditions.
* **Core Logic:** Focused primarily on testing fundamental concepts rather than cluttering scripts with too many indicators. Main areas of testing include **Volume-Weighted Analysis**, **Trend Break & Retest** setups, and Mean Reversion.
* **Basic Performance Metrics:** Evaluated results based on straightforward execution metrics, tracking the simple win-rate and Risk-Reward (R:R) Ratios.

### 3. AI-Assisted Scripting (Claude)
* Used AI coding assistants (Claude) to help write and refactor the backtesting scripts.
* Utilized AI to implement and experiment with more complex mathematical concepts found in quantitative trading—such as Kalman Filters, Brownian Motion, and Markov Chains—to see how they perform in historical simulations.

---

## 🛠️ Tech Stack
* **Engine/Infrastructure:** QuantConnect LEAN Engine, Docker, LEAN CLI
* **Languages:** Python, C#
* **Libraries & APIs:** `yfinance`, Coinbase API, Pandas, NumPy

---
*For a high-level overview of my other projects, including unity game prototypes and full-stack web apps, please visit my main profile: [github.com/limguytheboy](https://github.com/limguytheboy)*
