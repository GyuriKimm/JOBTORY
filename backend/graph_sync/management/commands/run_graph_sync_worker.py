from __future__ import annotations

import time

from django.core.management.base import BaseCommand

from graph_sync.graph_queue import start_worker


class Command(BaseCommand):
    help = "Run the graph sync background worker in the foreground."

    def handle(self, *args, **options):
        self.stdout.write(self.style.SUCCESS("Starting graph sync worker..."))
        start_worker()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Graph sync worker stopped."))
