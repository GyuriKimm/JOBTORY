import json
import logging
import secrets
import string
from datetime import timedelta

import jwt
from django.conf import settings
from django.core.mail import send_mail
from django.contrib.auth.hashers import check_password, make_password
from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import AuthIdentity, User
from tts_client import generate_interview_audio

from .email_utils import send_verification_code, verify_code
from .google_oauth import GoogleOAuthError, exchange_code_for_tokens, fetch_userinfo
from .jwt_utils import create_access_token
from .serializers import SignupSerializer
from .throttling import (
    EmailActionRateThrottle,
    LoginRateThrottle,
    PasswordResetRateThrottle,
)
from .interview_utils import _generate_tts_payload

logger = logging.getLogger(__name__)


class TTSView(APIView):
    """
    텍스트를 받아 TTS만 수행하는 엔드포인트.
    - POST /api/tts/intro/?session_id=...
    - POST /api/tts/intro/?session_id=...
      body: {"tts_text": "..."} 또는 {"text": "..."}
      response: CodingProblemSessionInitView와 동일한 오디오 청크 구조
    """

    def post(self, request):
        data = request.data or {}
        text = (data.get("tts_text") or data.get("text") or "").strip()
        max_sentences = data.get("max_sentences")

        if not text:
            return Response(
                {"detail": "tts_text(또는 text) 필드를 전달해 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        session_id = (
            request.query_params.get("session_id")
            or request.headers.get("X-Session-Id")
            or (request.data.get("session_id") if hasattr(request, "data") else None)
        )

        try:
            sentences_payload = _generate_tts_payload(text, session_id, max_sentences)
        except Exception as exc:  # noqa: BLE001
            logger.exception("TTS payload generation failed")
            return Response(
                {
                    "detail": "TTS 함수를 실행할 수 없습니다.",
                    "error": str(exc),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response({"tts_text": sentences_payload}, status=status.HTTP_200_OK)


class TTSStreamView(APIView):
    """
    텍스트를 받아 문장 단위로 TTS를 생성해 스트리밍으로 반환합니다.

    - POST /api/tts/intro/stream/?session_id=...
      body: {"tts_text": "..."} 또는 {"text": "..."}
      response: NDJSON (각 줄이 1개 청크 JSON)
        {"text":"...","audio":"<base64>","sentence_number":1,"is_first":true,"generation_time":0.0}
    """

    def post(self, request):
        data = request.data or {}
        text = data.get("tts_text") or data.get("text") or ""
        if not isinstance(text, str):
            return Response(
                {"detail": "tts_text(또는 text) 필드는 문자열이어야 합니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        text = text.strip()
        max_sentences = data.get("max_sentences")

        if not text:
            return Response(
                {"detail": "tts_text(또는 text) 필드를 전달해 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        session_id = (
            request.query_params.get("session_id")
            or request.headers.get("X-Session-Id")
            or (request.data.get("session_id") if hasattr(request, "data") else None)
        )

        config = {"configurable": {}}
        if session_id:
            config["configurable"]["thread_id"] = session_id
        if isinstance(max_sentences, int) and max_sentences > 0:
            config["configurable"]["max_sentences"] = max_sentences
        if not config["configurable"]:
            config = None

        def iter_ndjson():
            try:
                for chunk in generate_interview_audio(text, config=config):
                    payload = {
                        "text": chunk.get("text", ""),
                        "audio": chunk.get("audio_base64"),
                        "sentence_number": chunk.get("sentence_number"),
                        "is_first": chunk.get("is_first"),
                        "generation_time": chunk.get("generation_time"),
                    }
                    if chunk.get("error"):
                        payload["error"] = chunk.get("error")
                    yield (json.dumps(payload, ensure_ascii=False) + "\n").encode(
                        "utf-8"
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("TTS stream generation failed")
                payload = {"error": str(exc)}
                yield (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

        resp = StreamingHttpResponse(
            iter_ndjson(), content_type="application/x-ndjson; charset=utf-8"
        )
        resp["Cache-Control"] = "no-cache"
        resp["X-Accel-Buffering"] = "no"
        return resp


class SignupView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        return Response(
            {
                "user_id": user.user_id,
                "email": user.email,
                "name": user.name,
                "phone_number": user.phone_number,
                "birthdate": user.birthdate,
            },
            status=status.HTTP_201_CREATED,
        )


class UserIdCheckView(APIView):
    """
    아이디 중복 여부를 확인하는 엔드포인트.
    GET /api/auth/user-id/check/?user_id=some_id
    """

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        user_id = request.query_params.get("user_id")
        if not user_id:
            return Response(
                {"detail": "user_id를 쿼리스트링으로 전달해 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        exists = User.objects.filter(user_id=user_id).exists()
        return Response({"user_id": user_id, "available": not exists})


class LoginView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        identifier = request.data.get("user_id") or request.data.get("email")
        password = request.data.get("password")

        if not identifier or not password:
            return Response(
                {"detail": "아이디(또는 이메일)와 비밀번호를 입력해 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.filter(user_id=identifier).first()
        if not user:
            user = User.objects.filter(email=identifier).first()

        if (
            not user
            or not user.password_hash
            or not check_password(password, user.password_hash)
        ):
            return Response(
                {"detail": "아이디 또는 비밀번호가 올바르지 않습니다."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        access_token = create_access_token(user)

        return Response(
            {
                "user_id": user.user_id,
                "email": user.email,
                "name": user.name,
                "access_token": access_token,
                "token_type": "bearer",
            },
            status=status.HTTP_200_OK,
        )


class UserMeView(APIView):
    """단순 JWT 검증 후 사용자 프로필 반환."""

    def get(self, request):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.lower().startswith("bearer "):
            return Response(
                {"detail": "인증 정보가 필요합니다."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        token = auth_header.split(" ", 1)[1].strip()
        try:
            payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        except jwt.ExpiredSignatureError:
            return Response(
                {"detail": "토큰이 만료되었습니다."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        except jwt.InvalidTokenError:
            return Response(
                {"detail": "유효하지 않은 토큰입니다."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        user_id = payload.get("sub")
        if not user_id:
            return Response(
                {"detail": "유효하지 않은 토큰입니다."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        user = User.objects.filter(user_id=user_id).first()
        if not user:
            return Response(
                {"detail": "사용자를 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            {
                "user_id": user.user_id,
                "email": user.email,
                "name": user.name,
                "phone_number": user.phone_number,
                "birthdate": user.birthdate,
            },
            status=status.HTTP_200_OK,
        )


class LogoutView(APIView):
    """클라이언트 측 토큰 제거용 엔드포인트 (서버 상태 없음)."""

    def post(self, request):
        return Response({"detail": "logged_out"}, status=status.HTTP_200_OK)


class EmailSendView(APIView):
    permission_classes = [permissions.AllowAny]
    throttle_classes = [EmailActionRateThrottle]

    def post(self, request):
        email = request.data.get("email")
        if not email:
            return Response(
                {"detail": "이메일을 입력해 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        code, expires_at = send_verification_code(email)
        return Response({"email": email, "expires_at": expires_at})


class EmailVerifyView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        email = request.data.get("email")
        code = request.data.get("code")
        if not email or not code:
            return Response(
                {"detail": "이메일과 인증코드를 입력해 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ok, msg = verify_code(email, code)
        if not ok:
            return Response({"detail": msg}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"email": email, "verified": True, "message": msg})


class FindIdView(APIView):
    """이메일을 기준으로 user_id를 찾는 엔드포인트."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [EmailActionRateThrottle]

    def post(self, request):
        email = request.data.get("email")
        if not email:
            return Response(
                {"detail": "이메일을 입력해 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.filter(email=email).first()
        if not user:
            return Response(
                {"detail": "해당 이메일로 가입된 아이디가 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if AuthIdentity.objects.filter(user=user, provider="google").exists():
            return Response(
                {
                    "detail": "구글 소셜 로그인으로 가입한 계정은 아이디 찾기 기능을 사용할 수 없습니다. 구글 로그인을 이용해 주세요."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response({"email": email, "user_id": user.user_id})


class FindPasswordView(APIView):
    """이름, 아이디, 이메일을 기준으로 사용자를 찾고 임시 비밀번호를 이메일로 발송합니다."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [PasswordResetRateThrottle]

    def post(self, request):
        name = request.data.get("name")
        user_id = request.data.get("user_id")
        email = request.data.get("email")

        if not name or not user_id or not email:
            return Response(
                {"detail": "이름, 아이디, 이메일을 모두 입력해 주세요."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.filter(user_id=user_id, email=email, name=name).first()
        if not user:
            return Response(
                {"detail": "입력하신 정보와 일치하는 계정을 찾을 수 없습니다."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if AuthIdentity.objects.filter(user=user, provider="google").exists():
            return Response(
                {
                    "detail": "구글 소셜 로그인으로 가입한 계정은 비밀번호 찾기 기능을 사용할 수 없습니다. 구글 로그인을 이용해 주세요."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        alphabet = string.ascii_letters + string.digits
        temp_password = "".join(secrets.choice(alphabet) for _ in range(10))

        user.password_hash = make_password(temp_password)
        user.updated_at = timezone.now()
        user.save(update_fields=["password_hash", "updated_at"])

        subject = "[JobTory] 임시 비밀번호 안내"
        message = (
            f"{user.name}님, 안녕하세요.\n\n"
            f"요청하신 임시 비밀번호는 아래와 같습니다.\n\n"
            f"임시 비밀번호: {temp_password}\n\n"
            "로그인 후 마이페이지에서 비밀번호를 꼭 변경해 주세요.\n"
        )
        try:
            send_mail(
                subject,
                message,
                settings.DEFAULT_FROM_EMAIL,
                [user.email],
                fail_silently=False,
            )
        except Exception:
            logger.exception("Temporary password email send failed")
            return Response(
                {"detail": "임시 비밀번호 이메일 발송 중 오류가 발생했습니다."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {"detail": "임시 비밀번호를 이메일로 발송했습니다. 메일을 확인해 주세요."},
            status=status.HTTP_200_OK,
        )


class GoogleAuthView(APIView):
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        code = request.data.get("code")
        if not code:
            return Response(
                {"detail": "code가 필요합니다."}, status=status.HTTP_400_BAD_REQUEST
            )

        redirect_uri = settings.GOOGLE_REDIRECT_URI
        try:
            token_data = exchange_code_for_tokens(code, redirect_uri)
            userinfo = fetch_userinfo(token_data["access_token"])
        except GoogleOAuthError as exc:
            logger.warning("Google OAuth failed: %s", exc)
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        sub = userinfo["sub"]
        email = userinfo.get("email")
        name = userinfo.get("name") or userinfo.get("given_name")

        expires_in = token_data.get("expires_in")
        refresh_token = token_data.get("refresh_token")

        auth_identity = (
            AuthIdentity.objects.filter(provider="google", provider_user_id=sub)
            .select_related("user")
            .first()
        )
        if auth_identity:
            user = auth_identity.user
            fields_to_update = []
            if refresh_token:
                auth_identity.refresh_token = refresh_token
                fields_to_update.append("refresh_token")
            if expires_in:
                auth_identity.token_expires_at = timezone.now() + timedelta(
                    seconds=int(expires_in)
                )
                fields_to_update.append("token_expires_at")
            if fields_to_update:
                auth_identity.save(update_fields=fields_to_update)
        else:
            user = User.objects.filter(email=email).first()
            if not user:
                user_pk = email or sub
                user = User.objects.create(
                    user_id=user_pk,
                    email=email,
                    name=name,
                    password_hash=None,
                    phone_number=None,
                    birthdate=None,
                    created_at=timezone.now(),
                    updated_at=timezone.now(),
                )
            expires_at = (
                timezone.now() + timedelta(seconds=int(expires_in))
                if expires_in is not None
                else None
            )
            AuthIdentity.objects.create(
                user=user,
                provider="google",
                provider_user_id=sub,
                refresh_token=refresh_token,
                token_expires_at=expires_at,
                created_at=timezone.now(),
            )

        access_token = create_access_token(user)

        return Response(
            {
                "user_id": user.user_id,
                "email": user.email,
                "name": user.name,
                "provider": "google",
                "access_token": access_token,
                "token_type": "bearer",
            }
        )
