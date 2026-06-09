"""
models.py — KWallet v2 Data Models
====================================
Models for a pan-East African multi-currency wallet with:
  - Country-aware wallet registration
  - 5 mandatory home currencies (KES, TZS, UGX, RWF, ETB)
  - 5 optional international currencies chosen by user
  - Fee tracking per transaction
  - Multi-rail payment method storage (M-Pesa, MTN MoMo, Airtel, bank)
  - Revenue/fee ledger for company accounting
  - USD-equivalent transaction limits (per-tx: $100, daily: $500 + $10/day growth)
"""

from django.db import models
from django.contrib.auth.models import User
from django.core.validators import RegexValidator
from django.core.exceptions import ValidationError
from decimal import Decimal
import secrets
from typing import List, Tuple, Dict, Any
from django.utils import timezone


# ── Shared phone-number validator ─────────────────────────────────────────────
# Kenyan format: starts with 07 or 01, exactly 10 digits total.
# Defined at module level so every model that stores a phone number can
# reference the same validator — Wallet, MpesaTransaction, and PaymentMethod
# phone-based rails all use this.
PHONE_REGEX = RegexValidator(
    regex=r'^(07|01)\d{8}$',
    message='Phone number must start with 07 or 01 and contain exactly 10 digits.',
)

# Rails that carry a phone number as their identifier (vs. a bank account number)
PHONE_RAILS = {
    'mpesa_ke', 'mpesa_tz',
    'mtn_ug', 'mtn_rw',
    'airtel_ke', 'airtel_tz', 'airtel_ug',
    'tigopesa',
    'telebirr',
}


# ── All supported currencies ──────────────────────────────────────────────────
ALL_CURRENCIES = [
    # East African home currencies
    ('KES', 'Kenyan Shilling'),
    ('TZS', 'Tanzanian Shilling'),
    ('UGX', 'Ugandan Shilling'),
    ('RWF', 'Rwandan Franc'),
    ('ETB', 'Ethiopian Birr'),
    # International currencies (user chooses 5)
    ('USD', 'US Dollar'),
    ('EUR', 'Euro'),
    ('GBP', 'British Pound'),
    ('JPY', 'Japanese Yen'),
    ('CNY', 'Chinese Yuan'),
    ('AED', 'UAE Dirham'),
    ('INR', 'Indian Rupee'),
    ('CAD', 'Canadian Dollar'),
    ('AUD', 'Australian Dollar'),
    ('CHF', 'Swiss Franc'),
    ('ZAR', 'South African Rand'),
    ('NGN', 'Nigerian Naira'),
    ('GHS', 'Ghanaian Cedi'),
    ('XOF', 'West African CFA Franc'),
    ('MUR', 'Mauritian Rupee'),
]

# Currencies every user gets automatically based on their country
COUNTRY_HOME_CURRENCY = {
    'KE': 'KES',
    'TZ': 'TZS',
    'UG': 'UGX',
    'RW': 'RWF',
    'ET': 'ETB',
}

# Every wallet gets these regardless of country
UNIVERSAL_CURRENCIES = ['KES', 'TZS', 'UGX', 'RWF', 'ETB']

INTERNATIONAL_CURRENCIES = [
    'USD', 'EUR', 'GBP', 'JPY', 'CNY',
    'AED', 'INR', 'CAD', 'AUD', 'CHF',
    'ZAR', 'NGN', 'GHS', 'XOF', 'MUR',
]

COUNTRY_CHOICES = [
    ('KE', 'Kenya'),
    ('TZ', 'Tanzania'),
    ('UG', 'Uganda'),
    ('RW', 'Rwanda'),
    ('ET', 'Ethiopia'),
]


# ── Default USD limits for new users ─────────────────────────────────────────
# All limits are expressed in USD-equivalent and compared against the
# USD value of the currency being transacted.

DEFAULT_PER_TRANSACTION_LIMIT_USD = Decimal('100.00')   # max single tx
DEFAULT_DAILY_LIMIT_USD           = Decimal('500.00')   # day-0 daily cap
DAILY_LIMIT_INCREMENT_USD         = Decimal('10.00')    # grows $10/day


# ====================== TIERED BRACKETS (M-Pesa Style - Applied to All) ======================

SEND_BRACKETS: List[Tuple[Decimal, Decimal, Decimal]] = [
    (Decimal('1'),     Decimal('49'),     Decimal('0')),
    (Decimal('50'),    Decimal('100'),    Decimal('0')),
    (Decimal('101'),   Decimal('500'),    Decimal('7')),
    (Decimal('501'),   Decimal('1000'),   Decimal('13')),
    (Decimal('1001'),  Decimal('1500'),   Decimal('23')),
    (Decimal('1501'),  Decimal('2500'),   Decimal('33')),
    (Decimal('2501'),  Decimal('3500'),   Decimal('53')),
    (Decimal('3501'),  Decimal('5000'),   Decimal('57')),
    (Decimal('5001'),  Decimal('7500'),   Decimal('78')),
    (Decimal('7501'),  Decimal('10000'),  Decimal('90')),
    (Decimal('10001'), Decimal('15000'),  Decimal('100')),
    (Decimal('15001'), Decimal('20000'),  Decimal('105')),
    (Decimal('20001'), Decimal('35000'),  Decimal('108')),
    (Decimal('35001'), Decimal('50000'),  Decimal('108')),
    (Decimal('50001'), Decimal('250000'), Decimal('108')),
]

WITHDRAW_BRACKETS: List[Tuple[Decimal, Decimal, Decimal]] = [
    (Decimal('50'),    Decimal('100'),    Decimal('11')),
    (Decimal('101'),   Decimal('500'),    Decimal('29')),
    (Decimal('501'),   Decimal('1000'),   Decimal('29')),
    (Decimal('1001'),  Decimal('1500'),   Decimal('29')),
    (Decimal('1501'),  Decimal('2500'),   Decimal('29')),
    (Decimal('2501'),  Decimal('3500'),   Decimal('52')),
    (Decimal('3501'),  Decimal('5000'),   Decimal('69')),
    (Decimal('5001'),  Decimal('7500'),   Decimal('87')),
    (Decimal('7501'),  Decimal('10000'),  Decimal('115')),
    (Decimal('10001'), Decimal('15000'),  Decimal('167')),
    (Decimal('15001'), Decimal('20000'),  Decimal('185')),
    (Decimal('20001'), Decimal('35000'),  Decimal('197')),
    (Decimal('35001'), Decimal('50000'),  Decimal('278')),
    (Decimal('50001'), Decimal('250000'), Decimal('309')),
]


def get_tiered_fee(amount: Decimal, brackets: List[Tuple[Decimal, Decimal, Decimal]]) -> Decimal:
    if amount <= 0:
        return Decimal('0')
    amount = amount.quantize(Decimal('0.01'))
    for min_amt, max_amt, fee in brackets:
        if min_amt <= amount <= max_amt:
            return fee
    return brackets[-1][2]


# ── Exchange fee tiers (USD-equivalent thresholds) ────────────────────────────
# Rate steps DOWN as the exchange amount grows — rewards larger transactions
# without punishing everyday users.
#
#   $0 – $500        → 1.50%   (standard rate, covers rate-cache spread risk)
#   $500.01 – $2,000 → 1.00%   (mid-tier discount)
#   $2,000.01 – $10k → 0.75%   (high-value discount)
#   $10,000.01+      → 0.50%   (wholesale / power-user rate)
#
# Each bracket: (usd_min, usd_max_or_None, rate_as_decimal)
# usd_max=None means unbounded (last bracket).

EXCHANGE_FEE_TIERS: List[Tuple[Decimal, Any, Decimal]] = [
    (Decimal('0'),        Decimal('500'),    Decimal('0.0150')),
    (Decimal('500.01'),   Decimal('2000'),   Decimal('0.0100')),
    (Decimal('2000.01'),  Decimal('10000'),  Decimal('0.0075')),
    (Decimal('10000.01'), None,              Decimal('0.0050')),
]


def get_exchange_fee_rate(usd_equivalent: Decimal) -> Decimal:
    """
    Returns the applicable percentage rate (as a Decimal fraction, e.g. 0.015)
    for a given USD-equivalent exchange amount.
    """
    for usd_min, usd_max, rate in EXCHANGE_FEE_TIERS:
        if usd_max is None:
            if usd_equivalent >= usd_min:
                return rate
        elif usd_min <= usd_equivalent <= usd_max:
            return rate
    return EXCHANGE_FEE_TIERS[0][2]  # fallback to highest rate


# ====================== FEE SCHEDULE ======================

FEE_SCHEDULE: Dict[str, Dict[str, Any]] = {
    # SEND / P2P Transfers — tiered flat fees (M-Pesa style)
    'mpesa_send':      {'type': 'tiered', 'brackets': SEND_BRACKETS,     'pct': Decimal('0')},
    'mtn_send':        {'type': 'tiered', 'brackets': SEND_BRACKETS,     'pct': Decimal('0')},
    'airtel_send':     {'type': 'tiered', 'brackets': SEND_BRACKETS,     'pct': Decimal('0')},
    'bank_transfer':   {'type': 'tiered', 'brackets': SEND_BRACKETS,     'pct': Decimal('0')},
    'p2p_send':        {'type': 'tiered', 'brackets': SEND_BRACKETS,     'pct': Decimal('0')},

    # WITHDRAWALS — tiered flat fees (M-Pesa style)
    'mpesa_withdraw':  {'type': 'tiered', 'brackets': WITHDRAW_BRACKETS, 'pct': Decimal('0')},
    'mtn_withdraw':    {'type': 'tiered', 'brackets': WITHDRAW_BRACKETS, 'pct': Decimal('0')},
    'airtel_withdraw': {'type': 'tiered', 'brackets': WITHDRAW_BRACKETS, 'pct': Decimal('0')},
    'bank_withdraw':   {'type': 'tiered', 'brackets': WITHDRAW_BRACKETS, 'pct': Decimal('0')},

    # DEPOSITS — free
    'mpesa_deposit':   {'type': 'percent', 'pct': Decimal('0.000'), 'min': Decimal('0')},
    'mtn_deposit':     {'type': 'percent', 'pct': Decimal('0.000'), 'min': Decimal('0')},
    'airtel_deposit':  {'type': 'percent', 'pct': Decimal('0.000'), 'min': Decimal('0')},
    'bank_deposit':    {'type': 'percent', 'pct': Decimal('0.000'), 'min': Decimal('0')},

    # EXCHANGE — tiered percentage based on USD-equivalent amount.
    # calculate_fee() handles the USD conversion internally.
    'exchange':        {'type': 'exchange_tiered'},
}


def calculate_fee(transaction_type: str, amount: Decimal, currency: str = 'USD') -> Decimal:
    """
    Calculate the fee for a transaction.

    For 'exchange' transactions the fee uses a tiered percentage that steps
    down as the USD-equivalent amount grows.  Pass `currency` so the function
    can convert to USD before selecting the tier.

    All other transaction types ignore `currency`.
    """
    schedule = FEE_SCHEDULE.get(transaction_type)
    if not schedule:
        return Decimal('0')

    amount = amount.quantize(Decimal('0.01'))

    if schedule['type'] == 'tiered':
        return get_tiered_fee(amount, schedule['brackets'])

    if schedule['type'] == 'exchange_tiered':
        # Convert source amount to USD to determine which tier applies,
        # then apply that rate to the original amount.
        usd_equiv = amount  # default: already USD
        if currency != 'USD':
            try:
                from . import rates as rate_service
                rates    = rate_service.get_rates()
                usd_rate = rates.get(f"{currency}_USD")
                if usd_rate:
                    usd_equiv = (amount * usd_rate).quantize(Decimal('0.01'))
            except Exception:
                pass  # fallback: treat as USD-equivalent for tier lookup
        rate = get_exchange_fee_rate(usd_equiv)
        return (amount * rate).quantize(Decimal('0.0001'))

    # Flat percentage (deposits, etc.)
    fee = (amount * schedule['pct']).quantize(Decimal('0.0001'))
    return max(fee, schedule.get('min', Decimal('0')))


# ── Wallet ────────────────────────────────────────────────────────────────────

class Wallet(models.Model):

    KYC_CHOICES = [
        ('pending',  'Pending'),
        ('verified', 'Verified'),
        ('rejected', 'Rejected'),
    ]

    wallet_id  = models.CharField(max_length=20, unique=True, editable=False, primary_key=True)
    user       = models.OneToOneField(User, on_delete=models.CASCADE, related_name='wallet')
    phone      = models.CharField(max_length=10, unique=True, validators=[PHONE_REGEX])
    pin_hash   = models.CharField(max_length=128)
    country    = models.CharField(max_length=2, choices=COUNTRY_CHOICES, default='KE')
    kyc_status = models.CharField(max_length=10, choices=KYC_CHOICES, default='pending')
    created_at = models.DateTimeField(auto_now_add=True)

    def clean(self):
        """Run field-level validators explicitly so phone format is enforced
        even when .save() is called directly (e.g. from management commands
        or the Django shell), not only via ModelForm."""
        super().clean()
        try:
            PHONE_REGEX(self.phone)
        except ValidationError as exc:
            raise ValidationError({'phone': exc.messages})

    def save(self, *args, **kwargs):
        # Always validate phone before persisting — guards programmatic saves
        # that bypass form validation.
        self.full_clean()
        if not self.wallet_id:
            self.wallet_id = 'kwl_' + secrets.token_hex(6)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.user.get_full_name()} [{self.country}] — {self.phone}"

    @property
    def home_currency(self):
        return COUNTRY_HOME_CURRENCY.get(self.country, 'KES')

    def get_balance(self, currency_code):
        try:
            return self.currency_balances.get(currency=currency_code).balance
        except CurrencyBalance.DoesNotExist:
            return Decimal('0.00')

    def get_all_balances(self):
        return {cb.currency: cb.balance for cb in self.currency_balances.all()}

    def get_active_currencies(self):
        return list(self.currency_balances.values_list('currency', flat=True))

    @property
    def days_since_registration(self) -> int:
        """Number of complete days since the wallet was created."""
        return (timezone.now().date() - self.created_at.date()).days


# ── CurrencyBalance ────────────────────────────────────────────────────────────

class CurrencyBalance(models.Model):
    wallet   = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='currency_balances')
    currency = models.CharField(max_length=3, choices=ALL_CURRENCIES)
    balance  = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal('0.0000'))
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('wallet', 'currency')
        ordering = ['currency']

    def __str__(self):
        return f"{self.wallet.phone} | {self.currency}: {self.balance}"


# ── PaymentMethod ─────────────────────────────────────────────────────────────

class PaymentMethod(models.Model):
    """
    Stores a user's registered payment methods (mobile money, bank, etc).
    Each method is tied to a specific rail and can be used for deposit/withdrawal.
    """
    RAIL_CHOICES = [
        ('mpesa_ke',      'M-Pesa Kenya'),
        ('mpesa_tz',      'M-Pesa Tanzania'),
        ('mtn_ug',        'MTN MoMo Uganda'),
        ('mtn_rw',        'MTN MoMo Rwanda'),
        ('airtel_ke',     'Airtel Money Kenya'),
        ('airtel_tz',     'Airtel Tanzania'),
        ('airtel_ug',     'Airtel Uganda'),
        ('tigopesa',      'Tigo Pesa Tanzania'),
        ('telebirr',      'Telebirr Ethiopia'),
        ('bank_pesalink', 'PesaLink Kenya'),
        ('bank_swift',    'SWIFT International'),
    ]

    wallet      = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='payment_methods')
    rail        = models.CharField(max_length=20, choices=RAIL_CHOICES)
    label       = models.CharField(max_length=60)       # e.g. "My Safaricom Line"
    identifier  = models.CharField(max_length=100)      # phone number or account number
    currency    = models.CharField(max_length=3)        # currency this rail operates in
    country     = models.CharField(max_length=2)
    is_verified = models.BooleanField(default=False)
    is_default  = models.BooleanField(default=False)
    added_at    = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('wallet', 'rail', 'identifier')

    def clean(self):
        """Validate that phone-based rails carry a properly formatted phone number."""
        super().clean()
        if self.rail in PHONE_RAILS:
            try:
                PHONE_REGEX(self.identifier)
            except ValidationError:
                raise ValidationError({
                    'identifier': (
                        f"The '{self.get_rail_display()}' rail requires a valid phone number "
                        f"(starts with 07 or 01, exactly 10 digits). Got: '{self.identifier}'."
                    )
                })

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.label} ({self.rail}) — {self.identifier}"


# ── Transaction ────────────────────────────────────────────────────────────────

class Transaction(models.Model):
    TYPE_CHOICES = [
        ('mpesa_deposit',   'M-Pesa Deposit'),
        ('mpesa_withdraw',  'M-Pesa Withdrawal'),
        ('mtn_deposit',     'MTN MoMo Deposit'),
        ('mtn_withdraw',    'MTN MoMo Withdrawal'),
        ('airtel_deposit',  'Airtel Deposit'),
        ('airtel_withdraw',  'Airtel Withdrawal'),
        ('bank_deposit',    'Bank Deposit'),
        ('bank_withdraw',   'Bank Withdrawal'),
        ('exchange',        'Currency Exchange'),
        ('p2p_send',        'P2P Send'),
        ('p2p_receive',     'P2P Receive'),
        ('fee',             'Platform Fee'),
    ]
    STATUS_CHOICES = [
        ('pending',   'Pending'),
        ('completed', 'Completed'),
        ('failed',    'Failed'),
        ('refunded',  'Refunded'),
    ]

    wallet           = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    amount           = models.DecimalField(max_digits=18, decimal_places=4)
    fee              = models.DecimalField(max_digits=18, decimal_places=4, default=Decimal('0'))
    currency         = models.CharField(max_length=3)
    status           = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    details          = models.TextField(blank=True)
    reference        = models.CharField(max_length=100, unique=True, blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = 'tx_' + secrets.token_hex(8)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.transaction_type} | {self.amount} {self.currency} | {self.status}"

    @property
    def net_amount(self):
        """Amount after fee deduction."""
        return self.amount - self.fee


# ── WalletLimit ────────────────────────────────────────────────────────────────

class WalletLimit(models.Model):
    """
    Stores USD-equivalent transaction limits for a wallet.

    Limits are defined and enforced in USD-equivalent terms:
      - per_transaction_limit_usd  : max single transaction value (default $100)
      - base_daily_limit_usd       : the user's day-0 daily cap (default $500)
      - daily_limit_increment_usd  : USD added to the daily cap each full day
                                     since registration (default $10/day)

    At enforcement time, the transaction's currency amount is first converted
    to its USD equivalent using live/cached exchange rates, then compared
    against these USD thresholds.
    """

    wallet = models.OneToOneField(
        Wallet, on_delete=models.CASCADE, related_name='limit'
    )

    # Per-transaction cap in USD-equivalent
    per_transaction_limit_usd = models.DecimalField(
        max_digits=18, decimal_places=4,
        default=DEFAULT_PER_TRANSACTION_LIMIT_USD,
        help_text="Maximum USD-equivalent value allowed for a single transaction.",
    )

    # Base daily cap in USD-equivalent (before growth increment is applied)
    base_daily_limit_usd = models.DecimalField(
        max_digits=18, decimal_places=4,
        default=DEFAULT_DAILY_LIMIT_USD,
        help_text="Starting daily limit in USD-equivalent (day 0).",
    )

    # How much the daily limit grows per day
    daily_limit_increment_usd = models.DecimalField(
        max_digits=18, decimal_places=4,
        default=DAILY_LIMIT_INCREMENT_USD,
        help_text="USD added to the daily cap for each full day since registration.",
    )

    created_at  = models.DateTimeField(auto_now_add=True)
    updated_at  = models.DateTimeField(auto_now=True)

    def __str__(self):
        return (
            f"{self.wallet.phone} limits | "
            f"per-tx: ${self.per_transaction_limit_usd} | "
            f"daily base: ${self.base_daily_limit_usd} + "
            f"${self.daily_limit_increment_usd}/day"
        )

    # ── computed properties ──────────────────────────────────────────────────

    @property
    def effective_daily_limit_usd(self) -> Decimal:
        """
        Current daily limit = base + (days since registration × increment).
        Example: day 0 → $500, day 1 → $510, day 30 → $800, etc.
        """
        days = self.wallet.days_since_registration
        return (
            self.base_daily_limit_usd
            + Decimal(days) * self.daily_limit_increment_usd
        ).quantize(Decimal('0.01'))

    # ── enforcement helpers ──────────────────────────────────────────────────

    def _to_usd(self, amount: Decimal, currency: str) -> Decimal:
        """Convert an amount in `currency` to its USD equivalent."""
        if currency == 'USD':
            return amount.quantize(Decimal('0.01'))
        from . import rates as rate_service
        rates = rate_service.get_rates()
        rate  = rates.get(f"{currency}_USD")
        if not rate:
            # Fallback: use hardcoded rates if live unavailable
            from .rates import USD_FALLBACK
            usd_rate = USD_FALLBACK.get(currency)
            if usd_rate:
                rate = (Decimal('1') / usd_rate).quantize(Decimal('0.000001'))
        if not rate:
            raise ValueError(f"Cannot convert {currency} to USD for limit check.")
        return (amount * rate).quantize(Decimal('0.01'))

    def check_per_transaction(self, amount: Decimal, currency: str) -> Tuple[bool, str]:
        """
        Returns (True, '') if the transaction amount is within the per-tx limit,
        or (False, error_message) if it exceeds the limit.
        """
        usd_equiv = self._to_usd(amount, currency)
        limit     = self.per_transaction_limit_usd
        if usd_equiv > limit:
            return False, (
                f"Transaction of {amount} {currency} (≈ ${usd_equiv} USD) "
                f"exceeds your single-transaction limit of ${limit} USD."
            )
        return True, ''

    def check_daily_limit(self, amount: Decimal, currency: str) -> Tuple[bool, str]:
        """
        Returns (True, '') if today's completed outgoing transactions plus this
        amount are within the effective daily limit, otherwise (False, error_message).

        Only outgoing transaction types count against the daily limit:
        withdrawals, p2p sends, and exchanges.
        """
        OUTGOING_TYPES = (
            'mpesa_withdraw', 'mtn_withdraw', 'airtel_withdraw', 'bank_withdraw',
            'p2p_send', 'exchange',
        )
        today = timezone.now().date()
        todays_txns = self.wallet.transactions.filter(
            created_at__date=today,
            status='completed',
            transaction_type__in=OUTGOING_TYPES,
        )

        # Sum today's outgoing value in USD-equivalent
        spent_usd = Decimal('0')
        for tx in todays_txns:
            try:
                spent_usd += self._to_usd(tx.amount, tx.currency)
            except ValueError:
                pass  # Skip if currency rate unavailable

        usd_equiv   = self._to_usd(amount, currency)
        daily_limit = self.effective_daily_limit_usd

        if spent_usd + usd_equiv > daily_limit:
            remaining = max(daily_limit - spent_usd, Decimal('0'))
            return False, (
                f"This transaction (≈ ${usd_equiv} USD) would exceed your daily limit "
                f"of ${daily_limit} USD. "
                f"You have approximately ${remaining} USD remaining today."
            )
        return True, ''

    def check_all(self, amount: Decimal, currency: str) -> Tuple[bool, str]:
        """
        Convenience method: runs both per-transaction and daily checks.
        Returns (True, '') only if both pass.
        """
        ok, err = self.check_per_transaction(amount, currency)
        if not ok:
            return False, err
        return self.check_daily_limit(amount, currency)


def create_default_wallet_limit(wallet) -> 'WalletLimit':
    """
    Creates a WalletLimit with default USD values for a newly registered wallet.
    Safe to call multiple times — uses get_or_create.
    """
    limit, _ = WalletLimit.objects.get_or_create(
        wallet=wallet,
        defaults={
            'per_transaction_limit_usd': DEFAULT_PER_TRANSACTION_LIMIT_USD,
            'base_daily_limit_usd':      DEFAULT_DAILY_LIMIT_USD,
            'daily_limit_increment_usd': DAILY_LIMIT_INCREMENT_USD,
        },
    )
    return limit


# ── FeeRecord ─────────────────────────────────────────────────────────────────

class FeeRecord(models.Model):
    """
    Tracks every fee collected by KWallet for company revenue accounting.
    Each fee-generating transaction creates one FeeRecord.
    Used for finance dashboard, tax reporting, and revenue analytics.
    """
    transaction  = models.OneToOneField(Transaction, on_delete=models.CASCADE, related_name='fee_record')
    wallet       = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='fees_paid')
    amount       = models.DecimalField(max_digits=18, decimal_places=4)
    currency     = models.CharField(max_length=3)
    fee_type     = models.CharField(max_length=20)  # matches transaction_type
    collected_at = models.DateTimeField(auto_now_add=True)
    settlement   = models.ForeignKey(
        'FeeSettlement',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='fee_records',
        help_text="Set when this fee is included in a settlement batch.",
    )

    class Meta:
        ordering = ['-collected_at']

    def __str__(self):
        return f"Fee {self.amount} {self.currency} | {self.fee_type} | {self.collected_at.date()}"


# ── MpesaTransaction ──────────────────────────────────────────────────────────

class MpesaTransaction(models.Model):
    wallet              = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='mpesa_transactions')
    phone               = models.CharField(max_length=10, validators=[PHONE_REGEX])
    amount              = models.DecimalField(max_digits=10, decimal_places=2)
    checkout_request_id = models.CharField(max_length=100, unique=True)
    merchant_request_id = models.CharField(max_length=100)
    direction           = models.CharField(max_length=10, choices=[('in', 'Deposit'), ('out', 'Withdrawal')])
    status              = models.CharField(max_length=10, default='pending')
    result_code         = models.CharField(max_length=10, blank=True)
    result_desc         = models.CharField(max_length=255, blank=True)
    mpesa_receipt       = models.CharField(max_length=50, blank=True)
    created_at          = models.DateTimeField(auto_now_add=True)
    updated_at          = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"M-Pesa {self.direction} | {self.amount} KES | {self.phone} | {self.status}"


# ── CompanyAccount ────────────────────────────────────────────────────────────

class CompanyAccount(models.Model):
    """
    Represents a real-world account that KWallet controls.

    There are two kinds:
      CLIENT_FLOAT  — the segregated account where all user deposits land and
                      all user withdrawals leave from.  The sum of all
                      CurrencyBalance rows for a given currency must never
                      exceed the balance of this account in that currency.

      COMPANY_REVENUE — the company's own operating account.  Fee sweeps
                        land here.  This money belongs to KWallet, not users.

    One CompanyAccount row per real account (e.g. one for the M-Pesa Paybill,
    one for the KES bank account, one for the USD Equity Bank account, …).
    """

    ACCOUNT_TYPE_CHOICES = [
        ('client_float',    'Client Float (Segregated)'),
        ('company_revenue', 'Company Revenue'),
    ]
    RAIL_CHOICES = [
        ('mpesa',      'M-Pesa Paybill / Till'),
        ('bank_kes',   'Bank — KES'),
        ('bank_usd',   'Bank — USD'),
        ('bank_other', 'Bank — Other Currency'),
        ('psp',        'PSP / Payment Partner'),
    ]

    name             = models.CharField(max_length=100, unique=True,
                           help_text="Human label, e.g. 'M-Pesa Client Float KES'")
    account_type     = models.CharField(max_length=20, choices=ACCOUNT_TYPE_CHOICES)
    rail             = models.CharField(max_length=20, choices=RAIL_CHOICES)
    currency         = models.CharField(max_length=3)
    identifier       = models.CharField(max_length=100,
                           help_text="Paybill/till number, IBAN, account number, etc.")
    # Ledger balance — updated by every PoolLedger entry.
    # This is KWallet's internal view of what the real account holds.
    # Should match the real account statement; reconciliation flags any gap.
    ledger_balance   = models.DecimalField(max_digits=18, decimal_places=4,
                           default=Decimal('0.0000'))
    is_active        = models.BooleanField(default=True)
    notes            = models.TextField(blank=True)
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['account_type', 'currency', 'name']

    def __str__(self):
        return f"[{self.get_account_type_display()}] {self.name} ({self.currency})"

    @property
    def total_user_liability(self) -> Decimal:
        """
        Sum of all CurrencyBalance rows for this currency.
        For client float accounts this should always be ≤ ledger_balance.
        """
        if self.account_type != 'client_float':
            return Decimal('0')
        from django.db.models import Sum
        result = CurrencyBalance.objects.filter(
            currency=self.currency
        ).aggregate(total=Sum('balance'))
        return (result['total'] or Decimal('0')).quantize(Decimal('0.0001'))

    @property
    def surplus(self) -> Decimal:
        """
        ledger_balance − total_user_liability.
        Positive = healthy buffer (includes unsettled fees).
        Negative = INSOLVENT — emergency.
        """
        return (self.ledger_balance - self.total_user_liability).quantize(Decimal('0.0001'))

    @property
    def is_solvent(self) -> bool:
        return self.surplus >= Decimal('0')


# ── PoolLedger ────────────────────────────────────────────────────────────────

class PoolLedger(models.Model):
    """
    An immutable double-entry record of every real-money movement affecting
    a CompanyAccount.

    Every time real money flows in or out of a real-world account, one row
    is written here.  The running sum of all PoolLedger rows for an account
    must equal CompanyAccount.ledger_balance.

    Entry types:
      deposit_in     — user deposited; real money landed in client float
      withdrawal_out — user withdrew; real money left client float
      fee_sweep_out  — accumulated fees moved from client float → company revenue
      fee_sweep_in   — same sweep, credit side hitting company revenue account
      fx_rebalance_out / fx_rebalance_in — manual FX rebalancing between accounts
      adjustment     — manual correction by an admin (always requires a note)
    """

    ENTRY_CHOICES = [
        ('deposit_in',       'Deposit In'),
        ('withdrawal_out',   'Withdrawal Out'),
        ('fee_sweep_out',    'Fee Sweep — Debit Client Float'),
        ('fee_sweep_in',     'Fee Sweep — Credit Company Revenue'),
        ('fx_rebalance_out', 'FX Rebalance — Out'),
        ('fx_rebalance_in',  'FX Rebalance — In'),
        ('adjustment',       'Manual Adjustment'),
    ]

    account         = models.ForeignKey(CompanyAccount, on_delete=models.PROTECT,
                          related_name='ledger_entries')
    entry_type      = models.CharField(max_length=20, choices=ENTRY_CHOICES)
    amount          = models.DecimalField(max_digits=18, decimal_places=4,
                          help_text="Always positive. Direction implied by entry_type.")
    currency        = models.CharField(max_length=3)
    balance_after   = models.DecimalField(max_digits=18, decimal_places=4,
                          help_text="CompanyAccount.ledger_balance after this entry.")
    # Links back to the transaction or settlement that triggered this entry
    transaction     = models.ForeignKey(Transaction, null=True, blank=True,
                          on_delete=models.SET_NULL, related_name='pool_entries')
    settlement      = models.ForeignKey('FeeSettlement', null=True, blank=True,
                          on_delete=models.SET_NULL, related_name='pool_entries')
    note            = models.TextField(blank=True)
    created_at      = models.DateTimeField(auto_now_add=True)
    created_by      = models.CharField(max_length=100, default='system',
                          help_text="'system', 'sweep_job', or admin username.")

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return (
            f"{self.entry_type} | {self.amount} {self.currency} "
            f"→ {self.account.name} | bal: {self.balance_after}"
        )


# ── FeeSettlement ─────────────────────────────────────────────────────────────

class FeeSettlement(models.Model):
    """
    Records one batch sweep of collected fees from the client float account
    to the company revenue account.

    One FeeSettlement covers all FeeRecord rows that were unsettled at the
    time the sweep ran.  After the sweep:
      - FeeRecord.settlement is set to this object
      - PoolLedger debit entry written against the client float account
      - PoolLedger credit entry written against the company revenue account
      - Both CompanyAccount.ledger_balance values are updated atomically

    Status flow:  pending → completed | failed
    """

    STATUS_CHOICES = [
        ('pending',   'Pending'),
        ('completed', 'Completed'),
        ('failed',    'Failed'),
    ]

    reference        = models.CharField(max_length=40, unique=True, editable=False)
    currency         = models.CharField(max_length=3)
    total_fees       = models.DecimalField(max_digits=18, decimal_places=4,
                           help_text="Sum of all FeeRecord amounts in this batch.")
    fee_count        = models.IntegerField(default=0,
                           help_text="Number of FeeRecord rows included.")
    from_account     = models.ForeignKey(CompanyAccount, on_delete=models.PROTECT,
                           related_name='sweeps_out',
                           help_text="Client float account being debited.")
    to_account       = models.ForeignKey(CompanyAccount, on_delete=models.PROTECT,
                           related_name='sweeps_in',
                           help_text="Company revenue account being credited.")
    status           = models.CharField(max_length=10, choices=STATUS_CHOICES,
                           default='pending')
    failure_reason   = models.TextField(blank=True)
    initiated_by     = models.CharField(max_length=100, default='system')
    created_at       = models.DateTimeField(auto_now_add=True)
    completed_at     = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.reference:
            self.reference = 'settle_' + secrets.token_hex(8)
        super().save(*args, **kwargs)

    def __str__(self):
        return (
            f"Settlement {self.reference} | {self.total_fees} {self.currency} "
            f"| {self.fee_count} fees | {self.status}"
        )


# NOTE: FeeRecord.settlement ForeignKey is defined directly on the model above.
# Access via:  fee_record.settlement  (may be None = unsettled)