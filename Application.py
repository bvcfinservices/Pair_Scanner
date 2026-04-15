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

# ── Thread-local session ───────────────────────────────────────────────────────
_local = threading.local()

def get_session() -> requests.Session:
    if not hasattr(_local, "session"):
        s = requests.Session()
        a = requests.adapters.HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20,
            max_retries=urllib3.util.retry.Retry(
                total=2, backoff_factor=0.3,
                status_forcelist=[500, 502, 503, 504],
                allowed_methods=["GET"],
            ),
        )
        s.mount("https://", a)
        s.mount("http://",  a)
        _local.session = s
    return _local.session


# ── Single API function — always goes through worker ──────────────────────────

def api_get(worker: str, path: str, params: dict | None = None):
    """
    ALL requests go to:  worker + path + ?params
    e.g. https://my-proxy.workers.dev/fapi/v1/klines?symbol=BTCUSDT&...
    Never touches fapi.binance.com directly.
    """
    url  = f"{worker}{path}"
    wait = RETRY_WAIT
    last = None
    for _ in range(MAX_RETRY):
        try:
            r = get_session().get(url, params=params, timeout=15)
            if r.status_code == 429:
                time.sleep(wait); wait *= 2; continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(wait); wait *= 2
    raise RuntimeError(str(last))


def ping(worker: str) -> bool:
    try:
        r = get_session().get(f"{worker}/fapi/v1/ping", timeout=10)
        return r.status_code == 200
    except Exception:
        return False


def get_all_symbols(worker: str) -> list[str]:
    data = api_get(worker, "/fapi/v1/exchangeInfo")
    return sorted(
        s["symbol"] for s in data["symbols"]
        if s["status"] == "TRADING" and s["quoteAsset"] == "USDT"
    )


def fetch_klines(worker: str, symbol: str, interval: str, limit: int) -> list | None:
    try:
        raw = api_get(
            worker, "/fapi/v1/klines",
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


# ── Pattern logic ──────────────────────────────────────────────────────────────

def is_bull(c): return c["close"] > c["open"]
def is_bear(c): return c["close"] < c["open"]


def find_p1(candles):
    hits = []
    for i in range(len(candles) - 3):
        c1, c2, c3, c4 = candles[i], candles[i+1], candles[i+2], candles[i+3]
        if (is_bull(c1) and is_bear(c2)
                and c2["high"]  < c1["high"]
                and c2["close"] < c1["low"]
                and c3["high"]  < c1["high"]
                and is_bull(c3) and is_bear(c4)
                and c4["high"]  < c3["high"]
                and c4["close"] < c3["low"]):
            hits.append(i)
    return hits


def find_p2(candles):
    hits = []
    for i in range(len(candles) - 3):
        c1, c2, c3, c4 = candles[i], candles[i+1], candles[i+2], candles[i+3]
        if (is_bear(c1) and is_bull(c2)
                and c2["low"]   > c1["low"]
                and c2["close"] > c1["high"]
                and c3["low"]   > c1["low"]
                and is_bear(c3) and is_bull(c4)
                and c4["low"]   > c3["low"]
                and c4["close"] > c3["high"]):
            hits.append(i)
    return hits


def scan_symbol(args):
    worker, symbol, interval, n, mode = args
    candles = fetch_klines(worker, symbol, interval, n)
    if not candles or len(candles) < 4:
        return symbol, []
    results = []
    if mode in ("Pattern 1  (Bull→Bear)", "Both"):
        hits = find_p1(candles)
        if hits:
            results.append({
                "type": "Bull→Bear", "color": "#e05c2a", "icon": "🔻",
                "rule": "C2.high<C1.high · C2.close<C1.low · C3.high<C1.high",
                "count": len(hits),
                "labels": " · ".join(f"[C{i+1}C{i+2}|C{i+3}C{i+4}]" for i in hits),
            })
    if mode in ("Pattern 2  (Bear→Bull)", "Both"):
        hits = find_p2(candles)
        if hits:
            results.append({
                "type": "Bear→Bull", "color": "#21c354", "icon": "🔺",
                "rule": "C2.low>C1.low · C2.close>C1.high · C3.low>C1.low",
                "count": len(hits),
                "labels": " · ".join(f"[C{i+1}C{i+2}|C{i+3}C{i+4}]" for i in hits),
            })
    return symbol, results


# ── UI ─────────────────────────────────────────────────────────────────────────

st.set_page_config(page_title="Binance Pair Scanner", page_icon="🕯️", layout="wide")
st.title("🕯️ Binance Futures — Pair Pattern Scanner")

# ── Read worker URL (secrets → sidebar input) ──────────────────────────────────
default_worker = ""
try:
    default_worker = st.secrets["proxy"]["worker_url"].rstrip("/")
except Exception:
    pass

with st.sidebar:
    st.header("⚙️ Settings")

    worker_url = st.text_input(
        "Cloudflare Worker URL",
        value=default_worker,
        placeholder="https://YOUR-WORKER.YOUR-NAME.workers.dev",
    ).strip().rstrip("/")

    if st.button("🔌 Test connection", use_container_width=True):
        if not worker_url:
            st.warning("Enter a worker URL first.")
        else:
            with st.spinner("Pinging…"):
                ok = ping(worker_url)
            if ok:
                st.success("✅ Worker reachable!")
            else:
                st.error("❌ Worker did not respond.")

    st.divider()
    timeframe   = st.selectbox("Timeframe",         ["1H","4H","1D","1W"], index=1)
    n_candles   = st.selectbox("Lookback (candles)", [5,10,15,20],          index=1)
    mode        = st.radio("Pattern", [
        "Pattern 1  (Bull→Bear)",
        "Pattern 2  (Bear→Bull)",
        "Both",
    ], index=2)
    max_workers = st.slider("Threads", 1, 10, 5)

    st.divider()
    with st.expander("Pattern rules"):
        st.markdown("""
**P1 — Bull→Bear**
```
C1 bull · C2 bear
C2.high < C1.high
C2.close < C1.low
C3 bull · C3.high < C1.high
C4 bear · C4.high < C3.high
C4.close < C3.low
```
**P2 — Bear→Bull**
```
C1 bear · C2 bull
C2.low > C1.low
C2.close > C1.high
C3 bear · C3.low > C1.low
C4 bull · C4.low > C3.low
C4.close > C3.high
```
""")

# ── Guard: must have worker URL ────────────────────────────────────────────────
if not worker_url:
    st.info(
        "### Setup: Cloudflare Worker required\n\n"
        "Binance blocks all cloud server IPs. A free Cloudflare Worker proxies "
        "the requests from a Cloudflare edge IP that Binance allows.\n\n"
        "**Steps (5 minutes, free):**\n"
        "1. Go to [dash.cloudflare.com](https://dash.cloudflare.com) → sign up free\n"
        "2. **Workers & Pages** → **Create** → **Create Worker**\n"
        "3. Delete default code, paste `cloudflare_worker.js`, click **Deploy**\n"
        "4. Copy the worker URL shown (e.g. `https://binance-proxy.yourname.workers.dev`)\n"
        "5. Paste it in the **Cloudflare Worker URL** field in the sidebar\n\n"
        "Free tier: 100,000 requests/day — more than enough."
    )
    st.stop()

# ── Scan ───────────────────────────────────────────────────────────────────────
if st.button("▶ Start Scan", type="primary", use_container_width=True):
    interval = TF_MAP[timeframe]
    t0 = time.time()

    # Connection check
    status = st.empty()
    status.info(f"🔌 Connecting via worker…")
    if not ping(worker_url):
        status.error(
            "❌ Worker did not respond. "
            "Make sure you deployed `cloudflare_worker.js` and the URL is correct."
        )
        st.stop()
    status.success(f"✅ Connected via `{worker_url}`")

    # Fetch symbols
    with st.spinner("Fetching symbol list…"):
        try:
            symbols = get_all_symbols(worker_url)
        except Exception as e:
            st.error(f"Symbol fetch failed: {e}"); st.stop()

    total, scanned, hits_total = len(symbols), 0, 0
    prog   = st.progress(0.0, text="Starting…")
    info   = st.empty()
    st.markdown(f"### Results — `{timeframe}` · `{n_candles}` candles · `{mode}`")
    box = st.container()

    args_list = [(worker_url, s, interval, n_candles, mode) for s in symbols]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(scan_symbol, a): a[1] for a in args_list}
        for fut in concurrent.futures.as_completed(futs):
            sym, results = fut.result()
            scanned += 1
            for res in results:
                hits_total += 1
                with box:
                    st.markdown(f"""
<div style="border-left:4px solid {res['color']};padding:10px 16px;
margin-bottom:8px;border-radius:6px;background:rgba(128,128,128,0.05);
display:flex;align-items:center;gap:14px;flex-wrap:wrap;">
<span style="font-size:1.2em">{res['icon']}</span>
<b style="font-size:1.05em;min-width:130px">{sym}</b>
<span style="background:{res['color']};color:#fff;padding:2px 10px;
border-radius:4px;font-size:0.8em;font-weight:500">{res['type']}</span>
<span style="background:rgba(128,128,128,0.1);padding:2px 8px;
border-radius:4px;font-size:0.78em;color:gray;font-family:monospace">{res['rule']}</span>
<span style="color:gray;font-size:0.85em">
{res['count']} window{'s' if res['count']>1 else ''} · {res['labels']}
</span>
<span style="color:gray;font-size:0.8em;margin-left:auto">{timeframe} · N={n_candles}</span>
</div>""", unsafe_allow_html=True)

            prog.progress(scanned / total, text=f"{scanned}/{total} scanned…")
            info.caption(
                f"⏱ {time.time()-t0:.1f}s  |  "
                f"✅ {scanned}/{total}  |  🎯 {hits_total} matches"
            )

    prog.empty(); info.empty(); status.empty()
    st.success(
        f"✅ Done — {total} symbols · {time.time()-t0:.1f}s · "
        f"**{hits_total}** setup{'s' if hits_total!=1 else ''} found"
    )
    if hits_total == 0:
        st.info("No setups found. Try N=20 or a different timeframe.")
