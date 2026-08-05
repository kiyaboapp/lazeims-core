from celery import Celery

celery_app = Celery(
    "lazeims_central",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/1",
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    result_expires=3600,  # 1 hour
)
celery_app.autodiscover_tasks(["app.tasks"])

# Explicit import to ensure tasks are registered
import app.tasks.push_collection  # noqa: F401, E402
