# backend/stt/views.py
import logging
import os
from functools import lru_cache

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from .stt_client import STTClient

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_stt_client() -> STTClient:
    """STT 클라이언트를 요청 시점에 1회만 생성해 재사용합니다."""
    return STTClient(model_size="base")


@csrf_exempt
def transcribe_only(request):
    """
    POST /api/stt/transcribe/
    body: 브라우저에서 보낸 audio/webm 바이트
    → OpenAI Whisper STT만 수행하고 바로 텍스트/segments를 반환합니다.
    """
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    webm_bytes = request.body
    if not webm_bytes:
        return JsonResponse({"error": "No audio data"}, status=400)
    logger.info("webm(len)=%s", len(webm_bytes))

    max_bytes = int(os.getenv("STT_MAX_WEBM_BYTES", "2000000"))  # 약 2MB
    if len(webm_bytes) > max_bytes > 0:
        webm_bytes = webm_bytes[-max_bytes:]
        logger.info("trimmed webm to %s bytes", len(webm_bytes))

    try:
        stt_client = _get_stt_client()
        lines = stt_client.transcribe_pcm_sync(webm_bytes)
        logger.info("stt lines=%s", lines)
        text = " ".join(
            (ln.get("text") or "").strip()
            for ln in (lines or [])
            if isinstance(ln, dict)
        ).strip()

        return JsonResponse(
            {
                "lines": lines,
                "stt_text": text,
            },
            status=200,
        )
    except RuntimeError as exc:
        logger.exception("STT client is unavailable: %s", exc)
        return JsonResponse(
            {"error": "stt_unavailable", "detail": str(exc)},
            status=503,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("STT transcribe_only failed: %s", exc)
        return JsonResponse(
            {"error": "stt_failed", "detail": str(exc)},
            status=500,
        )
