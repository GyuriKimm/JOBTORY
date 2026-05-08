import logging
import os
import sys

import django

logger = logging.getLogger(__name__)


def _setup_django():
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    backend_root = os.path.join(repo_root, "backend")
    sys.path.insert(0, backend_root)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()


def main():
    _setup_django()
    from api.models import LivecodingReport
    from graph_sync.service import ensure_graph_sources, sync_report_to_graph
    from graph_sync.user_similarity import update_all_user_similarity

    ensure_graph_sources()
    qs = LivecodingReport.objects.select_related("user").order_by("created_at", "id")
    for report in qs.iterator(chunk_size=200):
        try:
            sync_report_to_graph(report, report.user, report.graph_output or {})
        except Exception as exc:
            logger.exception(
                "graph_sync report sync failed: session_id=%s error=%s",
                report.session_id,
                exc,
            )

    update_all_user_similarity(top_k=20)


if __name__ == "__main__":
    main()
