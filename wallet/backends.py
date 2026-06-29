from django.contrib.auth.backends import BaseBackend
from .models import WalletUser


class PinBackend(BaseBackend):
    """
    Authenticates WalletUser via phone + PIN (bcrypt/pepper).
    Used by Django admin and any other authentication flow.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        # Django admin passes the USERNAME_FIELD value as 'username'
        phone = username
        if not phone or not password:
            return None

        try:
            user = WalletUser.objects.get(phone=phone)
        except WalletUser.DoesNotExist:
            return None

        if user.is_locked():
            return None

        if user.check_pin(password):
            user.record_successful_login()
            return user

        user.record_failed_login()
        return None

    def get_user(self, user_id):
        try:
            return WalletUser.objects.get(pk=user_id)
        except WalletUser.DoesNotExist:
            return None
