# KWallet — Settlement & Monitoring Schedule

## Overview

Three management commands should run on a schedule in production:

| Command | Purpose | Recommended frequency |
|---|---|---|
| `resolve_orphans` | Auto-refund timed-out pending M-Pesa/Airtel/bank transactions | Every 30 minutes |
| `reconcile --strict` | Compare CurrencyBalance liability vs CompanyAccount pool balance, alert on gaps | Daily at 06:00 + after resolve_orphans |
| `pool_status` | Snapshot of all currency pool health | Every 4 hours |

`sweep_fees` is **not implemented** against the current schema (no separate
revenue-account model exists yet — see the command's module docstring) and
is intentionally excluded from this schedule until that's built.

---

## Crontab (system cron)

Edit with `crontab -e` as the user running your Django app:

```cron
# KWallet settlement engine
# Adjust /path/to/venv and /path/to/kwallet_v2 to your actual paths.

SHELL=/bin/bash
MAILTO=ops@yourdomain.com

# 1. Resolve orphaned/timed-out transactions — every 30 minutes
*/30 * * * * /path/to/venv/bin/python /path/to/kwallet_v2/manage.py resolve_orphans >> /var/log/kwallet/resolve_orphans.log 2>&1

# 2. Reconcile — 06:00 Nairobi time daily
0 6 * * * /path/to/venv/bin/python /path/to/kwallet_v2/manage.py reconcile --strict >> /var/log/kwallet/reconcile.log 2>&1

# 3. Pool status check — every 4 hours
0 */4 * * * /path/to/venv/bin/python /path/to/kwallet_v2/manage.py pool_status --problems-only >> /var/log/kwallet/pool_status.log 2>&1
```

---

## Celery Beat (if you add Celery later)

```python
# In your celery.py or settings.py CELERY_BEAT_SCHEDULE:

CELERY_BEAT_SCHEDULE = {
    'resolve-orphans': {
        'task':     'wallet.tasks.resolve_orphans',
        'schedule': crontab(minute='*/30'),
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

`CompanyAccount` rows are created automatically (via `get_or_create`) the
first time a deposit or withdrawal happens for a given currency — there's
no manual setup required before `reconcile` or `pool_status` can run; they'll
just report "nothing to reconcile" until balances exist.

If you want to seed an opening balance to match real-world float you're
holding before go-live (e.g. you're migrating from another system), edit
the `CompanyAccount.balance` field directly in the Django admin
(`/admin/wallet/companyaccount/`) for that currency.

From that point forward, every deposit and withdrawal updates
`CompanyAccount.balance` automatically via `_pool_in` / `_pool_out` in
`wallet/views.py`, with a matching `PoolLedger` entry for audit history.

---

## What each verdict means in `reconcile`

| Verdict | Meaning | Action |
|---|---|---|
| ✅ SOLVENT | `CompanyAccount.balance` ≥ sum of user `CurrencyBalance` rows for that currency. | None |
| 🔴 INSOLVENT | Real pooled money held is less than what users are owed. | **Immediate action.** Suspend withdrawals for that currency. Investigate missing PoolLedger entries or a reconciliation bug. Top up the float. |

---

## Log file locations

| Log | Location |
|---|---|
| Settlement (resolve_orphans / reconcile_pool logger output) | `logs/settlement.log` (dev) or `/var/log/kwallet/settlement.log` (prod) |
| resolve_orphans command output | `/var/log/kwallet/resolve_orphans.log` |
| Reconcile command output | `/var/log/kwallet/reconcile.log` |
| Pool status output | `/var/log/kwallet/pool_status.log` |
