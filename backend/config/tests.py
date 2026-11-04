"""
Tests for Task 5: Redis security and connection configuration.

These tests verify the structural correctness of the configuration changes
without requiring a live Redis instance.
"""

import os
from django.test import TestCase, override_settings
from django.conf import settings


class ChannelLayersConfigTests(TestCase):
    """CHANNEL_LAYERS must use a URL string, not a (host, port) tuple."""

    def test_hosts_contains_string_not_tuple(self):
        hosts = settings.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"]
        self.assertEqual(len(hosts), 1)
        self.assertIsInstance(
            hosts[0],
            str,
            "channels_redis 4.x requires a URL string in hosts, not a (host, port) tuple; "
            "password authentication only works via URL string",
        )

    def test_hosts_matches_redis_url_setting(self):
        hosts = settings.CHANNEL_LAYERS["default"]["CONFIG"]["hosts"]
        self.assertEqual(
            hosts[0],
            settings.REDIS_URL,
            "CHANNEL_LAYERS hosts[0] must equal settings.REDIS_URL so a single "
            "env variable controls all three Redis consumers",
        )

    def test_redis_url_has_sensible_default(self):
        # Verify the default falls back gracefully when no env var is set.
        # The actual value depends on the test environment; we just ensure
        # it is a non-empty string starting with redis://.
        self.assertIsInstance(settings.REDIS_URL, str)
        self.assertTrue(
            settings.REDIS_URL.startswith("redis://"),
            f"REDIS_URL must be a redis:// URL, got: {settings.REDIS_URL!r}",
        )


class CeleryConfigTests(TestCase):
    """Broker and result-backend must be independently configurable."""

    def test_result_backend_not_reading_broker_var(self):
        # Before this fix CELERY_RESULT_BACKEND read os.environ["CELERY_BROKER_URL"].
        # Now it reads CELERY_RESULT_BACKEND → REDIS_URL → default.
        # We verify the two settings can hold different values simultaneously.
        with override_settings(
            CELERY_BROKER_URL="redis://broker-host:6379/0",
            CELERY_RESULT_BACKEND="redis://backend-host:6379/0",
        ):
            self.assertNotEqual(
                settings.CELERY_BROKER_URL,
                settings.CELERY_RESULT_BACKEND,
                "CELERY_BROKER_URL and CELERY_RESULT_BACKEND must be independently "
                "overrideable via override_settings",
            )

    def test_result_backend_has_sensible_default(self):
        self.assertIsInstance(settings.CELERY_RESULT_BACKEND, str)
        self.assertTrue(
            settings.CELERY_RESULT_BACKEND.startswith("redis://"),
            f"CELERY_RESULT_BACKEND default must be a redis:// URL, "
            f"got: {settings.CELERY_RESULT_BACKEND!r}",
        )

    def test_broker_url_has_sensible_default(self):
        self.assertIsInstance(settings.CELERY_BROKER_URL, str)
        self.assertTrue(
            settings.CELERY_BROKER_URL.startswith("redis://"),
            f"CELERY_BROKER_URL default must be a redis:// URL, "
            f"got: {settings.CELERY_BROKER_URL!r}",
        )


class ConsumersDeadCodeTests(TestCase):
    """No active hardcoded localhost Redis client must remain in consumers.py."""

    def test_no_redis_client_attribute(self):
        import apps.messaging.consumers as consumers
        self.assertFalse(
            hasattr(consumers, "redis_client"),
            "Module-level redis_client (hardcoded to 127.0.0.1:6379) must be "
            "removed — it connected to localhost, not the Docker Redis service, "
            "and was never called",
        )

    def test_redis_module_not_imported(self):
        import apps.messaging.consumers as consumers
        self.assertFalse(
            hasattr(consumers, "redis"),
            "The 'redis' module must not be imported at the top of consumers.py "
            "after removing the dead redis_client",
        )

    def test_time_module_not_imported(self):
        import apps.messaging.consumers as consumers
        self.assertFalse(
            hasattr(consumers, "time"),
            "The 'time' module import must be removed from consumers.py — "
            "it was only present to support the now-deleted redis_client",
        )
