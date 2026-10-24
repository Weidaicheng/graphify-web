"""GitHub API client — check repo existence and latest commit."""

import os
import httpx


def _get_proxy_kwargs() -> dict:
    """Build httpx client kwargs with explicit proxy to avoid NO_PROXY IPv6 bug."""
    proxy_url = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
    )
    kwargs = {"timeout": 15.0, "trust_env": False}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    return kwargs


class GitHubAPI:
    BASE = "https://api.github.com"

    @classmethod
    async def repo_exists(cls, owner: str, name: str) -> bool:
        """Check if a GitHub repository exists (public)."""
        httpx_mod = httpx
        async with httpx_mod.AsyncClient(**_get_proxy_kwargs()) as client:
            resp = await client.get(f"{cls.BASE}/repos/{owner}/{name}")
            return resp.status_code == 200

    @classmethod
    async def get_latest_commit(cls, owner: str, name: str) -> str | None:
        """Return the SHA of the latest commit on the default branch, or None."""
        httpx_mod = httpx
        async with httpx_mod.AsyncClient(**_get_proxy_kwargs()) as client:
            resp = await client.get(
                f"{cls.BASE}/repos/{owner}/{name}/commits",
                params={"per_page": 1},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data:
                return None
            return data[0]["sha"]
