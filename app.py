import streamlit as st
import requests
import urllib3
import time
import concurrent.futures
import threading

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TF_MAP     = {"1H": "1h", "4H": "4h", "1D": "1d", "1W": "1w"}
MAX_RETRY  = 3
RETRY_WAIT = 1.5

BASE_URLS = [
    "https://fapi.binance.com",
    "https://fapi1.binance.com",
    "https://fapi2.binance.com",
    "https://fapi3.binance.com",
]

_local       = threading.local()
_base_url:   str  = ""
_ssl_verify: bool = True


def get_session() -> requests.Session:
    if not hasattr(_local, "session"):
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20,
            max_retries=urllib3.util.retry.Retry(
                total=3, backoff_factor=0.5,
                status_forcelist=[500, 502, 503, 504],
                allowed_methods=["GET"],
            ),
        )
        s.mount("https://", adapter)
        s.mount("http://",  adapter)
        _local.session = s
    _local.session.verify = _ssl_verify
    return _local.session


def detect_base_url() -> tuple[str, bool]:
    for verify in (True, False):
        for base in BASE_URLS:
            try:
                r = requests.get(f"{base}/fapi/v1/ping", timeout=6, verify=verify)
                if r.status_code == 200:
                    return base, verify
            except Exception:
                continue
    raise RuntimeError(
        "Cannot reach Binance Futures API. Check your internet or use a VPN."
    )


def api_get(path: str, params: dict | None = None):
    url, wait, last = f"{_base_url}{path}", RETRY_WAIT, None
    session = get_session()
    for _ in range(MAX_RETRY):
        try:
            r = session.get(url, params=params, timeout=10)
            if r.status_code == 429:
                time.sleep(wait); wait *= 2; continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.SSLError:
            session.verify = False; time.sleep(wait); wait *= 2
        except Exception as e:
            last = e; time.sleep(wait); wait *= 2
    raise RuntimeError(f"API failed: {last}")


def get_all_symbols() -> list[str]:
    data = api_get("/fapi/v1/exchangeInfo")
    return sorted(
        s["symbol"] for s in data["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
    )


def fetch_klines(symbol: str, interval: str, limit: int) -> list | None:
    try:
        raw = api_get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit + 1},
        )
        raw = raw[:-1]
        if len(raw) < limit:
            return None
        return [
            {"open": float(c[1]), "high": float(c[2]),
             "low":  float(c[3]), "close": float(c[4])}
            for c in raw[-limit:]
        ]
    except Exception:
        return None


# ── Pattern helpers ────────────────────────────────────────────────────────────

def is_bull(c) -> bool: return c["close"] > c["open"]
def is_bear(c) -> bool: return c["close"] < c["open"]


def find_p1_windows(candles: list) -> list[int]:
    """
    Pattern 1 — two consecutive Bull→Bear pairs, no gap.
    C1 bullish, C2 bearish: C2.high<C1.high, C2.close<C1.low
    C3 bullish: C3.high<C1.high
    C4 bearish: C4.high<C3.high, C4.close<C3.low
    """
    hits = []
    for i in range(len(candles) - 3):
        c1, c2, c3, c4 = candles[i], candles[i+1], candles[i+2], candles[i+3]
        if (
            is_bull(c1)
            and is_bear(c2)
            and c2["high"]  < c1["high"]
            and c2["close"] < c1["low"]
            and c3["high"]  < c1["high"]
            and is_bull(c3)
            and is_bear(c4)
            and c4["high"]  < c3["high"]
            and c4["close"] < c3["low"]
        ):
            hits.append(i)
    return hits


def find_p2_windows(candles: list) -> list[int]:
    """
    Pattern 2 — two consecutive Bear→Bull pairs, no gap.
    C1 bearish, C2 bullish: C2.low>C1.low, C2.close>C1.high
    C3 bearish: C3.low>C1.low
    C4 bullish: C4.low>C3.low, C4.close>C3.high
    """
    hits = []
    for i in range(len(candles) - 3):
        c1, c2, c3, c4 = candles[i], candles[i+1], candles[i+2], candles[i+3]
        if (
            is_bear(c1)
            and is_bull(c2)
            and c2["low"]   > c1["low"]
            and c2["close"] > c1["high"]
            and c3["low"]   > c1["low"]
            and is_bear(c3)
            and is_bull(c4)
            and c4["low"]   > c3["low"]
            and c4["close"] > c3["high"]
        ):
            hits.append(i)
    return hits


def scan_symbol(symbol: str, interval: str, n: int, mode: str):
    candles = fetch_klines(symbol, interval, n)
    if not candles or len(candles) < 4:
        return symbol, []

    results = []

    if mode in ("Pattern 1  (Bull→Bear breakdown)", "Both"):
        hits = find_p1_windows(candles)
        if hits:
            results.append({
                "type":   "Bull→Bear",
                "rule":   "C2.high<C1.high · C2.close<C1.low · C3.high<C1.high",
                "color":  "#e05c2a",
                "icon":   "🔻",
                "count":  len(hits),
                "labels": " · ".join(
                    f"[C{i+1}C{i+2}|C{i+3}C{i+4}]" for i in hits
                ),
            })

    if mode in ("Pattern 2  (Bear→Bull breakout)", "Both"):
        hits = find_p2_windows(candles)
        if hits:
            results.append({
                "type":   "Bear→Bull",
                "rule":   "C2.low>C1.low · C2.close>C1.high · C3.low>C1.low",
                "color":  "#21c354",
                "icon":   "🔺",
                "count":  len(hits),
                "labels": " · ".join(
                    f"[C{i+1}C{i+2}|C{i+3}C{i+4}]" for i in hits
                ),
            })

    return symbol, results


# ── Streamlit UI ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="Binance Pair Scanner", page_icon="🕯️", layout="wide")
st.title("🕯️ Binance Futures — Pair Pattern Scanner")
st.caption(
    "Two qualifying pairs in **4 consecutive candles** (no gap). "
    "Results stream live as each symbol finishes."
)

with st.sidebar:
    st.header("⚙️ Settings")
    timeframe   = st.selectbox("Timeframe",          ["1H","4H","1D","1W"], index=1)
    n_candles   = st.selectbox("Lookback (candles)",  [5, 10, 15, 20],       index=1)
    mode        = st.radio("Pattern", [
        "Pattern 1  (Bull→Bear breakdown)",
        "Pattern 2  (Bear→Bull breakout)",
        "Both",
    ], index=2)
    # Streamlit Cloud has limited CPU — keep concurrency sensible
    max_workers = st.slider("Concurrency (threads)", 1, 10, 6,
                            help="Streamlit Cloud has shared CPU — keep at 4–6.")
    st.divider()
    with st.expander("Pattern rules", expanded=True):
        st.markdown("""
**Pattern 1 — Bull→Bear (breakdown)**
```
C1  bullish
C2  bearish · C2.high  < C1.high
             · C2.close < C1.low
C3  bullish · C3.high  < C1.high
C4  bearish · C4.high  < C3.high
             · C4.close < C3.low
```
**Pattern 2 — Bear→Bull (breakout)**
```
C1  bearish
C2  bullish · C2.low   > C1.low
             · C2.close > C1.high
C3  bearish · C3.low   > C1.low
C4  bullish · C4.low   > C3.low
             · C4.close > C3.high
```
All 4 candles consecutive — zero gap between pairs.
""")

# ── Run ────────────────────────────────────────────────────────────────────────
if st.button("▶ Start Scan", type="primary", use_container_width=True):
    interval = TF_MAP[timeframe]
    t0 = time.time()

    conn_info = st.empty()
    conn_info.info("🔌 Detecting best Binance endpoint…")
    try:
        _base_url, _ssl_verify = detect_base_url()
        ssl_note = "" if _ssl_verify else " (SSL verify disabled — region fallback)"
        conn_info.success(f"✅ Connected to `{_base_url}`{ssl_note}")
    except RuntimeError as e:
        conn_info.empty()
        st.error(f"❌ {e}")
        st.stop()

    with st.spinner("Fetching USDT-M symbol list…"):
        try:
            symbols = get_all_symbols()
        except Exception as e:
            st.error(f"Could not load symbols: {e}"); st.stop()

    total, scanned, match_count = len(symbols), 0, 0
    prog        = st.progress(0.0, text="Initialising…")
    stats       = st.empty()
    st.markdown(f"### Results — `{timeframe}` · last `{n_candles}` candles · `{mode}`")
    result_area = st.container()

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(scan_symbol, sym, interval, n_candles, mode): sym
            for sym in symbols
        }
        for fut in concurrent.futures.as_completed(futures):
            symbol, results = fut.result()
            scanned += 1
            for res in results:
                match_count += 1
                with result_area:
                    st.markdown(f"""
<div style="border-left:4px solid {res['color']};padding:10px 16px;margin-bottom:8px;
border-radius:6px;background:rgba(128,128,128,0.05);
display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
  <span style="font-size:1.2em">{res['icon']}</span>
  <b style="font-size:1.05em;min-width:130px">{symbol}</b>
  <span style="background:{res['color']};color:#fff;padding:2px 10px;
  border-radius:4px;font-size:0.8em;font-weight:500">{res['type']}</span>
  <span style="background:rgba(128,128,128,0.1);padding:2px 8px;border-radius:4px;
  font-size:0.78em;color:gray;font-family:monospace">{res['rule']}</span>
  <span style="color:gray;font-size:0.85em">
    {res['count']} window{'s' if res['count']>1 else ''} &nbsp;·&nbsp; {res['labels']}
  </span>
  <span style="color:gray;font-size:0.8em;margin-left:auto">{timeframe} · N={n_candles}</span>
</div>""", unsafe_allow_html=True)

            elapsed = time.time() - t0
            prog.progress(scanned / total, text=f"Scanning {scanned}/{total}…")
            stats.caption(
                f"⏱ {elapsed:.1f}s  |  ✅ {scanned}/{total} scanned  |  🎯 {match_count} matches"
            )

    elapsed = time.time() - t0
    prog.empty(); stats.empty(); conn_info.empty()
    st.success(
        f"✅ Scan complete — {total} symbols in {elapsed:.1f}s · "
        f"**{match_count}** setup{'s' if match_count != 1 else ''} found"
    )
    if match_count == 0:
        st.info(
            "No setups found. Try a larger lookback (15 or 20) or a different timeframe."
        )
