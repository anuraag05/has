"""NSE Close-vs-Heikin-Ashi scan (Dhan data) - Streamlit app.

For every timeframe, flags UP when the regular candle close is above the
Heikin-Ashi close and DOWN when it is below.
"""
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from plotly.subplots import make_subplots

st.set_page_config(page_title="NSE Close vs Heikin-Ashi Scan", layout="wide")

API = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
NSE_LISTS = {
    "Nifty 50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "Nifty 100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "Nifty 500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
}
ALL_TFS = ["1m", "5m", "15m", "30m", "1H", "1D"]
IST = "Asia/Kolkata"


# ---------------------------------------------------------------- credentials
def get_creds():
    cid = st.secrets.get("DHAN_CLIENT_ID", "") if hasattr(st, "secrets") else ""
    tok = st.secrets.get("DHAN_ACCESS_TOKEN", "") if hasattr(st, "secrets") else ""
    with st.sidebar:
        st.header("Dhan credentials")
        if cid and tok:
            st.success("Credentials loaded from Streamlit secrets")
            with st.expander("Token expired? Use a new one for now"):
                new_tok = st.text_input("New access token (this session only)", type="password")
                if new_tok.strip():
                    tok = new_tok.strip()
                    st.caption("Using the pasted token. Update Secrets to make it permanent.")
        else:
            cid = st.text_input("Client ID", type="password").strip()
            tok = st.text_input("Access token", type="password").strip()
    return cid, tok


# ---------------------------------------------------------------- rate limiter
class Limiter:
    def __init__(self, per_sec=4.0):
        self.gap, self.last, self.lock = 1.0 / per_sec, 0.0, threading.Lock()

    def wait(self):
        with self.lock:
            now = time.monotonic()
            sleep = self.last + self.gap - now
            if sleep > 0:
                time.sleep(sleep)
            self.last = time.monotonic()


LIMITER = Limiter(2.5)  # Dhan data API allows 5 req/sec; stay well under it
ABORT = threading.Event()
THROTTLED = {"n": 0}
ERRS = {}
_ERR_LOCK = threading.Lock()


def note_err(msg):
    with _ERR_LOCK:
        ERRS[msg] = ERRS.get(msg, 0) + 1


# ---------------------------------------------------------------- universe
@st.cache_data(ttl=24 * 3600, show_spinner="Loading Dhan instrument master...")
def load_master():
    df = pd.read_csv(MASTER_URL, low_memory=False)
    df = df[(df["SEM_EXM_EXCH_ID"] == "NSE") & (df["SEM_SEGMENT"] == "E")]
    df = df[df["SEM_INSTRUMENT_NAME"] == "EQUITY"]
    if "SEM_SERIES" in df.columns:
        df = df[df["SEM_SERIES"] == "EQ"]
    out = df[["SEM_TRADING_SYMBOL", "SEM_SMST_SECURITY_ID"]].drop_duplicates("SEM_TRADING_SYMBOL")
    out.columns = ["symbol", "security_id"]
    return out.reset_index(drop=True)


@st.cache_data(ttl=24 * 3600)
def load_index_symbols(name):
    r = requests.get(NSE_LISTS[name], headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text))["Symbol"].str.strip().tolist()


# ---------------------------------------------------------------- data
def _to_df(js):
    if not js or "close" not in js or not js["close"]:
        return None
    ts = pd.Series(js["timestamp"], dtype="float64")
    if ts.iloc[0] < 946684800:  # some responses use a 1980 epoch
        ts = ts + 315513000
    idx = pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert(IST)
    df = pd.DataFrame(
        {k: js[k] for k in ("open", "high", "low", "close", "volume")}
    ).astype(float)
    df.index = idx
    return df


def _post(path, body, headers):
    for attempt in range(4):
        if ABORT.is_set():
            return None
        LIMITER.wait()
        try:
            r = requests.post(f"{API}{path}", json=body, headers=headers, timeout=25)
            if r.status_code == 200:
                THROTTLED["n"] = 0
                js = r.json()
                if not isinstance(js, dict) or not js.get("close"):
                    note_err(f"HTTP 200 but no candle data: {str(js)[:160]}")
                return js
            if r.status_code == 429:
                note_err("HTTP 429 DH-904 rate limit (backing off)")
                with _ERR_LOCK:
                    THROTTLED["n"] += 1
                    if THROTTLED["n"] >= 10:
                        ABORT.set()
                with LIMITER.lock:  # every worker waits behind this pause
                    LIMITER.last = max(LIMITER.last, time.monotonic() + 5 * (attempt + 1))
                continue
            note_err(f"HTTP {r.status_code}: {r.text[:200]}")
            return None
        except (requests.RequestException, ValueError) as e:
            note_err(f"Request error: {e}")
            time.sleep(1)
    return None


def fetch_raw(headers, sec_id, kind):
    """kind 'D' = daily, '5' = 5-minute, '1' = 1-minute candles."""
    now = datetime.now()
    base = {"securityId": str(sec_id), "exchangeSegment": "NSE_EQ", "instrument": "EQUITY"}
    if kind == "D":
        body = {**base, "expiryCode": 0, "oi": False,
                "fromDate": (now - timedelta(days=150)).strftime("%Y-%m-%d"),
                "toDate": (now + timedelta(days=1)).strftime("%Y-%m-%d")}
        return _to_df(_post("/charts/historical", body, headers))
    days = 20 if kind == "5" else 5
    body = {**base, "interval": kind, "oi": False,
            "fromDate": (now - timedelta(days=days)).strftime("%Y-%m-%d 09:15:00"),
            "toDate": now.strftime("%Y-%m-%d 15:30:00")}
    return _to_df(_post("/charts/intraday", body, headers))


def resample(df, rule):
    # sessions start 09:15, so shift bin edges by 15 minutes
    return df.resample(rule, offset="15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()


def get_frames(headers, sec_id, tfs=tuple(ALL_TFS)):
    """Up to 3 API calls per stock (daily, 5m, 1m); 15m, 30m and 1H are built from the 5m data."""
    out = {t: None for t in ALL_TFS}
    if "1D" in tfs:
        out["1D"] = fetch_raw(headers, sec_id, "D")
    if any(t in tfs for t in ("5m", "15m", "30m", "1H")):
        m5 = fetch_raw(headers, sec_id, "5")
        if m5 is not None:
            out["5m"] = m5
            out["15m"] = resample(m5, "15min")
            out["30m"] = resample(m5, "30min")
            out["1H"] = resample(m5, "60min")
    if "1m" in tfs:
        out["1m"] = fetch_raw(headers, sec_id, "1")
    return out


@st.cache_data(ttl=300, show_spinner="Fetching candles...")
def cached_frames(cid, tok, sid):
    h = {"access-token": tok, "client-id": cid, "Content-Type": "application/json",
         "Accept": "application/json"}
    return get_frames(h, sid)


def heikin_ashi(df):
    ha = pd.DataFrame(index=df.index)
    ha["close"] = (df.open + df.high + df.low + df.close) / 4
    opens = [(df.open.iloc[0] + df.close.iloc[0]) / 2]
    for i in range(1, len(df)):
        opens.append((opens[-1] + ha.close.iloc[i - 1]) / 2)
    ha["open"] = opens
    ha["high"] = pd.concat([df.high, ha.open, ha.close], axis=1).max(axis=1)
    ha["low"] = pd.concat([df.low, ha.open, ha.close], axis=1).min(axis=1)
    return ha


TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60}


def drop_forming(df, tf):
    """Remove the last candle if it has not closed yet."""
    if df is None or df.empty:
        return df
    now = pd.Timestamp.now(tz=IST)
    day_close = df.index[-1].normalize() + pd.Timedelta(hours=15, minutes=30)
    end = day_close if tf == "1D" else min(df.index[-1] + pd.Timedelta(minutes=TF_MIN[tf]), day_close)
    return df.iloc[:-1] if end > now else df


def signal(df):
    """UP when regular close > Heikin-Ashi close, DOWN when below."""
    if df is None or len(df) < 3:
        return "n/a"
    diff = df.close.iloc[-1] - heikin_ashi(df).close.iloc[-1]
    return "Up" if diff > 0 else "Down" if diff < 0 else "Flat"


def scan_one(headers, symbol, sec_id, tfs):
    frames = get_frames(headers, sec_id, tfs)
    row = {"Symbol": symbol}
    for tf in tfs:
        row[tf] = signal(frames[tf])
    sigs = [row[tf] for tf in tfs]
    row["All TFs"] = "ALL UP" if all(x == "Up" for x in sigs) else "ALL DOWN" if all(x == "Down" for x in sigs) else ""
    return row


# ---------------------------------------------------------------- UI
st.title("NSE Close vs Heikin-Ashi Scan (Dhan)")
st.caption(
    "Latest candle per timeframe. **Up** (BUY signal) = regular close is above the Heikin-Ashi close, "
    "**Down** (SELL signal) = regular close is below it. 15m, 30m and 1H are built from 5m data."
)

client_id, token = get_creds()
headers = {"access-token": token, "client-id": client_id, "Content-Type": "application/json",
           "Accept": "application/json"}

with st.sidebar:
    universe = st.selectbox("Universe", ["Nifty 50", "Nifty 100", "Nifty 500", "All NSE EQ", "Custom"])
    custom = st.text_area("Custom symbols (comma separated)", "RELIANCE, TCS, INFY") if universe == "Custom" else ""
    limit = st.number_input("Max stocks (0 = no limit)", 0, 3000, 0)
    sel_tfs = st.multiselect("Timeframes", ALL_TFS, default=ALL_TFS)
    st.caption("Each stock needs 1 call for 1D, 1 for 5m/15m/30m/1H, and 1 for 1m. Untick 1m to scan faster.")
    workers = st.slider("Parallel workers", 1, 4, 2)
    run = st.button("Run scan", type="primary", disabled=not (client_id and token and sel_tfs))
    st.caption("Dhan allows 5 data requests/sec. Start with Nifty 50/100; all NSE takes hours.")
    if st.button("Test connection", disabled=not (client_id and token)):
        try:
            sid0 = load_master().set_index("symbol").security_id.get("RELIANCE")
            body = {"securityId": str(sid0), "exchangeSegment": "NSE_EQ", "instrument": "EQUITY",
                    "expiryCode": 0, "oi": False,
                    "fromDate": (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d"),
                    "toDate": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")}
            r = requests.post(f"{API}/charts/historical", json=body, headers=headers, timeout=25)
            st.code(f"HTTP {r.status_code}\n{r.text[:500]}")
        except Exception as e:
            st.code(f"Failed: {e}")

if run:
    master = load_master()
    if universe == "All NSE EQ":
        syms = master.symbol.tolist()
    elif universe == "Custom":
        syms = [s.strip().upper() for s in custom.split(",") if s.strip()]
    else:
        syms = load_index_symbols(universe)
    m = master.set_index("symbol").security_id
    syms = [s for s in syms if s in m.index]
    if limit:
        syms = syms[: int(limit)]

    tfs = [t for t in ALL_TFS if t in sel_tfs]
    ERRS.clear()
    ABORT.clear()
    THROTTLED["n"] = 0
    rows, bar = [], st.progress(0.0, text="Scanning...")
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = [ex.submit(scan_one, headers, s_, m[s_], tfs) for s_ in syms]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                rows.append(f.result())
            except Exception:
                pass
            bar.progress(i / len(futs), text=f"Scanned {i}/{len(futs)}")
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    bar.empty()
    if ABORT.is_set():
        st.error("Dhan kept returning 429 (rate limit), so the scan was stopped. Wait a few minutes, "
                 "then rerun with a smaller universe or 1 worker.")
    st.session_state["res"] = pd.DataFrame(rows).sort_values("Symbol").reset_index(drop=True)
    st.session_state["tfs"] = tfs
    st.session_state["errs"] = dict(ERRS)
    st.session_state["scanned_at"] = datetime.now().strftime("%d %b %Y %H:%M")

if "res" in st.session_state:
    res = st.session_state["res"]
    TFS = st.session_state.get("tfs", ALL_TFS)
    st.subheader(f"Results - {len(res)} stocks (scanned {st.session_state['scanned_at']})")

    ok = res[TFS].ne("n/a").all(axis=1)
    if (~ok).any():
        st.warning(f"{(~ok).sum()} stocks had missing data on at least one timeframe (shown as n/a).")
        errs = st.session_state.get("errs", {})
        if errs:
            st.error("Dhan API problems seen during the scan (most common first):")
            for k, v in sorted(errs.items(), key=lambda kv: -kv[1])[:5]:
                st.code(f"{v}x  {k}")

    c1, c2, c3 = st.columns(3)
    pick = c1.multiselect("Timeframes to filter on", TFS)
    kind = c2.selectbox("Signal", ["Any", "Up", "Down"])
    allt = c3.selectbox("Aligned on all timeframes", ["Any", "ALL UP", "ALL DOWN"])
    view = res
    for t in pick:
        view = view[view[t].isin(["Up", "Down"])] if kind == "Any" else view[view[t] == kind]
    if allt != "Any":
        view = view[view["All TFs"] == allt]

    def color(v):
        if v in ("Up", "ALL UP"):
            return "background-color:#c8f7c5;color:#064"
        if v in ("Down", "ALL DOWN"):
            return "background-color:#f9c9c9;color:#800"
        return ""

    st.dataframe(view.style.map(color), use_container_width=True, height=420)
    st.download_button("Download CSV", view.to_csv(index=False), "close_vs_ha_scan.csv", "text/csv")

    fig = go.Figure()
    for sig, col in [("Up", "#2ca02c"), ("Down", "#d62728")]:
        fig.add_bar(x=TFS, y=[(res[t] == sig).sum() for t in TFS], name=sig, marker_color=col)
    fig.update_layout(barmode="group", title="Up vs Down count by timeframe", height=350)
    st.plotly_chart(fig, use_container_width=True)

if "res" not in st.session_state:
    st.info("Choose a universe in the sidebar and press **Run scan**.")

# ---------------------------------------------------------------- charts (independent of scan)
st.divider()
st.header("Charts: regular vs Heikin-Ashi (any stock)")
master = load_master()
scanned = st.session_state["res"].Symbol.tolist() if "res" in st.session_state else []
all_syms = scanned + [s_ for s_ in master.symbol if s_ not in set(scanned)]
k1, k2 = st.columns([2, 1])
sym = k1.selectbox("Stock", all_syms)
bars = k2.slider("Candles to show", 30, 200, 100)
sid = master.set_index("symbol").security_id.get(sym)

if not (client_id and token):
    st.info("Enter Dhan credentials to load charts.")
else:
    frames = cached_frames(client_id, token, sid)
    for tab, tf in zip(st.tabs(ALL_TFS), ALL_TFS):
        with tab:
            df = frames.get(tf)
            if df is None or df.empty:
                st.warning("No data returned. Press 'Test connection' in the sidebar to see Dhan's error.")
                continue
            d, h = df.tail(bars), heikin_ashi(df).tail(bars)
            x = d.index.strftime("%d-%b %H:%M")
            diff = d.close - h.close
            st.markdown(f"**Latest: {'Up' if diff.iloc[-1] > 0 else 'Down' if diff.iloc[-1] < 0 else 'Flat'}** "
                        f"(close {d.close.iloc[-1]:.2f} vs HA close {h.close.iloc[-1]:.2f})")
            f = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.05,
                              row_heights=[0.4, 0.4, 0.2],
                              subplot_titles=("Regular candles", "Heikin-Ashi candles",
                                              "Close minus HA close (green = Up, red = Down)"))
            f.add_trace(go.Candlestick(x=x, open=d.open, high=d.high, low=d.low, close=d.close), 1, 1)
            f.add_trace(go.Candlestick(x=x, open=h.open, high=h.high, low=h.low, close=h.close), 2, 1)
            f.add_trace(go.Bar(x=x, y=diff, marker_color=["#2ca02c" if v > 0 else "#d62728" for v in diff]), 3, 1)
            f.update_xaxes(type="category", nticks=12, rangeslider_visible=False)
            f.update_layout(height=800, showlegend=False, title=f"{sym} - {tf}")
            st.plotly_chart(f, use_container_width=True)


# ---------------------------------------------------------------- live monitor
st.divider()
st.header("Live monitor (works only while this page stays open)")
st.caption("Up = BUY signal, Down = SELL signal, shown when a closed candle flips. For alerts with the "
           "page closed, run alert_worker.py (see README).")
lm1, lm2, lm3 = st.columns([3, 1, 1])
mon_syms = lm1.text_input("Symbols (comma separated)", "RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK")
mon_tf = lm2.selectbox("Timeframe", ["5m", "15m", "30m", "1H", "1D", "1m"], index=1)
mon_on = lm3.toggle("Monitor on")


@st.fragment(run_every=30 if mon_on else None)
def monitor():
    if not mon_on:
        st.info("Switch **Monitor on** to start. It re-checks every 30 seconds.")
        return
    if not (client_id and token):
        st.warning("Enter Dhan credentials first.")
        return
    mst = st.session_state.setdefault("mon_state", {})
    mlog = st.session_state.setdefault("mon_log", [])
    ids = load_master().set_index("symbol").security_id
    rows = []
    for s_ in [x.strip().upper() for x in mon_syms.split(",") if x.strip()]:
        if s_ not in ids.index:
            continue
        df = drop_forming(get_frames(headers, ids[s_], [mon_tf])[mon_tf], mon_tf)
        sig = signal(df)
        if sig == "n/a":
            rows.append({"Symbol": s_, "Signal": "n/a", "Candle": ""})
            continue
        t = df.index[-1].isoformat()
        key = f"{s_}|{mon_tf}"
        prev = mst.get(key)
        if prev and prev["t"] != t and prev["sig"] != sig and sig in ("Up", "Down"):
            action = "BUY" if sig == "Up" else "SELL"
            msg = f"{action} - {s_} flipped {prev['sig']} -> {sig} on {mon_tf} close"
            mlog.insert(0, {"Time": datetime.now().strftime("%H:%M:%S"), "Alert": msg})
            st.toast(msg)
        mst[key] = {"sig": sig, "t": t}
        rows.append({"Symbol": s_, "Signal": {"Up": "Up (BUY)", "Down": "Down (SELL)"}.get(sig, sig),
                     "Candle": df.index[-1].strftime("%d-%b %H:%M")})
    st.dataframe(pd.DataFrame(rows), use_container_width=True)
    if mlog:
        st.subheader("Alerts this session")
        st.dataframe(pd.DataFrame(mlog[:50]), use_container_width=True)


monitor()
