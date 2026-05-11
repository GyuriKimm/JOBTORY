import logging
import time

from django.core.cache import cache
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)

ANTI_CHEAT_DEBOUNCE_SECONDS = 5.0
ANTI_CHEAT_REQUIRED_STREAK = 2
ANTI_CHEAT_COUNT_COOLDOWN_SECONDS = 15.0
ANTI_CHEAT_NEUTRAL_REASONS = {"face_not_detected", "pose_calc_failed"}


def _load_anti_cheat_state(session_id: str) -> dict:
    state = cache.get(f"livecoding:{session_id}:anti-cheat") or {}
    return state if isinstance(state, dict) else {}


def _save_anti_cheat_state(session_id: str, state: dict) -> None:
    cache.set(f"livecoding:{session_id}:anti-cheat", state, timeout=60 * 60)


def _should_count_anti_cheat_event(state: dict, detail_reason: str, now: float) -> bool:
    last_seen_at = float(state.get("last_seen_at") or 0.0)
    last_counted_at = float(state.get("last_counted_at") or 0.0)
    last_reason = str(state.get("last_reason") or "")
    streak = int(state.get("streak") or 0)

    if detail_reason and detail_reason == last_reason and now - last_seen_at <= ANTI_CHEAT_DEBOUNCE_SECONDS:
        streak += 1
    else:
        streak = 1

    state["last_seen_at"] = now
    state["last_reason"] = detail_reason
    state["streak"] = streak

    if streak < ANTI_CHEAT_REQUIRED_STREAK:
        return False

    if now - last_counted_at < ANTI_CHEAT_COUNT_COOLDOWN_SECONDS:
        return False

    state["last_counted_at"] = now
    return True


class CheatAnalysisView(APIView):
    """
    mediapipe를 사용해 단일 이미지 프레임에서 부정행위 여부를 분석하는 엔드포인트.
    - 입력: multipart/form-data, field name: "image"
    - 출력: { is_cheating, reason}
    """

    permission_classes = [permissions.AllowAny]
    # 이 엔드포인트는 5초마다 호출되므로 전역 DRF throttling에서 제외합니다.
    throttle_classes: list = []
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request):
        from .analyzer import analyze_frame

        session_id = request.query_params.get("session_id")
        file = request.FILES.get("image")
        if not file:
            return Response(
                {"detail": "image 파일을 multipart/form-data로 전송해 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            result = analyze_frame(file.read())
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response(
                {"detail": "이미지 분석 중 오류가 발생했습니다."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        data = result.to_dict()

        # 세션 ID가 있으면 부정행위 이벤트를 Redis(캐시)에 기록합니다.
        if session_id:
            event_key = f"livecoding:{session_id}:anti-cheat-events"
            cache_data = _load_anti_cheat_state(session_id)
            detail_reason = str(data.get("detail_reason") or "")
            now = time.time()

            if data.get("is_cheating"):
                if _should_count_anti_cheat_event(cache_data, detail_reason, now):
                    cheat_count = int(cache_data.get("cheat_count", 0)) + 1
                    reasons = cache_data.get("reasons", [])
                    if not isinstance(reasons, list):
                        reasons = []
                    reasons.append(
                        {
                            "ts": now,
                            "cheat_reason": detail_reason or data.get("reason"),
                            "streak": int(cache_data.get("streak") or 0),
                        }
                    )
                    cache_data["cheat_count"] = cheat_count
                    cache_data["reasons"] = reasons
                    _save_anti_cheat_state(session_id, cache_data)

                    event_payload = cache.get(event_key) or {}
                    camera = event_payload.get("camera") or {}
                    camera["mediapipe"] = int(camera.get("mediapipe", 0)) + 1
                    event_payload["camera"] = camera
                    cache.set(event_key, event_payload, timeout=60 * 60)
                else:
                    logger.debug(
                        "anti-cheat event debounced session_id=%s detail_reason=%s streak=%s",
                        session_id,
                        detail_reason,
                        cache_data.get("streak"),
                    )
                    _save_anti_cheat_state(session_id, cache_data)
            else:
                if detail_reason in ANTI_CHEAT_NEUTRAL_REASONS:
                    cache_data["streak"] = 0
                    cache_data["last_reason"] = detail_reason
                    cache_data["last_seen_at"] = now
                    _save_anti_cheat_state(session_id, cache_data)

        return Response(data, status=status.HTTP_200_OK)

class FacePresenceView(APIView):
    """
    설정 페이지용 경량 얼굴 존재 확인 엔드포인트.
    - 입력: multipart/form-data, field name: "image"
    - 출력: { face_count, face_visible: bool }
    """

    permission_classes = [permissions.AllowAny]
    throttle_classes: list = []
    parser_classes = (MultiPartParser, FormParser)

    def post(self, request):
        from .analyzer import _face_count_from_bytes

        file = request.FILES.get("image")
        if not file:
            return Response(
                {"detail": "image 파일을 multipart/form-data로 전송해 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            count = _face_count_from_bytes(file.read())
        except ValueError as e:
            return Response({"detail": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response(
                {"detail": "얼굴 감지 중 오류가 발생했습니다."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {"face_count": count, "face_visible": count > 0},
            status=status.HTTP_200_OK,
        )
