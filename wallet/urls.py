from django.urls import path
from . import views

urlpatterns = [
    # Auth
    path('register/',        views.register_view,         name='register'),
    path('login/',           views.login_view,            name='login'),
    path('logout/',          views.logout_view,           name='logout'),
    path('idle-ping/',       views.idle_ping_view,        name='idle_ping'),

    # Dashboard
    path('',                 views.dashboard_view,        name='dashboard'),

    # Deposit — Flutterwave (card, bank transfer, M-Pesa, Airtel via FLW)
    path('deposit/',         views.flw_deposit_view,      name='deposit'),
    path('deposit/return/',  views.flw_redirect_view,     name='flw_redirect'),

    # Legacy deposit URL aliases — redirect to unified deposit
    path('deposit/mpesa/',   views.flw_deposit_view,      name='mpesa_deposit'),
    path('deposit/airtel/',  views.flw_deposit_view,      name='airtel_deposit'),
    path('deposit/card/',    views.flw_deposit_view,      name='flw_deposit'),

    # Payout — Flutterwave (bank transfer, mobile money out)
    path('payout/',          views.flw_payout_view,       name='flw_payout'),

    # Legacy withdraw URL — redirects to payout
    path('withdraw/',        views.withdraw_view,          name='withdraw'),

    # Flutterwave webhook (Risk #05: IP + secret-hash verified inside view)
    path('flw/webhook/',     views.flw_webhook,            name='flw_webhook'),

    # Exchange
    path('exchange/',        views.exchange_view,          name='exchange'),

    # P2P
    path('send/',            views.p2p_view,               name='p2p'),

    # Currencies
    path('currencies/add/',    views.add_currency_view,    name='add_currency'),
    path('currencies/remove/', views.remove_currency_view, name='remove_currency'),

    # Transactions
    path('transactions/',    views.transactions_view,      name='transactions'),

    # Rates API (Risk #09: authenticated)
    path('api/rates/',       views.rates_api_view,         name='rates_api'),

    # Health (Risk #09: no config leak)
    path('health/',          views.health_check,           name='health_check'),

    # QR Payments
    path('qr/',                     views.qr_payment_list,    name='qr_payment_list'),
    path('qr/new/',                 views.qr_payment_create,  name='qr_payment_create'),
    path('qr/<str:token>/',         views.qr_payment_detail,  name='qr_payment_detail'),
    path('qr/<str:token>/disable/', views.qr_payment_disable, name='qr_payment_disable'),
    path('pay/<str:token>/',        views.qr_pay_view,        name='qr_pay'),

    # KYC (Risk #15)
    path('kyc/',             views.kyc_start_view,         name='kyc_start'),

    # PIN reset (Risk #03)
    path('reset-pin/',         views.pin_reset_request_view, name='pin_reset_request'),
    path('reset-pin/verify/',  views.pin_reset_verify_view,  name='pin_reset_verify'),
    path('reset-pin/set/',     views.pin_reset_set_view,     name='pin_reset_set'),

    # Sandbox / Testing panel (only active when WALLET_SANDBOX_MODE=True)
    path('sandbox/',               views.sandbox_panel_view,        name='sandbox_panel'),
    path('sandbox/deposit/',       views.sandbox_deposit_view,      name='sandbox_deposit'),
    path('sandbox/withdraw/',      views.sandbox_withdraw_view,     name='sandbox_withdraw'),
    path('sandbox/bank-deposit/',  views.sandbox_bank_deposit_view, name='sandbox_bank_deposit'),
    path('sandbox/seed/',          views.sandbox_seed_view,         name='sandbox_seed'),
    path('sandbox/reset/',         views.sandbox_reset_view,        name='sandbox_reset'),
    path('sandbox/exchange/',      views.sandbox_exchange_view,     name='sandbox_exchange'),
]
