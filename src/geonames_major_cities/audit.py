from __future__ import annotations

import gzip
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .builder import canonical_language_code
from .models import DatasetError, normalized_name

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WOF_PARQUET = (
    "https://data.geocode.earth/wof/dist/parquet/whosonfirst-data-admin-latest.parquet"
)
WOF_INVENTORY = "https://data.geocode.earth/wof/dist/parquet/inventory.json"
WOF_LANGUAGES = {
    "ara": "ar",
    "ben": "bn",
    "deu": "de",
    "eng": "en",
    "ell": "el",
    "fas": "fa",
    "fra": "fr",
    "heb": "he",
    "hin": "hi",
    "hun": "hu",
    "ind": "id",
    "ita": "it",
    "jpn": "ja",
    "kor": "ko",
    "nld": "nl",
    "pol": "pl",
    "por": "pt",
    "rus": "ru",
    "spa": "es",
    "swe": "sv",
    "tur": "tr",
    "ukr": "uk",
    "urd": "ur",
    "vie": "vi",
    "zho": "zh",
}
CHINESE_LANGUAGES = {"zh", "zh-CN", "zh-Hans", "zh-SG"}


@dataclass(frozen=True)
class Location:
    source_id: int
    type: str
    name: str
    country_code: str


def _progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def load_dataset(
    path: Path,
) -> tuple[dict[int, Location], dict[int, dict[str, str]], dict[str, str]]:
    if not path.is_file():
        raise DatasetError(f"dataset does not exist: {path}")
    connection = sqlite3.connect(path)
    try:
        locations = {
            int(row[0]): Location(int(row[0]), row[1], row[2], row[3])
            for row in connection.execute(
                "SELECT source_id, type, name, country_code FROM locations"
            )
        }
        names: dict[int, dict[str, str]] = defaultdict(dict)
        for source_id, language, name in connection.execute(
            "SELECT source_id, language_code, name FROM location_names"
        ):
            names[int(source_id)][language] = name
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
    finally:
        connection.close()
    return locations, dict(names), metadata


def extract_wikidata_links(
    path: Path, eligible_ids: set[int]
) -> tuple[dict[int, str], list[dict[str, object]]]:
    if not path.is_file():
        raise DatasetError(f"GeoNames alternate names file does not exist: {path}")
    links: dict[int, str] = {}
    conflicts: list[dict[str, object]] = []
    with zipfile.ZipFile(path) as archive:
        members = [
            name
            for name in archive.namelist()
            if Path(name).name == "alternateNamesV2.txt"
        ]
        if len(members) != 1:
            raise DatasetError(
                f"expected alternateNamesV2.txt in {path}, found {len(members)}"
            )
        with archive.open(members[0]) as raw:
            for binary_line in raw:
                fields = binary_line.decode("utf-8").rstrip("\n").split("\t")
                if len(fields) < 4 or fields[2] != "wkdt":
                    continue
                try:
                    source_id = int(fields[1])
                except ValueError:
                    continue
                qid = fields[3].strip()
                if source_id not in eligible_ids or not _is_qid(qid):
                    continue
                previous = links.get(source_id)
                if previous is not None and previous != qid:
                    conflicts.append(
                        {"sourceId": source_id, "first": previous, "other": qid}
                    )
                    continue
                links[source_id] = qid
    return links, conflicts


def _is_qid(value: str) -> bool:
    return len(value) > 1 and value[0] == "Q" and value[1:].isdigit()


def _read_gzip_json(path: Path) -> object:
    with gzip.open(path, "rt", encoding="utf-8") as source:
        return json.load(source)


def _write_gzip_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with (
        path.open("wb") as raw,
        gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as zipped,
    ):
        zipped.write(
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        )


def _post_wikidata_batch(qids: Sequence[str]) -> dict[str, object]:
    payload = urllib.parse.urlencode(
        {
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "props": "labels|info",
            "format": "json",
            "formatversion": "2",
        }
    ).encode("ascii")
    request = urllib.request.Request(
        WIKIDATA_API,
        data=payload,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "geonames-major-cities/0.3 source-audit",
        },
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read()
                if response.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                document = json.loads(body)
                return document.get("entities", {})
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise
            delay = float(error.headers.get("Retry-After", 2**attempt))
        except (TimeoutError, urllib.error.URLError):
            delay = float(2**attempt)
        time.sleep(min(delay, 30.0))
    raise DatasetError(f"Wikidata request failed after retries: {qids[0]}...")


def load_wikidata(
    qids: Iterable[str], cache_path: Path, refresh: bool, workers: int
) -> dict[str, dict[str, object]]:
    if workers < 1 or workers > 8:
        raise DatasetError("--wikidata-workers must be between 1 and 8")
    wanted = sorted(set(qids), key=lambda value: int(value[1:]))
    cached: dict[str, dict[str, object]] = {}
    if cache_path.is_file() and not refresh:
        document = _read_gzip_json(cache_path)
        if isinstance(document, dict):
            cached = document.get("entities", {})  # type: ignore[assignment]
    missing = [qid for qid in wanted if qid not in cached]
    batches = [missing[index : index + 50] for index in range(0, len(missing), 50)]
    if batches:
        _progress(
            f"Wikidata: fetching {len(missing):,} entities in {len(batches):,} batches"
        )
        completed = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_post_wikidata_batch, batch): batch for batch in batches
            }
            for future in as_completed(futures):
                raw_entities = future.result()
                for qid, entity in raw_entities.items():
                    labels = {
                        language: item["value"]
                        for language, item in entity.get("labels", {}).items()
                        if isinstance(item, dict) and item.get("value")
                    }
                    cached[qid] = {
                        "id": qid,
                        "revision": entity.get("lastrevid"),
                        "modified": entity.get("modified"),
                        "missing": bool(entity.get("missing")),
                        "labels": labels,
                    }
                completed += 1
                if completed % 25 == 0 or completed == len(batches):
                    _progress(f"Wikidata: completed {completed}/{len(batches)} batches")
        _write_gzip_json(
            cache_path,
            {
                "source": WIKIDATA_API,
                "fetchedAt": _utc_now(),
                "entities": cached,
            },
        )
    return {qid: cached[qid] for qid in wanted if qid in cached}


def _duckdb():
    try:
        import duckdb  # type: ignore
    except ImportError as error:
        raise DatasetError(
            "source audit requires DuckDB; install with: pip install -e '.[audit]'"
        ) from error
    return duckdb


def _fetch_json(url: str) -> object:
    request = urllib.request.Request(
        url, headers={"User-Agent": "geonames-major-cities/0.3 source-audit"}
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.load(response)


def _find_inventory_entry(value: object, file_name: str) -> dict[str, object] | None:
    if isinstance(value, dict):
        if file_name in {
            value.get("name"),
            value.get("file"),
            value.get("name_compressed"),
        }:
            return {
                key: value.get(key)
                for key in (
                    "repo",
                    "commit",
                    "last_modified",
                    "last_updated",
                    "name_compressed",
                    "size_compressed",
                    "sha256_compressed",
                )
            }
        for child in value.values():
            match = _find_inventory_entry(child, file_name)
            if match:
                return match
    elif isinstance(value, list):
        for child in value:
            match = _find_inventory_entry(child, file_name)
            if match:
                return match
    return None


def load_wof(
    locations: Mapping[int, Location], cache_path: Path, refresh: bool
) -> dict[str, object]:
    if cache_path.is_file() and not refresh:
        cached = _read_gzip_json(cache_path)
        if isinstance(cached, dict):
            metadata = cached.get("metadata", {})
            if isinstance(metadata, dict) and not metadata.get("inventoryEntry"):
                inventory = _fetch_json(WOF_INVENTORY)
                metadata["inventoryEntry"] = _find_inventory_entry(
                    inventory, "whosonfirst-data-admin-latest.parquet"
                )
                _write_gzip_json(cache_path, cached)
            return cached
    _progress(
        "Who's On First: exact GeoNames-ID join against projected Parquet columns"
    )
    duckdb = _duckdb()
    connection = duckdb.connect()
    try:
        connection.execute(
            "CREATE TEMP TABLE selected (geonames_id VARCHAR, location_type VARCHAR, country_code VARCHAR)"
        )
        connection.executemany(
            "INSERT INTO selected VALUES (?, ?, ?)",
            [
                (str(item.source_id), item.type, item.country_code)
                for item in locations.values()
            ],
        )
        name_columns = ", ".join(f"w.name_{code}" for code in WOF_LANGUAGES)
        rows = connection.execute(
            f"""
            SELECT w.gn_id, w.id, w.wd_id, w.name, w.placetype, w.country,
                   w.parent_id, w.geom_src, {name_columns}
            FROM read_parquet('{WOF_PARQUET}') AS w
            JOIN selected AS s ON w.gn_id = s.geonames_id
            WHERE w.gn_id <> ''
            """
        ).fetchall()
        columns = [item[0] for item in connection.description]
    finally:
        connection.close()
    inventory_entry = None
    try:
        inventory = _fetch_json(WOF_INVENTORY)
        inventory_entry = _find_inventory_entry(
            inventory, "whosonfirst-data-admin-latest.parquet"
        )
    except (OSError, ValueError, urllib.error.URLError):
        pass
    result = {
        "metadata": {
            "source": WOF_PARQUET,
            "inventory": WOF_INVENTORY,
            "fetchedAt": _utc_now(),
            "inventoryEntry": inventory_entry,
        },
        "rows": [dict(zip(columns, row)) for row in rows],
    }
    _write_gzip_json(cache_path, result)
    return result


def load_overture(
    qids: Iterable[str], cache_path: Path, refresh: bool, release: str
) -> dict[str, object]:
    if cache_path.is_file() and not refresh:
        cached = _read_gzip_json(cache_path)
        if (
            isinstance(cached, dict)
            and cached.get("metadata", {}).get("release") == release
        ):
            return cached
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.\d+", release):
        raise DatasetError(f"invalid Overture release: {release}")
    _progress("Overture: resolving the pinned division Parquet snapshot")
    duckdb = _duckdb()
    files = _overture_files(release)
    if len(files) != 1:
        raise DatasetError(
            f"expected one Overture division Parquet file for {release}, found {len(files)}"
        )
    source_file = files[0]
    parquet_path = cache_path.parent / f"overture-{release}-division.parquet"
    _download_file(str(source_file["url"]), parquet_path, int(source_file["size"]))
    _progress("Overture: exact Wikidata-ID join against local projected columns")
    connection = duckdb.connect()
    try:
        connection.execute("CREATE TEMP TABLE selected (qid VARCHAR)")
        connection.executemany(
            "INSERT INTO selected VALUES (?)", [(qid,) for qid in sorted(set(qids))]
        )
        escaped_path = str(parquet_path).replace("'", "''")
        rows = connection.execute(
            f"""
            SELECT o.wikidata, o.id, o.subtype, o.country, o.region,
                   o.names.primary AS primary_name,
                   o.names.common AS common_names,
                   o.sources
            FROM read_parquet('{escaped_path}') AS o
            JOIN selected AS s ON o.wikidata = s.qid
            WHERE o.wikidata IS NOT NULL
            """
        ).fetchall()
        columns = [item[0] for item in connection.description]
    finally:
        connection.close()
        parquet_path.unlink(missing_ok=True)
    result = {
        "metadata": {
            "source": source_file["url"],
            "release": release,
            "fetchedAt": _utc_now(),
            "file": {
                "key": source_file["key"],
                "size": source_file["size"],
                "etag": source_file["etag"],
                "lastModified": source_file["lastModified"],
            },
        },
        "rows": [dict(zip(columns, row)) for row in rows],
    }
    _write_gzip_json(cache_path, result)
    return result


def _overture_files(release: str) -> list[dict[str, object]]:
    prefix = f"release/{release}/theme=divisions/type=division/"
    query = urllib.parse.urlencode({"list-type": "2", "prefix": prefix})
    endpoint = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/?" + query
    request = urllib.request.Request(
        endpoint, headers={"User-Agent": "geonames-major-cities/0.3 source-audit"}
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        root = ET.fromstring(response.read())
    namespace = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
    files: list[dict[str, object]] = []
    for item in root.findall("s3:Contents", namespace):
        key = item.findtext("s3:Key", namespaces=namespace) or ""
        if not key.endswith(".parquet"):
            continue
        files.append(
            {
                "key": key,
                "url": (
                    "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com/"
                    + urllib.parse.quote(key, safe="/=-_.")
                ),
                "size": int(item.findtext("s3:Size", "0", namespace)),
                "etag": (item.findtext("s3:ETag", "", namespace) or "").strip('"'),
                "lastModified": item.findtext("s3:LastModified", "", namespace),
            }
        )
    return files


def _download_file(url: str, path: Path, expected_size: int) -> None:
    if path.is_file() and path.stat().st_size == expected_size:
        _progress(f"Overture: reusing {expected_size / 1024 / 1024:.1f} MiB download")
        return
    partial = path.with_suffix(path.suffix + ".partial")
    partial.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url, headers={"User-Agent": "geonames-major-cities/0.3 source-audit"}
    )
    _progress(
        f"Overture: downloading one {expected_size / 1024 / 1024:.1f} MiB "
        "release file temporarily"
    )
    copied = 0
    next_progress = 64 * 1024 * 1024
    try:
        with (
            urllib.request.urlopen(request, timeout=120) as response,
            partial.open("wb") as target,
        ):
            while chunk := response.read(1024 * 1024):
                target.write(chunk)
                copied += len(chunk)
                if copied >= next_progress:
                    _progress(
                        f"Overture: downloaded {copied / 1024 / 1024:.0f}/"
                        f"{expected_size / 1024 / 1024:.0f} MiB"
                    )
                    next_progress += 64 * 1024 * 1024
        if copied != expected_size:
            raise DatasetError(
                f"Overture download size mismatch: expected {expected_size}, got {copied}"
            )
        partial.replace(path)
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _canonical_names(raw: Mapping[str, object]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for raw_language in sorted(raw):
        raw_value = raw[raw_language]
        language = canonical_language_code(str(raw_language))
        value = str(raw_value).strip() if raw_value is not None else ""
        if language and value and language not in selected:
            selected[language] = value
    return selected


def _wof_names(row: Mapping[str, object]) -> dict[str, str]:
    return _canonical_names(
        {
            language: row.get(f"name_{column}")
            for column, language in WOF_LANGUAGES.items()
            if row.get(f"name_{column}")
        }
    )


def _type_compatible(location_type: str, source: str, source_type: str) -> bool:
    allowed = {
        "wikidata": {
            "COUNTRY": {"linked"},
            "SUBDIVISION": {"linked"},
            "CITY": {"linked"},
        },
        "wof": {
            "COUNTRY": {"country", "dependency"},
            "SUBDIVISION": {"region", "macroregion"},
            "CITY": {"locality"},
        },
        "overture": {
            "COUNTRY": {"country"},
            "SUBDIVISION": {"region"},
            "CITY": {"locality"},
        },
    }
    return source_type.lower() in allowed[source][location_type]


def _has_chinese(names: Mapping[str, str]) -> bool:
    return any(language in names for language in CHINESE_LANGUAGES)


def analyze_candidates(
    source: str,
    locations: Mapping[int, Location],
    current_names: Mapping[int, Mapping[str, str]],
    rows_by_location: Mapping[int, Sequence[Mapping[str, object]]],
) -> dict[str, object]:
    exact_matches = len(rows_by_location)
    selected: dict[int, Mapping[str, object]] = {}
    incompatible_types: Counter[str] = Counter()
    country_mismatches = 0
    for source_id, rows in rows_by_location.items():
        location = locations[source_id]
        ranked: list[tuple[int, int, str, Mapping[str, object]]] = []
        for row in rows:
            source_type = str(row.get("sourceType", ""))
            type_ok = _type_compatible(location.type, source, source_type)
            source_country = str(row.get("countryCode", "")).upper()
            country_ok = (
                not source_country or source_country == location.country_code.upper()
            )
            if not type_ok:
                incompatible_types[source_type or "(missing)"] += 1
            if not country_ok:
                country_mismatches += 1
            ranked.append(
                (
                    0 if type_ok else 1,
                    0 if country_ok else 1,
                    str(row.get("externalId", "")),
                    row,
                )
            )
        best = min(ranked)
        if best[0] == 0 and best[1] == 0:
            selected[source_id] = best[3]

    gains: Counter[str] = Counter()
    conflicts: list[dict[str, object]] = []
    chinese_samples: list[dict[str, object]] = []
    locations_with_new_names: set[int] = set()
    chinese_filled: set[int] = set()
    new_pairs = 0
    for source_id, row in selected.items():
        candidate_names = row.get("names", {})
        if not isinstance(candidate_names, dict):
            continue
        existing = current_names.get(source_id, {})
        if not _has_chinese(existing) and _has_chinese(candidate_names):
            chinese_filled.add(source_id)
            if len(chinese_samples) < 15:
                chinese_samples.append(
                    {
                        "sourceId": source_id,
                        "type": locations[source_id].type,
                        "currentName": locations[source_id].name,
                        "candidate": next(
                            candidate_names[language]
                            for language in sorted(CHINESE_LANGUAGES)
                            if language in candidate_names
                        ),
                        "externalId": row.get("externalId"),
                    }
                )
        for language, value in candidate_names.items():
            previous = existing.get(language)
            if previous is None:
                gains[language] += 1
                new_pairs += 1
                locations_with_new_names.add(source_id)
            elif normalized_name(previous) != normalized_name(value):
                if len(conflicts) < 30:
                    conflicts.append(
                        {
                            "sourceId": source_id,
                            "type": locations[source_id].type,
                            "language": language,
                            "current": previous,
                            "candidate": value,
                            "externalId": row.get("externalId"),
                        }
                    )
    return {
        "exactIdentifierMatches": exact_matches,
        "exactIdentifierMatchRate": round(exact_matches / len(locations), 4),
        "typeAndCountryCompatibleMatches": len(selected),
        "typeAndCountryCompatibleMatchRate": round(len(selected) / len(locations), 4),
        "newLocalizedNamePairs": new_pairs,
        "locationsWithAnyNewName": len(locations_with_new_names),
        "missingChineseLocationsFilled": len(chinese_filled),
        "conflictingExistingNameCount": sum(
            1
            for source_id, row in selected.items()
            for language, value in row.get("names", {}).items()
            if language in current_names.get(source_id, {})
            and normalized_name(current_names[source_id][language])
            != normalized_name(value)
        ),
        "incompatibleSourceTypes": dict(incompatible_types.most_common()),
        "countryMismatchRows": country_mismatches,
        "topLanguageGains": dict(gains.most_common(30)),
        "sampleChineseGains": chinese_samples,
        "sampleConflicts": conflicts,
        "_missingChineseIds": sorted(chinese_filled),
    }


def wikidata_rows(
    links: Mapping[int, str], entities: Mapping[str, Mapping[str, object]]
) -> dict[int, list[dict[str, object]]]:
    result: dict[int, list[dict[str, object]]] = defaultdict(list)
    for source_id, qid in links.items():
        entity = entities.get(qid)
        if not entity or entity.get("missing"):
            continue
        labels = entity.get("labels", {})
        if not isinstance(labels, dict):
            continue
        result[source_id].append(
            {
                "externalId": qid,
                "sourceType": "linked",
                "countryCode": "",
                "names": _canonical_names(labels),
                "revision": entity.get("revision"),
                "modified": entity.get("modified"),
            }
        )
    return dict(result)


def wof_rows(
    rows: Sequence[Mapping[str, object]], locations: Mapping[int, Location]
) -> dict[int, list[dict[str, object]]]:
    result: dict[int, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        try:
            source_id = int(str(row.get("gn_id", "")))
        except ValueError:
            continue
        if source_id not in locations:
            continue
        result[source_id].append(
            {
                "externalId": row.get("id"),
                "sourceType": row.get("placetype"),
                "countryCode": row.get("country"),
                "names": _wof_names(row),
                "wikidata": row.get("wd_id"),
                "geometrySource": row.get("geom_src"),
            }
        )
    return dict(result)


def overture_rows(
    rows: Sequence[Mapping[str, object]],
    links: Mapping[int, str],
) -> dict[int, list[dict[str, object]]]:
    ids_by_qid: dict[str, list[int]] = defaultdict(list)
    for source_id, qid in links.items():
        ids_by_qid[qid].append(source_id)
    result: dict[int, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        qid = str(row.get("wikidata", ""))
        common = row.get("common_names") or {}
        if not isinstance(common, dict):
            common = {}
        for source_id in ids_by_qid.get(qid, []):
            result[source_id].append(
                {
                    "externalId": row.get("id"),
                    "sourceType": row.get("subtype"),
                    "countryCode": row.get("country"),
                    "names": _canonical_names(common),
                    "wikidata": qid,
                    "sources": row.get("sources"),
                }
            )
    return dict(result)


def run_source_audit(
    *,
    dataset: Path,
    alternate_names: Path,
    cache_dir: Path,
    output_dir: Path,
    refresh: bool = False,
    include_wikidata: bool = True,
    include_wof: bool = True,
    include_overture: bool = True,
    wikidata_workers: int = 4,
    overture_release: str = "2026-08-19.0",
) -> dict[str, object]:
    locations, current_names, dataset_metadata = load_dataset(dataset)
    _progress(f"Dataset: loaded {len(locations):,} locations")
    links, link_conflicts = extract_wikidata_links(alternate_names, set(locations))
    qids = set(links.values())
    source_ids_by_qid: dict[str, list[int]] = defaultdict(list)
    for source_id, qid in links.items():
        source_ids_by_qid[qid].append(source_id)
    shared_qids = {
        qid: sorted(source_ids)
        for qid, source_ids in source_ids_by_qid.items()
        if len(source_ids) > 1
    }
    _progress(f"GeoNames: recovered {len(links):,} exact Wikidata links")
    cache_dir.mkdir(parents=True, exist_ok=True)
    sources: dict[str, object] = {}
    chinese_gains_by_source: dict[str, set[int]] = {}

    if include_wikidata:
        entities = load_wikidata(
            qids,
            cache_dir / "wikidata-entities.json.gz",
            refresh,
            wikidata_workers,
        )
        metrics = analyze_candidates(
            "wikidata", locations, current_names, wikidata_rows(links, entities)
        )
        chinese_gains_by_source["wikidata"] = set(metrics.pop("_missingChineseIds"))
        sources["wikidata"] = {
            "status": "audited",
            "matchKey": "GeoNames alternateNamesV2 wkdt -> Wikidata QID",
            "snapshot": {
                "api": WIKIDATA_API,
                "fetchedEntityCount": len(entities),
                "entityRevisionPinnedInCache": True,
            },
            "license": "CC0-1.0",
            "validation": (
                "GeoNames explicit wkdt link; Wikidata P31 and country hierarchy were "
                "not fetched in this name-coverage audit"
            ),
            "provenance": "缓存为每个实体保留 QID、修订号和修改时间",
            "recommendation": "可作为核心补充候选，但必须先审查名称冲突",
            "metrics": metrics,
        }

    if include_wof:
        raw_wof = load_wof(locations, cache_dir / "wof-matches.json.gz", refresh)
        rows = raw_wof.get("rows", [])
        metrics = analyze_candidates(
            "wof", locations, current_names, wof_rows(rows, locations)
        )
        chinese_gains_by_source["whosOnFirst"] = set(metrics.pop("_missingChineseIds"))
        sources["whosOnFirst"] = {
            "status": "audited",
            "matchKey": "Who's On First gn_id -> GeoNames ID",
            "snapshot": raw_wof.get("metadata"),
            "license": "mixed; repository and record sources must be evaluated",
            "validation": "GeoNames ID、地点类型和国家代码必须同时兼容",
            "provenance": (
                "保留 WOF ID 和几何来源，但反规范化名称列没有字段级来源信息"
            ),
            "recommendation": "仅用于审计；字段级许可证与来源未解决前不得合并",
            "metrics": metrics,
        }

    if include_overture:
        raw_overture = load_overture(
            qids,
            cache_dir / f"overture-{overture_release}-matches.json.gz",
            refresh,
            overture_release,
        )
        rows = raw_overture.get("rows", [])
        metrics = analyze_candidates(
            "overture", locations, current_names, overture_rows(rows, links)
        )
        chinese_gains_by_source["overture"] = set(metrics.pop("_missingChineseIds"))
        sources["overture"] = {
            "status": "audited",
            "matchKey": "GeoNames wkdt QID -> Overture division wikidata",
            "snapshot": raw_overture.get("metadata"),
            "license": "ODbL-1.0 for the divisions theme; row sources may add CC0 fields",
            "validation": "Wikidata QID、地点类型和国家代码必须同时兼容",
            "provenance": "忽略目录中的匹配缓存保留 Overture sources 结构",
            "recommendation": "仅作为独立 ODbL 输出候选，不得混入 MIT 核心数据",
            "metrics": metrics,
        }

    current_chinese = sum(
        1 for source_id in locations if _has_chinese(current_names.get(source_id, {}))
    )
    report: dict[str, object] = {
        "schemaVersion": 1,
        "generatedAt": _utc_now(),
        "method": {
            "usesLanguageModel": False,
            "matching": "exact identifiers only",
            "fuzzyMatching": False,
            "machineTranslation": False,
            "typeAndCountryValidation": (
                "WOF and Overture validated; Wikidata follows GeoNames explicit wkdt links"
            ),
        },
        "dataset": {
            "path": str(dataset),
            "locationCount": len(locations),
            "currentChineseCoverage": current_chinese,
            "currentChineseCoverageRate": round(current_chinese / len(locations), 4),
            "metadata": dataset_metadata,
        },
        "geonamesWikidataLinks": {
            "count": len(links),
            "uniqueQidCount": len(qids),
            "conflictCount": len(link_conflicts),
            "sampleConflicts": link_conflicts[:20],
            "sharedQidCount": len(shared_qids),
            "sampleSharedQids": [
                {"qid": qid, "sourceIds": source_ids}
                for qid, source_ids in sorted(shared_qids.items())[:20]
            ],
        },
        "sources": sources,
    }
    combined_chinese = set().union(*chinese_gains_by_source.values())
    report["potentialChineseCoverage"] = {
        "note": (
            "technical coverage only; sources cannot be merged without applying each "
            "license and provenance rule"
        ),
        "perSource": {
            name: {
                "newLocations": len(source_ids),
                "projectedCoveredLocations": current_chinese + len(source_ids),
                "projectedCoverageRate": round(
                    (current_chinese + len(source_ids)) / len(locations), 4
                ),
            }
            for name, source_ids in chinese_gains_by_source.items()
        },
        "allSourcesUniqueUnion": {
            "newLocations": len(combined_chinese),
            "projectedCoveredLocations": current_chinese + len(combined_chinese),
            "projectedCoverageRate": round(
                (current_chinese + len(combined_chinese)) / len(locations), 4
            ),
        },
        "pairwiseOverlap": {
            "wikidataAndWhosOnFirst": len(
                chinese_gains_by_source.get("wikidata", set())
                & chinese_gains_by_source.get("whosOnFirst", set())
            ),
            "wikidataAndOverture": len(
                chinese_gains_by_source.get("wikidata", set())
                & chinese_gains_by_source.get("overture", set())
            ),
            "whosOnFirstAndOverture": len(
                chinese_gains_by_source.get("whosOnFirst", set())
                & chinese_gains_by_source.get("overture", set())
            ),
        },
    }
    summary = {
        name: {
            "exactMatches": item["metrics"]["exactIdentifierMatches"],
            "compatibleMatches": item["metrics"]["typeAndCountryCompatibleMatches"],
            "newNamePairs": item["metrics"]["newLocalizedNamePairs"],
            "missingChineseFilled": item["metrics"]["missingChineseLocationsFilled"],
        }
        for name, item in sources.items()
    }
    report["summary"] = summary
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "source-audit.json"
    markdown_path = output_dir / "source-audit.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown_report(report), encoding="utf-8")
    _progress(f"Wrote {json_path} and {markdown_path}")
    return report


def _markdown_report(report: Mapping[str, object]) -> str:
    dataset = report["dataset"]
    lines = [
        "# 补充地名来源覆盖审计",
        "",
        f"生成时间：`{report['generatedAt']}`",
        "",
        (
            "本报告由确定性脚本生成。只使用精确的 GeoNames ID 或 Wikidata QID 关联；"
            "不使用模糊名称匹配、大模型判断或机器翻译。"
        ),
        "",
        "## 当前基准",
        "",
        f"- 地点总数：{dataset['locationCount']:,}",
        (
            f"- 已有简体中文覆盖：{dataset['currentChineseCoverage']:,} "
            f"({dataset['currentChineseCoverageRate']:.1%})"
        ),
        (
            f"- GeoNames 中可用的 Wikidata 精确链接："
            f"{report['geonamesWikidataLinks']['count']:,}，对应 "
            f"{report['geonamesWikidataLinks']['uniqueQidCount']:,} 个唯一 QID；"
            f"其中 {report['geonamesWikidataLinks']['sharedQidCount']:,} 个 QID "
            "关联多个当前地点，已单独列入 JSON 审查"
        ),
        "",
        "## 审计结果",
        "",
        "| 来源 | 精确匹配 | 通过来源校验 | 新名称对 | 可补中文地点 | 结论 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    display_names = {
        "wikidata": "Wikidata",
        "whosOnFirst": "Who's On First",
        "overture": "Overture",
    }
    for name, item in report["sources"].items():
        metrics = item["metrics"]
        lines.append(
            f"| {display_names[name]} | {metrics['exactIdentifierMatches']:,} | "
            f"{metrics['typeAndCountryCompatibleMatches']:,} | "
            f"{metrics['newLocalizedNamePairs']:,} | "
            f"{metrics['missingChineseLocationsFilled']:,} | "
            f"{item['recommendation']} |"
        )
    lines += [
        "",
        (
            "各来源的增量不能直接相加，因为它们会覆盖同一批地点。冲突只是审计结果，"
            "本次没有写入正式数据。"
        ),
        "",
        "## 中文覆盖推算",
        "",
        "| 方案 | 新增中文地点 | 推算总覆盖 | 覆盖率 |",
        "|---|---:|---:|---:|",
    ]
    coverage = report["potentialChineseCoverage"]
    for name, values in coverage["perSource"].items():
        lines.append(
            f"| {display_names[name]} | {values['newLocations']:,} | "
            f"{values['projectedCoveredLocations']:,} | "
            f"{values['projectedCoverageRate']:.1%} |"
        )
    union = coverage["allSourcesUniqueUnion"]
    lines += [
        (
            f"| 三方唯一并集（仅技术上限） | {union['newLocations']:,} | "
            f"{union['projectedCoveredLocations']:,} | "
            f"{union['projectedCoverageRate']:.1%} |"
        ),
        "",
        "三方唯一并集只表示技术覆盖上限，不表示可以直接合并；仍必须分别满足许可证和字段溯源要求。",
        "",
        "## 授权与后续使用边界",
        "",
    ]
    for name, item in report["sources"].items():
        lines += [
            f"### {display_names[name]}",
            "",
            f"- 许可：{item['license']}",
            f"- 校验：{item['validation']}",
            f"- 溯源：{item['provenance']}",
            f"- 建议：{item['recommendation']}",
            "",
        ]
    lines += [
        "## 可复现性",
        "",
        (
            "忽略目录 `audit-cache/` 保存源数据匹配缓存与 Wikidata 实体修订号；"
            "`--refresh` 可强制重新抓取。JSON 报告包含各来源快照信息和机器可读指标。"
        ),
        "",
    ]
    return "\n".join(lines)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
