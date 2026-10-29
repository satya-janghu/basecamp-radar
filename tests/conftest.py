"""Test configuration: provide dummy settings so app modules import without a live database."""

import os

os.environ.setdefault("DATABASE_URL", "postgresql://user:password@localhost:5432/radar_test")
os.environ.setdefault("PASSWORD", "test-password")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("BASECAMP_CLIENT_ID", "test-client-id")
os.environ.setdefault("BASECAMP_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GEMINI_API_KEY", "")
