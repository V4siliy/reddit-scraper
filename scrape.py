"""Reddit scraper — universal, config-driven via YAML profiles.

Usage:
    uv run scrape.py --client-id ID --client-secret SECRET
    uv run scrape.py --client-id ID --client-secret SECRET --profile profiles/ai_side_projects.yaml
    uv run scrape.py --client-id ID --client-secret SECRET --profile profiles/ai_tools_landscape.yaml --no-date-filter
    uv run scrape.py --client-id ID --client-secret SECRET --date-from 2026-03-01 --date-to 2026-03-31
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone, date
from pathlib import Path

from tqdm import tqdm

import praw
import yaml
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import IntPrompt


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Term:
    name: str
    patterns: list[re.Pattern]


@dataclass
class ExtractionCategory:
    key: str
    display: str
    terms: dict[str, Term]


@dataclass
class Config:
    name: str
    description: str
    subreddits: list[str]
    search_queries: list[str]
    date_from: date | None
    date_to: date | None
    time_filter: str
    min_score: int
    post_types: dict[str, list[re.Pattern]]
    extractions: list[ExtractionCategory]
    notable_min_score: int
    notable_trigger_extractions: list[str]
    source_path: Path
    step2_system_prompt: str | None = None
    step4_system_prompt: str | None = None


def _compile(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def load_profile(path: Path) -> Config:
    raw = yaml.safe_load(path.read_text())

    post_types = {
        tag: _compile(pats)
        for tag, pats in (raw.get("post_types") or {}).items()
    }

    extractions: list[ExtractionCategory] = []
    for cat_key, cat_data in (raw.get("extractions") or {}).items():
        terms = {
            term_key: Term(
                name=td["name"],
                patterns=_compile(td.get("patterns", [])),
            )
            for term_key, td in (cat_data.get("terms") or {}).items()
        }
        extractions.append(ExtractionCategory(
            key=cat_key,
            display=cat_data.get("display", cat_key),
            terms=terms,
        ))

    nq = raw.get("notable_quotes") or {}
    df = raw.get("date_from")
    dt = raw.get("date_to")

    return Config(
        name=raw["name"],
        description=raw.get("description", ""),
        subreddits=raw.get("subreddits", []),
        search_queries=raw.get("search_queries", []),
        date_from=date.fromisoformat(str(df)) if df else None,
        date_to=date.fromisoformat(str(dt)) if dt else None,
        time_filter=raw.get("time_filter", "month"),
        min_score=raw.get("min_score", 1),
        post_types=post_types,
        extractions=extractions,
        notable_min_score=nq.get("min_score", 3),
        notable_trigger_extractions=nq.get("trigger_extractions", []),
        source_path=path,
        step2_system_prompt=raw.get("step2_system_prompt") or None,
        step4_system_prompt=raw.get("step4_system_prompt") or None,
    )


def select_profile(profiles_dir: Path, console: Console) -> Config:
    yamls = sorted(profiles_dir.glob("*.yaml")) + sorted(profiles_dir.glob("*.yml"))
    if not yamls:
        console.print(f"[red]No profiles found in {profiles_dir}/[/red]")
        sys.exit(1)

    console.print("\n[bold]Available profiles:[/bold]\n")
    configs: list[Config | None] = []
    for i, p in enumerate(yamls, 1):
        try:
            cfg = load_profile(p)
            configs.append(cfg)
            console.print(f"  [cyan]{i}.[/cyan] [bold]{cfg.name}[/bold]")
            if cfg.description:
                console.print(f"     [dim]{cfg.description}[/dim]")
            dates_str = (
                f"{cfg.date_from} → {cfg.date_to}" if cfg.date_from
                else f"time_filter={cfg.time_filter}"
            )
            console.print(f"     [dim]{len(cfg.subreddits)} subreddits · {dates_str}[/dim]")
        except Exception as e:
            console.print(f"  [red]{i}. {p.name} (failed to load: {e})[/red]")
            configs.append(None)

    console.print()
    choice = IntPrompt.ask("Select profile", default=1)
    idx = choice - 1
    if idx < 0 or idx >= len(configs) or configs[idx] is None:
        console.print("[red]Invalid selection[/red]")
        sys.exit(1)
    return configs[idx]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Classification & extraction
# ---------------------------------------------------------------------------

def classify_post_types(title: str, body: str, config: Config) -> list[str]:
    text = f"{title} {body[:4000]}"
    return [
        tag for tag, patterns in config.post_types.items()
        if any(p.search(text) for p in patterns)
    ]


def extract_category(text: str, cat: ExtractionCategory) -> list[str]:
    return [
        key for key, term in cat.terms.items()
        if any(p.search(text) for p in term.patterns)
    ]


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------

def create_reddit(client_id: str, client_secret: str, user_agent: str) -> praw.Reddit:
    return praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
    )


def scrape_subreddit(
    reddit: praw.Reddit,
    subreddit_name: str,
    config: Config,
    limit: int,
    position: int = 0,
) -> list[dict]:
    """Scrape one subreddit using config queries, date range, and extraction categories."""
    sub = reddit.subreddit(subreddit_name)
    seen_ids: set[str] = set()
    results: list[dict] = []
    consecutive_429s = 0

    with tqdm(
        config.search_queries,
        desc=f"r/{subreddit_name:<22}",
        position=position,
        leave=True,
        unit="q",
        dynamic_ncols=True,
    ) as pbar:
        for query in pbar:
            pbar.set_postfix(found=len(results), refresh=False)
            try:
                for post in sub.search(
                    query, sort="relevance", time_filter=config.time_filter, limit=limit
                ):
                    if post.id in seen_ids:
                        continue
                    seen_ids.add(post.id)

                    if post.score < config.min_score:
                        continue

                    post_date = datetime.fromtimestamp(post.created_utc, tz=timezone.utc).date()
                    if config.date_from and post_date < config.date_from:
                        continue
                    if config.date_to and post_date > config.date_to:
                        continue

                    post.comment_sort = "best"
                    post.comments.replace_more(limit=0)
                    top_comments: list[dict] = []
                    for comment in post.comments[:15]:
                        if hasattr(comment, "body") and comment.score >= 1:
                            top_comments.append({
                                "author": str(comment.author) if comment.author else "[deleted]",
                                "score": comment.score,
                                "body": comment.body[:2000],
                                "created_utc": datetime.fromtimestamp(
                                    comment.created_utc, tz=timezone.utc
                                ).isoformat(),
                            })
                            if comment.score >= 5 and hasattr(comment, "replies"):
                                for reply in comment.replies[:3]:
                                    if hasattr(reply, "body") and reply.score >= 2:
                                        top_comments.append({
                                            "author": str(reply.author) if reply.author else "[deleted]",
                                            "score": reply.score,
                                            "body": reply.body[:1000],
                                            "is_reply": True,
                                            "created_utc": datetime.fromtimestamp(
                                                reply.created_utc, tz=timezone.utc
                                            ).isoformat(),
                                        })

                    full_text = post.title + " " + (post.selftext or "")
                    comment_text = " ".join(c["body"] for c in top_comments)
                    all_text = full_text + " " + comment_text

                    results.append({
                        "id": post.id,
                        "subreddit": subreddit_name,
                        "title": post.title,
                        "score": post.score,
                        "upvote_ratio": post.upvote_ratio,
                        "num_comments": post.num_comments,
                        "url": f"https://reddit.com{post.permalink}",
                        "created_utc": datetime.fromtimestamp(
                            post.created_utc, tz=timezone.utc
                        ).isoformat(),
                        "selftext": post.selftext[:5000] if post.selftext else "",
                        "flair": post.link_flair_text,
                        "post_types": classify_post_types(post.title, post.selftext or "", config),
                        "extractions": {
                            cat.key: extract_category(all_text, cat)
                            for cat in config.extractions
                        },
                        "top_comments": top_comments,
                    })
                consecutive_429s = 0
            except Exception as e:
                if "429" in str(e):
                    consecutive_429s += 1
                    if consecutive_429s >= 2:
                        pbar.set_postfix(found=len(results), status="rate-limited", refresh=True)
                        tqdm.write(f"  r/{subreddit_name}: rate-limited (429), skipping remaining queries")
                        break
                else:
                    tqdm.write(f"  r/{subreddit_name} query error: {e}")

    return results


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_results(posts: list[dict], config: Config) -> dict:
    counters: dict[str, Counter] = {cat.key: Counter() for cat in config.extractions}
    post_type_counter: Counter = Counter()

    for post in posts:
        for cat in config.extractions:
            for key in post["extractions"].get(cat.key, []):
                counters[cat.key][key] += 1
        for t in post.get("post_types", []):
            post_type_counter[t] += 1

    total = max(len(posts), 1)

    def rank(counter: Counter, cat: ExtractionCategory) -> list[dict]:
        return [
            {
                "key": key,
                "name": cat.terms[key].name if key in cat.terms else key,
                "count": count,
                "pct": round(count / total * 100, 1),
            }
            for key, count in counter.most_common()
        ]

    cat_by_key = {cat.key: cat for cat in config.extractions}

    notable_quotes: list[dict] = []
    for post in posts:
        for comment in post.get("top_comments", []):
            if comment["score"] < config.notable_min_score:
                continue
            body = comment["body"]
            triggered: dict[str, list[str]] = {}
            for cat_key in config.notable_trigger_extractions:
                cat = cat_by_key.get(cat_key)
                if cat:
                    matches = extract_category(body, cat)
                    if matches:
                        triggered[cat_key] = matches
            if triggered:
                notable_quotes.append({
                    "text": body[:400],
                    "score": comment["score"],
                    "matches": triggered,
                    "post_title": post["title"],
                    "post_url": post["url"],
                })

    seen_texts: set[str] = set()
    unique_quotes: list[dict] = []
    for q in sorted(notable_quotes, key=lambda x: -x["score"]):
        key = q["text"][:80]
        if key not in seen_texts:
            seen_texts.add(key)
            unique_quotes.append(q)
    unique_quotes = unique_quotes[:50]

    top_posts_sorted = sorted(
        posts,
        key=lambda p: p["score"] * (1 + p["num_comments"] * 0.1),
        reverse=True,
    )[:30]

    def term_names(keys: list[str], cat: ExtractionCategory) -> list[str]:
        return [cat.terms[k].name if k in cat.terms else k for k in keys]

    top_posts_out = [
        {
            "title": p["title"],
            "score": p["score"],
            "comments": p["num_comments"],
            "url": p["url"],
            "date": p["created_utc"][:10],
            "subreddit": p["subreddit"],
            "post_types": p["post_types"],
            "extractions": {
                cat.key: term_names(p["extractions"].get(cat.key, []), cat)
                for cat in config.extractions
            },
            "selftext_preview": (
                (p["selftext"][:300] + "...") if len(p["selftext"]) > 300 else p["selftext"]
            ),
        }
        for p in top_posts_sorted
    ]

    examples: dict[str, dict[str, list]] = {}
    for cat in config.extractions:
        examples[cat.key] = {
            key: [
                {
                    "title": p["title"],
                    "score": p["score"],
                    "url": p["url"],
                    "date": p["created_utc"][:10],
                    "subreddit": p["subreddit"],
                }
                for p in top_posts_sorted
                if key in p["extractions"].get(cat.key, [])
            ][:5]
            for key in counters[cat.key]
        }

    return {
        "meta": {
            "total_posts": total,
            "profile": config.name,
            "post_types": dict(post_type_counter),
        },
        "rankings": {cat.key: rank(counters[cat.key], cat) for cat in config.extractions},
        "examples": examples,
        "top_posts": top_posts_out,
        "notable_quotes": unique_quotes,
    }


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _rank_table(title: str, ranking: list[dict], console: Console) -> None:
    if not ranking:
        return
    max_count = max(r["count"] for r in ranking)
    console.print(f"\n[bold]{title}[/bold]")
    t = Table()
    t.add_column("#", style="dim", width=3)
    t.add_column("Name", style="cyan")
    t.add_column("Posts", justify="right", style="green")
    t.add_column("%", justify="right", style="yellow")
    t.add_column("Bar", style="blue")
    for i, r in enumerate(ranking, 1):
        bar = "█" * int(r["count"] / max_count * 30)
        t.add_row(str(i), r["name"], str(r["count"]), f"{r['pct']}%", bar)
    console.print(t)


def display_analysis(analysis: dict, config: Config, console: Console) -> None:
    meta = analysis["meta"]
    pt = meta["post_types"]

    pt_parts = [f"Total posts: [bold]{meta['total_posts']}[/bold]"]
    for tag, count in pt.items():
        pt_parts.append(f"{tag}: [cyan]{count}[/cyan]")

    console.print()
    console.print(Panel(
        "  |  ".join(pt_parts),
        title=f"Post Overview — {config.name}",
    ))

    for cat in config.extractions:
        _rank_table(cat.display, analysis["rankings"].get(cat.key, []), console)

    console.print("\n[bold]Top Posts[/bold]")
    tbl = Table(show_lines=True)
    tbl.add_column("Score", justify="right", style="green", width=6)
    tbl.add_column("Sub", style="dim", width=16)
    tbl.add_column("Title", style="white", max_width=50)
    visible_cats = config.extractions[:2]
    for cat in visible_cats:
        tbl.add_column(cat.display, style="cyan", max_width=22)
    tbl.add_column("Date", style="dim", width=10)

    for p in analysis["top_posts"][:20]:
        row = [str(p["score"]), f"r/{p['subreddit']}", p["title"]]
        for cat in visible_cats:
            vals = p["extractions"].get(cat.key, [])
            row.append(", ".join(vals[:2]) if vals else "-")
        row.append(p["date"])
        tbl.add_row(*row)
    console.print(tbl)

    quotes = analysis.get("notable_quotes", [])[:8]
    if quotes:
        console.print("\n[bold]Notable Quotes[/bold]")
        for q in quotes:
            all_matches = [m for matches in q["matches"].values() for m in matches]
            console.print(f"  [dim]↑{q['score']}[/dim] [cyan]{', '.join(all_matches)}[/cyan]")
            text = q["text"][:200].replace("\n", " ")
            console.print(f'    "{text}..."')
            console.print(f"    [dim]— {q['post_title'][:60]}[/dim]")
            console.print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

PROFILES_DIR = Path(__file__).parent / "profiles"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reddit scraper — config-driven via YAML profiles"
    )
    parser.add_argument("--client-id", required=True, help="Reddit API client ID")
    parser.add_argument("--client-secret", required=True, help="Reddit API client secret")
    parser.add_argument("--user-agent", default="reddit-scraper/2.0")
    parser.add_argument("--profile", help="Path to YAML profile (skips interactive selection)")
    parser.add_argument("--limit", type=int, default=30, help="Max posts per query per subreddit")
    parser.add_argument("--subreddits", nargs="+", help="Override subreddits from profile")
    parser.add_argument("--date-from", help="Override date_from (YYYY-MM-DD)")
    parser.add_argument("--date-to", help="Override date_to (YYYY-MM-DD)")
    parser.add_argument("--no-date-filter", action="store_true", help="Disable date filtering")
    parser.add_argument(
        "--time-filter", choices=["week", "month", "year", "all"],
        help="Override Reddit time window",
    )
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    parser.add_argument("--workers", type=int, default=5, help="Parallel subreddit workers")
    parser.add_argument("--output", default=f"results_{run_id}.json")
    parser.add_argument("--raw-output", default=f"raw_posts_{run_id}.json")
    args = parser.parse_args()

    console = Console()

    if args.profile:
        config = load_profile(Path(args.profile))
    else:
        config = select_profile(PROFILES_DIR, console)

    # CLI overrides
    if args.subreddits:
        config.subreddits = args.subreddits
    if args.time_filter:
        config.time_filter = args.time_filter
    if args.no_date_filter:
        config.date_from = None
        config.date_to = None
    else:
        if args.date_from:
            config.date_from = date.fromisoformat(args.date_from)
        if args.date_to:
            config.date_to = date.fromisoformat(args.date_to)

    dates_str = (
        f"{config.date_from} → {config.date_to}" if config.date_from
        else f"time_filter={config.time_filter}"
    )
    console.print(f"\n[bold]Profile:[/bold] {config.name}")
    console.print(f"Subreddits ({len(config.subreddits)}): {', '.join(config.subreddits)}")
    console.print(
        f"Date range: {dates_str} | Limit: {args.limit}/query | "
        f"Queries: {len(config.search_queries)} | Workers: {args.workers}"
    )
    console.print()

    reddit = create_reddit(args.client_id, args.client_secret, args.user_agent)

    all_posts: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(scrape_subreddit, reddit, sub, config, args.limit, i): sub
            for i, sub in enumerate(config.subreddits)
        }
        with tqdm(
            total=len(futures),
            desc=f"{'Overall':<26}",
            position=len(config.subreddits),
            unit="sub",
            dynamic_ncols=True,
        ) as overall:
            for future in as_completed(futures):
                sub_name = futures[future]
                try:
                    posts = future.result()
                    all_posts.extend(posts)
                except Exception as e:
                    tqdm.write(f"r/{sub_name} failed: {e}")
                overall.update(1)

    # Deduplicate across subreddits
    seen: set[str] = set()
    unique_posts = [p for p in all_posts if not (p["id"] in seen or seen.add(p["id"]))]  # type: ignore[func-returns-value]

    console.print(f"\n[bold]Total unique posts: {len(unique_posts)}[/bold]")

    analysis = analyze_results(unique_posts, config)
    display_analysis(analysis, config, console)

    Path(args.output).write_text(json.dumps(analysis, indent=2, ensure_ascii=False))
    console.print(f"\n[green]Analysis → {args.output}[/green]")

    Path(args.raw_output).write_text(json.dumps(unique_posts, indent=2, ensure_ascii=False))
    console.print(f"[green]Raw data → {args.raw_output} ({len(unique_posts)} posts)[/green]")


if __name__ == "__main__":
    main()
