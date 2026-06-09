# KWallet — Settlement & Monitoring Schedule

## Overview

Three management commands must run on a schedule in production:

| Command | Purpose | Recommended frequency |
|---|---|---|
| `sweep_fees --all` | Move collected fees from client float → company revenue | Daily at 02:00 |
| `reconcile --all --strict` | Compare ledger vs pool, alert on gaps | Daily at 06:00 + after every sweep |
| `pool_status` | Snapshot of all account health | Every 4 hours |

---

## Crontab (system cron)

Edit with `crontab -e` as the user running your Django app:

```cron
# KWallet settlement engine
# Adjust /path/to/venv and /path/to/kwallet_v2 to your actual paths.

SHELL=/bin/bash
MAILTO=ops@yourdomain.com

# 1. Fee sweep — 02:00 Nairobi time daily
0 2 * * * /path/to/venv/bin/python /path/to/kwallet_v2/manage.py sweep_fees --all >> /var/log/kwallet/sweep.log 2>&1

# 2. Reconcile — 06:00 Nairobi time daily (after sweep has completed)
0 6 * * * /path/to/venv/bin/python /path/to/kwallet_v2/manage.py reconcile --all --strict >> /var/log/kwallet/reconcile.log 2>&1

# 3. Pool status check — every 4 hours
0 */4 * * * /path/to/venv/bin/python /path/to/kwallet_v2/manage.py pool_status --problems-only >> /var/log/kwallet/pool_status.log 2>&1
```

---

## Celery Beat (if you add Celery later)

```python
# In your celery.py or settings.py CELERY_BEAT_SCHEDULE:

CELERY_BEAT_SCHEDULE = {
    'sweep-fees-daily': {
        'task':     'wallet.tasks.sweep_all_fees',
        'schedule': crontab(hour=2, minute=0),     # 02:00 UTC (adjust for Nairobi = UTC+3)
    },
    'reconcile-daily': {
        'task':     'wallet.tasks.reconcile_all',
        'schedule': crontab(hour=6, minute=0),
    },
    'pool-status-check': {
        'task':     'wallet.tasks.check_pool_status',
        'schedule': crontab(minute=0, hour='*/4'),
    },
}
```

---

## First-time setup checklist

Before the sweep and reconcile commands can run, you must create
CompanyAccount records in the Django admin panel (`/admin/wallet/companyaccount/`).

### Required accounts (minimum):

**For KES (M-Pesa):**

| Field | Client Float | Company Revenue |
|---|---|---|
| Name | M-Pesa Client Float KES | M-Pesa Revenue KES |
| Account type | Client Float (Segregated) | Company Revenue |
| Rail | M-Pesa Paybill / Till | M-Pesa Paybill / Till |
| Currency | KES | KES |
| Identifier | Your Paybill number | Your revenue Till number |

Repeat for every currency you hold real funds in (TZS, UGX, USD, etc.).

### After creating accounts — seed the opening balances:

The `ledger_balance` field starts at 0. You need to set it to the actual
balance in your real M-Pesa / bank account on the day you go live.
Do this via the Django admin: edit the CompanyAccount and set `ledger_balance`
to the real opening balance. Document this as the "go-live adjustment" in the
notes field.

From that point forward, every deposit and withdrawal will update the balance
automatically via the PoolLedger.

---

## What each verdict means in `reconcile`

| Verdict | Meaning | Action |
|---|---|---|
| ✅ HEALTHY | Gap = unsettled fees. All good. | None |
| 🟡 WARNING | Gap ≠ unsettled fees by > 1%. A movement may not be recorded. | Review PoolLedger for missing entries. Check M-Pesa statement. |
| 🔴 INSOLVENT | User liabilities > real money held. | **Immediate action.** Suspend withdrawals. Top up the float account. Investigate. |

---

## Log file locations

| Log | Location |
|---|---|
| Settlement / sweep | `logs/settlement.log` (dev) or `/var/log/kwallet/settlement.log` (prod) |
| Sweep command output | `/var/log/kwallet/sweep.log` |
| Reconcile command output | `/var/log/kwallet/reconcile.log` |
| Pool status output | `/var/log/kwallet/pool_status.log` |
