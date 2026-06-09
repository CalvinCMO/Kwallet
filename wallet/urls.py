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
    path('currencies/add/', views.add_currency_view, name='add_currency'),
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

from django.conf import settings
if settings.DEBUG:
    urlpatterns += [
        path('mpesa/mock-complete/<str:checkout_id>/',
             views.mpesa_mock_complete, name='mpesa_mock_complete'),
    ]
