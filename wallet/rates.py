"""
rates.py — KWallet v2 Exchange Rate Service
============================================
Supports all East African + international currencies.
Three-layer fallback: cache → frankfurter.app → hardcoded rates.
"""

import logging
import requests
from decimal import Decimal, ROUND_HALF_UP
from django.core.cache import cache
from django.conf import settings

logger = logging.getLogger(__name__)

# All currencies KWallet supports
CURRENCIES = [
    'KES', 'TZS', 'UGX', 'RWF', 'ETB',
    'USD', 'EUR', 'GBP', 'JPY', 'CNY',
    'AED', 'INR', 'CAD', 'AUD', 'CHF',
    'ZAR', 'NGN', 'GHS', 'MUR',
]

# Frankfurter only covers major currencies — EA currencies fetched via USD cross
FRANKFURTER_CURRENCIES = ['USD', 'EUR', 'GBP', 'JPY', 'CNY', 'AED', 'INR', 'CAD', 'AUD', 'CHF', 'ZAR']
EA_CURRENCIES = ['KES', 'TZS', 'UGX', 'RWF', 'ETB', 'NGN', 'GHS', 'MUR']

CACHE_KEY   = 'kwallet_v2_exchange_rates'
CACHE_TTL   = getattr(settings, 'EXCHANGE_RATE_CACHE_TTL', 3600)
API_TIMEOUT = 8
API_BASE    = 'https://api.frankfurter.app'

# ── Hardcoded fallback rates (USD base) ──────────────────────────────────────
# EA currencies are IMF/CBK approximates — update quarterly
USD_FALLBACK = {
    'EUR': Decimal('0.9200'), 'GBP': Decimal('0.7800'),
    'JPY': Decimal('149.50'), 'CNY': Decimal('7.2400'),
    'AED': Decimal('3.6700'), 'INR': Decimal('83.200'),
    'CAD': Decimal('1.3600'), 'AUD': Decimal('1.5400'),
    'CHF': Decimal('0.8900'), 'ZAR': Decimal('18.800'),
    # East African currencies
    'KES': Decimal('130.00'), 'TZS': Decimal('2650.0'),
    'UGX': Decimal('3750.0'), 'RWF': Decimal('1320.0'),
    'ETB': Decimal('57.500'), 'NGN': Decimal('1550.0'),
    'GHS': Decimal('15.500'), 'MUR': Decimal('44.500'),
}


def _build_full_matrix(usd_rates: dict) -> dict:
    """
    Given USD→X rates, builds a complete N×N matrix of all pairs.
    Uses cross-rate formula: A→B = (USD→B) / (USD→A)
    """
    flat = {}
    all_rates = {**usd_rates, 'USD': Decimal('1.0')}
    currencies_in_rates = list(all_rates.keys())

    for base in currencies_in_rates:
        for target in currencies_in_rates:
            if base == target:
                continue
            try:
                rate = (all_rates[target] / all_rates[base]).quantize(
                    Decimal('0.000001'), rounding=ROUND_HALF_UP
                )
                flat[f"{base}_{target}"] = rate
            except Exception:
                pass
    return flat


def _fetch_from_api() -> dict | None:
    """Fetches live rates from frankfurter.app and builds full cross-rate matrix."""
    try:
        targets = ','.join(FRANKFURTER_CURRENCIES)
        response = requests.get(
            f"{API_BASE}/latest",
            params={'from': 'USD', 'to': targets},
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        usd_rates = {k: Decimal(str(v)) for k, v in data.get('rates', {}).items()}

        # Add EA currencies from fallback (updated less frequently)
        for curr in EA_CURRENCIES:
            if curr not in usd_rates and curr in USD_FALLBACK:
                usd_rates[curr] = USD_FALLBACK[curr]

        return _build_full_matrix(usd_rates)

    except requests.exceptions.Timeout:
        logger.warning("Frankfurter API timeout")
        return None
    except requests.RequestException as e:
        logger.warning(f"Frankfurter API connection error: {e}")
        return None
    except Exception as e:
        logger.warning(f"Rate parse error: {e}")
        return None


def get_rates() -> dict:
    """Public interface — always returns a usable rates dict."""
    cached = cache.get(CACHE_KEY)
    if cached:
        logger.debug("Exchange rates: cache hit")
        return cached

    logger.info("Exchange rates: fetching from frankfurter.app")
    live = _fetch_from_api()

    if live:
        cache.set(CACHE_KEY, live, timeout=CACHE_TTL)
        logger.info(f"Exchange rates: cached {len(live)} pairs for {CACHE_TTL}s")
        return live

    logger.warning("Exchange rates: using hardcoded fallback rates")
    return _build_full_matrix(USD_FALLBACK)


def convert(amount: Decimal, from_curr: str, to_curr: str) -> Decimal:
    """Convert between any two supported currencies."""
    if from_curr == to_curr:
        return amount
    rates = get_rates()
    rate  = rates.get(f"{from_curr}_{to_curr}")
    if not rate:
        raise ValueError(f"No rate for {from_curr} → {to_curr}")
    return (amount * rate).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)


def get_rates_for_display() -> dict:
    """Returns { 'USD': {'EUR': '0.9200', ...}, ... } for templates."""
    flat = get_rates()
    nested = {}
    for key, rate in flat.items():
        base, target = key.split('_', 1)
        nested.setdefault(base, {})[target] = f"{rate:.4f}"
    return nested


def get_portfolio_value(balances: dict, target_currency: str = 'USD') -> Decimal:
    """Converts all balances to a single target currency and sums them."""
    rates = get_rates()
    total = Decimal('0')
    for curr, cb in balances.items():
        if curr == target_currency:
            total += cb.balance
        else:
            rate = rates.get(f"{curr}_{target_currency}")
            if rate:
                total += cb.balance * rate
    return total.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def invalidate_cache():
    cache.delete(CACHE_KEY)
    logger.info("Exchange rate cache invalidated")


def get_supported_currencies():
    return CURRENCIES.copy()
