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


def _get_blob(token: str, owner: str, repo: str, sha: str) -> bytes:
    """Fetch a blob's content directly by sha, via the Git Data API.

    The Contents API's GET /contents/{path} silently returns an EMPTY
    "content" field for any file over 1MB (documented GitHub behavior,
    easy to miss since it's not an error -- base64-decoding that empty
    string just produces empty bytes, so a request for a large file looks
    like it "succeeded" while actually writing out nothing usable). The
    Blobs API has no such limit (up to 100MB), so we fall back to it
    whenever Contents didn't actually give us the bytes.
    """
    resp = requests.get(f"{_api_base(owner, repo)}/git/blobs/{sha}",
                        headers=_headers(token), timeout=60)
    resp.raise_for_status()
    return base64.b64decode(resp.json()["content"])


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
    sha = data.get("sha")
    content = data.get("content")
    if content:
        raw = base64.b64decode(content)
    elif data.get("size", 0) > 0:
        # Non-empty file, but Contents API gave us no content -- the >1MB
        # case described above.
        raw = _get_blob(token, owner, repo, sha)
    else:
        raw = b""
    return raw, sha


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


def put_bytes_retrying(token: str, path: str, content: bytes, message: str,
                       *, owner: str = config.DATA_REPO_OWNER,
                       repo: str = config.DATA_REPO_NAME,
                       cached_sha: str | None = None, attempts: int = 3) -> str:
    """put_bytes, but resolves the current sha first and retries on conflict.

    Needed for any path that gets written more than once (e.g. the same
    day's log, re-uploaded on a later request): GitHub's Contents API
    requires the file's current sha to update an existing file and returns
    422 (not 409) if it's simply missing — so, unlike put_json_retrying,
    this always fetches the sha up front rather than only after a conflict.
    """
    sha = cached_sha
    last_exc: Exception | None = None
    for _ in range(attempts):
        if sha is None:
            _, sha = get_file(token, path, owner=owner, repo=repo)
        try:
            return put_bytes(token, path, content, message, sha, owner=owner, repo=repo)
        except ConflictError as exc:
            last_exc = exc
            sha = None  # force a fresh fetch next loop
            time.sleep(0.5)
    raise last_exc or ConflictError(path)


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


def get_file_conditional(token: str, path: str, etag: str | None = None, *,
                         owner: str = config.DATA_REPO_OWNER,
                         repo: str = config.DATA_REPO_NAME):
    """Cheap polling primitive: a conditional GET using If-None-Match.

    Returns (changed, raw_bytes_or_None, sha_or_None, etag_or_None).
    ``changed`` is False on a 304 (content identical to what ``etag`` names) —
    GitHub does not count a 304 against the API rate limit, so this is safe to
    call every few seconds even across many machines sharing one token.
    Returns changed=True, raw=None on a 404 (file deleted/never existed).
    """
    headers = _headers(token)
    if etag:
        headers["If-None-Match"] = etag
    resp = requests.get(f"{_api_base(owner, repo)}/contents/{path}",
                        params={"ref": "main"}, headers=headers, timeout=15)
    if resp.status_code == 304:
        return False, None, None, etag
    if resp.status_code == 404:
        return True, None, None, None
    resp.raise_for_status()
    data = resp.json()
    content = data.get("content")
    if content:
        raw = base64.b64decode(content)
    elif data.get("size", 0) > 0:
        raw = _get_blob(token, owner, repo, data.get("sha"))
    else:
        raw = b""
    return True, raw, data.get("sha"), resp.headers.get("ETag")


def delete_file(token: str, path: str, sha: str, message: str, *,
                owner: str = config.DATA_REPO_OWNER,
                repo: str = config.DATA_REPO_NAME) -> None:
    resp = requests.delete(
        f"{_api_base(owner, repo)}/contents/{path}", headers=_headers(token),
        json={"message": message, "sha": sha, "branch": "main"}, timeout=20,
    )
    resp.raise_for_status()


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
