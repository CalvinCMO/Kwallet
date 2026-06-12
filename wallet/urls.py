from django.urls import path
from . import views

urlpatterns = [
    path('',                views.login_view,       name='login'),
    path('register/',       views.register_view,    name='register'),
    path('logout/',         views.logout_view,       name='logout'),
    path('dashboard/',      views.dashboard,         name='dashboard'),
    path('exchange/',       views.exchange_view,     name='exchange'),
    path('transfer/',       views.p2p_view,          name='p2p'),
    path('transactions/',   views.transactions_view, name='transactions'),
    path('currencies/add/', views.add_currency_view,    name='add_currency'),
    path('currencies/remove/<str:currency>/', views.remove_currency_view, name='remove_currency'),
    path('api/rates/',      views.rates_api,         name='rates_api'),
    path('health/',         views.health_check,      name='health_check'),
    path('mpesa/deposit/',                   views.mpesa_deposit_view,  name='mpesa_deposit'),
    path('mpesa/withdraw/',                  views.mpesa_withdraw_view, name='mpesa_withdraw'),
    path('mpesa/callback/',                  views.mpesa_callback,      name='mpesa_callback'),
    path('mpesa/b2c/result/',                views.b2c_result,          name='b2c_result'),
    path('mpesa/b2c/timeout/',               views.b2c_timeout,         name='b2c_timeout'),
    path('mpesa/pending/<str:checkout_id>/', views.mpesa_pending_view,  name='mpesa_pending'),
    path('mpesa/status/<str:checkout_id>/',  views.mpesa_status_api,    name='mpesa_status'),
    path('mpesa/query/<str:checkout_id>/',   views.stk_query,           name='stk_query'),
]

# ── QR Payment URLs ───────────────────────────────────────────────────────────
# Authenticated (wallet owner manages their payment requests)
urlpatterns += [
    path('qr/',                          views.qr_payment_list,    name='qr_payment_list'),
    path('qr/create/',                   views.qr_payment_create,  name='qr_payment_create'),
    path('qr/<str:token>/',              views.qr_payment_detail,  name='qr_payment_detail'),
    path('qr/<str:token>/disable/',      views.qr_payment_disable, name='qr_payment_disable'),
]

# Public (payer — no login required)
urlpatterns += [
    path('pay/<str:token>/',                              views.qr_pay_view,    name='qr_pay'),
    path('pay/<str:token>/pending/<str:checkout_id>/',    views.qr_pay_pending, name='qr_pay_pending'),
    path('pay/<str:token>/status/<str:checkout_id>/',     views.qr_pay_status,  name='qr_pay_status'),
    path('pay/<str:token>/success/',                      views.qr_pay_success, name='qr_pay_success'),
]

# ── PIN Reset ────────────────────────────────────────────────────────────────
urlpatterns += [
    path('pin/reset/',         views.pin_reset_request_view, name='pin_reset_request'),
    path('pin/reset/verify/',  views.pin_reset_verify_view,  name='pin_reset_verify'),
    path('pin/reset/set/',     views.pin_reset_set_view,     name='pin_reset_set'),
]

from django.conf import settings
if settings.DEBUG:
    urlpatterns += [
        path('mpesa/mock-complete/<str:checkout_id>/',
             views.mpesa_mock_complete, name='mpesa_mock_complete'),
    ]
