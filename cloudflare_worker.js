// Cloudflare Worker — Binance Futures API proxy
// Deploy at: https://dash.cloudflare.com → Workers & Pages → Create Worker
// Paste this entire file, click Deploy.
// Your worker URL will be: https://YOUR-WORKER-NAME.YOUR-SUBDOMAIN.workers.dev

const BINANCE_BASE = "https://fapi.binance.com";

// Only allow these paths to prevent abuse
const ALLOWED_PATHS = [
  "/fapi/v1/ping",
  "/fapi/v1/exchangeInfo",
  "/fapi/v1/klines",
];

export default {
  async fetch(request, env, ctx) {

    // Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: corsHeaders(),
      });
    }

    const url = new URL(request.url);
    const path = url.pathname;

    // Validate path is allowed
    const allowed = ALLOWED_PATHS.some((p) => path.startsWith(p));
    if (!allowed) {
      return new Response(JSON.stringify({ error: "Path not allowed" }), {
        status: 403,
        headers: { "Content-Type": "application/json", ...corsHeaders() },
      });
    }

    // Build Binance URL — forward all query params unchanged
    const binanceUrl = `${BINANCE_BASE}${path}${url.search}`;

    try {
      const binanceResp = await fetch(binanceUrl, {
        method: "GET",
        headers: {
          "User-Agent": "Mozilla/5.0",
          "Accept": "application/json",
        },
      });

      const body = await binanceResp.text();

      return new Response(body, {
        status: binanceResp.status,
        headers: {
          "Content-Type": "application/json",
          "Cache-Control": "no-store",
          ...corsHeaders(),
        },
      });

    } catch (err) {
      return new Response(JSON.stringify({ error: err.message }), {
        status: 502,
        headers: { "Content-Type": "application/json", ...corsHeaders() },
      });
    }
  },
};

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}
