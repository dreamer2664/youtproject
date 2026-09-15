"""Buffer analytics: what the channel actually did. Read-only, never raises.

Shapes were discovered by introspecting Buffer's GraphQL live and verified
against real posts: posts(first/after, input:{organizationId}) returns
edges{node{...}} newest-first, and every sent Post carries metrics[] of
{name, value, unit} ("Video Views", "Reach", "Eng. Rate", ...). Feeds
`python main.py stats` and Jarvis's channel_report.
"""

from __future__ import annotations

import time

from config import Config

_NODE = "id status text sentAt channelService dueAt createdAt metrics { name value }"
_PAGE = 50


def _client(cfg: Config):
    from autopost import BufferClient

    if not cfg.buffer_api_key:
        return None
    return BufferClient(cfg.buffer_api_key)


def fetch_posts(cfg: Config, limit: int = 100) -> list[dict]:
    """Newest-first Buffer posts with metrics. [] when unconfigured/failing."""
    client = _client(cfg)
    if client is None:
        return []
    try:
        orgs = client.organizations()
        if not orgs:
            return []
        query = ("query($first: Int, $after: String, $input: PostsInput!) {"
                 " posts(first: $first, after: $after, input: $input) {"
                 f" edges {{ node {{ {_NODE} }} cursor }}"
                 " pageInfo { hasNextPage endCursor } } }")
        out, after = [], None
        while len(out) < limit:
            page = client._gql(query, {"first": min(_PAGE, limit - len(out)),
                                       "after": after,
                                       "input": {"organizationId": orgs[0]["id"]}})
            posts = (page.get("posts") or {})
            for edge in posts.get("edges") or []:
                if edge.get("node"):
                    out.append(edge["node"])
            info = posts.get("pageInfo") or {}
            after = info.get("endCursor")
            if not info.get("hasNextPage") or not after:
                break
        return out[:limit]
    except Exception:
        return []


def _metric(node: dict, name: str) -> float:
    for metric in node.get("metrics") or []:
        if metric.get("name") == name:
            try:
                return float(metric.get("value") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def channel_stats(cfg: Config, days: int = 30) -> dict:
    """Roll-up for the window. Always returns a dict; {'error'} when blind."""
    if not cfg.buffer_api_key:
        return {"error": "no Buffer API key (buffer.api_key)"}
    cutoff = time.time() - max(1, days) * 86400
    posts = fetch_posts(cfg)
    if not posts:
        return {"days": days, "posts": 0, "note": "no Buffer posts found"}
    sent, services, top = [], {}, None
    scheduled = sum(1 for node in posts if node.get("status") == "scheduled")
    for node in posts:
        stamp = node.get("sentAt") or node.get("createdAt") or ""
        try:
            recent = time.mktime(time.strptime(stamp[:19], "%Y-%m-%dT%H:%M:%S"))
        except (ValueError, TypeError):
            recent = 0
        if node.get("status") != "sent" or recent < cutoff:
            continue
        service = node.get("channelService") or "?"
        views = _metric(node, "Video Views")
        bucket = services.setdefault(service, {"posts": 0, "views": 0,
                                               "reach": 0, "eng": []})
        bucket["posts"] += 1
        bucket["views"] += views
        bucket["reach"] += _metric(node, "Reach")
        bucket["eng"].append(_metric(node, "Eng. Rate"))
        sent.append(node)
        if top is None or views > top["views"]:
            top = {"id": node.get("id"), "service": service, "views": views,
                   "text": (node.get("text") or "")[:80]}
    for bucket in services.values():
        eng = bucket.pop("eng")
        bucket["eng_rate_avg"] = round(sum(eng) / len(eng), 2) if eng else 0
        bucket["views"] = int(bucket["views"])
        bucket["reach"] = int(bucket["reach"])
    return {"days": days, "posts": len(sent), "scheduled": scheduled,
            "services": services, "top": top}
