from __future__ import annotations

import gzip
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

from geonames_major_cities.builder import build_dataset
from geonames_major_cities.policy import city_selection_reason
from geonames_major_cities.sources import sha256_file


class DatasetBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.data_dir = self.root / "data"
        self.output_dir = self.root / "output"
        self.data_dir.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_city_policy(self) -> None:
        self.assertEqual(
            "CHINA_CITY_POLICY_CANDIDATE",
            city_selection_reason("CN", "02", "PPLA3", 10),
        )
        self.assertIsNone(city_selection_reason("CN", "22", "PPLC", 20_000_000))
        self.assertEqual(
            "ADMIN2_CAPITAL",
            city_selection_reason("AX", "211", "PPLA2", 515),
        )
        self.assertEqual(
            "POPULATION_AT_LEAST_50000",
            city_selection_reason("US", "CA", "PPL", 50_000),
        )
        self.assertIsNone(city_selection_reason("US", "CA", "PPL", 49_999))

    def test_builds_reviewable_outputs_without_counties_or_duplicates(self) -> None:
        manifest = self.write_fixture()

        report = build_dataset(manifest, self.data_dir, self.output_dir)

        self.assertEqual("PASS", report["qualityStatus"])
        self.assertEqual(
            {"COUNTRY": 3, "SUBDIVISION": 3, "CITY": 4}, report["counts"]
        )
        self.assertEqual(1, report["duplicateCityOptionsRemoved"])
        self.assertTrue(report["policy"]["translationsIncluded"])
        self.assertGreaterEqual(report["languageCount"], 4)
        self.assertGreaterEqual(report["localizedNameCount"], 10)
        csv_text = self.gzip_text(self.output_dir / "locations.csv.gz")
        self.assertIn("Hangzhou", csv_text)
        self.assertIn("Ningbo", csv_text)
        self.assertIn("Los Angeles", csv_text)
        self.assertIn("Macau", csv_text)
        self.assertNotIn("Deqing", csv_text)
        self.assertNotIn("Yiwu", csv_text)
        self.assertNotIn("Beijing City", csv_text)
        self.assertEqual(1, csv_text.count("Los Angeles"))
        names_text = self.gzip_text(self.output_dir / "location-names.csv.gz")
        self.assertIn("COUNTRY,1814991,zh,中国,True", names_text)
        self.assertIn("SUBDIVISION,1784764,zh,浙江省,True", names_text)
        self.assertIn("CITY,1808926,zh-CN,杭州市,True", names_text)
        self.assertNotIn("https://example.invalid", names_text)
        self.assertNotIn(",link,", names_text)
        self.assertNotIn(",LA,", names_text)
        self.assertNotIn("Old Los Angeles", names_text)

        html = (self.output_dir / "review.html").read_text(encoding="utf-8")
        self.assertIn("double-click it", html)
        self.assertIn("id=\"language\"", html)
        self.assertIn("id=\"city\" size=\"12\"", html)
        self.assertNotIn("<script src=", html)
        self.assertNotIn("fetch(", html)

        connection = sqlite3.connect(self.output_dir / "locations.sqlite3")
        try:
            self.assertEqual(
                ("Hangzhou", "Zhejiang", "China"),
                connection.execute(
                    "SELECT city_name, subdivision_name, country_name "
                    "FROM city_flat WHERE city_name = 'Hangzhou'"
                ).fetchone(),
            )
            self.assertEqual(
                0, connection.execute("SELECT COUNT(*) FROM duplicate_check").fetchone()[0]
            )
            self.assertEqual(
                0, connection.execute("SELECT COUNT(*) FROM orphan_check").fetchone()[0]
            )
            self.assertEqual(
                "中国",
                connection.execute(
                    "SELECT name FROM location_names "
                    "WHERE location_type='COUNTRY' AND source_id=1814991 "
                    "AND language_code='zh'"
                ).fetchone()[0],
            )
            self.assertEqual(
                "Hangzhou",
                connection.execute(
                    "SELECT name FROM location_names "
                    "WHERE location_type='CITY' AND source_id=1808926 "
                    "AND language_code='en'"
                ).fetchone()[0],
            )
        finally:
            connection.close()

    def test_csv_output_is_reproducible(self) -> None:
        manifest = self.write_fixture()
        first = self.root / "first"
        second = self.root / "second"

        build_dataset(manifest, self.data_dir, first)
        build_dataset(manifest, self.data_dir, second)

        self.assertEqual(
            sha256_file(first / "locations.csv.gz"),
            sha256_file(second / "locations.csv.gz"),
        )
        self.assertEqual(
            sha256_file(first / "location-names.csv.gz"),
            sha256_file(second / "location-names.csv.gz"),
        )

    def write_fixture(self) -> dict:
        countries = self.data_dir / "countryInfo.txt"
        countries.write_text(
            "#ISO\tISO3\tISO-Numeric\tfips\tCountry\tCapital\tArea\tPopulation\t"
            "Continent\ttld\tCurrencyCode\tCurrencyName\tPhone\tPostal\tRegex\t"
            "Languages\tgeonameid\tneighbours\tEquivalent\n"
            "CN\tCHN\t156\tCH\tChina\tBeijing\t1\t1400000000\tAS\t.cn\tCNY\t"
            "Yuan\t86\t\t\tzh\t1814991\t\t\n"
            "US\tUSA\t840\tUS\tUnited States\tWashington\t1\t330000000\tNA\t.us\t"
            "USD\tDollar\t1\t\t\ten\t6252001\t\t\n"
            "MO\tMAC\t446\tMC\tMacao\tMacao\t1\t650000\tAS\t.mo\tMOP\t"
            "Pataca\t853\t\t\tzh\t1821275\t\t\n"
            "AN\tANT\t530\tNT\tNetherlands Antilles\tWillemstad\t1\t0\tNA\t.an\t"
            "ANG\tGuilder\t599\t\t\tnl\t8505032\t\t\n",
            encoding="utf-8",
        )
        subdivisions = self.data_dir / "admin1CodesASCII.txt"
        subdivisions.write_text(
            "CN.02\tZhejiang\tZhejiang\t1784764\n"
            "CN.22\tBeijing\tBeijing\t2038349\n"
            "US.CA\tCalifornia\tCalifornia\t5332921\n",
            encoding="utf-8",
        )
        cities = self.data_dir / "cities500.zip"
        self.write_zip(
            cities,
            "cities500.txt",
            "".join(
                [
                    self.city_row(1808926, "Hangzhou", "PPLA", "CN", "02", 9_000_000),
                    self.city_row(1799397, "Ningbo", "PPLA2", "CN", "02", 4_000_000),
                    self.city_row(1812966, "Deqing", "PPLA3", "CN", "02", 100_000),
                    self.city_row(1805528, "Yiwu", "PPLA3", "CN", "02", 1_000_000),
                    self.city_row(1816670, "Beijing City", "PPLC", "CN", "22", 20_000_000),
                    self.city_row(5368361, "Los Angeles", "PPL", "US", "CA", 3_800_000),
                    self.city_row(5368362, "Los Ángeles", "PPL", "US", "CA", 100_000),
                    self.city_row(999001, "Smallville", "PPL", "US", "CA", 49_999),
                    self.city_row(1821274, "Macau", "PPLC", "MO", "02", 649_335),
                ]
            ),
        )
        alternate_names = self.data_dir / "alternateNamesV2.zip"
        self.write_zip(
            alternate_names,
            "alternateNamesV2.txt",
            "1\t1808926\ten\tHangchow\t\t\t\t\t\n"
            "2\t1808926\ten\tHangzhou\t1\t\t\t\t\n"
            "11\t1808926\tzh-CN\t杭州市\t1\t\t\t\t\n"
            "21\t1808926\tzh\t杭州\t1\t\t\t\t\n"
            "3\t1799397\ten\tNingbo\t1\t\t\t\t\n"
            "12\t1799397\tzh-CN\t宁波市\t1\t\t\t\t\n"
            "13\t1812966\tzh-CN\t德清县\t1\t\t\t\t\n"
            "14\t1805528\tzh-CN\t义乌市\t1\t\t\t\t\n"
            "4\t5368361\ten\tLos Angeles\t1\t\t\t\t\n"
            "5\t5368362\ten\tLos Angeles\t1\t\t\t\t\n"
            "6\t1821274\ten\tMacau\t1\t\t\t\t\n"
            "30\t1814991\tzh\t中国\t1\t\t\t\t\n"
            "31\t6252001\tes\tEstados Unidos\t1\t\t\t\t\n"
            "32\t1784764\tzh\t浙江省\t1\t\t\t\t\n"
            "33\t5332921\tes\tCalifornia\t1\t\t\t\t\n"
            "40\t5368361\tlink\thttps://example.invalid\t1\t\t\t\t\n"
            "41\t5368361\ten\tLA\t\t1\t\t\t\n"
            "42\t5368361\ten\tOld Los Angeles\t\t\t\t1\t\n",
        )
        china_cities = self.data_dir / "china-prefecture-city-policy.json"
        china_cities.write_text(
            json.dumps(
                [
                    {"c": "3301", "n": "杭州市", "p": "33"},
                    {"c": "3302", "n": "宁波市", "p": "33"},
                    {"c": "330681", "n": "义乌市", "p": "33"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        assets = {
            "countries": self.asset(countries),
            "subdivisions": self.asset(subdivisions),
            "cities": self.asset(cities),
            "alternateNames": self.asset(alternate_names),
            "chinaCities": self.asset(china_cities),
        }
        return {
            "source": "GeoNames",
            "sourceVersion": "fixture",
            "assets": assets,
            "qualityChecks": {
                "excludedCountryCodes": ["AN"],
                "requiredCityNamesBySubdivisionCode": {
                    "CN.02": ["Hangzhou", "Ningbo"]
                },
                "forbiddenCityNamesBySubdivisionCode": {
                    "CN.02": ["Deqing", "Yiwu"]
                },
            },
        }

    @staticmethod
    def asset(path: Path) -> dict:
        return {
            "fileName": path.name,
            "url": "https://example.invalid/" + path.name,
            "sha256": sha256_file(path),
            "size": path.stat().st_size,
        }

    @staticmethod
    def city_row(
        source_id: int,
        name: str,
        feature_code: str,
        country: str,
        admin1: str,
        population: int,
    ) -> str:
        fields = [
            str(source_id),
            name,
            name,
            "",
            "30.0",
            "120.0",
            "P",
            feature_code,
            country,
            "",
            admin1,
            "",
            "",
            "",
            str(population),
            "",
            "Asia/Shanghai",
            "2026-09-17",
            "",
        ]
        return "\t".join(fields) + "\n"

    @staticmethod
    def write_zip(path: Path, name: str, value: str) -> None:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(name, value)

    @staticmethod
    def gzip_text(path: Path) -> str:
        with gzip.open(path, "rt", encoding="utf-8") as source:
            return source.read()


if __name__ == "__main__":
    unittest.main()
