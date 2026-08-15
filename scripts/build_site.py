#!/usr/bin/env python3
"""World News Daily - site generator."""

import html
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from jinja2 import Template

JST = ZoneInfo("Asia/Tokyo")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

REQUEST_TIMEOUT = 15

BBC_FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC", "https://feeds.bbci.co.uk/news/rss.xml"),
]

ECONOMIST_FEEDS = [
    ("The Economist", "https://www.economist.com/international/rss.xml"),
    ("The Economist", "https://www.economist.com/the-world-this-week/rss.xml"),
    ("The Economist", "https://www.economist.com/europe/rss.xml"),
]

ECONOMIST_FALLBACK_FEED = (
    "The Economist",
    "https://news.google.com/rss/search?q=site:economist.com+when:2d&hl=en-US&gl=US&ceid=US:en",
)

TOTAL_STORIES = 10
MIN_ECONOMIST_STORIES = 3

TAG_RE = re.compile(r"<[^>]+>")


def clean_summary(raw_summary):
    if not raw_summary:
        return ""
    text = TAG_RE.sub(" ", raw_summary)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 280:
        text = text[:280].rsplit(" ", 1)[0] + "..."
    return text


def parse_published(pub_date_text):
    if not pub_date_text:
        return None
    try:
        dt = parsedate_to_datetime(pub_date_text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _text(el):
    return (el.text or "").strip() if el is not None else ""


def fetch_feed(source_name, url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  ! {source_name} ({url}) fetch failed: {exc}", file=sys.stderr)
        return []

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        print(f"  ! {source_name} ({url}) parse failed: {exc}", file=sys.stderr)
        return []

    items = root.findall("./channel/item")
    stories = []
    for item in items:
        title = html.unescape(_text(item.find("title")))
        link = _text(item.find("link"))
        if not title or not link:
            continue
        summary = clean_summary(_text(item.find("description")))
        published = parse_published(_text(item.find("pubDate")))
        display_title = (
            re.sub(r"\s*-\s*[^-]+$", "", title)
            if source_name == "The Economist" and " - " in title
            else title
        )
        stories.append(
            {
                "source": source_name,
                "title": display_title,
                "summary": summary,
                "link": link,
                "published": published,
            }
        )
    print(f"  - {source_name} ({url}): {len(stories)} items")
    return stories


def dedupe(stories):
    seen = set()
    out = []
    for s in stories:
        key = " ".join(s["title"].lower().split()[:8])
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def collect_stories():
    print("Fetching BBC feeds...")
    bbc_stories = []
    for name, url in BBC_FEEDS:
        bbc_stories.extend(fetch_feed(name, url))
    bbc_stories = dedupe(bbc_stories)

    print("Fetching The Economist feeds...")
    economist_stories = []
    for name, url in ECONOMIST_FEEDS:
        economist_stories.extend(fetch_feed(name, url))
        if len(economist_stories) >= MIN_ECONOMIST_STORIES:
            break
    economist_stories = dedupe(economist_stories)

    if len(economist_stories) < MIN_ECONOMIST_STORIES:
        print("  Economist feeds thin, trying Google News fallback...")
        name, url = ECONOMIST_FALLBACK_FEED
        economist_stories = dedupe(economist_stories + fetch_feed(name, url))

    def sort_key(s):
        return s["published"] or datetime.min.replace(tzinfo=timezone.utc)

    bbc_stories.sort(key=sort_key, reverse=True)
    economist_stories.sort(key=sort_key, reverse=True)

    picked = []
    bi, ei = 0, 0
    econ_quota = min(MIN_ECONOMIST_STORIES, len(economist_stories))
    while len(picked) < TOTAL_STORIES and (bi < len(bbc_stories) or ei < len(economist_stories)):
        if ei < econ_quota and ei < len(economist_stories):
            picked.append(economist_stories[ei])
            ei += 1
        elif bi < len(bbc_stories):
            picked.append(bbc_stories[bi])
            bi += 1
        elif ei < len(economist_stories):
            picked.append(economist_stories[ei])
            ei += 1
        else:
            break

    picked = dedupe(picked)[:TOTAL_STORIES]
    picked.sort(key=sort_key, reverse=True)
    return picked


PAGE_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>World News Digest</title>
<meta name="description" content="BBCとThe Economistから毎日19時(JST)に自動更新される世界のニュースまとめ">
<style>
  :root {
    --bg: #0f1115; --card-bg: #171a21; --border: #262b36;
    --text: #e7e9ee; --text-dim: #9aa1b1; --accent: #4f8dfd;
    --bbc: #bb1919; --economist: #e3120b;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f4f5f7; --card-bg: #ffffff; --border: #e2e4ea;
      --text: #1a1d24; --text-dim: #5b6373; --accent: #2563eb;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Sans",
      "Noto Sans JP", Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.6;
  }
  header { max-width: 760px; margin: 0 auto; padding: 40px 20px 20px; }
  h1 { font-size: 1.7rem; margin: 0 0 6px; letter-spacing: -0.02em; }
  .subtitle { color: var(--text-dim); font-size: 0.95rem; margin: 0 0 4px; }
  .updated {
    color: var(--text-dim); font-size: 0.85rem; margin: 14px 0 0;
    padding-top: 14px; border-top: 1px solid var(--border);
  }
  main { max-width: 760px; margin: 0 auto; padding: 0 20px 60px; }
  .story {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px 22px; margin-bottom: 16px;
  }
  .story-meta { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .badge {
    font-size: 0.7rem; font-weight: 700; letter-spacing: 0.03em;
    padding: 3px 9px; border-radius: 999px; color: #fff;
  }
  .badge.bbc { background: var(--bbc); }
  .badge.economist { background: var(--economist); }
  .story-time { color: var(--text-dim); font-size: 0.78rem; }
  .story h2 { font-size: 1.08rem; margin: 0 0 8px; line-height: 1.4; }
  .story h2 a { color: var(--text); text-decoration: none; }
  .story h2 a:hover { color: var(--accent); text-decoration: underline; }
  .story p { margin: 0; color: var(--text-dim); font-size: 0.92rem; }
  footer {
    max-width: 760px; margin: 0 auto; padding: 0 20px 50px;
    color: var(--text-dim); font-size: 0.8rem;
  }
  footer a { color: var(--accent); }
  .empty-note { color: var(--text-dim); font-size: 0.9rem; }
</style>
</head>
<body>
<header>
  <h1>World News Digest</h1>
  <p class="subtitle">BBC と The Economist から、世界の主要ニュースを毎日19:00(JST)に自動更新</p>
  <p class="updated">最終更新: {{ generated_at }} (JST)</p>
</header>
<main>
  {% for s in stories %}
  <article class="story">
    <div class="story-meta">
      <span class="badge {{ 'bbc' if s.source == 'BBC' else 'economist' }}">{{ s.source }}</span>
      {% if s.published_jst %}<span class="story-time">{{ s.published_jst }}</span>{% endif %}
    </div>
    <h2><a href="{{ s.link }}" target="_blank" rel="noopener">{{ s.title }}</a></h2>
    {% if s.summary %}<p>{{ s.summary }}</p>{% endif %}
  </article>
  {% else %}
  <p class="empty-note">現在ニュースを取得できませんでした。次回の自動更新をお待ちください。</p>
  {% endfor %}
</main>
<footer>
  このページは GitHub Actions により毎日19:00(JST)に自動生成されています。見出しは原文(英語)のままです。
  出典: <a href="https://www.bbc.com/news/world" target="_blank" rel="noopener">BBC News</a>,
  <a href="https://www.economist.com/international" target="_blank" rel="noopener">The Economist</a>
</footer>
</body>
</html>
"""
)


def render(stories):
    for s in stories:
        s["published_jst"] = (
            s["published"].astimezone(JST).strftime("%m/%d %H:%M") if s["published"] else None
        )
    generated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    return PAGE_TEMPLATE.render(stories=stories, generated_at=generated_at)


def main():
    stories = collect_stories()
    print(f"Total stories selected: {len(stories)}")

    repo_root = Path(__file__).resolve().parent.parent
    out_path = repo_root / "index.html"

    if not stories:
        print("No stories fetched - leaving existing index.html untouched.")
        return

    out_path.write_text(render(stories), encoding="utf-8")
    (repo_root / ".nojekyll").touch()
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
