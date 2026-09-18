from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from .models import (
    DatasetError,
    LocalizedName,
    LocationRecord,
    ReviewFinding,
    normalized_name,
)
from .outputs import write_outputs
from .policy import (
    ALWAYS_INCLUDED_CITY_CODES,
    CHINA_CITY_CANDIDATE_CODES,
    MIN_GENERAL_CITY_POPULATION,
    city_selection_reason,
    deduplicate_city_records,
    parse_china_city_policy,
    select_china_city_ids,
)
from .sources import source_path, text_lines, verify_assets, zip_text_lines


def parse_countries(
    path: Path, excluded_country_codes: set[str]
) -> tuple[list[LocationRecord], dict[str, int]]:
    records: list[LocationRecord] = []
    ids: dict[str, int] = {}
    for line in text_lines(path):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.rstrip("\n").split("\t")
        if len(fields) < 17:
            raise DatasetError("countryInfo.txt has an unexpected row")
        country_code = fields[0]
        if country_code in excluded_country_codes:
            continue
        name = fields[4].strip()
        source_id = int(fields[16])
        ids[country_code] = source_id
        records.append(
            LocationRecord(
                "COUNTRY",
                source_id,
                None,
                None,
                country_code,
                name,
                country_code,
                None,
                None,
                None,
                int(fields[7]) if fields[7] else None,
                "PCLI",
                "GEONAMES_COUNTRY_INFO",
            )
        )
    return records, ids


def parse_subdivisions(
    path: Path, country_ids: Mapping[str, int]
) -> tuple[list[LocationRecord], dict[str, int]]:
    records: list[LocationRecord] = []
    ids: dict[str, int] = {}
    for line in text_lines(path):
        if not line.strip():
            continue
        fields = line.rstrip("\n").split("\t")
        if len(fields) != 4 or "." not in fields[0]:
            raise DatasetError("admin1CodesASCII.txt has an unexpected row")
        key, name, _ascii_name, source_id_value = fields
        country_code, admin1_code = key.split(".", 1)
        country_id = country_ids.get(country_code)
        if country_id is None:
            continue
        source_id = int(source_id_value)
        ids[key] = source_id
        records.append(
            LocationRecord(
                "SUBDIVISION",
                source_id,
                "COUNTRY",
                country_id,
                key,
                name.strip(),
                country_code,
                admin1_code,
                None,
                None,
                None,
                "ADM1",
                "GEONAMES_ADMIN1",
            )
        )
    return records, ids


def parse_city_candidates(
    path: Path,
    country_ids: Mapping[str, int],
    subdivision_ids: Mapping[str, int],
) -> tuple[dict[int, dict], Counter]:
    candidates: dict[int, dict] = {}
    filtered: Counter = Counter()
    countries_with_subdivisions = {
        key.split(".", 1)[0] for key in subdivision_ids
    }
    for line in zip_text_lines(path):
        fields = line.rstrip("\n").split("\t")
        if len(fields) < 19:
            raise DatasetError("cities500.txt has an unexpected row")
        source_id = int(fields[0])
        country_code = fields[8]
        admin1_code = fields[10]
        feature_class = fields[6]
        feature_code = fields[7]
        population = int(fields[14] or 0)
        if feature_class != "P":
            filtered["NON_POPULATED_PLACE"] += 1
            continue
        reason = city_selection_reason(
            country_code, admin1_code, feature_code, population
        )
        if reason is None:
            filtered["NOT_MAJOR_CITY"] += 1
            continue
        country_id = country_ids.get(country_code)
        if country_id is None:
            filtered["MISSING_COUNTRY"] += 1
            continue

        subdivision_key = f"{country_code}.{admin1_code}"
        parent_id = subdivision_ids.get(subdivision_key)
        parent_type = "SUBDIVISION"
        if parent_id is None:
            can_use_country_parent = (
                country_code not in countries_with_subdivisions
                and feature_code in {"PPLC", "PPLA"}
            )
            if admin1_code not in {"", "00"} and not can_use_country_parent:
                filtered["MISSING_SUBDIVISION"] += 1
                continue
            parent_id = country_id
            parent_type = "COUNTRY"

        candidates[source_id] = {
            "source_id": source_id,
            "name": fields[1].strip(),
            "ascii_name": fields[2].strip(),
            "latitude": fields[4],
            "longitude": fields[5],
            "feature_code": feature_code,
            "country_code": country_code,
            "admin1_code": admin1_code or None,
            "population": population,
            "parent_id": parent_id,
            "parent_type": parent_type,
            "reason": reason,
        }
    return candidates, filtered


VALID_LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def canonical_language_code(value: str) -> str | None:
    if not VALID_LANGUAGE_CODE.fullmatch(value):
        return None
    parts = value.split("-")
    canonical = [parts[0].lower()]
    for part in parts[1:]:
        if len(part) == 2 and part.isalpha():
            canonical.append(part.upper())
        elif len(part) == 4 and part.isalpha():
            canonical.append(part.title())
        else:
            canonical.append(part)
    return "-".join(canonical)


def parse_names(
    path: Path, eligible_ids: set[int], china_candidate_ids: set[int]
) -> tuple[
    dict[int, str],
    dict[int, set[str]],
    dict[tuple[int, str], tuple[str, bool]],
]:
    best: dict[tuple[int, str], tuple[int, int, str, bool]] = {}
    chinese: dict[int, set[str]] = defaultdict(set)
    for line in zip_text_lines(path):
        fields = line.rstrip("\n").split("\t")
        if len(fields) < 4:
            continue
        try:
            source_id = int(fields[1])
        except ValueError:
            continue
        if source_id not in eligible_ids:
            continue
        fields += [""] * (8 - len(fields))
        raw_language = fields[2].strip()
        is_short = fields[5] == "1"
        is_colloquial = fields[6] == "1"
        is_historic = fields[7] == "1"
        if is_colloquial or is_historic:
            continue
        alternate_name = fields[3].strip()
        if (
            source_id in china_candidate_ids
            and raw_language in {"zh", "zh-CN"}
            and alternate_name
        ):
            chinese[source_id].add(alternate_name.removesuffix("市"))
        language = canonical_language_code(raw_language)
        if language is None or is_short or not alternate_name:
            continue
        preferred = fields[4] == "1"
        try:
            alternate_id = int(fields[0])
        except ValueError:
            continue
        candidate = (0 if preferred else 1, alternate_id, alternate_name, preferred)
        key = (source_id, language)
        if key not in best or candidate[:2] < best[key][:2]:
            best[key] = candidate

    selected = {
        key: (value[2], value[3]) for key, value in best.items()
    }
    english = {
        source_id: name
        for (source_id, language), (name, _preferred) in selected.items()
        if language == "en"
    }
    return english, chinese, selected


def apply_english_names(
    records: Sequence[LocationRecord], english_names: Mapping[int, str]
) -> list[LocationRecord]:
    return [
        replace(record, name=english_names.get(record.sourceId, record.name))
        for record in records
    ]


def build_localized_names(
    records: Sequence[LocationRecord],
    selected_names: Mapping[tuple[int, str], tuple[str, bool]],
) -> list[LocalizedName]:
    type_by_source_id = {record.sourceId: record.type for record in records}
    type_order = {"COUNTRY": 0, "SUBDIVISION": 1, "CITY": 2}
    names = [
        LocalizedName(
            type_by_source_id[source_id], source_id, language, name, preferred
        )
        for (source_id, language), (name, preferred) in selected_names.items()
        if source_id in type_by_source_id
    ]
    return sorted(
        names,
        key=lambda value: (
            type_order[value.type],
            value.sourceId,
            value.languageCode,
        ),
    )


def build_city_records(
    candidates: Mapping[int, dict],
    english_names: Mapping[int, str],
    china_city_ids: set[int],
    china_official_codes: Mapping[int, str],
) -> list[LocationRecord]:
    records: list[LocationRecord] = []
    for source_id, city in candidates.items():
        if city["country_code"] == "CN" and source_id not in china_city_ids:
            continue
        # Prefer an explicit English alternate name. GeoNames' primary name is
        # retained as a proper name when no English alternate exists; this is
        # why valid diacritics such as Brändö are not transliterated away.
        name = english_names.get(source_id) or city["name"] or city["ascii_name"]
        reason = city["reason"]
        code = None
        if city["country_code"] == "CN":
            reason = "CHINA_OFFICIAL_PREFECTURE_LEVEL_CITY"
            code = "CN-" + china_official_codes[source_id]
        records.append(
            LocationRecord(
                "CITY",
                source_id,
                city["parent_type"],
                city["parent_id"],
                code,
                name.strip(),
                city["country_code"],
                city["admin1_code"],
                city["latitude"],
                city["longitude"],
                city["population"],
                city["feature_code"],
                reason,
            )
        )
    return records


def review_records(records: Sequence[LocationRecord]) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    ids = {(record.type, record.sourceId) for record in records}
    seen_keys: set[tuple[str, int]] = set()
    names_by_parent: dict[tuple[str, int | None, str], list[LocationRecord]] = (
        defaultdict(list)
    )
    for record in records:
        key = (record.type, record.sourceId)
        if key in seen_keys:
            findings.append(
                ReviewFinding(
                    "DUPLICATE_SOURCE_ID",
                    record.type,
                    record.sourceId,
                    record.parentSourceId,
                    record.name,
                    "duplicate type/sourceId",
                )
            )
        seen_keys.add(key)
        if not record.name or any(ord(character) < 32 for character in record.name):
            findings.append(
                ReviewFinding(
                    "INVALID_NAME",
                    record.type,
                    record.sourceId,
                    record.parentSourceId,
                    record.name,
                    "blank or control character",
                )
            )
        if record.type == "CITY" and (
            record.latitude is None or record.longitude is None
        ):
            findings.append(
                ReviewFinding(
                    "MISSING_COORDINATES",
                    record.type,
                    record.sourceId,
                    record.parentSourceId,
                    record.name,
                    "city is missing latitude or longitude",
                )
            )
        if record.parentType is not None and (
            record.parentType,
            record.parentSourceId,
        ) not in ids:
            findings.append(
                ReviewFinding(
                    "ORPHAN",
                    record.type,
                    record.sourceId,
                    record.parentSourceId,
                    record.name,
                    f"missing {record.parentType} parent",
                )
            )
        names_by_parent[
            (record.type, record.parentSourceId, normalized_name(record.name))
        ].append(record)

    for duplicates in names_by_parent.values():
        if len(duplicates) <= 1:
            continue
        for record in duplicates:
            findings.append(
                ReviewFinding(
                    "DUPLICATE_DISPLAY_NAME",
                    record.type,
                    record.sourceId,
                    record.parentSourceId,
                    record.name,
                    f"{len(duplicates)} normalized matches under the same parent",
                )
            )
    return findings


def review_localized_names(
    records: Sequence[LocationRecord], names: Sequence[LocalizedName]
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    record_keys = {(record.type, record.sourceId) for record in records}
    seen: set[tuple[str, int, str]] = set()
    for name in names:
        key = (name.type, name.sourceId, name.languageCode)
        if key in seen:
            findings.append(
                ReviewFinding(
                    "DUPLICATE_LOCALIZED_NAME",
                    name.type,
                    name.sourceId,
                    None,
                    name.name,
                    f"duplicate language={name.languageCode}",
                )
            )
        seen.add(key)
        if (name.type, name.sourceId) not in record_keys:
            findings.append(
                ReviewFinding(
                    "ORPHAN_LOCALIZED_NAME",
                    name.type,
                    name.sourceId,
                    None,
                    name.name,
                    f"language={name.languageCode}",
                )
            )
        if not name.name or any(ord(character) < 32 for character in name.name):
            findings.append(
                ReviewFinding(
                    "INVALID_LOCALIZED_NAME",
                    name.type,
                    name.sourceId,
                    None,
                    name.name,
                    f"language={name.languageCode}",
                )
            )
    return findings


def review_policy_sentinels(
    records: Sequence[LocationRecord], quality_checks: Mapping
) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    subdivision_ids = {
        record.code: record.sourceId
        for record in records
        if record.type == "SUBDIVISION" and record.code
    }
    city_names_by_parent: dict[int, set[str]] = defaultdict(set)
    for record in records:
        if record.type == "CITY" and record.parentSourceId is not None:
            city_names_by_parent[record.parentSourceId].add(record.name)

    for code, required_names in quality_checks.get(
        "requiredCityNamesBySubdivisionCode", {}
    ).items():
        parent_id = subdivision_ids.get(code)
        for name in required_names:
            if parent_id is None or name not in city_names_by_parent[parent_id]:
                findings.append(
                    ReviewFinding(
                        "REQUIRED_CITY_MISSING",
                        "CITY",
                        0,
                        parent_id,
                        name,
                        f"required by sentinel for {code}",
                    )
                )
    for code, forbidden_names in quality_checks.get(
        "forbiddenCityNamesBySubdivisionCode", {}
    ).items():
        parent_id = subdivision_ids.get(code)
        for name in forbidden_names:
            if parent_id is not None and name in city_names_by_parent[parent_id]:
                findings.append(
                    ReviewFinding(
                        "FORBIDDEN_CITY_PRESENT",
                        "CITY",
                        0,
                        parent_id,
                        name,
                        f"forbidden by sentinel for {code}",
                    )
                )
    return findings


def build_dataset(manifest: Mapping, data_dir: Path, output_dir: Path) -> dict:
    verify_assets(manifest, data_dir)
    quality_checks = manifest.get("qualityChecks", {})
    excluded_country_codes = set(quality_checks.get("excludedCountryCodes", []))
    countries, country_ids = parse_countries(
        source_path(manifest, data_dir, "countries"), excluded_country_codes
    )
    subdivisions, subdivision_ids = parse_subdivisions(
        source_path(manifest, data_dir, "subdivisions"), country_ids
    )
    candidates, filtered = parse_city_candidates(
        source_path(manifest, data_dir, "cities"), country_ids, subdivision_ids
    )
    china_candidate_ids = {
        source_id
        for source_id, city in candidates.items()
        if city["country_code"] == "CN"
    }
    eligible_name_ids = (
        set(country_ids.values()) | set(subdivision_ids.values()) | set(candidates)
    )
    english_names, china_names, selected_names = parse_names(
        source_path(manifest, data_dir, "alternateNames"),
        eligible_name_ids,
        china_candidate_ids,
    )
    china_policy = parse_china_city_policy(
        source_path(manifest, data_dir, "chinaCities")
    )
    overrides = {
        str(code): int(source_id)
        for code, source_id in quality_checks.get(
            "chinaCityGeoNamesOverrides", {}
        ).items()
    }
    china_city_ids, china_official_codes, china_findings = select_china_city_ids(
        candidates, china_names, china_policy, overrides
    )
    cities, duplicate_cities_removed = deduplicate_city_records(
        build_city_records(
            candidates,
            english_names,
            china_city_ids,
            china_official_codes,
        )
    )
    countries = apply_english_names(countries, english_names)
    subdivisions = apply_english_names(subdivisions, english_names)
    type_order = {"COUNTRY": 0, "SUBDIVISION": 1, "CITY": 2}
    records = sorted(
        countries + subdivisions + cities,
        key=lambda value: (
            type_order[value.type],
            value.countryCode,
            value.parentSourceId or 0,
            value.name.casefold(),
            value.sourceId,
        ),
    )
    localized_names = build_localized_names(records, selected_names)
    findings = review_records(records)
    findings.extend(review_localized_names(records, localized_names))
    findings.extend(china_findings)
    findings.extend(review_policy_sentinels(records, quality_checks))
    findings.sort(
        key=lambda value: (
            value.kind,
            value.type,
            value.parentSourceId or 0,
            value.name,
            value.sourceId,
        )
    )
    counts = Counter(record.type for record in records)
    coverage: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for name in localized_names:
        coverage[name.languageCode].add((name.type, name.sourceId))
    language_coverage = {
        language: len(location_keys)
        for language, location_keys in sorted(
            coverage.items(), key=lambda item: (-len(item[1]), item[0])
        )
    }
    translated_location_keys = set().union(*coverage.values()) if coverage else set()
    report = {
        "source": manifest["source"],
        "sourceVersion": manifest["sourceVersion"],
        "policy": {
            "labels": "English alternate name preferred; GeoNames primary name otherwise",
            "generalCityPopulationMinimum": MIN_GENERAL_CITY_POPULATION,
            "alwaysIncludedFeatureCodes": sorted(ALWAYS_INCLUDED_CITY_CODES),
            "chinaCandidateFeatureCodes": sorted(CHINA_CITY_CANDIDATE_CODES),
            "chinaCitySelection": "PINNED_PREFECTURE_LEVEL_CITY_POLICY",
            "chinaDirectMunicipalitiesAreTerminalSubdivisions": True,
            "excludedCountryCodes": sorted(excluded_country_codes),
            "translationsIncluded": True,
            "localizedNameSelection": (
                "preferred non-short, non-colloquial, non-historic name; "
                "lowest alternate-name ID as deterministic fallback"
            ),
            "runtimeFallback": "exact locale, base language, English, primary name",
        },
        "counts": {
            key: counts.get(key, 0) for key in ("COUNTRY", "SUBDIVISION", "CITY")
        },
        "englishPreferredNameCount": sum(
            1 for record in records if (record.sourceId, "en") in selected_names
        ),
        "localizedNameCount": len(localized_names),
        "languageCount": len(language_coverage),
        "translatedLocationCount": len(translated_location_keys),
        "languageCoverage": language_coverage,
        "chinaOfficialPrefectureCityCount": sum(
            len(values) for values in china_policy.values()
        ),
        "chinaMatchedPrefectureCityCount": len(china_city_ids),
        "filteredCityCounts": dict(sorted(filtered.items())),
        "duplicateCityOptionsRemoved": duplicate_cities_removed,
        "findingCounts": dict(
            sorted(Counter(value.kind for value in findings).items())
        ),
        "qualityStatus": "PASS" if not findings else "REVIEW_REQUIRED",
        "assets": {
            key: {
                "fileName": value["fileName"],
                "url": value["url"],
                "sha256": value["sha256"],
            }
            for key, value in manifest["assets"].items()
        },
    }
    return write_outputs(output_dir, records, localized_names, findings, report)
