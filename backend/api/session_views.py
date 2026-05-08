import logging
import secrets
from datetime import datetime

from django.core.cache import cache
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import JWTAuthentication
from .interview_utils import _generate_tts_payload, get_cached_graph
from .models import CodingProblemLanguage, TestCase, User
from .stt_buffer import clear_utterances

logger = logging.getLogger(__name__)


class LiveCodingStartView(APIView):
    """
    라이브 코딩 세션을 시작하면서 세션 정보를 저장하는 엔드포인트.
    - Authorization: Bearer <access_token> (LoginView/GoogleAuthView에서 발급한 토큰)
    - 저장되는 키: livecoding:{session_id}:meta,
      값: { state, problem_id, user_id, session_id }
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "라이브 코딩을 시작하려면 로그인이 필요합니다."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        problem_data = request.data.get("problem_data")
        if not isinstance(problem_data, dict):
            return Response({"detail": "problem_data가 필요합니다."}, status=400)
        required = [
            "problem_id",
            "problem",
            "difficulty",
            "category",
            "language",
            "function_name",
            "starter_code",
            "test_cases",
            "algorithm",
        ]
        missing = [k for k in required if k not in problem_data]
        if missing:
            return Response(
                {"detail": f"problem_data 필드 누락: {', '.join(missing)}"}, status=400
            )
        session_id = secrets.token_hex(16)
        start_at = timezone.now()  # Redis(캐시)에 저장할 세션 메타 정보
        meta = {
            "stage": "intro",
            "user_id": user.user_id,
            "session_id": session_id,
            "problem_id": problem_data["problem_id"],
            "language": problem_data["language"],
            "starter_code": problem_data["starter_code"],
            "time_limit_seconds": int(
                problem_data.get("time_limit_seconds") or 40 * 60
            ),
            "start_at": start_at.isoformat(),
            # remaining_seconds / timer_paused 는 사용자가 실제로
            # 문제 화면을 떠날 때에만 TimerUpdateView를 통해 설정한다.
        }
        problem_payload = {
            "problem_id": problem_data["problem_id"],
            "problem": problem_data["problem"],
            "difficulty": problem_data["difficulty"],
            "category": problem_data["category"],
            "language": problem_data["language"],
            "function_name": problem_data["function_name"],
            "starter_code": problem_data["starter_code"],
            "test_cases": problem_data["test_cases"],
            "algorithm": problem_data["algorithm"],
        }

        cache.set(f"livecoding:{session_id}:meta", meta, timeout=None)
        cache.set(
            f"livecoding:{session_id}:problem",
            problem_payload,
            timeout=None,
        )
        cache.set(
            f"livecoding:user:{user.user_id}:current_session",
            session_id,
            timeout=None,
        )
        return Response(
            {
                "session_id": session_id,
                "state": meta["stage"],
                "time_limit_seconds": meta["time_limit_seconds"],
                "start_at": meta["start_at"],
                "remaining_seconds": meta["time_limit_seconds"],
                "problem_data": problem_payload,
            },
            status=status.HTTP_201_CREATED,
        )


class LiveCodingCodeSnapshotView(APIView):
    """
    라이브 코딩 세션 중 작성 중인 코드를 Redis(cache)에 지속적으로 저장/조회하는 엔드포인트.
    - POST: 코드 스냅샷 저장
      body: { "session_id": "...", "language": "python3", "code": "..." }
    - GET: 마지막(또는 언어별 마지막) 코드 스냅샷 조회
      query: ?session_id=...&language=python3 (language는 선택)
    """

    def _get_and_validate_meta(self, user, session_id: str):
        if not session_id:
            return None, Response(
                {"detail": "session_id가 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        meta_key = f"livecoding:{session_id}:meta"
        meta = cache.get(meta_key)
        if not meta:
            return None, Response(
                {"detail": "해당 세션 정보를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if str(meta.get("user_id")) != str(getattr(user, "user_id", None)):
            return None, Response(
                {"detail": "이 세션에 접근할 권한이 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        return meta, None

    def post(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "코드를 저장하려면 로그인이 필요합니다."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        session_id = request.data.get("session_id")
        code = request.data.get("code")
        language = (request.data.get("language") or "").lower() or None

        if code is None:
            return Response(
                {"detail": "code 필드는 비워둘 수 없습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        meta, error_response = self._get_and_validate_meta(user, session_id)
        if error_response is not None:
            return error_response

        if not language:
            language = (meta.get("language") or "").lower() or None

        snapshot = {
            "code": str(code),
            "language": language,
            "saved_at": timezone.now().isoformat(),
        }

        key = f"livecoding:{session_id}:code"
        data = cache.get(key) or {}
        history = data.get("history") or []
        is_first_snapshot = len(history) == 0
        history.append(snapshot)

        if len(history) > 200:
            history = history[-200:]

        if is_first_snapshot and "initial_code" not in data:
            data["initial_code"] = snapshot.get("code") or ""

        data["latest"] = snapshot
        data["history"] = history
        cache.set(key, data, timeout=None)

        return Response(
            {"saved": True, "snapshot": snapshot},
            status=status.HTTP_201_CREATED,
        )

    def get(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "코드를 조회하려면 로그인이 필요합니다."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        session_id = request.query_params.get("session_id")
        requested_language = (request.query_params.get("language") or "").lower()

        meta, error_response = self._get_and_validate_meta(user, session_id)
        if error_response is not None:
            return error_response

        key = f"livecoding:{session_id}:code"
        data = cache.get(key) or {}
        if not data:
            return Response(
                {"detail": "저장된 코드 스냅샷이 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        history = data.get("history") or []
        latest = data.get("latest") or {}

        snapshot = None
        if requested_language and history:
            for item in reversed(history):
                if (item.get("language") or "").lower() == requested_language:
                    snapshot = item
                    break

        if snapshot is None:
            snapshot = latest

        if not snapshot:
            return Response(
                {"detail": "저장된 코드 스냅샷이 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "session_id": session_id,
                "language": snapshot.get("language") or meta.get("language"),
                "code": snapshot.get("code") or "",
                "saved_at": snapshot.get("saved_at"),
            },
            status=status.HTTP_200_OK,
        )


class LiveCodingHintView(APIView):
    """
    라이브코딩 세션 중 힌트 요청을 LangGraph(챕터2)로 전달합니다.
    - Authorization: Bearer <access_token>
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "힌트 요청을 위해서는 로그인/인증이 필요합니다."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        data = request.data or {}
        session_id = (
            data.get("session_id")
            or request.query_params.get("session_id")
            or request.headers.get("X-Session-Id")
        )
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
                {"detail": "본인 세션에만 접근할 수 있습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        language = (data.get("language") or meta.get("language") or "").lower() or None
        code = data.get("code") or ""
        real_algorithm_category = data.get("problem_algorithm_category") or ""
        problem_description = data.get("problem_description") or ""
        hint_trigger = data.get("hint_trigger") or "manual"
        conversation_log = (
            data.get("conversation_log")
            if isinstance(data.get("conversation_log"), list)
            else None
        )
        hint_count_raw = data.get("hint_count")
        test_cases_payload = data.get("test_cases")
        hint_request_text = (data.get("hint_request_text") or "").strip()

        meta_hint_count_raw = meta.get("hint_count")
        try:
            meta_hint_count = (
                int(meta_hint_count_raw) if meta_hint_count_raw is not None else 0
            )
        except Exception:
            meta_hint_count = 0

        try:
            hint_count = int(hint_count_raw) if hint_count_raw is not None else None
        except Exception:
            hint_count = None

        if hint_count is None:
            if meta_hint_count:
                hint_count = meta_hint_count
            elif conversation_log:
                hint_count = sum(
                    1
                    for entry in conversation_log
                    if isinstance(entry, dict) and entry.get("type") == "hint"
                )
            else:
                hint_count = 0
        else:
            hint_count = max(hint_count, meta_hint_count)

        problem_lang = None
        if meta.get("problem_id"):
            qs = (
                CodingProblemLanguage.objects.select_related("problem")
                .prefetch_related("problem__test_cases")
                .filter(problem__problem_id=meta.get("problem_id"))
            )
            if language:
                qs = qs.filter(language__iexact=language)
            problem_lang = qs.first()

        if problem_lang:
            problem_obj = problem_lang.problem
            if not problem_description:
                problem_description = problem_obj.problem or ""
            if not real_algorithm_category:
                real_algorithm_category = problem_obj.category or ""
            if not test_cases_payload:
                test_cases_payload = [
                    {"input": tc.input_data, "output": tc.output_data}
                    for tc in (
                        problem_obj.test_cases.all()
                        if hasattr(problem_obj, "test_cases")
                        else []
                    )
                ]

        strategy_answer = (meta.get("strategy_answer") or "").strip()

        logger.info(
            "HINT view payload session_id=%s language=%s code_len=%s hint_request_text=%s",
            session_id,
            language,
            len(code or ""),
            hint_request_text,
        )

        state = {
            "meta": {"session_id": session_id, "user_id": str(user.user_id)},
            "current_user_code": code,
            "problem_description": problem_description,
            "real_algorithm_category": real_algorithm_category,
            "hint_trigger": hint_trigger,
            "hint_count": hint_count,
            "user_strategy_answer": strategy_answer,
            "hint_request_text": hint_request_text,
            "stt_text": hint_request_text,
        }
        if conversation_log is not None:
            state["conversation_log"] = conversation_log

        try:
            graph = get_cached_graph(name="chapter2_hint")
            result_state = graph.invoke(
                state,
                config={"configurable": {"thread_id": f"{session_id}:chapter2_hint"}},
            )
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"detail": "langgraph invoke failed", "error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        hint_text = (result_state.get("hint_text") or "").strip()
        new_hint_count = result_state.get("hint_count", hint_count)

        try:
            meta["hint_count"] = int(new_hint_count)
        except Exception:
            meta["hint_count"] = new_hint_count
        cache.set(meta_key, meta, timeout=None)

        return Response(
            {
                "hint_text": hint_text,
                "hint_count": new_hint_count,
                "conversation_log": result_state.get("conversation_log"),
                "hint_trigger": hint_trigger,
            },
            status=status.HTTP_200_OK,
        )


class LiveCodingSessionView(APIView):
    """
    Redis에 저장된 라이브 코딩 세션 정보를 가져오는 엔드포인트.
    - Authorization: Bearer <access_token>
    - query: ?session_id=<sid>
    - 응답: LiveCodingStartView와 동일한 형태의 문제/세션 정보
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "세션 정보를 조회하려면 로그인이 필요합니다."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        session_id = request.query_params.get("session_id")
        if not session_id:
            return Response(
                {"detail": "session_id 쿼리 파라미터가 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        meta_key = f"livecoding:{session_id}:meta"
        problem_key = f"livecoding:{session_id}:problem"

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

        problem = cache.get(problem_key) or {}
        if not problem:
            return Response(
                {"detail": "세션의 문제 정보를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        test_cases = problem.get("test_cases") or []
        if isinstance(test_cases, list) and test_cases:
            normalized = []
            for tc in test_cases:
                if not isinstance(tc, dict):
                    continue
                if "input" in tc or "output" in tc:
                    normalized.append(tc)
                    continue
                if "input_data" in tc or "output_data" in tc:
                    normalized.append(
                        {
                            "id": tc.get("id"),
                            "input": tc.get("input_data") or "",
                            "output": tc.get("output_data") or "",
                        }
                    )
            test_cases = normalized
        else:
            test_cases = []

        if not test_cases:
            try:
                problem_id = problem.get("problem_id") or meta.get("problem_id")
                test_cases = [
                    {"id": tc.id, "input": tc.input_data, "output": tc.output_data}
                    for tc in TestCase.objects.filter(problem_id=problem_id).order_by(
                        "id"
                    )
                ]
                try:
                    problem["test_cases"] = test_cases
                    cache.set(problem_key, problem, timeout=None)
                except Exception:
                    pass
            except Exception:
                test_cases = []

        time_limit_seconds = int(meta.get("time_limit_seconds") or 40 * 60)
        stored_remaining = meta.get("remaining_seconds")
        if stored_remaining is not None:
            try:
                remaining_seconds = max(0, int(stored_remaining))
            except (TypeError, ValueError):
                remaining_seconds = time_limit_seconds
        else:
            start_at_str = meta.get("start_at")
            remaining_seconds = time_limit_seconds
            if start_at_str:
                try:
                    start_at_dt = datetime.fromisoformat(start_at_str)
                    if timezone.is_naive(start_at_dt):
                        start_at_dt = timezone.make_aware(
                            start_at_dt, timezone=timezone.utc
                        )
                    elapsed = max(
                        0, int((timezone.now() - start_at_dt).total_seconds())
                    )
                    remaining_seconds = max(0, time_limit_seconds - elapsed)
                except Exception:
                    remaining_seconds = time_limit_seconds

        return Response(
            {
                "session_id": session_id,
                "stage": meta.get("stage"),
                "user_id": meta.get("user_id"),
                "problem_id": problem.get("problem_id") or meta.get("problem_id"),
                "problem": problem.get("problem"),
                "difficulty": problem.get("difficulty"),
                "category": problem.get("category"),
                "language": problem.get("language") or meta.get("language"),
                "function_name": problem.get("function_name"),
                "starter_code": problem.get("starter_code"),
                "test_cases": test_cases,
                "time_limit_seconds": time_limit_seconds,
                "start_at": meta.get("start_at"),
                "remaining_seconds": remaining_seconds,
                "hint_count": int(meta.get("hint_count") or 0),
            },
            status=status.HTTP_200_OK,
        )


class LiveCodingActiveSessionView(APIView):
    """
    현재 로그인한 사용자의 진행 중인 라이브 코딩 세션을 Redis에서 조회하는 엔드포인트.
    - Authorization: Bearer <access_token>
    - 응답: 세션이 있으면 LiveCodingSessionView와 동일한 구조, 없으면 404
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "세션 정보를 조회하려면 로그인이 필요합니다."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        mapping_key = f"livecoding:user:{user.user_id}:current_session"
        session_id = cache.get(mapping_key)
        if not session_id:
            return Response(
                {"available": False},
                status=status.HTTP_200_OK,
            )

        meta_key = f"livecoding:{session_id}:meta"
        problem_key = f"livecoding:{session_id}:problem"
        meta = cache.get(meta_key)
        problem = cache.get(problem_key)
        if not meta or not problem:
            try:
                cache.delete(mapping_key)
            except Exception:
                pass
            return Response(
                {"available": False},
                status=status.HTTP_200_OK,
            )

        if meta.get("user_id") != str(user.user_id):
            return Response(
                {"detail": "이 세션에 접근할 권한이 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        problem_id = problem.get("problem_id")
        language = problem.get("language")

        qs = (
            CodingProblemLanguage.objects.select_related("problem")
            .prefetch_related("problem__test_cases")
            .filter(problem__problem_id=problem_id)
        )
        if language:
            qs = qs.filter(language__iexact=language)

        problem_lang = qs.first()
        if not problem_lang:
            return Response(
                {"detail": "세션의 문제 정보를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        problem = problem_lang.problem
        test_cases = [
            {"id": tc.id, "input": tc.input_data, "output": tc.output_data}
            for tc in (
                problem.test_cases.all() if hasattr(problem, "test_cases") else []
            )
        ]

        time_limit_seconds = int(meta.get("time_limit_seconds") or 40 * 60)
        stored_remaining = meta.get("remaining_seconds")
        if stored_remaining is not None:
            try:
                remaining_seconds = max(0, int(stored_remaining))
            except (TypeError, ValueError):
                remaining_seconds = time_limit_seconds
        else:
            start_at_str = meta.get("start_at")
            remaining_seconds = time_limit_seconds
            if start_at_str:
                try:
                    start_at_dt = datetime.fromisoformat(start_at_str)
                    if timezone.is_naive(start_at_dt):
                        start_at_dt = timezone.make_aware(
                            start_at_dt, timezone=timezone.utc
                        )
                    elapsed = max(
                        0, int((timezone.now() - start_at_dt).total_seconds())
                    )
                    remaining_seconds = max(0, time_limit_seconds - elapsed)
                except Exception:
                    remaining_seconds = time_limit_seconds

        return Response(
            {
                "session_id": session_id,
                "state": meta.get("state"),
                "user_id": meta.get("user_id"),
                "problem_id": meta.get("problem_id"),
                "problem": problem.problem,
                "difficulty": problem.difficulty,
                "category": problem.category,
                "language": problem_lang.language,
                "function_name": problem_lang.function_name,
                "starter_code": problem_lang.starter_code,
                "test_cases": test_cases,
                "time_limit_seconds": time_limit_seconds,
                "start_at": meta.get("start_at"),
                "remaining_seconds": remaining_seconds,
            },
            status=status.HTTP_200_OK,
        )


class LiveCodingEndSessionView(APIView):
    """
    현재 로그인한 사용자의 진행 중인 라이브 코딩 세션을 종료(삭제)하는 엔드포인트.
    - Authorization: Bearer <access_token>
    - Redis에서 메타/매핑/STT 버퍼를 제거합니다.
    """

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "세션을 종료하려면 로그인이 필요합니다."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        mapping_key = f"livecoding:user:{user.user_id}:current_session"
        session_id = cache.get(mapping_key)
        if not session_id:
            return Response(
                {"detail": "진행 중인 라이브 코딩 세션이 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        meta_key = f"livecoding:{session_id}:meta"
        code_key = f"livecoding:{session_id}:code"
        problem_key = f"livecoding:{session_id}:problem"
        anti_cheat_key = f"livecoding:{session_id}:anti-cheat-events"

        cache.delete(meta_key)
        cache.delete(code_key)
        cache.delete(problem_key)
        cache.delete(anti_cheat_key)
        cache.delete(mapping_key)

        try:
            clear_utterances(str(session_id))
        except Exception:
            pass

        try:
            from .interview_utils import get_checkpointer

            cp = get_checkpointer()
            if cp is not None:
                for chapter in ("chapter1", "chapter2", "chapter2_hint", "chapter3"):
                    thread_id = f"{session_id}:{chapter}"
                    for ns in ("__empty__", "default"):
                        try:
                            if hasattr(cp, "delete_state"):
                                cp.delete_state(thread_id=thread_id, checkpoint_ns=ns)
                        except Exception:
                            continue
        except Exception:
            pass

        return Response(status=status.HTTP_204_NO_CONTENT)


class CodingQuestionView(APIView):
    def post(self, request):
        user = getattr(request, "user", None)
        if not isinstance(user, User):
            return Response(
                {"detail": "질문 생성을 위해서는 로그인이 필요합니다."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        session_id = request.data.get("session_id") or request.query_params.get(
            "session_id"
        )
        if not session_id:
            return Response(
                {"detail": "session_id가 필요합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        meta_key = f"livecoding:{session_id}:meta"
        problem_key = f"livecoding:{session_id}:problem"
        code_key = f"livecoding:{session_id}:code"
        code_data = cache.get(code_key) or {}

        meta = cache.get(meta_key) or {}
        if not meta:
            return Response(
                {"detail": "해당 세션 정보를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if str(meta.get("user_id")) != str(getattr(user, "user_id", None)):
            return Response(
                {"detail": "이 세션에 접근할 권한이 없습니다."},
                status=status.HTTP_403_FORBIDDEN,
            )

        problem = cache.get(problem_key) or {}

        question_cnt = int(code_data.get("question_cnt") or 0)
        if question_cnt >= 3:
            return Response(
                {
                    "skipped": True,
                    "reason": "max_questions_reached",
                    "question": "",
                    "tts_audio": [],
                },
                status=status.HTTP_200_OK,
            )

        history = code_data.get("history") or []
        latest = code_data.get("latest") or {}

        if not latest:
            return Response(
                {
                    "skipped": True,
                    "reason": "no_code_snapshot",
                    "question": "",
                    "tts_audio": [],
                },
                status=status.HTTP_200_OK,
            )

        latest_code = latest.get("code") or ""
        language = (
            (problem.get("language") or meta.get("language") or "python")
            .strip()
            .lower()
        )

        last_question_text = (code_data.get("last_question_text") or "").strip()
        question_history = code_data.get("question_history") or []

        prev_code = ""
        if question_history:
            try:
                prev_code = question_history[-1].get("code") or ""
            except Exception:
                prev_code = ""

        starter_code = (problem.get("starter_code") or "").strip()
        if not starter_code:
            starter_code = (meta.get("starter_code") or "").strip()
        if not starter_code:
            starter_code = (code_data.get("initial_code") or "").strip()
        if not starter_code and history:
            try:
                starter_code = (history[0].get("code") or "").strip()
            except Exception:
                starter_code = ""

        if not starter_code:
            return Response(
                {
                    "skipped": True,
                    "reason": "missing_starter_code",
                    "question": "",
                    "tts_audio": [],
                },
                status=status.HTTP_200_OK,
            )

        graph = get_cached_graph(name="chapter2")
        coding_state = {
            "meta": {"session_id": session_id, "user_id": user.user_id},
            "event_type": "coding_tick",
            "code": latest_code,
            "language": language,
            "question_cnt": question_cnt,
            "starter_code": starter_code,
            "prev_code": prev_code,
            "last_question_text": last_question_text,
        }
        try:
            result = graph.invoke(
                coding_state,
                config={"configurable": {"thread_id": f"{session_id}:chapter2"}},
            )
        except Exception as exc:  # noqa: BLE001
            return Response(
                {"detail": "코딩 질문 그래프 호출에 실패했습니다.", "error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        question_text = (result.get("tts_text") or "").strip()
        if not question_text:
            skip_reason = (
                result.get("question_skip_reason") or ""
            ).strip() or "empty_question"
            return Response(
                {
                    "skipped": True,
                    "reason": skip_reason,
                    "question": "",
                    "tts_audio": [],
                },
                status=status.HTTP_200_OK,
            )

        code_data["question_cnt"] = question_cnt + 1
        code_data["last_question_text"] = question_text
        cache.set(meta_key, meta, timeout=None)

        question_history.append(latest)
        if len(question_history) > 50:
            question_history = question_history[-50:]
        code_data["question_history"] = question_history
        cache.set(code_key, code_data, timeout=None)
        try:
            mapping_key = f"livecoding:user:{user.user_id}:current_session"
            cache.set(mapping_key, session_id, timeout=None)
        except Exception:
            pass

        try:
            tts_chunks = _generate_tts_payload(
                question_text,
                session_id=session_id,
                max_sentences=2,
            )
        except Exception:
            tts_chunks = []

        return Response(
            {
                "skipped": False,
                "reason": None,
                "question": question_text,
                "tts_audio": tts_chunks,
                "state": result,
            },
            status=status.HTTP_200_OK,
        )
