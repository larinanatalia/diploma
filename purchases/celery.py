import celery

from purchases.settings import CELERY_BROKER_URL, CELERY_RESULT_BACKEND


celery_app = celery.Celery(
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND
)

celery_app.autodiscover_tasks()