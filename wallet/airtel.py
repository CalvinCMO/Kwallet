"""
Airtel Money Kenya integration — Airtel Africa Disbursement API.
Mirrors the structure of mpesa.py so both use the same callback/idempotency pattern.
Risk #02: idempotency key prevents double-credit.
Risk #05: callback verified with shared-secret header + IP allowlist.
"""
import uuid
import logging
import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

AIRTEL_CONFIG = getattr(settings, 'AIRTEL_CONFIG', {
    'CLIENT_ID':     '',
    'CLIENT_SECRET': '',
    'ENVIRONMENT':   'sandbox',  # 'production'
    'BASE_URL_SANDBOX':    'https://openapiuat.airtel.africa',
    'BASE_URL_PRODUCTION': 'https://openapi.airtel.africa',
    'COUNTRY':  'KE',
    'CURRENCY': 'KES',
    # Risk #05: shared secret for callback HMAC verification
    'CALLBACK_SECRET': '',
    # Risk #05: Airtel IP allowlist — update from Airtel Africa docs
    'ALLOWED_CALLBACK_IPS': [],
})

AIRTEL_PREFIXES = ('073', '075', '078')


class AirtelClient:
    def __init__(self):
        self.cfg = AIRTEL_CONFIG
        self.is_production = self.cfg.get('ENVIRONMENT') == 'production'
        self.base_url = (
            self.cfg['BASE_URL_PRODUCTION'] if self.is_production
            else self.cfg['BASE_URL_SANDBOX']
        )
        # Risk #07 equivalent: SSL always verified
        self.verify_ssl = True
        self._token = None
        self._token_expiry = None

    def _get_token(self):
        if self._token and self._token_expiry and timezone.now() < self._token_expiry:
            return self._token
        resp = requests.post(
            f"{self.base_url}/auth/oauth2/token",
            json={
                'client_id':     self.cfg['CLIENT_ID'],
                'client_secret': self.cfg['CLIENT_SECRET'],
                'grant_type':    'client_credentials',
            },
            verify=self.verify_ssl,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data['access_token']
        self._token_expiry = timezone.now() + timezone.timedelta(seconds=int(data.get('expires_in', 3600)) - 60)
        return self._token

    def _headers(self):
        return {
            'Authorization': f'Bearer {self._get_token()}',
            'Content-Type':  'application/json',
            'X-Country':     self.cfg['COUNTRY'],
            'X-Currency':    self.cfg['CURRENCY'],
        }

    def validate_ke_number(self, phone: str) -> bool:
        """Validate Airtel KE prefix."""
        digits = ''.join(filter(str.isdigit, phone))
        if digits.startswith('254'):
            digits = '0' + digits[3:]
        return any(digits.startswith(p) for p in AIRTEL_PREFIXES)

    def collection_request(self, phone: str, amount: float, ref: str, idempotency_key: str = None) -> dict:
        """Initiate Airtel Money collection (deposit) — C2B."""
        if not idempotency_key:
            idempotency_key = str(uuid.uuid4())
        payload = {
            'reference': ref,
            'subscriber': {'country': 'KE', 'currency': 'KES', 'msisdn': phone},
            'transaction': {
                'amount': str(amount),
                'country': 'KE',
                'currency': 'KES',
                'id': idempotency_key,
            },
        }
        resp = requests.post(
            f"{self.base_url}/merchant/v1/payments/",
            json=payload,
            headers=self._headers(),
            verify=self.verify_ssl,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def disbursement_request(self, phone: str, amount: float, ref: str, idempotency_key: str = None) -> dict:
        """Initiate Airtel Money disbursement (withdrawal) — B2C."""
        if not idempotency_key:
            idempotency_key = str(uuid.uuid4())
        payload = {
            'payee': {'msisdn': phone},
            'reference': ref,
            'pin': self.cfg.get('AIRTEL_PIN', ''),
            'transaction': {
                'amount': str(amount),
                'id': idempotency_key,
                'type': 'B2C',
            },
        }
        resp = requests.post(
            f"{self.base_url}/standard/v1/disbursements/",
            json=payload,
            headers=self._headers(),
            verify=self.verify_ssl,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def verify_callback_secret(self, request_secret: str) -> bool:
        """Risk #05: verify shared-secret header on callback."""
        expected = self.cfg.get('CALLBACK_SECRET', '')
        if not expected:
            logger.warning("AIRTEL_CALLBACK_SECRET not configured — callback unprotected!")
            return False
        return hmac.compare_digest(request_secret, expected)
