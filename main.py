"""
Options Second-Order Greeks Analyzer — entry point.

Startup sequence (live mode):
  1. Authenticate via Schwab OAuth
  2. Fetch Treasury yield curve once (today + yesterday)
  3. Fetch SPY options chain + VIX concurrently
  4. Parse chain → DataFrame
  5. Launch Tkinter dashboard

Demo mode activates when SCHWAB_CLIENT_ID is not set.
"""

from dotenv import load_dotenv
load_dotenv()

import datetime
import numpy as np

from greeks  import parse_chain, STRIKE_PCT
import api   as api_mod
import macro as macro_mod
from ui.app  import launch_dashboard

DEMO_SPOT = 679.67

_SPY_OI_ANCHORS = {
    685: (28000, 4000), 690: (14000, 3000), 700: (10000, 2000),
    695: (5000,  1500), 680: (8000,  6000), 679: (3000,  3000),
    678: (2000,  2500), 675: (4000,  5000), 673: (3000,  6000),
    670: (2000, 18000), 668: (1000,  5000), 665: (1500, 12000),
    660: (1000, 14000), 655: (500,   4000),
}

RISK_FREE = 0.045
MAX_DTE   = 45


def generate_demo_chain(spot):
    from greeks import calc_gamma, calc_vanna, calc_charm
    import pandas as pd

    np.random.seed(42)
    rows  = []
    r     = RISK_FREE
    today = datetime.date.today()
    lo    = round(spot * (1 - STRIKE_PCT))
    hi    = round(spot * (1 + STRIKE_PCT))

    for dte in [0, 3, 7, 14, 21, 28, 45]:
        if dte > MAX_DTE: continue
        T        = max(dte, 0.5) / 365
        exp_date = (today + datetime.timedelta(days=dte)).strftime("%Y-%m-%d")
        dte_scale = {0: 1.8, 3: 1.4, 7: 1.2, 14: 1.0,
                     21: 0.8, 28: 0.6, 45: 0.4}.get(dte, 0.5)

        for K in np.arange(lo, hi + 1, 1.0):
            Kr = round(K)
            for side, call in [("call", True), ("put", False)]:
                mn    = np.log(K / spot)
                skew  = -0.45 * mn if not call else -0.12 * mn
                sigma = max(0.10, 0.20 + abs(mn) * 0.35 + skew
                            + np.random.normal(0, 0.006))
                if Kr in _SPY_OI_ANCHORS:
                    base = _SPY_OI_ANCHORS[Kr][0 if call else 1]
                else:
                    dist     = abs(K - spot)
                    decay    = max(0.03, 1.0 - (dist / (spot * STRIKE_PCT)) ** 1.6)
                    is_10    = (Kr % 10 == 0)
                    is_5     = (Kr % 5  == 0)
                    round_w  = 2.5 if is_10 else (1.4 if is_5 else 0.6)
                    side_bias = (1.0 if call else 1.6) if K < 673 else \
                                (1.6 if call else 1.0)
                    base = int(1200 * decay * round_w * side_bias)
                oi = max(0, int(np.random.poisson(max(10, base * dte_scale))))
                if oi < 10: continue
                try:
                    g  = calc_gamma(spot, K, T, r, sigma)
                    va = calc_vanna(spot, K, T, r, sigma)
                    ch = calc_charm(spot, K, T, r, sigma, call)
                except:
                    continue
                mult = oi * 100
                sign = 1 if call else -1
                rows.append({
                    "strike":       round(K, 2),
                    "type":         side,
                    "dte":          dte,
                    "expiry":       exp_date,
                    "oi":           oi,
                    "iv":           sigma,
                    "GEX_call":     g  * mult * spot if call     else 0,
                    "GEX_put":     -g  * mult * spot if not call else 0,
                    "VannEX":       sign * va * mult,
                    "VannEX_call":  va * mult         if call     else 0,
                    "VannEX_put":  -va * mult         if not call else 0,
                    "CharmEX":      sign * ch * mult,
                    "CharmEX_call": ch * mult         if call     else 0,
                    "CharmEX_put": -ch * mult         if not call else 0,
                })
    return pd.DataFrame(rows)


def main():
    import os, sys
    demo = "--demo" in sys.argv or (os.environ.get("SCHWAB_CLIENT_ID", "YOUR_CLIENT_ID") == "YOUR_CLIENT_ID")

    today_curve    = None
    yesterday_curve = None

    macro_quotes   = None
    futures_yields = None

    if demo:
        print("DEMO mode — add credentials to .env for live data\n")
        df           = generate_demo_chain(DEMO_SPOT)
        initial_data = (df, DEMO_SPOT)
        vix_data     = None
    else:
        from auth import get_valid_access_token
        token = get_valid_access_token()

        # Fetch Treasury yield curve once at startup
        print("Fetching Treasury yield curve...")
        today_curve, yesterday_curve = macro_mod.fetch_yield_curves()
        if today_curve:
            print(f"  Yield curve: {today_curve.get('date', '?')}  "
                  f"10Y={today_curve.get('y10')}%  30Y={today_curve.get('y30')}%")
        else:
            print("  Yield curve unavailable")

        # Fetch SPY chain + VIX concurrently
        print("Fetching SPY data...")
        raw_result, vix_data = api_mod.fetch_live_data(token)
        if not raw_result:
            print("No data loaded — check credentials and try again.")
            return
        chain_dict, spot = raw_result
        df = parse_chain(chain_dict)
        if df.empty:
            print("Chain parsed but empty — check credentials and try again.")
            return
        initial_data = (df, spot)
        print(f"  SPY ${spot:.2f} — {len(df)} rows")

        # Fetch macro quotes + futures yields at startup so MACRO tab is
        # immediately populated (otherwise blank until first 5-min refresh)
        print("Fetching macro data...")
        macro_quotes   = macro_mod.get_macro_quotes(token)
        futures_yields = macro_mod.get_futures_yields(token)
        if macro_quotes.get("tlt_price"):
            print(f"  TLT ${macro_quotes['tlt_price']:.2f}  "
                  f"VIX {macro_quotes.get('vix', '?')}")
        if futures_yields.get("zb_yield"):
            print(f"  2Y={futures_yields.get('zt_yield')}%  "
                  f"10Y={futures_yields.get('zn_yield')}%  "
                  f"30Y={futures_yields.get('zb_yield')}%")

    print("Launching dashboard...")
    launch_dashboard(
        initial_data,
        demo=demo,
        vix_data=vix_data,
        today_curve=today_curve,
        yesterday_curve=yesterday_curve,
        macro_quotes=macro_quotes,
        futures_yields=futures_yields,
    )


if __name__ == "__main__":
    main()
