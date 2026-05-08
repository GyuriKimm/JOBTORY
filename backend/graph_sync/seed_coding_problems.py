from __future__ import annotations

import ast
import csv
import json
import os
from pathlib import Path
from typing import Any

import psycopg2
from psycopg2 import sql


DEFAULT_CSV_DIR = Path(__file__).resolve().parents[2] / "docker" / "csv_files"
TRUNCATE_TABLES_SQL = (
    "TRUNCATE TABLE coding_problem_language, test_case, coding_problem, recommended_videos "
    "RESTART IDENTITY CASCADE;"
)


def _db_params() -> dict[str, str]:
    return {
        "database": os.getenv("POSTGRES_DB") or os.getenv("DB_NAME", "jobtory"),
        "user": os.getenv("POSTGRES_USER") or os.getenv("DB_USER", "gyulcross"),
        "password": os.getenv("POSTGRES_PASSWORD")
        or os.getenv("DB_PASSWORD", "gyulcross0113"),
        "host": os.getenv("POSTGRES_HOST") or os.getenv("DB_HOST", "localhost"),
        "port": os.getenv("POSTGRES_PORT") or os.getenv("DB_PORT", "5432"),
    }


def _connect():
    return psycopg2.connect(**_db_params())


def _parse_algorithm(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        try:
            return json.dumps(ast.literal_eval(text))
        except (ValueError, SyntaxError):
            return json.dumps([text])


def _parse_array(cell: Any, field_name: str) -> list[Any]:
    if not cell or not str(cell).strip():
        return []
    text = str(cell).strip()
    try:
        parsed = json.loads(text)
    except Exception:
        try:
            parsed = ast.literal_eval(text)
        except Exception:
            raise ValueError(f"{field_name} 파싱 실패: {text[:80]}")
    if isinstance(parsed, list):
        return parsed
    return [parsed]


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def seed_coding_problems(
    *, reset: bool = False, csv_dir: Path | str | None = None
) -> dict[str, int]:
    csv_dir_path = Path(csv_dir) if csv_dir else DEFAULT_CSV_DIR
    if not csv_dir_path.exists():
        raise FileNotFoundError(f"csv dir not found: {csv_dir_path}")

    problems_path = csv_dir_path / "coding_problems.csv"
    testcases_path = csv_dir_path / "coding_problems_testcases.csv"
    languages_path = csv_dir_path / "coding_problem_language.csv"
    videos_path = csv_dir_path / "final_data.csv"

    if not problems_path.exists():
        raise FileNotFoundError(f"missing csv: {problems_path}")
    if not testcases_path.exists():
        raise FileNotFoundError(f"missing csv: {testcases_path}")
    if not languages_path.exists():
        raise FileNotFoundError(f"missing csv: {languages_path}")
    if not videos_path.exists():
        raise FileNotFoundError(f"missing csv: {videos_path}")

    conn = _connect()
    cur = conn.cursor()
    counts = {
        "coding_problem": 0,
        "test_case": 0,
        "coding_problem_language": 0,
        "recommended_videos": 0,
    }

    try:
        if reset:
            cur.execute(TRUNCATE_TABLES_SQL)
            conn.commit()
        else:
            for table_name in (
                "coding_problem",
                "test_case",
                "coding_problem_language",
                "recommended_videos",
            ):
                cur.execute(f"SELECT COUNT(*) FROM {table_name}")
                if cur.fetchone()[0] > 0:
                    raise RuntimeError(
                        "seed data already exists. Use --reset to reimport the CSV fixtures."
                    )
            # 모든 대상 테이블이 비어 있을 때만 초기 적재를 진행한다.
            # 부분 적재 상태를 묵인하면 중복/불일치가 생기기 쉬워서 여기서 끊는다.

        for row in _load_csv_rows(problems_path):
            cur.execute(
                "INSERT INTO coding_problem (problem_id, problem, difficulty, category, algorithm) "
                "VALUES (%s, %s, %s, %s, %s)",
                (
                    row["problem_id"],
                    row["problem"],
                    row["difficulty"],
                    row["category"],
                    _parse_algorithm(row.get("algorithm")),
                ),
            )
            counts["coding_problem"] += 1
        conn.commit()

        for row in _load_csv_rows(testcases_path):
            cur.execute(
                "INSERT INTO test_case (problem_id, input, output) VALUES (%s, %s, %s)",
                (row["problem_id"], row["input"], row["output"]),
            )
            counts["test_case"] += 1
        conn.commit()

        for row in _load_csv_rows(languages_path):
            cur.execute(
                "INSERT INTO coding_problem_language (problem_id, function_name, starter_code, language) "
                "VALUES (%s, %s, %s, %s)",
                (
                    row["problem_id"],
                    row["function_name"],
                    row["starter_code"],
                    row["language"],
                ),
            )
            counts["coding_problem_language"] += 1
        conn.commit()

        cur.execute(
            sql.SQL(
                "SELECT setval("
                "pg_get_serial_sequence(%s, %s), "
                "COALESCE((SELECT MAX({col}) FROM {tbl}), 0) + 1, "
                "false"
                ");"
            ).format(
                tbl=sql.Identifier("coding_problem"), col=sql.Identifier("problem_id")
            ),
            ("coding_problem", "problem_id"),
        )
        conn.commit()

        for row in _load_csv_rows(videos_path):
            code_lang = _parse_array(row.get("code_lang"), "code_lang")
            category = _parse_array(row.get("category"), "category")
            domain = row.get("domain") or "algorithm"
            cur.execute(
                "INSERT INTO recommended_videos (id, code_lang, video_url, summary, category, domain) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    int(row["id"]) if row.get("id") else None,
                    code_lang,
                    row["video_url"],
                    row["summary"],
                    category,
                    domain,
                ),
            )
            counts["recommended_videos"] += 1
        conn.commit()
        return counts
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
