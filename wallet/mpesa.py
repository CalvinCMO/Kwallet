"""
mpesa.py — KWallet Safaricom Daraja integration.
Risk #07: SSL always verified; disabled only via explicit DEV_DISABLE_SSL flag.
Risk #05: callback IP allowlist + HMAC secret header verification.
"""
import base64
import hashlib
import hmac
import logging
import time
from datetime import datetime

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

MPESA_CONFIG = getattr(settings, 'MPESA_CONFIG', {})

# Risk #05: Published Safaricom callback IP ranges (update from Daraja docs periodically)
SAFARICOM_IP_RANGES = [
    '196.201.214.', '196.201.213.',
    '125.159.20.', '125.159.22.',
]

TOKEN_CACHE_KEY = 'mpesa_oauth_token'


class MpesaClient:
    def __init__(self):
        cfg = MPESA_CONFIG
        self.consumer_key    = cfg.get('CONSUMER_KEY', '')
        self.consumer_secret = cfg.get('CONSUMER_SECRET', '')
        self.shortcode       = cfg.get('SHORTCODE', '')
        self.passkey         = cfg.get('PASSKEY', '')
        self.b2c_initiator   = cfg.get('B2C_INITIATOR', '')
        self.b2c_credential  = cfg.get('B2C_SECURITY_CREDENTIAL', '')
        self.callback_url    = cfg.get('CALLBACK_URL', '')
        self.b2c_callback_url= cfg.get('B2C_RESULT_URL', '')
        self.callback_secret = cfg.get('CALLBACK_SECRET', '')
        self.use_mock        = cfg.get('USE_MOCK', False)
        self.is_production   = cfg.get('ENVIRONMENT', 'sandbox') == 'production'
        # Risk #07: SSL always True unless explicit dev override — never based on environment alone
        self.verify_ssl      = not cfg.get('DEV_DISABLE_SSL', False)
        if not self.verify_ssl:
            logger.warning('SSL verification disabled via DEV_DISABLE_SSL — never use in production!')
        self.base_url = (
            'https://api.safaricom.co.ke' if self.is_production
            else 'https://sandbox.safaricom.co.ke'
        )

    def _get_token(self):
        """Cache OAuth token in Redis/cache to survive restarts (Risk #10)."""
        cached = cache.get(TOKEN_CACHE_KEY)
        if cached:
            return cached
        credentials = base64.b64encode(
            f'{self.consumer_key}:{self.consumer_secret}'.encode()
        ).decode()
        resp = requests.get(
            f'{self.base_url}/oauth/v1/generate?grant_type=client_credentials',
            headers={'Authorization': f'Basic {credentials}'},
            verify=self.verify_ssl,
            timeout=10,
        )
        resp.raise_for_status()
        token = resp.json()['access_token']
        cache.set(TOKEN_CACHE_KEY, token, timeout=3540)  # ~59 min
        return token

    def _password(self):
        ts  = datetime.now().strftime('%Y%m%d%H%M%S')
        raw = f'{self.shortcode}{self.passkey}{ts}'
        return base64.b64encode(raw.encode()).decode(), ts

    def stk_push(self, phone, amount, account_ref, transaction_desc='KWallet'):
        if self.use_mock:
            return {'CheckoutRequestID': f'mock_{int(time.time())}', 'MerchantRequestID': 'mock'}

        password, timestamp = self._password()
        payload = {
            'BusinessShortCode': self.shortcode,
            'Password':          password,
            'Timestamp':         timestamp,
            'TransactionType':   'CustomerPayBillOnline',
            'Amount':            int(amount),
            'PartyA':            phone,
            'PartyB':            self.shortcode,
            'PhoneNumber':       phone,
            'CallBackURL':       self.callback_url,
            'AccountReference':  account_ref,
            'TransactionDesc':   transaction_desc,
        }
        resp = requests.post(
            f'{self.base_url}/mpesa/stkpush/v1/processrequest',
            json=payload,
            headers={'Authorization': f'Bearer {self._get_token()}'},
            verify=self.verify_ssl,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def b2c_payment(self, phone, amount, remarks):
        if self.use_mock:
            return {'ConversationID': f'mock_b2c_{int(time.time())}'}

        payload = {
            'InitiatorName':          self.b2c_initiator,
            'SecurityCredential':     self.b2c_credential,
            'CommandID':              'BusinessPayment',
            'Amount':                 int(amount),
            'PartyA':                 self.shortcode,
            'PartyB':                 phone,
            'Remarks':                remarks,
            'QueueTimeOutURL':        self.b2c_callback_url,
            'ResultURL':              self.b2c_callback_url,
            'Occasion':               '',
        }
        resp = requests.post(
            f'{self.base_url}/mpesa/b2c/v1/paymentrequest',
            json=payload,
            headers={'Authorization': f'Bearer {self._get_token()}'},
            verify=self.verify_ssl,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def verify_callback_ip(self, ip: str) -> bool:
        """Risk #05: only accept callbacks from Safaricom IP ranges."""
        if self.use_mock:
            return True
        if not SAFARICOM_IP_RANGES:
            logger.warning('No Safaricom IP allowlist configured!')
            return False
        return any(ip.startswith(prefix) for prefix in SAFARICOM_IP_RANGES)

    def verify_callback_secret(self, provided_secret: str) -> bool:
        """Risk #05: HMAC shared-secret header check."""
        expected = self.callback_secret
        if not expected:
            logger.warning('MPESA_CALLBACK_SECRET not set — callback endpoint unprotected!')
            return self.use_mock  # only allow in mock mode if not configured
        return hmac.compare_digest(provided_secret, expected)
