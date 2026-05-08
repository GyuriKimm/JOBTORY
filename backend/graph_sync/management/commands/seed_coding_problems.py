from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from graph_sync.seed_coding_problems import seed_coding_problems


class Command(BaseCommand):
    help = "Seed coding problems, test cases, languages, and recommended videos from docker/csv_files."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset",
            action="store_true",
            help="Truncate target tables before importing the CSV fixtures.",
        )
        parser.add_argument(
            "--csv-dir",
            type=str,
            default=None,
            help="Path to the CSV directory. Defaults to docker/csv_files at the repo root.",
        )

    def handle(self, *args, **options):
        csv_dir = Path(options["csv_dir"]) if options["csv_dir"] else None
        try:
            counts = seed_coding_problems(reset=bool(options["reset"]), csv_dir=csv_dir)
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                "Seed completed: "
                f"coding_problem={counts['coding_problem']}, "
                f"test_case={counts['test_case']}, "
                f"coding_problem_language={counts['coding_problem_language']}, "
                f"recommended_videos={counts['recommended_videos']}"
            )
        )
