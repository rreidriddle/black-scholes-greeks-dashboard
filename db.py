"""
db.py — Backtest database read layer.

Read-only interface to greeks_history.db populated by collector.py.
Used exclusively by the BACKTEST tab. All other dashboard data is fetched
live from the Schwab API or Treasury.gov via macro.py and api.py.

Set GREEKS_DB_PATH in .env to point at the collector's database file.
The BACKTEST tab degrades gracefully if the database is not present.
"""

import os
import sqlite3
import datetime
import warnings
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get("GREEKS_DB_PATH", "greeks_history.db")

REGIME_STRONG_POS_THRESHOLD = 2e9
REGIME_WEAK_POS_THRESHOLD   = 0.0
REGIME_FLIP_ZONE_PCT        = 0.005
REGIME_WEAK_NEG_THRESHOLD   = -2e9

# ── Connection ─────────────────────────────────────────────────────────────────

def get_connection(path: str = None) -> sqlite3.Connection | None:
    db = path or DB_PATH
    if not os.path.exists(db):
        warnings.warn(
            f"db.py: database not found at '{db}'. "
            "Run collector.py to build history. BACKTEST tab will be unavailable.",
            stacklevel=2,
        )
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        warnings.warn(f"db.py: could not connect: {e}", stacklevel=2)
        return None

# ── Empty frame helpers ────────────────────────────────────────────────────────

def _empty_summary() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "timestamp", "symbol", "spot",
        "net_GEX", "net_VannEX", "net_CharmEX",
        "gamma_flip", "call_wall", "put_wall", "max_pain",
        "total_oi", "total_volume", "iv_atm",
        "gex_0dte", "gex_1_7dte", "gex_8_30dte",
        "gex_31_90dte", "gex_91_180dte", "gex_180plus_dte",
        "regime",
    ])


def _empty_strikes() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "timestamp", "symbol", "spot", "strike", "dte", "dte_bucket",
        "GEX_call", "GEX_put", "GEX_net",
        "VannEX_call", "VannEX_put", "VannEX_net",
        "CharmEX_call", "CharmEX_put", "CharmEX_net",
        "total_oi", "total_volume", "iv_call", "iv_put",
    ])

# ── Regime classification ──────────────────────────────────────────────────────

def classify_regime(net_gex: float, spot: float,
                    gamma_flip: float | None,
                    flip_zone_pct: float = REGIME_FLIP_ZONE_PCT) -> str:
    if gamma_flip is not None and spot > 0:
        if abs(spot - gamma_flip) / spot <= flip_zone_pct:
            return "FLIP_ZONE"
    if net_gex >= REGIME_STRONG_POS_THRESHOLD: return "STRONG_POS"
    if net_gex >= REGIME_WEAK_POS_THRESHOLD:   return "WEAK_POS"
    if net_gex >= REGIME_WEAK_NEG_THRESHOLD:   return "WEAK_NEG"
    return "STRONG_NEG"

# ── Utility queries ────────────────────────────────────────────────────────────

def get_available_symbols() -> list[str]:
    conn = get_connection()
    if conn is None:
        return []
    try:
        return [row[0] for row in conn.execute(
            "SELECT DISTINCT symbol FROM summary ORDER BY symbol"
        ).fetchall()]
    except Exception as e:
        warnings.warn(f"db.py get_available_symbols: {e}", stacklevel=2)
        return []
    finally:
        conn.close()


def get_date_range(symbol: str) -> tuple[str | None, str | None]:
    conn = get_connection()
    if conn is None:
        return None, None
    try:
        row = conn.execute(
            "SELECT MIN(DATE(timestamp)), MAX(DATE(timestamp)) FROM summary WHERE symbol = ?",
            (symbol,)
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)
    except Exception as e:
        warnings.warn(f"db.py get_date_range: {e}", stacklevel=2)
        return None, None
    finally:
        conn.close()


def get_pull_timestamps(symbol: str,
                        start_date: str | datetime.date,
                        end_date:   str | datetime.date) -> list[str]:
    conn = get_connection()
    if conn is None:
        return []
    try:
        return [row[0] for row in conn.execute(
            """
            SELECT DISTINCT timestamp FROM summary
            WHERE symbol = ?
              AND DATE(timestamp) BETWEEN DATE(?) AND DATE(?)
            ORDER BY timestamp
            """,
            (symbol, str(start_date), str(end_date))
        ).fetchall()]
    except Exception as e:
        warnings.warn(f"db.py get_pull_timestamps: {e}", stacklevel=2)
        return []
    finally:
        conn.close()

# ── Summary queries ────────────────────────────────────────────────────────────

def get_latest_summary(symbol: str) -> dict | None:
    conn = get_connection()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM summary WHERE symbol = ? ORDER BY timestamp DESC LIMIT 1",
            (symbol,)
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["regime"] = classify_regime(d.get("net_GEX", 0), d.get("spot", 0), d.get("gamma_flip"))
        return d
    except Exception as e:
        warnings.warn(f"db.py get_latest_summary: {e}", stacklevel=2)
        return None
    finally:
        conn.close()


def get_max_pain(symbol: str, offset: int = 0) -> float | None:
    conn = get_connection()
    if conn is None:
        return None
    try:
        row = conn.execute(
            """
            SELECT max_pain FROM summary
            WHERE symbol = ? AND max_pain IS NOT NULL
            ORDER BY timestamp DESC LIMIT 1 OFFSET ?
            """,
            (symbol, offset)
        ).fetchone()
        return float(row[0]) if row else None
    except Exception as e:
        warnings.warn(f"db.py get_max_pain: {e}", stacklevel=2)
        return None
    finally:
        conn.close()


def get_summary_history(symbol: str,
                        start_date: str | datetime.date,
                        end_date:   str | datetime.date) -> pd.DataFrame:
    conn = get_connection()
    if conn is None:
        return _empty_summary()
    try:
        df = pd.read_sql_query(
            """
            SELECT * FROM summary
            WHERE symbol = ?
              AND DATE(timestamp) BETWEEN DATE(?) AND DATE(?)
            ORDER BY timestamp
            """,
            conn,
            params=(symbol, str(start_date), str(end_date)),
        )
        if df.empty:
            return _empty_summary()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp").sort_index()
        df["regime"] = df.apply(
            lambda r: classify_regime(r.get("net_GEX", 0), r.get("spot", 0), r.get("gamma_flip")),
            axis=1,
        )
        return df
    except Exception as e:
        warnings.warn(f"db.py get_summary_history: {e}", stacklevel=2)
        return _empty_summary()
    finally:
        conn.close()


def get_opening_snapshot(symbol: str,
                         date: str | datetime.date) -> dict | None:
    conn = get_connection()
    if conn is None:
        return None
    try:
        date_str = str(date)
        row = conn.execute(
            """
            SELECT * FROM summary
            WHERE symbol = ?
              AND DATE(timestamp) = DATE(?)
              AND TIME(timestamp) >= '13:30:00'
            ORDER BY timestamp ASC LIMIT 1
            """,
            (symbol, date_str)
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT * FROM summary
                WHERE symbol = ? AND DATE(timestamp) = DATE(?)
                ORDER BY timestamp ASC LIMIT 1
                """,
                (symbol, date_str)
            ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["regime"] = classify_regime(d.get("net_GEX", 0), d.get("spot", 0), d.get("gamma_flip"))
        return d
    except Exception as e:
        warnings.warn(f"db.py get_opening_snapshot: {e}", stacklevel=2)
        return None
    finally:
        conn.close()

# ── Strike-level queries ───────────────────────────────────────────────────────

def get_gex_surface(symbol: str,
                    timestamp: str | datetime.datetime) -> pd.DataFrame:
    conn = get_connection()
    if conn is None:
        return _empty_strikes()
    try:
        ts_str = (timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
                  if isinstance(timestamp, datetime.datetime) else str(timestamp))
        row = conn.execute(
            """
            SELECT timestamp FROM strike_data
            WHERE symbol = ?
              AND ABS(strftime('%s', timestamp) - strftime('%s', ?)) <= 600
            ORDER BY ABS(strftime('%s', timestamp) - strftime('%s', ?))
            LIMIT 1
            """,
            (symbol, ts_str, ts_str)
        ).fetchone()
        if row is None:
            return _empty_strikes()
        df = pd.read_sql_query(
            "SELECT * FROM strike_data WHERE symbol = ? AND timestamp = ? ORDER BY strike",
            conn, params=(symbol, row[0]),
        )
        if df.empty:
            return _empty_strikes()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df
    except Exception as e:
        warnings.warn(f"db.py get_gex_surface: {e}", stacklevel=2)
        return _empty_strikes()
    finally:
        conn.close()


# ── Read-write connection ──────────────────────────────────────────────────────

def get_rw_connection(path: str = None) -> sqlite3.Connection | None:
    db = path or DB_PATH
    try:
        conn = sqlite3.connect(db, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        warnings.warn(f"db.py: could not open rw connection: {e}", stacklevel=2)
        return None

# ── Price bar cache ────────────────────────────────────────────────────────────

def init_price_cache() -> None:
    """Create price_bars and hist_volume_cache tables if they don't exist."""
    conn = get_rw_connection()
    if conn is None:
        return
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS price_bars (
                symbol  TEXT NOT NULL,
                date    TEXT NOT NULL,
                ts      TEXT NOT NULL,
                open    REAL,
                high    REAL,
                low     REAL,
                close   REAL,
                volume  INTEGER,
                PRIMARY KEY (symbol, date, ts)
            );
            CREATE TABLE IF NOT EXISTS hist_volume_cache (
                symbol     TEXT NOT NULL,
                date       TEXT NOT NULL,
                volume     INTEGER,
                cached_on  TEXT NOT NULL,
                PRIMARY KEY (symbol, date)
            );
        """)
        conn.commit()
    except Exception as e:
        warnings.warn(f"db.py init_price_cache: {e}", stacklevel=2)
    finally:
        conn.close()


def get_cached_bars(symbol: str, date: str) -> pd.DataFrame | None:
    """
    Return 1-min bars for symbol+date from price_bars, or None if not found.
    Returns DataFrame with UTC DatetimeIndex and columns Open/High/Low/Close/Volume.
    """
    conn = get_connection()
    if conn is None:
        return None
    try:
        df = pd.read_sql_query(
            "SELECT ts, open, high, low, close, volume FROM price_bars "
            "WHERE symbol = ? AND date = ? ORDER BY ts",
            conn,
            params=(symbol, date),
        )
        if df.empty:
            return None
        df = df.rename(columns={
            "ts":     "datetime",
            "open":   "Open",
            "high":   "High",
            "low":    "Low",
            "close":  "Close",
            "volume": "Volume",
        })
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        df = df.set_index("datetime").sort_index()
        return df
    except Exception as e:
        warnings.warn(f"db.py get_cached_bars: {e}", stacklevel=2)
        return None
    finally:
        conn.close()


def save_bars(symbol: str, date: str, df: pd.DataFrame) -> None:
    """Write 1-min OHLCV DataFrame rows to price_bars using INSERT OR REPLACE."""
    if df is None or df.empty:
        return
    conn = get_rw_connection()
    if conn is None:
        return
    try:
        rows = []
        for ts, row in df.iterrows():
            ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            rows.append((
                symbol,
                date,
                ts_str,
                row.get("Open"),
                row.get("High"),
                row.get("Low"),
                row.get("Close"),
                int(row["Volume"]) if pd.notna(row.get("Volume")) else None,
            ))
        conn.executemany(
            "INSERT OR REPLACE INTO price_bars "
            "(symbol, date, ts, open, high, low, close, volume) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except Exception as e:
        warnings.warn(f"db.py save_bars: {e}", stacklevel=2)
    finally:
        conn.close()


def get_cached_hist_volume(symbol: str) -> pd.DataFrame | None:
    """
    Return hist_volume_cache rows for symbol where cached_on = today.
    Returns DataFrame with date index and Volume column, or None if not found/stale.
    """
    conn = get_connection()
    if conn is None:
        return None
    try:
        today_str = datetime.date.today().isoformat()
        df = pd.read_sql_query(
            "SELECT date, volume FROM hist_volume_cache "
            "WHERE symbol = ? AND cached_on = ? ORDER BY date",
            conn,
            params=(symbol, today_str),
        )
        if df.empty:
            return None
        df = df.rename(columns={"volume": "Volume"})
        df["date"] = pd.to_datetime(df["date"]).dt.date
        df = df.set_index("date")
        return df
    except Exception as e:
        warnings.warn(f"db.py get_cached_hist_volume: {e}", stacklevel=2)
        return None
    finally:
        conn.close()


def save_hist_volume(symbol: str, df: pd.DataFrame) -> None:
    """
    Delete old hist_volume_cache rows for symbol, write new rows with cached_on = today.
    df must have a date index and a Volume column.
    """
    if df is None or df.empty:
        return
    conn = get_rw_connection()
    if conn is None:
        return
    try:
        today_str = datetime.date.today().isoformat()
        conn.execute(
            "DELETE FROM hist_volume_cache WHERE symbol = ?", (symbol,)
        )
        rows = []
        for idx, row in df.iterrows():
            date_str = idx.isoformat() if hasattr(idx, "isoformat") else str(idx)
            vol = int(row["Volume"]) if pd.notna(row.get("Volume")) else None
            rows.append((symbol, date_str, vol, today_str))
        conn.executemany(
            "INSERT OR REPLACE INTO hist_volume_cache "
            "(symbol, date, volume, cached_on) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    except Exception as e:
        warnings.warn(f"db.py save_hist_volume: {e}", stacklevel=2)
    finally:
        conn.close()


def get_dates_missing_bars(symbol: str) -> list[str]:
    """
    Return list of YYYY-MM-DD date strings that exist in summary
    but NOT in price_bars for symbol, excluding today.
    """
    conn = get_connection()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT DATE(timestamp) FROM summary
            WHERE symbol = ?
              AND DATE(timestamp) < DATE('now')
              AND DATE(timestamp) NOT IN (
                  SELECT DISTINCT date FROM price_bars WHERE symbol = ?
              )
            ORDER BY 1
            """,
            (symbol, symbol),
        ).fetchall()
        return [row[0] for row in rows]
    except Exception as e:
        warnings.warn(f"db.py get_dates_missing_bars: {e}", stacklevel=2)
        return []
    finally:
        conn.close()


def get_session_summary(symbol: str,
                        start_date: str | datetime.date,
                        end_date:   str | datetime.date) -> pd.DataFrame:
    conn = get_connection()
    if conn is None:
        return pd.DataFrame()
    try:
        df = pd.read_sql_query(
            """
            SELECT * FROM summary
            WHERE symbol = ?
              AND DATE(timestamp) BETWEEN DATE(?) AND DATE(?)
            ORDER BY timestamp
            """,
            conn, params=(symbol, str(start_date), str(end_date)),
        )
        if df.empty:
            return pd.DataFrame()
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df["date"]      = df["timestamp"].dt.date
        rows = []
        for date, group in df.groupby("date"):
            group = group.sort_values("timestamp")
            first, last = group.iloc[0], group.iloc[-1]
            open_flip  = first.get("gamma_flip")
            open_spot  = first.get("spot", 0)
            close_spot = last.get("spot", 0)
            flip_tested = False
            if open_flip and open_flip > 0:
                flip_tested = any(
                    abs(s - open_flip) / open_flip <= 0.001
                    for s in group["spot"].values
                )
            rows.append({
                "date":               date,
                "symbol":             symbol,
                "open_spot":          open_spot,
                "open_net_gex":       first.get("net_GEX"),
                "open_gamma_flip":    open_flip,
                "open_max_pain":      first.get("max_pain"),
                "open_call_wall":     first.get("call_wall"),
                "open_put_wall":      first.get("put_wall"),
                "open_iv_atm":        first.get("iv_atm"),
                "open_regime":        classify_regime(first.get("net_GEX", 0), open_spot, open_flip),
                "close_spot":         close_spot,
                "close_net_gex":      last.get("net_GEX"),
                "close_gamma_flip":   last.get("gamma_flip"),
                "intraday_high_spot": group["spot"].max(),
                "intraday_low_spot":  group["spot"].min(),
                "closed_above_flip":  open_flip is not None and close_spot > open_flip,
                "flip_tested":        flip_tested,
            })
        return pd.DataFrame(rows).set_index("date")
    except Exception as e:
        warnings.warn(f"db.py get_session_summary: {e}", stacklevel=2)
        return pd.DataFrame()
    finally:
        conn.close()
