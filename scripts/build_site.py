#!/usr/bin/env python3
"""World News Daily - site generator (English excerpt + Japanese translation)."""

import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from jinja2 import Template

JST = ZoneInfo("Asia/Tokyo")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/rss+xml,application/xml,*/*",
    "Accept-Language": "en-GB,en;q=0.9",
}

REQUEST_TIMEOUT = 20

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

MAX_PARAGRAPHS = 4
MAX_EXCERPT_CHARS = 900

TAG_RE = re.compile(r"<[^>]+>")

BOILERPLATE_MARKERS = (
    "sign up for",
    "newsletter",
    "this video can not be played",
    "getty images",
    "follow bbc",
    "bbc is not responsible",
    "read more about",
    "advertisement",
    "cookie",
    "subscribe to",
    "all rights reserved",
)


def clean_text(raw):
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


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

    stories = []
    for item in root.findall("./channel/item"):
        title = html.unescape(_text(item.find("title")))
        link = _text(item.find("link"))
        if not title or not link:
            continue
        summary = clean_text(_text(item.find("description")))
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
                "feed_summary": summary,
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


def extract_paragraphs(html_text):
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript", "figure", "aside", "nav", "header", "footer"]):
        tag.decompose()

    container = soup.find("article") or soup.find("main") or soup

    paragraphs = []
    for p in container.find_all("p"):
        text = " ".join(p.get_text(" ", strip=True).split())
        if len(text) < 40:
            continue
        low = text.lower()
        if any(marker in low for marker in BOILERPLATE_MARKERS):
            continue
        if text in paragraphs:
            continue
        paragraphs.append(text)
    return paragraphs


def fetch_article_excerpt(story):
    if story["source"] != "BBC":
        return [story["feed_summary"]] if story["feed_summary"] else []

    try:
        resp = requests.get(story["link"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  ! article fetch failed ({story['link']}): {exc}", file=sys.stderr)
        return [story["feed_summary"]] if story["feed_summary"] else []

    paragraphs = extract_paragraphs(resp.text)
    if not paragraphs:
        print(f"  ! no body paragraphs found for {story['link']}", file=sys.stderr)
        return [story["feed_summary"]] if story["feed_summary"] else []

    kept = []
    total = 0
    for para in paragraphs[:MAX_PARAGRAPHS]:
        if total + len(para) > MAX_EXCERPT_CHARS and kept:
            break
        kept.append(para)
        total += len(para)
    return kept


TRANSLATE_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
TRANSLATE_CHUNK = 1200


def _translate_chunk(text):
    params = {"client": "gtx", "sl": "en", "tl": "ja", "dt": "t", "q": text}
    resp = requests.get(
        TRANSLATE_ENDPOINT,
        params=params,
        headers={"User-Agent": HEADERS["User-Agent"]},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    data = json.loads(resp.text)
    return "".join(segment[0] for segment in data[0] if segment and segment[0])


def translate_to_ja(text):
    text = (text or "").strip()
    if not text:
        return None

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= TRANSLATE_CHUNK:
            chunks.append(remaining)
            break
        split_at = remaining.rfind(" ", 0, TRANSLATE_CHUNK)
        if split_at <= 0:
            split_at = TRANSLATE_CHUNK
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip()

    out = []
    for chunk in chunks:
        for attempt in range(3):
            try:
                out.append(_translate_chunk(chunk))
                break
            except Exception as exc:
                if attempt == 2:
                    print(f"  ! translation failed: {exc}", file=sys.stderr)
                    return None
                time.sleep(1.5 * (attempt + 1))
        time.sleep(0.4)
    return "".join(out).strip() or None


def enrich(stories):
    for i, story in enumerate(stories, 1):
        print(f"[{i}/{len(stories)}] {story['title'][:70]}")
        paragraphs = fetch_article_excerpt(story)
        story["excerpt_en"] = paragraphs

        story["title_ja"] = translate_to_ja(story["title"])

        translated = []
        for para in paragraphs:
            ja = translate_to_ja(para)
            if ja is None:
                translated = []
                break
            translated.append(ja)
        story["excerpt_ja"] = translated
        time.sleep(0.5)
    return stories


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
    --text: #e7e9ee; --text-dim: #9aa1b1; --text-faint: #6f7789;
    --accent: #4f8dfd; --bbc: #bb1919; --economist: #e3120b;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f4f5f7; --card-bg: #ffffff; --border: #e2e4ea;
      --text: #1a1d24; --text-dim: #4b5464; --text-faint: #808895;
      --accent: #2563eb;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Sans",
      "Noto Sans JP", Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.75;
  }
  header { max-width: 720px; margin: 0 auto; padding: 40px 20px 20px; }
  h1 { font-size: 1.7rem; margin: 0 0 6px; letter-spacing: -0.02em; }
  .subtitle { color: var(--text-dim); font-size: 0.95rem; margin: 0; }
  .updated {
    color: var(--text-faint); font-size: 0.85rem; margin: 14px 0 0;
    padding-top: 14px; border-top: 1px solid var(--border);
  }
  main { max-width: 720px; margin: 0 auto; padding: 0 20px 60px; }
  .story {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 12px; padding: 22px 24px; margin-bottom: 18px;
  }
  .story-meta { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
  .badge {
    font-size: 0.7rem; font-weight: 700; letter-spacing: 0.03em;
    padding: 3px 9px; border-radius: 999px; color: #fff;
  }
  .badge.bbc { background: var(--bbc); }
  .badge.economist { background: var(--economist); }
  .story-time { color: var(--text-faint); font-size: 0.78rem; }
  .title-ja { font-size: 1.15rem; font-weight: 700; margin: 0 0 4px; line-height: 1.5; }
  .title-en {
    font-size: 0.85rem; color: var(--text-faint); margin: 0 0 14px; line-height: 1.5;
  }
  .title-en a { color: inherit; text-decoration: none; }
  .title-en a:hover { color: var(--accent); text-decoration: underline; }
  .body-ja p { margin: 0 0 10px; color: var(--text-dim); font-size: 0.95rem; }
  .body-ja p:last-child { margin-bottom: 0; }
  details { margin-top: 14px; border-top: 1px dashed var(--border); padding-top: 12px; }
  summary {
    cursor: pointer; color: var(--text-faint); font-size: 0.8rem;
    list-style: none; user-select: none;
  }
  summary::-webkit-details-marker { display: none; }
  summary::before { content: "▸ "; }
  details[open] summary::before { content: "▾ "; }
  details p {
    margin: 10px 0 0; color: var(--text-faint); font-size: 0.86rem; line-height: 1.7;
  }
  .read-more { display: inline-block; margin-top: 12px; font-size: 0.82rem; }
  .read-more a { color: var(--accent); text-decoration: none; }
  .read-more a:hover { text-decoration: underline; }
  footer {
    max-width: 720px; margin: 0 auto; padding: 0 20px 50px;
    color: var(--text-faint); font-size: 0.8rem; line-height: 1.7;
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

    {% if s.title_ja %}
    <p class="title-ja">{{ s.title_ja }}</p>
    <p class="title-en"><a href="{{ s.link }}" target="_blank" rel="noopener">{{ s.title }}</a></p>
    {% else %}
    <p class="title-ja"><a href="{{ s.link }}" target="_blank" rel="noopener"
      style="color:inherit;text-decoration:none">{{ s.title }}</a></p>
    {% endif %}

    {% if s.excerpt_ja %}
    <div class="body-ja">
      {% for para in s.excerpt_ja %}<p>{{ para }}</p>{% endfor %}
    </div>
    {% elif s.excerpt_en %}
    <div class="body-ja">
      {% for para in s.excerpt_en %}<p>{{ para }}</p>{% endfor %}
    </div>
    {% endif %}

    {% if s.excerpt_ja and s.excerpt_en %}
    <details>
      <summary>原文(英語)を表示</summary>
      {% for para in s.excerpt_en %}<p>{{ para }}</p>{% endfor %}
    </details>
    {% endif %}

    <span class="read-more"><a href="{{ s.link }}" target="_blank" rel="noopener">
      記事全文を読む →</a></span>
  </article>
  {% else %}
  <p class="empty-note">現在ニュースを取得できませんでした。次回の自動更新をお待ちください。</p>
  {% endfor %}
</main>
<footer>
  このページは GitHub Actions により毎日19:00(JST)に自動生成されています。
  日本語部分は記事冒頭を機械翻訳したもので、正確性は原文をご確認ください。<br>
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

    enrich(stories)

    out_path.write_text(render(stories), encoding="utf-8")
    (repo_root / ".nojekyll").touch()
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
