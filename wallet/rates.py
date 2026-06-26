"""
rates.py — KWallet
Risk #01: stale fallback detection + ops alert.
Risk #13: primary + secondary provider with fallback stored in DB-compatible dict.
"""
import logging
import time
from django.core.cache import cache
from django.conf import settings

logger = logging.getLogger(__name__)

CACHE_KEY = 'kwallet_exchange_rates'
CACHE_TTL = 300        # 5 minutes
STALE_AFTER = 600      # 10 minutes — considered stale
_last_fetch_key = 'kwallet_rates_last_fetch'

# Risk #13: fallback rates stored centrally (not scattered in code).
# In production: move to a DB table so ops can update without redeploy.
USD_FALLBACK = {
    # East Africa — sourced from CBK/regional central banks
    'USD_KES': 130.00, 'KES_USD': 1/130.00,
    'USD_TZS': 2550.0, 'TZS_USD': 1/2550.0,
    'USD_UGX': 3750.0, 'UGX_USD': 1/3750.0,
    'USD_RWF': 1300.0, 'RWF_USD': 1/1300.0,
    'USD_ETB': 56.50,  'ETB_USD': 1/56.50,
    'USD_NGN': 1550.0, 'NGN_USD': 1/1550.0,
    'USD_GHS': 12.5,   'GHS_USD': 1/12.5,
    'USD_ZAR': 18.50,  'ZAR_USD': 1/18.50,
    # Major
    'USD_EUR': 0.92,   'EUR_USD': 1/0.92,
    'USD_GBP': 0.79,   'GBP_USD': 1/0.79,
    'USD_JPY': 149.0,  'JPY_USD': 1/149.0,
    'USD_CNY': 7.25,   'CNY_USD': 1/7.25,
    'USD_AED': 3.67,   'AED_USD': 1/3.67,
    'USD_INR': 83.0,   'INR_USD': 1/83.0,
    'USD_CAD': 1.36,   'CAD_USD': 1/1.36,
    'USD_AUD': 1.55,   'AUD_USD': 1/1.55,
    'USD_CHF': 0.89,   'CHF_USD': 1/0.89,
}

PRIMARY_PROVIDERS = [
    'https://api.frankfurter.app/latest?from=USD',
    'https://open.er-api.com/v6/latest/USD',   # Risk #13: secondary provider
]


def _fetch_from_api():
    """Tries each provider in order, returns USD-based rates dict or None."""
    import requests
    for url in PRIMARY_PROVIDERS:
        try:
            resp = requests.get(url, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            # Both providers return {'rates': {...}} structure
            usd_rates = data.get('rates', {})
            if usd_rates:
                logger.debug(f'Rates fetched from {url}')
                return usd_rates
        except Exception as e:
            logger.warning(f'Rate provider {url} failed: {e}')
    return None


def _build_cross_rates(usd_rates: dict) -> dict:
    """Build all cross-pairs from USD base rates."""
    all_currencies = list(usd_rates.keys()) + ['USD']
    rates = {}
    for f in all_currencies:
        for t in all_currencies:
            if f == t:
                continue
            f_usd = 1.0 if f == 'USD' else (1.0 / usd_rates[f] if f in usd_rates else None)
            t_usd = 1.0 if t == 'USD' else usd_rates.get(t)
            if f_usd and t_usd:
                rates[f'{f}_{t}'] = round(f_usd * t_usd, 8)
    return rates


def get_rates() -> dict:
    """
    Returns exchange rate dict. Falls back to hardcoded rates with an alert.
    Risk #01: if using fallback, cache is marked stale and ops are notified.
    """
    cached = cache.get(CACHE_KEY)
    if cached:
        return cached

    usd_rates = _fetch_from_api()

    if usd_rates:
        # Merge with our EA currencies that may not be in the API (Risk #13)
        for pair, val in USD_FALLBACK.items():
            parts = pair.split('_')
            if len(parts) == 2 and parts[0] == 'USD':
                usd_rates.setdefault(parts[1], val)
        rates = _build_cross_rates(usd_rates)
        cache.set(CACHE_KEY, rates, CACHE_TTL)
        cache.set(_last_fetch_key, time.time(), CACHE_TTL * 2)
        return rates
    else:
        # Risk #01: stale fallback — alert ops
        logger.error('RATE ALERT: All rate providers failed. Serving fallback rates. Exchanges >KES 5,000 will be blocked.')
        try:
            from django.core.mail import mail_admins
            mail_admins(
                subject='[KWallet] Exchange rate API unreachable — using fallback',
                message='Both rate providers are unreachable. Fallback rates are in use. Exchanges above KES 5,000 are blocked.',
            )
        except Exception:
            pass
        rates = _build_cross_rates({k.replace('USD_', ''): v for k, v in USD_FALLBACK.items() if k.startswith('USD_')})
        # Short TTL on fallback so we retry sooner
        cache.set(CACHE_KEY, rates, 60)
        return rates


def get_pair_rate(from_curr: str, to_curr: str) -> float:
    if from_curr == to_curr:
        return 1.0  # Identity rate — no conversion needed
    rates = get_rates()
    key   = f'{from_curr}_{to_curr}'
    if key in rates:
        return float(rates[key])
    # Fallback via USD
    f_usd = rates.get(f'{from_curr}_USD')
    usd_t = rates.get(f'USD_{to_curr}')
    if f_usd and usd_t:
        return float(f_usd) * float(usd_t)
    raise ValueError(f'No rate for {from_curr}/{to_curr}')


def rates_are_stale() -> bool:
    """Risk #01: return True when on fallback (no live fetch recently)."""
    last = cache.get(_last_fetch_key)
    if not last:
        return True
    return (time.time() - last) > STALE_AFTER
