import logging
import os
import socket
import threading

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from crm.connect.scheduler import tick

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Run the connector worker: a scheduler thread that records a heartbeat and queues "
        "due syncs, plus the database task worker that runs syncs, webhook processing, "
        "and event deliveries."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--worker-id",
            default=f"{socket.gethostname()}-{os.getpid()}",
            help="Name recorded in the heartbeat; defaults to host and process id.",
        )

    def handle(self, *args, **options):
        stop = threading.Event()
        scheduler = threading.Thread(
            target=self.schedule,
            args=(options["worker_id"][:120], stop),
            name="connector-scheduler",
            daemon=True,
        )
        scheduler.start()
        try:
            call_command("db_worker")
        finally:
            stop.set()

    def schedule(self, worker_id, stop):
        while not stop.is_set():
            try:
                queued = tick(worker_id)
                if queued:
                    logger.info("Queued %s background task(s).", queued)
            except Exception:
                logger.exception("Connector scheduler tick failed.")
            finally:
                close_old_connections()
            stop.wait(settings.WORKER_TICK_SECONDS)
