from unittest.mock import patch

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()

FAKE_MESSAGES = ["Hello", "Hi there"]


def _jwt_header(user) -> str:
    return f"Bearer {RefreshToken.for_user(user).access_token}"


class AIEndpointAuthTests(APITestCase):
    """
    Verify that all AI endpoints require authentication.
    Anonymous requests must receive 401; authenticated requests must pass
    the permission check and reach the view logic.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="aitest@example.com",
            username="aitest",
            password="irrelevant-password",
        )

    def _auth(self):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_header(self.user))

    def _clear_auth(self):
        self.client.credentials()

    # ── suggest-reply ──────────────────────────────────────────────────────

    def test_suggest_reply_anonymous_returns_401(self):
        self._clear_auth()
        res = self.client.post("/api/ai/suggest-reply/", {"messages": FAKE_MESSAGES}, format="json")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("apps.ai.views.generate_reply", return_value="Sure!")
    def test_suggest_reply_authenticated_passes(self, mock_fn):
        self._auth()
        res = self.client.post("/api/ai/suggest-reply/", {"messages": FAKE_MESSAGES}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("suggestion", res.data)
        mock_fn.assert_called_once()

    # ── summary ────────────────────────────────────────────────────────────

    def test_summary_anonymous_returns_401(self):
        self._clear_auth()
        res = self.client.post("/api/ai/summary/", {"messages": FAKE_MESSAGES}, format="json")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("apps.ai.views.summarize_chat", return_value="A brief chat.")
    def test_summary_authenticated_passes(self, mock_fn):
        self._auth()
        res = self.client.post("/api/ai/summary/", {"messages": FAKE_MESSAGES}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("summary", res.data)
        mock_fn.assert_called_once()

    # ── translate ──────────────────────────────────────────────────────────

    def test_translate_anonymous_returns_401(self):
        self._clear_auth()
        res = self.client.post(
            "/api/ai/translate/",
            {"message": "Hola", "target_language": "English"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("apps.ai.views.translate_message", return_value="Hello")
    def test_translate_authenticated_passes(self, mock_fn):
        self._auth()
        res = self.client.post(
            "/api/ai/translate/",
            {"message": "Hola", "target_language": "English"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("translated_text", res.data)
        mock_fn.assert_called_once()

    # ── grammar-fix ────────────────────────────────────────────────────────

    def test_grammar_fix_anonymous_returns_401(self):
        self._clear_auth()
        res = self.client.post("/api/ai/grammar-fix/", {"message": "I goes there."}, format="json")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("apps.ai.views.grammar_fix", return_value="I go there.")
    def test_grammar_fix_authenticated_passes(self, mock_fn):
        self._auth()
        res = self.client.post("/api/ai/grammar-fix/", {"message": "I goes there."}, format="json")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("corrected_text", res.data)
        mock_fn.assert_called_once()

    # ── rewrite ────────────────────────────────────────────────────────────

    def test_rewrite_anonymous_returns_401(self):
        self._clear_auth()
        res = self.client.post(
            "/api/ai/rewrite/",
            {"message": "hey", "tone": "formal"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("apps.ai.views.rewrite_message", return_value="Good day.")
    def test_rewrite_authenticated_passes(self, mock_fn):
        self._auth()
        res = self.client.post(
            "/api/ai/rewrite/",
            {"message": "hey", "tone": "formal"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("rewritten_text", res.data)
        mock_fn.assert_called_once()

    # ── search ─────────────────────────────────────────────────────────────

    def test_search_anonymous_returns_401(self):
        self._clear_auth()
        res = self.client.post(
            "/api/ai/search/",
            {"messages": FAKE_MESSAGES, "question": "What did we say?"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("apps.ai.views.search_chat", return_value="You said hello.")
    def test_search_authenticated_passes(self, mock_fn):
        self._auth()
        res = self.client.post(
            "/api/ai/search/",
            {"messages": FAKE_MESSAGES, "question": "What did we say?"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn("answer", res.data)
        mock_fn.assert_called_once()
