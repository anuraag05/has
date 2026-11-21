"""Live Close-vs-Heikin-Ashi alert worker (Dhan -> Telegram / email).

At every candle close of the chosen timeframes it checks each watchlist stock and
sends an alert when the regular close is above (UP = BUY signal) or below (DOWN = SELL
signal) the Heikin-Ashi close. It only alerts; it never places orders. Configuration comes from environment variables or a .env file (see .env.example).

Run:   python alert_worker.py          (keep it running)
Test:  python alert_worker.py --test   (sends a test message and fetches one stock)
"""
import io
import json
import os
import smtplib
import sys
import time
from email.message import EmailMessage

import pandas as pd
import requests

import fno

IST = "Asia/Kolkata"
API = "https://api.dhan.co/v2"
MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
NSE_LISTS = {
    "NIFTY50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "NIFTY100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "NIFTY500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
}
TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1H": 60}
ALL_TFS = ["1m", "5m", "15m", "30m", "1H", "1D"]


# ---------------------------------------------------------------- config
def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
env = lambda k, d="": os.environ.get(k, d).strip()  # noqa: E731
CID, TOKEN = env("DHAN_CLIENT_ID"), env("DHAN_ACCESS_TOKEN")
TFS = [t for t in (x.strip() for x in env("TIMEFRAMES", "5m,15m,30m,1H,1D").split(",")) if t in ALL_TFS]
WATCH = env("WATCHLIST", "NIFTY50")
# FNO_INDEX | FNO_STOCKS | FNO_ALL as WATCHLIST also adds the ATM call/put to every alert
OPTION_MODE = env("OPTION_MODE", "buy").lower()  # buy: Up=BUY CE, Down=BUY PE | sell: Up=SELL PE, Down=SELL CE
OPTION_MIN_DAYS = int(env("OPTION_MIN_DAYS", "0") or 0)  # skip expiries closer than this many days
TRIGGER = env("TRIGGER", "flip").lower()  # flip | up | down | every
DELAY = int(env("CLOSE_DELAY_SEC", "8") or 8)  # wait after a close so Dhan has the candle
STATE_FILE = env("STATE_FILE", "state.json")
TG_TOKEN, TG_CHAT = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
SMTP_HOST, SMTP_PORT = env("SMTP_HOST"), int(env("SMTP_PORT", "465") or 465)
SMTP_USER, SMTP_PASS, EMAIL_TO = env("SMTP_USER"), env("SMTP_PASS"), env("EMAIL_TO")
HEADERS = {"access-token": TOKEN, "client-id": CID, "Content-Type": "application/json",
           "Accept": "application/json"}


def log(msg):
    print(f"{pd.Timestamp.now(tz=IST):%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------------------------------------------- notifications
def notify(text):
    log("ALERT\n" + text)
    if TG_TOKEN and TG_CHAT:
        for i in range(0, len(text), 3800):
            try:
                r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                                  json={"chat_id": TG_CHAT, "text": text[i:i + 3800]}, timeout=15)
                if r.status_code != 200:
                    log(f"Telegram error {r.status_code}: {r.text[:150]}")
            except requests.RequestException as e:
                log(f"Telegram failed: {e}")
    if SMTP_HOST and EMAIL_TO:
        try:
            msg = EmailMessage()
            msg["Subject"] = text.splitlines()[0][:120]
            msg["From"], msg["To"] = SMTP_USER or EMAIL_TO, EMAIL_TO
            msg.set_content(text)
            if SMTP_PORT == 465:
                srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20)
            else:
                srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
                srv.starttls()
            with srv:
                if SMTP_USER:
                    srv.login(SMTP_USER, SMTP_PASS)
                srv.send_message(msg)
        except Exception as e:  # noqa: BLE001
            log(f"Email failed: {e}")


_token_alert_at = 0.0


def token_problem(detail):
    global _token_alert_at
    log(f"Dhan auth problem: {detail[:200]}")
    if time.time() - _token_alert_at > 6 * 3600:
        _token_alert_at = time.time()
        notify("WARNING: Dhan rejected the access token (expired or invalid). "
               "Generate a new token and update DHAN_ACCESS_TOKEN, then restart the worker.\n" + detail[:200])


# ---------------------------------------------------------------- Dhan data
_last_call = 0.0
GAP = 0.4  # ~2.5 requests/sec, well under Dhan's 5/sec data limit


def post(path, body):
    global _last_call
    for attempt in range(4):
        wait = GAP - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()
        try:
            r = requests.post(f"{API}{path}", json=body, headers=HEADERS, timeout=25)
        except requests.RequestException as e:
            log(f"request error: {e}")
            time.sleep(2)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            log("429 rate limit, backing off")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 401 or "DH-901" in r.text:
            token_problem(r.text)
            return None
        log(f"HTTP {r.status_code}: {r.text[:160]}")
        return None
    return None


def to_df(js):
    if not js or not js.get("close"):
        return None
    ts = pd.Series(js["timestamp"], dtype="float64")
    if ts.iloc[0] < 946684800:  # some responses use a 1980 epoch
        ts = ts + 315513000
    idx = pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert(IST)
    df = pd.DataFrame({k: js[k] for k in ("open", "high", "low", "close", "volume")}).astype(float)
    df.index = idx
    return df


def fetch_raw(sec_id, kind):
    now = pd.Timestamp.now(tz=IST)
    sid, seg, inst = fno.seg_of(sec_id)
    base = {"securityId": sid, "exchangeSegment": seg, "instrument": inst}
    if kind == "D":
        body = {**base, "expiryCode": 0, "oi": False,
                "fromDate": (now - pd.Timedelta(days=120)).strftime("%Y-%m-%d"),
                "toDate": (now + pd.Timedelta(days=1)).strftime("%Y-%m-%d")}
        return to_df(post("/charts/historical", body))
    days = 4 if kind == "1" else 10  # calendar days; enough sessions for Heikin-Ashi to settle
    body = {**base, "interval": kind, "oi": False,
            "fromDate": (now - pd.Timedelta(days=days)).strftime("%Y-%m-%d 09:15:00"),
            "toDate": now.strftime("%Y-%m-%d 15:30:00")}
    return to_df(post("/charts/intraday", body))


def resample(df, rule):
    return df.resample(rule, offset="15min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()


def get_frames(sec_id, tfs):
    """One call each for 1D, 5m (also builds 15m/30m/1H) and 1m, only for what is needed."""
    out = {}
    if "1D" in tfs:
        out["1D"] = fetch_raw(sec_id, "D")
    if any(t in tfs for t in ("5m", "15m", "30m", "1H")):
        m5 = fetch_raw(sec_id, "5")
        if m5 is not None:
            out["5m"] = m5
            out["15m"], out["30m"], out["1H"] = (resample(m5, r) for r in ("15min", "30min", "60min"))
    if "1m" in tfs:
        out["1m"] = fetch_raw(sec_id, "1")
    return out


def heikin_ashi(df):
    close = (df.open + df.high + df.low + df.close) / 4
    opens = [(df.open.iloc[0] + df.close.iloc[0]) / 2]
    for i in range(1, len(df)):
        opens.append((opens[-1] + close.iloc[i - 1]) / 2)
    return close, pd.Series(opens, index=df.index)


# ---------------------------------------------------------------- candle timing
def session_close(ts):
    return ts.normalize() + pd.Timedelta(hours=15, minutes=30)


def candle_end(start, tf):
    if tf == "1D":
        return session_close(start)
    return min(start + pd.Timedelta(minutes=TF_MIN[tf]), session_close(start))


def boundaries(tf, day):
    """All candle-close times for one trading day (IST)."""
    open_, close = day + pd.Timedelta(hours=9, minutes=15), day + pd.Timedelta(hours=15, minutes=30)
    if tf == "1D":
        return [close]
    out, t = [], open_ + pd.Timedelta(minutes=TF_MIN[tf])
    while t < close:
        out.append(t)
        t += pd.Timedelta(minutes=TF_MIN[tf])
    return out + [close]


def latest_due(tf, now):
    delay = pd.Timedelta(seconds=max(DELAY, 90) if tf == "1D" else DELAY)
    ok = [b for b in boundaries(tf, now.normalize()) if b + delay <= now]
    return ok[-1] if ok else None


def evaluate(df, tf, b):
    """Signal for the candle that closed exactly at b, or None if Dhan does not have it yet."""
    if df is None or len(df) < 5:
        return None
    keep = pd.Series([candle_end(i, tf) <= b for i in df.index], index=df.index)
    df = df[keep]
    if len(df) < 5 or candle_end(df.index[-1], tf) != b:
        return None
    ha_close, _ = heikin_ashi(df)
    c, h = df.close.iloc[-1], ha_close.iloc[-1]
    return ("Up" if c > h else "Down" if c < h else "Flat"), c, h


def should_alert(prev, sig):
    if sig == "Flat":
        return False
    if TRIGGER == "every":
        return True
    if prev is None or prev == sig:  # first observation only records the state
        return False
    return TRIGGER == "flip" or (TRIGGER == "up" and sig == "Up") or (TRIGGER == "down" and sig == "Down")


# ---------------------------------------------------------------- universe
CHAIN = None


def load_watchlist():
    global CHAIN
    master = pd.read_csv(MASTER_URL, low_memory=False)
    CHAIN, eq_ids = fno.load_fno(master)
    log(f"Option chain loaded: {CHAIN.und.nunique()} underlyings, {len(CHAIN)} contracts")
    key = WATCH.upper()
    if key in ("FNO_INDEX", "FNO_STOCKS", "FNO_ALL"):
        out = fno.underlyings(CHAIN, eq_ids, {"FNO_INDEX": "index", "FNO_STOCKS": "stocks", "FNO_ALL": "all"}[key])
        if not out:
            sys.exit("No F&O underlyings found in the Dhan master. Run python alert_worker.py --test.")
        return out
    if key in NSE_LISTS:
        r = requests.get(NSE_LISTS[key], headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        r.raise_for_status()
        syms = pd.read_csv(io.StringIO(r.text))["Symbol"].str.strip().tolist()
    else:
        syms = [x.strip().upper() for x in WATCH.split(",") if x.strip()]
    idx = fno.underlyings(CHAIN, eq_ids, "index")
    out, missing = {}, []
    for x in syms:
        if x in idx:
            out[x] = idx[x]
        elif x in eq_ids:
            out[x] = (eq_ids[x], *fno.EQ)
        else:
            missing.append(x)
    if missing:
        log(f"Not found on Dhan NSE, skipped: {', '.join(missing)}")
    return out


def option_line(sym, spot, sig):
    """ATM contract line for an alert, only for F&O underlyings."""
    if CHAIN is None:
        return ""
    a = fno.atm(CHAIN, sym, spot, min_days=OPTION_MIN_DAYS)
    return ("\n   -> " + fno.describe(a, sig, OPTION_MODE)) if a else ""


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        json.dump(state, open(STATE_FILE, "w"))
    except OSError as e:
        log(f"could not save state: {e}")


# ---------------------------------------------------------------- main loop
def run():
    if not (CID and TOKEN):
        sys.exit("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN (environment or .env).")
    if not TFS:
        sys.exit("TIMEFRAMES has no valid entries. Use any of: " + ", ".join(ALL_TFS))
    watch = load_watchlist()
    state, done, todo = load_state(), {}, {}
    n_fno = sum(1 for x in watch if x in set(CHAIN.und))
    notify(f"Alert worker started: {len(watch)} symbols ({n_fno} with ATM options), timeframes {', '.join(TFS)}, trigger '{TRIGGER}'.")

    while True:
        now = pd.Timestamp.now(tz=IST)
        if now.weekday() >= 5 or not (9 * 60 + 14 <= now.hour * 60 + now.minute <= 16 * 60 + 5):
            time.sleep(30)
            continue

        for tf in TFS:  # schedule newly closed candles
            b = latest_due(tf, now)
            if b is not None and done.get(tf) != b:
                done[tf] = b
                todo[tf] = {"b": b, "left": set(watch), "deadline": time.time() + (1800 if tf == "1D" else 90)}

        if todo:
            alerts = {tf: {"Up": [], "Down": []} for tf in todo}
            for sym in list(watch):
                need = [tf for tf in todo if sym in todo[tf]["left"]]
                if not need:
                    continue
                frames = get_frames(watch[sym], need)
                for tf in need:
                    res = evaluate(frames.get(tf), tf, todo[tf]["b"])
                    if res is None:
                        continue
                    sig, c, h = res
                    todo[tf]["left"].discard(sym)
                    key = f"{sym}|{tf}"
                    prev = state.get(key, {}).get("sig")
                    if should_alert(prev, sig):
                        alerts[tf][sig].append(f"{sym}: close {c:.2f} {'>' if sig == 'Up' else '<'} HA {h:.2f}"
                                               f" (was {prev or 'n/a'})" + option_line(sym, c, sig))
                    state[key] = {"sig": sig, "t": todo[tf]["b"].isoformat()}
            save_state(state)

            for tf in list(todo):
                b = todo[tf]["b"]
                for sig, mark in (("Up", "BUY signal (UP)"), ("Down", "SELL signal (DOWN)")):
                    if alerts[tf][sig]:
                        notify(f"{mark} | {tf} close {b:%d-%b %H:%M}\n" + "\n".join(alerts[tf][sig]))
                if not todo[tf]["left"]:
                    del todo[tf]
                elif time.time() > todo[tf]["deadline"]:
                    log(f"{tf} {b:%H:%M}: no candle yet for {len(todo[tf]['left'])} stocks, giving up")
                    del todo[tf]
        time.sleep(3)


def self_test():
    notify("Test alert from alert_worker.py: notifications are working.")
    watch = load_watchlist()
    sym, sid = next(iter(watch.items()))
    for u in [x for x in ("NIFTY", "BANKNIFTY") if x in watch][:1] + list(watch)[:1]:
        spot = 24000.0 if u in ("NIFTY", "BANKNIFTY") else 1000.0
        df0 = fetch_raw(watch[u], "D")
        spot = float(df0.close.iloc[-1]) if df0 is not None else spot
        for sg in ("Up", "Down"):
            log(f"{u} spot {spot:.2f} {sg}: {option_line(u, spot, sg).strip() or 'no option chain'}")
    now = pd.Timestamp.now(tz=IST)
    df = fetch_raw(sid, "D")
    log(f"Dhan daily fetch for {sym}: {'OK, ' + str(len(df)) + ' candles' if df is not None else 'FAILED'}")
    if df is not None:
        b = candle_end(df.index[-1], "1D")
        log(f"Latest daily candle closes {b:%d-%b %H:%M}; signal: {evaluate(df, '1D', b)}; now {now:%H:%M}")


if __name__ == "__main__":
    try:
        self_test() if "--test" in sys.argv else run()
    except KeyboardInterrupt:
        log("stopped")
