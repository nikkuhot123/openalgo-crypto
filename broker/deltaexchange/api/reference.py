# Delta Exchange reference data (non-account, non-market).
#
# Delta India settles in INR but quotes every derivative in USD: an option
# premium of 217.8 is USD, while GET /v2/wallet/balances reports balance_inr.
# Anything that mixes the two — risk-based position sizing above all — needs
# Delta's OWN conversion rate, not an external FX quote, or the sizing drifts
# away from the number the venue actually uses to compute margin.
#
# GET /v2/settings exposes it, unauthenticated:
#   result.fiat_to_usd.asset_to_fiat_value  ->  85
import os
import time

from broker.deltaexchange.api.baseurl import get_url
from utils.httpx_client import get_httpx_client
from utils.logging import get_logger

logger = get_logger(__name__)

# Delta moves this figure rarely (it is a reference rate, not a live tick), so
# an hour of cache keeps the strategies off the network without going stale.
_CACHE_TTL_SEC = 3600
_cache = {"rate": None, "fetched_at": 0.0}

# Only used when Delta is unreachable AND no override is set. Deliberately the
# last resort: a wrong constant here silently mis-sizes every auto-lot trade.
_FALLBACK_RATE = 85.0


def get_usd_inr_rate(force_refresh: bool = False) -> float:
    """Delta's own USD->INR reference rate, cached for an hour.

    Resolution order:
      1. DELTA_USD_INR_RATE env var (explicit operator override, no network)
      2. GET /v2/settings -> result.fiat_to_usd.asset_to_fiat_value
      3. cached value, even if expired
      4. _FALLBACK_RATE

    Returns a positive float; never raises.
    """
    override = os.getenv("DELTA_USD_INR_RATE")
    if override:
        try:
            val = float(override)
            if val > 0:
                return val
            logger.warning(f"[DeltaExchange] DELTA_USD_INR_RATE={override!r} is not positive; ignoring")
        except (TypeError, ValueError):
            logger.warning(f"[DeltaExchange] DELTA_USD_INR_RATE={override!r} is not a number; ignoring")

    now = time.monotonic()
    cached = _cache["rate"]
    if not force_refresh and cached and (now - _cache["fetched_at"]) < _CACHE_TTL_SEC:
        return cached

    try:
        client = get_httpx_client()
        response = client.get(get_url("/v2/settings"), timeout=15.0)
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}: {response.text[:200]}")

        payload = response.json()
        if not payload.get("success", False):
            raise ValueError(f"API error: {payload.get('error', {})}")

        rate = float(payload["result"]["fiat_to_usd"]["asset_to_fiat_value"])
        if rate <= 0:
            raise ValueError(f"non-positive rate {rate}")

        _cache["rate"] = rate
        _cache["fetched_at"] = now
        logger.info(f"[DeltaExchange] USD->INR reference rate from /v2/settings: {rate}")
        return rate

    except Exception as e:
        if cached:
            logger.warning(
                f"[DeltaExchange] USD->INR refresh failed ({e}); reusing stale rate {cached}"
            )
            return cached
        logger.error(
            f"[DeltaExchange] USD->INR unavailable ({e}); falling back to {_FALLBACK_RATE}. "
            f"Set DELTA_USD_INR_RATE to pin it explicitly."
        )
        return _FALLBACK_RATE
