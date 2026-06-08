"""Daily Reddit project digest → Telegram bot.

Env vars required:
  REDDIT_CLIENT_ID
  REDDIT_CLIENT_SECRET
  TELEGRAM_BOT_TOKEN      default channel; overridden per-profile via telegram_chat_id in YAML
  OPENROUTER_API_KEY

Env vars optional:
  REDDIT_PROFILES     comma-separated profile paths (default: profiles/ai_side_projects.yaml)
  REDDIT_TIME_FILTER  hour|day|week|month|year|all (default: day)
  DIGEST_TOP_N        projects per digest (default: 10)
  STEP2_MODEL         OpenRouter model for per-topic descriptions (default: google/gemini-3.1-flash-lite)
  STEP4_MODEL         OpenRouter model for digest formatting (default: google/gemini-3.5-flash)
"""

from __future__ import annotations

import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from operator import attrgetter
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from telegram import Bot
from telegram.constants import ParseMode

from scrape import Config, create_reddit, load_profile, scrape_subreddit

PROFILES_DIR = Path(__file__).parent / "profiles"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_STEP2_MODEL = "google/gemini-3.1-flash-lite"
DEFAULT_STEP4_MODEL = "google/gemini-3.5-flash"
DEFAULT_TIME_FILTER = "day"
DEFAULT_TOP_N = 10
POSTS_PER_SUBREDDIT = 20
MAX_SCRAPE_WORKERS = 5
TG_MAX_CHARS = 4000
TOPIC_SELFTEXT_MAX_CHARS = 3000
COMMENT_BODY_MAX_CHARS = 600
COMMENT_PREVIEW_MAX_CHARS = 300
PROMOTED_TITLE_MAX_CHARS = 100

STEP2_SYSTEM_PROMPT = (
    "You analyze Reddit posts about side projects, AI tools, and technology. "
    "For each post produce two things:\n"
    "1. headline — a punchy rewritten title (≤80 chars) that captures the core idea "
    "more compellingly than the original. Think newsletter subject line, not clickbait.\n"
    "2. tagline — one sharp sentence (≤120 chars) that delivers the key insight or "
    "takeaway. Write it as a statement worth sharing, not a description of what the post says.\n"
    "No markdown, no HTML. Plain text only.\n"
    "Also list indices of top-level COMMENT[N] entries (by their 0-based N) that discuss "
    "a completely independent shareable project, not just a reaction to the main post. "
    "If no comment qualifies, return an empty list for promoted_comment_indices."
)

STEP4_SYSTEM_PROMPT = (
    "Format the provided Reddit topics into a Telegram HTML digest. "
    "Allowed tags only: <b>, <i>, <a href='...'>, <code>. "
    "Each entry must follow this exact structure:\n\n"
    "RANK. <a href=\"POST_URL\"><b>HEADLINE</b></a>\n"
    "r/SUBREDDIT · ↑SCORE · NUM_COMMENTS comments\n"
    "TAGLINE\n\n"
    "Open the digest with: <b>Reddit Side Projects — DATE</b>\n\n"
    "Never place raw URLs as visible text — always use <a href> anchor tags. "
    "Output only the formatted HTML digest, nothing else."
)


class Comment(BaseModel):
    author: str
    score: int
    body: str
    created_utc: str
    replies: list[Comment] = []


Comment.model_rebuild()


class Topic(BaseModel):
    id: str
    subreddit: str
    title: str
    score: int
    num_comments: int
    url: str
    created_utc: str
    selftext: str
    post_types: list[str]
    extractions: dict[str, list[str]]
    top_comments: list[Comment]
    selftext_preview: str


class TopicAnalysis(BaseModel):
    headline: str
    tagline: str
    promoted_comment_indices: list[int]


class DigestText(BaseModel):
    html: str


class AnalyzedTopic(BaseModel):
    topic: Topic
    headline: str
    tagline: str
    rank_score: float


class Env(BaseModel):
    reddit_client_id: str
    reddit_client_secret: str
    tg_token: str
    tg_chat_id: str | None
    openrouter_api_key: str
    profile_paths: list[Path]
    time_filter: str
    top_n: int
    step2_model: str
    step4_model: str


class RunConfig(BaseModel):
    tg_token: str
    tg_chat_id: str
    openrouter_api_key: str
    top_n: int
    step2_model: str
    step4_model: str
    step2_prompt: str
    step4_prompt: str
    profile_name: str


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"Error: {name} environment variable not set", file=sys.stderr)
        sys.exit(1)
    return val


def _parse_profile_paths() -> list[Path]:
    profiles_raw = os.environ.get("REDDIT_PROFILES", "").strip()
    if profiles_raw:
        return [Path(p.strip()) for p in profiles_raw.split(",") if p.strip()]
    single = os.environ.get("REDDIT_PROFILE", "").strip()
    if single:
        return [Path(single)]
    return [PROFILES_DIR / "ai_side_projects.yaml"]


def load_env() -> Env:
    return Env(
        reddit_client_id=_require_env("REDDIT_CLIENT_ID"),
        reddit_client_secret=_require_env("REDDIT_CLIENT_SECRET"),
        tg_token=_require_env("TELEGRAM_BOT_TOKEN"),
        tg_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip() or None,
        openrouter_api_key=_require_env("OPENROUTER_API_KEY"),
        profile_paths=_parse_profile_paths(),
        time_filter=os.environ.get("REDDIT_TIME_FILTER", DEFAULT_TIME_FILTER),
        top_n=int(os.environ.get("DIGEST_TOP_N", str(DEFAULT_TOP_N))),
        step2_model=os.environ.get("STEP2_MODEL", DEFAULT_STEP2_MODEL),
        step4_model=os.environ.get("STEP4_MODEL", DEFAULT_STEP4_MODEL),
    )


def build_run_config(env: Env, config: Config) -> RunConfig:
    chat_id = config.telegram_chat_id or env.tg_chat_id
    if not chat_id:
        print(
            f"Error: no telegram_chat_id for profile '{config.name}' "
            "and TELEGRAM_CHAT_ID env var is not set",
            file=sys.stderr,
        )
        sys.exit(1)
    return RunConfig(
        tg_token=env.tg_token,
        tg_chat_id=chat_id,
        openrouter_api_key=env.openrouter_api_key,
        top_n=env.top_n,
        step2_model=env.step2_model,
        step4_model=env.step4_model,
        step2_prompt=config.step2_system_prompt or STEP2_SYSTEM_PROMPT,
        step4_prompt=config.step4_system_prompt or STEP4_SYSTEM_PROMPT.replace(
            "Reddit Side Projects", config.name
        ),
        profile_name=config.name,
    )


def make_openrouter_model(model_name: str, api_key: str) -> OpenAIChatModel:
    provider = OpenAIProvider(base_url=OPENROUTER_BASE_URL, api_key=api_key)
    return OpenAIChatModel(model_name, provider=provider)


def _group_comments(flat_comments: list[dict]) -> list[Comment]:
    """Convert flat comment list with is_reply markers into a parent→replies tree."""
    grouped: list[Comment] = []
    current_parent: Comment | None = None
    for raw in flat_comments:
        comment = Comment(
            author=raw["author"],
            score=raw["score"],
            body=raw["body"][:COMMENT_BODY_MAX_CHARS],
            created_utc=raw["created_utc"],
        )
        if raw.get("is_reply") and current_parent is not None:
            current_parent.replies.append(comment)
        else:
            grouped.append(comment)
            current_parent = comment
    return grouped


def post_to_topic(post: dict) -> Topic:
    return Topic(
        id=post["id"],
        subreddit=post["subreddit"],
        title=post["title"],
        score=post["score"],
        num_comments=post["num_comments"],
        url=post["url"],
        created_utc=post["created_utc"],
        selftext=post.get("selftext", "")[:TOPIC_SELFTEXT_MAX_CHARS],
        post_types=post.get("post_types", []),
        extractions=post.get("extractions", {}),
        top_comments=_group_comments(post.get("top_comments", [])),
        selftext_preview=post.get("selftext_preview", ""),
    )


def _scrape_one(reddit, subreddit_name: str, config: Config, position: int) -> list[dict]:
    return scrape_subreddit(
        reddit,
        subreddit_name=subreddit_name,
        config=config,
        limit=POSTS_PER_SUBREDDIT,
        position=position,
    )


def collect_topics(reddit, config: Config) -> list[Topic]:
    """Scrape all configured subreddits concurrently and return deduplicated topics."""
    raw_posts: list[dict] = []
    with ThreadPoolExecutor(max_workers=MAX_SCRAPE_WORKERS) as executor:
        futures = {
            executor.submit(_scrape_one, reddit, subreddit_name=sub, config=config, position=i): sub
            for i, sub in enumerate(config.subreddits)
        }
        for future in as_completed(futures):
            try:
                raw_posts.extend(future.result())
            except Exception as exc:
                print(f"Warning: subreddit scrape failed — {exc}", file=sys.stderr)

    unique: list[dict] = []
    seen: set[str] = set()
    for post in raw_posts:
        if post["id"] not in seen:
            seen.add(post["id"])
            unique.append(post)
    return [post_to_topic(p) for p in unique]


def topic_context_text(topic: Topic) -> str:
    """Render a topic and its nested comments as a prompt-ready text block."""
    lines = [f"TITLE: {topic.title}", f"URL: {topic.url}"]
    if topic.selftext:
        lines.append(f"BODY:\n{topic.selftext}")
    for i, comment in enumerate(topic.top_comments):
        lines.append(f"COMMENT[{i}] (score={comment.score}): {comment.body}")
        for reply in comment.replies:
            lines.append(f"  REPLY (score={reply.score}): {reply.body}")
    return "\n".join(lines)


def compute_rank_score(topic: Topic) -> float:
    return topic.score * (1 + topic.num_comments * 0.1)


def promoted_comment_to_topic(comment: Comment, parent: Topic, index: int) -> Topic:
    """Build a synthetic top-level topic from a comment promoted out of its thread."""
    first_line = comment.body.split("\n")[0][:PROMOTED_TITLE_MAX_CHARS]
    return Topic(
        id=f"{parent.id}_comment_{index}",
        subreddit=parent.subreddit,
        title=first_line,
        score=comment.score,
        num_comments=len(comment.replies),
        url=parent.url,
        created_utc=comment.created_utc,
        selftext=comment.body,
        post_types=[],
        extractions={},
        top_comments=comment.replies,
        selftext_preview=comment.body[:COMMENT_PREVIEW_MAX_CHARS],
    )


async def _safe_analyze_topic(
    agent: Agent,
    topic: Topic,
) -> tuple[Topic, TopicAnalysis, list[Topic]] | None:
    try:
        result = await agent.run(topic_context_text(topic))
        analysis: TopicAnalysis = result.output
        promoted = [
            promoted_comment_to_topic(topic.top_comments[idx], parent=topic, index=idx)
            for idx in analysis.promoted_comment_indices
            if 0 <= idx < len(topic.top_comments)
        ]
        return topic, analysis, promoted
    except Exception as exc:
        print(
            f"Warning: topic analysis failed for '{topic.title[:60]}' — {exc}",
            file=sys.stderr,
        )
        return None


async def analyze_all_topics(
    topics: list[Topic],
    model: OpenAIChatModel,
    system_prompt: str = STEP2_SYSTEM_PROMPT,
) -> list[AnalyzedTopic]:
    """Run flash model on all topics in parallel; collect descriptions and promoted comments."""
    agent = Agent(model, output_type=TopicAnalysis, system_prompt=system_prompt)
    outcomes = await asyncio.gather(*[_safe_analyze_topic(agent, t) for t in topics])

    results: list[AnalyzedTopic] = []
    promoted_topics: list[Topic] = []
    for outcome in outcomes:
        if outcome is None:
            continue
        original, analysis, promoted = outcome
        results.append(AnalyzedTopic(
            topic=original,
            headline=analysis.headline,
            tagline=analysis.tagline,
            rank_score=compute_rank_score(original),
        ))
        promoted_topics.extend(promoted)

    for synthetic in promoted_topics:
        results.append(AnalyzedTopic(
            topic=synthetic,
            headline=synthetic.title,
            tagline=synthetic.selftext_preview,
            rank_score=compute_rank_score(synthetic),
        ))

    return results


def _topics_to_prompt(topics: list[AnalyzedTopic], date_str: str) -> str:
    """Serialize analyzed topics into a structured prompt string for the formatter model."""
    lines = [f"Date: {date_str}", ""]
    for i, at in enumerate(topics, 1):
        t = at.topic
        lines.append(
            f"{i}. headline={at.headline!r} subreddit={t.subreddit!r} "
            f"score={t.score} num_comments={t.num_comments} url={t.url}"
        )
        lines.append(f"   tagline: {at.tagline}")
        lines.append("")
    return "\n".join(lines)


async def format_digest(
    topics: list[AnalyzedTopic],
    date_str: str,
    model: OpenAIChatModel,
    system_prompt: str = STEP4_SYSTEM_PROMPT,
) -> str:
    """Run the step-4 formatter model to produce Telegram HTML from the top topics."""
    agent = Agent(model, output_type=DigestText, system_prompt=system_prompt)
    result = await agent.run(_topics_to_prompt(topics, date_str))
    return result.output.html


def split_into_chunks(text: str) -> list[str]:
    """Split on double newlines keeping each chunk under TG_MAX_CHARS."""
    if len(text) <= TG_MAX_CHARS:
        return [text]
    blocks = text.split("\n\n")
    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = (current + "\n\n" + block).lstrip() if current else block
        if len(candidate) > TG_MAX_CHARS:
            if current:
                chunks.append(current.strip())
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current.strip())
    return chunks


async def send_digest_chunks(chunks: list[str], tg_token: str, tg_chat_id: str) -> None:
    async with Bot(token=tg_token) as bot:
        for chunk in chunks:
            await bot.send_message(
                chat_id=tg_chat_id,
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )


async def async_main(topics: list[Topic], run_cfg: RunConfig) -> None:
    if not topics:
        async with Bot(token=run_cfg.tg_token) as bot:
            await bot.send_message(
                chat_id=run_cfg.tg_chat_id,
                text=f"Reddit digest ({run_cfg.profile_name}): no posts found today.",
            )
        print("No posts found, sent placeholder message.")
        return

    print(f"Step 2: analyzing {len(topics)} topics with {run_cfg.step2_model}...")
    flash_model = make_openrouter_model(run_cfg.step2_model, run_cfg.openrouter_api_key)
    analyzed = await analyze_all_topics(topics, flash_model, system_prompt=run_cfg.step2_prompt)
    analyzed.sort(key=attrgetter("rank_score"), reverse=True)
    top = analyzed[: run_cfg.top_n]

    date_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    print(f"Step 4: formatting top {len(top)} topics with {run_cfg.step4_model}...")
    formatter_model = make_openrouter_model(run_cfg.step4_model, run_cfg.openrouter_api_key)
    digest_html = await format_digest(top, date_str, formatter_model, system_prompt=run_cfg.step4_prompt)

    chunks = split_into_chunks(digest_html)
    await send_digest_chunks(chunks, run_cfg.tg_token, run_cfg.tg_chat_id)
    print(f"Sent {len(top)} projects across {len(chunks)} message(s).")


def main() -> None:
    load_dotenv()
    env = load_env()

    reddit = create_reddit(
        env.reddit_client_id,
        client_secret=env.reddit_client_secret,
        user_agent="reddit-digest/1.0",
    )

    for profile_path in env.profile_paths:
        print(f"\n── {profile_path.name} ──")
        config = load_profile(profile_path)
        config.date_from = None
        config.date_to = None
        config.time_filter = env.time_filter

        run_cfg = build_run_config(env, config)

        print("Step 1: collecting posts from Reddit...")
        topics = collect_topics(reddit, config)
        print(f"Collected {len(topics)} unique topics.")

        asyncio.run(async_main(topics, run_cfg))


if __name__ == "__main__":
    main()
