from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from monitor import (  # noqa: E402
    ArticleRange,
    Consolidation,
    Impact,
    MonitorError,
    Selection,
    csv_document,
    html_document,
    merge_entries,
    natural_key,
    resolve_end_date,
    selection_matches,
    validate_entry_language_coverage,
)


class MonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.consolidation = Consolidation(
            effective_date="2026-08-18",
            sr_number="946.231.143.6",
            title="Verordnung über Massnahmen & Tests",
            abstract_uri="https://fedlex.data.admin.ch/eli/cc/2025/838",
        )
        self.impact = Impact(
            effective_date="2026-08-18",
            abstract_uri=self.consolidation.abstract_uri,
            amendment_date="2026-08-17",
            publication_date="2026-08-18",
            as_number="422",
            act_uri="https://fedlex.data.admin.ch/eli/oc/2026/422",
            authorities=("Staatssekretariat für Wirtschaft",),
            target_subdivisions=(
                "https://fedlex.data.admin.ch/eli/cc/2025/838/art_1",
            ),
        )

    def test_merge_derives_requested_columns(self) -> None:
        entries = merge_entries([self.consolidation], [self.impact])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].as_reference, "AS 2026 422")
        self.assertEqual(entries[0].amendment_date, "2026-08-17")
        self.assertEqual(entries[0].authority, "Staatssekretariat für Wirtschaft")

    def test_merge_rejects_unenriched_consolidation(self) -> None:
        with self.assertRaises(MonitorError):
            merge_entries([self.consolidation], [])

    def test_sr_numbers_are_sorted_naturally(self) -> None:
        values = ["11.2", "2.10", "2.2", "0.9"]
        self.assertEqual(
            sorted(values, key=natural_key), ["0.9", "2.2", "2.10", "11.2"]
        )

    def test_dynamic_end_date_uses_five_calendar_years(self) -> None:
        end_date, years = resolve_end_date(
            {"years_from_today": 5}, today=date(2026, 8, 19)
        )
        self.assertEqual(end_date, "2031-08-19")
        self.assertEqual(years, 5)

        leap_end, _ = resolve_end_date(
            {"years_from_today": 5}, today=date(2024, 2, 29)
        )
        self.assertEqual(leap_end, "2029-02-28")

    def test_selection_matches_prefix_exact_and_or_article_range(self) -> None:
        selection = Selection(
            description="Testauswahl",
            exact_sr_numbers=frozenset({"101"}),
            sr_number_prefixes=("837",),
            article_ranges=(ArticleRange("220", 319, 362),),
        )
        prefix_item = Consolidation("2027-01-01", "837.02", "AVIG", "urn:837")
        exact_item = Consolidation("2027-01-01", "101", "BV", "urn:101")
        or_item = Consolidation("2027-01-01", "220", "OR", "urn:220")
        unrelated_item = Consolidation("2027-01-01", "8370.1", "Nicht 837", "urn:x")
        or_inside = Impact(
            "2027-01-01",
            "urn:220",
            "2026-01-01",
            "2026-01-02",
            "1",
            "https://fedlex.data.admin.ch/eli/oc/2026/1",
            (),
            ("https://fedlex.data.admin.ch/eli/cc/or/art_330a",),
        )
        or_outside = Impact(
            "2027-01-01",
            "urn:220",
            "2026-01-01",
            "2026-01-02",
            "1",
            "https://fedlex.data.admin.ch/eli/oc/2026/1",
            (),
            ("https://fedlex.data.admin.ch/eli/cc/or/art_656b",),
        )

        self.assertTrue(selection_matches(prefix_item, self.impact, selection))
        self.assertTrue(selection_matches(exact_item, self.impact, selection))
        self.assertTrue(selection_matches(or_item, or_inside, selection))
        self.assertFalse(selection_matches(or_item, or_outside, selection))
        self.assertFalse(selection_matches(unrelated_item, self.impact, selection))

    def test_documents_are_deterministic_and_escape_html(self) -> None:
        entries = merge_entries([self.consolidation], [self.impact])
        first_csv = csv_document(entries)
        second_csv = csv_document(entries)
        self.assertEqual(first_csv, second_csv)
        self.assertIn("AS 2026 422", first_csv)

        document = html_document(
            entries,
            "2026-08-18",
            "2031-08-19",
            "SR 837 & SR 101",
            5,
        )
        self.assertIn("Massnahmen &amp; Tests", document)
        self.assertIn("SR 837 &amp; SR 101", document)
        self.assertIn("bis 5 Jahre nach dem jeweiligen Abruf", document)
        self.assertNotIn("2031-08-19", document)
        self.assertNotIn("Generiert", document)

    def test_french_output_uses_french_title_authority_and_links(self) -> None:
        consolidation = Consolidation(
            effective_date="2026-08-18",
            sr_number="946.231.143.6",
            title="Ordonnance sur les mesures",
            abstract_uri=self.consolidation.abstract_uri,
            language_code="fr",
        )
        impact = Impact(
            effective_date="2026-08-18",
            abstract_uri=self.consolidation.abstract_uri,
            amendment_date="2026-08-17",
            publication_date="2026-08-18",
            as_number="422",
            act_uri="https://fedlex.data.admin.ch/eli/oc/2026/422",
            authorities=("Secrétariat d’État à l’économie",),
            target_subdivisions=(
                "https://fedlex.data.admin.ch/eli/cc/2025/838/art_1",
            ),
            language_code="fr",
        )
        entries = merge_entries([consolidation], [impact])
        self.assertEqual(entries[0].as_reference, "RO 2026 422")
        self.assertIn("Titre (français)", csv_document(entries, "fr"))

        document = html_document(
            entries,
            "2026-08-18",
            "2031-08-19",
            "Sélection de test",
            5,
            "fr",
        )
        self.assertIn('<html lang="fr">', document)
        self.assertIn("Ordonnance sur les mesures", document)
        self.assertIn("Secrétariat d’État à l’économie", document)
        self.assertIn("/fr\"", document)
        self.assertIn("5 ans après chaque consultation", document)

    def test_language_coverage_must_match_german_rows(self) -> None:
        entries = merge_entries([self.consolidation], [self.impact])
        with self.assertRaises(MonitorError):
            validate_entry_language_coverage(entries)


if __name__ == "__main__":
    unittest.main()
