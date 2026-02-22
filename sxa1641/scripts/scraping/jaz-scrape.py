#!/usr/bin/env python3
"""
aljazeera_sitemap_collector.py

Al Jazeera-only URL discovery using Al Jazeera XML sitemaps (NOT RSS).
Outputs aljazeera-output.csv with up to N Israel/Palestine-related Al Jazeera article URLs.

Extras in this version:
- Live progress line that updates in-place in the shell (single-line status)
- Periodic milestones every N items

Requirements:
- Python 3.10+
- stdlib + requests only
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import random
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

import requests


# -----------------------------
# Config
# -----------------------------

OUTPUT_CSV = "aljazeera-output.csv"
MAX_OUTPUT_ROWS = 2000

SOURCE_NAME = "Al Jazeera"
SOURCE_DOMAIN = "aljazeera.com"
DISCOVERY_METHOD = "aj_xml_sitemap"

SITEMAP_SEEDS = [
    "https://www.aljazeera.com/news-sitemap.xml",
    "https://www.aljazeera.com/sitemaps/article-new.xml",
    "https://www.aljazeera.com/sitemaps/article-archive.xml",
    "https://www.aljazeera.com/sitemap.xml",
]

MAX_SITEMAP_DEPTH = 2

MIN_DELAY_S = 0.35
MAX_DELAY_S = 0.85

TIMEOUT_S = 20
MAX_RETRIES = 4
BACKOFF_BASE_S = 0.8

USER_AGENT = "aljazeera-sitemap-collector/1.1"

REQUIRE_PATH_PREFIXES = ("/news/",)

EXCLUDED_PATH_SUBSTRINGS = [
    "/video/",
    "/liveblog/",
    "/program/",
    "/podcasts/",
    "/gallery/",
    "/opinions/",  # remove if you want to include opinions
]

KEYWORDS = [
    "israel",
    "palestin",
    "gaza",
    "hamas",
    "west-bank",
    "westbank",
    "jerusalem",
    "ceasefire",
    "idf",
    "settlement",
    "settler",
    "rafah",
]

# Progress printing
PROGRESS_EVERY_N_URLS = 200
PROGRESS_EVERY_N_SITEMAPS = 10


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
    p = urllib.parse.urlsplit(url)
    scheme = "https"
    netloc = (p.netloc or "").lower()
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urllib.parse.urlunsplit((scheme, netloc, path, "", ""))

def is_aljazeera_domain(url: str) -> bool:
    try:
        host = urllib.parse.urlsplit(url).netloc.lower()
    except Exception:
        return False
    return host.endswith("aljazeera.com")

def url_path_lower(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).path.lower()
    except Exception:
        return ""

def looks_like_article(url: str) -> bool:
    if not is_aljazeera_domain(url):
        return False
    path = url_path_lower(url)
    if not any(path.startswith(pfx) for pfx in REQUIRE_PATH_PREFIXES):
        return False
    for bad in EXCLUDED_PATH_SUBSTRINGS:
        if bad in path:
            return False
    if path.rstrip("/") in ("/news",):
        return False
    return True

def keyword_match_in_path(url: str) -> bool:
    path = url_path_lower(url).replace("_", "-")
    return any(kw in path for kw in KEYWORDS)

def parse_iso_date(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s
    return ""

def make_article_id(url: str) -> str:
    cu = canonicalize_url(url)
    if not cu:
        return ""
    return hashlib.sha1(cu.encode("utf-8")).hexdigest()[:12]

def strip_ns(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag

def maybe_decompress(content: bytes, url: str, headers: dict) -> bytes:
    enc = (headers.get("Content-Encoding") or "").lower()
    ctype = (headers.get("Content-Type") or "").lower()
    if url.lower().endswith(".gz") or "gzip" in enc or "application/x-gzip" in ctype:
        try:
            return gzip.decompress(content)
        except OSError:
            return content
    return content


def progress_line(stats: "Stats", prefix: str = "") -> None:
    """
    Single-line progress that updates in-place.
    """
    msg = (
        f"{prefix}"
        f"sitemaps={stats.scanned_sitemaps} "
        f"urls_scanned={stats.scanned_urls} "
        f"kept={stats.kept}/{MAX_OUTPUT_ROWS} "
        f"dup={stats.skipped_dup} "
        f"excluded={stats.skipped_excluded} "
        f"offtopic={stats.skipped_offtopic} "
        f"non_article={stats.skipped_non_article}"
    )
    # pad to clear previous line
    sys.stderr.write("\r" + msg.ljust(140))
    sys.stderr.flush()


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
            return maybe_decompress(resp.content, url, dict(resp.headers))
        except (requests.RequestException, requests.HTTPError) as e:
            last_err = e
            backoff = (BACKOFF_BASE_S ** attempt) + random.uniform(0.0, 0.4)
            time.sleep(backoff)

    sys.stderr.write(f"\n[warn] failed fetch after retries: {url} ({last_err})\n")
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


def ensure_csv_header(path: str) -> None:
    try:
        with open(path, "r", newline="", encoding="utf-8") as _:
            return
    except FileNotFoundError:
        pass

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "source_name",
            "source_domain",
            "url",
            "published_at",
            "retrieved_at",
            "article_id",
            "discovery_method",
            "sitemap_url",
        ])


def load_existing_urls(path: str) -> Set[str]:
    existing: Set[str] = set()
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cu = canonicalize_url(row.get("url", "") or "")
                if cu:
                    existing.add(cu)
    except FileNotFoundError:
        return set()
    except Exception as e:
        sys.stderr.write(f"\n[warn] could not load existing CSV ({e}); starting fresh.\n")
    return existing


def append_rows(path: str, rows: List[dict]) -> None:
    if not rows:
        return
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "source_name",
                "source_domain",
                "url",
                "published_at",
                "retrieved_at",
                "article_id",
                "discovery_method",
                "sitemap_url",
            ],
        )
        for r in rows:
            writer.writerow(r)


@dataclass
class Stats:
    scanned_sitemaps: int = 0
    scanned_urls: int = 0
    kept: int = 0
    skipped_dup: int = 0
    skipped_offtopic: int = 0
    skipped_excluded: int = 0
    skipped_non_article: int = 0


def collect_from_sitemap(
    session: requests.Session,
    sitemap_url: str,
    depth: int,
    max_depth: int,
    seen_sitemaps: Set[str],
    existing_urls: Set[str],
    out_rows: List[dict],
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

    if stats.scanned_sitemaps % PROGRESS_EVERY_N_SITEMAPS == 0:
        progress_line(stats, prefix="fetching... ")

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

        if stats.scanned_urls % PROGRESS_EVERY_N_URLS == 0:
            progress_line(stats, prefix="scanning... ")

        cu = canonicalize_url(loc)
        if not cu:
            stats.skipped_non_article += 1
            continue

        if cu in existing_urls:
            stats.skipped_dup += 1
            continue

        if not looks_like_article(cu):
            path = url_path_lower(cu)
            if any(bad in path for bad in EXCLUDED_PATH_SUBSTRINGS):
                stats.skipped_excluded += 1
            else:
                stats.skipped_non_article += 1
            continue

        if not keyword_match_in_path(cu):
            stats.skipped_offtopic += 1
            continue

        out_rows.append({
            "source_name": SOURCE_NAME,
            "source_domain": SOURCE_DOMAIN,
            "url": cu,
            "published_at": e.lastmod or "",
            "retrieved_at": retrieved_at,
            "article_id": make_article_id(cu),
            "discovery_method": DISCOVERY_METHOD,
            "sitemap_url": sitemap_url,
        })
        existing_urls.add(cu)
        stats.kept += 1


def main() -> int:
    ensure_csv_header(OUTPUT_CSV)

    existing_urls = load_existing_urls(OUTPUT_CSV)
    start_existing = len(existing_urls)

    stats = Stats()
    seen_sitemaps: Set[str] = set()
    buffer_rows: List[dict] = []

    progress_line(stats, prefix="starting... ")
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

            if len(buffer_rows) >= 200:
                append_rows(OUTPUT_CSV, buffer_rows)
                buffer_rows.clear()
                progress_line(stats, prefix="saved... ")

    append_rows(OUTPUT_CSV, buffer_rows)
    buffer_rows.clear()

    # Finish the progress line cleanly
    sys.stderr.write("\n")
    sys.stderr.flush()

    end_existing = len(existing_urls)
    newly_added = end_existing - start_existing

    sys.stderr.write(
        "\n".join([
            "=== Al Jazeera Sitemap Collector Summary ===",
            f"Sitemaps fetched/parsing attempts: {stats.scanned_sitemaps}",
            f"URLs scanned (from urlsets):      {stats.scanned_urls}",
            f"Kept (written this run):          {newly_added}",
            f"Skipped (duplicate/resume):       {stats.skipped_dup}",
            f"Skipped (excluded path):          {stats.skipped_excluded}",
            f"Skipped (offtopic keywords):      {stats.skipped_offtopic}",
            f"Skipped (non-article):            {stats.skipped_non_article}",
            f"Total kept overall in CSV:        {end_existing}",
            "============================================\n",
        ]) + "\n"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
