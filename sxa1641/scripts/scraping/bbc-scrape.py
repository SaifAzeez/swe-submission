#!/usr/bin/env python3
"""
bbc_scrape.py

BBC-only URL discovery via BBC XML Sitemaps (NOT RSS).
Outputs bbc-output.csv with up to 1000 Israel/Palestine-related BBC News article URLs.

Requirements:
- Python 3.10+
- stdlib + requests only
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import random
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

import requests


# -----------------------------
# Config
# -----------------------------

OUTPUT_CSV = "bbc-output-2.csv"
MAX_OUTPUT_ROWS = 2000

SOURCE_NAME = "BBC"
SOURCE_DOMAIN = "bbc.com"
USER_AGENT = "bbc-sitemap-collector/1.1"

SITEMAP_SEEDS = [
    "https://www.bbc.com/sitemaps/https-index-com-news.xml",
    "https://www.bbc.com/sitemaps/https-index-com-archive.xml",
]

MIDDLE_EAST_PATHS = [
    "/news/world/middle-east",
    "/news/world-middle-east",
]

MAX_SITEMAP_DEPTH = 2

EXCLUDED_PATH_SUBSTRINGS = [
    "/live/",
    "/video/",
    "/topics/",
    "/av/",
    "/sounds/",
    "/newsround/",
]

REQUIRE_NEWS_PATH_PREFIXES = ("/news/",)

KEYWORDS = [
    "israel",
    "palestine",
    "gaza",
    "hamas",
    "west-bank",
    "jerusalem",
    "ceasefire",
    "idf",
]

MIN_DELAY_S = 0.35
MAX_DELAY_S = 0.85

TIMEOUT_S = 20
MAX_RETRIES = 4
BACKOFF_BASE_S = 0.8

MIN_DATE = "2023-10-07"

# Title substrings that indicate non-article pages (case-insensitive)
EXCLUDED_TITLE_SUBSTRINGS = [
    "media guide",
    "country profile",
    "at a glance",
    "in pictures",
    "quiz:",
]


# -----------------------------
# Helpers
# -----------------------------

def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()

def sleep_polite() -> None:
    time.sleep(random.uniform(MIN_DELAY_S, MAX_DELAY_S))

def canonicalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    parsed = urllib.parse.urlsplit(url)
    scheme = "https"
    netloc = (parsed.netloc or "").lower()
    path = parsed.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urllib.parse.urlunsplit((scheme, netloc, path, "", ""))

def url_path_lower(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).path.lower()
    except Exception:
        return ""

def is_bbc_domain(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return False
    return host.endswith("bbc.co.uk") or host.endswith("bbc.com")

def looks_like_news_article(url: str) -> bool:
    if not is_bbc_domain(url):
        return False
    path = url_path_lower(url)
    if not any(path.startswith(pfx) for pfx in REQUIRE_NEWS_PATH_PREFIXES):
        return False
    if path.rstrip("/") == "/news":
        return False
    for bad in EXCLUDED_PATH_SUBSTRINGS:
        if bad in path:
            return False
    return True

def keyword_match_in_path(url: str) -> bool:
    path = url_path_lower(url).replace("_", "-")
    if any(path.startswith(p) for p in MIDDLE_EAST_PATHS):
        return True
    return any(kw in path for kw in KEYWORDS)

def extract_article_id(url: str) -> str:
    path = urllib.parse.urlsplit(url).path
    m = re.search(r"-(\d{6,})$", path)
    if m:
        return m.group(1)
    m = re.search(r"/(\d{6,})$", path)
    if m:
        return m.group(1)
    return ""

def parse_iso_date(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s
    return ""

def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag

def maybe_decompress(content: bytes, url: str, headers: Dict[str, str]) -> bytes:
    enc = (headers.get("Content-Encoding") or "").lower()
    ctype = (headers.get("Content-Type") or "").lower()
    if url.lower().endswith(".gz") or "gzip" in enc or "application/x-gzip" in ctype:
        try:
            return gzip.decompress(content)
        except OSError:
            return content
    return content

def is_excluded_page(session: requests.Session, url: str) -> bool:
    """Fetch the page and check the <title> for non-article indicators."""
    try:
        resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S)
        resp.raise_for_status()
        m = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.IGNORECASE | re.DOTALL)
        if m:
            title = m.group(1).lower()
            if any(excl in title for excl in EXCLUDED_TITLE_SUBSTRINGS):
                sys.stderr.write(f"[filter] excluded by title: {url}\n")
                return True
    except Exception as e:
        sys.stderr.write(f"[warn] could not verify page title for {url} ({e})\n")
    return False

@dataclass
class SitemapEntry:
    loc: str
    lastmod: str

def fetch_url(session: requests.Session, url: str) -> Optional[bytes]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/xml,text/xml,*/*;q=0.8",
    }
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, headers=headers, timeout=TIMEOUT_S)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {resp.status_code} for {url}")
            resp.raise_for_status()
            data = maybe_decompress(resp.content, url, dict(resp.headers))
            return data
        except (requests.RequestException, requests.HTTPError) as e:
            last_err = e
            backoff = (BACKOFF_BASE_S ** attempt) + random.uniform(0.0, 0.4)
            time.sleep(backoff)
    sys.stderr.write(f"[warn] failed fetch after retries: {url} ({last_err})\n")
    return None

def parse_sitemap_xml(xml_bytes: bytes) -> Tuple[str, List[SitemapEntry]]:
    try:
        xml_bytes = xml_bytes.lstrip()
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return ("unknown", [])

    root_tag = strip_ns(root.tag).lower()
    entries: List[SitemapEntry] = []

    if root_tag == "sitemapindex":
        for sm in list(root):
            if strip_ns(sm.tag).lower() != "sitemap":
                continue
            loc = ""
            lastmod = ""
            for child in list(sm):
                t = strip_ns(child.tag).lower()
                if t == "loc" and child.text:
                    loc = child.text.strip()
                elif t == "lastmod" and child.text:
                    lastmod = parse_iso_date(child.text)
            if loc:
                entries.append(SitemapEntry(loc=loc, lastmod=lastmod))
        return ("sitemapindex", entries)

    if root_tag == "urlset":
        for u in list(root):
            if strip_ns(u.tag).lower() != "url":
                continue
            loc = ""
            lastmod = ""
            for child in list(u):
                t = strip_ns(child.tag).lower()
                if t == "loc" and child.text:
                    loc = child.text.strip()
                elif t == "lastmod" and child.text:
                    lastmod = parse_iso_date(child.text)
            if loc:
                entries.append(SitemapEntry(loc=loc, lastmod=lastmod))
        return ("urlset", entries)

    return ("unknown", [])

def load_existing(output_csv: str) -> Set[str]:
    existing: Set[str] = set()
    try:
        with open(output_csv, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cu = canonicalize_url(row.get("url", "") or "")
                if cu:
                    existing.add(cu)
    except FileNotFoundError:
        return set()
    except Exception as e:
        sys.stderr.write(f"[warn] could not load existing CSV ({e}); starting fresh.\n")
    return existing

def ensure_csv_header(path: str) -> None:
    try:
        with open(path, "r", newline="", encoding="utf-8") as _:
            return
    except FileNotFoundError:
        pass
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "source_name", "source_domain", "url", "published_at",
            "retrieved_at", "article_id", "discovery_method", "sitemap_url",
        ])

def append_rows(path: str, rows: List[Dict[str, str]]) -> None:
    if not rows:
        return
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "source_name", "source_domain", "url", "published_at",
            "retrieved_at", "article_id", "discovery_method", "sitemap_url",
        ])
        for r in rows:
            writer.writerow(r)


# -----------------------------
# Collector
# -----------------------------

@dataclass
class Stats:
    scanned_sitemaps: int = 0
    scanned_urls: int = 0
    kept: int = 0
    skipped_dup: int = 0
    skipped_offtopic: int = 0
    skipped_excluded: int = 0
    skipped_non_news: int = 0

def collect_from_sitemap(
    session: requests.Session,
    sitemap_url: str,
    depth: int,
    max_depth: int,
    seen_sitemaps: Set[str],
    existing_urls: Set[str],
    out_rows: List[Dict[str, str]],
    stats: Stats,
) -> None:
    if stats.kept >= MAX_OUTPUT_ROWS or depth > max_depth:
        return

    sm_canon = canonicalize_url(sitemap_url)
    if not sm_canon or sm_canon in seen_sitemaps:
        return
    seen_sitemaps.add(sm_canon)

    sleep_polite()
    xml_bytes = fetch_url(session, sitemap_url)
    stats.scanned_sitemaps += 1
    if not xml_bytes:
        return

    root_type, entries = parse_sitemap_xml(xml_bytes)
    if root_type == "unknown":
        return

    if root_type == "sitemapindex":
        for e in entries:
            if stats.kept >= MAX_OUTPUT_ROWS:
                break
            if e.loc:
                collect_from_sitemap(
                    session=session,
                    sitemap_url=e.loc,
                    depth=depth + 1,
                    max_depth=max_depth,
                    seen_sitemaps=seen_sitemaps,
                    existing_urls=existing_urls,
                    out_rows=out_rows,
                    stats=stats,
                )
        return

    # urlset
    retrieved_at = now_utc_iso()
    for e in entries:
        if stats.kept >= MAX_OUTPUT_ROWS:
            break

        loc = e.loc
        if not loc:
            continue

        stats.scanned_urls += 1
        cu = canonicalize_url(loc)
        if not cu:
            stats.skipped_non_news += 1
            continue

        if cu in existing_urls:
            stats.skipped_dup += 1
            continue

        if not looks_like_news_article(cu):
            stats.skipped_non_news += 1
            continue

        path_l = url_path_lower(cu)
        if any(bad in path_l for bad in EXCLUDED_PATH_SUBSTRINGS):
            stats.skipped_excluded += 1
            continue

        if e.lastmod and e.lastmod < MIN_DATE:
            stats.skipped_offtopic += 1
            continue

        if not keyword_match_in_path(cu):
            stats.skipped_offtopic += 1
            continue

        # Fetch page to verify it's a real news article (not a guide/profile)
        if is_excluded_page(session, cu):
            stats.skipped_excluded += 1
            continue

        out_rows.append({
            "source_name": SOURCE_NAME,
            "source_domain": SOURCE_DOMAIN,
            "url": cu,
            "published_at": e.lastmod or "",
            "retrieved_at": retrieved_at,
            "article_id": extract_article_id(cu),
            "discovery_method": "bbc_xml_sitemap",
            "sitemap_url": sitemap_url,
        })
        existing_urls.add(cu)
        stats.kept += 1


def main() -> int:
    ensure_csv_header(OUTPUT_CSV)

    existing_urls = load_existing(OUTPUT_CSV)
    start_existing = len(existing_urls)

    stats = Stats()
    seen_sitemaps: Set[str] = set()
    buffer_rows: List[Dict[str, str]] = []

    with requests.Session() as session:
        for seed in SITEMAP_SEEDS:
            if stats.kept >= MAX_OUTPUT_ROWS:
                break
            collect_from_sitemap(
                session=session,
                sitemap_url=seed,
                depth=0,
                max_depth=MAX_SITEMAP_DEPTH,
                seen_sitemaps=seen_sitemaps,
                existing_urls=existing_urls,
                out_rows=buffer_rows,
                stats=stats,
            )
            if len(buffer_rows) >= 100:
                append_rows(OUTPUT_CSV, buffer_rows)
                buffer_rows.clear()

    append_rows(OUTPUT_CSV, buffer_rows)
    buffer_rows.clear()

    end_existing = len(existing_urls)
    newly_added = end_existing - start_existing

    sys.stderr.write(
        "\n".join([
            "=== BBC Sitemap Collector Summary ===",
            f"Sitemaps fetched/parsing attempts: {stats.scanned_sitemaps}",
            f"URLs scanned (from urlsets):      {stats.scanned_urls}",
            f"Kept (written this run):          {newly_added}",
            f"Skipped (duplicate/resume):       {stats.skipped_dup}",
            f"Skipped (excluded path):          {stats.skipped_excluded}",
            f"Skipped (offtopic keywords):      {stats.skipped_offtopic}",
            f"Skipped (non-news/non-article):   {stats.skipped_non_news}",
            f"Total kept overall in CSV:        {end_existing}",
            "====================================\n",
        ]) + "\n"
    )

    # Write JSON output
    json_rows = []
    try:
        with open(OUTPUT_CSV, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                json_rows.append(row)
    except Exception as e:
        sys.stderr.write(f"[warn] could not read CSV for JSON export ({e})\n")

    json_path = OUTPUT_CSV.replace(".csv", ".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(json_rows, f, indent=2, ensure_ascii=False)

    sys.stderr.write(f"JSON written to {json_path}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())