"""Tests for curator.py against a stub Plex client.

The stub deliberately includes the traps this tool exists to defeat:
a same-title item from the wrong year, and a title that is absent.
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import curator


class FakePlex:
    def __init__(self, with_collection=True):
        self.library = [
            {"ratingKey": "100", "title": "Elf", "year": 2003},
            {"ratingKey": "101", "title": "Krampus", "year": 2015},
            {"ratingKey": "102", "title": "It's a Wonderful Life", "year": 1946},
            # Same title, wrong year — the classic trap.
            {"ratingKey": "900", "title": "The Lodge", "year": 2014},
            {"ratingKey": "901", "title": "The Lodge", "year": 2019},
            {"ratingKey": "777", "title": "Unlisted Extra", "year": 1999},
        ]
        self._collections = []
        self.members = {}
        if with_collection:
            self._collections = [{"ratingKey": "50", "title": "The Christmas Shelf"}]
            self.members = {"50": ["100", "777"]}  # Elf shelved; an extra lurks

    # -- client surface used by curator.run / find_exact ----------------
    def machine_id(self):
        return "testmachine"

    def item_uri(self, rating_key):
        return curator.ITEM_URI_TEMPLATE.format(machine_id=self.machine_id(),
                                                rating_key=rating_key)

    def sections(self):
        return [{"key": "1", "type": "movie", "title": "Movies"}]

    def movie_section(self, name=None):
        return self.sections()[0]

    def all_items(self, section_key):
        return list(self.library)

    def search(self, section_key, title):
        want = curator.normalize_title(title)
        return [i for i in self.library if want in curator.normalize_title(i["title"])]

    def collections(self, section_key):
        return list(self._collections)

    def collection_children(self, key):
        by_key = {i["ratingKey"]: i for i in self.library}
        return [by_key[k] for k in self.members.get(key, [])]

    def create_collection(self, section_key, title, first_item_key):
        self._collections.append({"ratingKey": "99", "title": title})
        self.members["99"] = [first_item_key]
        return {}

    def add_to_collection(self, collection_key, item_key):
        self.members.setdefault(collection_key, []).append(item_key)
        return {}


ENTRIES = [("Elf", 2003), ("Krampus", 2015), ("The Lodge", 2019), ("Absent Film", 2020)]


def run_quiet(fake, mode):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = curator.run(fake, ENTRIES, "The Christmas Shelf", None, mode)
    return rc, buf.getvalue()


class TestFindExact(unittest.TestCase):
    def test_year_disambiguates_same_title(self):
        fake = FakePlex()
        status, item = curator.find_exact(fake, "1", "The Lodge", 2019)
        self.assertEqual(status, "found")
        self.assertEqual(item["ratingKey"], "901")

    def test_wrong_year_is_missing_not_a_match(self):
        fake = FakePlex()
        status, _ = curator.find_exact(fake, "1", "The Lodge", 2020)
        self.assertEqual(status, "missing")

    def test_curly_apostrophe_matches_straight_via_all_items(self):
        fake = FakePlex()

        def fail_search(*args, **kwargs):
            raise AssertionError("find_exact must use all_items, not search")

        fake.search = fail_search
        status, item = curator.find_exact(fake, "1", "It’s a Wonderful Life", 1946)
        self.assertEqual(status, "found")
        self.assertEqual(item["ratingKey"], "102")


class TestRun(unittest.TestCase):
    def test_check_reports_counts(self):
        rc, out = run_quiet(FakePlex(), "check")
        self.assertEqual(rc, 0)
        self.assertIn("3 in library, 1 missing", out)
        self.assertIn("MISSING    Absent Film (2020)", out)

    def test_apply_adds_only_unshelved_and_verifies(self):
        fake = FakePlex()
        rc, out = run_quiet(fake, "apply")
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(fake.members["50"]), ["100", "101", "777", "901"])
        self.assertIn("ADDED      Krampus (2015)", out)
        # Second run: nothing left to add.
        rc, out = run_quiet(fake, "apply")
        self.assertIn("Nothing to add", out)

    def test_apply_creates_collection_when_absent(self):
        fake = FakePlex(with_collection=False)
        rc, out = run_quiet(fake, "apply")
        self.assertEqual(rc, 0)
        self.assertIn("Creating collection", out)
        self.assertEqual(sorted(fake.members["99"]), ["100", "101", "901"])

    def test_audit_reports_extras(self):
        rc, out = run_quiet(FakePlex(), "audit")
        self.assertIn("EXTRA      Unlisted Extra (1999)", out)

    def test_duplicate_entries_added_once(self):
        fake = FakePlex()
        entries = [("Krampus", 2015), ("krampus", 2015), ("Krampus", 2015)]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = curator.run(fake, entries, "The Christmas Shelf", None, "apply")
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertEqual(fake.members["50"].count("101"), 1)
        self.assertIn("DUPLICATE  krampus (2015)", out)
        self.assertIn("DUPLICATE  Krampus (2015)", out)


class TestItemUri(unittest.TestCase):
    def test_uri_uses_server_form_with_machine_id(self):
        fake = FakePlex()
        self.assertEqual(
            fake.item_uri("901"),
            "server://testmachine/com.plexapp.plugins.library/library/metadata/901")


class TestParseList(unittest.TestCase):
    def test_csv_and_plain_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "list.csv")
            txt_path = os.path.join(tmp, "list.txt")
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write("title,year\nElf,2003\n\"Home Alone 2: Lost in New York\",1992\n")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("# comment\nElf (2003)\n\nKrampus (2015)\n")
            self.assertEqual(curator.parse_list(csv_path),
                             [("Elf", 2003), ("Home Alone 2: Lost in New York", 1992)])
            self.assertEqual(curator.parse_list(txt_path),
                             [("Elf", 2003), ("Krampus", 2015)])

    def test_utf8_bom_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "bom.csv")
            with open(csv_path, "w", encoding="utf-8-sig") as f:
                f.write("title,year\nElf,2003\nKrampus,2015\n")
            self.assertEqual(curator.parse_list(csv_path),
                             [("Elf", 2003), ("Krampus", 2015)])

    def test_plain_text_starting_with_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            txt_path = os.path.join(tmp, "titles.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("Title of Show (2008)\nElf (2003)\n")
            self.assertEqual(curator.parse_list(txt_path),
                             [("Title of Show", 2008), ("Elf", 2003)])

    def test_malformed_csv_raises_with_line_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = os.path.join(tmp, "bad.csv")
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write("title,year\nElf,2003\nBadMovie,notayear\n")
            with self.assertRaises(ValueError) as ctx:
                curator.parse_list(csv_path)
            self.assertIn("line 3", str(ctx.exception))
            self.assertIn("BadMovie,notayear", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
