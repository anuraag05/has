"""F&O helpers: NSE index / F&O-stock underlyings and their ATM call (CE) and put (PE).

Built from Dhan's compact instrument master. The ATM strike is the listed strike closest to
the spot price, taken from the nearest expiry that is still open.
"""
import re

import pandas as pd

MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
# Dhan security ids of the NSE indices on segment IDX_I
INDEX_IDS = {"NIFTY": 13, "BANKNIFTY": 25, "FINNIFTY": 27, "MIDCPNIFTY": 442}
EQ = ("NSE_EQ", "EQUITY")
IDX = ("IDX_I", "INDEX")
_UND = re.compile(r"^(.*?)-\d{0,2}[A-Za-z]{3}\d{4}-")  # NIFTY-Nov2026-24000-CE -> NIFTY


def seg_of(sec):
    """Normalise an id to (security_id, exchangeSegment, instrument). A bare id means NSE equity."""
    if isinstance(sec, (tuple, list)):
        return str(sec[0]), sec[1], sec[2]
    return str(sec), EQ[0], EQ[1]


def load_fno(master=None):
    """Returns (chain, eq_ids): the option chain table and a symbol -> equity security id map."""
    df = master if master is not None else pd.read_csv(MASTER_URL, low_memory=False)
    nse = df[df["SEM_EXM_EXCH_ID"] == "NSE"]
    eq = nse[(nse["SEM_SEGMENT"] == "E") & (nse["SEM_INSTRUMENT_NAME"] == "EQUITY")]
    if "SEM_SERIES" in eq.columns:
        eq = eq[eq["SEM_SERIES"] == "EQ"]
    eq_ids = eq.drop_duplicates("SEM_TRADING_SYMBOL").set_index("SEM_TRADING_SYMBOL")["SEM_SMST_SECURITY_ID"].to_dict()

    d = nse[(nse["SEM_SEGMENT"] == "D") & nse["SEM_OPTION_TYPE"].isin(["CE", "PE"])
            & nse["SEM_INSTRUMENT_NAME"].isin(["OPTIDX", "OPTSTK"])].copy()
    d["und"] = d["SEM_TRADING_SYMBOL"].astype(str).str.extract(_UND)[0]
    d["exp"] = pd.to_datetime(d["SEM_EXPIRY_DATE"], errors="coerce").dt.normalize()
    d["strike"] = pd.to_numeric(d["SEM_STRIKE_PRICE"], errors="coerce")
    d = d.dropna(subset=["und", "exp", "strike"])
    d["wk"] = d["SEM_EXPIRY_FLAG"].astype(str).str.upper().eq("W") if "SEM_EXPIRY_FLAG" in d.columns else False
    chain = d.rename(columns={"SEM_INSTRUMENT_NAME": "kind", "SEM_SMST_SECURITY_ID": "sid",
                              "SEM_TRADING_SYMBOL": "name", "SEM_LOT_UNITS": "lot",
                              "SEM_OPTION_TYPE": "cp"})[["und", "kind", "sid", "name", "lot", "exp", "strike", "cp", "wk"]]
    return chain.reset_index(drop=True), eq_ids


def underlyings(chain, eq_ids, which="all"):
    """{symbol: (security_id, segment, instrument)} for F&O underlyings: index, stocks or all."""
    out = {}
    if which in ("index", "all"):
        for u in sorted(chain[chain.kind == "OPTIDX"].und.unique()):
            if u in INDEX_IDS:
                out[u] = (INDEX_IDS[u], *IDX)
    if which in ("stocks", "all"):
        for u in sorted(chain[chain.kind == "OPTSTK"].und.unique()):
            if u in eq_ids:
                out[u] = (eq_ids[u], *EQ)
    return out


def atm(chain, und, spot, today=None, min_days=0):
    """ATM call and put for an underlying at a spot price, or None if no chain/contract."""
    c = chain[chain.und == und]
    if c.empty or spot is None or pd.isna(spot):
        return None
    today = pd.Timestamp(today or pd.Timestamp.now(tz="Asia/Kolkata").date()).normalize()
    all_exps = sorted(c.exp.unique())
    exps = [e for e in all_exps if e >= today + pd.Timedelta(days=min_days)]
    months = pd.Series(all_exps).dt.to_period("M")
    # an index has a weekly series if Dhan flags a weekly expiry or a month has more than one expiry
    # stock options never have weeklies, so they always take the monthly path
    has_weekly = c.kind.iloc[0] == "OPTIDX" and (bool(c.wk.any()) or bool(months.duplicated().any()))
    if not has_weekly:
        # no weekly series (stocks, BANKNIFTY, FINNIFTY, MIDCPNIFTY ...): last expiry of each month
        last = {}
        for e in exps:
            last[(e.year, e.month)] = e
        exps = sorted(last.values())
    if not exps:
        return None
    sub = c[c.exp == exps[0]]
    strikes = sub.strike.unique()
    k = strikes[abs(strikes - spot).argmin()]
    ce = sub[(sub.strike == k) & (sub.cp == "CE")]
    pe = sub[(sub.strike == k) & (sub.cp == "PE")]
    if ce.empty or pe.empty:
        return None
    ce, pe = ce.iloc[0], pe.iloc[0]
    return {"strike": float(k), "expiry": exps[0], "lot": int(ce.lot) if pd.notna(ce.lot) else None,
            "ce": ce["name"], "pe": pe["name"], "ce_id": str(ce.sid), "pe_id": str(pe.sid)}


def option_action(sig, mode="buy"):
    """Up = bullish, Down = bearish. mode 'buy': Up -> BUY CE, Down -> BUY PE.
    mode 'sell': Up -> SELL PE, Down -> SELL CE. Returns (action text, 'ce'|'pe')."""
    if sig == "Up":
        return ("BUY CALL (CE)", "ce") if mode == "buy" else ("SELL PUT (PE)", "pe")
    if sig == "Down":
        return ("BUY PUT (PE)", "pe") if mode == "buy" else ("SELL CALL (CE)", "ce")
    return None, None


def describe(a, sig, mode="buy"):
    """One-line description of the ATM contract to act on for a signal."""
    act, leg = option_action(sig, mode)
    if a is None or act is None:
        return ""
    return (f"{act}: {a[leg]} | ATM {a['strike']:g} | expiry {a['expiry']:%d-%b-%Y} | lot {a['lot']}")


def expiry_table(chain, und, today=None):
    """Open expiries of an underlying: type (weekly/monthly), number of strikes, strike range."""
    c = chain[chain.und == und]
    today = pd.Timestamp(today or pd.Timestamp.now(tz="Asia/Kolkata").date()).normalize()
    c = c[c.exp >= today]
    if c.empty:
        return pd.DataFrame(columns=["Expiry", "Type", "Strikes", "Min strike", "Max strike", "Lot"])
    month_last = c.groupby(c.exp.dt.to_period("M")).exp.transform("max")
    rows = []
    for e, g in c.groupby("exp"):
        monthly = (month_last[g.index[0]] == e)
        rows.append({"Expiry": e.date(), "Type": "Monthly" if monthly else "Weekly",
                     "Strikes": g.strike.nunique(), "Min strike": g.strike.min(), "Max strike": g.strike.max(),
                     "Lot": int(g.lot.iloc[0]) if pd.notna(g.lot.iloc[0]) else None})
    return pd.DataFrame(rows)


def contracts(chain, und, expiries, spot, n=5, kinds=("CE", "PE")):
    """Contracts for the given expiries on the ATM strike +/- n listed strikes (CE and/or PE)."""
    c = chain[(chain.und == und) & chain.exp.isin(pd.to_datetime(list(expiries)))]
    out = []
    for e, g in c.groupby("exp"):
        strikes = sorted(g.strike.unique())
        if not strikes:
            continue
        i = min(range(len(strikes)), key=lambda j: abs(strikes[j] - spot))
        for j in range(max(0, i - n), min(len(strikes), i + n + 1)):
            k = strikes[j]
            for cp in kinds:
                r = g[(g.strike == k) & (g.cp == cp)]
                if r.empty:
                    continue
                r = r.iloc[0]
                money = "ATM" if j == i else (("ITM" if k < spot else "OTM") if cp == "CE" else ("ITM" if k > spot else "OTM"))
                out.append({"Contract": r["name"], "Type": cp, "Strike": float(k), "Moneyness": money,
                            "Expiry": e.date(), "Lot": int(r.lot) if pd.notna(r.lot) else None,
                            "seg": (str(r.sid), "NSE_FNO", r.kind)})
    return pd.DataFrame(out)
