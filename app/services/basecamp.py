"""Basecamp 3 API client — read-only."""

import asyncio
import logging
import time

import httpx
from sqlalchemy.orm import Session

from app.config import settings

logger = logging.getLogger(__name__)

USER_AGENT = settings.BASECAMP_USER_AGENT  # Basecamp requires an app name + contact
MIN_REQUEST_SPACING = 0.2  # 200ms between requests
DEFAULT_RETRY_AFTER = 3.0  # Default 429 backoff seconds
REQUEST_TIMEOUT = 30.0
MAX_PAGES = 100  # Safety limit to prevent infinite pagination loops


class BasecampClient:
    """Sync HTTP client for the Basecamp 3 API (read-only)."""

    def __init__(self, access_token: str, account_id: str):
        self._account_id = account_id
        self._client = httpx.Client(
            base_url=f"https://3.basecampapi.com/{account_id}/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        self._last_request_at: float = 0.0

    @property
    def account_id(self) -> str:
        return self._account_id

    # -- Rate limiting ---------------------------------------------------------

    def _rate_limit(self) -> None:
        """Enforce minimum spacing between requests."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_REQUEST_SPACING:
            time.sleep(MIN_REQUEST_SPACING - elapsed)

    # -- Core request helpers --------------------------------------------------

    def _request(self, method: str, path: str) -> dict | list | None:
        """Rate-limited request. Returns None on 404, retries once on 429."""
        self._rate_limit()
        self._last_request_at = time.monotonic()

        resp = self._client.request(method, path)

        if resp.status_code == 429:
            try:
                retry_after = float(resp.headers.get("Retry-After", str(DEFAULT_RETRY_AFTER)))
            except (ValueError, TypeError):
                retry_after = DEFAULT_RETRY_AFTER
            logger.warning("Basecamp 429 — backing off %.1fs", retry_after)
            time.sleep(retry_after)
            self._last_request_at = time.monotonic()
            resp = self._client.request(method, path)

        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    def _get_paginated(self, path: str) -> list:
        """Follow Link rel=next headers to collect all pages."""
        results: list = []
        url: str | None = path
        page = 0

        while url:
            page += 1
            if page > MAX_PAGES:
                logger.warning("Pagination limit reached (%d pages) for %s", MAX_PAGES, path)
                break

            self._rate_limit()
            self._last_request_at = time.monotonic()

            resp = self._client.get(url)

            if resp.status_code == 429:
                try:
                    retry_after = float(resp.headers.get("Retry-After", str(DEFAULT_RETRY_AFTER)))
                except (ValueError, TypeError):
                    retry_after = DEFAULT_RETRY_AFTER
                logger.warning("Basecamp 429 — backing off %.1fs", retry_after)
                time.sleep(retry_after)
                self._last_request_at = time.monotonic()
                resp = self._client.get(url)

            if resp.status_code == 404:
                return results  # endpoint doesn't exist for this resource
            resp.raise_for_status()
            results.extend(resp.json())

            # Parse Link header for next page
            url = None
            link_header = resp.headers.get("Link", "")
            if 'rel="next"' in link_header:
                for part in link_header.split(","):
                    if 'rel="next"' in part:
                        url = part.split(";")[0].strip().strip("<>")
                        break

        return results

    # -- API methods -----------------------------------------------------------

    def list_projects(self) -> list[dict]:
        return self._get_paginated("projects.json")

    def get_project(self, project_id: int | str) -> dict | None:
        return self._request("GET", f"projects/{project_id}.json")

    def get_todoset_id(self, project_id: int | str) -> int | None:
        """Extract the todoset dock entry ID from a project's detail."""
        project = self.get_project(project_id)
        if not project:
            return None
        for dock in project.get("dock", []):
            if dock.get("name") == "todoset" and dock.get("enabled"):
                return dock["id"]
        return None

    def get_todolists(self, project_id: int | str, todoset_id: int | str) -> list[dict]:
        return self._get_paginated(
            f"buckets/{project_id}/todosets/{todoset_id}/todolists.json"
        )

    def get_todos(self, project_id: int | str, todolist_id: int | str) -> list[dict]:
        return self._get_paginated(
            f"buckets/{project_id}/todolists/{todolist_id}/todos.json"
        )

    def get_todolist_groups(self, project_id: int | str, todolist_id: int | str) -> list[dict]:
        return self._get_paginated(
            f"buckets/{project_id}/todolists/{todolist_id}/groups.json"
        )

    def get_todo(self, bucket_id: int | str, todo_id: int | str) -> dict | None:
        return self._request("GET", f"buckets/{bucket_id}/todos/{todo_id}.json")

    def get_comments(self, bucket_id: int | str, todo_id: int | str) -> list[dict]:
        return self._get_paginated(
            f"buckets/{bucket_id}/recordings/{todo_id}/comments.json"
        )

    def close(self) -> None:
        self._client.close()


def get_basecamp_client(db: Session) -> BasecampClient | None:
    """Build a BasecampClient from stored config, or None if not connected."""
    from app.models import Config
    from app.routes.auth import get_basecamp_token

    token = get_basecamp_token(db)
    if not token:
        return None

    acct_row = db.get(Config, "basecamp_account_id")
    if not acct_row or not acct_row.value:
        return None

    return BasecampClient(access_token=token, account_id=str(acct_row.value))


# ---------------------------------------------------------------------------
# Async client — used by the background poller
# ---------------------------------------------------------------------------


class AsyncBasecampClient:
    """Async HTTP client for the Basecamp 3 API (read-only). Used by the poller."""

    def __init__(self, access_token: str, account_id: str):
        self._account_id = account_id
        self._client = httpx.AsyncClient(
            base_url=f"https://3.basecampapi.com/{account_id}/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
            },
            timeout=REQUEST_TIMEOUT,
        )
        self._last_request_at: float = 0.0
        self._rate_lock = asyncio.Lock()

    @property
    def account_id(self) -> str:
        return self._account_id

    # -- Rate limiting ---------------------------------------------------------

    async def _rate_limit(self) -> None:
        """Enforce minimum spacing. Must be called inside self._rate_lock."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < MIN_REQUEST_SPACING:
            await asyncio.sleep(MIN_REQUEST_SPACING - elapsed)
        self._last_request_at = time.monotonic()

    # -- Core request helpers --------------------------------------------------

    async def _request(self, method: str, path: str) -> dict | list | None:
        """Rate-limited request. Returns None on 404, retries once on 429."""
        async with self._rate_lock:
            await self._rate_limit()
        resp = await self._client.request(method, path)

        if resp.status_code == 429:
            try:
                retry_after = float(resp.headers.get("Retry-After", str(DEFAULT_RETRY_AFTER)))
            except (ValueError, TypeError):
                retry_after = DEFAULT_RETRY_AFTER
            logger.warning("Basecamp 429 — backing off %.1fs", retry_after)
            await asyncio.sleep(retry_after)
            async with self._rate_lock:
                self._last_request_at = time.monotonic()
            resp = await self._client.request(method, path)

        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    async def _get_paginated(self, path: str) -> list:
        """Follow Link rel=next headers to collect all pages."""
        results: list = []
        url: str | None = path
        page = 0

        while url:
            page += 1
            if page > MAX_PAGES:
                logger.warning("Pagination limit reached (%d pages) for %s", MAX_PAGES, path)
                break

            async with self._rate_lock:
                await self._rate_limit()
            resp = await self._client.get(url)

            if resp.status_code == 429:
                try:
                    retry_after = float(resp.headers.get("Retry-After", str(DEFAULT_RETRY_AFTER)))
                except (ValueError, TypeError):
                    retry_after = DEFAULT_RETRY_AFTER
                logger.warning("Basecamp 429 — backing off %.1fs", retry_after)
                await asyncio.sleep(retry_after)
                async with self._rate_lock:
                    self._last_request_at = time.monotonic()
                resp = await self._client.get(url)

            if resp.status_code == 404:
                return results  # endpoint doesn't exist for this resource
            resp.raise_for_status()
            results.extend(resp.json())

            # Parse Link header for next page
            url = None
            link_header = resp.headers.get("Link", "")
            if 'rel="next"' in link_header:
                for part in link_header.split(","):
                    if 'rel="next"' in part:
                        url = part.split(";")[0].strip().strip("<>")
                        break

        return results

    # -- API methods -----------------------------------------------------------

    async def list_projects(self) -> list[dict]:
        return await self._get_paginated("projects.json")

    async def get_project(self, project_id: int | str) -> dict | None:
        return await self._request("GET", f"projects/{project_id}.json")

    async def get_todoset_id(self, project_id: int | str) -> int | None:
        """Extract the todoset dock entry ID from a project's detail."""
        project = await self.get_project(project_id)
        if not project:
            return None
        for dock in project.get("dock", []):
            if dock.get("name") == "todoset" and dock.get("enabled"):
                return dock["id"]
        return None

    async def get_todolists(self, project_id: int | str, todoset_id: int | str) -> list[dict]:
        return await self._get_paginated(
            f"buckets/{project_id}/todosets/{todoset_id}/todolists.json"
        )

    async def get_todos(self, project_id: int | str, todolist_id: int | str) -> list[dict]:
        return await self._get_paginated(
            f"buckets/{project_id}/todolists/{todolist_id}/todos.json"
        )

    async def get_todolist_groups(self, project_id: int | str, todolist_id: int | str) -> list[dict]:
        return await self._get_paginated(
            f"buckets/{project_id}/todolists/{todolist_id}/groups.json"
        )

    async def get_todo(self, bucket_id: int | str, todo_id: int | str) -> dict | None:
        return await self._request("GET", f"buckets/{bucket_id}/todos/{todo_id}.json")

    async def get_comments(self, bucket_id: int | str, todo_id: int | str) -> list[dict]:
        return await self._get_paginated(
            f"buckets/{bucket_id}/recordings/{todo_id}/comments.json"
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()


def get_async_basecamp_client(db: Session) -> AsyncBasecampClient | None:
    """Build an AsyncBasecampClient from stored config, or None if not connected."""
    from app.models import Config
    from app.routes.auth import get_basecamp_token

    token = get_basecamp_token(db)
    if not token:
        return None

    acct_row = db.get(Config, "basecamp_account_id")
    if not acct_row or not acct_row.value:
        return None

    return AsyncBasecampClient(access_token=token, account_id=str(acct_row.value))
