import csv
import sys

def dedup_csv(input_file, output_file=None):
    if output_file is None:
        output_file = input_file.replace(".csv", "_deduped.csv")

    seen_titles = set()
    seen_descriptions = set()
    rows_kept = []
    rows_removed = 0

    with open(input_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames

        for row in reader:
            title = row.get("title", "").strip().lower()
            description = row.get("description", "").strip().lower()

            if title in seen_titles or description in seen_descriptions:
                rows_removed += 1
                continue

            if title:
                seen_titles.add(title)
            if description:
                seen_descriptions.add(description)

            rows_kept.append(row)

    with open(output_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_kept)

    print(f"Done: kept {len(rows_kept)} rows, removed {rows_removed} duplicates → {output_file}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python dedup_csv.py input.csv [output.csv]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None
    dedup_csv(input_file, output_file)