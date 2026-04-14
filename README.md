# Binance Futures Pair Pattern Scanner

Scans all Binance USDT-M perpetual futures for two consecutive candle pair patterns.

## Patterns

**Pattern 1 — Bull→Bear breakdown**
- C1 bullish, C2 bearish: `C2.high < C1.high`, `C2.close < C1.low`
- C3 bullish: `C3.high < C1.high`
- C4 bearish: `C4.high < C3.high`, `C4.close < C3.low`

**Pattern 2 — Bear→Bull breakout**
- C1 bearish, C2 bullish: `C2.low > C1.low`, `C2.close > C1.high`
- C3 bearish: `C3.low > C1.low`
- C4 bullish: `C4.low > C3.low`, `C4.close > C3.high`

All 4 candles are consecutive with zero gap between pairs.

## Deploy on Streamlit Cloud

1. Fork or push this repo to your GitHub account
2. Go to [share.streamlit.io](https://share.streamlit.io)
3. Click **New app**
4. Select your repo, branch `main`, file `app.py`
5. Click **Deploy**

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```
