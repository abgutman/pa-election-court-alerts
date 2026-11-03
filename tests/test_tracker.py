from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

import tracker


class TrackerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = tracker.load_json(tracker.CONFIG_PATH)

    def test_scope_includes_all_target_counties(self):
        counties = set(self.config["scope"]["counties"])
        self.assertEqual(counties, {
            "Philadelphia", "Bucks", "Chester", "Delaware", "Montgomery",
            "Lackawanna", "Luzerne", "Monroe", "Pike", "Wayne",
        })

    def test_named_and_generic_matching(self):
        terms = tracker.watch_terms(self.config)
        hits = tracker.matching_terms(
            "Bob Harvie sued the Bucks County Board of Elections over a mail ballot.", terms
        )
        self.assertIn("Bob Harvie", hits)
        self.assertIn("Bucks County Board of Elections", hits)
        self.assertIn("mail ballot", hits)

    def test_word_boundaries_avoid_substring_false_positive(self):
        self.assertEqual(tracker.matching_terms("The voters arrived", ["vote"]), [])

    def test_date_only_source_is_conservatively_included_after_intraday_cutoff(self):
        since = datetime(2026, 11, 2, 8, tzinfo=ZoneInfo("America/New_York"))
        item = tracker.Item("s", "c", "1", "", "", "2026-11-02", "", "", "")
        self.assertTrue(tracker.item_is_since(item, since))

    def test_alert_fingerprint_is_stable(self):
        first = tracker.Item("s", "c", "id", "1", "title", "2026-11-02", "desc", "url", "text")
        second = tracker.Item("s", "c", "id", "changed", "changed", "2026-11-03", "changed", "changed", "changed")
        self.assertEqual(first.fingerprint, second.fingerprint)


if __name__ == "__main__":
    unittest.main()
