import base64
import hashlib
import hmac
import time

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()

_FAKE_SECRET = "test-hmac-secret-for-unit-tests-only"
_FAKE_URL = "turn:localhost:3478"
_TURN_SETTINGS = dict(
    TURN_AUTH_SECRET=_FAKE_SECRET,
    TURN_SERVER_URL=_FAKE_URL,
    TURN_CREDENTIAL_TTL=600,
)


def _expected_credential(secret: str, username: str) -> str:
    """Independently compute the expected HMAC-SHA1 credential."""
    return base64.b64encode(
        hmac.new(
            secret.encode("utf-8"),
            username.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")


def _jwt_header(user) -> str:
    return f"Bearer {RefreshToken.for_user(user).access_token}"


class TurnCredentialViewTests(APITestCase):
    """
    Tests for GET /api/turn-credentials/

    All tests override Django settings so the real .env secret is never used.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="testcaller@example.com",
            username="testcaller",
            password="irrelevant-password",
        )
        cls.other_user = User.objects.create_user(
            email="othercaller@example.com",
            username="othercaller",
            password="irrelevant-password",
        )

    def _auth(self, user=None):
        self.client.credentials(
            HTTP_AUTHORIZATION=_jwt_header(user or self.user)
        )

    # ------------------------------------------------------------------ #
    # 1. Authenticated request succeeds
    # ------------------------------------------------------------------ #
    @override_settings(**_TURN_SETTINGS)
    def test_authenticated_request_returns_200(self):
        self._auth()
        response = self.client.get("/api/turn-credentials/")
        self.assertEqual(response.status_code, 200)

    # ------------------------------------------------------------------ #
    # 2. Anonymous request is rejected
    # ------------------------------------------------------------------ #
    @override_settings(**_TURN_SETTINGS)
    def test_anonymous_request_rejected(self):
        response = self.client.get("/api/turn-credentials/")
        self.assertIn(response.status_code, [401, 403])

    # ------------------------------------------------------------------ #
    # 3. Username structure: <expiry>:<user_email>
    # ------------------------------------------------------------------ #
    @override_settings(**_TURN_SETTINGS)
    def test_username_has_correct_structure(self):
        self._auth()
        before = int(time.time())
        response = self.client.get("/api/turn-credentials/")
        after = int(time.time())

        username = response.data["username"]
        parts = username.split(":", 1)
        self.assertEqual(len(parts), 2, "username must contain exactly one ':'")

        expiry = int(parts[0])
        user_identifier = parts[1]

        # Expiry should be approximately now + TTL
        self.assertGreaterEqual(expiry, before + 600)
        self.assertLessEqual(expiry, after + 601)
        # Identifier must be the authenticated user's email
        self.assertEqual(user_identifier, self.user.email)

    # ------------------------------------------------------------------ #
    # 4. Credential is correct HMAC-SHA1/Base64
    # ------------------------------------------------------------------ #
    @override_settings(**_TURN_SETTINGS)
    def test_credential_matches_hmac_sha1(self):
        self._auth()
        response = self.client.get("/api/turn-credentials/")

        username = response.data["username"]
        credential = response.data["credential"]

        expected = _expected_credential(_FAKE_SECRET, username)
        # Use assertEqual — hmac.compare_digest is for timing-safe comparisons
        # against attacker-controlled input; here both sides are locally computed.
        self.assertEqual(credential, expected)

    # ------------------------------------------------------------------ #
    # 5. Configurable TTL
    # ------------------------------------------------------------------ #
    @override_settings(**{**_TURN_SETTINGS, "TURN_CREDENTIAL_TTL": 1800})
    def test_configurable_ttl(self):
        self._auth()
        before = int(time.time())
        response = self.client.get("/api/turn-credentials/")

        self.assertEqual(response.data["ttl"], 1800)
        expiry = int(response.data["username"].split(":")[0])
        self.assertGreaterEqual(expiry, before + 1800)

    # ------------------------------------------------------------------ #
    # 6. Client cannot choose another user's identity
    # ------------------------------------------------------------------ #
    @override_settings(**_TURN_SETTINGS)
    def test_client_cannot_inject_identity(self):
        self._auth()
        # Attempt to pass a different email as a query parameter
        response = self.client.get(
            "/api/turn-credentials/",
            {"user": "attacker@example.com", "email": "attacker@example.com"},
        )
        self.assertEqual(response.status_code, 200)
        user_in_username = response.data["username"].split(":", 1)[1]
        self.assertEqual(user_in_username, self.user.email)

    # ------------------------------------------------------------------ #
    # 7. Username is tied to the authenticated account (not another user's)
    # ------------------------------------------------------------------ #
    @override_settings(**_TURN_SETTINGS)
    def test_username_tied_to_authenticated_account(self):
        self._auth(self.user)
        r1 = self.client.get("/api/turn-credentials/")
        id1 = r1.data["username"].split(":", 1)[1]

        self._auth(self.other_user)
        r2 = self.client.get("/api/turn-credentials/")
        id2 = r2.data["username"].split(":", 1)[1]

        self.assertEqual(id1, self.user.email)
        self.assertEqual(id2, self.other_user.email)
        self.assertNotEqual(id1, id2)

    # ------------------------------------------------------------------ #
    # 8. Missing shared secret fails safely (no secret in response)
    # ------------------------------------------------------------------ #
    @override_settings(TURN_AUTH_SECRET="", TURN_SERVER_URL=_FAKE_URL)
    def test_missing_secret_returns_503_without_leaking(self):
        self._auth()
        response = self.client.get("/api/turn-credentials/")
        self.assertEqual(response.status_code, 503)
        response_text = str(response.data)
        self.assertNotIn("secret", response_text.lower())
        self.assertNotIn(_FAKE_SECRET, response_text)

    # ------------------------------------------------------------------ #
    # 9. Shared secret is never present in a successful response
    # ------------------------------------------------------------------ #
    @override_settings(**_TURN_SETTINGS)
    def test_secret_not_in_successful_response(self):
        self._auth()
        response = self.client.get("/api/turn-credentials/")
        response_text = str(response.data)
        self.assertNotIn(_FAKE_SECRET, response_text)
        # Also ensure the key name doesn't appear
        self.assertNotIn("auth_secret", response_text.lower())
