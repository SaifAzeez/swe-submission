#!/usr/bin/env python3
"""
bbc_pipeline.py

Single-file pipeline to:
1) Load a BBC discovery CSV (from your sitemap collector)
2) Ensure headers / normalize columns
3) Fetch titles + descriptions from each URL (polite + retry/backoff)
4) Add shortlist flag based on title+description keywords
5) Basic text cleaning (optional light cleanup)
6) Write enriched output CSV

Requirements:
- Python 3.10+
- stdlib + requests only

Usage:
  python bbc_pipeline.py --in output/bbc-output.csv --out output/bbc-reviewed.csv --limit 2000
  python bbc_pipeline.py --in output/bbc-output.csv --out output/bbc-reviewed.csv --limit 2000 --resume
  python bbc_pipeline.py --in output/bbc-output.csv --out output/bbc-reviewed.csv --limit 2000 --resume --only-shortlist

Notes:
- If your input CSV has NO header row, use: --no-header
- Resumable: if --resume and output exists, skips URLs already in output.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import os
import random
import re
import sys
import time
import urllib.parse
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests


# -----------------------------
# Config
# -----------------------------

USER_AGENT = "bbc-single-pipeline/1.0"
TIMEOUT_S = 20
MAX_RETRIES = 4
BACKOFF_BASE_S = 0.9

MIN_DELAY_S = 0.35
MAX_DELAY_S = 0.85

# Output fields (input fields preserved where possible)
OUT_FIELDS = [
    "source_name",
    "source_domain",
    "url",
    "published_at",
    "retrieved_at",
    "article_id",
    "discovery_method",
    "sitemap_url",
    "title",
    "description",
    "fetched_at",
    "http_status",
    "notes",
    "shortlist",
]

# Shortlist keywords for title+description (higher precision than URL-only)
SHORTLIST_KEYWORDS = [
    "israel", "israeli", "palestin", "gaza", "hamas", "idf",
    "west bank", "jerusalem", "rafah", "hostage", "ceasefire",
    "settlement", "airstrike", "rocket", "iran", "hezbollah",
]

# Expected input column order if input has no header row
DEFAULT_INPUT_COLS = [
    "source_name",
    "source_domain",
    "url",
    "published_at",
    "retrieved_at",
    "article_id",
    "discovery_method",
    "sitemap_url",
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
    p = urllib.parse.urlsplit(url)
    scheme = "https"
    netloc = (p.netloc or "").lower()
    path = re.sub(r"/{2,}", "/", p.path or "/")
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    return urllib.parse.urlunsplit((scheme, netloc, path, "", ""))

def clean_text(s: str) -> str:
    """
    Light cleanup:
    - HTML unescape
    - normalize whitespace
    - strip non-printing oddities conservatively
    """
    s = html.unescape(s or "")
    s = s.replace("\u00a0", " ")  # nbsp
    s = re.sub(r"\s+", " ", s).strip()
    return s

def contains_any(text: str, kws: List[str]) -> bool:
    t = (text or "").lower()
    return any(k in t for k in kws)

def fetch_html(session: requests.Session, url: str) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    last_err: Optional[str] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, headers=headers, timeout=TIMEOUT_S, allow_redirects=True)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            r.encoding = r.encoding or "utf-8"
            return r.text, r.status_code, None
        except (requests.RequestException, requests.HTTPError) as e:
            last_err = str(e)
            backoff = (BACKOFF_BASE_S ** attempt) + random.uniform(0.0, 0.4)
            time.sleep(backoff)
    return None, None, f"fetch_failed: {last_err}" if last_err else "fetch_failed"


# -----------------------------
# HTML extraction (no BeautifulSoup)
# -----------------------------

_META_TAG_RE = re.compile(r"<meta\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(
    r"""(\w[\w:-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""",
    re.IGNORECASE,
)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

def _parse_meta_attrs(tag: str) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for m in _ATTR_RE.finditer(tag):
        key = (m.group(1) or "").strip().lower()
        val = m.group(2) or m.group(3) or m.group(4) or ""
        attrs[key] = html.unescape(val.strip())
    return attrs

def extract_title_description(html_text: str) -> Tuple[str, str, str]:
    """
    Extract:
      - title: og:title -> twitter:title -> <title>
      - desc : og:description -> name=description -> twitter:description

    Returns (title, description, note)
    """
    og_title = tw_title = title_tag = ""
    og_desc = meta_desc = tw_desc = ""

    for tag in _META_TAG_RE.findall(html_text or ""):
        attrs = _parse_meta_attrs(tag)
        if not attrs:
            continue

        prop = (attrs.get("property") or "").lower()
        name = (attrs.get("name") or "").lower()
        content = (attrs.get("content") or "").strip()
        if not content:
            continue

        if prop == "og:title" and not og_title:
            og_title = content
        elif name == "twitter:title" and not tw_title:
            tw_title = content
        elif prop == "og:description" and not og_desc:
            og_desc = content
        elif name == "description" and not meta_desc:
            meta_desc = content
        elif name == "twitter:description" and not tw_desc:
            tw_desc = content

    m = _TITLE_TAG_RE.search(html_text or "")
    if m:
        raw = html.unescape(m.group(1))
        title_tag = re.sub(r"\s+", " ", raw).strip()

    title = (og_title or tw_title or title_tag).strip()
    desc = (og_desc or meta_desc or tw_desc).strip()

    # Clean common suffixes
    if title.lower().endswith(" - bbc news"):
        title = title[: -len(" - BBC News")].strip()
    if title.lower().endswith(" - bbc"):
        title = title[: -len(" - BBC")].strip()

    note = ""
    if not title and not desc:
        note = "no_meta_found"

    return clean_text(title), clean_text(desc), note


# -----------------------------
# CSV IO
# -----------------------------

def sniff_has_header(csv_path: str) -> bool:
    """
    Best-effort check: if first row contains 'url' somewhere, assume header.
    """
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        first = f.readline()
    first = first.strip().lower()
    return "url" in first and "source_domain" in first

def read_input_rows(csv_path: str, no_header: bool) -> List[Dict[str, str]]:
    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        if no_header:
            reader = csv.reader(f)
            rows: List[Dict[str, str]] = []
            for r in reader:
                if not r:
                    continue
                # pad/truncate
                r = (r + [""] * len(DEFAULT_INPUT_COLS))[: len(DEFAULT_INPUT_COLS)]
                rows.append(dict(zip(DEFAULT_INPUT_COLS, r)))
            return rows
        else:
            reader = csv.DictReader(f)
            rows = [row for row in reader if row]
            return rows

def ensure_out_header(out_csv: str) -> None:
    if os.path.exists(out_csv) and os.path.getsize(out_csv) > 0:
        return
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        w.writeheader()

def load_done_urls(out_csv: str) -> Set[str]:
    done: Set[str] = set()
    if not os.path.exists(out_csv):
        return done
    try:
        with open(out_csv, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                cu = canonicalize_url(row.get("url", "") or "")
                if cu:
                    done.add(cu)
    except Exception as e:
        sys.stderr.write(f"[warn] could not read output for resume: {e}\n")
    return done

def append_out(out_csv: str, rows: List[Dict[str, str]]) -> None:
    if not rows:
        return
    with open(out_csv, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        for r in rows:
            # guarantee all fields
            out_row = {k: r.get(k, "") for k in OUT_FIELDS}
            w.writerow(out_row)


# -----------------------------
# Pipeline
# -----------------------------

def build_base_row(in_row: Dict[str, str]) -> Dict[str, str]:
    # Keep discovered columns if present; else blank.
    return {
        "source_name": (in_row.get("source_name") or "").strip(),
        "source_domain": (in_row.get("source_domain") or "").strip(),
        "url": canonicalize_url(in_row.get("url") or ""),
        "published_at": (in_row.get("published_at") or "").strip(),
        "retrieved_at": (in_row.get("retrieved_at") or "").strip(),
        "article_id": (in_row.get("article_id") or "").strip(),
        "discovery_method": (in_row.get("discovery_method") or "").strip(),
        "sitemap_url": (in_row.get("sitemap_url") or "").strip(),
        "title": "",
        "description": "",
        "fetched_at": "",
        "http_status": "",
        "notes": "",
        "shortlist": "",
    }

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_csv", required=True, help="Input discovery CSV")
    ap.add_argument("--out", dest="out_csv", required=True, help="Output enriched CSV")
    ap.add_argument("--limit", type=int, default=2000, help="Max URLs to process")
    ap.add_argument("--resume", action="store_true", help="Skip URLs already present in --out")
    ap.add_argument("--no-header", action="store_true", help="Input CSV has no header row")
    ap.add_argument("--flush-every", type=int, default=50, help="Write rows every N processed")
    ap.add_argument("--only-shortlist", action="store_true", help="Write only shortlisted rows to output")
    ap.add_argument("--no-clean", action="store_true", help="Disable light text cleaning")
    args = ap.parse_args()

    if not os.path.exists(args.in_csv):
        sys.stderr.write(f"[error] input not found: {args.in_csv}\n")
        return 2

    # If user didn't specify, try to infer
    no_header = args.no_header
    if not args.no_header and not sniff_has_header(args.in_csv):
        # This is a hint, not forced; but helps avoid confusion.
        sys.stderr.write("[info] input doesn't look like it has a header; consider --no-header if parsing looks wrong.\n")

    rows_in = read_input_rows(args.in_csv, no_header=no_header)

    ensure_out_header(args.out_csv)
    done = load_done_urls(args.out_csv) if args.resume else set()

    # Order-preserving unique URLs
    seen: Set[str] = set()
    items: List[Dict[str, str]] = []
    for r in rows_in:
        base = build_base_row(r)
        u = base["url"]
        if not u:
            continue
        if u in seen:
            continue
        seen.add(u)
        items.append(base)

    items = items[: max(0, args.limit)]
    if args.resume:
        items = [it for it in items if it["url"] not in done]

    sys.stderr.write(f"[info] unique input urls={len(seen)} | to process={len(items)} | resume={args.resume}\n")

    ok = 0
    fail = 0
    kept_shortlist = 0
    buf: List[Dict[str, str]] = []

    # Allow turning off cleaning
    global clean_text
    if args.no_clean:
        def clean_text(s: str) -> str:  # type: ignore[no-redef]
            return (s or "").strip()

    with requests.Session() as session:
        for idx, row in enumerate(items, 1):
            url = row["url"]
            sleep_polite()
            fetched_at = now_utc_iso()

            html_text, status, err = fetch_html(session, url)
            row["fetched_at"] = fetched_at

            if html_text is None:
                fail += 1
                row["http_status"] = ""
                row["notes"] = err or "fetch_failed"
                row["title"] = ""
                row["description"] = ""
                row["shortlist"] = "0"
            else:
                ok += 1
                row["http_status"] = str(status) if status is not None else ""
                title, desc, note = extract_title_description(html_text)
                row["title"] = title
                row["description"] = desc
                row["notes"] = note

                td = f"{title} {desc}"
                is_short = contains_any(td, SHORTLIST_KEYWORDS)
                row["shortlist"] = "1" if is_short else "0"
                if is_short:
                    kept_shortlist += 1

            if args.only_shortlist and row["shortlist"] != "1":
                pass
            else:
                buf.append(row)

            if len(buf) >= args.flush_every:
                append_out(args.out_csv, buf)
                buf.clear()

            if idx % 100 == 0:
                sys.stderr.write(
                    f"[info] {idx}/{len(items)} ok={ok} fail={fail} shortlisted={kept_shortlist}\n"
                )

    append_out(args.out_csv, buf)
    buf.clear()

    sys.stderr.write(
        f"[done] processed={len(items)} ok={ok} fail={fail} shortlisted={kept_shortlist} out={args.out_csv}\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
