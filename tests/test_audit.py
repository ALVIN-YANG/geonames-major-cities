from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from geonames_major_cities.audit import (
    Location,
    analyze_candidates,
    extract_wikidata_links,
    overture_rows,
    wof_rows,
)


class SourceAuditTest(unittest.TestCase):
    def test_extracts_only_exact_wikidata_links_for_selected_locations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alternateNamesV2.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "alternateNamesV2.txt",
                    "1\t1808926\twkdt\tQ4970\t\t\t\t\t\n"
                    "2\t1808926\tzh\t杭州市\t1\t\t\t\t\n"
                    "3\t999\twkdt\tQ123\t\t\t\t\t\n"
                    "4\t1808926\twkdt\tnot-a-qid\t\t\t\t\t\n",
                )

            links, conflicts = extract_wikidata_links(path, {1808926})

        self.assertEqual({1808926: "Q4970"}, links)
        self.assertEqual([], conflicts)

    def test_wof_prefers_city_locality_and_reports_real_gains(self) -> None:
        locations = {
            1808926: Location(1808926, "CITY", "Hangzhou", "CN"),
        }
        raw_rows = [
            {
                "gn_id": "1808926",
                "id": 1,
                "placetype": "localadmin",
                "country": "CN",
                "name_eng": "Hangzhou",
                "name_zho": "杭州",
            },
            {
                "gn_id": "1808926",
                "id": 2,
                "placetype": "locality",
                "country": "CN",
                "name_eng": "Hangzhou",
                "name_zho": "杭州市",
                "name_jpn": "杭州市",
            },
        ]

        report = analyze_candidates(
            "wof",
            locations,
            {1808926: {"en": "Hangzhou"}},
            wof_rows(raw_rows, locations),
        )

        self.assertEqual(1, report["exactIdentifierMatches"])
        self.assertEqual(1, report["typeAndCountryCompatibleMatches"])
        self.assertEqual(2, report["newLocalizedNamePairs"])
        self.assertEqual(1, report["missingChineseLocationsFilled"])
        self.assertEqual({"zh": 1, "ja": 1}, report["topLanguageGains"])
        self.assertEqual({"localadmin": 1}, report["incompatibleSourceTypes"])

    def test_overture_uses_exact_qid_and_rejects_county_for_city(self) -> None:
        locations = {
            1808926: Location(1808926, "CITY", "Hangzhou", "CN"),
        }
        links = {1808926: "Q4970"}
        rows = [
            {
                "wikidata": "Q4970",
                "id": "county-id",
                "subtype": "county",
                "country": "CN",
                "common_names": {"zh-Hans": "杭州市"},
            },
            {
                "wikidata": "Q4970",
                "id": "locality-id",
                "subtype": "locality",
                "country": "CN",
                "common_names": {"zh-Hans": "杭州市", "en": "Hangzhou"},
            },
        ]

        report = analyze_candidates(
            "overture",
            locations,
            {1808926: {"en": "Hangzhou"}},
            overture_rows(rows, links),
        )

        self.assertEqual(1, report["exactIdentifierMatches"])
        self.assertEqual(1, report["typeAndCountryCompatibleMatches"])
        self.assertEqual(1, report["newLocalizedNamePairs"])
        self.assertEqual({"county": 1}, report["incompatibleSourceTypes"])


if __name__ == "__main__":
    unittest.main()
