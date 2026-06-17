web: gunicorn kwallet.wsgi --workers 2 --threads 2 --timeout 120
release: python manage.py migrate --noinput
cron: python manage.py resolve_orphans
