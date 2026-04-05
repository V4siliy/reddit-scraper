"""Generate a magazine-style EPUB from Reddit scraper results.

Usage:
    uv run epub.py results_20260405_143022_abc.json raw_posts_20260405_143022_abc.json
    uv run epub.py --latest
    uv run epub.py --latest --output my_digest.epub
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import datetime
from pathlib import Path

import markdown as md
from ebooklib import epub

# ---------------------------------------------------------------------------
# CSS — clean serif layout for e-ink
# ---------------------------------------------------------------------------

CSS = """
body {
    font-family: Georgia, "Times New Roman", serif;
    font-size: 1em;
    line-height: 1.7;
    margin: 1.8em 1.5em;
    color: #000;
    background: #fff;
}

/* ── Cover ── */
.cover {
    text-align: center;
    padding: 3em 1em;
}
.cover-title {
    font-size: 2.4em;
    font-weight: bold;
    line-height: 1.2;
    margin-bottom: 0.3em;
}
.cover-subtitle {
    font-size: 1.1em;
    font-style: italic;
    margin-bottom: 2em;
}
.cover-rule {
    border: none;
    border-top: 3px solid #000;
    margin: 1em auto;
    width: 60%;
}
.cover-stats {
    font-size: 0.9em;
    line-height: 2;
}
.cover-stat-value {
    font-size: 1.8em;
    font-weight: bold;
    display: block;
}

/* ── Section headings ── */
h1 {
    font-size: 1.9em;
    margin-top: 0;
    margin-bottom: 0.2em;
    line-height: 1.2;
}
h2 {
    font-size: 1.4em;
    font-weight: bold;
    border-bottom: 2px solid #000;
    padding-bottom: 0.3em;
    margin-top: 2em;
    margin-bottom: 0.8em;
}
h3 {
    font-size: 1.1em;
    font-weight: bold;
    margin-top: 1.5em;
    margin-bottom: 0.3em;
}

/* ── Article ── */
.article {
    margin-bottom: 3em;
}
.article-headline {
    font-size: 1.6em;
    font-weight: bold;
    line-height: 1.25;
    margin-bottom: 0.4em;
}
.article-byline {
    font-size: 0.8em;
    color: #444;
    margin-bottom: 0.6em;
    font-style: italic;
}
.article-tags {
    margin-bottom: 1em;
    font-size: 0.8em;
}
.tag {
    display: inline-block;
    border: 1px solid #888;
    padding: 0.1em 0.5em;
    margin: 0 0.2em 0.2em 0;
    border-radius: 2px;
    font-size: 0.85em;
}
.article-body p {
    margin: 0.7em 0;
    text-align: justify;
}
.article-rule {
    border: none;
    border-top: 1px solid #ccc;
    margin: 2.5em 0;
}

/* ── Comments ── */
.comments-heading {
    font-size: 0.85em;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-top: 1.5em;
    margin-bottom: 0.8em;
    border-top: 1px solid #000;
    padding-top: 0.6em;
}
.comment {
    margin: 0.8em 0;
    padding-left: 1em;
    border-left: 2px solid #bbb;
    font-size: 0.9em;
}
.comment-meta {
    font-size: 0.75em;
    color: #555;
    font-style: italic;
}

/* ── Digest cards (brief listing) ── */
.digest-item {
    margin: 1.2em 0;
    padding-bottom: 1.2em;
    border-bottom: 1px solid #ddd;
}
.digest-title {
    font-weight: bold;
    font-size: 1em;
}
.digest-meta {
    font-size: 0.78em;
    color: #555;
    font-style: italic;
    margin: 0.2em 0;
}
.digest-excerpt {
    font-size: 0.88em;
    margin-top: 0.3em;
}

/* ── Quotes ── */
.pull-quote {
    margin: 1.5em 0.5em;
    padding: 0.8em 1.2em;
    border-left: 4px solid #000;
    font-style: italic;
    font-size: 1.05em;
    line-height: 1.6;
}
.pull-quote-attr {
    font-size: 0.78em;
    font-style: normal;
    color: #444;
    margin-top: 0.4em;
    display: block;
}

/* ── Rankings ── */
.ranking-list {
    list-style: none;
    padding: 0;
    margin: 0.5em 0 1.5em 0;
}
.ranking-list li {
    display: flex;
    justify-content: space-between;
    padding: 0.35em 0;
    border-bottom: 1px solid #eee;
    font-size: 0.95em;
}
.ranking-list li:last-child {
    border-bottom: none;
}
.rank-num {
    color: #888;
    width: 1.8em;
    flex-shrink: 0;
}
.rank-name {
    flex: 1;
}
.rank-bar {
    font-size: 0.7em;
    letter-spacing: -0.05em;
    color: #444;
    align-self: center;
    margin: 0 0.5em;
}
.rank-count {
    color: #555;
    font-size: 0.85em;
    white-space: nowrap;
}

/* ── Stats block ── */
.stats-grid {
    margin: 1em 0;
}
.stat-row {
    display: flex;
    justify-content: space-between;
    padding: 0.4em 0;
    border-bottom: 1px solid #eee;
    font-size: 0.9em;
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _md_to_html(text: str) -> str:
    """Convert Reddit markdown to HTML, cleaning up common quirks."""
    if not text:
        return ""
    # Normalize line endings, strip excessive blank lines
    text = re.sub(r'\r\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return md.markdown(text, extensions=["nl2br", "sane_lists"])


def _excerpt(text: str, length: int = 220) -> str:
    """Plain-text excerpt, no markdown."""
    plain = re.sub(r'[#*_`>\[\]]+', '', text or "").strip()
    plain = re.sub(r'\s+', ' ', plain)
    if len(plain) <= length:
        return plain
    cut = plain[:length].rsplit(' ', 1)[0]
    return cut + "…"


def _score_label(score: int) -> str:
    if score >= 1000:
        return f"{score / 1000:.1f}k"
    return str(score)


def _get_extractions(post: dict) -> dict[str, list[str]]:
    """Normalize old (flat) and new (extractions dict) post formats."""
    if "extractions" in post:
        return post["extractions"]
    # Old format: flat keys
    return {
        "ai_tools": post.get("ai_tools", []),
        "tech_stack": post.get("tech_stack", []),
        "acquisition": post.get("acquisition_channels", []),
        "domains": post.get("problem_domains", []),
    }


def _get_rankings(results: dict) -> dict[str, list[dict]]:
    """Normalize old and new results formats."""
    if "rankings" in results:
        return results["rankings"]
    # Old format: flat _ranking keys
    out = {}
    for key in ["ai_tools_ranking", "tech_stack_ranking", "acquisition_ranking", "problem_domain_ranking"]:
        if key in results:
            label = key.replace("_ranking", "")
            out[label] = results[key]
    return out


def _new_chapter(uid: str, title: str, file_name: str, content: str) -> epub.EpubHtml:
    ch = epub.EpubHtml(title=title, file_name=file_name, lang="en")
    ch.content = f'<?xml version="1.0" encoding="utf-8"?>\n{content}'
    ch.add_item(epub.EpubItem(uid="style_ref", file_name="style/main.css",
                               media_type="text/css", content=""))
    return ch


def _wrap(body: str, extra_class: str = "") -> str:
    cls = f" {extra_class}" if extra_class else ""
    return f'<html xmlns="http://www.w3.org/1999/xhtml"><head><link rel="stylesheet" type="text/css" href="style/main.css"/></head><body{cls}>{body}</body></html>'


# ---------------------------------------------------------------------------
# Chapter builders
# ---------------------------------------------------------------------------

def build_cover(results: dict) -> str:
    meta = results.get("meta", {})
    profile = meta.get("profile", "Reddit Digest")
    total = meta.get("total_posts", 0)
    pt = meta.get("post_types", {})
    now = datetime.now().strftime("%B %Y")

    launch = pt.get("launch_story", 0)
    ai_posts = pt.get("ai_assisted", 0)
    paid = pt.get("paid_users", 0)

    html = f"""
<div class="cover">
  <div class="cover-title">{profile}</div>
  <div class="cover-subtitle">Weekly Research Digest</div>
  <hr class="cover-rule"/>
  <div class="cover-subtitle">{now}</div>
  <hr class="cover-rule"/>
  <div class="cover-stats">
    <span class="cover-stat-value">{total}</span> posts analysed<br/><br/>
    <span class="cover-stat-value">{launch}</span> launch stories<br/><br/>
    <span class="cover-stat-value">{ai_posts}</span> AI-assisted builds<br/><br/>
    <span class="cover-stat-value">{paid}</span> posts mentioning revenue
  </div>
</div>"""
    return _wrap(html)


def build_feature_article(post: dict, index: int) -> str:
    ext = _get_extractions(post)

    tags_html = ""
    for cat_vals in ext.values():
        for v in cat_vals[:3]:
            tags_html += f'<span class="tag">{v}</span>'

    body_html = _md_to_html(post.get("selftext", ""))
    if not body_html:
        body_html = "<p><em>(link post — no body text)</em></p>"

    date_str = post.get("created_utc", "")[:10]
    score = _score_label(post.get("score", 0))
    comments_count = post.get("num_comments", 0)

    # Top 3 comments
    comments_html = ""
    top = [c for c in post.get("top_comments", []) if not c.get("is_reply") and len(c.get("body", "")) > 40][:3]
    if top:
        comments_html = '<p class="comments-heading">Selected Comments</p>'
        for c in top:
            author = c.get("author", "?")
            c_score = c.get("score", 0)
            body = _excerpt(c.get("body", ""), 300)
            comments_html += f"""
<div class="comment">
  <div class="comment-meta">↑{c_score} · u/{author}</div>
  <p>{body}</p>
</div>"""

    ordinals = ["", "I", "II", "III", "IV", "V", "VI", "VII"]
    label = ordinals[index] if index < len(ordinals) else str(index)

    html = f"""
<div class="article">
  <p style="font-size:0.75em; text-transform:uppercase; letter-spacing:0.1em; margin-bottom:0.3em;">Feature {label}</p>
  <div class="article-headline">{post['title']}</div>
  <div class="article-byline">r/{post['subreddit']} · ↑{score} · {comments_count} comments · {date_str}</div>
  <div class="article-tags">{tags_html}</div>
  <div class="article-body">{body_html}</div>
  {comments_html}
</div>"""
    return _wrap(html)


def build_digest_section(title: str, posts: list[dict], intro: str = "") -> str:
    if not posts:
        return ""

    items_html = ""
    for post in posts:
        ext = _get_extractions(post)
        all_tags = [v for vals in ext.values() for v in vals[:2]]
        tags_str = " · ".join(all_tags[:4]) if all_tags else ""
        excerpt = _excerpt(post.get("selftext", ""), 180)
        score = _score_label(post.get("score", 0))
        date_str = post.get("created_utc", "")[:10]
        sub = post.get("subreddit", "")

        items_html += f"""
<div class="digest-item">
  <div class="digest-title">{post['title']}</div>
  <div class="digest-meta">r/{sub} · ↑{score} · {date_str}{(" · " + tags_str) if tags_str else ""}</div>
  {f'<div class="digest-excerpt">{excerpt}</div>' if excerpt else ""}
</div>"""

    intro_html = f"<p>{intro}</p>" if intro else ""
    html = f"<h2>{title}</h2>{intro_html}{items_html}"
    return _wrap(html)


def build_quotes(notable_quotes: list[dict]) -> str:
    if not notable_quotes:
        return ""

    items_html = ""
    for q in notable_quotes[:20]:
        text = q.get("text", "")[:350].replace("\n", " ").strip()
        post_title = q.get("post_title", "")[:70]
        score = q.get("score", 0)
        items_html += f"""
<div class="pull-quote">
  {text}
  <span class="pull-quote-attr">↑{score} — in "{post_title}"</span>
</div>"""

    html = f"""
<h2>Notable Quotes</h2>
<p>Highest-scored comments that discuss tools, acquisition, or key insights.</p>
{items_html}"""
    return _wrap(html)


def build_rankings(results: dict) -> str:
    rankings = _get_rankings(results)
    if not rankings:
        return ""

    sections_html = ""
    labels = {
        "ai_tools": "AI Coding Tools",
        "ai_tools_ranking": "AI Coding Tools",
        "tech_stack": "Tech Stack",
        "tech_stack_ranking": "Tech Stack",
        "acquisition": "How They Got First Users",
        "acquisition_ranking": "How They Got First Users",
        "domains": "Problem Domains",
        "problem_domain_ranking": "Problem Domains",
    }

    for cat_key, items in rankings.items():
        if not items:
            continue
        label = labels.get(cat_key, cat_key.replace("_", " ").title())
        top = items[:12]
        max_count = max(r["count"] for r in top) if top else 1

        rows = ""
        for i, r in enumerate(top, 1):
            bar_len = max(1, int(r["count"] / max_count * 12))
            bar = "█" * bar_len
            rows += f"""
<li>
  <span class="rank-num">{i}.</span>
  <span class="rank-name">{r['name']}</span>
  <span class="rank-bar">{bar}</span>
  <span class="rank-count">{r['count']} ({r['pct']}%)</span>
</li>"""

        sections_html += f"""
<h2>{label}</h2>
<ul class="ranking-list">{rows}</ul>"""

    html = f"""
<h1>By the Numbers</h1>
<p>Aggregated counts across all scraped posts. Percentages are share of total posts.</p>
{sections_html}"""
    return _wrap(html)


def build_glance(results: dict) -> str:
    meta = results.get("meta", {})
    pt = meta.get("post_types", {})
    rankings = _get_rankings(results)

    # Top item from first two rankings
    highlights = []
    for cat_key, items in list(rankings.items())[:2]:
        if items:
            top = items[0]
            highlights.append(f"<b>{top['name']}</b> was the most mentioned {cat_key.replace('_ranking','').replace('_',' ')} ({top['count']} posts, {top['pct']}%)")

    highlight_html = "".join(f"<li>{h}</li>" for h in highlights)

    rows = ""
    for tag, count in sorted(pt.items(), key=lambda x: -x[1]):
        label = tag.replace("_", " ").title()
        rows += f'<div class="stat-row"><span>{label}</span><span><b>{count}</b></span></div>'

    html = f"""
<h2>This Week at a Glance</h2>
<p>A quick summary of what the community was talking about.</p>

<h3>Highlights</h3>
<ul>{highlight_html}</ul>

<h3>Post Types</h3>
<div class="stats-grid">{rows}</div>
"""
    return _wrap(html)


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_epub(results: dict, raw_posts: list[dict], output_path: Path) -> None:
    profile_name = results.get("meta", {}).get("profile", "Reddit Digest")
    total = results.get("meta", {}).get("total_posts", len(raw_posts))

    book = epub.EpubBook()
    book.set_identifier(str(uuid.uuid4()))
    book.set_title(profile_name)
    book.set_language("en")
    book.add_author("Reddit Scraper")
    book.add_metadata("DC", "description", f"Research digest · {total} posts")

    style_item = epub.EpubItem(
        uid="main_css",
        file_name="style/main.css",
        media_type="text/css",
        content=CSS,
    )
    book.add_item(style_item)

    # Sort raw posts by engagement for feature selection
    scored = sorted(
        raw_posts,
        key=lambda p: p.get("score", 0) * (1 + p.get("num_comments", 0) * 0.1),
        reverse=True,
    )

    # Deduplicate (same id may appear from multiple subreddits)
    seen: set[str] = set()
    unique: list[dict] = []
    for p in scored:
        if p["id"] not in seen:
            seen.add(p["id"])
            unique.append(p)

    # Separate feature posts (have body text) from link posts
    with_text = [p for p in unique if len(p.get("selftext", "")) > 100]
    features = with_text[:6]
    feature_ids = {p["id"] for p in features}

    # Categorise remaining posts by post_type
    rest = [p for p in unique if p["id"] not in feature_ids]

    def of_type(tag: str) -> list[dict]:
        return [p for p in rest if tag in p.get("post_types", [])][:12]

    launch_posts = of_type("launch_story")
    first_user_posts = of_type("first_users")
    ai_posts = [p for p in rest
                if "ai_assisted" in p.get("post_types", [])
                and p["id"] not in {x["id"] for x in launch_posts + first_user_posts}][:10]

    used_ids = feature_ids | {p["id"] for p in launch_posts + first_user_posts + ai_posts}
    more_posts = [p for p in unique[6:] if p["id"] not in used_ids][:20]

    chapters: list[epub.EpubHtml] = []
    toc: list = []

    def add_chapter(uid: str, title: str, filename: str, html: str) -> epub.EpubHtml:
        ch = epub.EpubHtml(title=title, file_name=filename, lang="en")
        ch.content = html
        ch.add_item(style_item)
        book.add_item(ch)
        chapters.append(ch)
        toc.append(epub.Link(filename, title, uid))
        return ch

    # ── Cover ──
    add_chapter("cover", profile_name, "cover.xhtml", build_cover(results))

    # ── At a Glance ──
    add_chapter("glance", "This Week at a Glance", "glance.xhtml", build_glance(results))

    # ── Feature articles (one chapter each) ──
    toc_features: list[epub.Link] = []
    for i, post in enumerate(features, 1):
        uid = f"feature_{i}"
        title = post["title"][:60]
        filename = f"feature_{i}.xhtml"
        html = build_feature_article(post, i)
        ch = epub.EpubHtml(title=title, file_name=filename, lang="en")
        ch.content = html
        ch.add_item(style_item)
        book.add_item(ch)
        chapters.append(ch)
        toc_features.append(epub.Link(filename, title, uid))

    toc.append((epub.Section("Feature Stories"), toc_features))

    # ── Digest sections ──
    if launch_posts:
        add_chapter("launches", "Launch Stories",
                    "launches.xhtml",
                    build_digest_section(
                        "Launch Stories",
                        launch_posts,
                        "Projects that shipped this week — from solo founders to small teams."))

    if first_user_posts:
        add_chapter("first_users", "Getting First Users",
                    "first_users.xhtml",
                    build_digest_section(
                        "Getting First Users",
                        first_user_posts,
                        "Stories and strategies for finding those critical early adopters."))

    if ai_posts:
        add_chapter("ai_builds", "Built with AI",
                    "ai_builds.xhtml",
                    build_digest_section(
                        "Built with AI",
                        ai_posts,
                        "Projects where AI coding tools played a central role."))

    # ── Notable quotes ──
    notable = results.get("notable_quotes", [])
    if notable:
        add_chapter("quotes", "Notable Quotes", "quotes.xhtml", build_quotes(notable))

    # ── Rankings ──
    rankings_html = build_rankings(results)
    if rankings_html:
        add_chapter("numbers", "By the Numbers", "numbers.xhtml", rankings_html)

    # ── More stories ──
    if more_posts:
        add_chapter("more", "More Stories",
                    "more.xhtml",
                    build_digest_section(
                        "More Stories",
                        more_posts,
                        "Further reading from the week's posts."))

    # ── Assemble ──
    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + chapters

    epub.write_epub(str(output_path), book)
    print(f"✓ {output_path}  ({len(unique)} posts · {len(chapters)} chapters)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def find_latest_pair() -> tuple[Path, Path]:
    results_files = sorted(Path(".").glob("results_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not results_files:
        raise FileNotFoundError("No results_*.json files found in current directory")
    results_path = results_files[0]
    raw_path = Path(str(results_path).replace("results_", "raw_posts_", 1))
    if not raw_path.exists():
        raise FileNotFoundError(f"Matching raw posts file not found: {raw_path}")
    return results_path, raw_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate magazine-style EPUB from scraper results")
    parser.add_argument("results", nargs="?", help="results_*.json file")
    parser.add_argument("raw", nargs="?", help="raw_posts_*.json file")
    parser.add_argument("--latest", action="store_true", help="Auto-use most recent results/raw pair")
    parser.add_argument("--output", help="Output EPUB path (auto-named by default)")
    args = parser.parse_args()

    if args.latest or (not args.results and not args.raw):
        results_path, raw_path = find_latest_pair()
        print(f"Using: {results_path.name} + {raw_path.name}")
    else:
        if not args.results or not args.raw:
            parser.error("Provide both results and raw_posts files, or use --latest")
        results_path = Path(args.results)
        raw_path = Path(args.raw)

    results = json.loads(results_path.read_text())
    raw_posts = json.loads(raw_path.read_text())

    if args.output:
        output_path = Path(args.output)
    else:
        stem = results_path.stem.replace("results_", "digest_", 1)
        output_path = Path(f"{stem}.epub")

    build_epub(results, raw_posts, output_path)


if __name__ == "__main__":
    main()
