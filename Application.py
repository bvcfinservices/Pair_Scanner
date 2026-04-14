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

# ── Proxy config ───────────────────────────────────────────────────────────────
# Your Cloudflare Worker URL — set in Streamlit Cloud secrets as:
#   [proxy]
#   worker_url = "https://YOUR-WORKER.YOUR-SUBDOMAIN.workers.dev"
#
# Or hard-code it below for quick testing (replace with your actual URL):
#   WORKER_URL = "https://my-binance-proxy.myname.workers.dev"

def get_worker_url() -> str:
    try:
        # Read from Streamlit secrets (recommended for deployment)
        return st.secrets["proxy"]["worker_url"].rstrip("/")
    except Exception:
        # Fallback: hard-coded — replace with your worker URL after deploying
        return st.session_state.get("worker_url", "").rstrip("/")


# ── Session (thread-local, pooled) ────────────────────────────────────────────
_local = threading.local()

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
    return _local.session


# ── API ────────────────────────────────────────────────────────────────────────

def api_get(path: str, params: dict | None = None, worker_url: str = "") -> any:
    """
    Route through Cloudflare Worker proxy.
    Falls back to direct Binance if worker_url is empty (local use).
    """
    if worker_url:
        base = worker_url
    else:
        base = "https://fapi.binance.com"

    url  = f"{base}{path}"
    wait = RETRY_WAIT
    last = None

    for _ in range(MAX_RETRY):
        try:
            r = get_session().get(url, params=params, timeout=12)
            if r.status_code == 429:
                time.sleep(wait); wait *= 2; continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(wait); wait *= 2

    raise RuntimeError(f"Request failed: {last}")


def check_connection(worker_url: str) -> bool:
    try:
        base = worker_url if worker_url else "https://fapi.binance.com"
        r = get_session().get(f"{base}/fapi/v1/ping", timeout=8)
        return r.status_code == 200
    except Exception:
        return False


def get_all_symbols(worker_url: str) -> list[str]:
    data = api_get("/fapi/v1/exchangeInfo", worker_url=worker_url)
    return sorted(
        s["symbol"] for s in data["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
    )


def fetch_klines(symbol: str, interval: str, limit: int, worker_url: str) -> list | None:
    try:
        raw = api_get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit + 1},
            worker_url=worker_url,
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


# ── Pattern logic ──────────────────────────────────────────────────────────────

def is_bull(c) -> bool: return c["close"] > c["open"]
def is_bear(c) -> bool: return c["close"] < c["open"]


def find_p1_windows(candles: list) -> list[int]:
    """
    Pattern 1 — two consecutive Bull→Bear pairs, no gap.
    C1 bull, C2 bear: C2.high<C1.high, C2.close<C1.low
    C3 bull: C3.high<C1.high
    C4 bear: C4.high<C3.high, C4.close<C3.low
    """
    hits = []
    for i in range(len(candles) - 3):
        c1, c2, c3, c4 = candles[i], candles[i+1], candles[i+2], candles[i+3]
        if (
            is_bull(c1) and is_bear(c2)
            and c2["high"]  < c1["high"]
            and c2["close"] < c1["low"]
            and c3["high"]  < c1["high"]
            and is_bull(c3) and is_bear(c4)
            and c4["high"]  < c3["high"]
            and c4["close"] < c3["low"]
        ):
            hits.append(i)
    return hits


def find_p2_windows(candles: list) -> list[int]:
    """
    Pattern 2 — two consecutive Bear→Bull pairs, no gap.
    C1 bear, C2 bull: C2.low>C1.low, C2.close>C1.high
    C3 bear: C3.low>C1.low
    C4 bull: C4.low>C3.low, C4.close>C3.high
    """
    hits = []
    for i in range(len(candles) - 3):
        c1, c2, c3, c4 = candles[i], candles[i+1], candles[i+2], candles[i+3]
        if (
            is_bear(c1) and is_bull(c2)
            and c2["low"]   > c1["low"]
            and c2["close"] > c1["high"]
            and c3["low"]   > c1["low"]
            and is_bear(c3) and is_bull(c4)
            and c4["low"]   > c3["low"]
            and c4["close"] > c3["high"]
        ):
            hits.append(i)
    return hits


def scan_symbol(args):
    symbol, interval, n, mode, worker_url = args
    candles = fetch_klines(symbol, interval, n, worker_url)
    if not candles or len(candles) < 4:
        return symbol, []
    results = []
    if mode in ("Pattern 1  (Bull→Bear breakdown)", "Both"):
        hits = find_p1_windows(candles)
        if hits:
            results.append({
                "type":   "Bull→Bear",
                "rule":   "C2.high<C1.high · C2.close<C1.low · C3.high<C1.high",
                "color":  "#e05c2a", "icon": "🔻",
                "count":  len(hits),
                "labels": " · ".join(f"[C{i+1}C{i+2}|C{i+3}C{i+4}]" for i in hits),
            })
    if mode in ("Pattern 2  (Bear→Bull breakout)", "Both"):
        hits = find_p2_windows(candles)
        if hits:
            results.append({
                "type":   "Bear→Bull",
                "rule":   "C2.low>C1.low · C2.close>C1.high · C3.low>C1.low",
                "color":  "#21c354", "icon": "🔺",
                "count":  len(hits),
                "labels": " · ".join(f"[C{i+1}C{i+2}|C{i+3}C{i+4}]" for i in hits),
            })
    return symbol, results


# ── Streamlit UI ───────────────────────────────────────────────────────────────

st.set_page_config(page_title="Binance Pair Scanner", page_icon="🕯️", layout="wide")
st.title("🕯️ Binance Futures — Pair Pattern Scanner")
st.caption("Two qualifying pairs in **4 consecutive candles** (no gap). Results stream live.")

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Settings")

    # Worker URL input — pre-filled from secrets if available
    default_worker = ""
    try:
        default_worker = st.secrets["proxy"]["worker_url"]
    except Exception:
        pass

    worker_url = st.text_input(
        "Cloudflare Worker URL",
        value=default_worker,
        placeholder="https://YOUR-WORKER.YOUR-SUBDOMAIN.workers.dev",
        help="Deploy cloudflare_worker.js first. Paste your worker URL here.",
    ).strip().rstrip("/")

    st.divider()
    timeframe   = st.selectbox("Timeframe",         ["1H","4H","1D","1W"], index=1)
    n_candles   = st.selectbox("Lookback (candles)", [5, 10, 15, 20],       index=1)
    mode        = st.radio("Pattern", [
        "Pattern 1  (Bull→Bear breakdown)",
        "Pattern 2  (Bear→Bull breakout)",
        "Both",
    ], index=2)
    max_workers = st.slider("Concurrency (threads)", 1, 10, 5)

    st.divider()
    with st.expander("Pattern rules", expanded=False):
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
""")

    # Connection test button
    if st.button("🔌 Test connection"):
        if not worker_url:
            st.warning("Enter your Worker URL first.")
        elif check_connection(worker_url):
            st.success("✅ Connected!")
        else:
            st.error("❌ Cannot reach API via this worker.")

# ── Worker URL guard ───────────────────────────────────────────────────────────
if not worker_url:
    st.warning(
        "**Setup required** — paste your Cloudflare Worker URL in the sidebar.\n\n"
        "1. Go to [dash.cloudflare.com](https://dash.cloudflare.com) (free account)\n"
        "2. Workers & Pages → Create Worker → paste `cloudflare_worker.js` → Deploy\n"
        "3. Copy the worker URL and paste it in the sidebar field above"
    )
    st.stop()

# ── Run ────────────────────────────────────────────────────────────────────────
if st.button("▶ Start Scan", type="primary", use_container_width=True):
    interval = TF_MAP[timeframe]
    t0 = time.time()

    conn_info = st.empty()
    conn_info.info(f"🔌 Connecting via `{worker_url}`…")

    if not check_connection(worker_url):
        conn_info.error("❌ Worker did not respond. Check the URL and redeploy if needed.")
        st.stop()
    conn_info.success(f"✅ Connected via Cloudflare Worker")

    with st.spinner("Fetching USDT-M symbol list…"):
        try:
            symbols = get_all_symbols(worker_url)
        except Exception as e:
            st.error(f"Could not load symbols: {e}"); st.stop()

    total, scanned, match_count = len(symbols), 0, 0
    prog        = st.progress(0.0, text="Initialising…")
    stats       = st.empty()
    st.markdown(f"### Results — `{timeframe}` · last `{n_candles}` candles · `{mode}`")
    result_area = st.container()

    args_list = [
        (sym, interval, n_candles, mode, worker_url)
        for sym in symbols
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(scan_symbol, args): args[0] for args in args_list}

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
        st.info("No setups found. Try a larger lookback (15–20) or a different timeframe.")
