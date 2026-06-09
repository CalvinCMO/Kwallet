from django.contrib import admin
from django.db.models import Sum
from django.utils.html import format_html
from decimal import Decimal

from .models import (
    Wallet, CurrencyBalance, Transaction, MpesaTransaction,
    FeeRecord, PaymentMethod, WalletLimit,
    CompanyAccount, PoolLedger, FeeSettlement,
)


# ── Wallet ─────────────────────────────────────────────────────────────────────

@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display  = ('wallet_id', 'user', 'phone', 'country', 'kyc_status', 'created_at')
    list_filter   = ('country', 'kyc_status')
    search_fields = ('phone', 'wallet_id', 'user__first_name', 'user__last_name')
    readonly_fields = ('wallet_id', 'created_at')


# ── CurrencyBalance ────────────────────────────────────────────────────────────

@admin.register(CurrencyBalance)
class CurrencyBalanceAdmin(admin.ModelAdmin):
    readonly_fields = ('wallet', 'currency', 'balance', 'last_updated')
    list_display = ('wallet', 'currency', 'balance', 'last_updated')
    list_filter  = ('currency',)
    search_fields = ('wallet__phone',)


# ── Transaction ────────────────────────────────────────────────────────────────

@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display  = ('reference', 'wallet', 'transaction_type', 'currency',
                     'amount', 'fee', 'status', 'created_at')
    list_filter   = ('transaction_type', 'currency', 'status')
    search_fields = ('wallet__phone', 'reference')
    readonly_fields = ('reference', 'created_at', 'updated_at')


# ── FeeRecord ─────────────────────────────────────────────────────────────────

@admin.register(FeeRecord)
class FeeRecordAdmin(admin.ModelAdmin):
    list_display  = ('wallet', 'amount', 'currency', 'fee_type',
                     'settlement_status', 'collected_at')
    list_filter   = ('fee_type', 'currency')
    search_fields = ('wallet__phone',)
    readonly_fields = ('collected_at',)

    @admin.display(description='Settled?')
    def settlement_status(self, obj):
        if obj.settlement:
            return format_html(
                '<span style="color:green">✅ {}</span>',
                obj.settlement.reference,
            )
        return format_html('<span style="color:orange">⏳ Unsettled</span>')


# ── MpesaTransaction ───────────────────────────────────────────────────────────

@admin.register(MpesaTransaction)
class MpesaTransactionAdmin(admin.ModelAdmin):
    list_display  = ('wallet', 'phone', 'amount', 'direction',
                     'status', 'mpesa_receipt', 'created_at')
    list_filter   = ('direction', 'status')
    search_fields = ('phone', 'mpesa_receipt', 'checkout_request_id')
    readonly_fields = ('created_at', 'updated_at')


# ── PaymentMethod ─────────────────────────────────────────────────────────────

@admin.register(PaymentMethod)
class PaymentMethodAdmin(admin.ModelAdmin):
    list_display  = ('wallet', 'rail', 'label', 'identifier',
                     'currency', 'is_verified', 'is_default')
    list_filter   = ('rail', 'country', 'is_verified')
    search_fields = ('wallet__phone', 'identifier')


# ── WalletLimit ────────────────────────────────────────────────────────────────

@admin.register(WalletLimit)
class WalletLimitAdmin(admin.ModelAdmin):
    list_display  = ('wallet', 'per_transaction_limit_usd',
                     'base_daily_limit_usd', 'daily_limit_increment_usd',
                     'effective_daily_limit', 'created_at')
    search_fields = ('wallet__phone',)
    readonly_fields = ('created_at', 'updated_at')

    @admin.display(description='Effective Daily Limit (USD)')
    def effective_daily_limit(self, obj):
        return f"${obj.effective_daily_limit_usd}"


# ── CompanyAccount ─────────────────────────────────────────────────────────────

@admin.register(CompanyAccount)
class CompanyAccountAdmin(admin.ModelAdmin):
    list_display  = ('name', 'account_type', 'rail', 'currency',
                     'identifier', 'ledger_balance', 'solvency_badge',
                     'user_liability_display', 'surplus_display', 'is_active')
    list_filter   = ('account_type', 'currency', 'rail', 'is_active')
    search_fields = ('name', 'identifier')
    readonly_fields = ('ledger_balance', 'created_at', 'updated_at',
                       'solvency_badge', 'user_liability_display', 'surplus_display')

    fieldsets = (
        ('Account Details', {
            'fields': ('name', 'account_type', 'rail', 'currency',
                       'identifier', 'is_active', 'notes'),
        }),
        ('Balances (read-only)', {
            'fields': ('ledger_balance', 'user_liability_display',
                       'surplus_display', 'solvency_badge'),
            'description': (
                'ledger_balance is updated automatically by the settlement engine. '
                'Do not edit directly unless performing a manual reconciliation adjustment.'
            ),
        }),
        ('Timestamps', {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',),
        }),
    )

    @admin.display(description='Solvency')
    def solvency_badge(self, obj):
        if obj.account_type != 'client_float':
            return '—'
        if obj.is_solvent:
            return format_html('<span style="color:green;font-weight:bold">✅ SOLVENT</span>')
        return format_html('<span style="color:red;font-weight:bold">🔴 INSOLVENT</span>')

    @admin.display(description='User Liability')
    def user_liability_display(self, obj):
        if obj.account_type != 'client_float':
            return '—'
        return f"{obj.total_user_liability:.4f} {obj.currency}"

    @admin.display(description='Surplus')
    def surplus_display(self, obj):
        if obj.account_type != 'client_float':
            return '—'
        s = obj.surplus
        color = 'green' if s >= 0 else 'red'
        return format_html(
            '<span style="color:{}">{:.4f} {}</span>', color, s, obj.currency
        )


# ── FeeSettlement ──────────────────────────────────────────────────────────────

@admin.register(FeeSettlement)
class FeeSettlementAdmin(admin.ModelAdmin):
    list_display  = ('reference', 'currency', 'total_fees', 'fee_count',
                     'from_account', 'to_account', 'status_badge',
                     'initiated_by', 'created_at', 'completed_at')
    list_filter   = ('status', 'currency')
    search_fields = ('reference', 'initiated_by')
    readonly_fields = ('reference', 'created_at', 'completed_at',
                       'total_fees', 'fee_count', 'status_badge')

    # Settlements should never be edited after creation — they are immutable
    # accounting records. Allow viewing only.
    def has_change_permission(self, request, obj=None):
        if obj and obj.status == 'completed':
            return False
        return super().has_change_permission(request, obj)

    @admin.display(description='Status')
    def status_badge(self, obj):
        colours = {
            'completed': 'green',
            'pending':   'orange',
            'failed':    'red',
        }
        icons = {'completed': '✅', 'pending': '⏳', 'failed': '❌'}
        colour = colours.get(obj.status, 'grey')
        icon   = icons.get(obj.status, '?')
        return format_html(
            '<span style="color:{};font-weight:bold">{} {}</span>',
            colour, icon, obj.get_status_display(),
        )


# ── PoolLedger ─────────────────────────────────────────────────────────────────

@admin.register(PoolLedger)
class PoolLedgerAdmin(admin.ModelAdmin):
    list_display  = ('created_at', 'account', 'entry_type', 'currency',
                     'amount', 'balance_after', 'created_by',
                     'linked_transaction', 'linked_settlement')
    list_filter   = ('entry_type', 'currency', 'account')
    search_fields = ('account__name', 'note', 'created_by')
    readonly_fields = ('created_at', 'account', 'entry_type', 'amount',
                       'currency', 'balance_after', 'transaction',
                       'settlement', 'note', 'created_by')
    ordering = ('-created_at',)

    # Pool ledger is fully immutable — no additions or edits via admin
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description='Transaction')
    def linked_transaction(self, obj):
        if obj.transaction:
            return format_html(
                '<a href="/admin/wallet/transaction/?reference={}">{}</a>',
                obj.transaction.reference, obj.transaction.reference[:16] + '…',
            )
        return '—'

    @admin.display(description='Settlement')
    def linked_settlement(self, obj):
        if obj.settlement:
            return format_html(
                '<a href="/admin/wallet/feesettlement/?reference={}">{}</a>',
                obj.settlement.reference, obj.settlement.reference[:18] + '…',
            )
        return '—'
