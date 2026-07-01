"""
middleware.py — KWallet security middleware

1. IdleTimeoutMiddleware  — logs out users after IDLE_TIMEOUT_SECONDS of inactivity
2. SingleDeviceMiddleware — enforces one active session per user; kicks older sessions
"""

import logging
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout as auth_logout
from django.shortcuts import redirect
from django.utils import timezone

logger = logging.getLogger(__name__)

# ── Configurable constants (override in settings.py) ────────────────────────
IDLE_TIMEOUT_SECONDS = getattr(settings, 'IDLE_TIMEOUT_SECONDS', 300)   # 5 minutes
EXEMPT_PATHS = getattr(settings, 'SESSION_EXEMPT_PATHS', [
    '/login/', '/register/', '/logout/', '/idle-ping/',
    '/mpesa/callback/', '/mpesa/b2c/result/', '/airtel/callback/',
    '/bank/webhook/', '/health/',
    '/static/', '/media/',
    # Django admin has its own session/permission model and its own
    # (much longer) usage pattern than the customer-facing wallet UI.
    # Without this, IdleTimeoutMiddleware silently logs staff out of
    # /admin/ after 5 minutes and redirects them to the customer /login/
    # page with a confusing "logged out after 5 minutes" message that has
    # nothing to do with the admin credentials themselves.
    '/admin/',
])


def _is_exempt(path: str) -> bool:
    """Return True if path should bypass session checks (callbacks, static, etc.)."""
    return any(path.startswith(p) for p in EXEMPT_PATHS)


class IdleTimeoutMiddleware:
    """
    Logs a user out and redirects to /login/ if they have been idle for
    IDLE_TIMEOUT_SECONDS (default: 300 s / 5 minutes).

    Activity is tracked by updating WalletUser.last_activity on every
    authenticated, non-exempt request. The middleware reads that field
    rather than storing the timestamp in the session so it is accurate
    across cache/session restarts.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.user.is_authenticated and not _is_exempt(request.path):
            user = request.user
            try:
                if user.is_idle(IDLE_TIMEOUT_SECONDS):
                    logger.info(
                        'Idle timeout for user %s after %ds',
                        user.phone, IDLE_TIMEOUT_SECONDS,
                    )
                    auth_logout(request)
                    # Add message to the *new* request context via session
                    request.session['idle_timeout_msg'] = (
                        f'You were logged out after {IDLE_TIMEOUT_SECONDS // 60} minutes '
                        'of inactivity. Please log in again.'
                    )
                    return redirect('login')
                else:
                    # Touch only every 30 s to avoid excessive DB writes
                    last = user.last_activity
                    if last is None or (timezone.now() - last).total_seconds() > 30:
                        user.touch_activity()
            except Exception:
                logger.exception('IdleTimeoutMiddleware error')

        response = self.get_response(request)
        return response


class SingleDeviceMiddleware:
    """
    Enforces one active login session per wallet user.

    When a user logs in, login_view calls user.register_session(session_key),
    which stores the current session key in WalletUser.active_session_key.

    On every subsequent authenticated request this middleware compares the
    current session key to the stored one.  If they differ, the current
    session is from an *older* device — it is invalidated and the user is
    redirected to /login/ with an explanatory message.

    Edge-cases:
    - Staff / superusers are exempt (admin panel needs multi-device access).
    - Callbacks and static files are exempt.
    - If active_session_key is blank (legacy users) the check is skipped
      and the field is populated on the next successful login.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.user.is_authenticated
            and not request.user.is_staff
            and not _is_exempt(request.path)
        ):
            user = request.user
            try:
                stored_key = user.active_session_key
                current_key = request.session.session_key

                if stored_key and current_key and stored_key != current_key:
                    # This session was superseded by a login on another device
                    logger.warning(
                        'Single-device kick: user %s session %s vs stored %s',
                        user.phone, current_key[:8], stored_key[:8],
                    )
                    auth_logout(request)
                    request.session['device_kick_msg'] = (
                        'Your account was logged in on another device. '
                        'If this wasn\'t you, change your PIN immediately.'
                    )
                    return redirect('login')
            except Exception:
                logger.exception('SingleDeviceMiddleware error')

        response = self.get_response(request)
        return response


# ── Template context processor ───────────────────────────────────────────────

def idle_timeout_context(request):
    """Inject IDLE_TIMEOUT_SECONDS and sandbox mode into every template context."""
    from django.conf import settings as _s
    return {
        'IDLE_TIMEOUT_SECONDS': getattr(_s, 'IDLE_TIMEOUT_SECONDS', 300),
        'GLOBAL_SANDBOX_MODE':  getattr(_s, 'WALLET_SANDBOX_MODE', True),
    }