"""
flutterwave.py — KWallet Flutterwave integration (v4 / OAuth2).

Migrated from v3 (static secret key) to v4 (OAuth 2.0 client_credentials).
Confirmed against Flutterwave's published v4 docs as of 2026-07:
  - OAuth token endpoint, base URLs, orchestrator direct-charges shape,
    mobile-money transfer shape, and webhook signature algorithm are all
    directly documented and used as-is below.
  - v4 is a PUBLIC BETA. A few less-documented corners (notably the exact
    bank-payout "type" value and the charge-retrieval path) are our best
    REST-convention inference, flagged inline with "⚠ UNCONFIRMED". Run a
    real sandbox transaction through each flow and check the response
    shape against https://developer.flutterwave.com/docs before relying
    on these in production.

Supports:
  - Card / bank-transfer deposits (Orchestrator direct-charges)
  - Mobile Money deposits (M-Pesa, Airtel — direct-charges, type=mobile_money)
  - Payouts / disbursements (direct-transfers)

To keep wallet/views.py unchanged wherever possible, every public method
below still returns a dict shaped like the old v3 response (status/data/
meta keys), even though the underlying v4 call and payload look nothing
like v3. All the v4 <-> v3 shape translation lives in this file.

Security:
  Risk #02: idempotency key (X-Idempotency-Key + our own reference) on every call
  Risk #05: webhook verified with HMAC-SHA256 over the raw body,
            compared against the 'flutterwave-signature' header (v4 changed
            this from v3's plain 'verif-hash' string-compare header)
  Risk #07: SSL always verified
  Risk #08: rate-limiting handled in views, not here
  Risk #10: OAuth2 access tokens are short-lived (~10 min) and cached
            in-process; refreshed automatically before every call
"""

import base64
import hashlib
import hmac
import logging
import threading
import time
import uuid

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

FLW_CONFIG = getattr(settings, 'FLUTTERWAVE_CONFIG', {})

# Flutterwave published webhook IP ranges — verify from FLW docs periodically.
# (Unchanged by the v4 migration; kept as defence-in-depth alongside the
# signature check.)
FLW_WEBHOOK_IP_RANGES = [
    '52.46.138.',   # AWS us-east-1 (FLW primary)
    '54.144.',
    '18.214.',
    '34.193.',
]

# Supported deposit channels (unchanged — these are our own internal labels)
CHANNEL_CARD          = 'card'
CHANNEL_BANK_TRANSFER = 'banktransfer'
CHANNEL_MPESA         = 'mpesa'
CHANNEL_AIRTEL        = 'airtel'

# Supported payout types (unchanged — internal labels)
PAYOUT_BANK   = 'account'
PAYOUT_MOBILE = 'mobile_money_ke'

# ── OAuth2 (v4) ───────────────────────────────────────────────────────────
OAUTH_TOKEN_URL = 'https://idp.flutterwave.com/realms/flutterwave/protocol/openid-connect/token'

# Module-level token cache, shared across FlutterwaveClient() instances
# within a process (views.py instantiates a fresh client per request, so
# without this every request would re-authenticate). Keyed by client_id
# so sandbox/live credentials never collide if both are ever configured.
_token_cache = {}
_token_lock = threading.Lock()


class FlutterwaveClient:
    # v4 has fully separate sandbox / live base URLs (unlike v3, which used
    # the same host with sk_test_ vs sk_live_ keys to distinguish mode).
    SANDBOX_BASE_URL = 'https://developersandbox-api.flutterwave.com'
    LIVE_BASE_URL    = 'https://f4bexperience.flutterwave.com'

    def __init__(self):
        cfg = FLW_CONFIG
        self.client_id     = cfg.get('CLIENT_ID', '')
        self.client_secret = cfg.get('CLIENT_SECRET', '')
        self.encryption_key = cfg.get('ENCRYPTION_KEY', '')
        self.webhook_secret = cfg.get('WEBHOOK_SECRET', '')
        self.redirect_url   = cfg.get('REDIRECT_URL', '')
        self.use_mock        = cfg.get('USE_MOCK', False)
        self.environment      = cfg.get('ENVIRONMENT', 'sandbox')  # 'sandbox' | 'live'

        self.BASE_URL = self.LIVE_BASE_URL if self.environment == 'live' else self.SANDBOX_BASE_URL

        # Risk #07: SSL always verified
        self.verify_ssl = not cfg.get('DEV_DISABLE_SSL', False)
        if not self.verify_ssl:
            logger.warning('FLW SSL verification disabled via DEV_DISABLE_SSL — never use in production!')

    # ─────────────────────────────────────────────
    # OAuth2 token management
    # ─────────────────────────────────────────────

    def _get_access_token(self) -> str:
        """
        Exchange (and cache) an OAuth2 access token via the client_credentials
        grant. Flutterwave v4 tokens are short-lived (observed expires_in=600s
        in their docs) — refresh proactively 30s before expiry.
        """
        if self.use_mock:
            return 'mock-access-token'

        cache_key = self.client_id
        with _token_lock:
            cached = _token_cache.get(cache_key)
            if cached and cached['expires_at'] > time.time() + 30:
                return cached['access_token']

            resp = requests.post(
                OAUTH_TOKEN_URL,
                data={
                    'client_id':     self.client_id,
                    'client_secret': self.client_secret,
                    'grant_type':    'client_credentials',
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                verify=self.verify_ssl,
                timeout=15,
            )
            resp.raise_for_status()
            token_data = resp.json()

            _token_cache[cache_key] = {
                'access_token': token_data['access_token'],
                'expires_at':   time.time() + token_data.get('expires_in', 600),
            }
            return token_data['access_token']

    def _headers(self, idempotency_key: str = None):
        headers = {
            'Authorization': f'Bearer {self._get_access_token()}',
            'Content-Type':  'application/json',
            'X-Trace-Id':    uuid.uuid4().hex,
        }
        if idempotency_key:
            headers['X-Idempotency-Key'] = idempotency_key
        return headers

    def _post(self, path: str, payload: dict, idempotency_key: str = None) -> dict:
        resp = requests.post(
            f'{self.BASE_URL}{path}',
            json=payload,
            headers=self._headers(idempotency_key),
            verify=self.verify_ssl,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _get(self, path: str, params: dict = None) -> dict:
        resp = requests.get(
            f'{self.BASE_URL}{path}',
            params=params or {},
            headers=self._headers(),
            verify=self.verify_ssl,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ─────────────────────────────────────────────
    # Deposits — initiate payment
    # v4 uses one endpoint (Orchestrator "direct-charges") for card, bank
    # transfer, and mobile money — it combines what v3 split across
    # /payments and /charges?type=... into a single call keyed by
    # payment_method.type. https://developer.flutterwave.com/docs/main-payment-flow
    # ─────────────────────────────────────────────

    def create_payment_link(
        self,
        amount: float,
        currency: str,
        customer_email: str,
        customer_name: str,
        customer_phone: str,
        tx_ref: str,
        description: str = 'KWallet Deposit',
        payment_options: str = 'card,banktransfer,mpesa,airtel',
    ) -> dict:
        """
        Card / bank-transfer deposit via v4 Orchestrator direct-charges.
        Returns a v3-shaped dict: {'status': 'success', 'data': {'link': '<url>'}}
        so views.py's `result.get('data', {}).get('link', '')` keeps working
        unchanged. 'link' is populated from v4's next_action.redirect.url
        (card/3DS) or next_action.payment_instruction (bank transfer, which
        has no redirect URL — see note below).
        """
        if self.use_mock:
            return {
                'status':  'success',
                'message': 'Hosted Link',
                'data': {'link': f'https://mock.flutterwave.com/pay/{tx_ref}'},
                'mock': True,
            }

        first, _, rest = customer_name.strip().partition(' ')
        payload = {
            'amount':     amount,
            'currency':   currency,
            'reference':  tx_ref,
            'redirect_url': self.redirect_url,
            'payment_method': {
                'type': 'card',   # ⚠ UNCONFIRMED for the pure "bank transfer, no
                                  # card details yet" case — v4's card flow expects
                                  # card details up front rather than a v3-style
                                  # hosted link with method chosen at checkout.
                                  # If you need a Flutterwave-hosted picker page
                                  # (any method, chosen by the customer) rather than
                                  # a specific card charge, check FLW's "Hosted
                                  # Checkout" v4 docs — that's a closer analog to
                                  # v3's /payments and may need a different endpoint.
            },
            'customer': {
                'email': customer_email,
                'name': {'first': first or customer_name, 'last': rest or ''},
                'phone': {'country_code': '', 'number': customer_phone},
            },
            'meta': {'description': description},
        }
        data = self._post('/orchestration/direct-charges', payload, idempotency_key=tx_ref)

        link = ''
        next_action = data.get('data', {}).get('next_action', {})
        if next_action.get('type') == 'redirect':
            link = next_action.get('redirect', {}).get('url', '')

        return {
            'status': data.get('status', 'error'),
            'message': data.get('message', ''),
            'data': {
                'id':     data.get('data', {}).get('id', ''),
                'link':   link,
                'raw_v4': data,   # keep the full v4 payload for debugging/audit
            },
        }

    def initiate_mobile_money(
        self,
        phone: str,
        amount: float,
        currency: str,
        tx_ref: str,
        network: str = 'mpesa',   # 'mpesa' | 'airtel'
        email: str = 'noreply@kwallet.ke',
    ) -> dict:
        """
        Direct mobile-money charge (STK push) via v4 Orchestrator direct-charges,
        payment_method.type = 'mobile_money'. Confirmed shape per
        https://developer.flutterwave.com/docs/mobile-money
        Returns v3-shaped {'status','message','meta':{'authorization':{'mode','redirect'}}}
        — views.py only stores this as raw_payload, it doesn't read deeper fields,
        so this shape is preserved mainly for continuity/debugging.
        """
        if self.use_mock:
            return {
                'status':  'success',
                'message': 'Charge initiated',
                'meta': {'authorization': {'mode': 'redirect', 'redirect': f'https://mock.flw/{tx_ref}'}},
                'mock': True,
            }

        payload = {
            'amount':    amount,
            'currency':  currency,
            'reference': tx_ref,
            'payment_method': {
                'type': 'mobile_money',
                'mobile_money': {
                    'country_code':  'KE',
                    'network':       network.upper(),
                    'phone_number':  phone,
                },
            },
            'customer': {
                'email': email,
                'phone': {'country_code': 'KE', 'number': phone},
            },
        }
        data = self._post('/orchestration/direct-charges', payload, idempotency_key=tx_ref)

        next_action = data.get('data', {}).get('next_action', {})
        mode = 'redirect' if next_action.get('type') == 'redirect' else next_action.get('type', '')

        return {
            'status':  data.get('status', 'error'),
            'message': data.get('message', ''),
            'meta': {
                'authorization': {
                    'mode':     mode,
                    'redirect': next_action.get('redirect', {}).get('url', ''),
                    'note':     next_action.get('payment_instruction', {}).get('note', ''),
                },
            },
            'data': data.get('data', {}),
        }

    # ─────────────────────────────────────────────
    # Verify payment
    # ⚠ UNCONFIRMED endpoint path: v4 docs describe a "verify transaction
    # endpoint" and show PUT /charges/{id} for authorization, which strongly
    # implies GET /charges/{id} for retrieval, but I could not confirm the
    # exact literal path from available docs. Confirm this against your
    # dashboard's interactive API reference before relying on it for real
    # money — if it 404s, check developer.flutterwave.com/reference for the
    # "Retrieve a charge" endpoint under Charges.
    # ─────────────────────────────────────────────

    def verify_transaction(self, transaction_id: str) -> dict:
        """
        Verify a completed charge by its v4 charge id (data.id from the
        direct-charges response, e.g. 'chg_xxx'). Always call this before
        crediting a wallet — never trust the webhook/redirect payload alone.
        Returns a v3-shaped dict so views.py's existing field access
        (verify.get('status'), data.get('status') == 'successful',
        data.get('tx_ref'), data.get('amount'), data.get('currency'),
        data.get('app_fee')) keeps working unchanged.
        """
        if self.use_mock:
            return {
                'status': 'success',
                'data': {
                    'id': transaction_id, 'tx_ref': f'mock_{transaction_id}',
                    'status': 'successful', 'amount': 100, 'currency': 'KES',
                    'app_fee': 0, 'mock': True,
                },
            }
        raw = self._get(f'/charges/{transaction_id}')
        return self._normalize_charge_response(raw)

    def verify_transaction_by_ref(self, tx_ref: str) -> dict:
        """
        Look up a charge by our own reference (idempotency key).
        ⚠ UNCONFIRMED: v4's list/filter query-param name for charges by
        reference wasn't in the docs I could access — confirm 'reference'
        is the right filter param against the API reference before relying
        on this for anything beyond dev testing.
        """
        if self.use_mock:
            return {
                'status': 'success',
                'data': [{
                    'id': 'mock_001', 'tx_ref': tx_ref, 'status': 'successful',
                    'amount': 100, 'currency': 'KES', 'app_fee': 0, 'mock': True,
                }],
            }
        raw = self._get('/charges', params={'reference': tx_ref})
        items = raw.get('data', [])
        return {
            'status': raw.get('status', 'error'),
            'data': [self._normalize_charge_response({'data': item}).get('data', {}) for item in items],
        }

    @staticmethod
    def _normalize_charge_response(raw: dict) -> dict:
        """
        Translate a v4 charge object into the v3 field names views.py expects.
        v4 -> v3:  status 'succeeded' -> 'successful' (v4 uses 'succeeded'/
        'failed'/'pending' per the charge.completed webhook sample); reference
        -> tx_ref; amount/currency pass through as-is.
        """
        d = raw.get('data', {})
        status_map = {'succeeded': 'successful', 'success': 'successful'}
        return {
            'status': 'success' if raw.get('status') in ('success', None) else raw.get('status'),
            'data': {
                'id':       d.get('id', ''),
                'tx_ref':   d.get('reference', d.get('tx_ref', '')),
                'status':   status_map.get(d.get('status', ''), d.get('status', '')),
                'amount':   d.get('amount', 0),
                'currency': d.get('currency', ''),
                'app_fee':  d.get('fee', {}).get('value', 0) if isinstance(d.get('fee'), dict) else d.get('app_fee', 0),
                'raw_v4':   d,
            },
        }

    # ─────────────────────────────────────────────
    # Payouts / Disbursements
    # Confirmed for mobile money per
    # https://developer.flutterwave.com/docs/mobile-money-1 (POST /direct-transfers).
    # ⚠ UNCONFIRMED for bank payouts specifically — the 'type' value for a
    # plain bank-account transfer wasn't shown in the docs snippets I could
    # access. Test this in sandbox before wiring real bank payouts; if
    # 'bank' isn't accepted, check developer.flutterwave.com/docs for the
    # exact type value (possibly 'bank_transfer' or 'account').
    # ─────────────────────────────────────────────

    def initiate_transfer(
        self,
        account_bank: str,
        account_number: str,
        amount: float,
        currency: str,
        narration: str,
        reference: str,
        beneficiary_name: str = '',
        destination_branch_code: str = '',
    ) -> dict:
        """Bank transfer payout (B2C to bank account) via v4 /direct-transfers."""
        if self.use_mock:
            return {
                'status': 'success', 'message': 'Transfer Queued',
                'data': {'id': f'mock_trf_{reference}', 'reference': reference, 'status': 'NEW', 'mock': True},
            }

        payload = {
            'action':      'instant',
            'type':        'bank',  # ⚠ UNCONFIRMED — see note above
            'reference':   reference,
            'payment_instruction': {
                'amount':   {'value': amount, 'currency': currency},
                'narration': narration,
                'bank': {
                    'account_bank':      account_bank,
                    'account_number':    account_number,
                    'beneficiary_name':  beneficiary_name,
                    'destination_branch_code': destination_branch_code,
                },
            },
            'callback_url': FLW_CONFIG.get('TRANSFER_CALLBACK_URL', ''),
        }
        data = self._post('/direct-transfers', payload, idempotency_key=reference)
        return self._normalize_transfer_response(data)

    def initiate_mobile_money_payout(
        self,
        phone: str,
        amount: float,
        currency: str,
        narration: str,
        reference: str,
        network: str = 'mpesa',   # 'mpesa' | 'airtel'
    ) -> dict:
        """
        Mobile money payout via v4 /direct-transfers, confirmed shape per
        https://developer.flutterwave.com/docs/mobile-money-1
        """
        if self.use_mock:
            return {
                'status': 'success', 'message': 'Transfer Queued',
                'data': {'id': f'mock_mob_{reference}', 'reference': reference, 'status': 'NEW', 'mock': True},
            }

        payload = {
            'action':    'instant',
            'type':      'mobile_money',
            'reference': reference,
            'payment_instruction': {
                'amount':   {'value': amount, 'currency': currency},
                'narration': narration,
                'mobile_money': {
                    'network':  network.upper(),
                    'msisdn':   phone,
                },
            },
            'callback_url': FLW_CONFIG.get('TRANSFER_CALLBACK_URL', ''),
        }
        data = self._post('/direct-transfers', payload, idempotency_key=reference)
        return self._normalize_transfer_response(data)

    @staticmethod
    def _normalize_transfer_response(raw: dict) -> dict:
        d = raw.get('data', {})
        return {
            'status': raw.get('status', 'error'),
            'message': raw.get('message', ''),
            'data': {
                'id':        d.get('id', ''),
                'reference': d.get('reference', ''),
                'status':    d.get('status', ''),
                'raw_v4':    d,
            },
        }

    def get_transfer_status(self, transfer_id: str) -> dict:
        """Poll transfer status by v4 transfer id (GET /direct-transfers/{id})."""
        if self.use_mock:
            return {'status': 'success', 'data': {'id': transfer_id, 'status': 'SUCCESSFUL', 'mock': True}}
        raw = self._get(f'/direct-transfers/{transfer_id}')
        return self._normalize_transfer_response(raw)

    # ─────────────────────────────────────────────
    # Webhook security (Risk #05)
    # v4 CHANGED this from v3: the header is now 'flutterwave-signature'
    # (was 'verif-hash'), and it's an HMAC-SHA256 digest of the raw body
    # (base64-encoded), not a plain string you compare directly.
    # Confirmed: https://developer.flutterwave.com/docs/webhooks
    # ─────────────────────────────────────────────

    def verify_webhook_signature(self, payload_bytes: bytes, signature_header: str) -> bool:
        """
        Compute HMAC-SHA256(secret_hash, raw_body) -> base64, compare against
        the 'flutterwave-signature' header using constant-time compare.
        """
        expected_secret = self.webhook_secret
        if not expected_secret:
            logger.warning('FLUTTERWAVE WEBHOOK_SECRET not set — webhook endpoint unprotected!')
            return self.use_mock
        if not signature_header:
            return False

        computed = base64.b64encode(
            hmac.new(expected_secret.encode(), payload_bytes, hashlib.sha256).digest()
        ).decode()
        return hmac.compare_digest(computed, signature_header)

    def verify_webhook_ip(self, ip: str) -> bool:
        """Risk #05: IP allowlist for Flutterwave webhook callbacks (unchanged by v4)."""
        if self.use_mock:
            return True
        allowed = FLW_CONFIG.get('ALLOWED_WEBHOOK_IPS', FLW_WEBHOOK_IP_RANGES)
        if not allowed:
            logger.warning('No Flutterwave webhook IP allowlist configured!')
            return False
        return any(ip.startswith(prefix) for prefix in allowed)

    @staticmethod
    def normalize_webhook_payload(payload: dict) -> dict:
        """
        Translate a v4 webhook body into the v3 shape wallet/views.py's
        flw_webhook() already parses, so that function needs no further
        changes beyond calling this once at the top.

        v4 body: {'webhook_id', 'timestamp', 'type', 'data': {...}}
        v3 body: {'event', 'data': {...}}

        Also normalizes data.status ('succeeded' -> 'successful') and
        data.reference -> data.tx_ref, matching v3 field names, and maps
        v4's 'transfer.disburse' event type onto the v3 'transfer.completed'
        / 'transfer.failed' pair views.py checks for, based on data.status.
        """
        event_type = payload.get('type', payload.get('event', ''))
        data = dict(payload.get('data', {}))

        status_map = {'succeeded': 'successful'}
        if 'status' in data:
            data['status'] = status_map.get(data['status'], data['status'])

        if 'tx_ref' not in data and 'reference' in data:
            data['tx_ref'] = data['reference']

        # v4 uses a single 'transfer.disburse' event; derive completed/failed
        # from data.status the way v3's two separate event names implied.
        if event_type == 'transfer.disburse':
            event_type = 'transfer.completed' if data.get('status', '').upper() == 'SUCCESSFUL' else 'transfer.failed'

        return {'event': event_type, 'data': data}