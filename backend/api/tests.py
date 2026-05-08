from unittest.mock import Mock, patch

import jwt
from django.test import SimpleTestCase, override_settings
from django.urls import resolve
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.test import APIRequestFactory, force_authenticate

from .authentication import JWTAuthentication
from .chap1_views import LiveCodingPreloadView
from .report_views import (
    LiveCodingFinalEvalStartView,
    LiveCodingReportListView,
    save_strategy_answer,
)
from .profile_views import ProfileView
from .session_views import (
    CodingQuestionView,
    LiveCodingActiveSessionView,
    LiveCodingCodeSnapshotView,
    LiveCodingEndSessionView,
    LiveCodingHintView,
    LiveCodingSessionView,
    LiveCodingStartView,
)
from .auth_views import SignupView
from .models import User


class RoutingTests(SimpleTestCase):
    def test_livecoding_preload_route_resolves(self):
        match = resolve("/api/livecoding/preload/")
        self.assertEqual(match.url_name, "livecoding-preload")
        self.assertIs(match.func.view_class, LiveCodingPreloadView)

    def test_split_views_route_to_new_modules(self):
        routes = [
            ("/api/auth/signup/", "signup", SignupView),
            ("/api/user/profile/", "profile", ProfileView),
            ("/api/livecoding/start/", "livecoding-start", LiveCodingStartView),
            ("/api/livecoding/session/", "livecoding-session", LiveCodingSessionView),
            (
                "/api/livecoding/session/active/",
                "livecoding-session-active",
                LiveCodingActiveSessionView,
            ),
            (
                "/api/livecoding/session/end/",
                "livecoding-session-end",
                LiveCodingEndSessionView,
            ),
            (
                "/api/livecoding/session/code/",
                "livecoding-session-code",
                LiveCodingCodeSnapshotView,
            ),
            (
                "/api/livecoding/session/hint/",
                "livecoding-session-hint",
                LiveCodingHintView,
            ),
            (
                "/api/livecoding/session/question/",
                "livecoding-session-question",
                CodingQuestionView,
            ),
            (
                "/api/livecoding/final-eval/start/",
                None,
                LiveCodingFinalEvalStartView,
            ),
            ("/api/livecoding/reports/", "livecoding-report-list", LiveCodingReportListView),
        ]
        for path, expected_name, expected_view in routes:
            with self.subTest(path=path):
                match = resolve(path)
                if expected_name is not None:
                    self.assertEqual(match.url_name, expected_name)
                self.assertIs(match.func.view_class, expected_view)

        match = resolve("/api/livecoding/session/strategy/")
        self.assertEqual(match.url_name, "save-strategy")
        self.assertIs(match.func, save_strategy_answer)


class LiveCodingPreloadViewTests(SimpleTestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User(user_id="user-1", email="user@example.com", name="User One")

    def test_returns_seed_required_when_no_problem_exists(self):
        request = self.factory.post("/api/livecoding/preload/", {"language": "python"}, format="json")
        force_authenticate(request, user=self.user)

        manager = Mock()
        manager.select_related.return_value.prefetch_related.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with patch("api.chap1_views.CodingProblemLanguage.objects", manager), patch(
            "api.chap1_views.LiveCodingPreloadView.check_throttles", return_value=None
        ):
            response = LiveCodingPreloadView.as_view()(request)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["code"], "seed_required")
        self.assertEqual(response.data["language"], "python")


class JWTAuthenticationTests(SimpleTestCase):
    @override_settings(SECRET_KEY="test-secret")
    def test_authenticates_bearer_token(self):
        factory = APIRequestFactory()
        request = factory.get("/api/auth/me/", HTTP_AUTHORIZATION="Bearer token-123")

        user = User(user_id="user-1", email="user@example.com", name="User One")

        with patch("api.authentication.jwt.decode", return_value={"sub": "user-1"}), patch(
            "api.authentication.User.objects.get", return_value=user
        ) as get_user:
            authenticated = JWTAuthentication().authenticate(request)

        self.assertEqual(authenticated[0], user)
        self.assertEqual(authenticated[1]["sub"], "user-1")
        get_user.assert_called_once_with(user_id="user-1")

    @override_settings(SECRET_KEY="test-secret")
    def test_rejects_invalid_token(self):
        factory = APIRequestFactory()
        request = factory.get("/api/auth/me/", HTTP_AUTHORIZATION="Bearer invalid-token")

        with patch(
            "api.authentication.jwt.decode",
            side_effect=jwt.PyJWTError("bad token"),
        ):
            with self.assertRaises(AuthenticationFailed):
                JWTAuthentication().authenticate(request)
