import base64
import hashlib
import hmac
import logging
import time

from django.conf import settings
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework import status

from .throttles import TurnCredentialThrottle

logger = logging.getLogger(__name__)

_CREDENTIAL_TTL_DEFAULT = 600  # seconds


class TurnCredentialView(APIView):
    """
    GET /api/turn-credentials/

    Returns temporary Coturn REST API credentials for the authenticated user.

    Credentials are generated using the TURN REST API mechanism:
      username = "<expiry_unix_timestamp>:<user_email>"
      password = Base64( HMAC-SHA1( TURN_AUTH_SECRET, username ) )

    Coturn verifies the HMAC using its own copy of TURN_AUTH_SECRET.
    The shared secret is never returned to the client.

    Credentials expire after TURN_CREDENTIAL_TTL seconds (default 600).
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [TurnCredentialThrottle]

    def get(self, request):
        auth_secret = getattr(settings, "TURN_AUTH_SECRET", None) or ""
        turn_url = getattr(settings, "TURN_SERVER_URL", None) or ""

        if not auth_secret:
            logger.error(
                "TURN_AUTH_SECRET is not configured — /api/turn-credentials/ unavailable"
            )
            return Response(
                {"error": "TURN service is not available"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        if not turn_url:
            logger.error(
                "TURN_SERVER_URL is not configured — /api/turn-credentials/ unavailable"
            )
            return Response(
                {"error": "TURN service is not available"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        ttl = int(getattr(settings, "TURN_CREDENTIAL_TTL", _CREDENTIAL_TTL_DEFAULT))

        expiry = int(time.time()) + ttl
        # Username ties the credential to the authenticated account.
        # The user identifier comes from the server — never from the request.
        username = f"{expiry}:{request.user.email}"

        # HMAC-SHA1 of the username, keyed by the shared secret.
        # SHA1 is required by the Coturn REST API specification (RFC 8489 §9.2).
        credential = base64.b64encode(
            hmac.new(
                auth_secret.encode("utf-8"),
                username.encode("utf-8"),
                hashlib.sha1,
            ).digest()
        ).decode("utf-8")

        return Response(
            {
                "urls": [turn_url],
                "username": username,
                "credential": credential,
                "ttl": ttl,
            }
        )
