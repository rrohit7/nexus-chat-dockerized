from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import transaction
import os

User = get_user_model()


class Command(BaseCommand):
    help = "Create or promote superuser from DJANGO_SUPERUSER_* environment variables"

    def handle(self, *args, **kwargs):
        email = os.getenv("DJANGO_SUPERUSER_EMAIL")
        username = os.getenv("DJANGO_SUPERUSER_USERNAME")
        password = os.getenv("DJANGO_SUPERUSER_PASSWORD")

        if not email or not username or not password:
            self.stdout.write(self.style.ERROR("Missing env variables"))
            return

        with transaction.atomic():
            try:
                user = User.objects.get(email=email)
            except User.DoesNotExist:
                User.objects.create_superuser(
                    email=email,
                    username=username,
                    password=password,
                )
                self.stdout.write(self.style.SUCCESS("✅ Superuser created"))
                return

            if user.is_staff and user.is_superuser:
                self.stdout.write("ℹ️ Superuser already exists")
                return

            user.is_staff = True
            user.is_superuser = True
            user.save(update_fields=["is_staff", "is_superuser"])
            self.stdout.write(self.style.SUCCESS("✅ Existing user promoted to superuser"))
