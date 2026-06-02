"""Daily Reddit project digest → Telegram bot.

Env vars required:
  REDDIT_CLIENT_ID
  REDDIT_CLIENT_SECRET
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID

Env vars optional:
  REDDIT_PROFILE      path to YAML profile (default: profiles/ai_side_projects.yaml)
  REDDIT_TIME_FILTER  week|month|year|all (default: week)
  DIGEST_TOP_N        projects per digest (default: 10)
"""

from __future__ import annotations

import html
import json
import os
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from scrape import analyze_results, create_reddit, load_profile, scrape_subreddit

PROFILES_DIR = Path(__file__).parent / "profiles"
TG_MAX_CHARS = 4000


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"Error: {name} environment variable not set", file=sys.stderr)
        sys.exit(1)
    return val


def send_telegram(token: str, chat_id: str, text: str) -> None:
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"Telegram API {resp.status}: {body[:200]}")


def split_into_chunks(text: str, limit: int = TG_MAX_CHARS) -> list[str]:
    """Split on double newlines to keep project blocks intact."""
    if len(text) <= limit:
        return [text]
    blocks = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = (current + "\n\n" + block).lstrip() if current else block
        if len(candidate) > limit:
            if current:
                chunks.append(current.strip())
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current.strip())
    return chunks


def make_description(post: dict) -> str:
    """Return ≤250-char plain-text description from post data."""
    preview = (post.get("selftext_preview") or "").strip()
    if preview:
        preview = preview.rstrip(".")
        return preview[:247] + "..." if len(preview) > 250 else preview

    # Fallback: synthesize from extracted tags
    extr = post.get("extractions") or {}
    parts: list[str] = []
    domains = extr.get("domains") or []
    if domains:
        parts.append(", ".join(domains[:3]))
    tools = extr.get("ai_tools") or []
    if tools:
        parts.append("built with " + ", ".join(tools[:2]))
    tech = extr.get("tech_stack") or []
    if tech:
        parts.append("stack: " + ", ".join(tech[:3]))
    text = " · ".join(parts) if parts else "No description available"
    return text[:250]


def format_digest(posts: list[dict], date_str: str) -> str:
    header = f"<b>Reddit Side Projects — {date_str}</b>"
    entries: list[str] = []
    for i, post in enumerate(posts, 1):
        raw_title = post["title"]
        title = html.escape(raw_title[:77] + "…" if len(raw_title) > 80 else raw_title)
        desc = html.escape(make_description(post).replace("\\~", "~").replace("\\.", "."))
        sub = html.escape(post["subreddit"])
        score = post["score"]
        url = post["url"]
        entries.append(
            f"{i}. <b>{title}</b>\n"
            f"r/{sub} · ↑{score}\n"
            f"{desc}\n"
            f"{url}"
        )
    return header + "\n\n" + "\n\n".join(entries)


def main() -> None:
    client_id = _require_env("REDDIT_CLIENT_ID")
    client_secret = _require_env("REDDIT_CLIENT_SECRET")
    tg_token = _require_env("TELEGRAM_BOT_TOKEN")
    tg_chat = _require_env("TELEGRAM_CHAT_ID")

    profile_path = Path(os.environ.get("REDDIT_PROFILE") or PROFILES_DIR / "ai_side_projects.yaml")
    time_filter = os.environ.get("REDDIT_TIME_FILTER", "week")
    top_n = int(os.environ.get("DIGEST_TOP_N", "10"))

    config = load_profile(profile_path)
    config.date_from = None
    config.date_to = None
    config.time_filter = time_filter

    reddit = create_reddit(client_id, client_secret, "reddit-digest/1.0")

    all_posts: list[dict] = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(scrape_subreddit, reddit, sub, config, 20, i): sub
            for i, sub in enumerate(config.subreddits)
        }
        for future in as_completed(futures):
            try:
                all_posts.extend(future.result())
            except Exception as e:
                print(f"Warning: subreddit scrape failed — {e}", file=sys.stderr)

    seen: set[str] = set()
    unique = [p for p in all_posts if not (p["id"] in seen or seen.add(p["id"]))]  # type: ignore[func-returns-value]

    if not unique:
        send_telegram(tg_token, tg_chat, "Reddit digest: no posts found today.")
        print("No posts found, sent placeholder message.")
        return

    analysis = analyze_results(unique, config)
    top_posts = analysis["top_posts"][:top_n]

    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    digest = format_digest(top_posts, date_str)

    chunks = split_into_chunks(digest)
    for chunk in chunks:
        send_telegram(tg_token, tg_chat, chunk)

    print(f"Sent {len(top_posts)} projects across {len(chunks)} message(s).")


if __name__ == "__main__":
    main()
