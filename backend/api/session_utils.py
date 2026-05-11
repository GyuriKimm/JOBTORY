from __future__ import annotations

from typing import Optional, Tuple

from django.core.cache import cache
from rest_framework import status
from rest_framework.response import Response


def get_livecoding_meta_key(session_id: str) -> str:
    return f"livecoding:{session_id}:meta"


def get_livecoding_problem_key(session_id: str) -> str:
    return f"livecoding:{session_id}:problem"


def get_livecoding_code_key(session_id: str) -> str:
    return f"livecoding:{session_id}:code"


def get_livecoding_current_session_key(user_id: str) -> str:
    return f"livecoding:user:{user_id}:current_session"


def load_livecoding_meta(session_id: str) -> dict:
    if not session_id:
        return {}
    return cache.get(get_livecoding_meta_key(session_id)) or {}


def require_livecoding_owner(user, session_id: str) -> Tuple[Optional[dict], Optional[Response]]:
    if not session_id:
        return None, Response(
            {"detail": "session_id가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST
        )

    meta = load_livecoding_meta(session_id)
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
