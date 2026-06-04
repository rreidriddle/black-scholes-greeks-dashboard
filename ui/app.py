"""ui/app.py — Tkinter dashboard: tab wiring, rendering, and refresh loop."""

import os
import time
import datetime
import threading
import warnings

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import tkinter as tk
from tkinter import ttk
from tkcalendar import DateEntry

warnings.filterwarnings("ignore")

from charts.common   import C
from charts.gex      import draw_gex
from charts.vanna_charm import draw_vanna, draw_charm
from charts.vol_smile   import draw_vol_smile
from charts.yield_curve import draw_yield_curve, draw_yield_data_table
from charts.price       import _draw_price_chart, _draw_volume_panel

from greeks import (
    STRIKE_PCT, parse_chain,
    calc_gex_regime, calc_gamma_flip,
)
import macro as macro_mod
import api as api_mod
from ui.state import _load_config, _save_config

SYMBOL           = "SPY"
REFRESH_INTERVAL = 300   # seconds

DTE_FILTERS   = {"0DTE": 0, "0-7": 7, "0-21": 21, "0-45": 45}
STRIKE_RANGES = {"+/-3%": 0.03, "+/-5%": 0.05, "+/-8%": 0.08}


# ── Data helpers ───────────────────────────────────────────────────────────────

def filter_df(df, spot, dte_label="0-45", expiry="ALL", strike_pct=0.05):
    out     = df.copy()
    max_dte = DTE_FILTERS.get(dte_label, 45)
    if dte_label == "0DTE":
        out = out[out["dte"] < 1]
    else:
        out = out[out["dte"] <= max_dte]
    if expiry != "ALL" and "expiry" in out.columns:
        out = out[out["expiry"] == expiry]
    out = out[((out["strike"] - spot).abs() / spot) <= strike_pct]
    return out


def build_smile_df(df, spot, expiry, target_dte=30.0):
    out = df.copy()
    if expiry != "ALL":
        out = out[out["expiry"] == expiry]
    else:
        # Pin to the single expiry closest to target_dte (front-month standard)
        unique_dtes = out["dte"].unique()
        best_dte    = unique_dtes[int(np.argmin(np.abs(unique_dtes - target_dte)))]
        out = out[out["dte"] == best_dte]
    if out.empty:
        return pd.DataFrame(), "N/A"
    resolved_expiry = out["expiry"].iloc[0] if "expiry" in out.columns else str(int(out["dte"].iloc[0])) + "DTE"
    unique_strikes = out["strike"].unique()
    dists          = np.abs(unique_strikes - spot)
    sorted_strikes = unique_strikes[np.argsort(dists)][:40]
    out            = out[out["strike"].isin(sorted_strikes)]
    pivot = (out.pivot_table(
                    index="strike",
                    columns="type",
                    values="iv",
                    aggfunc="mean")
               .reset_index()
               .rename(columns={"call": "call_iv", "put": "put_iv"})
               .sort_values("strike"))
    return pivot.dropna(subset=["call_iv", "put_iv"]), resolved_expiry


def _chart_agg(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse filtered per-row chain data to per-strike totals for chart functions.
    greeks.aggregate() groups by (strike, dte, bucket) for DB writes;
    chart functions need a single row per strike."""
    cols = ["GEX_call", "GEX_put",
            "VannEX", "VannEX_call", "VannEX_put",
            "CharmEX", "CharmEX_call", "CharmEX_put", "oi"]
    a = (df.groupby("strike")[cols]
           .sum()
           .reset_index()
           .sort_values("strike"))
    a["GEX_net"] = a["GEX_call"] + a["GEX_put"]
    return a


def _calc_max_pain(df: pd.DataFrame) -> float | None:
    if df.empty or "strike" not in df.columns:
        return None
    strikes = df["strike"].unique()
    if len(strikes) == 0:
        return None
    pain  = {}
    calls = df[df["type"] == "call"]
    puts  = df[df["type"] == "put"]
    for s in strikes:
        call_pain = ((s - calls["strike"]).clip(lower=0) * calls["oi"]).sum()
        put_pain  = ((puts["strike"] - s).clip(lower=0)  * puts["oi"]).sum()
        pain[s]   = call_pain + put_pain
    return float(min(pain, key=pain.get))


# ── Dashboard ──────────────────────────────────────────────────────────────────

def launch_dashboard(initial_data, demo=False, vix_data=None,
                     today_curve=None, yesterday_curve=None,
                     macro_quotes=None, futures_yields=None):
    """
    initial_data   : (df, spot) — parsed DataFrame + spot price
    today_curve    : dict from macro_mod.fetch_yield_curves() — fetched at startup
    yesterday_curve: dict | None
    macro_quotes   : dict from macro_mod.get_macro_quotes() — fetched at startup
    futures_yields : dict from macro_mod.get_futures_yields() — fetched at startup
    """
    root = tk.Tk()
    root.title("Options Greeks Analyzer")
    root.configure(bg=C["bg"])
    root.state("zoomed")

    sw  = root.winfo_screenwidth()
    sh  = root.winfo_screenheight()
    dpi = 96

    _data_lock  = threading.Lock()
    _live_data  = {"df": initial_data[0], "spot": initial_data[1]}
    _vix_state  = {"data": vix_data}
    _macro_state = {
        "macro":           macro_quotes,    # from macro_mod.get_macro_quotes
        "futures":         futures_yields,  # from macro_mod.get_futures_yields
        "today_curve":     today_curve,
        "yesterday_curve": yesterday_curve,
    }

    vanna_split = tk.BooleanVar(value=False)
    charm_split = tk.BooleanVar(value=False)
    dte_var     = tk.StringVar(value="0-45")
    expiry_var  = tk.StringVar(value="ALL")
    strike_var  = tk.StringVar(value="+/-5%")
    active_tab  = tk.StringVar(value="CHARTS")

    # ── Control bar ────────────────────────────────────────────────────────────

    ctrl = tk.Frame(root, bg=C["ctrl"], pady=7, padx=14)
    ctrl.pack(side="top", fill="x")

    tk.Label(ctrl, text="OPTIONS  GREEKS  ANALYZER",
             fg=C["text"], bg=C["ctrl"],
             font=("Courier New", 11, "bold")).pack(side="left", padx=(0, 20))

    def div():
        tk.Frame(ctrl, bg=C["border"], width=1).pack(
            side="left", fill="y", padx=10, pady=4)

    sty = ttk.Style()
    sty.theme_use("clam")
    sty.configure("D.TCombobox",
        fieldbackground=C["btn_off"], background=C["btn_off"],
        foreground=C["text"], selectbackground=C["btn_on"],
        selectforeground=C["text"], bordercolor=C["border"],
        arrowcolor=C["subtext"])

    def make_tab_btn(label):
        btn = tk.Button(ctrl, text=label,
                        bg=C["btn_on"] if label == "CHARTS" else C["btn_off"],
                        fg=C["text"],
                        activebackground=C["btn_on"],
                        activeforeground=C["text"],
                        relief="flat",
                        highlightbackground=C["border"],
                        highlightthickness=1,
                        font=("Courier New", 9, "bold"),
                        cursor="hand2", padx=14, pady=3)
        btn.pack(side="left", padx=(0, 4))
        return btn

    charts_btn   = make_tab_btn("CHARTS")
    macro_btn    = make_tab_btn("MACRO")
    backtest_btn = make_tab_btn("BACKTEST")
    div()

    tk.Label(ctrl, text="SYMBOL", fg=C["subtext"], bg=C["ctrl"],
             font=("Courier New", 7)).pack(side="left", padx=(0, 4))
    tk.Label(ctrl, text="SPY", fg=C["text"], bg=C["ctrl"],
             font=("Courier New", 10, "bold")).pack(side="left", padx=(0, 6))
    div()

    charts_ctrl_widgets = []

    def _lbl(text):
        w = tk.Label(ctrl, text=text, fg=C["subtext"], bg=C["ctrl"],
                     font=("Courier New", 7))
        w.pack(side="left", padx=(0, 4))
        charts_ctrl_widgets.append(w)
        return w

    def _div_c():
        w = tk.Frame(ctrl, bg=C["border"], width=1)
        w.pack(side="left", fill="y", padx=10, pady=4)
        charts_ctrl_widgets.append(w)
        return w

    _lbl("DTE")
    dte_cb = ttk.Combobox(ctrl, textvariable=dte_var,
                          values=list(DTE_FILTERS.keys()),
                          state="readonly", width=6,
                          style="D.TCombobox",
                          font=("Courier New", 10))
    dte_cb.pack(side="left", padx=(0, 6))
    charts_ctrl_widgets.append(dte_cb)
    _div_c()

    _lbl("EXPIRY")
    expiry_cb = ttk.Combobox(ctrl, textvariable=expiry_var,
                             values=["ALL"],
                             state="readonly", width=11,
                             style="D.TCombobox",
                             font=("Courier New", 10))
    expiry_cb.pack(side="left", padx=(0, 6))
    charts_ctrl_widgets.append(expiry_cb)
    _div_c()

    _lbl("RANGE")
    range_cb = ttk.Combobox(ctrl, textvariable=strike_var,
                            values=list(STRIKE_RANGES.keys()),
                            state="readonly", width=6,
                            style="D.TCombobox",
                            font=("Courier New", 10))
    range_cb.pack(side="left", padx=(0, 6))
    charts_ctrl_widgets.append(range_cb)
    _div_c()

    def make_toggle(label, var):
        lbl = tk.Label(ctrl, text=label, fg=C["subtext"], bg=C["ctrl"],
                       font=("Courier New", 7))
        lbl.pack(side="left", padx=(0, 4))
        charts_ctrl_widgets.append(lbl)
        dv  = tk.StringVar(value="NET")
        btn = tk.Button(ctrl, textvariable=dv,
                        bg=C["btn_off"], fg=C["text"],
                        activebackground=C["btn_on"],
                        activeforeground=C["text"],
                        relief="flat",
                        highlightbackground=C["border"],
                        highlightthickness=1,
                        font=("Courier New", 9, "bold"),
                        cursor="hand2", padx=14, pady=3)
        btn.pack(side="left", padx=(0, 4))
        charts_ctrl_widgets.append(btn)
        def toggle():
            var.set(not var.get())
            dv.set("SPLIT" if var.get() else "NET")
            btn.config(bg=C["btn_on"] if var.get() else C["btn_off"])
            render_charts()
        btn.config(command=toggle)

    make_toggle("VANNA", vanna_split)
    _div_c()
    make_toggle("CHARM", charm_split)
    _div_c()

    tk.Label(ctrl,
             text="LIVE" if not demo else "DEMO",
             fg=C["live"] if not demo else C["demo"], bg=C["ctrl"],
             font=("Courier New", 9, "bold")).pack(side="right", padx=(10, 4))

    tk.Label(ctrl, text="Ctrl+Scroll: Zoom  |  R: Reset",
             fg=C["subtext"], bg=C["ctrl"],
             font=("Courier New", 7)).pack(side="right", padx=10)

    _vix_str = {"text": ""}

    def _build_vix_str(vd):
        if vd is None:
            _vix_str["text"] = "VIX --.-"
            return
        arrow = "▲" if vd["change"] >= 0 else "▼"
        _vix_str["text"] = (
            f"VIX {vd['last']:.2f}  "
            f"{arrow} {abs(vd['change']):.2f} ({abs(vd['change_pct']):.1f}%)"
        )

    _build_vix_str(vix_data)

    if not demo:
        tk.Frame(ctrl, bg=C["border"], width=1).pack(
            side="right", fill="y", padx=10, pady=4)
        countdown_var = tk.StringVar(value="")
        tk.Label(ctrl, textvariable=countdown_var,
                 fg=C["subtext"], bg=C["ctrl"],
                 font=("Courier New", 8)).pack(side="right", padx=(0, 6))
        ind_label = tk.Label(ctrl, text="●",
                             fg=C["ind_green"], bg=C["ctrl"],
                             font=("Courier New", 14))
        ind_label.pack(side="right", padx=(0, 2))

        def _set_indicator(state):
            color = {"green": C["ind_green"],
                     "red":   C["ind_red"],
                     "yellow": C["ind_yellow"]}[state]
            ind_label.config(fg=color)

    tk.Frame(root, bg=C["border"], height=1).pack(fill="x")

    # ── Content area ───────────────────────────────────────────────────────────

    content = tk.Frame(root, bg=C["bg"])
    content.pack(fill="both", expand=True)

    # ── CHARTS TAB ─────────────────────────────────────────────────────────────

    charts_frame = tk.Frame(content, bg=C["bg"])
    charts_frame.place(relx=0, rely=0, relwidth=1, relheight=1)

    fig_w = sw / dpi
    fig_h = (sh - 52) / dpi
    fig   = plt.Figure(figsize=(fig_w, fig_h), facecolor=C["bg"], dpi=dpi)

    outer    = gridspec.GridSpec(1, 2, figure=fig,
                                 left=0.05, right=0.975,
                                 top=0.93, bottom=0.07,
                                 wspace=0.28, width_ratios=[1, 1.5])
    ax_gex   = fig.add_subplot(outer[0, 0])
    right    = outer[0, 1].subgridspec(2, 1, hspace=0.42)
    ax_vanna = fig.add_subplot(right[0, 0])
    ax_charm = fig.add_subplot(right[1, 0])

    charts_canvas = FigureCanvasTkAgg(fig, master=charts_frame)
    charts_canvas.get_tk_widget().pack(fill="both", expand=True)

    # ── MACRO TAB ──────────────────────────────────────────────────────────────

    macro_frame = tk.Frame(content, bg=C["bg"])

    badge_frame = tk.Frame(macro_frame, bg=C["ctrl"], pady=8, padx=14)
    badge_frame.pack(side="top", fill="x")

    regime_label = tk.Label(badge_frame, text="██ MACRO REGIME: —",
                            fg=C["subtext"], bg=C["ctrl"],
                            font=("Courier New", 13, "bold"))
    regime_label.pack(side="left", padx=(0, 20))

    reason_label = tk.Label(badge_frame, text="",
                            fg=C["subtext"], bg=C["ctrl"],
                            font=("Courier New", 9))
    reason_label.pack(side="left", padx=(0, 20))

    signal_label = tk.Label(badge_frame, text="",
                            fg=C["subtext"], bg=C["ctrl"],
                            font=("Courier New", 10, "bold"))
    signal_label.pack(side="right", padx=(20, 0))

    gex_regime_label = tk.Label(badge_frame, text="",
                                fg=C["subtext"], bg=C["ctrl"],
                                font=("Courier New", 9))
    gex_regime_label.pack(side="right", padx=(0, 10))

    tk.Frame(macro_frame, bg=C["border"], height=1).pack(fill="x")

    macro_fig_w = sw / dpi
    macro_fig_h = (sh - 100) / dpi
    macro_fig   = plt.Figure(figsize=(macro_fig_w, macro_fig_h),
                             facecolor=C["bg"], dpi=dpi)

    macro_gs = gridspec.GridSpec(
        3, 1, figure=macro_fig,
        left=0.07, right=0.97,
        top=0.96, bottom=0.06,
        hspace=0.35,
        height_ratios=[45, 15, 40],
    )
    ax_curve = macro_fig.add_subplot(macro_gs[0, 0])
    ax_table = macro_fig.add_subplot(macro_gs[1, 0])
    ax_smile = macro_fig.add_subplot(macro_gs[2, 0])

    macro_canvas     = FigureCanvasTkAgg(macro_fig, master=macro_frame)
    macro_expiry_var = tk.StringVar(value="ALL")
    macro_canvas.get_tk_widget().pack(fill="both", expand=True)

    # ── BACKTEST TAB ───────────────────────────────────────────────────────────

    backtest_frame = tk.Frame(content, bg=C["bg"])

    _cfg            = _load_config()
    _saved_date_str = _cfg.get("backtest_date", "")
    try:
        _init_date = datetime.date.fromisoformat(_saved_date_str)
    except (ValueError, TypeError):
        if not demo:
            import schwab_price
            _init_date = schwab_price.prev_trading_day(datetime.date.today())
        else:
            _init_date = datetime.date.today() - datetime.timedelta(days=1)

    bt_ctrl = tk.Frame(backtest_frame, bg=C["ctrl"], pady=6, padx=14)
    bt_ctrl.pack(side="top", fill="x")

    tk.Label(bt_ctrl, text="DATE", fg=C["subtext"], bg=C["ctrl"],
             font=("Courier New", 7)).pack(side="left", padx=(0, 4))

    bt_date_entry = DateEntry(
        bt_ctrl,
        date_pattern="mm/dd/yy",
        background=C["btn_off"], foreground=C["text"],
        bordercolor=C["border"],
        headersbackground=C["ctrl"], headersforeground=C["text"],
        selectbackground=C["btn_on"], selectforeground=C["text"],
        normalbackground=C["panel"], normalforeground=C["text"],
        weekendbackground=C["panel"], weekendforeground=C["subtext"],
        othermonthbackground=C["bg"], othermonthforeground=C["border"],
        font=("Courier New", 10), width=10, state="readonly",
    )
    bt_date_entry.set_date(_init_date)
    bt_date_entry.pack(side="left", padx=(0, 6))

    tk.Frame(bt_ctrl, bg=C["border"], width=1).pack(
        side="left", fill="y", padx=10, pady=4)

    tk.Label(bt_ctrl, text="GEX", fg=C["subtext"], bg=C["ctrl"],
             font=("Courier New", 7)).pack(side="left", padx=(0, 4))
    bt_dte_var = tk.StringVar(value="0-45")
    for lbl in ["0DTE", "0-45"]:
        tk.Radiobutton(
            bt_ctrl, text=lbl, variable=bt_dte_var, value=lbl,
            bg=C["ctrl"], fg=C["text"], selectcolor=C["btn_on"],
            activebackground=C["ctrl"], activeforeground=C["text"],
            font=("Courier New", 9), cursor="hand2",
            command=lambda: render_backtest(),
        ).pack(side="left", padx=(0, 2))

    tk.Frame(bt_ctrl, bg=C["border"], width=1).pack(
        side="left", fill="y", padx=10, pady=4)

    tk.Label(bt_ctrl, text="TIMEFRAME", fg=C["subtext"], bg=C["ctrl"],
             font=("Courier New", 7)).pack(side="left", padx=(0, 4))
    bt_tf_var  = tk.StringVar(value="5min")
    bt_tf_btns = {}
    for tf in ["1min", "5min", "10min", "15min", "30min"]:
        btn = tk.Button(
            bt_ctrl, text=tf,
            bg=C["btn_on"] if tf == "5min" else C["btn_off"],
            fg=C["text"],
            activebackground=C["btn_on"], activeforeground=C["text"],
            relief="flat", highlightbackground=C["border"],
            highlightthickness=1,
            font=("Courier New", 9, "bold"),
            cursor="hand2", padx=10, pady=3,
        )
        btn.pack(side="left", padx=(0, 2))
        bt_tf_btns[tf] = btn

    def _set_tf(tf):
        bt_tf_var.set(tf)
        for t, b in bt_tf_btns.items():
            b.config(bg=C["btn_on"] if t == tf else C["btn_off"])
        render_backtest()

    for tf, btn in bt_tf_btns.items():
        btn.config(command=lambda t=tf: _set_tf(t))

    tk.Frame(bt_ctrl, bg=C["border"], width=1).pack(
        side="left", fill="y", padx=10, pady=4)

    bt_status_var = tk.StringVar(value="")
    tk.Label(bt_ctrl, textvariable=bt_status_var,
             fg=C["subtext"], bg=C["ctrl"],
             font=("Courier New", 8)).pack(side="left", padx=(0, 6))

    bt_fig_w = sw / dpi
    bt_fig_h = (sh - 90) / dpi
    bt_fig   = plt.Figure(figsize=(bt_fig_w, bt_fig_h),
                          facecolor=C["bg"], dpi=dpi)

    bt_outer  = gridspec.GridSpec(1, 2, figure=bt_fig,
                                  left=0.05, right=0.975,
                                  top=0.93, bottom=0.07,
                                  wspace=0.28, width_ratios=[1, 1.5])
    ax_bt_gex = bt_fig.add_subplot(bt_outer[0, 0])
    right_gs  = bt_outer[0, 1].subgridspec(2, 1, hspace=0.0,
                                            height_ratios=[4, 1])
    ax_bt_price  = bt_fig.add_subplot(right_gs[0, 0])
    ax_bt_volume = bt_fig.add_subplot(right_gs[1, 0], sharex=ax_bt_price)

    bt_canvas = FigureCanvasTkAgg(bt_fig, master=backtest_frame)
    bt_canvas.get_tk_widget().pack(fill="both", expand=True)

    # ── Tooltip ────────────────────────────────────────────────────────────────

    tooltip     = tk.Label(root, text="", bg="#1a1a2e", fg=C["text"],
                           font=("Courier New", 8), relief="flat",
                           borderwidth=1, padx=8, pady=5, justify="left")
    _line_data  = {}
    _smile_data = {}

    def _store_line(ax, x_vals, y_vals):
        _line_data[id(ax)] = {"x": np.array(x_vals), "y": np.array(y_vals)}

    def _store_smile(ax, strikes, call_iv, put_iv):
        _smile_data[id(ax)] = {
            "x": np.array(strikes),
            "call_iv": np.array(call_iv),
            "put_iv":  np.array(put_iv),
        }

    def _fmt_tip(v):
        if abs(v) >= 1e6: return f"{v/1e6:+.3f}M"
        if abs(v) >= 1e3: return f"{v/1e3:+.1f}K"
        return f"{v:+.4f}"

    def on_hover_charts(event):
        if event.inaxes is None:
            tooltip.place_forget(); return
        dat = _line_data.get(id(event.inaxes))
        x   = event.xdata
        if x is None or dat is None or not len(dat["x"]):
            tooltip.place_forget(); return
        idx  = int(np.argmin(np.abs(dat["x"] - x)))
        text = (f"Strike : ${dat['x'][idx]:.0f}\n"
                f"Value  : {_fmt_tip(dat['y'][idx])}\n"
                f"Cursor : {_fmt_tip(event.ydata)}")
        tooltip.config(text=text)
        px = root.winfo_pointerx() - root.winfo_rootx()
        py = root.winfo_pointery() - root.winfo_rooty()
        tooltip.place(x=min(max(px + 15, 5), root.winfo_width() - 160),
                      y=min(max(py - 65,  5), root.winfo_height() - 80))
        tooltip.lift()

    def on_hover_smile(event):
        if event.inaxes is None:
            tooltip.place_forget(); return
        dat = _smile_data.get(id(event.inaxes))
        x   = event.xdata
        if x is None or dat is None or not len(dat["x"]):
            tooltip.place_forget(); return
        idx  = int(np.argmin(np.abs(dat["x"] - x)))
        text = (f"Strike  : ${dat['x'][idx]:.0f}\n"
                f"Call IV : {dat['call_iv'][idx]:.2f}%\n"
                f"Put IV  : {dat['put_iv'][idx]:.2f}%")
        tooltip.config(text=text)
        px = root.winfo_pointerx() - root.winfo_rootx()
        py = root.winfo_pointery() - root.winfo_rooty()
        tooltip.place(x=min(max(px + 15, 5), root.winfo_width() - 160),
                      y=min(max(py - 65,  5), root.winfo_height() - 80))
        tooltip.lift()

    fig.canvas.mpl_connect("motion_notify_event", on_hover_charts)
    macro_fig.canvas.mpl_connect("motion_notify_event", on_hover_smile)

    # ── Expiry list ────────────────────────────────────────────────────────────

    def _update_expiry_list(*_):
        with _data_lock:
            df   = _live_data["df"]
            spot = _live_data["spot"]
        filtered = filter_df(df, spot, dte_label=dte_var.get(),
                             expiry="ALL",
                             strike_pct=STRIKE_RANGES.get(strike_var.get(), 0.05))
        expiries = sorted(filtered["expiry"].unique().tolist()) \
                   if "expiry" in filtered.columns else []
        expiry_cb["values"] = ["ALL"] + expiries
        if expiry_var.get() not in ["ALL"] + expiries:
            expiry_var.set("ALL")
        macro_expiry_var.set("ALL")

    # ── RENDER: CHARTS ─────────────────────────────────────────────────────────

    def render_charts(*_):
        with _data_lock:
            df   = _live_data["df"]
            spot = _live_data["spot"]

        fdf = filter_df(df, spot,
                        dte_label=dte_var.get(),
                        expiry=expiry_var.get(),
                        strike_pct=STRIKE_RANGES.get(strike_var.get(), 0.05))

        for ax in [ax_gex, ax_vanna, ax_charm]:
            ax.clear()

        if fdf.empty:
            for ax in [ax_gex, ax_vanna, ax_charm]:
                ax.set_facecolor(C["panel"])
                ax.text(0.5, 0.5, "No data for selected filters",
                        transform=ax.transAxes, ha="center", va="center",
                        color=C["subtext"], fontsize=11)
            charts_canvas.draw()
            return

        agg      = _chart_agg(fdf)
        max_pain = _calc_max_pain(fdf)

        draw_gex(ax_gex, agg, spot, SYMBOL, max_pain=max_pain)

        nv = agg["VannEX"].values  / 1e6
        nc = agg["CharmEX"].values / 1e6
        _store_line(ax_vanna, agg["strike"].values, nv)
        _store_line(ax_charm, agg["strike"].values, nc)

        draw_vanna(ax_vanna, agg, spot, SYMBOL, vanna_split.get())
        draw_charm(ax_charm, agg, spot, SYMBOL, charm_split.get())

        expiry_lbl = f"  |  {expiry_var.get()}" if expiry_var.get() != "ALL" else ""
        filter_lbl = f"DTE: {dte_var.get()}  Range: {strike_var.get()}{expiry_lbl}"
        ts         = datetime.datetime.now().strftime("%H:%M:%S")

        fig.suptitle(
            f"SPY   Spot ${spot:.2f}      {_vix_str['text']}   "
            f"|   {ts}   |   {filter_lbl}",
            color=C["subtext"], fontsize=8.5,
            x=0.5, y=0.975, fontfamily="monospace"
        )
        charts_canvas.draw()
        fig.savefig("dashboard.png", dpi=dpi,
                    bbox_inches="tight", facecolor=C["bg"])

    # ── RENDER: MACRO ──────────────────────────────────────────────────────────

    def _update_regime_badge(macro_quotes, futures_yields, agg):
        tlt_price = macro_quotes.get("tlt_price") if macro_quotes else None
        zb_yield  = futures_yields.get("zb_yield") if futures_yields else None
        zt_yield  = futures_yields.get("zt_yield") if futures_yields else None
        zn_yield  = futures_yields.get("zn_yield") if futures_yields else None

        macro_regime = macro_mod.classify_macro_regime(tlt_price, zb_yield)
        reason       = macro_mod.build_regime_reason(tlt_price, zb_yield,
                                                     zt_yield, zn_yield)

        if agg is not None and not agg.empty:
            with _data_lock:
                spot = _live_data["spot"]
            gf         = calc_gamma_flip(agg)
            gex_regime = calc_gex_regime(agg, spot, gf)
        else:
            gex_regime = "—"

        signal = macro_mod.build_combined_signal(macro_regime, gex_regime)

        regime_color = {
            "GREEN":  C["regime_green"],
            "YELLOW": C["regime_yellow"],
            "ORANGE": C["regime_orange"],
            "RED":    C["regime_red"],
        }.get(macro_regime, C["subtext"])

        regime_label.config(
            text=f"██ MACRO REGIME: {macro_regime}", fg=regime_color)
        reason_label.config(text=reason, fg=C["subtext"])
        gex_regime_label.config(text=f"GEX: {gex_regime}", fg=C["subtext"])

        signal_color = (C["regime_red"]    if "SHORT" in signal or "CAUTION" in signal
                   else C["regime_green"]  if "LONG"  in signal
                   else C["regime_yellow"] if "NEUTRAL" in signal
                   else C["subtext"])
        signal_label.config(text=f"→ {signal}", fg=signal_color)

    def render_macro(*_):
        macro_quotes  = _macro_state["macro"]
        futures_yields = _macro_state["futures"]
        today_c       = _macro_state["today_curve"]
        yesterday_c   = _macro_state["yesterday_curve"]

        with _data_lock:
            df   = _live_data["df"]
            spot = _live_data["spot"]

        fdf = filter_df(df, spot, dte_label="0-45", expiry="ALL",
                        strike_pct=STRIKE_PCT)
        agg = _chart_agg(fdf) if not fdf.empty else None

        _update_regime_badge(macro_quotes, futures_yields, agg)

        for ax in [ax_curve, ax_table, ax_smile]:
            ax.clear()

        draw_yield_curve(ax_curve, today_c, yesterday_c)
        draw_yield_data_table(ax_table, macro_quotes, futures_yields)

        smile_df, smile_expiry = build_smile_df(df, spot, macro_expiry_var.get())
        result   = draw_vol_smile(ax_smile, smile_df, spot,
                                  SYMBOL, smile_expiry)
        if result[0] is not None:
            _store_smile(ax_smile, result[0], result[1], result[2])

        ts = datetime.datetime.now().strftime("%H:%M:%S")
        macro_fig.suptitle(
            f"SPY   Spot ${spot:.2f}      {_vix_str['text']}   |   {ts}",
            color=C["subtext"], fontsize=8.5,
            x=0.5, y=0.99, fontfamily="monospace"
        )
        macro_canvas.draw()

    # ── RENDER: BACKTEST ───────────────────────────────────────────────────────

    _bt_fetch_lock = threading.Lock()

    def render_backtest(*_):
        if demo:
            for ax in [ax_bt_gex, ax_bt_price, ax_bt_volume]:
                ax.clear()
                ax.set_facecolor(C["panel"])
            ax_bt_gex.text(
                0.5, 0.5,
                "Backtest tab requires live mode.\n"
                "Add Schwab API credentials to .env to enable.",
                transform=ax_bt_gex.transAxes, ha="center", va="center",
                color=C["subtext"], fontsize=11)
            bt_canvas.draw()
            return

        if not _bt_fetch_lock.acquire(blocking=False):
            return

        selected_date     = bt_date_entry.get_date()
        selected_date_str = selected_date.strftime("%Y-%m-%d")
        dte_lbl           = bt_dte_var.get()
        frequency         = bt_tf_var.get()

        _save_config({"backtest_date": selected_date_str})
        bt_status_var.set("Fetching...")
        root.update_idletasks()

        def _fetch_and_render():
            try:
                import db
                import schwab_price
                from charts.common import _style, _title

                has_gex          = False
                snap_spot        = 0
                agg_df           = None
                computed_flip    = None
                surface_max_pain = None
                snap_regime      = ""

                snapshot = db.get_opening_snapshot(SYMBOL, selected_date)
                is_today = (selected_date == datetime.date.today())

                if is_today:
                    latest = db.get_latest_summary(SYMBOL)
                    if latest:
                        snap_spot = latest.get("spot", 0)
                else:
                    if snapshot:
                        snap_spot = snapshot.get("spot", 0)

                if snapshot:
                    snap_regime = snapshot.get("regime", "")
                    ts          = snapshot.get("timestamp", "")
                    surface     = db.get_gex_surface(SYMBOL, ts)

                    if surface is not None and not surface.empty:
                        sf = surface[surface["dte"] < 1] \
                             if dte_lbl == "0DTE" \
                             else surface[surface["dte"] <= 45]

                        if not sf.empty:
                            cols = ["GEX_call", "GEX_put", "GEX_net",
                                    "VannEX_call", "VannEX_put", "VannEX_net",
                                    "CharmEX_call", "CharmEX_put", "CharmEX_net",
                                    "total_oi"]
                            rename_map = {
                                "VannEX_net":  "VannEX",
                                "CharmEX_net": "CharmEX",
                                "total_oi":    "oi",
                            }
                            agg_cols = [c for c in cols if c in sf.columns]
                            agg_df   = (sf.groupby("strike")[agg_cols]
                                          .sum()
                                          .reset_index()
                                          .rename(columns=rename_map)
                                          .sort_values("strike"))
                            if "GEX_net" not in agg_df.columns:
                                agg_df["GEX_net"] = (
                                    agg_df.get("GEX_call", 0) +
                                    agg_df.get("GEX_put",  0)
                                )
                            has_gex = True
                            if "type" in sf.columns:
                                surface_max_pain = _calc_max_pain(sf)

                from auth import get_valid_access_token
                token = get_valid_access_token(silent=True)
                bars  = schwab_price.get_single_day_bars(
                    token, SYMBOL, selected_date, frequency=frequency)

                hist_vol = schwab_price.get_historical_volume(token, SYMBOL)
                vol_90th = None
                vol_avg  = None
                if not hist_vol.empty and "Volume" in hist_vol.columns:
                    daily_vols = hist_vol["Volume"]
                    vol_90th   = float(np.percentile(daily_vols, 90))
                    vol_avg    = float(daily_vols.mean())
                    n_bars_est = {"1min": 390, "5min": 78, "10min": 39,
                                  "15min": 26, "30min": 13}.get(frequency, 78)
                    vol_90th   = vol_90th / n_bars_est

                def _draw():
                    ax_bt_gex.clear()
                    ax_bt_price.clear()
                    ax_bt_volume.clear()

                    nonlocal computed_flip

                    if has_gex:
                        computed_flip = draw_gex(
                            ax_bt_gex, agg_df, snap_spot, SYMBOL,
                            max_pain=surface_max_pain
                        )
                        _title(ax_bt_gex,
                               f"GEX at Open  -  {SYMBOL}  |  "
                               f"{selected_date_str}  [{dte_lbl}]")
                        ax_bt_gex.text(
                            0.01, 0.01, f"Regime: {snap_regime}",
                            transform=ax_bt_gex.transAxes,
                            color=C["subtext"], fontsize=7.5,
                            fontfamily="monospace", va="bottom",
                        )
                    else:
                        _style(ax_bt_gex)
                        ax_bt_gex.text(
                            0.5, 0.5,
                            f"No Greeks data\nfor {selected_date_str}",
                            transform=ax_bt_gex.transAxes,
                            ha="center", va="center",
                            color=C["subtext"], fontsize=11,
                        )
                        _title(ax_bt_gex,
                               f"GEX at Open  -  {SYMBOL}  |  "
                               f"{selected_date_str}")

                    _draw_price_chart(
                        ax_bt_price, bars, selected_date,
                        computed_flip, surface_max_pain,
                        SYMBOL, frequency
                    )
                    _draw_volume_panel(
                        ax_bt_volume, bars, selected_date,
                        vol_90th, vol_avg, frequency
                    )

                    bt_fig.suptitle(
                        f"SPY   {selected_date_str}   |   "
                        f"{frequency}   |   {dte_lbl} GEX",
                        color=C["subtext"], fontsize=8.5,
                        x=0.5, y=0.975, fontfamily="monospace",
                    )
                    bt_canvas.draw()
                    bt_status_var.set(
                        f"Updated {datetime.datetime.now().strftime('%H:%M:%S')}"
                    )

                root.after(0, _draw)

            except Exception as e:
                print(f"  Backtest render failed: {e}")
                root.after(0, lambda: bt_status_var.set(f"Error: {e}"))
            finally:
                _bt_fetch_lock.release()

        threading.Thread(target=_fetch_and_render, daemon=True).start()

    bt_date_entry.bind("<<DateEntrySelected>>", lambda e: render_backtest())

    # ── Tab switching ──────────────────────────────────────────────────────────

    def switch_to_charts():
        active_tab.set("CHARTS")
        charts_btn.config(bg=C["btn_on"])
        macro_btn.config(bg=C["btn_off"])
        backtest_btn.config(bg=C["btn_off"])
        macro_frame.place_forget()
        backtest_frame.place_forget()
        charts_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        for w in charts_ctrl_widgets:
            w.pack(side="left", padx=(0, 4))
        tooltip.place_forget()
        render_charts()

    def switch_to_macro():
        active_tab.set("MACRO")
        macro_btn.config(bg=C["btn_on"])
        charts_btn.config(bg=C["btn_off"])
        backtest_btn.config(bg=C["btn_off"])
        charts_frame.place_forget()
        backtest_frame.place_forget()
        macro_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        for w in charts_ctrl_widgets:
            w.pack_forget()
        tooltip.place_forget()
        render_macro()

    def switch_to_backtest():
        active_tab.set("BACKTEST")
        backtest_btn.config(bg=C["btn_on"])
        charts_btn.config(bg=C["btn_off"])
        macro_btn.config(bg=C["btn_off"])
        charts_frame.place_forget()
        macro_frame.place_forget()
        backtest_frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        for w in charts_ctrl_widgets:
            w.pack_forget()
        tooltip.place_forget()
        render_backtest()

    charts_btn.config(command=switch_to_charts)
    macro_btn.config(command=switch_to_macro)
    backtest_btn.config(command=switch_to_backtest)

    # ── Bindings ───────────────────────────────────────────────────────────────

    dte_cb.bind("<<ComboboxSelected>>",
                lambda e: [_update_expiry_list(), render_charts()])
    expiry_cb.bind("<<ComboboxSelected>>", lambda e: render_charts())
    range_cb.bind("<<ComboboxSelected>>",  lambda e: render_charts())

    _update_expiry_list()

    # ── Zoom ───────────────────────────────────────────────────────────────────

    zoom   = [1.0]
    base_w = [sw]
    base_h = [sh - 52]

    def on_scroll(event):
        if not (event.state & 0x4): return
        if active_tab.get() != "CHARTS": return
        zoom[0] = max(0.4, min(3.5,
                               zoom[0] * (0.88 if event.delta > 0 else 1.12)))
        nw = int(base_w[0] * zoom[0])
        nh = int(base_h[0] * zoom[0])
        fig.set_size_inches(nw / dpi, nh / dpi)
        charts_canvas.get_tk_widget().config(width=nw, height=nh)
        charts_canvas.draw()

    def on_key(event):
        if event.keysym.lower() == "r" and active_tab.get() == "CHARTS":
            zoom[0] = 1.0
            fig.set_size_inches(base_w[0] / dpi, base_h[0] / dpi)
            charts_canvas.get_tk_widget().config(
                width=base_w[0], height=base_h[0])
            render_charts()

    root.bind("<MouseWheel>", on_scroll)
    root.bind("<Key>", on_key)

    # ── Auto-refresh ───────────────────────────────────────────────────────────

    if not demo:
        _refresh_state = {"last_refresh": time.time(), "fetching": False}

        def _do_refresh():
            _refresh_state["fetching"] = True
            root.after(0, lambda: _set_indicator("red"))
            try:
                from auth import get_valid_access_token
                token = get_valid_access_token(silent=True)

                # Fetch SPY chain + VIX concurrently
                raw_result, vd = api_mod.fetch_live_data(token)
                if raw_result:
                    chain_dict, spot = raw_result
                    df = parse_chain(chain_dict)
                    if not df.empty:
                        with _data_lock:
                            _live_data["df"]   = df
                            _live_data["spot"] = spot
                        _vix_state["data"] = vd
                        _refresh_state["last_refresh"] = time.time()
                        root.after(0, _update_expiry_list)
                        root.after(0, lambda vd=vd: _build_vix_str(vd))

                # Refresh macro + futures in background
                try:
                    mq = macro_mod.get_macro_quotes(token)
                    fy = macro_mod.get_futures_yields(token)
                    _macro_state["macro"]   = mq
                    _macro_state["futures"] = fy
                except Exception as me:
                    print(f"  Macro refresh failed: {me}")

                if active_tab.get() == "CHARTS":
                    root.after(0, render_charts)
                elif active_tab.get() == "MACRO":
                    root.after(0, render_macro)

                root.after(0, lambda: _set_indicator("green"))
            except Exception as e:
                print(f"  Auto-refresh failed: {e}")
                root.after(0, lambda: _set_indicator("green"))
            finally:
                _refresh_state["fetching"] = False

        def _tick():
            if not _refresh_state["fetching"]:
                elapsed   = time.time() - _refresh_state["last_refresh"]
                remaining = max(0, REFRESH_INTERVAL - elapsed)
                m = int(remaining) // 60
                s = int(remaining) % 60
                countdown_var.set(f"next {m}:{s:02d}")
                if remaining <= 30:
                    _set_indicator("yellow")
                if remaining <= 0:
                    threading.Thread(target=_do_refresh, daemon=True).start()
            else:
                countdown_var.set("fetching...")
            root.after(1000, _tick)

        _tick()

    render_charts()
    root.mainloop()
