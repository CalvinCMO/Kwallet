from django.urls import path
from . import views

urlpatterns = [
    # Auth
    path('register/',        views.register_view,         name='register'),
    path('login/',           views.login_view,            name='login'),
    path('logout/',          views.logout_view,           name='logout'),

    # Dashboard
    path('',                 views.dashboard_view,        name='dashboard'),

    # Deposit — all methods on one page with tabs
    path('deposit/',         views.mpesa_deposit_view,    name='mpesa_deposit'),
    path('deposit/airtel/',  views.airtel_deposit_view,   name='airtel_deposit'),
    path('deposit/bank/',    views.bank_deposit_notify_view, name='bank_deposit_notify'),

    # Withdraw — unified landing
    path('withdraw/',              views.withdraw_view,         name='withdraw'),
    path('withdraw/mpesa/',        views.mpesa_withdraw_view,   name='mpesa_withdraw'),
    path('withdraw/airtel/',       views.airtel_withdraw_view,  name='airtel_withdraw'),
    path('withdraw/bank/',         views.bank_withdraw_view,    name='bank_withdraw'),

    # M-Pesa callbacks (Risk #05: IP + secret verified inside view)
    path('mpesa/callback/',        views.mpesa_callback,        name='mpesa_callback'),
    path('mpesa/b2c/result/',      views.mpesa_b2c_result,      name='mpesa_b2c_result'),

    # Airtel callback
    path('airtel/callback/',       views.airtel_callback,       name='airtel_callback'),

    # Bank — unified deposit + withdrawal
    path('bank/',                  views.bank_view,             name='bank'),

    # Bank webhook (PesaLink confirms deposit)
    path('bank/webhook/',          views.bank_deposit_webhook,  name='bank_deposit_webhook'),

    # STK query (Risk #02: read-only — no credit)
    path('mpesa/query/',           views.stk_query_view,        name='stk_query'),

    # Exchange
    path('exchange/',              views.exchange_view,         name='exchange'),

    # P2P
    path('send/',                  views.p2p_view,              name='p2p'),

    # Currencies
    path('currencies/add/',        views.add_currency_view,     name='add_currency'),
    path('currencies/remove/',     views.remove_currency_view,  name='remove_currency'),

    # Transactions
    path('transactions/',          views.transactions_view,     name='transactions'),

    # Rates API (Risk #09: authenticated)
    path('api/rates/',             views.rates_api_view,        name='rates_api'),

    # Health (Risk #09: no config leak)
    path('health/',                views.health_check,          name='health_check'),

    # QR Payments
    path('qr/',                    views.qr_payment_list,       name='qr_payment_list'),
    path('qr/new/',                views.qr_payment_create,     name='qr_payment_create'),
    path('qr/<str:token>/',        views.qr_payment_detail,     name='qr_payment_detail'),
    path('qr/<str:token>/disable/',views.qr_payment_disable,    name='qr_payment_disable'),
    path('pay/<str:token>/',       views.qr_pay_view,           name='qr_pay'),

    # KYC (Risk #15)
    path('kyc/',                   views.kyc_start_view,        name='kyc_start'),

    # PIN reset (Risk #03)
    path('reset-pin/',             views.pin_reset_request_view, name='pin_reset_request'),
    path('reset-pin/verify/',      views.pin_reset_verify_view,  name='pin_reset_verify'),
    path('reset-pin/set/',         views.pin_reset_set_view,     name='pin_reset_set'),
]
