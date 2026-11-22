#!/usr/bin/env python3
"""Fetch drivers for MCX gold/silver/copper and Nifty futures -> site/data.json.

No logins. Every source is wrapped so one failure never kills the run: a failed
source keeps its previous value from the old data.json and is marked stale.

Usage:  python fetch_data.py            # live
        python fetch_data.py --demo     # synthetic data, for testing the page only
"""
import bisect
import csv
import datetime as dt
import io
import json
import math
import pathlib
import random
import sys
import time

import requests

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "site" / "data.json"
CFG = json.loads((ROOT / "config.json").read_text())
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
NOW = dt.datetime.now(IST)
DEMO = "--demo" in sys.argv
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
OZ_G = 31.1034768
LB_KG = 0.45359237

try:
    PREV = json.loads(OUT.read_text())
except Exception:
    PREV = {}
if PREV.get("demo"):  # never reuse synthetic numbers as "last known" real values
    PREV = {}
STATUS = {}


# ---------- helpers ----------
def summarize(pts, label):
    pts = [[d, float(c)] for d, c in pts]
    last, prev = pts[-1][1], pts[-2][1]
    return {"label": label, "last": round(last, 4), "prev": round(prev, 4),
            "chg": round(last - prev, 4), "pct": round((last / prev - 1) * 100, 3) if prev else 0,
            "asof": pts[-1][0], "series": pts[-45:], "stale": False}


def guarded(name, fn, prev_val):
    """Run fn(); on failure return the previous value marked stale."""
    try:
        v = fn()
        STATUS[name] = {"ok": True}
        return v
    except Exception as e:  # noqa
        STATUS[name] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
        if isinstance(prev_val, dict) and prev_val:
            prev_val = dict(prev_val)
            prev_val["stale"] = True
        return prev_val


def lookup(m, keys, d):
    i = bisect.bisect_right(keys, d)
    return m[keys[i - 1]] if i else None


# ---------- Yahoo ----------
def yahoo(sym):
    last_err = None
    for host in ("query1", "query2"):
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sym}",
                             params={"range": "3mo", "interval": "1d"}, headers=UA, timeout=20)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            off = res["meta"].get("gmtoffset", 0)
            dd = {}
            for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
                if c is not None:
                    dd[dt.datetime.fromtimestamp(t + off, dt.timezone.utc).strftime("%Y-%m-%d")] = round(c, 4)
            pts = sorted(dd.items())
            if len(pts) < 7:
                raise ValueError("too few points")
            return pts
        except Exception as e:  # noqa
            last_err = e
    raise last_err


# ---------- FRED (public CSV, no key) ----------
def fred(sid):
    since = (NOW - dt.timedelta(days=120)).strftime("%Y-%m-%d")
    r = requests.get("https://fred.stlouisfed.org/graph/fredgraph.csv",
                     params={"id": sid, "cosd": since}, headers=UA, timeout=25)
    r.raise_for_status()
    rows = list(csv.reader(io.StringIO(r.text)))[1:]
    pts = [[d, float(v)] for d, v in rows if v not in (".", "")]
    if len(pts) < 7:
        raise ValueError("too few points")
    return pts


# ---------- US Treasury (backup for FRED real10y, no key) ----------
def treasury_real10y():
    since = (NOW - dt.timedelta(days=120)).date()
    pts = []
    for yr in sorted({since.year, NOW.year}):
        r = requests.get("https://home.treasury.gov/resource-center/data-chart-center/"
                         "interest-rates/daily-treasury-rates.csv/%d/all" % yr,
                         params={"type": "daily_treasury_real_yield_curve",
                                 "field_tdr_date_value": yr, "page": "", "_format": "csv"},
                         headers=UA, timeout=25)
        r.raise_for_status()
        for row in csv.DictReader(io.StringIO(r.text)):
            v = row.get("10 YR")
            if not v:
                continue
            d = dt.datetime.strptime(row["Date"], "%m/%d/%Y").date()
            if d >= since:
                pts.append([d.strftime("%Y-%m-%d"), float(v)])
    pts.sort()
    if len(pts) < 7:
        raise ValueError("too few points")
    return pts


def fred_real10y():
    try:
        return fred("DFII10")
    except Exception:
        return treasury_real10y()


# ---------- NSE (best effort, often blocked from cloud IPs) ----------
def nse_session():
    s = requests.Session()
    s.headers.update({**UA, "Accept": "application/json,text/plain,*/*",
                      "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.nseindia.com/"})
    s.get("https://www.nseindia.com", timeout=20)
    return s


def num(x):
    return float(str(x).replace(",", ""))


def nse_fiidii(s):
    j = s.get("https://www.nseindia.com/api/fiidiiTradeReact", timeout=20).json()
    out = {}
    for row in j:
        key = "fii" if "FII" in row["category"].upper() else "dii"
        out[key] = {"buy": num(row["buyValue"]), "sell": num(row["sellValue"]), "net": num(row["netValue"])}
        out["date"] = row["date"]
    if "fii" not in out or "dii" not in out:
        raise ValueError("unexpected FII/DII payload")
    return out


def nse_chain_retry(tries=3, delay=4):
    """Fresh session + warm-up on every attempt: NSE often blocks the first hit from a cloud IP."""
    last = None
    for i in range(tries):
        try:
            return nse_chain(nse_session())
        except Exception as e:  # noqa
            last = e
            if i < tries - 1:
                time.sleep(delay * (i + 1))
    raise last


def nse_chain(s):
    j = s.get("https://www.nseindia.com/api/option-chain-indices", params={"symbol": "NIFTY"}, timeout=25).json()
    rec = j["records"]
    exp = rec["expiryDates"][0]
    ce, pe = {}, {}
    for d in rec["data"]:
        if d["expiryDate"] != exp:
            continue
        k = d["strikePrice"]
        if "CE" in d:
            ce[k] = d["CE"]["openInterest"]
        if "PE" in d:
            pe[k] = d["PE"]["openInterest"]
    if not ce or not pe:
        raise ValueError("empty option chain")
    strikes = sorted(set(ce) | set(pe))
    pain = {k: sum(ce.get(x, 0) * max(0, k - x) + pe.get(x, 0) * max(0, x - k) for x in strikes) for k in strikes}
    top = lambda m: [k for k, _ in sorted(m.items(), key=lambda kv: -kv[1])[:3]]
    return {"expiry": exp, "spot": rec["underlyingValue"], "pcr": round(sum(pe.values()) / sum(ce.values()), 3),
            "max_pain": min(pain, key=pain.get), "resistance": top(ce), "support": top(pe)}


def nse_fii_fut():
    for back in range(0, 8):
        d = NOW - dt.timedelta(days=back)
        r = requests.get(f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{d:%d%m%Y}.csv",
                         headers=UA, timeout=20)
        if r.status_code != 200:
            continue
        rows = [[c.strip() for c in row] for row in csv.reader(io.StringIO(r.text))]
        hdr = next(i for i, row in enumerate(rows) if row and row[0].lower() == "client type")
        h = rows[hdr]
        fii = next(row for row in rows[hdr + 1:] if row and row[0].upper() == "FII")
        lg, sh = num(fii[h.index("Future Index Long")]), num(fii[h.index("Future Index Short")])
        return {"date": d.strftime("%Y-%m-%d"), "long": lg, "short": sh, "net": lg - sh,
                "long_pct": round(lg / (lg + sh) * 100, 2)}
    raise ValueError("no participant OI file in last 8 days")


# ---------- implied MCX fair value ----------
def implied(comm, usd, fn, duty, premium):
    um = {d: c for d, c in usd["series"]}
    keys = sorted(um)
    pts = []
    for d, c in comm["series"]:
        u = lookup(um, keys, d)
        if u:
            pts.append([d, round(fn(c, u) * (1 + duty) + premium, 2)])
    return pts


# ---------- tilt engine ----------
ASSETS = ["gold", "silver", "copper", "nifty"]
# driver: (weights for [gold, silver, copper, nifty] when driver RISES, threshold)
WEIGHTS = {
    "dxy": ([-2, -2, -1, -1], 0.4), "usdinr": ([1, 1, 1, -1], 0.3), "real10y": ([-2, -1, -1, -1], 0.05),
    "usvix": ([1, 0, -1, -1], 5.0), "indiavix": ([0, 0, 0, -2], 5.0), "brent": ([0, 0, 1, -1], 2.0),
    "copper": ([0, 1, 2, 1], 1.5), "es": ([0, 0, 1, 1], 0.7), "usdcny": ([0, -1, -2, -1], 0.3),
    "fii_flow": ([0, 0, 0, 2], 1000),
}
LABEL = {"dxy": "Dollar index", "usdinr": "USD/INR", "real10y": "US real yield", "usvix": "US VIX",
         "indiavix": "India VIX", "brent": "Brent", "copper": "Copper", "es": "US futures",
         "usdcny": "Yuan weakening", "fii_flow": "FII flows"}


def five_day(series, pct=True):
    if len(series) < 6:
        return 0.0
    a, b = series[-6][1], series[-1][1]
    return (b / a - 1) * 100 if pct else b - a


def tilt(market, fredd, nse):
    trends = {}
    for k in ("dxy", "usdinr", "usvix", "indiavix", "brent", "copper", "es", "usdcny"):
        if k in market and market[k].get("series"):
            trends[k] = five_day(market[k]["series"])
    if "real10y" in fredd and fredd["real10y"].get("series"):
        trends["real10y"] = five_day(fredd["real10y"]["series"], pct=False)
    if isinstance(nse.get("fii_dii"), dict) and "fii" in nse["fii_dii"]:
        trends["fii_flow"] = nse["fii_dii"]["fii"]["net"]
    out = {}
    for ai, a in enumerate(ASSETS):
        score, parts = 0, []
        for k, v in trends.items():
            w, thr = WEIGHTS[k]
            t = 1 if v > thr else -1 if v < -thr else 0
            if t and w[ai]:
                score += t * w[ai]
                parts.append({"driver": LABEL[k], "effect": t * w[ai], "chg": round(v, 2)})
        parts.sort(key=lambda p: -abs(p["effect"]))
        out[a] = {"score": score, "label": "Bullish" if score >= 3 else "Bearish" if score <= -3 else "Mixed",
                  "drivers": parts[:5]}
    return out


# ---------- demo data ----------
def demo_series(base, vol, seed):
    rnd = random.Random(seed)
    v, pts = base, []
    day = NOW - dt.timedelta(days=64)
    for _ in range(45):
        while day.weekday() > 4:
            day += dt.timedelta(days=1)
        v *= 1 + rnd.gauss(0, vol)
        pts.append([day.strftime("%Y-%m-%d"), round(v, 4)])
        day += dt.timedelta(days=1)
    return pts


DEMO_BASE = {"nifty": 24500, "banknifty": 53000, "indiavix": 14, "es": 6200, "nq": 22000, "nikkei": 38000,
             "hangseng": 24000, "gold": 3400, "silver": 38, "copper": 4.6, "brent": 68, "dxy": 99, "usdinr": 86,
             "usdcny": 7.2, "us10y": 4.3, "usvix": 17, "real10y": 1.9, "fedfunds": 4.3, "curve": 0.5}


# ---------- main ----------
def main():
    market, fredd, nse = {}, {}, {}
    for k, (label, sym) in CFG["yahoo"].items():
        if DEMO:
            market[k] = summarize(demo_series(DEMO_BASE[k], 0.008, hash(k) % 1000), label)
            STATUS[f"yahoo:{k}"] = {"ok": True}
        else:
            market[k] = guarded(f"yahoo:{k}", lambda sym=sym, label=label: summarize(yahoo(sym), label),
                                PREV.get("market", {}).get(k))
    for k, (label, sid) in CFG["fred"].items():
        if DEMO:
            fredd[k] = summarize(demo_series(DEMO_BASE[k], 0.01, hash(k) % 1000), label)
            STATUS[f"fred:{k}"] = {"ok": True}
        else:
            fetch = fred_real10y if k == "real10y" else (lambda sid=sid: fred(sid))
            fredd[k] = guarded(f"fred:{k}", lambda fetch=fetch, label=label: summarize(fetch(), label),
                               PREV.get("fred", {}).get(k))
    market = {k: v for k, v in market.items() if v}
    fredd = {k: v for k, v in fredd.items() if v}

    if DEMO:
        nse = {"fii_dii": {"date": "demo", "fii": {"buy": 9000, "sell": 10800, "net": -1800},
                           "dii": {"buy": 11000, "sell": 8800, "net": 2200}},
               "chain": {"expiry": "demo", "spot": 24500, "pcr": 1.08, "max_pain": 24500,
                         "resistance": [24800, 25000, 24600], "support": [24200, 24000, 24400]},
               "fii_fut": {"date": "demo", "long": 70000, "short": 130000, "net": -60000, "long_pct": 35.0}}
        for k in ("nse:fii_dii", "nse:chain", "nse:fii_fut"):
            STATUS[k] = {"ok": True}
    else:
        sess = None
        try:
            sess = nse_session()
        except Exception as e:  # noqa
            STATUS["nse:fii_dii"] = {"ok": False, "error": f"session: {type(e).__name__}"}
        pn = PREV.get("nse", {})
        def stale(v):
            return dict(v, stale=True) if isinstance(v, dict) else v
        nse["fii_dii"] = guarded("nse:fii_dii", lambda: nse_fiidii(sess), pn.get("fii_dii")) if sess else stale(pn.get("fii_dii"))
        nse["chain"] = guarded("nse:chain", nse_chain_retry, pn.get("chain"))
        nse["fii_fut"] = guarded("nse:fii_fut", nse_fii_fut, pn.get("fii_fut"))
        nse = {k: v for k, v in nse.items() if v}

    mcx = {}
    if "usdinr" in market:
        d, pr = CFG["duty"], CFG["premium_inr"]
        specs = {
            "gold": ("MCX Gold fair value", "₹/10g", lambda c, u: c / OZ_G * 10 * u, d["gold"], pr["gold_per_10g"]),
            "silver": ("MCX Silver fair value", "₹/kg", lambda c, u: c / OZ_G * 1000 * u, d["silver"], pr["silver_per_kg"]),
            "copper": ("MCX Copper fair value", "₹/kg", lambda c, u: c / LB_KG * u, d["copper"], pr["copper_per_kg"]),
        }
        for k, (label, unit, fn, duty, prem) in specs.items():
            if k in market:
                pts = implied(market[k], market["usdinr"], fn, duty, prem)
                if len(pts) >= 7:
                    mcx[k] = dict(summarize(pts, label), unit=unit, duty=duty,
                                  stale=bool(market[k].get("stale") or market["usdinr"].get("stale")))
    if "gold" in market and "silver" in market:
        gm = dict(market["gold"]["series"]); sm = dict(market["silver"]["series"])
        pts = [[d, round(gm[d] / sm[d], 3)] for d in sorted(gm) if d in sm and sm[d]]
        if len(pts) >= 7:
            mcx["gs_ratio"] = summarize(pts, "Gold/Silver ratio")

    data = {"generated": NOW.isoformat(timespec="seconds"), "demo": DEMO, "market": market, "fred": fredd,
            "nse": nse, "mcx": mcx, "tilt": tilt(market, fredd, nse), "status": STATUS,
            "notes": {"duty": CFG["duty"], "mcx_note": "Fair value = COMEX x USD/INR x (1+duty) + premium. "
                      "Not the traded MCX price; GST excluded."}}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, separators=(",", ":")))
    bad = [k for k, v in STATUS.items() if not v["ok"]]
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes); {len(STATUS) - len(bad)}/{len(STATUS)} sources ok")
    for k in bad:
        print("  FAILED", k, STATUS[k]["error"])
    # fail the job only if nothing at all was fetched and there is no old data to serve
    if len(bad) == len(STATUS) and not PREV:
        sys.exit(1)


if __name__ == "__main__":
    main()
