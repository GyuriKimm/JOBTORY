from datetime import datetime

from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from api.models import ProfileOption, User, UserProfile

from .authentication import JWTAuthentication
from .utils import _format_birthdate

class ProfileView(APIView):
    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user

        return Response(
            {
                "user_id": user.user_id,
                "email": user.email,
                "name": user.name,
                "phone_number": user.phone_number,
                "birthdate": _format_birthdate(user.birthdate),
            },
            status=status.HTTP_200_OK,
        )

    def patch(self, request):
        user = request.user
        data = request.data

        name = data.get("name")
        phone_number = data.get("phone_number")
        birthdate = data.get("birthdate")
        current_password = data.get("current_password")
        new_password = data.get("new_password")

        if not name:
            return Response({"detail": "이름은 필수 항목입니다."}, status=400)

        if phone_number:
            existing = (
                User.objects.filter(phone_number=phone_number)
                .exclude(user_id=user.user_id)
                .first()
            )
            if existing:
                return Response(
                    {"detail": "이미 사용 중인 전화번호입니다."}, status=400
                )

        if current_password and new_password:
            if not user.password_hash:
                return Response(
                    {"detail": "소셜 로그인 계정은 비밀번호를 변경할 수 없습니다."},
                    status=400,
                )

            from django.contrib.auth.hashers import check_password, make_password

            if not check_password(current_password, user.password_hash):
                return Response(
                    {"detail": "현재 비밀번호가 올바르지 않습니다."}, status=400
                )

            if len(new_password) < 8:
                return Response(
                    {"detail": "새 비밀번호는 8자 이상이어야 합니다."}, status=400
                )

            user.password_hash = make_password(new_password)

        elif current_password or new_password:
            return Response(
                {"detail": "현재 비밀번호와 새 비밀번호를 모두 입력해주세요."},
                status=400,
            )

        if birthdate:
            try:
                parsed = datetime.strptime(birthdate, "%Y-%m-%d").date()
                user.birthdate = parsed
            except ValueError:
                user.birthdate = birthdate
        else:
            user.birthdate = None

        user.name = name
        user.phone_number = phone_number if phone_number else None
        user.updated_at = timezone.now()

        user.save()

        return Response(
            {
                "message": "회원정보가 성공적으로 수정되었습니다.",
                "user_id": user.user_id,
                "email": user.email,
                "name": user.name,
                "phone_number": user.phone_number,
                "birthdate": _format_birthdate(user.birthdate),
            },
            status=status.HTTP_200_OK,
        )


class UserProfileDetailView(APIView):
    """user_profile 테이블을 조회/업데이트하는 뷰."""

    authentication_classes = [JWTAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def _serialize(self, profile):
        return {
            "user_id": profile.user.user_id,
            "graduated_school": profile.graduated_school,
            "university": profile.university,
            "major": profile.major,
            "academic_status": profile.academic_status,
            "graduation_year": profile.graduation_year,
            "career_level": profile.career_level,
            "current_status": profile.current_status,
            "tech_stack": profile.tech_stack or [],
            "desired_role": profile.desired_role or [],
            "detailed_role": profile.detailed_role or [],
            "region": profile.region or [],
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        }

    def get(self, request):
        user = request.user
        profile, created = UserProfile.objects.get_or_create(
            user=user,
            defaults={"created_at": timezone.now(), "updated_at": timezone.now()},
        )
        return Response(self._serialize(profile), status=status.HTTP_200_OK)

    def post(self, request):
        return self.patch(request)

    def patch(self, request):
        user = request.user
        payload = request.data or {}
        profile, _ = UserProfile.objects.get_or_create(
            user=user,
            defaults={"created_at": timezone.now()},
        )

        fields = [
            "graduated_school",
            "university",
            "major",
            "academic_status",
            "graduation_year",
            "career_level",
            "current_status",
            "tech_stack",
            "desired_role",
            "detailed_role",
            "region",
        ]
        for f in fields:
            if f not in payload:
                continue
            val = payload.get(f)
            if val == "":
                val = None
            if f == "graduation_year" and val is not None:
                try:
                    val = int(val)
                except ValueError:
                    val = None
            if f in ("tech_stack", "desired_role", "detailed_role", "region"):
                if val is None or val == "":
                    val = []
            setattr(profile, f, val)
        profile.updated_at = timezone.now()
        profile.save(update_fields=fields + ["updated_at"])
        return Response(self._serialize(profile), status=status.HTTP_200_OK)


class UserProfileOptionsView(APIView):
    """프로필 입력에 사용되는 선택지 목록을 제공하는 뷰."""

    permission_classes = [AllowAny]

    def get(self, request):
        rows = (
            ProfileOption.objects.all()
            .values_list("category", "value")
            .order_by("category", "display_order", "id")
        )

        grouped = {}
        for cat, val in rows:
            grouped.setdefault(cat, []).append(val)

        categories = [
            "graduated_school",
            "major",
            "academic_status",
            "career_level",
            "current_status",
            "tech_stack",
            "desired_role",
            "detailed_role",
            "region",
        ]
        for c in categories:
            grouped.setdefault(c, [])

        return Response(grouped, status=status.HTTP_200_OK)
