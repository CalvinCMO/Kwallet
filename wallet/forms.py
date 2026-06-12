"""
forms.py — KWallet v2 Forms
"""
from django import forms
from decimal import Decimal
from django.utils import timezone
from .models import (
    ALL_CURRENCIES, COUNTRY_CHOICES, INTERNATIONAL_CURRENCIES,
    UNIVERSAL_CURRENCIES, FEE_SCHEDULE, calculate_fee,
    PaymentRequest, PHONE_REGEX,
)

# ── Constants ────────────────────────────────────────────────────────────────

MAX_CURRENCIES = 10  # max currencies a single wallet may hold at once

# International-only choices for the registration picker
INTL_CURRENCY_CHOICES = [
    (code, f"{code} — {name}")
    for code, name in ALL_CURRENCIES
    if code in INTERNATIONAL_CURRENCIES
]

# Every supported currency (used by exchange / p2p forms whose choices are
# narrowed down dynamically in the view to only what the wallet holds)
ALL_CURRENCY_CHOICES = [
    (code, f"{code} — {name}") for code, name in ALL_CURRENCIES
]


# ── Registration ─────────────────────────────────────────────────────────────

class RegisterForm(forms.Form):
    full_name   = forms.CharField(max_length=100, label='Full Name')
    phone       = forms.CharField(max_length=15,  label='Phone Number')
    country     = forms.ChoiceField(choices=COUNTRY_CHOICES, label='Country')
    pin         = forms.CharField(
        widget=forms.PasswordInput, min_length=4, max_length=6, label='PIN'
    )
    pin_confirm = forms.CharField(widget=forms.PasswordInput, label='Confirm PIN')

    # User picks exactly 5 international currencies at registration.
    # EA universal currencies are always added automatically.
    # Total = len(UNIVERSAL_CURRENCIES) + 5  ≤ MAX_CURRENCIES
    currency_1 = forms.ChoiceField(choices=INTL_CURRENCY_CHOICES, label='International Currency 1')
    currency_2 = forms.ChoiceField(choices=INTL_CURRENCY_CHOICES, label='International Currency 2')
    currency_3 = forms.ChoiceField(choices=INTL_CURRENCY_CHOICES, label='International Currency 3')
    currency_4 = forms.ChoiceField(choices=INTL_CURRENCY_CHOICES, label='International Currency 4')
    currency_5 = forms.ChoiceField(choices=INTL_CURRENCY_CHOICES, label='International Currency 5')

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        from .models import Wallet
        if Wallet.objects.filter(phone=phone).exists():
            raise forms.ValidationError('A wallet with this phone number already exists.')
        return phone

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('pin') != cleaned.get('pin_confirm'):
            raise forms.ValidationError('PINs do not match.')

        # Collect chosen currencies, skip blanks
        chosen = [
            cleaned.get(f'currency_{i}')
            for i in range(1, 6)
            if cleaned.get(f'currency_{i}')
        ]
        # No duplicates among picks
        if len(set(chosen)) != len(chosen):
            raise forms.ValidationError('Please select 5 different international currencies.')

        # Total currencies that will be created = universal + chosen (deduplicated)
        total = len(set(UNIVERSAL_CURRENCIES) | set(chosen))
        if total > MAX_CURRENCIES:
            raise forms.ValidationError(
                f'Too many currencies. You can hold at most {MAX_CURRENCIES} total '
                f'({len(UNIVERSAL_CURRENCIES)} EA currencies are always included).'
            )
        return cleaned

    def get_chosen_currencies(self):
        """Returns the 5 user-chosen international currency codes."""
        return [self.cleaned_data[f'currency_{i}'] for i in range(1, 6)]


# ── Login ────────────────────────────────────────────────────────────────────

class LoginForm(forms.Form):
    phone = forms.CharField(max_length=15, label='Phone Number')
    pin   = forms.CharField(widget=forms.PasswordInput, label='PIN')


# ── Exchange ─────────────────────────────────────────────────────────────────

class ExchangeForm(forms.Form):
    from_currency = forms.ChoiceField(choices=ALL_CURRENCY_CHOICES, label='From')
    to_currency   = forms.ChoiceField(choices=ALL_CURRENCY_CHOICES, label='To')
    amount        = forms.DecimalField(min_value=Decimal('0.01'), decimal_places=4, label='Amount')

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('from_currency') == cleaned.get('to_currency'):
            raise forms.ValidationError('Cannot exchange a currency with itself.')
        return cleaned


# ── P2P Transfer ─────────────────────────────────────────────────────────────

class P2PTransferForm(forms.Form):
    recipient_phone = forms.CharField(max_length=15, label='Recipient Phone Number')
    currency        = forms.ChoiceField(choices=ALL_CURRENCY_CHOICES, label='Currency')
    amount          = forms.DecimalField(min_value=Decimal('0.01'), decimal_places=4, label='Amount')

    def clean_recipient_phone(self):
        phone = self.cleaned_data['recipient_phone'].strip()
        from .models import Wallet
        if not Wallet.objects.filter(phone=phone).exists():
            raise forms.ValidationError('No wallet found for that phone number.')
        return phone


# ── M-Pesa ───────────────────────────────────────────────────────────────────

class MpesaDepositForm(forms.Form):
    amount = forms.DecimalField(min_value=Decimal('10'), decimal_places=2, label='Amount (KES)')
    phone  = forms.CharField(max_length=15, required=False, label='M-Pesa Phone')

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount < 10:
            raise forms.ValidationError('Minimum M-Pesa deposit is KES 10.')
        return amount


class MpesaWithdrawForm(forms.Form):
    amount = forms.DecimalField(min_value=Decimal('10'), decimal_places=2, label='Amount (KES)')
    phone  = forms.CharField(max_length=15, required=False, label='M-Pesa Phone')

    def clean_amount(self):
        amount = self.cleaned_data['amount']
        if amount < 10:
            raise forms.ValidationError('Minimum withdrawal is KES 10.')
        return amount


# ── Add / Remove Currency ─────────────────────────────────────────────────────

class AddCurrencyForm(forms.Form):
    """
    Post-registration currency management.

    Rules enforced here (and mirrored in the view / template):
    • Max MAX_CURRENCIES (10) currencies per wallet at any time.
    • Home currency is immutable — the view never calls this for it.
    • Only currencies not already held are offered.
    """
    currency = forms.ChoiceField(choices=[], label='Currency to Add')

    def __init__(self, *args, wallet=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.wallet = wallet
        if wallet:
            existing_codes = set(wallet.get_active_currencies())
            available = [
                (code, f"{code} — {name}")
                for code, name in ALL_CURRENCIES
                if code not in existing_codes
            ]
            self.fields['currency'].choices = available
        else:
            self.fields['currency'].choices = ALL_CURRENCY_CHOICES

    def clean_currency(self):
        currency = self.cleaned_data['currency']
        if self.wallet:
            qs = self.wallet.currency_balances.all()
            if qs.filter(currency=currency).exists():
                raise forms.ValidationError(f'You already hold {currency}.')
            if qs.count() >= MAX_CURRENCIES:
                raise forms.ValidationError(
                    f'Wallet full — you already hold {MAX_CURRENCIES} currencies. '
                    f'Remove one with a zero balance before adding another.'
                )
        return currency


# ── QR Payment ───────────────────────────────────────────────────────────────

class PaymentRequestForm(forms.ModelForm):
    """Wallet owner creates a payment request / QR code."""

    class Meta:
        model  = PaymentRequest
        fields = ['amount', 'note', 'single_use', 'expires_at']
        widgets = {
            'amount': forms.NumberInput(attrs={
                'placeholder': 'Leave blank — payer chooses',
                'min': '1', 'step': '1',
            }),
            'note': forms.TextInput(attrs={
                'placeholder': 'e.g. Lunch split, Invoice #42, Rent…',
                'maxlength': '120',
            }),
            'expires_at': forms.DateTimeInput(
                attrs={'type': 'datetime-local'},
                format='%Y-%m-%dT%H:%M',
            ),
            'single_use': forms.CheckboxInput(),
        }
        labels = {
            'amount':     'Amount (KES) — optional',
            'note':       'Note / Description',
            'single_use': 'One-time use (disable after first payment)',
            'expires_at': 'Expiry date & time — optional',
        }

    def clean_amount(self):
        amount = self.cleaned_data.get('amount')
        if amount is not None and amount <= 0:
            raise forms.ValidationError('Amount must be greater than zero.')
        return amount

    def clean_expires_at(self):
        expires_at = self.cleaned_data.get('expires_at')
        if expires_at and expires_at <= timezone.now():
            raise forms.ValidationError('Expiry must be a future date and time.')
        return expires_at


class QRPayForm(forms.Form):
    """Public (no-login) pay page — used by the payer."""

    phone = forms.CharField(
        max_length=10,
        label='Your M-Pesa Phone Number',
        validators=[PHONE_REGEX],
        widget=forms.TextInput(attrs={
            'placeholder': '07XXXXXXXX',
            'inputmode': 'numeric',
            'maxlength': '10',
        }),
    )
    amount = forms.DecimalField(
        max_digits=10, decimal_places=2,
        min_value=Decimal('10'),
        label='Amount (KES)',
        widget=forms.NumberInput(attrs={
            'placeholder': 'Amount in KES',
            'min': '10', 'step': '1',
        }),
    )

    def __init__(self, *args, fixed_amount=None, **kwargs):
        super().__init__(*args, **kwargs)
        if fixed_amount is not None:
            self.fields['amount'].initial = fixed_amount
            self.fields['amount'].widget  = forms.NumberInput(attrs={
                'value':    str(fixed_amount),
                'readonly': 'readonly',
                'style':    'background:var(--bg);cursor:not-allowed',
            })
            self.fields['amount'].help_text = 'Amount set by the payee — cannot be changed.'

    def clean_phone(self):
        phone = self.cleaned_data.get('phone', '').strip()
        try:
            PHONE_REGEX(phone)
        except forms.ValidationError:
            raise forms.ValidationError(
                'Enter a valid Kenyan mobile number (10 digits, starting with 07 or 01).'
            )
        return phone


# ── PIN Reset ──────────────────────────────────────────────────────────────────

class PinResetRequestForm(forms.Form):
    phone = forms.CharField(max_length=15, label='Phone Number')

    def clean_phone(self):
        phone = self.cleaned_data['phone'].strip()
        from .models import Wallet
        if not Wallet.objects.filter(phone=phone).exists():
            raise forms.ValidationError('No wallet found for that phone number.')
        return phone


class PinResetVerifyForm(forms.Form):
    code = forms.CharField(
        max_length=6, min_length=6, label='Verification Code',
        widget=forms.TextInput(attrs={'placeholder': '000000', 'autocomplete': 'one-time-code'})
    )


class PinResetSetForm(forms.Form):
    pin = forms.CharField(
        widget=forms.PasswordInput, min_length=4, max_length=6, label='New PIN'
    )
    pin_confirm = forms.CharField(widget=forms.PasswordInput, label='Confirm New PIN')

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('pin') != cleaned.get('pin_confirm'):
            raise forms.ValidationError('PINs do not match.')
        return cleaned
