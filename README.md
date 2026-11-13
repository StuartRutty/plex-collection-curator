# plex-collection-curator

Build and audit Plex collections from a **verified list** — never from
keyword search.

## The problem

Most collection tools work backwards: they search your library for keywords
("Christmas", "Santa", "Holiday") and add whatever comes back. Search
results are not verification. They return near-matches, same-title
different movies, and wrong-year remakes — and a naive tool shelves all of
them. (A real example: a library where "The Lodge" matched an obscure
same-title short, while the actual film sat on disk unmatched. Exact title
plus exact year is the difference.)

This tool inverts the flow:

1. **You curate the list** — one title + year per entry. The human (or a
   very careful process) is the verification step.
2. **The tool matches exactly** — normalized title equality *and* year
   equality, or the entry is reported missing. Near-misses are never added.
3. **Dry-run by default** — `check` mode changes nothing.
4. **Apply verifies itself** — after adding, it re-reads the collection
   and proves every added item is actually a member before reporting
   success. A write response is not evidence; a re-read is.

## Usage

Stdlib-only Python 3. No dependencies.

```bash
export PLEX_URL="http://your-server:32400"
export PLEX_TOKEN="your-token"   # never printed or stored by the tool

# Report only: found / missing / ambiguous per entry
python3 curator.py --list lists/christmas-movies.csv --collection "The Christmas Shelf"

# Also report collection members that are NOT on the list (drift)
python3 curator.py --list lists/christmas-movies.csv --collection "The Christmas Shelf" --mode audit

# Add the found-but-unshelved items (creating the collection if needed), then verify
python3 curator.py --list lists/christmas-movies.csv --collection "The Christmas Shelf" --mode apply
```

If you have several movie libraries, pass `--library NAME`.

## List format

CSV with a header, or plain lines — both are just title + year:

```csv
title,year
Elf,2003
"Home Alone 2: Lost in New York",1992
```

```text
# comments and blank lines are fine
Elf (2003)
Krampus (2015)
```

`lists/christmas-movies.csv` is a worked example: a Christmas movie shelf
curated by hand, title by title.

## What the modes report

- **MISSING** — no exact title+year match in the library. The tool never
  substitutes a near-match.
- **AMBIGUOUS** — more than one exact match (duplicate files). Skipped;
  a human decides.
- **EXTRA** (audit) — in the collection but not on your list. Drift from
  manual additions or looser tooling, surfaced instead of silently kept.
- **ADDED** (apply) — added and confirmed present on a fresh re-read.
  Anything that doesn't land is reported FAILED with a non-zero exit.

## Scope and limits

- Movies, one library section. TV episodes are the obvious next step and
  are deliberately out of scope for now — episode collections need
  show/season/episode addressing, not title+year.
- Adds only. The tool never removes anything; EXTRA items are reported
  for a human to deal with.
- Matching is exact after case/whitespace/apostrophe normalization.
  If your library stores a title differently ("The Lodge" filed under a
  localized name), the entry reports MISSING — fix the list or the
  metadata, don't loosen the matcher.

## Tests

`tests/test_curator.py` runs the full check/audit/apply flow against a
stub Plex client seeded with the traps this tool exists to defeat —
a same-title wrong-year item, a missing title, and a collection with an
unlisted extra:

```bash
python3 -m unittest discover -s tests
```

## Origin

Distilled from a real home-library curation workflow in which a Christmas
collection of 90+ items was rebuilt from a hand-verified list after a
keyword-built version proved unreliable — the rebuild found the shelf
both over- and under-inclusive. The list in `lists/` is that shelf's
movie core.

---

*By Stuart Rutty. MIT licensed — see [LICENSE](LICENSE).*
