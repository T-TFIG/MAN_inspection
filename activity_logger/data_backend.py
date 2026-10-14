"""Thin wrapper around the GitHub Contents API, used as our "database".

Both the employee app (with the baked-in client token) and the Admin Console
(with the admin's own token) talk to the same private data repo through this
module: read/write a JSON file, upload a binary file, list a "directory".

This is deliberately simple — no caching, no batching — because call volume is
low (one heartbeat every few minutes per machine, one admin dashboard refresh
every minute or so). If the deployment ever grows past roughly a hundred
machines, this is the piece to replace with a small real backend; nothing
above this module needs to change except where it points.
"""

import base64
import time

import requests

from . import config


class ConflictError(Exception):
    """The file changed between our read and our write (someone else won the race)."""


def _api_base(owner: str, repo: str) -> str:
    return f"https://api.github.com/repos/{owner}/{repo}"


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def get_file(token: str, path: str, *, owner: str = config.DATA_REPO_OWNER,
            repo: str = config.DATA_REPO_NAME) -> tuple[bytes | None, str | None]:
    """Return (raw_bytes, sha); (None, None) if the file does not exist."""
    resp = requests.get(f"{_api_base(owner, repo)}/contents/{path}",
                        params={"ref": "main"}, headers=_headers(token), timeout=20)
    if resp.status_code == 404:
        return None, None
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        raise ValueError(f"{path!r} is a directory, not a file")
    raw = base64.b64decode(data["content"])
    return raw, data.get("sha")


def get_json(token: str, path: str, *, owner: str = config.DATA_REPO_OWNER,
            repo: str = config.DATA_REPO_NAME) -> tuple[dict | None, str | None]:
    raw, sha = get_file(token, path, owner=owner, repo=repo)
    if raw is None:
        return None, None
    import json
    try:
        return json.loads(raw.decode("utf-8")), sha
    except Exception as exc:
        raise ValueError(f"{path!r} is not valid JSON: {exc}") from exc


def put_bytes(token: str, path: str, content: bytes, message: str,
             sha: str | None = None, *, owner: str = config.DATA_REPO_OWNER,
             repo: str = config.DATA_REPO_NAME) -> str:
    """Create/update a file. Raises ConflictError on a stale ``sha``."""
    body = {
        "message": message,
        "content": base64.b64encode(content).decode("ascii"),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    resp = requests.put(f"{_api_base(owner, repo)}/contents/{path}",
                        headers=_headers(token), json=body, timeout=30)
    if resp.status_code == 409:
        raise ConflictError(f"{path!r} changed since it was last read")
    resp.raise_for_status()
    return resp.json()["content"]["sha"]


def put_json(token: str, path: str, data: dict, message: str,
            sha: str | None = None, *, owner: str = config.DATA_REPO_OWNER,
            repo: str = config.DATA_REPO_NAME) -> str:
    import json
    text = json.dumps(data, indent=2) + "\n"
    return put_bytes(token, path, text.encode("utf-8"), message, sha,
                     owner=owner, repo=repo)


def put_json_retrying(token: str, path: str, data: dict, message: str,
                      *, owner: str = config.DATA_REPO_OWNER,
                      repo: str = config.DATA_REPO_NAME,
                      cached_sha: str | None = None, attempts: int = 3) -> str:
    """put_json, but on a sha conflict re-fetches and retries a few times.

    ``cached_sha`` lets the caller skip the initial GET in the common case
    (pass the sha returned by the previous successful write); if it's stale
    we fall back to fetching fresh automatically.
    """
    sha = cached_sha
    last_exc: Exception | None = None
    for _ in range(attempts):
        if sha is None:
            _, sha = get_file(token, path, owner=owner, repo=repo)
        try:
            return put_json(token, path, data, message, sha, owner=owner, repo=repo)
        except ConflictError as exc:
            last_exc = exc
            sha = None  # force a fresh fetch next loop
            time.sleep(0.5)
    raise last_exc or ConflictError(path)


def list_dir(token: str, path: str, *, owner: str = config.DATA_REPO_OWNER,
            repo: str = config.DATA_REPO_NAME) -> list[dict]:
    """List entries in a "directory". Empty list if it doesn't exist."""
    resp = requests.get(f"{_api_base(owner, repo)}/contents/{path}",
                        params={"ref": "main"}, headers=_headers(token), timeout=20)
    if resp.status_code == 404:
        return []
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []
