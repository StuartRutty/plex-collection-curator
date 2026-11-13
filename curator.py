#!/usr/bin/env python3
"""Build and audit Plex collections from a verified list — never from search.

Keyword search is how wrong titles end up in collections: a search for a
title returns near-matches, same-title different movies, and different-year
remakes, and a naive script adds all of them. This tool inverts the flow:

  1. YOU curate the list (title + year per entry — the human verification).
  2. The tool finds each entry in your library by EXACT title and year.
  3. Only exact matches are eligible. Near-misses are reported, never added.
  4. Apply mode adds the eligible items, then re-reads the collection and
     proves every added item is actually a member before reporting success.

Modes:
  check  (default) — report found / missing / ambiguous for every list entry.
  audit            — check, plus list collection members that are NOT on
                     your list (drift: things added by hand or by a looser
                     tool).
  apply            — check, then add the found-but-unshelved items to the
                     collection (creating it if needed) and verify.

Connection: --url / --token flags, or PLEX_URL / PLEX_TOKEN environment
variables. The token is never printed or written anywhere.

Stdlib only. No dependencies.

Examples:
  python3 curator.py --list christmas.csv --collection "The Christmas Shelf"
  python3 curator.py --list christmas.csv --collection "The Christmas Shelf" --mode apply
  python3 curator.py --list christmas.csv --collection "The Christmas Shelf" --mode audit
"""
import argparse
import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request

# How Plex is told which item to add to a collection. Kept as one constant
# so the form is easy to adjust if a server version expects a variant.
ITEM_URI_TEMPLATE = "library:///library/metadata/{rating_key}"


def normalize_title(t):
    """Case/whitespace/apostrophe-insensitive, but otherwise exact."""
    t = (t or "").replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", t).strip().casefold()


def parse_list(path):
    """Read a verified list: CSV (title,year) or lines of 'Title (Year)'.

    Returns [(title, year)] preserving file order. Blank lines and lines
    starting with # are skipped. A CSV header row is skipped when present.
    """
    entries = []
    with open(path, newline="", encoding="utf-8") as f:
        text = f.read()
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not lines:
        return entries
    # CSV when the first line looks like title,year
    first = lines[0]
    if "," in first and re.search(r",\s*\d{4}\s*$", first) or first.lower().startswith("title"):
        for row in csv.reader(lines):
            if not row or row[0].strip().lower() == "title":
                continue
            title = row[0].strip()
            year = int(row[1].strip())
            entries.append((title, year))
        return entries
    for ln in lines:
        m = re.match(r"^(.*?)\s*\((\d{4})\)\s*$", ln)
        if not m:
            raise ValueError(f"cannot parse list line: {ln!r} (want 'Title (Year)' or CSV title,year)")
        entries.append((m.group(1).strip(), int(m.group(2))))
    return entries


class PlexClient:
    """Minimal Plex Media Server client (JSON responses)."""

    def __init__(self, base_url, token):
        self.base = base_url.rstrip("/")
        self.token = token

    def _request(self, method, path, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method)
        req.add_header("Accept", "application/json")
        req.add_header("X-Plex-Token", self.token)
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
        return json.loads(body) if body else {}

    def get(self, path, params=None):
        return self._request("GET", path, params)

    def put(self, path, params=None):
        return self._request("PUT", path, params)

    def post(self, path, params=None):
        return self._request("POST", path, params)

    # -- library ------------------------------------------------------
    def sections(self):
        data = self.get("/library/sections")
        return data.get("MediaContainer", {}).get("Directory", [])

    def movie_section(self, name=None):
        secs = [s for s in self.sections() if s.get("type") == "movie"]
        if name:
            for s in secs:
                if s.get("title") == name:
                    return s
            raise SystemExit(f"no movie library named {name!r}")
        if len(secs) != 1:
            raise SystemExit("multiple movie libraries; pass --library NAME")
        return secs[0]

    def search(self, section_key, title):
        """Server-side title search; returns candidate Metadata items."""
        data = self.get(f"/library/sections/{section_key}/all", {"title": title})
        return data.get("MediaContainer", {}).get("Metadata", [])

    def collections(self, section_key):
        data = self.get(f"/library/sections/{section_key}/collections")
        return data.get("MediaContainer", {}).get("Metadata", [])

    def collection_children(self, collection_key):
        data = self.get(f"/library/collections/{collection_key}/children")
        return data.get("MediaContainer", {}).get("Metadata", [])

    def create_collection(self, section_key, title, first_item_key):
        return self.post("/library/collections", {
            "type": 1,
            "title": title,
            "sectionId": section_key,
            "uri": ITEM_URI_TEMPLATE.format(rating_key=first_item_key),
        })

    def add_to_collection(self, collection_key, item_key):
        return self.put(f"/library/collections/{collection_key}/items", {
            "uri": ITEM_URI_TEMPLATE.format(rating_key=item_key),
        })


def find_exact(client, section_key, title, year):
    """Return (status, item) — status in found / missing / ambiguous."""
    want = normalize_title(title)
    exact = []
    for item in client.search(section_key, title):
        if normalize_title(item.get("title")) == want and item.get("year") == year:
            exact.append(item)
    if len(exact) == 1:
        return "found", exact[0]
    if len(exact) > 1:
        return "ambiguous", exact
    return "missing", None


def run(client, entries, collection_name, library_name, mode):
    section = client.movie_section(library_name)
    skey = section["key"]

    coll = None
    for c in client.collections(skey):
        if c.get("title") == collection_name:
            coll = c
            break
    member_keys = set()
    if coll:
        member_keys = {m["ratingKey"] for m in client.collection_children(coll["ratingKey"])}

    found, missing, ambiguous, to_add = [], [], [], []
    listed_keys = set()
    for title, year in entries:
        status, item = find_exact(client, skey, title, year)
        if status == "found":
            found.append((title, year, item))
            listed_keys.add(item["ratingKey"])
            if item["ratingKey"] not in member_keys:
                to_add.append((title, year, item))
        elif status == "ambiguous":
            ambiguous.append((title, year, item))
        else:
            missing.append((title, year))

    print(f"Library: {section.get('title')}  Collection: {collection_name}"
          f"{'' if coll else ' (does not exist yet)'}")
    print(f"List: {len(entries)} entries — {len(found)} in library, "
          f"{len(missing)} missing, {len(ambiguous)} ambiguous, "
          f"{len(to_add)} not yet in the collection")
    for title, year in missing:
        print(f"  MISSING    {title} ({year})")
    for title, year, items in ambiguous:
        keys = ", ".join(i["ratingKey"] for i in items)
        print(f"  AMBIGUOUS  {title} ({year}) — {len(items)} exact matches (keys {keys}); skipped")

    if mode == "audit" and coll:
        extras = [m for m in client.collection_children(coll["ratingKey"])
                  if m["ratingKey"] not in listed_keys]
        for m in extras:
            print(f"  EXTRA      {m.get('title')} ({m.get('year')}) — in the collection, not on the list")
        if not extras:
            print("  No extras: the collection matches the list exactly.")

    if mode == "apply":
        if not to_add:
            print("Nothing to add — every found item is already a member (verified).")
            return 0
        if coll is None:
            first = to_add[0][2]
            print(f"Creating collection {collection_name!r} with {first.get('title')!r}…")
            client.create_collection(skey, collection_name, first["ratingKey"])
            coll = next(c for c in client.collections(skey) if c.get("title") == collection_name)
            to_add = to_add[1:]
        for title, year, item in to_add:
            client.add_to_collection(coll["ratingKey"], item["ratingKey"])
        # Verify against a fresh read — never trust the write response.
        after = {m["ratingKey"] for m in client.collection_children(coll["ratingKey"])}
        added = [(t, y, it) for t, y, it in found if it["ratingKey"] not in member_keys]
        failed = [(t, y) for t, y, it in added if it["ratingKey"] not in after]
        if failed:
            for t, y in failed:
                print(f"  FAILED     {t} ({y}) — add did not land; not a member on re-read")
            return 1
        for t, y, _ in added:
            print(f"  ADDED      {t} ({y}) — membership verified on re-read")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build/audit a Plex collection from a verified title+year list.")
    ap.add_argument("--list", required=True, help="CSV (title,year) or 'Title (Year)' lines")
    ap.add_argument("--collection", required=True)
    ap.add_argument("--library", default=None, help="movie library name (default: the only movie library)")
    ap.add_argument("--mode", choices=["check", "audit", "apply"], default="check")
    ap.add_argument("--url", default=os.environ.get("PLEX_URL"))
    ap.add_argument("--token", default=os.environ.get("PLEX_TOKEN"))
    args = ap.parse_args(argv)
    if not args.url or not args.token:
        print("error: set --url/--token or PLEX_URL/PLEX_TOKEN", file=sys.stderr)
        return 2
    entries = parse_list(args.list)
    client = PlexClient(args.url, args.token)
    return run(client, entries, args.collection, args.library, args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
