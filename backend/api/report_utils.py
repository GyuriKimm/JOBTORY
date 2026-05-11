from __future__ import annotations

from typing import Any, Dict
from zoneinfo import ZoneInfo

from django.core.cache import cache
from django.utils import timezone

from api.models import CodingProblem, LivecodingReport

from .session_utils import (
    get_livecoding_code_key,
    get_livecoding_current_session_key,
    get_livecoding_meta_key,
    get_livecoding_problem_key,
)


def serialize_livecoding_report(report: LivecodingReport) -> Dict[str, Any]:
    graph_output = report.graph_output or {}
    return {
        "status": "done",
        "step": "saved",
        "session_id": report.session_id,
        "final_report_markdown": report.report_md or "",
        "final_score": report.final_score,
        "final_grade": report.final_grade,
        "problem_text": (report.problem or {}).get("problem_text")
        if hasattr(report, "problem")
        else None,
        "code_feedback": report.code_feedback,
        "problem_solving_evaluation": report.problem_solving_evaluation,
        "initial_strategy": report.initial_strategy,
        "approach_validity": report.approach_validity,
        "consistency_status": report.consistency_status,
        "consistency_feedback": report.consistency_feedback,
        "submitted_code": report.submitted_code,
        "annotated_code": report.annotated_code,
        "strength": report.strength,
        "improvement": report.improvement,
        "comprehensive_evaluation": report.comprehensive_evaluation,
        "anti_cheat_summary": report.anti_cheat_summary,
        "graph_output": graph_output,
        "problem_eval_score": report.problem_eval_score,
        "problem_eval_feedback": report.problem_eval_feedback,
        "code_collab_score": report.code_collab_score,
        "code_collab_feedback": report.code_collab_feedback,
        "problem_evidence": report.problem_evidence,
        "code_collab_evidence": report.code_collab_evidence,
        "created_at": _to_kst_iso(report.created_at),
        "updated_at": _to_kst_iso(report.updated_at),
    }


def build_problem_payload(session_id: str, meta: Dict[str, Any]) -> Dict[str, Any]:
    cached_problem = cache.get(get_livecoding_problem_key(session_id)) or {}
    return {
        "problem_id": cached_problem.get("problem_id"),
        "problem_text": cached_problem.get("problem") or cached_problem.get("problem_text"),
        "difficulty": cached_problem.get("difficulty"),
        "category": cached_problem.get("category"),
        "algorithm": cached_problem.get("algorithm"),
    }


def clear_livecoding_session_cache(session_id: str, user_id: str | None = None) -> None:
    mapping_key = get_livecoding_current_session_key(user_id) if user_id is not None else None
    if mapping_key:
        current_sid = cache.get(mapping_key)
        if str(current_sid) == str(session_id):
            cache.delete(mapping_key)
    cache.delete(get_livecoding_meta_key(session_id))
    cache.delete(get_livecoding_code_key(session_id))
    cache.delete(get_livecoding_problem_key(session_id))
    cache.delete(f"livecoding:{session_id}:anti-cheat-events")


def resolve_problem_text(session_id: str, meta: Dict[str, Any]) -> str | None:
    problem_text = None
    if isinstance(meta, dict):
        problem_text = (
            meta.get("problem_text")
            or meta.get("problem_description")
            or meta.get("problem")
        )

    if problem_text:
        return problem_text

    cached_meta = cache.get(get_livecoding_meta_key(session_id)) or {}
    if isinstance(cached_meta, dict):
        problem_text = (
            cached_meta.get("problem_text")
            or cached_meta.get("problem_description")
            or cached_meta.get("problem")
        )
        if not problem_text:
            pd = cached_meta.get("problem_payload") or cached_meta.get("problem_data") or {}
            if isinstance(pd, dict):
                problem_text = (
                    pd.get("problem")
                    or pd.get("problem_text")
                    or pd.get("problem_description")
                )
    if problem_text:
        return problem_text

    for key in (
        get_livecoding_problem_key(session_id),
        f"livecoding:{session_id}:problem_data",
    ):
        pd = cache.get(key) or {}
        if isinstance(pd, dict):
            problem_text = (
                pd.get("problem") or pd.get("problem_text") or pd.get("problem_description")
            )
            if problem_text:
                return problem_text

    return None


def resolve_problem_text_from_db(session_id: str, meta: Dict[str, Any]) -> str | None:
    problem_id = None
    if isinstance(meta, dict):
        problem_id = meta.get("problem_id")
    if not problem_id:
        cached_meta = cache.get(get_livecoding_meta_key(session_id)) or {}
        if isinstance(cached_meta, dict):
            problem_id = cached_meta.get("problem_id")
    if not problem_id:
        return None
    problem_row = CodingProblem.objects.filter(problem_id=problem_id).first()
    if problem_row:
        return problem_row.problem
    return None


def _to_kst_iso(dt) -> str | None:
    if not dt:
        return None
    aware_dt = dt
    if timezone.is_naive(aware_dt):
        aware_dt = timezone.make_aware(aware_dt, timezone=timezone.utc)
    return timezone.localtime(aware_dt, ZoneInfo("Asia/Seoul")).isoformat()
