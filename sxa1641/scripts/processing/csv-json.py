#!/usr/bin/env python3
"""
csv_to_json.py

Convert a CSV file to JSON.

Usage:
    python csv_to_json.py input.csv output.json

Output format:
[
  { "col1": "value", "col2": "value" },
  ...
]
"""

import csv
import json
import sys


def csv_to_json(csv_path: str, json_path: str) -> None:
    data = []

    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            data.append(row)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Converted {len(data)} rows → {json_path}")


def main():
    if len(sys.argv) != 3:
        print("Usage: python csv_to_json.py input.csv output.json")
        sys.exit(1)

    csv_path = sys.argv[1]
    json_path = sys.argv[2]

    csv_to_json(csv_path, json_path)


if __name__ == "__main__":
    main()
