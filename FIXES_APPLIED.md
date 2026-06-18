# Fixes applied — 2026-06-18

Patched copy based on the uploaded `Kwallet_comp.zip`. `.env` and `.git/`
were intentionally excluded from this copy (secrets / VCS history) — copy
your real `.env` back in before running this.

## 0. Random logouts in production (added after initial review)

**File:** `kwallet/settings.py`

`SESSION_ENGINE` was hardcoded to `'django.contrib.sessions.backends.cache'`,
backed by `CACHES['default']`, which itself falls back to `LocMemCache`
whenever `REDIS_URL` isn't set. `LocMemCache` is private to a single
process — it is NOT shared across gunicorn's worker processes (your
Procfile/railway config boot 2 workers; confirmed in your logs as
`pid: 5` and `pid: 6`).

Effect: when `REDIS_URL` is unset (your case), each gunicorn worker has its
own independent session store. A login handled by worker 5 writes the
session into worker 5's memory only; if the next request from the same
browser lands on worker 6 (gunicorn round-robins requests), worker 6 has
never seen that session ID and treats the user as logged out — even
though the `sessionid` cookie is present and valid. This produces exactly
the symptom reported: dashboard loads fine right after login, then a
later request randomly shows logged-out, with zero errors in the log
(Django is behaving correctly given what it can see — there's no
exception, the session genuinely isn't present in that worker's cache).

**Fix:** `SESSION_ENGINE` now only uses the cache backend when `REDIS_URL`
is actually set (a real shared store across workers). Otherwise it falls
back to `django.contrib.sessions.backends.db` — the database, which is
already shared across all workers via Postgres, and requires no new
migration (the `django_session` table already exists; `sessions` is in
`INSTALLED_APPS` and was already migrated per your log).

**Verified:** simulated two fully independent Python processes (no shared
memory — equivalent to two separate gunicorn workers) sharing only the
sqlite-backed session table. Process A logged in; process B, a brand-new
process, correctly recognized the session and rendered the dashboard
(200, no redirect). Re-running the same test forcing the old
`cache`-backed engine reproduced the bug exactly: process B got bounced
to `/login/` (302) despite holding a valid session cookie.

**Note:** the rate limiter (`_check_rate_limit`) and the M-Pesa OAuth
token / exchange-rate caches in `mpesa.py` / `rates.py` also use this same
cache and are technically also per-process without Redis — but those only
degrade gracefully (rate limits get counted per-worker instead of
globally, so effective limits are looser than configured; tokens/rates get
fetched slightly more often). Neither produces an incorrect user-facing
result the way sessions did, so they were left as-is. If you add Redis
later, all of these — including sessions — automatically become properly
shared with no further code changes needed.

## 1. Dashboard 500 → login-redirect-loop (the originally reported bug)

**File:** `wallet/templates/wallet/dashboard.html`

`{{ wallet.user.get_full_name|default:wallet.user.username }}` and
`{{ wallet.user.first_name|... }}` — `Wallet` has no `user` attribute (the
real field is `wallet_user`, and it's nullable). This threw
`VariableDoesNotExist` on every dashboard load after a successful login,
which is what was actually causing the "redirected back to login" symptom:
login succeeded, the dashboard redirect target then 500'd, and something
downstream (proxy/frontend) treated the non-200 as "not logged in."

**Fix:** use `user` (already in every template via Django's auth context
processor, and guaranteed to be the logged-in `WalletUser` at this point)
instead of going through `wallet.user`. Also swapped the `username` fallback
for `phone`, since `WalletUser` has no `username` field.

## 2. Same bug, second location

**File:** `wallet/templates/wallet/kyc_start.html`

Identical `{{ wallet.user.get_full_name }}` bug in the KYC form's name
pre-fill. Would have 500'd the moment any pending-KYC user clicked
"Verify →" from the dashboard banner. Fixed the same way.

## 3. Airtel callback — missing import, guaranteed crash

**File:** `wallet/airtel.py`

`verify_callback_secret` calls `hmac.compare_digest(...)` but `hmac` was
never imported in this file. Any real Airtel callback (or any POST to
`/airtel/callback/` once `AIRTEL_CALLBACK_SECRET` is set) would raise
`NameError`. Added `import hmac`.

## 4. Airtel callback — missing IP allowlist check

**Files:** `wallet/airtel.py`, `wallet/views.py`

`AIRTEL_CONFIG['ALLOWED_CALLBACK_IPS']` was defined in settings but never
used anywhere — `airtel_callback` only checked the shared secret, not the
IP, unlike the M-Pesa callback which checks both. Added
`AirtelClient.verify_callback_ip()` (mirrors `MpesaClient`'s method, fails
closed if no allowlist is configured) and wired it into `airtel_callback`.

**Behavior change to be aware of:** if `AIRTEL_CALLBACK_IPS` is not set in
your environment, real Airtel callbacks will now be rejected (403) until
you configure it. This is intentional — failing open on an unconfigured
IP allowlist is a security gap, not a convenience.

## 5. Three broken management commands

**Files:** `wallet/management/commands/reconcile.py`,
`wallet/management/commands/pool_status.py`,
`wallet/management/commands/sweep_fees.py`

All three imported functions/fields that don't exist in the current
`wallet/models.py` / `wallet/settlement.py` (`FeeRecord`,
`CompanyAccount.account_type`, `CompanyAccount.ledger_balance`,
`wallet.settlement.reconcile/pool_status/sweep_fees`). They would have
crashed immediately on every invocation — including if you ever wire up
the cron schedule documented in `CRONTAB.md`, which assumes these work.

**Fix:**
- `reconcile.py` and `pool_status.py` rewritten against the functions that
  actually exist (`wallet.settlement.reconcile_pool()`), reporting
  pool balance vs. user liability per currency.
- `sweep_fees.py` replaced with an explicit `CommandError` explaining what
  schema/model work is needed before fee-sweeping can be implemented,
  rather than silently no-op'ing or operating on nonexistent fields.
- `CRONTAB.md` updated to schedule `resolve_orphans` (which already worked
  correctly) instead of the non-functional `sweep_fees`, and corrected the
  setup checklist / verdict table to match the real `CompanyAccount` model.

## 6. X-Forwarded-For parsed incorrectly (3 locations)

**File:** `wallet/views.py`

`request.META.get('HTTP_X_FORWARDED_FOR', ...)` was used as-is in the login
IP rate limiter and both M-Pesa callback IP checks. Behind a proxy (e.g.
Railway), this header is a comma-separated chain of IPs, not a single IP —
using it raw produces invalid/inconsistent rate-limit cache keys (visible
in your logs as `CacheKeyWarning`) and is fragile for prefix-based IP
allowlist matching.

**Fix:** added `get_client_ip(request)` helper that takes the first entry
in the X-Forwarded-For chain (the original client IP) and falls back to
`REMOTE_ADDR`. Replaced all three call sites (`login_view`, `mpesa_callback`,
`mpesa_b2c_result`) to use it. The Airtel callback's new IP check (see #4)
uses it too.

---

## Verified

- `python manage.py check` — no issues.
- All edited files pass `py_compile`.
- End-to-end test: created a user/wallet, logged in via the test client,
  confirmed `/` now returns 200 with no redirect chain (previously 500 →
  bounced to `/login/`), confirmed the user's name renders correctly on
  both the dashboard and KYC pages.

## Not fixed (flagged for your decision, not changed)

These were found during the audit but intentionally left as-is since they
either need a product decision or aren't currently causing harm:

- `forms.py` is entirely incompatible with the current schema and unused
  by any view — left in place in case you want to revive/rewrite it rather
  than delete it outright.
- `wallet/templates/wallet/mpesa_pending.html` and
  `wallet/templates/wallet/mpesa_withdraw.html` are orphaned templates
  (never rendered by any view) referencing URL names that don't exist
  (`mpesa_status`, `mpesa_mock_complete`).
- `register.html`, `p2p.html`, `add_currency.html` reference a `form`
  template variable that's never passed into context by the corresponding
  views (they build forms manually instead) — silently degrades to no
  inline per-field error text; the `messages` framework banner still shows
  errors correctly, so this is cosmetic, not a crash.
- `kwallet/settings_production.py` is never loaded (`wsgi.py` and
  `railway.json` both default to `kwallet.settings`) unless
  `DJANGO_SETTINGS_MODULE=kwallet.settings_production` is set manually in
  your Railway dashboard, as `SETUP.md` instructs. Worth double-checking
  that variable is actually set in your live environment — if it isn't,
  you're running on `settings.py`'s defaults instead of the hardened
  production file.
- `mpesa.py`: callback IP/secret checks both pass automatically when
  `MPESA_USE_MOCK=True` and no secret is configured. Fine for sandbox;
  confirm `MPESA_USE_MOCK=False` and `MPESA_CALLBACK_SECRET` are actually
  set in production.
- `qr_payment_detail` view: `request.user.wallet` will raise
  `Wallet.DoesNotExist` (not return `None`) for an authenticated user with
  no wallet, unlike the `wallet_required` decorator elsewhere which guards
  this case explicitly.
