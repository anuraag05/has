# NSE Close vs Heikin-Ashi Scan (Dhan + Streamlit)

For every NSE stock and timeframe (1m, 5m, 15m, 30m, 1H, 1D) shows **Up** when the
regular candle close is above the Heikin-Ashi close and **Down** when it is below,
with summary and per-stock charts (regular candles, Heikin-Ashi candles, and the difference).

## Deploy on Streamlit Community Cloud
1. Push this repo to GitHub.
2. share.streamlit.io -> New app -> this repo, branch `main`, file `app.py`.
3. Advanced settings -> Secrets:
   ```toml
   DHAN_CLIENT_ID = "your-client-id"
   DHAN_ACCESS_TOKEN = "your-access-token"
   ```
4. Update the token in Secrets when it expires (or paste a temporary one in the sidebar).

## Notes
- 15m, 30m and 1H candles are built from 5m data; each stock needs up to 3 Dhan calls.
- Dhan data API is limited to 5 requests/sec; the app stays at ~2.5/sec. Start with Nifty 50/100.

## Live alerts (BUY / SELL)
`alert_worker.py` runs continuously and, at every candle close of your chosen timeframes,
sends **BUY signal (UP)** when the regular close is above the Heikin-Ashi close and
**SELL signal (DOWN)** when it is below. By default it alerts once when a stock *flips*.
It only sends alerts; it never places orders.

1. `cp .env.example .env` and fill in your Dhan details, watchlist, timeframes, and Telegram and/or email.
2. `pip install -r requirements.txt`
3. `python alert_worker.py --test` (sends a test message, checks Dhan), then `python alert_worker.py`.

It must stay running (your PC, or an always-on host such as Railway, Render or a VM; start
command `python alert_worker.py`, set the same values as environment variables). Streamlit
Cloud and GitHub Actions cannot do exact close-time alerts. Update `DHAN_ACCESS_TOKEN` when it
expires; the worker sends you a warning when Dhan rejects the token.
The Streamlit page also has a **Live monitor** that alerts only while the page is open.
