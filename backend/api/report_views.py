import logging
import threading

from django.core.cache import cache
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from zoneinfo import ZoneInfo

from api.models import CodingProblem, LivecodingReport, User
from .authentication import JWTAuthentication
from .utils import _to_kst_iso
from .interview_utils import get_cached_graph
from .stt_buffer import clear_utterances

logger = logging.getLogger(__name__)


class LiveCodingFinalEvalStartView(APIView):
    """
    제출하기 버튼 트리거:
    - POST /api/livecoding/final-eval/start/
      body: {"session_id": "..."}
    - 즉시 202 반환하고, 백그라운드 스레드에서 chapter3 graph 실행 시작
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "로그인이 필요합니다."}, status=status.HTTP_401_UNAUTHORIZED
            )

        session_id = (request.data or {}).get("session_id")
        if not session_id:
            return Response(
                {"detail": "session_id가 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        meta_key = f"livecoding:{session_id}:meta"
        meta = cache.get(meta_key)
        if not meta:
            return Response(
                {"detail": "해당 세션 정보를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if str(meta.get("user_id")) != str(user.user_id):
            return Response(
                {"detail": "이 세션에 접근할 권한이 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        def _runner():
            try:
                graph = get_cached_graph("chapter3")
                config = {"configurable": {"thread_id": f"{session_id}:chapter3"}}
                init_state = {
                    "meta": {"session_id": session_id, "user_id": str(user.user_id)},
                    "step": "init",
                    "status": "running",
                }
                graph.invoke(init_state, config=config)
                try:
                    snap = graph.get_state(config)
                    values = dict(snap.values or {})
                    values["step"] = values.get("step") or "saved"
                    values["status"] = "done"
                except Exception:
                    logger.exception("final eval state normalization failed")
            except Exception:
                logger.exception("final eval runner failed")

        threading.Thread(target=_runner, daemon=True).start()
        return Response({"status": "started"}, status=status.HTTP_202_ACCEPTED)


class LiveCodingFinalEvalStatusView(APIView):
    """
    rendering.vue 폴링용:
    - GET /api/livecoding/final-eval/status/?session_id=...
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "로그인이 필요합니다."}, status=status.HTTP_401_UNAUTHORIZED
            )

        session_id = request.query_params.get("session_id")
        if not session_id:
            return Response(
                {"detail": "session_id가 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        meta_key = f"livecoding:{session_id}:meta"
        meta = cache.get(meta_key)
        if meta and str(meta.get("user_id")) != str(user.user_id):
            return Response(
                {"detail": "이 세션에 접근할 권한이 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        graph = get_cached_graph("chapter3")
        config = {"configurable": {"thread_id": f"{session_id}:chapter3"}}

        try:
            snap = graph.get_state(config)
            values = snap.values or {}
        except Exception:
            values = {}

        step = values.get("step") or "init"
        st = values.get("status") or ("done" if step == "saved" else "running")

        return Response(
            {
                "status": st,
                "step": step,
                **values,
            },
            status=status.HTTP_200_OK,
        )


class LiveCodingFinalEvalReportView(APIView):
    """
    showreport.vue 용:
    - GET /api/livecoding/final-eval/report/?session_id=...
    - done이면 report/score/grade 반환
    - 아직 running이면 202로 반환
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "로그인이 필요합니다."}, status=status.HTTP_401_UNAUTHORIZED
            )

        session_id = request.query_params.get("session_id")
        if not session_id:
            return Response(
                {"detail": "session_id가 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        meta_key = f"livecoding:{session_id}:meta"
        meta = cache.get(meta_key)
        if not meta:
            report = LivecodingReport.objects.filter(
                session_id=session_id, user=user
            ).first()
            if report:
                graph_output = report.graph_output or {}
                try:
                    from graph_sync.graph_queue import (
                        enqueue_report_sync,
                        enqueue_recommendations,
                    )

                    enqueue_report_sync(report.id)
                    if not (graph_output.get("recommended_problems") or []):
                        enqueue_recommendations(report.id, str(user.user_id), "python")
                except Exception as exc:
                    logger.exception("report queue failed: %s", exc)
                return Response(
                    {
                        "status": "done",
                        "step": "saved",
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
                    },
                    status=status.HTTP_200_OK,
                )
            return Response(
                {"detail": "해당 세션 정보를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if str(meta.get("user_id")) != str(user.user_id):
            return Response(
                {"detail": "이 세션에 접근할 권한이 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        graph = get_cached_graph("chapter3")
        config = {"configurable": {"thread_id": f"{session_id}:chapter3"}}

        try:
            snap = graph.get_state(config)
            values = dict(snap.values or {})
        except Exception as exc:
            return Response(
                {"detail": "final-eval state를 읽을 수 없습니다.", "error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        step = values.get("step") or "init"
        st = values.get("status") or ("done" if step == "saved" else "running")

        if st != "done" or step != "saved":
            return Response(
                {"status": st, "step": step},
                status=status.HTTP_202_ACCEPTED,
            )

        report_md = values.get("final_report_markdown") or ""
        graph_output = values.get("graph_output") or {}
        problem_solving_evaluation = (
            graph_output.get("problem_solving_evaluation") or {}
        )

        if not graph_output.get("problem_text"):
            try:
                problem_text = None

                if isinstance(meta, dict):
                    problem_text = (
                        meta.get("problem_text")
                        or meta.get("problem_description")
                        or meta.get("problem")
                    )

                if not problem_text:
                    meta_key = f"livecoding:{session_id}:meta"
                    cached_meta = cache.get(meta_key) or {}
                    if isinstance(cached_meta, dict):
                        problem_text = (
                            cached_meta.get("problem_text")
                            or cached_meta.get("problem_description")
                            or cached_meta.get("problem")
                        )
                        if not problem_text:
                            pd = (
                                cached_meta.get("problem_payload")
                                or cached_meta.get("problem_data")
                                or {}
                            )
                            if isinstance(pd, dict):
                                problem_text = (
                                    pd.get("problem")
                                    or pd.get("problem_text")
                                    or pd.get("problem_description")
                                )

                if not problem_text:
                    for k in (
                        f"livecoding:{session_id}:problem",
                        f"livecoding:{session_id}:problem_data",
                    ):
                        pd = cache.get(k) or {}
                        if isinstance(pd, dict):
                            problem_text = (
                                pd.get("problem")
                                or pd.get("problem_text")
                                or pd.get("problem_description")
                            )
                            if problem_text:
                                break

                if not problem_text:
                    code_key = f"livecoding:{session_id}:code"
                    code_data = cache.get(code_key) or {}
                    if isinstance(code_data, dict):
                        problem_text = (
                            code_data.get("problem_text")
                            or code_data.get("problem_description")
                            or code_data.get("problem")
                        )

                if not problem_text:
                    try:
                        problem_id = None
                        if isinstance(meta, dict):
                            problem_id = meta.get("problem_id")
                        if not problem_id:
                            cached_meta = cache.get(meta_key) or {}
                            if isinstance(cached_meta, dict):
                                problem_id = cached_meta.get("problem_id")
                        if problem_id:
                            problem_row = CodingProblem.objects.filter(
                                problem_id=problem_id
                            ).first()
                            if problem_row:
                                problem_text = problem_row.problem
                    except Exception as exc:
                        logger.exception("report_api DB problem lookup failed: %s", exc)

                if problem_text:
                    graph_output["problem_text"] = problem_text
                    values["graph_output"] = graph_output
                    logger.info(
                        "report_api attached problem_text length=%s",
                        len(problem_text),
                    )
                else:
                    values["graph_output"] = graph_output
                    logger.warning("report_api could not resolve problem_text")

            except Exception as exc:
                values["graph_output"] = graph_output
                logger.exception("report_api problem_text load failed: %s", exc)

        try:
            problem_key = f"livecoding:{session_id}:problem"
            cached_problem = cache.get(problem_key) or {}
            problem_payload = {
                "problem_id": cached_problem.get("problem_id"),
                "problem_text": cached_problem.get("problem")
                or cached_problem.get("problem_text"),
                "difficulty": cached_problem.get("difficulty"),
                "category": cached_problem.get("category"),
                "algorithm": cached_problem.get("algorithm"),
            }
            now_local = timezone.localtime(timezone.now(), ZoneInfo("Asia/Seoul"))
            defaults = {
                "user": user,
                "report_md": report_md,
                "final_score": values.get("final_score"),
                "final_grade": values.get("final_grade"),
                "problem": problem_payload,
                "code_feedback": graph_output.get("code_feedback"),
                "problem_solving_evaluation": problem_solving_evaluation,
                "initial_strategy": problem_solving_evaluation.get("initial_strategy"),
                "approach_validity": problem_solving_evaluation.get(
                    "approach_validity"
                ),
                "consistency_status": problem_solving_evaluation.get(
                    "consistency_status"
                ),
                "consistency_feedback": problem_solving_evaluation.get(
                    "consistency_feedback"
                ),
                "submitted_code": graph_output.get("submitted_code"),
                "annotated_code": graph_output.get("annotated_code"),
                "strength": graph_output.get("strength"),
                "improvement": graph_output.get("improvement"),
                "comprehensive_evaluation": graph_output.get(
                    "comprehensive_evaluation"
                ),
                "anti_cheat_summary": graph_output.get("anti_cheat_summary"),
                "graph_output": values.get("graph_output") or {},
                "problem_eval_score": values.get("problem_eval_score"),
                "problem_eval_feedback": values.get("problem_eval_feedback"),
                "code_collab_score": values.get("code_collab_score"),
                "code_collab_feedback": values.get("code_collab_feedback"),
                "problem_evidence": values.get("problem_evidence"),
                "code_collab_evidence": values.get("code_collab_evidence"),
                "updated_at": now_local,
            }
            report_obj, created = LivecodingReport.objects.update_or_create(
                session_id=session_id,
                defaults=defaults,
            )
            if created:
                report_obj.created_at = now_local
                report_obj.save(update_fields=["created_at"])
        except Exception as exc:
            logger.exception("report_api livecoding_reports upsert failed: %s", exc)
            report_obj = None

        if report_obj:
            try:
                from graph_sync.graph_queue import (
                    enqueue_report_sync,
                    enqueue_recommendations,
                )

                enqueue_report_sync(report_obj.id)
                if not (graph_output.get("recommended_problems") or []):
                    language = (meta.get("language") or "python").lower()
                    enqueue_recommendations(report_obj.id, str(user.user_id), language)
            except Exception as exc:
                logger.exception("report_api graph queue failed: %s", exc)

        try:
            mapping_key = f"livecoding:user:{user.user_id}:current_session"
            meta_key = f"livecoding:{session_id}:meta"
            code_key = f"livecoding:{session_id}:code"
            problem_key = f"livecoding:{session_id}:problem"
            anti_cheat_key = f"livecoding:{session_id}:anti-cheat-events"

            current_sid = cache.get(mapping_key)
            if str(current_sid) == str(session_id):
                cache.delete(mapping_key)
            cache.delete(meta_key)
            cache.delete(code_key)
            cache.delete(problem_key)
            cache.delete(anti_cheat_key)

            try:
                clear_utterances(str(session_id))
            except Exception:
                pass

            try:
                from .interview_utils import get_checkpointer

                cp = get_checkpointer()
                if cp is not None:
                    for chapter in (
                        "chapter1",
                        "chapter2",
                        "chapter2_hint",
                        "chapter3",
                    ):
                        thread_id = f"{session_id}:{chapter}"
                        for ns in ("__empty__", "default"):
                            try:
                                if hasattr(cp, "delete_state"):
                                    cp.delete_state(
                                        thread_id=thread_id, checkpoint_ns=ns
                                    )
                            except Exception:
                                continue
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("report_api failed to clear session caches: %s", exc)

        return Response(
            {
                "status": "done",
                "step": "saved",
                "final_report_markdown": report_md,
                "final_score": values.get("final_score"),
                "final_grade": values.get("final_grade"),
                "problem_text": graph_output.get("problem_text"),
                "code_feedback": graph_output.get("code_feedback"),
                "problem_solving_evaluation": problem_solving_evaluation,
                "initial_strategy": problem_solving_evaluation.get("initial_strategy"),
                "approach_validity": problem_solving_evaluation.get(
                    "approach_validity"
                ),
                "consistency_status": problem_solving_evaluation.get(
                    "consistency_status"
                ),
                "consistency_feedback": problem_solving_evaluation.get(
                    "consistency_feedback"
                ),
                "submitted_code": graph_output.get("submitted_code"),
                "annotated_code": graph_output.get("annotated_code"),
                "strength": graph_output.get("strength"),
                "improvement": graph_output.get("improvement"),
                "comprehensive_evaluation": graph_output.get(
                    "comprehensive_evaluation"
                ),
                "anti_cheat_summary": graph_output.get("anti_cheat_summary"),
                "graph_output": values.get("graph_output"),
            },
            status=status.HTTP_200_OK,
        )


class LiveCodingAntiCheatEventView(APIView):
    """
    프론트엔드에서 감지한 부정행위 이벤트 카운트 기록
    - POST /api/livecoding/anti-cheat/event/
      body: {"session_id": "...", "event_type": "..."}
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "로그인이 필요합니다."}, status=status.HTTP_401_UNAUTHORIZED
            )

        data = request.data or {}
        session_id = data.get("session_id")
        event_type = (data.get("event_type") or "").strip()
        if not session_id or not event_type:
            return Response(
                {"detail": "session_id와 event_type이 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        meta_key = f"livecoding:{session_id}:meta"
        meta = cache.get(meta_key)
        if not meta:
            return Response(
                {"detail": "해당 세션 정보를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if str(meta.get("user_id")) != str(user.user_id):
            return Response(
                {"detail": "이 세션에 접근할 권한이 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        allowed = {
            "typing_paste",
            "typing_copy",
            "typing_offscreen",
            "camera_blocked",
            "camera_mediapipe",
        }
        if event_type not in allowed:
            return Response(
                {"detail": "지원하지 않는 event_type입니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        key = f"livecoding:{session_id}:anti-cheat-events"
        payload = cache.get(key) or {}
        typing = payload.get("typing") or {}
        camera = payload.get("camera") or {}

        if event_type.startswith("typing_"):
            label = event_type.replace("typing_", "", 1)
            typing[label] = int(typing.get(label, 0)) + 1
        else:
            label = event_type.replace("camera_", "", 1)
            camera[label] = int(camera.get(label, 0)) + 1

        payload["typing"] = typing
        payload["camera"] = camera
        payload["updated_at"] = timezone.now().isoformat()
        cache.set(key, payload, timeout=None)

        return Response({"ok": True, "counts": payload}, status=status.HTTP_200_OK)


class LiveCodingReportListView(APIView):
    """
    DB에 저장된 최종 리포트 목록 (로그인 사용자 본인)
    GET /api/livecoding/reports/
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "로그인이 필요합니다."}, status=status.HTTP_401_UNAUTHORIZED
            )

        reports = LivecodingReport.objects.filter(user=user).order_by(
            "-created_at", "-id"
        )
        results = []
        for r in reports:
            results.append(
                {
                    "session_id": r.session_id,
                    "final_score": r.final_score,
                    "final_grade": r.final_grade,
                    "has_report": bool(r.report_md),
                    "created_at": _to_kst_iso(r.created_at),
                    "updated_at": _to_kst_iso(r.updated_at),
                }
            )

        return Response({"results": results}, status=status.HTTP_200_OK)


class LiveCodingReportTrendView(APIView):
    """
    사용자별 라이브코딩 리포트 점수 추세 (final_score만)
    GET /api/livecoding/reports/trend/
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "로그인이 필요합니다."}, status=status.HTTP_401_UNAUTHORIZED
            )

        reports = (
            LivecodingReport.objects.filter(user=user, final_score__isnull=False)
            .order_by("created_at", "id")
            .only("session_id", "final_score", "created_at")
        )

        trend = [
            {
                "session_id": r.session_id,
                "final_score": float(r.final_score)
                if r.final_score is not None
                else None,
                "created_at": _to_kst_iso(r.created_at),
            }
            for r in reports
        ]

        return Response({"results": trend}, status=status.HTTP_200_OK)


class LiveCodingReportDetailView(APIView):
    """
    DB에 저장된 최종 리포트 상세 (로그인 사용자 본인)
    GET /api/livecoding/reports/<session_id>/
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, session_id: str):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "로그인이 필요합니다."}, status=status.HTTP_401_UNAUTHORIZED
            )

        report = LivecodingReport.objects.filter(
            session_id=session_id, user=user
        ).first()
        if not report:
            return Response(
                {"detail": "리포트를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
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
                "graph_output": report.graph_output or {},
                "problem_eval_score": report.problem_eval_score,
                "problem_eval_feedback": report.problem_eval_feedback,
                "code_collab_score": report.code_collab_score,
                "code_collab_feedback": report.code_collab_feedback,
                "problem_evidence": report.problem_evidence,
                "code_collab_evidence": report.code_collab_evidence,
                "created_at": _to_kst_iso(report.created_at),
                "updated_at": _to_kst_iso(report.updated_at),
            },
            status=status.HTTP_200_OK,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def save_strategy_answer(request):
    """chapter1 전략 답변을 명시적으로 저장"""
    session_id = request.data.get("session_id")
    strategy_answer = request.data.get("strategy_answer")

    if not session_id or not strategy_answer:
        return Response(
            {"error": "session_id and strategy_answer required"}, status=400
        )

    try:
        meta_key = f"livecoding:{session_id}:meta"
        meta = cache.get(meta_key) or {}
        meta["strategy_answer"] = strategy_answer
        cache.set(meta_key, meta, timeout=None)

        logger.info(
            "save_strategy stored strategy_answer prefix=%s",
            strategy_answer[:50],
        )

        try:
            from interview_engine.graph import get_checkpointer

            checkpointer = get_checkpointer()
            config = {"configurable": {"thread_id": session_id}}
            checkpoint = checkpointer.get(config)

            if checkpoint:
                channel_values = checkpoint.get("channel_values") or {}
                chapter1 = channel_values.get("chapter1") or {}
                chapter1["user_strategy_answer"] = strategy_answer
                channel_values["chapter1"] = chapter1
                checkpoint["channel_values"] = channel_values
                checkpointer.put(config, checkpoint)

                logger.info("save_strategy checkpoint persisted")
        except Exception as exc:
            logger.warning("save_strategy checkpoint persistence failed: %s", exc)

        return Response(
            {
                "success": True,
                "message": "Strategy answer saved",
                "length": len(strategy_answer),
            }
        )

    except Exception as exc:
        logger.exception("save_strategy failed: %s", exc)
        return Response({"error": str(exc)}, status=500)
