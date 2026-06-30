import hmac
import hashlib
import re
from django.db import models
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager
from django.utils import timezone
from django.conf import settings
import bcrypt


# ─────────────────────────────────────────────
# Risk #14: max 10 currencies per wallet
MAX_CURRENCIES = 10

TRANSACTION_TYPES = [
    ('mpesa_deposit',   'M-Pesa Deposit'),
    ('mpesa_withdraw',  'M-Pesa Withdrawal'),
    ('airtel_deposit',  'Airtel Money Deposit'),
    ('airtel_withdraw', 'Airtel Money Withdrawal'),
    ('bank_deposit',    'Bank Deposit'),
    ('bank_withdraw',   'Bank Withdrawal'),
    ('flw_card_deposit',   'Card Deposit (Flutterwave)'),
    ('flw_mobile_deposit', 'Mobile Money Deposit (Flutterwave)'),
    ('flw_bank_deposit',   'Bank Transfer Deposit (Flutterwave)'),
    ('flw_bank_payout',    'Bank Payout (Flutterwave)'),
    ('flw_mobile_payout',  'Mobile Money Payout (Flutterwave)'),
    ('exchange',        'Currency Exchange'),
    ('p2p_send',        'Transfer Sent'),
    ('p2p_receive',     'Transfer Received'),
]

TRANSACTION_STATUSES = [
    ('pending',   'Pending'),
    ('completed', 'Completed'),
    ('failed',    'Failed'),
    ('refunded',  'Refunded'),
]

KYC_STATUSES = [
    ('pending',  'Pending'),
    ('verified', 'Verified'),
    ('rejected', 'Rejected'),
]

# Tiered M-Pesa / Airtel withdrawal fees (KES)
WITHDRAW_BRACKETS = [
    (50,    100,   11),
    (101,   500,   29),
    (501,   1500,  29),
    (1501,  2500,  29),
    (2501,  3500,  52),
    (3501,  5000,  69),
    (5001,  7500,  87),
    (7501,  10000, 115),
    (10001, 15000, 167),
    (15001, 20000, 185),
    (20001, 35000, 197),
    (35001, 50000, 278),
    (50001, 250000,309),
]

# Tiered P2P send fees (KES)
SEND_BRACKETS = [
    (1,     100,   0),
    (101,   500,   7),
    (501,   1000,  13),
    (1001,  1500,  23),
    (1501,  2500,  33),
    (2501,  3500,  53),
    (3501,  5000,  57),
    (5001,  7500,  78),
    (7501,  10000, 90),
    (10001, 15000, 100),
    (15001, 20000, 105),
    (20001, 250000,108),
]

# Bank flat fee
BANK_WITHDRAW_FEE = 50

# AML daily withdrawal limit (Risk #16) — kept as the global fallback / legacy reference
DAILY_WITHDRAW_LIMIT = 70_000  # KES

# Exchange rate fallback limits (Risk #01)
STALE_RATE_MAX_EXCHANGE = 5_000  # KES equivalent

# ─────────────────────────────────────────────
# Progressive withdrawal limit tiers (Risk #15 / #16)
#
# Tier 0 — New unverified wallet (<30 days, no flags)
# Tier 1 — Established unverified wallet (30+ days, no flags)
# Tier 2 — KYC verified wallet (<90 days since verification)
# Tier 3 — Fully verified wallet (90+ days since KYC approval, no flags)
#
# Each tuple: (daily_kes, monthly_kes, per_txn_kes, label)
LIMIT_TIERS = {
    0: dict(daily=10_000,   monthly=300_000,   per_txn=10_000,   label='New (Unverified)'),
    1: dict(daily=30_000,   monthly=500_000,   per_txn=30_000,   label='Established (Unverified)'),
    2: dict(daily=70_000,   monthly=1_000_000, per_txn=150_000,  label='KYC Verified'),
    3: dict(daily=150_000,  monthly=3_000_000, per_txn=300_000,  label='Fully Verified'),
}

# Days thresholds for tier progression
TIER1_DAYS   = 30   # days of good standing before unverified tier bump
TIER3_DAYS   = 90   # days since KYC verification before full-verified bump
# "Good standing" = zero unreviewed suspicious-activity flags

# Tier 3 progressive daily growth (applied per day beyond TIER3_DAYS)
TIER3_DAILY_STEP_KES      = 1_100   # ≈ $10/day at ~110 KES/USD
TIER3_MONTHLY_MULTIPLIER  = 20      # monthly = daily × 20 (20 withdrawal-active days/month)
TIER3_PER_TXN_MULTIPLIER  = 2       # per-txn cap = daily × 2


def get_withdraw_fee(amount):
    for mn, mx, fee in WITHDRAW_BRACKETS:
        if mn <= amount <= mx:
            return fee
    return 309


def get_send_fee(amount):
    for mn, mx, fee in SEND_BRACKETS:
        if mn <= amount <= mx:
            return fee
    return 108


def mask_phone(phone: str) -> str:
    """Risk #06: mask phone to first 3 + last 2 digits."""
    phone = re.sub(r'\D', '', phone)
    if len(phone) >= 6:
        return phone[:3] + '*' * (len(phone) - 5) + phone[-2:]
    return '***'


def mask_name(name: str) -> str:
    """Risk #06: mask name to first initial + last initial."""
    parts = name.strip().split()
    if len(parts) >= 2:
        return f"{parts[0][0]}*** {parts[-1][0]}***"
    if parts:
        return f"{parts[0][0]}***"
    return '***'


# ─────────────────────────────────────────────
class WalletUserManager(BaseUserManager):
    def create_user(self, phone, pin, **extra):
        if not phone:
            raise ValueError('Phone is required')
        user = self.model(phone=phone, **extra)
        user.set_pin(pin)
        user.save(using=self._db)
        return user

    def create_superuser(self, phone, pin, **extra):
        extra.setdefault('is_staff', True)
        extra.setdefault('is_superuser', True)
        return self.create_user(phone, pin, **extra)


class WalletUser(AbstractBaseUser):
    phone        = models.CharField(max_length=20, unique=True)
    first_name   = models.CharField(max_length=80, blank=True)
    last_name    = models.CharField(max_length=80, blank=True)
    is_active    = models.BooleanField(default=True)
    is_staff     = models.BooleanField(default=False)
    is_superuser = models.BooleanField(default=False)
    date_joined  = models.DateTimeField(default=timezone.now)

    # Risk #03: brute-force tracking
    failed_login_attempts = models.PositiveIntegerField(default=0)
    locked_until          = models.DateTimeField(null=True, blank=True)

    # Single-device session enforcement (Risk #03)
    active_session_key = models.CharField(max_length=64, blank=True, default='')
    # Last activity — used for idle timeout tracking server-side
    last_activity      = models.DateTimeField(null=True, blank=True)

    USERNAME_FIELD  = 'phone'
    REQUIRED_FIELDS = []
    objects = WalletUserManager()

    class Meta:
        verbose_name = 'User'

    def __str__(self):
        return self.phone

    def get_full_name(self):
        return f"{self.first_name} {self.last_name}".strip() or self.phone

    # Risk #03 & #17: PIN stored with bcrypt + pepper
    def set_pin(self, raw_pin: str):
        pepper = settings.SECRET_KEY[:32].encode()
        peppered = hmac.new(pepper, raw_pin.encode(), hashlib.sha256).hexdigest()
        hashed = bcrypt.hashpw(peppered.encode(), bcrypt.gensalt(rounds=12))
        self.password = hashed.decode()
        self.failed_login_attempts = 0
        self.locked_until = None

    def check_pin(self, raw_pin: str) -> bool:
        pepper = settings.SECRET_KEY[:32].encode()
        peppered = hmac.new(pepper, raw_pin.encode(), hashlib.sha256).hexdigest()
        try:
            return bcrypt.checkpw(peppered.encode(), self.password.encode())
        except Exception:
            return False

    def is_locked(self) -> bool:
        """Risk #03: check whether account is currently locked."""
        if self.locked_until and timezone.now() < self.locked_until:
            return True
        return False

    def record_failed_login(self):
        """Risk #03: increment counter and lock after 5 failures."""
        self.failed_login_attempts += 1
        if self.failed_login_attempts >= 5:
            self.locked_until = timezone.now() + timezone.timedelta(minutes=15)
        self.save(update_fields=['failed_login_attempts', 'locked_until'])

    def record_successful_login(self):
        self.failed_login_attempts = 0
        self.locked_until = None
        self.save(update_fields=['failed_login_attempts', 'locked_until'])

    def register_session(self, session_key: str):
        """
        Record the new session as the ONE active session for this user.
        Any other session with a different key is the 'other device' — it
        will be invalidated on their next request by the middleware.
        """
        self.active_session_key = session_key
        self.last_activity      = timezone.now()
        self.save(update_fields=['active_session_key', 'last_activity'])

    def touch_activity(self):
        """Update last_activity timestamp (called by middleware on each request)."""
        self.last_activity = timezone.now()
        self.save(update_fields=['last_activity'])

    def is_idle(self, idle_seconds: int = 300) -> bool:
        """Return True if user has been idle longer than idle_seconds."""
        if not self.last_activity:
            return False
        return (timezone.now() - self.last_activity).total_seconds() > idle_seconds

    def has_perm(self, perm, obj=None):
        return self.is_superuser

    def has_module_perms(self, app_label):
        return self.is_superuser


# ─────────────────────────────────────────────
class Wallet(models.Model):
    # wallet_id is the original PK char field from 0001 (e.g. "KW1A2B3C4D5E")
    wallet_id   = models.CharField(max_length=20, primary_key=True, editable=False, unique=True)
    phone       = models.CharField(max_length=20, unique=True)
    pin_hash    = models.CharField(max_length=128, blank=True)  # legacy — new code uses WalletUser.password
    country     = models.CharField(max_length=2, default='KE')
    kyc_status  = models.CharField(max_length=10, choices=KYC_STATUSES, default='pending')  # Risk #15
    created_at  = models.DateTimeField(auto_now_add=True)
    # Legacy auth.User PK, kept only so historical rows aren't lost.
    # NOTE: this is a plain integer, not a ForeignKey/relation, because
    # Django forbids any model from referencing 'auth.User' once
    # AUTH_USER_MODEL has been swapped to wallet.WalletUser (E301).
    legacy_user_id = models.IntegerField(null=True, blank=True)
    # New FK to WalletUser (added in 0007)
    wallet_user = models.OneToOneField(
        WalletUser, null=True, blank=True,
        on_delete=models.CASCADE, related_name='wallet'
    )
    # Fields added in 0007
    wallet_id_str   = models.CharField(max_length=20, unique=True, null=True, blank=True)
    home_currency   = models.CharField(max_length=3, default='', blank=True)
    kyc_verified_at = models.DateTimeField(null=True, blank=True)
    updated_at      = models.DateTimeField(auto_now=True, null=True)

    # KYC document uploads (Risk #15)
    kyc_id_front    = models.ImageField(upload_to='kyc/id/', null=True, blank=True)
    kyc_id_back     = models.ImageField(upload_to='kyc/id/', null=True, blank=True)
    kyc_selfie      = models.ImageField(upload_to='kyc/selfie/', null=True, blank=True)
    kyc_full_name   = models.CharField(max_length=160, blank=True)
    kyc_id_number   = models.CharField(max_length=60, blank=True)
    kyc_dob         = models.DateField(null=True, blank=True)

    # Sandbox / testing flag — True = this wallet uses mock rails, no real money
    is_sandbox      = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        """
        Auto-stamp kyc_verified_at the first time kyc_status flips to 'verified',
        and trigger a limit-tier sync so WalletLimit cached values stay fresh.
        """
        if self.pk:
            try:
                prev = Wallet.objects.get(pk=self.pk)
                if prev.kyc_status != 'verified' and self.kyc_status == 'verified':
                    if not self.kyc_verified_at:
                        self.kyc_verified_at = timezone.now()
            except Wallet.DoesNotExist:
                pass
        super().save(*args, **kwargs)
        # Sync WalletLimit cache after any save (creates it if missing)
        try:
            limit, _ = WalletLimit.objects.get_or_create(wallet=self)
            limit.sync_from_tier()
        except Exception:
            pass  # Never let a limit sync error break the save

    def __str__(self):
        return f"Wallet({self.wallet_id}) — {self.phone}"

    def get_kes_balance(self):
        cb = self.currency_balances.filter(currency='KES').first()
        return float(cb.balance) if cb else 0

    def get_daily_withdrawn(self):
        """Risk #16: total withdrawn today for AML limit bar."""
        today = timezone.now().date()
        from django.db.models import Sum
        result = self.transactions.filter(
            transaction_type__in=['mpesa_withdraw', 'airtel_withdraw', 'bank_withdraw', 'p2p_send'],
            status='completed',
            created_at__date=today,
        ).aggregate(total=Sum('amount'))
        return result['total'] or 0

    def get_limit_tier(self) -> int:
        """
        Progressive limit tier for this wallet.

        Tier 0 — new unverified   (<30 days OR has open AML flags)
        Tier 1 — established      (30+ days, unverified, no open flags)
        Tier 2 — KYC verified     (verified, <90 days since verification)
        Tier 3 — fully verified   (verified, 90+ days, no open flags)
        """
        now = timezone.now()
        has_open_flags = self.flags.filter(reviewed=False).exists()

        if self.kyc_status == 'verified' and self.kyc_verified_at:
            days_verified = (now - self.kyc_verified_at).days
            if days_verified >= TIER3_DAYS and not has_open_flags:
                return 3
            return 2

        # Unverified path
        age_days = (now - self.created_at).days if self.created_at else 0
        if age_days >= TIER1_DAYS and not has_open_flags:
            return 1
        return 0

    def get_effective_limits(self) -> dict:
        """
        Return the active daily/monthly/per_txn limits (KES) for this wallet.

        For Tier 3 wallets, limits grow progressively after the 90-day threshold:
          - Daily:    +KES 1,100/day beyond day 90 (≈ $10/day at ~110 KES/USD)
          - Monthly:  daily_limit × 20  (20 active withdrawal days per month)
          - Per-txn:  daily_limit × 2   (a single txn can be up to 2 days' limit)

        Growth is uncapped — long-standing, clean wallets earn proportionally
        higher limits over time, matching real KYB/trust behaviour.
        """
        tier = self.get_limit_tier()
        base = dict(LIMIT_TIERS[tier])  # shallow copy so we don't mutate the constant

        if tier == 3 and self.kyc_verified_at:
            days_beyond = max(0, (timezone.now() - self.kyc_verified_at).days - TIER3_DAYS)
            if days_beyond > 0:
                # +KES 1,100 per day beyond day 90  (~$10 at 110 KES/USD)
                daily_bonus  = days_beyond * TIER3_DAILY_STEP_KES
                new_daily    = base['daily'] + daily_bonus
                new_monthly  = new_daily * TIER3_MONTHLY_MULTIPLIER
                new_per_txn  = new_daily * TIER3_PER_TXN_MULTIPLIER
                base['daily']   = int(new_daily)
                base['monthly'] = int(new_monthly)
                base['per_txn'] = int(new_per_txn)

        return base

    def get_monthly_withdrawn(self):
        """Total completed withdrawals this calendar month (KES)."""
        from django.db.models import Sum
        now = timezone.now()
        result = self.transactions.filter(
            transaction_type__in=['mpesa_withdraw', 'airtel_withdraw', 'bank_withdraw', 'p2p_send'],
            status='completed',
            created_at__year=now.year,
            created_at__month=now.month,
        ).aggregate(total=Sum('amount'))
        return result['total'] or 0

    def get_daily_pct(self):
        used   = self.get_daily_withdrawn()
        limits = self.get_effective_limits()
        return min(int((float(used) / limits['daily']) * 100), 100)


class CurrencyBalance(models.Model):
    wallet       = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='currency_balances')
    currency     = models.CharField(max_length=3)
    balance      = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    last_updated = models.DateTimeField(auto_now=True)  # matches 0001 schema

    @property
    def added_at(self):
        return self.last_updated

    class Meta:
        unique_together = ('wallet', 'currency')

    def __str__(self):
        return f"{self.wallet.wallet_id} — {self.currency} {self.balance}"


# ─────────────────────────────────────────────
class Transaction(models.Model):
    wallet           = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=20, choices=TRANSACTION_TYPES)
    currency         = models.CharField(max_length=3, default='KES')
    amount           = models.DecimalField(max_digits=18, decimal_places=6)
    fee              = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    status           = models.CharField(max_length=12, choices=TRANSACTION_STATUSES, default='pending')

    # Risk #06: details field stores only masked PII — never full name/phone
    details          = models.CharField(max_length=200, blank=True)

    # For M-Pesa / Airtel idempotency (Risk #02)
    external_ref     = models.CharField(max_length=120, blank=True, db_index=True)
    idempotency_key  = models.CharField(max_length=64, unique=True, null=True, blank=True)

    # For bank transfers
    bank_name        = models.CharField(max_length=80, blank=True)
    bank_account     = models.CharField(max_length=80, blank=True)

    # For P2P — stores masked recipient only (Risk #06)
    recipient_wallet = models.ForeignKey(
        Wallet, null=True, blank=True,
        on_delete=models.SET_NULL, related_name='received_transactions'
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.transaction_type} {self.currency} {self.amount} [{self.status}]"

    @property
    def masked_recipient(self):
        """Risk #06: returns masked phone for display in history."""
        if self.recipient_wallet:
            return mask_phone(self.recipient_wallet.phone)
        return '***'


# ─────────────────────────────────────────────
class MpesaTransaction(models.Model):
    """Tracks M-Pesa STK push / B2C references to prevent double-credit (Risk #02)."""
    wallet              = models.ForeignKey(Wallet, on_delete=models.CASCADE)
    checkout_request_id = models.CharField(max_length=100, unique=True, db_index=True)
    merchant_request_id = models.CharField(max_length=100, blank=True)
    amount              = models.DecimalField(max_digits=12, decimal_places=2)
    phone               = models.CharField(max_length=20)
    status              = models.CharField(max_length=12, choices=TRANSACTION_STATUSES, default='pending')
    mpesa_receipt       = models.CharField(max_length=60, blank=True)
    transaction_type    = models.CharField(max_length=20, default='mpesa_deposit')
    created_at          = models.DateTimeField(auto_now_add=True)
    updated_at          = models.DateTimeField(auto_now=True)
    # Risk #04: timeout field for orphaned pending withdrawals
    timeout_at          = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']


class AirtelTransaction(models.Model):
    """Tracks Airtel Money collection / disbursement to prevent double-credit."""
    wallet           = models.ForeignKey(Wallet, on_delete=models.CASCADE)
    airtel_ref       = models.CharField(max_length=100, unique=True, db_index=True)
    amount           = models.DecimalField(max_digits=12, decimal_places=2)
    phone            = models.CharField(max_length=20)
    status           = models.CharField(max_length=12, choices=TRANSACTION_STATUSES, default='pending')
    transaction_type = models.CharField(max_length=20, default='airtel_deposit')
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)
    timeout_at       = models.DateTimeField(null=True, blank=True)  # Risk #04


class BankTransaction(models.Model):
    """Tracks PesaLink / RTGS transactions."""
    wallet           = models.ForeignKey(Wallet, on_delete=models.CASCADE)
    pesalink_ref     = models.CharField(max_length=100, unique=True, db_index=True)
    amount           = models.DecimalField(max_digits=12, decimal_places=2)
    bank_name        = models.CharField(max_length=80)
    account_number   = models.CharField(max_length=80)
    account_name     = models.CharField(max_length=120)
    status           = models.CharField(max_length=12, choices=TRANSACTION_STATUSES, default='pending')
    transaction_type = models.CharField(max_length=20, default='bank_deposit')
    created_at       = models.DateTimeField(auto_now_add=True)
    updated_at       = models.DateTimeField(auto_now=True)
    timeout_at       = models.DateTimeField(null=True, blank=True)  # Risk #04


class FlutterwaveTransaction(models.Model):
    """
    Tracks every payment initiated via Flutterwave (deposits and payouts).

    Fields:
      flw_tx_id   — Flutterwave's own transaction/transfer ID (set after verification)
      tx_ref      — our idempotency key (Risk #02), generated before the API call
      channel     — card | banktransfer | mpesa | airtel | bank_payout | mobile_payout
      amount      — amount in `currency` (before fees)
      fee         — Flutterwave fee charged (populated from webhook)
      currency    — ISO 4217 code
      phone       — for mobile-money channels
      status      — pending | successful | failed | refunded
      direction   — 'in' (deposit) or 'out' (payout)
      raw_payload — last raw webhook / verify response (for audit trail)
    """
    DIRECTION_CHOICES = [('in', 'Deposit'), ('out', 'Payout')]

    wallet       = models.ForeignKey('Wallet', on_delete=models.PROTECT, related_name='flw_transactions')
    flw_tx_id    = models.CharField(max_length=120, blank=True, db_index=True)
    tx_ref       = models.CharField(max_length=120, unique=True, db_index=True)
    channel      = models.CharField(max_length=30)   # card / banktransfer / mpesa / airtel / bank_payout / mobile_payout
    amount       = models.DecimalField(max_digits=14, decimal_places=2)
    fee          = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    currency     = models.CharField(max_length=3, default='KES')
    phone        = models.CharField(max_length=20, blank=True)
    direction    = models.CharField(max_length=3, choices=DIRECTION_CHOICES, default='in')
    status       = models.CharField(max_length=12, choices=TRANSACTION_STATUSES, default='pending')
    raw_payload  = models.JSONField(null=True, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)
    timeout_at   = models.DateTimeField(null=True, blank=True)  # Risk #04: stale pending cleanup

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'FLW {self.direction} {self.currency} {self.amount} [{self.status}] ref={self.tx_ref}'


# ─────────────────────────────────────────────
class PoolLedger(models.Model):
    """Company liquidity ledger — tracked for Risk #04 reconciliation."""
    currency       = models.CharField(max_length=3, default='KES')
    entry_type     = models.CharField(max_length=20)  # deposit_in, withdrawal_out, fee_in, exchange
    amount         = models.DecimalField(max_digits=18, decimal_places=6)
    reference      = models.CharField(max_length=120, blank=True)
    created_at     = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class CompanyAccount(models.Model):
    currency = models.CharField(max_length=3, unique=True)
    balance  = models.DecimalField(max_digits=18, decimal_places=6, default=0)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_solvent(self):
        return self.balance >= 0

    def __str__(self):
        return f"CompanyAccount {self.currency}: {self.balance}"


# ─────────────────────────────────────────────
class QRPaymentRequest(models.Model):
    wallet     = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='qr_requests')
    token      = models.CharField(max_length=64, unique=True, db_index=True)
    amount     = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    note       = models.CharField(max_length=120, blank=True)
    single_use = models.BooleanField(default=False)
    status     = models.CharField(max_length=10, default='active',
                                  choices=[('active','Active'),('paid','Paid'),('expired','Expired'),('disabled','Disabled')])
    expires_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        if self.status not in ('active',):
            return False
        if self.expires_at and timezone.now() > self.expires_at:
            self.status = 'expired'
            self.save(update_fields=['status'])
            return False
        return True


# ─────────────────────────────────────────────
class SuspiciousActivityFlag(models.Model):
    """Risk #16: AML — flag structuring / velocity anomalies for compliance review."""
    wallet      = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='flags')
    flag_type   = models.CharField(max_length=40)  # velocity, structuring, round_number, threshold_breach
    description = models.TextField()
    transaction = models.ForeignKey(Transaction, null=True, blank=True, on_delete=models.SET_NULL)
    reviewed    = models.BooleanField(default=False)
    created_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Flag({self.flag_type}) wallet={self.wallet.wallet_id}"


class WalletLimit(models.Model):
    """
    Per-wallet transaction limits (Risk #15).

    Limits are now computed dynamically via Wallet.get_effective_limits() based on
    the wallet's progressive tier.  This model stores the CURRENT cached/snapshotted
    values (updated by a periodic task or on-write) and an optional admin override.

    Admin override: set tier_override to 0-3 to pin a wallet to a specific tier
    regardless of its automatic eligibility.  Leave as None for automatic.
    """
    TIER_CHOICES = [
        (0, 'Tier 0 — New Unverified'),
        (1, 'Tier 1 — Established Unverified'),
        (2, 'Tier 2 — KYC Verified'),
        (3, 'Tier 3 — Fully Verified'),
    ]

    wallet             = models.OneToOneField(Wallet, on_delete=models.CASCADE, related_name='limit')
    daily_withdraw_kes = models.DecimalField(max_digits=12, decimal_places=2, default=10000)
    per_txn_max_kes    = models.DecimalField(max_digits=12, decimal_places=2, default=10000)
    monthly_limit_kes  = models.DecimalField(max_digits=12, decimal_places=2, default=300_000)
    # Optional admin pin — overrides automatic tier calculation
    tier_override      = models.SmallIntegerField(null=True, blank=True, choices=TIER_CHOICES)
    last_tier_update   = models.DateTimeField(null=True, blank=True)

    def sync_from_tier(self):
        """Recompute and save limits from the wallet's current effective tier."""
        tier = self.tier_override if self.tier_override is not None else self.wallet.get_limit_tier()
        t = LIMIT_TIERS[tier]
        self.daily_withdraw_kes = t['daily']
        self.per_txn_max_kes    = t['per_txn']
        self.monthly_limit_kes  = t['monthly']
        self.last_tier_update   = timezone.now()
        self.save(update_fields=['daily_withdraw_kes', 'per_txn_max_kes',
                                  'monthly_limit_kes', 'last_tier_update'])

    def __str__(self):
        return f"Limit(tier={self.tier_override or 'auto'}) for {self.wallet.wallet_id}"


class PinResetToken(models.Model):
    """Risk #03: secure PIN reset with time-limited token."""
    user       = models.ForeignKey(WalletUser, on_delete=models.CASCADE)
    token      = models.CharField(max_length=64, unique=True)
    code       = models.CharField(max_length=6)
    used       = models.BooleanField(default=False)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    def is_valid(self):
        return not self.used and timezone.now() < self.expires_at
