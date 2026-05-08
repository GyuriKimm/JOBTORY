from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def main() -> int:
    try:
        from graph_sync.seed_coding_problems import seed_coding_problems
    except ModuleNotFoundError as exc:
        print(
            f"Import failed: {exc}. Run this script with the project virtual environment, "
            "for example: .venv/bin/python docker/data_db.py",
            file=sys.stderr,
        )
        return 1

    parser = argparse.ArgumentParser(
        description="Seed coding problem tables from docker/csv_files."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Truncate target tables before importing CSV fixtures.",
    )
    parser.add_argument(
        "--csv-dir",
        type=str,
        default=None,
        help="Path to the CSV directory. Defaults to docker/csv_files at the repo root.",
    )
    args = parser.parse_args()

    csv_dir = Path(args.csv_dir) if args.csv_dir else None
    counts = seed_coding_problems(reset=bool(args.reset), csv_dir=csv_dir)
    print(
        "✅ 모든 데이터 import 완료! "
        f"(coding_problem={counts['coding_problem']}, "
        f"test_case={counts['test_case']}, "
        f"coding_problem_language={counts['coding_problem_language']}, "
        f"recommended_videos={counts['recommended_videos']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
