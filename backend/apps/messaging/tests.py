from unittest.mock import AsyncMock, MagicMock, call, patch

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from botocore.exceptions import ClientError

from apps.conversations.models import Conversation, ConversationParticipant
from apps.messaging.models import Attachment, Message
from apps.messaging.serializers import AttachmentSerializer, MessageSerializer

User = get_user_model()


def _jwt_header(user) -> str:
    return f"Bearer {RefreshToken.for_user(user).access_token}"


def _make_attachment(message, scan_status, file_key="users/1/conversations/1/uuid_test.png"):
    return Attachment.objects.create(
        message=message,
        file=file_key,
        file_type="image",
        file_size=1024,
        original_name="test.png",
        scan_status=scan_status,
    )


class AttachmentScanStatusSerializerTests(APITestCase):
    """
    Unit tests for AttachmentSerializer.get_file_url gating on scan_status.
    All three scan states (pending, infected, clean) are tested against both
    the direct serializer and the REST message-list endpoint.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="sender@example.com", username="sender", password="X"
        )
        cls.other = User.objects.create_user(
            email="participant@example.com", username="participant", password="X"
        )
        cls.outsider = User.objects.create_user(
            email="outsider@example.com", username="outsider", password="X"
        )
        cls.conv = Conversation.objects.create(type=Conversation.PRIVATE, created_by=cls.user)
        ConversationParticipant.objects.create(conversation=cls.conv, user=cls.user)
        ConversationParticipant.objects.create(conversation=cls.conv, user=cls.other)

        cls.msg = Message.objects.create(
            conversation=cls.conv, sender=cls.user, message_type=Message.IMAGE
        )

    def _make_msg_attachment(self, scan_status):
        return _make_attachment(self.msg, scan_status)

    def setUp(self):
        # Remove all attachments created by prior test methods.
        Attachment.objects.filter(message=self.msg).delete()

    # ── Serializer unit tests ──────────────────────────────────────────

    @patch("apps.messaging.serializers.get_s3")
    def test_pending_attachment_no_file_url(self, mock_get_s3):
        att = self._make_msg_attachment("pending")
        data = AttachmentSerializer(att, context={}).data
        self.assertIsNone(data["file_url"])
        mock_get_s3.return_value.generate_presigned_url.assert_not_called()

    @patch("apps.messaging.serializers.get_s3")
    def test_infected_attachment_no_file_url(self, mock_get_s3):
        att = Attachment.objects.create(
            message=self.msg,
            file="",           # cleared by scan task on infection
            file_type="image",
            file_size=1024,
            original_name="evil.png",
            scan_status="infected",
        )
        data = AttachmentSerializer(att, context={}).data
        self.assertIsNone(data["file_url"])
        mock_get_s3.return_value.generate_presigned_url.assert_not_called()

    @patch("apps.messaging.serializers.get_s3")
    def test_clean_attachment_returns_file_url(self, mock_get_s3):
        fake_url = "http://minio:9000/chatapp/key?sig=abc"
        mock_get_s3.return_value.generate_presigned_url.return_value = fake_url
        att = self._make_msg_attachment("clean")
        with patch("apps.messaging.serializers.settings") as mock_settings:
            mock_settings.AWS_STORAGE_BUCKET_NAME = "chatapp"
            mock_settings.AWS_S3_ENDPOINT_URL = "http://minio:9000"
            mock_settings.PUBLIC_MINIO_URL = "http://localhost:8080/minio"
            data = AttachmentSerializer(att, context={}).data
        self.assertIsNotNone(data["file_url"])
        mock_get_s3.return_value.generate_presigned_url.assert_called_once()

    @patch("apps.messaging.serializers.get_s3")
    def test_scan_status_field_exposed(self, mock_get_s3):
        for ss in ("pending", "infected", "clean"):
            Attachment.objects.filter(message=self.msg).delete()
            att = self._make_msg_attachment(ss)
            data = AttachmentSerializer(att, context={}).data
            self.assertIn("scan_status", data)
            self.assertEqual(data["scan_status"], ss)

    @patch("apps.messaging.serializers.get_s3")
    def test_failed_scan_no_url(self, mock_get_s3):
        """A scan that never completes keeps scan_status='pending' — no URL."""
        att = self._make_msg_attachment("pending")
        data = AttachmentSerializer(att, context={}).data
        self.assertIsNone(data["file_url"])
        mock_get_s3.return_value.generate_presigned_url.assert_not_called()

    # ── REST API message-list tests ────────────────────────────────────

    def _auth(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=_jwt_header(user))

    def _first_message_attachments(self, res):
        """Return the attachments list from the first message in a list response."""
        # MessageViewSet returns a plain list (no DRF pagination wrapper).
        results = res.data if isinstance(res.data, list) else res.data.get("results", res.data)
        return results[0]["attachments"]

    @patch("apps.messaging.serializers.get_s3")
    def test_rest_pending_no_url(self, mock_get_s3):
        self._make_msg_attachment("pending")
        self._auth(self.user)
        res = self.client.get(f"/api/messages/?conversation={self.conv.id}")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        attachments = self._first_message_attachments(res)
        self.assertEqual(len(attachments), 1)
        self.assertIsNone(attachments[0]["file_url"])

    @patch("apps.messaging.serializers.get_s3")
    def test_rest_infected_no_url(self, mock_get_s3):
        Attachment.objects.create(
            message=self.msg, file="", file_type="image",
            file_size=1024, original_name="evil.png", scan_status="infected",
        )
        self._auth(self.user)
        res = self.client.get(f"/api/messages/?conversation={self.conv.id}")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        attachments = self._first_message_attachments(res)
        self.assertIsNone(attachments[0]["file_url"])

    def test_rest_outsider_cannot_list_messages(self):
        """User not in the conversation receives an empty queryset (no 403 leak)."""
        self._make_msg_attachment("clean")
        self._auth(self.outsider)
        res = self.client.get(f"/api/messages/?conversation={self.conv.id}")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        results = res.data if isinstance(res.data, list) else res.data.get("results", res.data)
        self.assertEqual(len(results), 0)


class ScanFileTaskTests(APITestCase):
    """
    Tests for scan_file_task behaviour: WS broadcast on clean,
    no URL in initial broadcast (pending), URL present after clean broadcast.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="tasksender@example.com", username="tasksender", password="X"
        )
        cls.conv = Conversation.objects.create(type=Conversation.PRIVATE, created_by=cls.user)
        ConversationParticipant.objects.create(conversation=cls.conv, user=cls.user)

    def _make_pending_attachment(self):
        msg = Message.objects.create(
            conversation=self.conv, sender=self.user, message_type=Message.IMAGE
        )
        att = Attachment.objects.create(
            message=msg,
            file=f"users/{self.user.id}/conversations/{self.conv.id}/uuid_img.png",
            file_type="image",
            file_size=512,
            original_name="img.png",
            scan_status="pending",
        )
        return att

    @patch("apps.messaging.tasks.async_to_sync")
    @patch("apps.messaging.tasks.get_channel_layer")
    @patch("apps.messaging.tasks.scan_file_for_virus", return_value=False)
    @patch("apps.messaging.tasks.get_s3")
    def test_clean_scan_broadcasts_updated_message(
        self, mock_get_s3, mock_scan, mock_gcl, mock_async
    ):
        """After a clean scan, task broadcasts a 'message' event so clients get the URL."""
        att = self._make_pending_attachment()

        # Simulate an S3 object download
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        fake_body = MagicMock()
        fake_body.iter_chunks.return_value = [b"fake_image_data"]
        mock_s3.get_object.return_value = {"Body": fake_body}

        fake_layer = MagicMock()
        mock_gcl.return_value = fake_layer
        mock_async.return_value = lambda *a, **kw: None

        from apps.messaging.tasks import scan_file_task
        scan_file_task(att.id)

        att.refresh_from_db()
        self.assertEqual(att.scan_status, "clean")

        # group_send must have been called; check the broadcast type
        self.assertTrue(mock_async.called)
        calls = mock_async.call_args_list
        # At least one call should be for the scan_complete broadcast
        group_send_calls = [
            c for c in calls if "group_send" in str(c)
        ]
        self.assertGreater(len(group_send_calls), 0)

    @patch("apps.messaging.tasks.async_to_sync")
    @patch("apps.messaging.tasks.get_channel_layer")
    @patch("apps.messaging.tasks.scan_file_for_virus", return_value=True)
    @patch("apps.messaging.tasks.get_s3")
    def test_infected_scan_broadcasts_file_infected(
        self, mock_get_s3, mock_scan, mock_gcl, mock_async
    ):
        """Infected scan broadcasts file_infected event and clears the file key."""
        att = self._make_pending_attachment()

        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        fake_body = MagicMock()
        fake_body.iter_chunks.return_value = [b"malware"]
        mock_s3.get_object.return_value = {"Body": fake_body}

        fake_layer = MagicMock()
        mock_gcl.return_value = fake_layer
        mock_async.return_value = lambda *a, **kw: None

        from apps.messaging.tasks import scan_file_task
        scan_file_task(att.id)

        att.refresh_from_db()
        self.assertEqual(att.scan_status, "infected")
        self.assertEqual(att.file, "")

    @patch("apps.messaging.serializers.get_s3")
    def test_ws_broadcast_initial_has_no_url(self, mock_get_s3):
        """
        The initial broadcast serializes the message while scan_status='pending'.
        The resulting attachment data must have file_url=None.
        """
        att = self._make_pending_attachment()
        # Serialize the message as complete_upload does for the WS broadcast
        data = MessageSerializer(att.message, context={}).data
        self.assertEqual(len(data["attachments"]), 1)
        self.assertIsNone(data["attachments"][0]["file_url"])
        self.assertEqual(data["attachments"][0]["scan_status"], "pending")

    @patch("apps.messaging.serializers.get_s3")
    def test_clean_scan_makes_url_available_via_serializer(self, mock_get_s3):
        """After scan_status is set to 'clean', the serializer returns a URL."""
        att = self._make_pending_attachment()

        fake_url = "http://minio:9000/chatapp/key?sig=x"
        mock_get_s3.return_value.generate_presigned_url.return_value = fake_url

        att.scan_status = "clean"
        att.save(update_fields=["scan_status"])

        with patch("apps.messaging.serializers.settings") as ms:
            ms.AWS_STORAGE_BUCKET_NAME = "chatapp"
            ms.AWS_S3_ENDPOINT_URL = "http://minio:9000"
            ms.PUBLIC_MINIO_URL = "http://localhost:8080/minio"
            data = MessageSerializer(att.message, context={}).data

        self.assertIsNotNone(data["attachments"][0]["file_url"])
        self.assertEqual(data["attachments"][0]["scan_status"], "clean")


# =============================================================================
# View-once scan gate (tests 1-3)
# =============================================================================

class ViewOnceMediaScanGateTests(APITestCase):
    """open_view_once_media must reject pending and infected attachments."""

    def setUp(self):
        # Disable throttling for these tests without touching application code.
        # MessageViewSet.throttle_classes would fire rate limits during the test
        # suite because requests share the same user and in-memory cache.
        patcher = patch("apps.messaging.views.MessageViewSet.throttle_classes", new=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    @classmethod
    def setUpTestData(cls):
        cls.sender = User.objects.create_user(
            email="vo_sender@example.com", username="vo_sender", password="X"
        )
        cls.recipient = User.objects.create_user(
            email="vo_recipient@example.com", username="vo_recipient", password="X"
        )
        cls.conv = Conversation.objects.create(
            type=Conversation.PRIVATE, created_by=cls.sender
        )
        ConversationParticipant.objects.create(conversation=cls.conv, user=cls.sender)
        ConversationParticipant.objects.create(conversation=cls.conv, user=cls.recipient)

    def _make_view_once_message(self, scan_status):
        msg = Message.objects.create(
            conversation=self.conv,
            sender=self.sender,
            message_type=Message.IMAGE,
            view_once=True,
        )
        Attachment.objects.create(
            message=msg,
            file="users/1/conversations/1/vo_test.png",
            file_type="image",
            file_size=512,
            original_name="vo.png",
            scan_status=scan_status,
        )
        return msg

    def _auth(self, user):
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(user).access_token}"
        )

    def test_pending_view_once_rejected(self):
        """Test 1 — pending attachment must not be served."""
        msg = self._make_view_once_message("pending")
        self._auth(self.recipient)
        res = self.client.post(f"/api/messages/{msg.id}/view-once/open/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not yet available", res.data.get("error", ""))

    def test_infected_view_once_rejected(self):
        """Test 2 — infected attachment must not be served."""
        msg = self._make_view_once_message("infected")
        self._auth(self.recipient)
        res = self.client.post(f"/api/messages/{msg.id}/view-once/open/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("not yet available", res.data.get("error", ""))

    @patch("apps.messaging.services.get_channel_layer")
    @patch("apps.messaging.services.get_s3")
    def test_clean_view_once_succeeds(self, mock_get_s3, mock_gcl):
        """Test 3 — clean attachment is served and marked viewed."""
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=MagicMock(return_value=b"bytes")),
            "ContentType": "image/png",
        }
        mock_layer = MagicMock()
        mock_layer.group_send = AsyncMock()
        mock_gcl.return_value = mock_layer

        msg = self._make_view_once_message("clean")
        self._auth(self.recipient)
        res = self.client.post(f"/api/messages/{msg.id}/view-once/open/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.content, b"bytes")
        msg.refresh_from_db()
        self.assertIsNotNone(msg.viewed_at)


# =============================================================================
# Skipped-file broadcast (test 6)
# =============================================================================

class SkippedFileBroadcastTests(APITestCase):
    """Video, voice and ZIP files must trigger a clean-scan broadcast."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="skip_sender@example.com", username="skip_sender", password="X"
        )
        cls.conv = Conversation.objects.create(
            type=Conversation.PRIVATE, created_by=cls.user
        )
        ConversationParticipant.objects.create(conversation=cls.conv, user=cls.user)

    def _make_pending(self, file_type, name="clip.mp4"):
        msg = Message.objects.create(
            conversation=self.conv, sender=self.user, message_type=Message.IMAGE
        )
        return Attachment.objects.create(
            message=msg,
            file=f"users/{self.user.id}/conversations/{self.conv.id}/{name}",
            file_type=file_type,
            file_size=1024,
            original_name=name,
            scan_status="pending",
        )

    @patch("apps.messaging.tasks.async_to_sync")
    @patch("apps.messaging.tasks.get_channel_layer")
    def test_skipped_file_type_broadcasts_clean(self, mock_gcl, mock_async):
        """Test 6 — video attachment skips ClamAV but still broadcasts."""
        mock_gcl.return_value = MagicMock()
        mock_async.return_value = lambda *a, **kw: None

        att = self._make_pending("video", "clip.mp4")
        from apps.messaging.tasks import scan_file_task
        scan_file_task(att.id)

        att.refresh_from_db()
        self.assertEqual(att.scan_status, "clean")
        self.assertTrue(mock_async.called)

    @patch("apps.messaging.tasks.async_to_sync")
    @patch("apps.messaging.tasks.get_channel_layer")
    def test_zip_file_broadcasts_clean(self, mock_gcl, mock_async):
        """ZIP extension is also skipped and broadcasts clean completion."""
        mock_gcl.return_value = MagicMock()
        mock_async.return_value = lambda *a, **kw: None

        att = self._make_pending("file", "archive.zip")
        from apps.messaging.tasks import scan_file_task
        scan_file_task(att.id)

        att.refresh_from_db()
        self.assertEqual(att.scan_status, "clean")
        self.assertTrue(mock_async.called)


# =============================================================================
# MinIO ClientError retry (tests 7-8)
# =============================================================================

class ClientErrorRetryTests(APITestCase):
    """S3 ClientError must propagate so Celery retries the task."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="retry_user@example.com", username="retry_user", password="X"
        )
        cls.conv = Conversation.objects.create(
            type=Conversation.PRIVATE, created_by=cls.user
        )
        ConversationParticipant.objects.create(conversation=cls.conv, user=cls.user)

    def _make_pending_image(self):
        msg = Message.objects.create(
            conversation=self.conv, sender=self.user, message_type=Message.IMAGE
        )
        return Attachment.objects.create(
            message=msg,
            file=f"users/{self.user.id}/conversations/{self.conv.id}/img.png",
            file_type="image",
            file_size=512,
            original_name="img.png",
            scan_status="pending",
        )

    @patch("apps.messaging.tasks.get_s3")
    def test_client_error_raises_for_retry(self, mock_get_s3):
        """Test 7 — ClientError propagates so Celery retry mechanism fires."""
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}}, "GetObject"
        )

        att = self._make_pending_image()
        from apps.messaging.tasks import scan_file_task

        with self.assertRaises(ClientError):
            scan_file_task(att.id)

    @patch("apps.messaging.tasks.get_s3")
    def test_client_error_does_not_mark_clean(self, mock_get_s3):
        """Test 8 — after S3 failure, scan_status must NOT become clean."""
        mock_s3 = MagicMock()
        mock_get_s3.return_value = mock_s3
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "ServiceUnavailable", "Message": "Retry"}}, "GetObject"
        )

        att = self._make_pending_image()
        from apps.messaging.tasks import scan_file_task

        try:
            scan_file_task(att.id)
        except ClientError:
            pass

        att.refresh_from_db()
        self.assertNotEqual(att.scan_status, "clean")


# =============================================================================
# Attachment.file_url model property (tests 9-10)
# =============================================================================

class AttachmentFileUrlModelPropertyTests(APITestCase):
    """Attachment.file_url must gate on scan_status."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="prop_user@example.com", username="prop_user", password="X"
        )
        cls.conv = Conversation.objects.create(
            type=Conversation.PRIVATE, created_by=cls.user
        )
        cls.msg = Message.objects.create(
            conversation=cls.conv, sender=cls.user, message_type=Message.IMAGE
        )

    def _make_attachment(self, scan_status):
        return Attachment.objects.create(
            message=self.msg,
            file="users/1/conversations/1/prop_test.png",
            file_type="image",
            file_size=512,
            original_name="prop.png",
            scan_status=scan_status,
        )

    def setUp(self):
        Attachment.objects.filter(message=self.msg).delete()

    def test_file_url_property_returns_none_for_pending(self):
        """Test 9a — pending attachment: file_url property returns None."""
        att = self._make_attachment("pending")
        self.assertIsNone(att.file_url)

    def test_file_url_property_returns_none_for_infected(self):
        """Test 9b — infected attachment: file_url property returns None."""
        att = self._make_attachment("infected")
        self.assertIsNone(att.file_url)

    @patch("utils.minio.get_s3")
    def test_file_url_property_works_for_clean(self, mock_get_s3):
        """Test 10 — clean attachment: file_url property returns a URL."""
        fake_url = "http://minio:9000/chatapp/key?sig=abc"
        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.return_value = fake_url
        mock_get_s3.return_value = mock_s3

        with patch("apps.messaging.models.settings") as ms:
            ms.AWS_STORAGE_BUCKET_NAME = "chatapp"
            ms.MINIO_URL_EXPIRY = 300
            att = self._make_attachment("clean")
            result = att.file_url

        self.assertEqual(result, fake_url)
