"""
M-Pesa Daraja API — STK Push Integration
=========================================
Aligned with KWallet models (updated):
  - Wallet.wallet_id  → CharField PK, format "kwl_<12 hex>"
  - Wallet.pin_hash   → bcrypt hash field
  - Wallet.kyc_status → 'pending' | 'verified' | 'rejected'
  - MpesaTransaction.wallet → ForeignKey(Wallet) — field named 'wallet', not 'wallet_id'
  - Transaction.wallet       → ForeignKey(Wallet)
  - Transaction.reference    → auto-generated "tx_<16 hex>"

Sandbox docs: https://developer.safaricom.co.ke/APIs/MpesaExpressSimulate
"""

import base64
import logging
import secrets
import urllib3
import requests
from datetime import datetime
from requests.auth import HTTPBasicAuth
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from django.conf import settings
from django.core.cache import cache

# Suppress InsecureRequestWarning in sandbox (Windows schannel SSL issue)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

SANDBOX_BASE    = 'https://sandbox.safaricom.co.ke'
PRODUCTION_BASE = 'https://api.safaricom.co.ke'

# OAuth token is cached to avoid fetching on every request.
# Daraja tokens expire in 3600s; we cache for 3500s to be safe.
TOKEN_CACHE_KEY = 'mpesa_oauth_token'
TOKEN_CACHE_TTL = 3500


# ── HTTP session with automatic retries ───────────────────────────────────────

def _make_session():
    """
    Returns a requests.Session configured with retry logic.
    Retries up to 3 times on server errors (500/502/503/504),
    with exponential backoff: waits 2s, 4s, 8s between attempts.
    """
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=['GET', 'POST'],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    session.mount('http://',  adapter)
    return session


# ── Mock helpers (used when USE_MOCK=True in settings) ───────────────────────

def _mock_stk_push(phone, amount):
    """
    Returns a fake successful STK Push response without hitting Safaricom.
    Used in local development when the sandbox is unreachable.
    The checkout_request_id starts with 'MOCK_' so the pending template
    detects it and shows the 'Simulate Payment Success' button instead
    of polling for a real callback.
    """
    checkout_id  = f"MOCK_{secrets.token_hex(8).upper()}"
    merchant_id  = f"MOCK_MR_{secrets.token_hex(4).upper()}"
    logger.info(f"[MOCK] STK Push → phone={phone} amount={amount} checkout_id={checkout_id}")
    return {
        'success':             True,
        'checkout_request_id': checkout_id,
        'merchant_request_id': merchant_id,
        'message':             f'[MOCK] STK Push simulated. Checkout ID: {checkout_id}',
        'mock':                True,
    }


def _mock_b2c(phone, amount):
    """
    Returns a fake successful B2C response without hitting Safaricom.
    The balance is deducted immediately by the view; this mock confirms
    the 'delivery' instantly.
    """
    conversation_id = f"MOCK_B2C_{secrets.token_hex(6).upper()}"
    logger.info(f"[MOCK] B2C → phone={phone} amount={amount} conv_id={conversation_id}")
    return {
        'success':         True,
        'conversation_id': conversation_id,
        'message':         f'[MOCK] Withdrawal of KES {amount} simulated to {phone}.',
        'mock':            True,
    }


# ── Main M-Pesa client ────────────────────────────────────────────────────────

class MpesaClient:
    """
    Handles all communication with the Safaricom Daraja API.

    Reads configuration from settings.MPESA_CONFIG:
        CONSUMER_KEY        — Daraja app consumer key
        CONSUMER_SECRET     — Daraja app consumer secret
        SHORTCODE           — Business shortcode (174379 for sandbox)
        PASSKEY             — STK Push passkey from Daraja portal
        CALLBACK_URL        — Public HTTPS URL Safaricom POSTs results to
        ENVIRONMENT         — 'sandbox' or 'production'
        USE_MOCK            — True skips all Safaricom calls (local dev)
        TIMEOUT             — Request timeout in seconds (default 60)

    Usage:
        client = MpesaClient()
        result = client.stk_push(phone='0712345678', amount=500)
        if result['success']:
            # store result['checkout_request_id'] in MpesaTransaction
    """

    def __init__(self):
        cfg = settings.MPESA_CONFIG
        self.consumer_key    = cfg['CONSUMER_KEY']
        self.consumer_secret = cfg['CONSUMER_SECRET']
        self.shortcode       = cfg['SHORTCODE']
        self.passkey         = cfg['PASSKEY']
        self.callback_url    = cfg['CALLBACK_URL']
        self.env             = cfg.get('ENVIRONMENT', 'sandbox')
        self.use_mock        = cfg.get('USE_MOCK', False)
        self.timeout         = cfg.get('TIMEOUT', 60)
        self.is_production   = (self.env == 'production')
        self.base_url        = PRODUCTION_BASE if self.is_production else SANDBOX_BASE
        # Use proper SSL verification in production.
        # In sandbox on Windows, Safaricom's cert revocation check fails
        # (schannel error 0x80092012) so we bypass it with verify=False.
        self.verify_ssl      = self.is_production

    # ── OAuth token ───────────────────────────────────────────────────────────

    def get_access_token(self):
        """
        Fetches a Daraja OAuth Bearer token using HTTP Basic Auth.

        The token is cached in Django's cache backend for TOKEN_CACHE_TTL
        seconds (3500s) to avoid a Safaricom round-trip on every API call.
        Daraja tokens expire after 3600 seconds.

        Returns:
            str  — the access token, or None if auth failed
        """
        # Return cached token if still valid
        cached = cache.get(TOKEN_CACHE_KEY)
        if cached:
            logger.debug("M-Pesa OAuth: using cached token")
            return cached

        session = _make_session()
        try:
            response = session.get(
                f"{self.base_url}/oauth/v1/generate?grant_type=client_credentials",
                auth=HTTPBasicAuth(self.consumer_key, self.consumer_secret),
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            token = response.json().get('access_token')

            if token:
                cache.set(TOKEN_CACHE_KEY, token, TOKEN_CACHE_TTL)
                logger.info("M-Pesa OAuth: new token obtained and cached")
            else:
                logger.error(f"M-Pesa OAuth: no token in response → {response.json()}")

            return token

        except requests.exceptions.Timeout:
            logger.error("M-Pesa OAuth: request timed out")
            return None
        except requests.RequestException as e:
            logger.error(f"M-Pesa OAuth error: {e}")
            return None

    # ── Private helpers ───────────────────────────────────────────────────────

    def _timestamp(self):
        """Returns current datetime in Daraja's required format: YYYYMMDDHHmmss"""
        return datetime.now().strftime('%Y%m%d%H%M%S')

    def _password(self, timestamp):
        """
        Generates the STK Push password.
        Formula: base64(shortcode + passkey + timestamp)
        Daraja uses this to verify the request originates from an authorised party.
        """
        raw = f"{self.shortcode}{self.passkey}{timestamp}"
        return base64.b64encode(raw.encode()).decode()

    def _normalize_phone(self, phone):
        """
        Normalises any Kenyan phone format to the 2547XXXXXXXX format
        that Daraja requires.

        Handles:
            07XXXXXXXX   → 2547XXXXXXXX
            01XXXXXXXX   → 2541XXXXXXXX
            +2547XXXXXXXX → 2547XXXXXXXX
            2547XXXXXXXX  → unchanged
        """
        phone = str(phone).strip().replace(' ', '').replace('-', '')
        if phone.startswith('+'):
            phone = phone[1:]
        if phone.startswith('07') or phone.startswith('01'):
            phone = '254' + phone[1:]
        return phone

    def _auth_headers(self, token):
        """Returns the standard Daraja API request headers."""
        return {
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
        }

    # ── STK Push (C2B deposit) ────────────────────────────────────────────────

    def stk_push(self, phone, amount, account_ref='KWallet', description='Wallet Deposit'):
        """
        Initiates a Lipa Na M-Pesa Online (STK Push) payment request.

        Safaricom sends a PIN prompt directly to the customer's phone.
        The customer enters their M-Pesa PIN to authorise the payment.
        Safaricom then POSTs the result to CALLBACK_URL.

        The view that calls this should:
          1. Check result['success']
          2. If True → create a MpesaTransaction record with:
                wallet   = request.wallet          (ForeignKey to Wallet)
                phone    = phone
                amount   = amount
                checkout_request_id = result['checkout_request_id']
                merchant_request_id = result['merchant_request_id']
                direction = 'in'
                status    = 'pending'
          3. Redirect to the pending page

        Args:
            phone (str)       — customer's M-Pesa phone number (any Kenyan format)
            amount (float)    — KES amount to charge (minimum 10, must be integer KES)
            account_ref (str) — shown on customer's M-Pesa confirmation SMS (max 12 chars)
            description (str) — internal description (max 13 chars)

        Returns:
            dict with keys:
                success (bool)
                checkout_request_id (str) — store this to track the payment
                merchant_request_id (str)
                message (str)             — user-facing message
        """
        # ── Mock mode: skip Safaricom entirely ────────────────────────
        if self.use_mock:
            return _mock_stk_push(phone, amount)

        # ── Real Daraja call ──────────────────────────────────────────
        token = self.get_access_token()
        if not token:
            return {
                'success': False,
                'message': (
                    'Could not authenticate with M-Pesa. '
                    'Check Consumer Key and Secret in settings.'
                )
            }

        timestamp = self._timestamp()
        phone_fmt = self._normalize_phone(phone)

        payload = {
            'BusinessShortCode': self.shortcode,
            'Password':          self._password(timestamp),
            'Timestamp':         timestamp,
            'TransactionType':   'CustomerPayBillOnline',
            'Amount':            int(float(amount)),  # Daraja requires integer KES
            'PartyA':            phone_fmt,
            'PartyB':            self.shortcode,
            'PhoneNumber':       phone_fmt,
            'CallBackURL':       self.callback_url,
            'AccountReference':  str(account_ref)[:12],
            'TransactionDesc':   str(description)[:13],
        }

        # Log payload without the password for security
        safe_payload = {k: v for k, v in payload.items() if k != 'Password'}
        logger.info(f"STK Push → phone={phone_fmt} amount={amount} payload={safe_payload}")

        session = _make_session()
        try:
            response = session.post(
                f"{self.base_url}/mpesa/stkpush/v1/processrequest",
                json=payload,
                headers=self._auth_headers(token),
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            data = response.json()
            logger.info(f"STK Push response: {data}")

            if str(data.get('ResponseCode')) == '0':
                return {
                    'success':             True,
                    'checkout_request_id': data['CheckoutRequestID'],
                    'merchant_request_id': data['MerchantRequestID'],
                    'message':             'STK Push sent! Check your phone and enter your M-Pesa PIN.',
                }
            else:
                err = data.get('errorMessage') or data.get('ResponseDescription', 'STK Push failed.')
                return {'success': False, 'message': f'M-Pesa error: {err}'}

        except requests.exceptions.Timeout:
            logger.error("STK Push: request timed out")
            return {
                'success': False,
                'message': (
                    'M-Pesa request timed out. '
                    'The Safaricom sandbox may be unreachable. '
                    'Enable USE_MOCK=True in settings to test locally.'
                ),
            }
        except requests.RequestException as e:
            logger.error(f"STK Push network error: {e}")
            return {'success': False, 'message': f'Network error: {e}'}

    # ── STK Query (manual status check) ──────────────────────────────────────

    def query_stk(self, checkout_request_id):
        """
        Manually queries Safaricom for the status of a pending STK Push.

        Used as a fallback when the callback hasn't arrived after ~60 seconds.
        The pending page calls this endpoint after polling the local DB status
        for 12 attempts (60s) without a callback being received.

        ResultCode meanings:
            '0'    → Success (payment received)
            '1032' → Cancelled by user
            '1037' → Timeout (user didn't respond)
            '2001' → Wrong PIN entered

        Args:
            checkout_request_id (str) — from the original stk_push() response

        Returns:
            dict with keys: success (bool), result_code (str), result_desc (str)
        """
        if self.use_mock:
            return {'success': True, 'result_code': '0', 'result_desc': '[MOCK] Success'}

        token = self.get_access_token()
        if not token:
            return {'success': False, 'result_code': '', 'result_desc': 'Auth failed'}

        timestamp = self._timestamp()
        payload = {
            'BusinessShortCode': self.shortcode,
            'Password':          self._password(timestamp),
            'Timestamp':         timestamp,
            'CheckoutRequestID': checkout_request_id,
        }

        session = _make_session()
        try:
            response = session.post(
                f"{self.base_url}/mpesa/stkpushquery/v1/query",
                json=payload,
                headers=self._auth_headers(token),
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            data        = response.json()
            result_code = str(data.get('ResultCode', ''))
            logger.info(f"STK Query [{checkout_request_id}]: {data}")
            return {
                'success':     result_code == '0',
                'result_code': result_code,
                'result_desc': data.get('ResultDesc', ''),
            }
        except requests.RequestException as e:
            logger.error(f"STK Query error: {e}")
            return {'success': False, 'result_code': '', 'result_desc': str(e)}

    # ── B2C Payment (wallet → M-Pesa withdrawal) ─────────────────────────────

    def b2c_payment(self, phone, amount, occasion='Wallet Withdrawal'):
        """
        Sends KES from the platform's business shortcode to a customer's M-Pesa.

        Used for withdrawals. The view should:
          1. Deduct the amount from wallet.KES balance BEFORE calling this
          2. Store the Transaction record with status='pending'
          3. The B2C result callback (/mpesa/b2c/result/) updates status to
             'completed' on success, or 'failed' and refunds balance on failure

        Production requirements (not needed in sandbox):
          - INITIATOR_NAME: your Daraja operator username
          - SECURITY_CREDENTIAL: your encrypted Initiator password
            (encrypt using Safaricom's public certificate)

        Args:
            phone (str)     — destination M-Pesa number (any Kenyan format)
            amount (float)  — KES amount to send
            occasion (str)  — description label (max 100 chars)

        Returns:
            dict with keys: success (bool), conversation_id (str), message (str)
        """
        if self.use_mock:
            return _mock_b2c(phone, amount)

        token = self.get_access_token()
        if not token:
            return {'success': False, 'message': 'Auth failed'}

        cfg = settings.MPESA_CONFIG

        # B2C callbacks go to separate endpoints from STK callbacks
        b2c_result_url  = self.callback_url.rstrip('/').rsplit('/callback', 1)[0] + '/b2c/result/'
        b2c_timeout_url = self.callback_url.rstrip('/').rsplit('/callback', 1)[0] + '/b2c/timeout/'

        payload = {
            'InitiatorName':      cfg.get('INITIATOR_NAME', 'testapi'),
            'SecurityCredential': cfg.get('SECURITY_CREDENTIAL', ''),
            'CommandID':          'BusinessPayment',
            'Amount':             int(float(amount)),
            'PartyA':             self.shortcode,
            'PartyB':             self._normalize_phone(str(phone)),
            'Remarks':            str(occasion)[:100],
            'QueueTimeOutURL':    b2c_timeout_url,
            'ResultURL':          b2c_result_url,
            'Occasion':           str(occasion)[:100],
        }

        logger.info(f"B2C → phone={phone} amount={amount}")

        session = _make_session()
        try:
            response = session.post(
                f"{self.base_url}/mpesa/b2c/v1/paymentrequest",
                json=payload,
                headers=self._auth_headers(token),
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            data = response.json()
            logger.info(f"B2C response: {data}")

            if str(data.get('ResponseCode')) == '0':
                return {
                    'success':         True,
                    'conversation_id': data.get('ConversationID', ''),
                    'message':         f'Withdrawal of KES {amount} initiated to {phone}.',
                }
            else:
                err = data.get('errorMessage') or data.get('ResponseDescription', 'B2C failed.')
                return {'success': False, 'message': f'M-Pesa B2C error: {err}'}

        except requests.exceptions.Timeout:
            logger.error("B2C: request timed out")
            return {'success': False, 'message': 'B2C request timed out.'}
        except requests.RequestException as e:
            logger.error(f"B2C error: {e}")
            return {'success': False, 'message': f'Network error: {e}'}

    # ── STK Callback Parser ───────────────────────────────────────────────────

    @staticmethod
    def parse_stk_callback(body):
        """
        Parses the raw JSON body that Safaricom POSTs to /mpesa/callback/
        after an STK Push completes or fails.

        The view receiving this callback should:
          1. Call this method with the parsed request body
          2. Look up MpesaTransaction by result['checkout_request_id']
             using MpesaTransaction.objects.filter(checkout_request_id=...)
             Note: field is 'wallet' (ForeignKey), not 'wallet_id'
          3. If result['success']:
               - Set mpesa_txn.status = 'completed'
               - Set mpesa_txn.mpesa_receipt = result['receipt']
               - Credit CurrencyBalance for 'KES' on mpesa_txn.wallet
               - Create a Transaction record:
                   wallet=mpesa_txn.wallet,  ← use 'wallet', not 'wallet_id'
                   transaction_type='mpesa_deposit',
                   currency='KES',
                   amount=result['amount']
             Else:
               - Set mpesa_txn.status = 'failed'

        Expected Safaricom body shape (success):
        {
          "Body": {
            "stkCallback": {
              "MerchantRequestID": "...",
              "CheckoutRequestID": "ws_CO_...",
              "ResultCode": 0,
              "ResultDesc": "The service request is processed successfully.",
              "CallbackMetadata": {
                "Item": [
                  {"Name": "Amount",             "Value": 100},
                  {"Name": "MpesaReceiptNumber", "Value": "QKA4ZXXX"},
                  {"Name": "TransactionDate",    "Value": 20241201120000},
                  {"Name": "PhoneNumber",        "Value": 254712345678}
                ]
              }
            }
          }
        }

        Args:
            body (dict) — parsed JSON from request.body

        Returns:
            dict with keys:
                success (bool)
                checkout_request_id (str)
                merchant_request_id (str)
                amount (Decimal | None)      — only on success
                receipt (str | None)         — M-Pesa receipt number, only on success
                phone (str)                  — only on success
                transaction_date (str)       — only on success
                result_code (str)
                result_desc (str)
        """
        try:
            stk         = body['Body']['stkCallback']
            result_code = stk.get('ResultCode')
            checkout_id = stk.get('CheckoutRequestID')
            merchant_id = stk.get('MerchantRequestID', '')

            if result_code != 0:
                return {
                    'success':             False,
                    'checkout_request_id': checkout_id,
                    'merchant_request_id': merchant_id,
                    'result_code':         str(result_code),
                    'result_desc':         stk.get('ResultDesc', 'Failed'),
                }

            items = {
                item['Name']: item.get('Value')
                for item in stk.get('CallbackMetadata', {}).get('Item', [])
            }
            return {
                'success':             True,
                'checkout_request_id': checkout_id,
                'merchant_request_id': merchant_id,
                'amount':              items.get('Amount'),
                'receipt':             items.get('MpesaReceiptNumber'),
                'phone':               str(items.get('PhoneNumber', '')),
                'transaction_date':    str(items.get('TransactionDate', '')),
                'result_code':         '0',
                'result_desc':         'Success',
            }
        except (KeyError, TypeError) as e:
            logger.error(f"STK callback parse error: {e} | body: {body}")
            return {'success': False, 'result_desc': 'Malformed callback body'}

    # ── B2C Result Parser ─────────────────────────────────────────────────────

    @staticmethod
    def parse_b2c_result(body):
        """
        Parses the JSON body Safaricom POSTs to /mpesa/b2c/result/
        after a B2C withdrawal completes or fails.

        The view receiving this should:
          1. Call this method with the parsed request body
          2. Look up Transaction by reference=result['conversation_id']
             using Transaction.objects.filter(reference=result['conversation_id'])
             Note: Transaction.wallet is a ForeignKey — use txn.wallet, not txn.wallet_id
          3. If result['success']:
               - Set txn.status = 'completed'
               - Update txn.details with receipt and receiver info
             Else:
               - Set txn.status = 'failed'
               - Refund: credit CurrencyBalance 'KES' on txn.wallet
          4. Always return JsonResponse({'ResultCode': 0, 'ResultDesc': 'Accepted'})
             so Safaricom doesn't retry the callback

        Expected Safaricom body shape (success):
        {
          "Result": {
            "ResultCode": 0,
            "ResultDesc": "...",
            "ConversationID": "AG_...",
            "TransactionID": "QKA4ZXXX",
            "ResultParameters": {
              "ResultParameter": [
                {"Key": "TransactionAmount",              "Value": 1000},
                {"Key": "TransactionReceipt",             "Value": "QKA4ZXXX"},
                {"Key": "ReceiverPartyPublicName",        "Value": "254712345678 - John Doe"},
                {"Key": "TransactionCompletedDateTime",   "Value": "24.12.2024 12:00:00"}
              ]
            }
          }
        }

        Args:
            body (dict) — parsed JSON from request.body

        Returns:
            dict with keys:
                success (bool)
                conversation_id (str)
                transaction_id (str)
                amount (float | None)    — only on success
                receipt (str | None)     — only on success
                receiver (str)           — only on success
                completed_at (str)       — only on success
                result_code (str)
                result_desc (str)
        """
        try:
            result      = body['Result']
            result_code = result.get('ResultCode')
            conv_id     = result.get('ConversationID', '')
            txn_id      = result.get('TransactionID', '')

            if result_code != 0:
                return {
                    'success':         False,
                    'conversation_id': conv_id,
                    'transaction_id':  txn_id,
                    'result_code':     str(result_code),
                    'result_desc':     result.get('ResultDesc', 'Failed'),
                }

            params = {
                p['Key']: p.get('Value')
                for p in result.get('ResultParameters', {}).get('ResultParameter', [])
            }
            return {
                'success':         True,
                'conversation_id': conv_id,
                'transaction_id':  txn_id,
                'amount':          params.get('TransactionAmount'),
                'receipt':         params.get('TransactionReceipt'),
                'receiver':        params.get('ReceiverPartyPublicName', ''),
                'completed_at':    params.get('TransactionCompletedDateTime', ''),
                'result_code':     '0',
                'result_desc':     'Success',
            }
        except (KeyError, TypeError) as e:
            logger.error(f"B2C result parse error: {e} | body: {body}")
            return {'success': False, 'result_desc': 'Malformed B2C result body'}
