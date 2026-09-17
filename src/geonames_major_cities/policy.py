from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

from .models import DatasetError, LocationRecord, ReviewFinding, normalized_name


MIN_GENERAL_CITY_POPULATION = 50_000
ALWAYS_INCLUDED_CITY_CODES = {"PPLC", "PPLA", "PPLA2"}
CHINA_CITY_CANDIDATE_CODES = {"PPL", "PPLA", "PPLA2", "PPLA3"}
CHINA_DIRECT_MUNICIPALITY_ADMIN1_CODES = {"22", "23", "28", "33"}

# The policy file uses Chinese GB/T 2260 province prefixes. GeoNames uses its
# own ADM1 codes, so this explicit bridge is intentionally version controlled.
CHINA_PROVINCE_TO_GEONAMES_ADMIN1 = {
    "13": "10",
    "14": "24",
    "15": "20",
    "21": "19",
    "22": "05",
    "23": "08",
    "32": "04",
    "33": "02",
    "34": "01",
    "35": "07",
    "36": "03",
    "37": "25",
    "41": "09",
    "42": "12",
    "43": "11",
    "44": "30",
    "45": "16",
    "46": "31",
    "51": "32",
    "52": "18",
    "53": "29",
    "54": "14",
    "61": "26",
    "62": "15",
    "63": "06",
    "64": "21",
    "65": "13",
}

CITY_REASON_PRIORITY = {
    "CHINA_OFFICIAL_PREFECTURE_LEVEL_CITY": 0,
    "NATIONAL_CAPITAL": 1,
    "ADMIN1_CAPITAL": 2,
    "ADMIN2_CAPITAL": 3,
    "POPULATION_AT_LEAST_50000": 4,
}


def city_selection_reason(
    country_code: str,
    admin1_code: str,
    feature_code: str,
    population: int,
) -> str | None:
    if country_code == "CN":
        if admin1_code in CHINA_DIRECT_MUNICIPALITY_ADMIN1_CODES:
            return None
        return (
            "CHINA_CITY_POLICY_CANDIDATE"
            if feature_code in CHINA_CITY_CANDIDATE_CODES
            else None
        )
    if feature_code == "PPLC":
        return "NATIONAL_CAPITAL"
    if feature_code == "PPLA":
        return "ADMIN1_CAPITAL"
    if feature_code == "PPLA2":
        return "ADMIN2_CAPITAL"
    if feature_code == "PPL" and population >= MIN_GENERAL_CITY_POPULATION:
        return "POPULATION_AT_LEAST_50000"
    return None


def parse_china_city_policy(path: Path) -> dict[str, dict[str, str]]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetError(f"cannot read China city policy: {path}") from error
    if not isinstance(rows, list):
        raise DatasetError("China city policy must be a JSON array")

    policy: dict[str, dict[str, str]] = defaultdict(dict)
    for row in rows:
        code = str(row.get("c", ""))
        name = str(row.get("n", "")).strip()
        province_code = str(row.get("p", ""))
        admin1_code = CHINA_PROVINCE_TO_GEONAMES_ADMIN1.get(province_code)
        # Four-digit codes are prefecture-level entries. Six/twelve-digit
        # entries are counties, districts, or other lower-level divisions.
        if len(code) != 4 or not name.endswith("市") or admin1_code is None:
            continue
        policy[admin1_code][name.removesuffix("市")] = code
    return dict(policy)


def select_china_city_ids(
    candidates: Mapping[int, dict],
    china_names: Mapping[int, set[str]],
    policy: Mapping[str, Mapping[str, str]],
    overrides: Mapping[str, int],
) -> tuple[set[int], dict[int, str], list[ReviewFinding]]:
    candidates_by_name: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for source_id, city in candidates.items():
        if city["country_code"] != "CN":
            continue
        for name in china_names.get(source_id, set()):
            candidates_by_name[(city["admin1_code"], name)].append(city)

    selected_ids: set[int] = set()
    official_codes: dict[int, str] = {}
    findings: list[ReviewFinding] = []
    feature_rank = {"PPLA": 0, "PPLA2": 1, "PPLA3": 2, "PPL": 3}
    for admin1_code, official_cities in policy.items():
        for official_name, official_code in official_cities.items():
            override_source_id = overrides.get(official_code)
            if override_source_id is not None:
                override = candidates.get(override_source_id)
                if (
                    override is None
                    or override["country_code"] != "CN"
                    or override["admin1_code"] != admin1_code
                ):
                    findings.append(
                        ReviewFinding(
                            "CHINA_CITY_OVERRIDE_INVALID",
                            "CITY",
                            override_source_id,
                            None,
                            official_name,
                            f"officialCode={official_code}, admin1={admin1_code}",
                        )
                    )
                    continue
                selected_ids.add(override_source_id)
                official_codes[override_source_id] = official_code
                continue

            matches = candidates_by_name.get((admin1_code, official_name), [])
            if not matches:
                findings.append(
                    ReviewFinding(
                        "CHINA_OFFICIAL_CITY_UNMATCHED",
                        "CITY",
                        0,
                        None,
                        official_name,
                        f"officialCode={official_code}, admin1={admin1_code}",
                    )
                )
                continue
            matches.sort(
                key=lambda city: (
                    feature_rank.get(city["feature_code"], 9),
                    -city["population"],
                    city["source_id"],
                )
            )
            selected = matches[0]
            selected_ids.add(selected["source_id"])
            official_codes[selected["source_id"]] = official_code
    return selected_ids, official_codes, findings


def deduplicate_city_records(
    records: Sequence[LocationRecord],
) -> tuple[list[LocationRecord], int]:
    """Keep one display option for each normalized name under one parent."""
    grouped: dict[tuple[int | None, str], list[LocationRecord]] = defaultdict(list)
    for record in records:
        grouped[(record.parentSourceId, normalized_name(record.name))].append(record)

    selected: list[LocationRecord] = []
    removed = 0
    for duplicates in grouped.values():
        duplicates.sort(
            key=lambda record: (
                CITY_REASON_PRIORITY.get(record.selectionReason, 99),
                -(record.population or 0),
                record.sourceId,
            )
        )
        selected.append(duplicates[0])
        removed += len(duplicates) - 1
    return selected, removed
