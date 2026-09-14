"""Post finished videos via Buffer (TikTok / YouTube / Instagram).

Flow: finished mp4 -> durable public URL -> Buffer createPost per channel.
Buffer only accepts videos by public URL, so the file is first uploaded to
Cloudinary (free tier, unsigned preset = no secret in code; 5-minute setup,
see config.example.yaml) unless --video-url is given.

Proven live 2026-09-14 against the real API: TikTok + YouTube drafts.
YouTube Shorts via Buffer REQUIRES portrait video (landscape is rejected
with a clear error). Posts are DRAFTS by default; --publish / --schedule /
--at release them.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from config import BUFFER_SERVICES, Config

BUFFER_GQL = "https://api.buffer.com/graphql"
REQUEST_TIMEOUT = 90

# Only PostActionSuccess + InvalidInputError are spread: the other error
# union members are identified by __typename alone, so an introspection
# gap there can never break the mutation.
CREATE_POST = """mutation($input: CreatePostInput!) {
  createPost(input: $input) {
    __typename
    ... on PostActionSuccess { post { id status dueAt text } }
    ... on InvalidInputError { message }
  }
}"""

_REFUSAL_HINTS = {
    "LimitReachedError": "plan or queue limit reached — check the Buffer dashboard",
    "UnauthorizedError": "API key lacks access — check the key and channel permissions",
    "NotFoundError": "channel not found — it may have been disconnected",
    "UnexpectedError": "Buffer internal error — retry later",
    "RestProxyError": "the social network rejected the post — see Buffer dashboard for why",
}


class BufferError(Exception):
    """Anything the Buffer API refuses or the network breaks."""


class BufferClient:
    """Tiny GraphQL client for the calls autopost needs."""

    def __init__(self, api_key: str, timeout: int = REQUEST_TIMEOUT):
        self.api_key = api_key
        self.timeout = timeout

    def _gql(self, query: str, variables: dict | None = None) -> dict:
        try:
            response = requests.post(
                BUFFER_GQL,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json={"query": query, "variables": variables or {}},
                timeout=self.timeout,
            )
        except Exception as exc:
            raise BufferError(f"network error talking to Buffer: {exc}")
        if response.status_code != 200:
            raise BufferError(f"Buffer HTTP {response.status_code}: {response.text[:200]}")
        try:
            payload = response.json()
        except ValueError:
            raise BufferError(f"Buffer returned non-JSON: {response.text[:200]}")
        if payload.get("errors"):
            detail = "; ".join(error.get("message", "?") for error in payload["errors"])
            raise BufferError(f"Buffer: {detail[:300]}")
        return payload["data"]

    def organizations(self) -> list[dict]:
        data = self._gql("{ account { email organizations { id name } } }")
        return data["account"]["organizations"]

    def channels(self, organization_id: str) -> list[dict]:
        data = self._gql(
            '{ channels(input: {organizationId: "%s"}) '
            "{ id name service type isDisconnected } }" % organization_id)
        return data["channels"]

    def create_post(self, post_input: dict) -> dict:
        data = self._gql(CREATE_POST, {"input": post_input})
        result = data["createPost"]
        typename = result["__typename"]
        if typename == "PostActionSuccess":
            return result["post"]
        if typename == "InvalidInputError":
            raise BufferError(f"Buffer refused the post: {result.get('message')}")
        raise BufferError(f"Buffer refused ({typename}): "
                          f"{_REFUSAL_HINTS.get(typename, 'see the Buffer dashboard')}")


def match_channels(channels: list[dict], wanted: list[str]) -> list[dict]:
    """Pick the connected channel for each wanted service. Pure (tested)."""
    picked = []
    for service in wanted:
        found = next((c for c in channels
                      if c["service"] == service and not c.get("isDisconnected")), None)
        if found is None:
            available = ", ".join(sorted({c["service"] for c in channels})) or "none"
            raise BufferError(f"no connected {service} channel (available: {available})")
        picked.append(found)
    return picked


def hashtags(tags: list[str]) -> str:
    """[' vienna woods ', 'fun-fact'] -> '#viennawoods #funfact'."""
    return " ".join("#" + re.sub(r"\W+", "", tag) for tag in tags if tag and tag.strip())


def build_post_input(channel_id: str, service: str, video_url: str, title: str,
                     description: str, tags: list[str], cfg: Config, *,
                     save_to_draft: bool = True, mode: str = "addToQueue",
                     due_at: str | None = None) -> dict:
    """Pure CreatePostInput builder (tested). AI-disclosure flags always on."""
    title = (title or "Untitled").strip()[:100]  # YouTube title limit
    tagline = hashtags(tags)
    if service == "youtube":
        metadata = {"youtube": {
            "title": title,
            "privacy": cfg.youtube_privacy,
            "madeForKids": cfg.buffer_made_for_kids,
            "isAiGenerated": True,
            "categoryId": cfg.youtube_category_id,
            "embeddable": True,
            "notifySubscribers": False,
        }}
        text = description.strip() or title
    elif service == "tiktok":
        caption = f"{title} {tagline}".strip()[:2200]
        metadata = {"tiktok": {"title": caption, "isAiGenerated": True}}
        text = description.strip() or caption
    elif service == "instagram":
        metadata = {"instagram": {"isAiGenerated": True,
                                  "shouldShareToFeed": True, "type": "reel"}}
        text = f"{description.strip() or title}\n\n{tagline}".strip()
    else:
        raise BufferError(f"unsupported service: {service} (want one of {BUFFER_SERVICES})")
    post = {
        "channelId": channel_id,
        "text": text,
        "assets": [{"video": {"url": video_url}}],
        "mode": mode,
        "schedulingType": "automatic",
        "needsApproval": False,
        "saveToDraft": save_to_draft,
        "aiAssisted": True,
        "metadata": metadata,
    }
    if due_at:
        post["dueAt"] = due_at
    return post


def resolve_mode(*, publish: bool = False, schedule: bool = False,
                 at: str | None = None) -> tuple[str, str | None, bool]:
    """CLI flags -> (graphql mode, due_at ISO, save_to_draft). Pure (tested)."""
    if at:
        try:
            moment = datetime.fromisoformat(at)
        except ValueError:
            raise BufferError(f"--at must be ISO like 2026-09-15T18:00 (got {at!r})")
        if moment.tzinfo is None:
            moment = moment.astimezone()  # naive = this PC's timezone
        moment = moment.astimezone(timezone.utc)
        if moment <= datetime.now(timezone.utc):
            raise BufferError("--at must be in the future")
        return "customScheduled", moment.isoformat(), False
    if publish:
        return "shareNow", None, False
    if schedule:
        return "addToQueue", None, False
    return "addToQueue", None, True


def cloudinary_upload_url(cloud_name: str) -> str:
    return f"https://api.cloudinary.com/v1_1/{cloud_name}/video/upload"


def upload_video(path: Path, cloud_name: str, upload_preset: str,
                 timeout: int = 600) -> str:
    """Upload to a Cloudinary unsigned preset. Returns the secure_url."""
    url = cloudinary_upload_url(cloud_name)
    data = Path(path).read_bytes()  # read once so retries don't re-open
    last = "unknown"
    for attempt in range(1, 4):
        try:
            response = requests.post(
                url,
                data={"upload_preset": upload_preset},
                files={"file": (Path(path).name, data, "video/mp4")},
                timeout=timeout,
            )
        except Exception as exc:
            last = str(exc)[:160]
            print(f"  [autopost] upload attempt {attempt}/3 failed: {last[:100]}")
            time.sleep(5 * attempt)
            continue
        if response.status_code == 200:
            secure = response.json().get("secure_url")
            if secure:
                return secure
            last = f"no secure_url in response: {response.text[:160]}"
        elif response.status_code in (400, 401, 403):
            # Config wrong (bad cloud name / preset not UNSIGNED) — no retry.
            raise BufferError(
                f"Cloudinary rejected the upload (HTTP {response.status_code}): "
                f"{response.text[:200]} — check cloud_name and that the "
                f"upload preset is UNSIGNED")
        else:
            last = f"HTTP {response.status_code}: {response.text[:160]}"
        print(f"  [autopost] upload attempt {attempt}/3 failed: {last[:100]}")
        time.sleep(5 * attempt)
    raise BufferError(f"Cloudinary upload failed: {last}")


def read_sidecar(video: Path) -> dict:
    """Title/description/tags from <video>.json or meta.json next to it."""
    for candidate in (video.with_suffix(".json"), video.parent / "meta.json"):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
    return {}


def newest_video(out_dir: Path) -> Path | None:
    """Most recently modified .mp4 in a directory, or None."""
    videos = sorted(Path(out_dir).glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    return videos[-1] if videos else None
