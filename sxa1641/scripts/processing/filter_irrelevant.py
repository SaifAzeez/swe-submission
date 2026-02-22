#!/usr/bin/env python3
"""
bbc_filter_irrelevant.py

Filter an enriched BBC CSV (with title/description) to keep only rows whose
(title + description) match conflict-related keywords. Writes a new CSV.

- Python 3.10+
- stdlib only (no pandas)
- Keeps original columns + adds: relevance_score, relevance_hits

Usage:
  python bbc_filter_irrelevant.py --in output/bbc-reviewed.csv --out output/bbc-shortlist.csv
  python bbc_filter_irrelevant.py --in output/bbc-reviewed.csv --out output/bbc-shortlist.csv --min-score 2
  python bbc_filter_irrelevant.py --in output/bbc-reviewed.csv --out output/bbc-shortlist.csv --limit 2000
"""

from __future__ import annotations

import argparse
import csv
import re
from typing import Dict, List, Tuple


# Keywords tuned for Israel/Palestine conflict relevance.
# Use stems where helpful (e.g., "palestin" catches "palestine/palestinian").
KEYWORDS = [
    # Core entities / places
    "israel", "israeli", "palestin", "gaza", "west bank", "jerusalem", "rafah",
    "khan younis", "khan yunis", "jabalia", "nablus", "hebron", "ramallah",
    "tel aviv", "jerusalem", "negev", "galilee",

    # Groups / institutions
    "hamas", "idf", "israeli military", "israel defense forces", "israeli army",
    "palestinian authority", "pa", "plo", "fatah", "hezbollah", "islamic jihad",

    # Conflict terms
    "ceasefire", "truce", "airstrike", "air strike", "strike", "bombard", "shell",
    "rocket", "missile", "ground offensive", "incursion", "siege", "blockade",
    "occupation", "settlement", "settlers", "two-state", "two state",
    "hostage", "hostages", "captives", "prisoner", "prisoners",

    # Diplomatic / legal
    "un", "unrwa", "icj", "icc", "security council", "humanitarian", "aid convoy",
    "border crossing", "rafah crossing", "kerem shalom",

    # Sensitive but relevant discourse terms (keep as signals, not stance)
    "genocide", "war crimes", "crimes against humanity",
]

# Some terms are too generic alone (e.g., "un", "strike"). We handle that by scoring:
# - "strong" keywords add 2
# - "weak" keywords add 1
STRONG = {
    "israel", "israeli", "palestin", "gaza", "west bank", "jerusalem", "rafah",
    "hamas", "idf", "hezbollah", "unrwa", "hostage", "hostages", "settlement",
    "settlers", "occupation", "two-state", "two state", "icj", "icc",
}
WEAK = {
    "ceasefire", "truce", "airstrike", "air strike", "rocket", "missile",
    "humanitarian", "aid convoy", "border crossing", "security council", "un",
    "prisoner", "prisoners", "war crimes", "genocide", "blockade", "siege",
    "incursion", "ground offensive",
}


def normalize_text(s: str) -> str:
    s = (s or "").lower()
    # normalize common punctuation and whitespace
    s = s.replace("\u00a0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def find_hits(text: str) -> Tuple[int, List[str]]:
    hits: List[str] = []
    score = 0
    for kw in KEYWORDS:
        if kw in text:
            hits.append(kw)
            if kw in STRONG:
                score += 2
            elif kw in WEAK:
                score += 1
            else:
                score += 1
    # Small bonus if multiple distinct strong entities appear
    strong_count = sum(1 for h in hits if h in STRONG)
    if strong_count >= 2:
        score += 1
    return score, hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="in_csv", required=True, help="Input CSV (must have at least url/title/description)")
    ap.add_argument("--out", dest="out_csv", required=True, help="Output CSV (filtered)")
    ap.add_argument("--min-score", type=int, default=2, help="Minimum relevance score to keep a row (default: 2)")
    ap.add_argument("--limit", type=int, default=0, help="Max rows to write (0 = no limit)")
    args = ap.parse_args()

    kept = 0
    scanned = 0

    with open(args.in_csv, "r", newline="", encoding="utf-8") as f_in:
        reader = csv.DictReader(f_in)
        if reader.fieldnames is None:
            raise SystemExit("Input CSV has no header row.")

        fieldnames = list(reader.fieldnames)
        # Add output fields if not already present
        if "relevance_score" not in fieldnames:
            fieldnames.append("relevance_score")
        if "relevance_hits" not in fieldnames:
            fieldnames.append("relevance_hits")

        with open(args.out_csv, "w", newline="", encoding="utf-8") as f_out:
            writer = csv.DictWriter(f_out, fieldnames=fieldnames)
            writer.writeheader()

            for row in reader:
                scanned += 1

                title = row.get("title", "") or ""
                desc = row.get("description", "") or ""
                blob = normalize_text(f"{title} {desc}")

                score, hits = find_hits(blob)

                if score < args.min_score:
                    continue

                row["relevance_score"] = str(score)
                row["relevance_hits"] = "|".join(hits)

                writer.writerow(row)
                kept += 1

                if args.limit and kept >= args.limit:
                    break

    print(f"Scanned: {scanned}")
    print(f"Kept:    {kept} (min_score={args.min_score})")
    print(f"Wrote:   {args.out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
