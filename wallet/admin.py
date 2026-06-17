from django.contrib import admin
from django.db.models import Sum
from django.utils.html import format_html
from decimal import Decimal

from .models import (
    WalletUser,
    Wallet,
    CurrencyBalance,
    Transaction,
    MpesaTransaction,
    AirtelTransaction,
    BankTransaction,
    WalletLimit,
    CompanyAccount,
    PoolLedger,
    QRPaymentRequest,
    SuspiciousActivityFlag,
    PinResetToken,
)


# ── WalletUser ───────────────────────────────────────────────────────────

@admin.register(WalletUser)
class WalletUserAdmin(admin.ModelAdmin):
    list_display  = ('phone', 'first_name', 'last_name', 'is_active',
                     'failed_login_attempts', 'locked_until', 'date_joined')
    list_filter   = ('is_active', 'is_staff')
    search_fields = ('phone', 'first_name', 'last_name')
    readonly_fields = ('date_joined', 'failed_login_attempts', 'locked_until', 'password')
    fieldsets = (
        ('Identity', {'fields': ('phone', 'first_name', 'last_name')}),
        ('Permissions', {'fields': ('is_active', 'is_staff', 'is_superuser')}),
        ('Security (read-only)', {
            'fields': ('password', 'failed_login_attempts', 'locked_until', 'date_joined'),
            'description': 'PIN is stored as bcrypt+pepper hash — never editable here.',
        }),
    )

    def has_delete_permission(self, request, obj=None):
        # Prevent accidental user deletion — use is_active=False instead
        return request.user.is_superuser


# ── Wallet ───────────────────────────────────────────────────────────────

@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display  = ('wallet_id', 'wallet_user', 'phone', 'home_currency',
                     'kyc_status_badge', 'kes_balance_display', 'created_at')
    list_filter   = ('kyc_status', 'home_currency')
    search_fields = ('phone', 'wallet_id', 'user__first_name', 'user__last_name')
    readonly_fields = ('wallet_id', 'created_at', 'updated_at', 'kyc_verified_at')

    @admin.display(description='KYC')
    def kyc_status_badge(self, obj):
        colours = {'verified': 'green', 'pending': 'orange', 'rejected': 'red'}
        icons   = {'verified': '✅', 'pending': '⏳', 'rejected': '❌'}
        c = colours.get(obj.kyc_status, 'grey')
        i = icons.get(obj.kyc_status, '?')
        return format_html('<span style="color:{};font-weight:600">{} {}</span>',
                           c, i, obj.kyc_status.title())

    @admin.display(description='KES Balance')
    def kes_balance_display(self, obj):
        bal = obj.get_kes_balance()
        return f'KES {bal:,.2f}'


# ── CurrencyBalance ──────────────────────────────────────────────────────

@admin.register(CurrencyBalance)
class CurrencyBalanceAdmin(admin.ModelAdmin):
    list_display  = ('wallet', 'currency', 'balance', 'last_updated')
    list_filter   = ('currency',)
    search_fields = ('wallet__phone', 'wallet__wallet_id')
    readonly_fields = ('wallet', 'currency', 'last_updated')


# ── Transaction ──────────────────────────────────────────────────────────

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display  = ('id', 'wallet', 'transaction_type', 'currency',
                     'amount', 'fee', 'status_badge', 'created_at')
    list_filter   = ('transaction_type', 'currency', 'status')
    search_fields = ('wallet__phone', 'wallet__wallet_id', 'external_ref', 'idempotency_key')
    readonly_fields = ('created_at', 'updated_at', 'idempotency_key', 'external_ref')
    ordering = ('-created_at',)

    @admin.display(description='Status')
    def status_badge(self, obj):
        colours = {'completed': 'green', 'pending': 'orange',
                   'failed': 'red', 'refunded': 'blue'}
        c = colours.get(obj.status, 'grey')
        return format_html('<span style="color:{};font-weight:600">{}</span>',
                           c, obj.status.title())


# ── MpesaTransaction ─────────────────────────────────────────────────────

@admin.register(MpesaTransaction)
class MpesaTransactionAdmin(admin.ModelAdmin):
    list_display  = ('wallet', 'phone', 'amount', 'transaction_type',
                     'status', 'mpesa_receipt', 'timeout_at', 'created_at')
    list_filter   = ('transaction_type', 'status')
    search_fields = ('phone', 'mpesa_receipt', 'checkout_request_id', 'wallet__phone')
    readonly_fields = ('created_at', 'updated_at', 'checkout_request_id',
                       'merchant_request_id', 'mpesa_receipt', 'timeout_at')
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False  # Created programmatically only


# ── AirtelTransaction ────────────────────────────────────────────────────

@admin.register(AirtelTransaction)
class AirtelTransactionAdmin(admin.ModelAdmin):
    list_display  = ('wallet', 'phone', 'amount', 'transaction_type',
                     'status', 'airtel_ref', 'timeout_at', 'created_at')
    list_filter   = ('transaction_type', 'status')
    search_fields = ('phone', 'airtel_ref', 'wallet__phone')
    readonly_fields = ('created_at', 'updated_at', 'airtel_ref', 'timeout_at')
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False


# ── BankTransaction ──────────────────────────────────────────────────────

@admin.register(BankTransaction)
class BankTransactionAdmin(admin.ModelAdmin):
    list_display  = ('wallet', 'bank_name', 'account_name', 'amount',
                     'transaction_type', 'status', 'pesalink_ref',
                     'timeout_at', 'created_at')
    list_filter   = ('transaction_type', 'status', 'bank_name')
    search_fields = ('wallet__phone', 'pesalink_ref', 'account_number', 'account_name')
    readonly_fields = ('created_at', 'updated_at', 'pesalink_ref', 'timeout_at')
    ordering = ('-created_at',)


# ── WalletLimit ──────────────────────────────────────────────────────────

@admin.register(WalletLimit)
class WalletLimitAdmin(admin.ModelAdmin):
    list_display  = ('wallet', 'daily_withdraw_kes',
                     'per_txn_max_kes', 'monthly_limit_kes')
    search_fields = ('wallet__phone', 'wallet__wallet_id')


# ── CompanyAccount ───────────────────────────────────────────────────────

@admin.register(CompanyAccount)
class CompanyAccountAdmin(admin.ModelAdmin):
    list_display  = ('currency', 'balance', 'solvency_badge', 'updated_at')
    readonly_fields = ('updated_at',)

    @admin.display(description='Solvency')
    def solvency_badge(self, obj):
        if obj.is_solvent:
            return format_html(
                '<span style="color:green;font-weight:bold">✅ SOLVENT</span>'
            )
        return format_html(
            '<span style="color:red;font-weight:bold">🔴 INSOLVENT — KES {:,.2f}</span>',
            obj.balance
        )


# ── PoolLedger ───────────────────────────────────────────────────────────

@admin.register(PoolLedger)
class PoolLedgerAdmin(admin.ModelAdmin):
    list_display  = ('created_at', 'currency', 'entry_type', 'amount', 'reference')
    list_filter   = ('entry_type', 'currency')
    search_fields = ('reference',)
    readonly_fields = ('created_at', 'currency', 'entry_type', 'amount', 'reference')
    ordering = ('-created_at',)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ── QRPaymentRequest ─────────────────────────────────────────────────────

@admin.register(QRPaymentRequest)
class QRPaymentRequestAdmin(admin.ModelAdmin):
    list_display  = ('wallet', 'amount', 'note', 'status',
                     'single_use', 'expires_at', 'created_at')
    list_filter   = ('status', 'single_use')
    search_fields = ('wallet__phone', 'note', 'token')
    readonly_fields = ('token', 'created_at')


# ── SuspiciousActivityFlag ───────────────────────────────────────────────

@admin.register(SuspiciousActivityFlag)
class SuspiciousActivityFlagAdmin(admin.ModelAdmin):
    list_display  = ('created_at', 'wallet', 'flag_type',
                     'short_description', 'reviewed')
    list_filter   = ('flag_type', 'reviewed')
    search_fields = ('wallet__phone', 'wallet__wallet_id', 'description')
    readonly_fields = ('created_at', 'wallet', 'flag_type', 'description', 'transaction')
    ordering = ('-created_at',)
    actions = ['mark_reviewed']

    @admin.display(description='Description')
    def short_description(self, obj):
        return obj.description[:80] + ('…' if len(obj.description) > 80 else '')

    @admin.action(description='Mark selected flags as reviewed')
    def mark_reviewed(self, request, queryset):
        updated = queryset.update(reviewed=True)
        self.message_user(request, f'{updated} flag(s) marked as reviewed.')


# ── PinResetToken ────────────────────────────────────────────────────────

@admin.register(PinResetToken)
class PinResetTokenAdmin(admin.ModelAdmin):
    list_display  = ('user', 'used', 'expires_at', 'created_at')
    list_filter   = ('used',)
    search_fields = ('user__phone',)
    readonly_fields = ('token', 'code', 'created_at', 'expires_at', 'user')

    def has_add_permission(self, request):
        return False
