# GeoNames Major Cities

A small, reproducible pipeline that turns pinned GeoNames dumps into a practical three-level location dataset:

```text
country → first-level subdivision → major city
```

It produces compressed CSV files, a queryable SQLite database, a quality report, and a self-contained multilingual HTML browser that opens by double-clicking. The build uses only the Python standard library.

[中文说明](README.zh-CN.md)

[Open the lightweight project presentation](docs/index.html)

## Why this exists

Raw global place dumps mix capitals, cities, towns, counties, districts, and many same-name records. They are excellent source material but are not directly suitable for a country/state/city selector.

This project makes the selection policy explicit, pins every input by SHA-256, and fails the build when it finds duplicate display options, broken parent links, unmatched Chinese prefecture-level cities, or policy regressions.

## Quick start

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .

geonames-major-cities all \
  --manifest manifests/geonames-2026-09-17.json \
  --data-dir data \
  --output-dir output
```

The largest pinned input is approximately 195 MiB. Downloads are cached and checksum-verified on later runs.

Open `output/review.html` directly in a browser. It contains the full hierarchy and does not start a server or make network requests.

The pinned 2026-09-17 snapshot has been verified end to end: 250 countries, 3,865 first-level subdivisions, and 31,805 cities. It contains 543,811 selected localized names across 526 language tags. The build matched all 293 Chinese prefecture-level cities, removed 71 same-parent duplicate city options, and finished with zero unresolved findings.

## Selection policy

| Scope | Included as a city |
|---|---|
| Most countries | National capitals (`PPLC`), first-level administrative capitals (`PPLA`), second-level administrative capitals (`PPLA2`), and ordinary populated places (`PPL`) with population at least 50,000 |
| China | Only four-digit prefecture-level city entries from a pinned Chinese administrative-division policy, matched back to GeoNames records |
| Beijing, Shanghai, Tianjin, Chongqing | Kept as terminal first-level subdivisions; subordinate districts and counties are not emitted as cities |

The result is a product-oriented “major city” list, not a universal legal definition of city status. For example, a small `PPLA2` administrative seat such as Brändö is intentionally retained even when its population is below 50,000.

The default `name` prefers a non-historic English alternate name from GeoNames. When none exists, the GeoNames primary proper name is retained. “English-preferred” does not mean ASCII-only: valid names can contain diacritics.

All selected display names with a valid language tag are also exported. Historic, colloquial, and short-name records are excluded from display-name selection. For a requested locale, consumers should use this fallback chain:

```text
exact locale → base language → English → primary name
zh-CN       → zh            → en      → name
```

Coverage is intentionally honest rather than machine-filled. In this snapshot, Russian has direct names for 19,667 locations, English for 15,796, Japanese for 11,887, and generic Chinese for 9,971. Missing translations fall back; this project does not invent them.

## Output files

| File | Purpose |
|---|---|
| `locations.csv.gz` | Portable hierarchy with source IDs, coordinates, population, feature code, and selection reason |
| `location-names.csv.gz` | One best display name per location and language tag |
| `locations.sqlite3` | Hierarchy plus `location_names`, ready for SQLite, DB Browser for SQLite, or Datasette |
| `review.html` | Self-contained multilingual cascading browser with coverage and quality summaries |
| `quality-report.json` | Counts, policy, source hashes, output hashes, and build status |
| `quality-findings.csv` | Empty except for its header on a passing build; actionable rows otherwise |

City rows include latitude and longitude from GeoNames. Country and subdivision rows intentionally do not infer coordinates.

Useful SQLite queries:

```sql
-- Browse flattened city data.
SELECT country_name, subdivision_name, city_name, latitude, longitude
FROM city_flat
ORDER BY country_name, subdivision_name, city_name;

-- Both queries must return zero rows on a passing build.
SELECT * FROM duplicate_check;
SELECT * FROM orphan_check;

-- Inspect localized names and coverage.
SELECT * FROM localized_city_names WHERE language_code = 'zh';
SELECT * FROM language_coverage LIMIT 20;
```

## Reproducibility and updates

The manifest records each source URL, byte size, and SHA-256. Mutable upstream URLs therefore cannot silently change a historical build.

To update the data:

1. Add a new dated manifest instead of overwriting an old one.
2. Record the new byte sizes and SHA-256 values.
3. Re-run the complete build and inspect the count delta and HTML browser.
4. Add or update regression sentinels when the policy changes intentionally.

Do not treat a new upstream snapshot as automatically safe. Administrative data changes, and GeoNames feature classifications can change independently.

## Supplementary-source audit

Wikidata, Who's On First, and Overture can be measured without changing the canonical GeoNames output. The audit uses exact GeoNames IDs and Wikidata QIDs only; it does not use fuzzy name matching, machine translation, or a language model.

Install the optional DuckDB dependency, then run:

```bash
python -m pip install -e '.[audit]'

geonames-major-cities audit-sources \
  --dataset output/locations.sqlite3 \
  --geonames-alternate-names data/alternateNamesV2.zip \
  --cache-dir audit-cache \
  --output-dir audit-output
```

The command projects only required Parquet columns and joins them to the current dataset by exact identifiers. Who's On First stays remote; the current Overture division release is one roughly 550 MiB Parquet file, so the script downloads that pinned file temporarily for a reliable nested-name scan and deletes it after caching only matched rows. It writes `source-audit.json` plus a Chinese `source-audit.md`. Use `--refresh` for a fresh upstream snapshot. The audit reports coverage, conflicts, hierarchy compatibility, provenance, and license boundaries; it never merges candidate names into the formal outputs.

## Development

```bash
python -m unittest discover -s tests -v
```

Tests use tiny local fixtures; CI never downloads the full GeoNames dumps.

## License and attribution

The code in this repository is MIT licensed. Generated datasets contain data from other sources and retain their source licenses and attribution requirements. See [DATA_SOURCES.md](DATA_SOURCES.md) before redistributing an output.
