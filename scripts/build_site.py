#!/usr/bin/env python3
"""World News Daily - multi-source digest grouped by region, with Japanese translation.

Sources: BBC, Al Jazeera, The Guardian, Deutsche Welle, France 24, NPR.
"""

import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
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

# (display name, css class, feed url)
FEEDS = [
    ("BBC", "bbc", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Al Jazeera", "aj", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("The Guardian", "guardian", "https://www.theguardian.com/world/rss"),
    ("DW", "dw", "https://rss.dw.com/rdf/rss-en-world"),
    ("France 24", "f24", "https://www.france24.com/en/rss"),
    ("NPR", "npr", "https://feeds.npr.org/1004/rss.xml"),
]

PER_SOURCE_LIMIT = 12          # newest N entries considered per source
PER_REGION_MAX = 4             # stories shown per region section
TOTAL_MAX = 18                 # overall cap
MAX_AGE_HOURS = 48             # skip stories older than this (if dated)

MAX_PARAGRAPHS = 5
MAX_EXCERPT_CHARS = 1200
LEAD_PARAGRAPHS = 2            # paragraphs visible before the fold

TAG_RE = re.compile(r"<[^>]+>")

BOILERPLATE_MARKERS = (
    "sign up for",
    "newsletter",
    "this video can not be played",
    "getty images",
    "follow bbc",
    "bbc is not responsible",
    "advertisement",
    "cookie",
    "subscribe to",
    "all rights reserved",
    "additional reporting by",
    "continue reading",
    "click here",
    "download the app",
    "©",
)

# ------------------------------------------------------------- regions ----

REGIONS = [
    ("europe", "欧州・ロシア"),
    ("mideast", "中東"),
    ("asia", "アジア・太平洋"),
    ("americas", "米州"),
    ("africa", "アフリカ"),
    ("other", "その他・国際"),
]

REGION_KEYWORDS = {
    "europe": (
        "ukraine", "russia", "russian", "putin", "kyiv", "moscow", "kremlin",
        "britain", "british", "england", "london", "scotland", "wales",
        "france", "french", "paris", "germany", "german", "berlin",
        "italy", "italian", "rome", "spain", "spanish", "madrid",
        "poland", "polish", "netherlands", "dutch", "belgium", "brussels",
        "greece", "greek", "sweden", "swedish", "norway", "norwegian",
        "finland", "denmark", "danish", "ireland", "irish", "portugal",
        "austria", "hungary", "romania", "bulgaria", "czech", "slovakia",
        "serbia", "croatia", "ukraine's", "europe", "european", "nato",
        "eu", "danube", "switzerland", "swiss", "iceland", "moldova",
        "belarus", "baltic", "balkan", "vatican",
    ),
    "mideast": (
        "israel", "israeli", "gaza", "palestinian", "palestinians", "west bank",
        "jerusalem", "tel aviv", "iran", "iranian", "tehran", "iraq", "iraqi",
        "syria", "syrian", "damascus", "lebanon", "lebanese", "beirut",
        "hezbollah", "hamas", "saudi", "riyadh", "yemen", "houthi", "houthis",
        "qatar", "doha", "uae", "emirates", "dubai", "abu dhabi", "kuwait",
        "bahrain", "oman", "jordan", "jordanian", "middle east", "turkey",
        "turkish", "istanbul", "ankara", "erdogan",
    ),
    "asia": (
        "china", "chinese", "beijing", "shanghai", "xi jinping", "japan",
        "japanese", "tokyo", "korea", "korean", "seoul", "pyongyang",
        "india", "indian", "delhi", "modi", "pakistan", "pakistani",
        "indonesia", "indonesian", "jakarta", "australia", "australian",
        "philippines", "filipino", "manila", "taiwan", "taiwanese",
        "vietnam", "vietnamese", "thailand", "thai", "bangkok", "myanmar",
        "malaysia", "singapore", "hong kong", "new zealand", "afghanistan",
        "afghan", "taliban", "bangladesh", "sri lanka", "nepal", "kashmir",
        "mongolia", "pacific", "fiji", "papua",
    ),
    "americas": (
        "united states", "u.s.", "trump", "washington", "white house",
        "american", "america", "congress", "senate", "pentagon",
        "new york", "california", "texas", "florida", "supreme court",
        "canada", "canadian", "ottawa", "toronto", "mexico", "mexican",
        "brazil", "brazilian", "argentina", "argentine", "venezuela",
        "colombia", "colombian", "chile", "chilean", "peru", "peruvian",
        "cuba", "cuban", "haiti", "haitian", "bolivia", "ecuador",
        "guatemala", "honduras", "nicaragua", "panama", "caribbean",
        "latin america",
    ),
    "africa": (
        "africa", "african", "nigeria", "nigerian", "kenya", "kenyan",
        "ethiopia", "ethiopian", "sudan", "sudanese", "egypt", "egyptian",
        "cairo", "south africa", "congo", "congolese", "mali", "niger",
        "ghana", "somalia", "somali", "libya", "libyan", "morocco",
        "moroccan", "tunisia", "tunisian", "algeria", "algerian",
        "zimbabwe", "uganda", "ugandan", "rwanda", "rwandan", "tanzania",
        "senegal", "cameroon", "mozambique", "zambia", "botswana",
        "burkina faso", "chad", "sahel",
    ),
}

STOPWORDS = frozenset(
    "the a an and or of in on at to for with as by from is are was were be "
    "been has have had will would can could may might after over under more "
    "than into out up down new says say said tells told amid its his her "
    "their our your this that these those they them he she it we you not no "
    "but so if when where while who whom whose which what why how".split()
)


def classify_region(story):
    text = f"{story['title']} {story['feed_summary']}".lower()
    best_key, best_score = "other", 0
    for key, keywords in REGION_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if " " in kw or "." in kw:
                if kw in text:
                    score += 2
            elif re.search(rf"\b{re.escape(kw)}\b", text):
                score += 1
        if score > best_score:
            best_key, best_score = key, score
    return best_key


# --------------------------------------------------------------- feeds ----


def clean_text(raw):
    if not raw:
        return ""
    text = TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    # Guardian feeds append "Continue reading..."
    return re.sub(r"Continue reading\.*\s*$", "", text).strip()


def parse_published(raw):
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _child_text(item, *names):
    wanted = set(names)
    for el in item:
        if _local(el.tag) in wanted and el.text:
            return el.text.strip()
    return ""


def fetch_feed(source_name, css, url):
    """Parse RSS 2.0 and RDF/RSS 1.0 feeds (namespace-agnostic)."""
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
    for el in root.iter():
        if _local(el.tag) != "item":
            continue
        title = html.unescape(_child_text(el, "title"))
        link = _child_text(el, "link")
        if not title or not link:
            continue
        stories.append(
            {
                "source": source_name,
                "css": css,
                "title": title,
                "feed_summary": clean_text(_child_text(el, "description", "summary")),
                "link": link,
                "published": parse_published(_child_text(el, "pubDate", "date")),
            }
        )
        if len(stories) >= PER_SOURCE_LIMIT:
            break
    print(f"  - {source_name}: {len(stories)} items")
    return stories


def title_tokens(title):
    """Crude stemming (5-char prefixes) so 'Russia'/'Russian' etc. match."""
    return frozenset(
        w[:5] for w in re.findall(r"[a-z']+", title.lower())
        if len(w) > 2 and w not in STOPWORDS
    )


def is_duplicate(story, kept):
    """Cross-source dedupe: same event reported by several outlets."""
    tokens = title_tokens(story["title"])
    if not tokens:
        return False
    for other in kept:
        other_tokens = other.setdefault("_tokens", title_tokens(other["title"]))
        overlap = len(tokens & other_tokens)
        smaller = min(len(tokens), len(other_tokens))
        if smaller and overlap >= 3 and overlap / smaller >= 0.5:
            return True
    return False


def collect_stories():
    all_stories = []
    print("Fetching feeds...")
    for name, css, url in FEEDS:
        all_stories.extend(fetch_feed(name, css, url))

    # Freshness filter (undated items are kept).
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    all_stories = [s for s in all_stories if s["published"] is None or s["published"] >= cutoff]

    def recency(s):
        return s["published"] or datetime.min.replace(tzinfo=timezone.utc)

    all_stories.sort(key=recency, reverse=True)

    # Classify, dedupe across sources, then pick per-region with source variety.
    by_region = {key: [] for key, _ in REGIONS}
    kept = []
    for story in all_stories:
        if is_duplicate(story, kept):
            continue
        story["region"] = classify_region(story)
        kept.append(story)
        by_region[story["region"]].append(story)

    total = 0
    sections = []
    for key, label in REGIONS:
        candidates = by_region[key]
        picked = []
        # Round-robin across sources so one outlet doesn't dominate a section.
        by_source = {}
        for s in candidates:
            by_source.setdefault(s["source"], []).append(s)
        order = sorted(by_source.values(), key=lambda lst: recency(lst[0]), reverse=True)
        while len(picked) < PER_REGION_MAX and total < TOTAL_MAX and any(order):
            progressed = False
            for lst in order:
                if lst and len(picked) < PER_REGION_MAX and total < TOTAL_MAX:
                    picked.append(lst.pop(0))
                    total += 1
                    progressed = True
            if not progressed:
                break
        picked.sort(key=recency, reverse=True)
        if picked:
            sections.append({"key": key, "label": label, "stories": picked})

    return sections


# ------------------------------------------------------ article bodies ----


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
    try:
        resp = requests.get(story["link"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        paragraphs = extract_paragraphs(resp.text)
    except requests.RequestException as exc:
        print(f"  ! article fetch failed ({story['link']}): {exc}", file=sys.stderr)
        paragraphs = []

    if not paragraphs:
        return [story["feed_summary"]] if story["feed_summary"] else []

    kept = []
    total = 0
    for para in paragraphs[:MAX_PARAGRAPHS]:
        if total + len(para) > MAX_EXCERPT_CHARS and kept:
            break
        kept.append(para)
        total += len(para)
    return kept


# --------------------------------------------------------- translation ----

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
        time.sleep(0.25)
    return "".join(out).strip() or None


def enrich(sections):
    stories = [s for sec in sections for s in sec["stories"]]
    for i, story in enumerate(stories, 1):
        print(f"[{i}/{len(stories)}] ({story['source']}) {story['title'][:60]}")
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
        story["lead_ja"] = translated[:LEAD_PARAGRAPHS]
        story["more_ja"] = translated[LEAD_PARAGRAPHS:]
        time.sleep(0.3)
    return sections


# ------------------------------------------------------------ rendering ---

PAGE_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>World News Digest</title>
<meta name="description" content="世界6メディアの主要ニュースを地域別に毎日19時(JST)自動更新">
<style>
  :root {
    --bg: #0f1115; --card-bg: #171a21; --border: #262b36;
    --text: #e7e9ee; --text-dim: #9aa1b1; --text-faint: #6f7789;
    --accent: #4f8dfd;
    --bbc: #bb1919; --aj: #d18700; --guardian: #0b4f8a;
    --dw: #008b9a; --f24: #5b48c2; --npr: #b0304d;
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f4f5f7; --card-bg: #ffffff; --border: #e2e4ea;
      --text: #1a1d24; --text-dim: #4b5464; --text-faint: #808895;
      --accent: #2563eb;
    }
  }
  * { box-sizing: border-box; }
  html { scroll-behavior: smooth; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Hiragino Sans",
      "Noto Sans JP", Roboto, sans-serif;
    background: var(--bg); color: var(--text); line-height: 1.8;
  }
  header { max-width: 720px; margin: 0 auto; padding: 40px 20px 8px; }
  h1 { font-size: 1.7rem; margin: 0 0 6px; letter-spacing: -0.02em; }
  .subtitle { color: var(--text-dim); font-size: 0.92rem; margin: 0; }
  .updated { color: var(--text-faint); font-size: 0.82rem; margin: 10px 0 0; }
  .toc {
    max-width: 720px; margin: 0 auto; padding: 14px 20px 4px;
    display: flex; flex-wrap: wrap; gap: 8px;
    position: sticky; top: 0; background: var(--bg); z-index: 5;
    border-bottom: 1px solid var(--border);
  }
  .toc a {
    display: inline-block; padding: 5px 13px; border-radius: 999px;
    border: 1px solid var(--border); background: var(--card-bg);
    color: var(--text-dim); font-size: 0.82rem; text-decoration: none;
    margin-bottom: 10px;
  }
  .toc a:hover { color: var(--accent); border-color: var(--accent); }
  .toc .count { color: var(--text-faint); font-size: 0.75rem; }
  main { max-width: 720px; margin: 0 auto; padding: 0 20px 60px; }
  .region-head {
    font-size: 1.15rem; font-weight: 700; margin: 34px 0 14px;
    padding-left: 12px; border-left: 4px solid var(--accent);
    scroll-margin-top: 70px;
  }
  .story {
    background: var(--card-bg); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px 22px; margin-bottom: 14px;
  }
  .story-meta { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
  .badge {
    font-size: 0.68rem; font-weight: 700; letter-spacing: 0.03em;
    padding: 3px 9px; border-radius: 999px; color: #fff;
  }
  .badge.bbc { background: var(--bbc); }
  .badge.aj { background: var(--aj); }
  .badge.guardian { background: var(--guardian); }
  .badge.dw { background: var(--dw); }
  .badge.f24 { background: var(--f24); }
  .badge.npr { background: var(--npr); }
  .story-time { color: var(--text-faint); font-size: 0.76rem; }
  .title-ja { font-size: 1.08rem; font-weight: 700; margin: 0 0 3px; line-height: 1.55; }
  .title-en {
    font-size: 0.8rem; color: var(--text-faint); margin: 0 0 12px; line-height: 1.5;
  }
  .title-en a { color: inherit; text-decoration: none; }
  .title-en a:hover { color: var(--accent); text-decoration: underline; }
  .body-ja p { margin: 0 0 10px; color: var(--text-dim); font-size: 0.93rem; }
  .body-ja p:last-child { margin-bottom: 0; }
  details { margin-top: 12px; border-top: 1px dashed var(--border); padding-top: 10px; }
  summary {
    cursor: pointer; color: var(--accent); font-size: 0.8rem;
    list-style: none; user-select: none;
  }
  summary::-webkit-details-marker { display: none; }
  summary::before { content: "▸ "; }
  details[open] summary::before { content: "▾ "; }
  details .more-ja p { margin: 10px 0 0; color: var(--text-dim); font-size: 0.93rem; }
  details .orig-label {
    margin: 14px 0 0; color: var(--text-faint); font-size: 0.75rem;
    text-transform: uppercase; letter-spacing: 0.05em;
  }
  details .orig p {
    margin: 8px 0 0; color: var(--text-faint); font-size: 0.84rem; line-height: 1.65;
  }
  .read-more { display: inline-block; margin-top: 10px; font-size: 0.8rem; }
  .read-more a { color: var(--accent); text-decoration: none; }
  .read-more a:hover { text-decoration: underline; }
  footer {
    max-width: 720px; margin: 0 auto; padding: 0 20px 50px;
    color: var(--text-faint); font-size: 0.78rem; line-height: 1.7;
  }
  footer a { color: var(--accent); }
  .empty-note { color: var(--text-dim); font-size: 0.9rem; padding: 20px; }
</style>
</head>
<body>
<header>
  <h1>World News Digest</h1>
  <p class="subtitle">BBC / Al Jazeera / The Guardian / DW / France 24 / NPR — 地域別・毎日19:00(JST)自動更新</p>
  <p class="updated">最終更新: {{ generated_at }} (JST)</p>
</header>

{% if sections %}
<nav class="toc">
  {% for sec in sections %}
  <a href="#{{ sec.key }}">{{ sec.label }} <span class="count">{{ sec.stories|length }}</span></a>
  {% endfor %}
</nav>
{% endif %}

<main>
  {% for sec in sections %}
  <h2 class="region-head" id="{{ sec.key }}">{{ sec.label }}</h2>
  {% for s in sec.stories %}
  <article class="story">
    <div class="story-meta">
      <span class="badge {{ s.css }}">{{ s.source }}</span>
      {% if s.published_jst %}<span class="story-time">{{ s.published_jst }}</span>{% endif %}
    </div>

    {% if s.title_ja %}
    <p class="title-ja">{{ s.title_ja }}</p>
    <p class="title-en"><a href="{{ s.link }}" target="_blank" rel="noopener">{{ s.title }}</a></p>
    {% else %}
    <p class="title-ja"><a href="{{ s.link }}" target="_blank" rel="noopener"
      style="color:inherit;text-decoration:none">{{ s.title }}</a></p>
    {% endif %}

    {% if s.lead_ja %}
    <div class="body-ja">
      {% for para in s.lead_ja %}<p>{{ para }}</p>{% endfor %}
    </div>
    {% elif s.excerpt_en %}
    <div class="body-ja">
      {% for para in s.excerpt_en[:2] %}<p>{{ para }}</p>{% endfor %}
    </div>
    {% endif %}

    {% if s.more_ja or (s.lead_ja and s.excerpt_en) %}
    <details>
      <summary>続きを読む</summary>
      {% if s.more_ja %}
      <div class="more-ja">
        {% for para in s.more_ja %}<p>{{ para }}</p>{% endfor %}
      </div>
      {% endif %}
      {% if s.excerpt_en %}
      <p class="orig-label">Original (English)</p>
      <div class="orig">
        {% for para in s.excerpt_en %}<p>{{ para }}</p>{% endfor %}
      </div>
      {% endif %}
    </details>
    {% endif %}

    <span class="read-more"><a href="{{ s.link }}" target="_blank" rel="noopener">
      記事全文を読む({{ s.source }}) →</a></span>
  </article>
  {% endfor %}
  {% else %}
  <p class="empty-note">現在ニュースを取得できませんでした。次回の自動更新をお待ちください。</p>
  {% endfor %}
</main>
<footer>
  このページは GitHub Actions により毎日19:00(JST)に自動生成されています。
  日本語部分は記事冒頭を機械翻訳したもので、正確性は原文をご確認ください。<br>
  出典:
  <a href="https://www.bbc.com/news/world" target="_blank" rel="noopener">BBC</a> /
  <a href="https://www.aljazeera.com/" target="_blank" rel="noopener">Al Jazeera</a> /
  <a href="https://www.theguardian.com/world" target="_blank" rel="noopener">The Guardian</a> /
  <a href="https://www.dw.com/en/" target="_blank" rel="noopener">DW</a> /
  <a href="https://www.france24.com/en/" target="_blank" rel="noopener">France 24</a> /
  <a href="https://www.npr.org/sections/world/" target="_blank" rel="noopener">NPR</a>
</footer>
</body>
</html>
"""
)


def render(sections):
    for sec in sections:
        for s in sec["stories"]:
            s["published_jst"] = (
                s["published"].astimezone(JST).strftime("%m/%d %H:%M") if s["published"] else None
            )
    generated_at = datetime.now(JST).strftime("%Y-%m-%d %H:%M")
    return PAGE_TEMPLATE.render(sections=sections, generated_at=generated_at)


def main():
    sections = collect_stories()
    n = sum(len(sec["stories"]) for sec in sections)
    print(f"Total stories selected: {n} in {len(sections)} regions")

    repo_root = Path(__file__).resolve().parent.parent
    out_path = repo_root / "index.html"

    if not n:
        print("No stories fetched - leaving existing index.html untouched.")
        return

    enrich(sections)

    out_path.write_text(render(sections), encoding="utf-8")
    (repo_root / ".nojekyll").touch()
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
