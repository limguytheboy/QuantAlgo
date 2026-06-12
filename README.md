# 📈 Quantitative Trading & Algorithmic Strategy Sandbox

A data-driven development space dedicated to designing, optimizing, and backtesting quantitative trading models. This repository hosts algorithmic strategy implementations tested across crypto and equity markets using a locally deployed backtesting architecture.

---

## 📂 Project Focus & Core Philosophy

### 1. Market Data Ingestion & Asset Classes
* **Cryptocurrency ($SOL):** Focused heavily on Solana ($SOL) market mechanics, parsing high-fidelity historical and live market structures fetched through the Coinbase API.
* **Large-Cap Equities (Big 100):** Integrated the `yfinance` library to dynamically pull raw historical financial data for top 100 large-cap US equities to model stock market patterns.
* **Multi-Timeframe & Asset Optimization:** Built automated optimization loops testing historical intervals spanning from high-frequency 1-minute candles for scalping up to 4-hour horizons to capture macro trend-following movements.

### 2. Strategy Ideation & Fundamental Approach
* **The "Less is More" Metric Insight:** Through extensive historical backtesting, I discovered that overloading a script with excessive technical indicators degrades signal reliability and causes overfitting. 
* **Core Indicators:** Pivoted the architecture away from heavy indicator noise, focusing on market fundamentals and structural price action—specifically **Volume-Weighted Analysis** and **Trend Break & Retest** setups to capture genuine market direction.
* **Performance Metrics:** Evaluated every strategy variation based on strict institutional risk management rules, actively monitoring win-rates, maximum drawdown (MDD), and Risk-Reward (R:R) Ratios to confirm true mathematical edge.

### 3. AI-Assisted Advanced Quantitative Experiments
* **LLM Collaboration Pipeline:** Effectively leveraged advanced AI agents (Claude) to bridge high-level quantitative finance mathematics with executable, asynchronous code logic.
* **Advanced Mathematical Models:** Prompt-engineered and implemented structural codebase experiments evaluating professional algorithmic concepts, including:
  * **Kalman Filters:** For dynamic price smoothing and noise reduction.
  * **Stochastic Modeling:** Exploring Brownian Motion and Markov Chains to predict probability paths and mean reversion tendencies.

---

## 🛠️ Infrastructure & Tech Stack
* **Backtesting Engine:** QuantConnect LEAN Engine (Locally deployed via CLI to bypass cloud limitations and run unrestricted free testing)
* **Languages:** Python, C#
* **Libraries & APIs:** `yfinance`, Coinbase API, NumPy, Pandas

---
*For a comprehensive high-level breakdown of my full portfolio, interactive game mechanics, and full-stack web applications, please visit my main profile: [github.com/limguytheboy](https://github.com/limguytheboy)*
