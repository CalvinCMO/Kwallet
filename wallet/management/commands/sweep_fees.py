"""
Management command: sweep_fees
==============================
STATUS: NOT IMPLEMENTED against the current schema.

This command previously assumed a separate `FeeRecord` model and a
"client float account -> company revenue account" sweep, neither of
which exist in the current wallet/models.py or wallet/settlement.py.

In the current schema, fees are stored directly on each `Transaction.fee`
field and are already reflected in `CompanyAccount.balance` via
`_pool_in` / `_pool_out` in wallet/views.py — there is currently no
separate revenue account to sweep collected fees into.

If you need fee-sweeping (e.g. periodically moving accumulated fee
revenue out of the operational float into a separate revenue account),
that requires:
  1. A revenue-tracking model/field (e.g. a second CompanyAccount per
     currency marked as a revenue account, or a FeeRecord model).
  2. A migration adding it.
  3. Sweep logic in wallet/settlement.py to move the swept amount.

Until that's built, this command intentionally exits with an explanatory
error rather than silently doing nothing or operating on fields that
don't exist.
"""

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = 'NOT IMPLEMENTED — see module docstring for what is needed before this can run.'

    def add_arguments(self, parser):
        parser.add_argument('--currency', type=str, metavar='CUR')
        parser.add_argument('--all', action='store_true')
        parser.add_argument('--dry-run', action='store_true')

    def handle(self, *args, **options):
        raise CommandError(
            "sweep_fees is not implemented for the current schema. "
            "Fees are tracked on Transaction.fee and already flow into "
            "CompanyAccount.balance via _pool_in/_pool_out — there is no "
            "separate revenue account to sweep into yet. See this file's "
            "module docstring for what would need to be built first."
        )
