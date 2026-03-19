#!/usr/bin/env python3
"""
clean_articles.py

Cleans news article titles and descriptions for NLP / RoBERTa preprocessing.

Input CSV columns:  outlet, publish_date, title, description, url
Output CSV:         articles_cleaned.csv (same columns, cleaned)

Usage:
    python clean_articles.py --in master.csv --out articles_cleaned.csv
"""

import argparse
import csv
import html
import re
import ftfy  # new import for fixing mojibake

MAX_TOKENS = 500  # conservative limit for RoBERTa's 512-token window

DATELINE_RE = re.compile(
    r"^[A-Z][A-Z\s,\.]+(?:\([^)]+\))?\s*[—–-]+\s*",
    re.UNICODE,
)

def decode_html_entities(text: str) -> str:
    """Convert HTML entities (&#x27; &amp; &quot; etc.) to plain characters."""
    return html.unescape(text or "")

def remove_dateline(text: str) -> str:
    """Strip datelines like 'WASHINGTON (AP) —' from the start of text."""
    return DATELINE_RE.sub("", text, count=1)

def normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace into a single space."""
    return re.sub(r"\s+", " ", text).strip()

def truncate_to_tokens(text: str, max_tokens: int = MAX_TOKENS) -> str:
    """Truncate to approximately max_tokens words (RoBERTa approximation)."""
    words = text.split()
    return " ".join(words[:max_tokens]) if len(words) > max_tokens else text

HTML_TAG_RE = re.compile(r"<[^>]+>")

def strip_html_tags(text: str) -> str:
    """Remove HTML tags like <strong>, <em>, <a href=...> etc."""
    return HTML_TAG_RE.sub("", text or "")

def fix_mojibake(text: str) -> str:
    """Fix weird characters from Times of India and other sources."""
    return ftfy.fix_text(text or "")

def clean_description(text: str) -> str:
    """Full pipeline for description: decode → fix mojibake → strip tags → dateline → whitespace → truncate."""
    text = decode_html_entities(text)
    text = fix_mojibake(text)          # new step
    text = strip_html_tags(text)
    text = remove_dateline(text)
    text = normalize_whitespace(text)
    text = truncate_to_tokens(text)
    return text

def clean_title(title: str) -> str:
    """Pipeline for titles: decode → fix mojibake → strip tags → whitespace (no truncation needed)."""
    title = decode_html_entities(title)
    title = fix_mojibake(title)        # new step
    title = strip_html_tags(title)
    title = normalize_whitespace(title)
    return title

def process(in_path: str, out_path: str) -> None:
    with open(in_path, newline="", encoding="utf-8") as f_in:
        reader = csv.DictReader(f_in)
        if not reader.fieldnames:
            raise SystemExit(f"[error] no header found in {in_path}")

        fieldnames = list(reader.fieldnames)
        total = 0
        rows_out = []

        for row in reader:
            total += 1
            row["title"]       = clean_title(row.get("title", "") or "")
            row["description"] = clean_description(row.get("description", "") or "")
            rows_out.append(row)

            if total % 1000 == 0:
                print(f"  processed {total:,} rows...")

    with open(out_path, "w", newline="", encoding="utf-8") as f_out:
        writer = csv.DictWriter(f_out, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"\nDone. {total:,} articles cleaned → {out_path}")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in",  dest="in_path",  required=True)
    ap.add_argument("--out", dest="out_path", default="articles_cleaned.csv")
    args = ap.parse_args()
    print(f"Reading:  {args.in_path}")
    print(f"Writing:  {args.out_path}")
    process(args.in_path, args.out_path)

if __name__ == "__main__":
    main()