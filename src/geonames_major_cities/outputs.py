from __future__ import annotations

import csv
import gzip
import io
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence

from .models import OUTPUT_COLUMNS, LocationRecord, ReviewFinding, normalized_name
from .sources import sha256_file


DATASET_FILE_NAME = "locations.csv.gz"
SQLITE_FILE_NAME = "locations.sqlite3"
HTML_FILE_NAME = "review.html"
FINDINGS_FILE_NAME = "quality-findings.csv"
REPORT_FILE_NAME = "quality-report.json"


def write_csv(path: Path, records: Sequence[LocationRecord]) -> None:
    # mtime=0 and an empty embedded filename make identical input reproducible.
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as zipped:
            with io.TextIOWrapper(zipped, encoding="utf-8", newline="") as text:
                writer = csv.DictWriter(text, fieldnames=OUTPUT_COLUMNS)
                writer.writeheader()
                writer.writerows(asdict(record) for record in records)


def write_findings_csv(path: Path, findings: Sequence[ReviewFinding]) -> None:
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=("kind", "type", "sourceId", "parentSourceId", "name", "detail"),
        )
        writer.writeheader()
        writer.writerows(asdict(finding) for finding in findings)


def write_sqlite(
    path: Path, records: Sequence[LocationRecord], report: Mapping
) -> None:
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA journal_mode = DELETE;
            PRAGMA synchronous = FULL;
            CREATE TABLE locations (
                type TEXT NOT NULL CHECK (type IN ('COUNTRY','SUBDIVISION','CITY')),
                source_id INTEGER NOT NULL,
                parent_type TEXT,
                parent_source_id INTEGER,
                code TEXT,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                country_code TEXT NOT NULL,
                admin1_code TEXT,
                latitude REAL,
                longitude REAL,
                population INTEGER,
                feature_code TEXT,
                selection_reason TEXT NOT NULL,
                PRIMARY KEY (type, source_id)
            ) WITHOUT ROWID;

            CREATE INDEX locations_parent_name_idx
                ON locations (type, parent_source_id, normalized_name);
            CREATE INDEX locations_country_idx
                ON locations (country_code, admin1_code, type);

            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;

            CREATE VIEW city_flat AS
            SELECT
                city.source_id AS city_source_id,
                city.name AS city_name,
                city.latitude,
                city.longitude,
                city.population,
                city.feature_code,
                city.selection_reason,
                subdivision.source_id AS subdivision_source_id,
                subdivision.name AS subdivision_name,
                country.source_id AS country_source_id,
                country.name AS country_name,
                country.country_code
            FROM locations AS city
            JOIN locations AS parent
              ON parent.type = city.parent_type
             AND parent.source_id = city.parent_source_id
            JOIN locations AS country
              ON country.type = 'COUNTRY'
             AND country.source_id = CASE
                   WHEN parent.type = 'COUNTRY' THEN parent.source_id
                   ELSE parent.parent_source_id
                 END
            LEFT JOIN locations AS subdivision
              ON parent.type = 'SUBDIVISION'
             AND subdivision.type = parent.type
             AND subdivision.source_id = parent.source_id
            WHERE city.type = 'CITY';

            CREATE VIEW duplicate_check AS
            SELECT type, parent_source_id, normalized_name,
                   COUNT(*) AS duplicate_count,
                   GROUP_CONCAT(name, ' | ') AS names
            FROM locations
            GROUP BY type, parent_source_id, normalized_name
            HAVING COUNT(*) > 1;

            CREATE VIEW orphan_check AS
            SELECT child.type, child.source_id, child.name,
                   child.parent_type, child.parent_source_id
            FROM locations AS child
            LEFT JOIN locations AS parent
              ON parent.type = child.parent_type
             AND parent.source_id = child.parent_source_id
            WHERE child.parent_type IS NOT NULL
              AND parent.source_id IS NULL;
            """
        )
        connection.executemany(
            """
            INSERT INTO locations (
                type, source_id, parent_type, parent_source_id, code, name,
                normalized_name, country_code, admin1_code, latitude, longitude,
                population, feature_code, selection_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    record.type,
                    record.sourceId,
                    record.parentType,
                    record.parentSourceId,
                    record.code,
                    record.name,
                    normalized_name(record.name),
                    record.countryCode,
                    record.admin1Code,
                    record.latitude,
                    record.longitude,
                    record.population,
                    record.featureCode,
                    record.selectionReason,
                )
                for record in records
            ],
        )
        metadata = {
            "source": str(report["source"]),
            "sourceVersion": str(report["sourceVersion"]),
            "qualityStatus": str(report["qualityStatus"]),
            "countryCount": str(report["counts"]["COUNTRY"]),
            "subdivisionCount": str(report["counts"]["SUBDIVISION"]),
            "cityCount": str(report["counts"]["CITY"]),
        }
        connection.executemany(
            "INSERT INTO metadata (key, value) VALUES (?, ?)", metadata.items()
        )
        connection.commit()
        connection.execute("VACUUM")
    finally:
        connection.close()


def write_review_html(
    path: Path,
    records: Sequence[LocationRecord],
    findings: Sequence[ReviewFinding],
    report: Mapping,
) -> None:
    payload = json.dumps(
        {
            "records": [
                {
                    "t": record.type,
                    "id": record.sourceId,
                    "pt": record.parentType,
                    "pid": record.parentSourceId,
                    "n": record.name,
                    "cc": record.countryCode,
                    "lat": record.latitude,
                    "lon": record.longitude,
                    "pop": record.population,
                    "fc": record.featureCode,
                    "why": record.selectionReason,
                }
                for record in records
            ],
            "findings": [asdict(finding) for finding in findings],
            "report": report,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    document = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GeoNames Major Cities Review</title>
<style>
:root{color-scheme:light dark;--bg:#f4f1ea;--card:#fff;--ink:#1e2930;--muted:#65727a;--line:#d8d5cc;--accent:#126b55;--soft:#e4f2ec;--warn:#a04a20}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1040px;margin:0 auto;padding:42px 22px 64px}h1{font-size:clamp(30px,5vw,54px);letter-spacing:-.04em;line-height:1;margin:0 0 12px}h2{margin:0 0 18px;font-size:20px}.lede{color:var(--muted);font-size:17px;max-width:760px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:26px 0}.stat,.card{background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:0 8px 30px #20342a0d}.stat{padding:18px}.stat b{display:block;font-size:29px}.stat span{color:var(--muted)}.card{padding:22px;margin-top:16px}.form{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}label{font-size:12px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.08em}select,input{width:100%;margin-top:7px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--ink);padding:11px 12px;font:inherit}.detail{margin-top:18px;padding:15px;border-radius:12px;background:var(--soft);white-space:pre-wrap}.ok{color:var(--accent);font-weight:700}.bad{color:var(--warn);font-weight:700}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--line)}th{color:var(--muted)}.scroll{overflow:auto;max-height:360px}@media(max-width:720px){.grid,.form{grid-template-columns:1fr}main{padding:28px 14px}}
@media(prefers-color-scheme:dark){:root{--bg:#111714;--card:#19211d;--ink:#ecf2ee;--muted:#9cab9f;--line:#344139;--accent:#72d8b2;--soft:#203c32;--warn:#f0a17c}}
</style>
</head>
<body><main>
<h1>GeoNames major cities</h1>
<p class="lede">A read-only, self-contained review page. It works by double-clicking this HTML file; no web server or network request is required.</p>
<div class="grid">
  <div class="stat"><b id="countries">–</b><span>countries</span></div>
  <div class="stat"><b id="subdivisions">–</b><span>subdivisions</span></div>
  <div class="stat"><b id="cities">–</b><span>cities</span></div>
</div>
<section class="card">
  <h2>Browse hierarchy</h2>
  <div class="form">
    <label>Country<select id="country"></select></label>
    <label>Subdivision<select id="subdivision"></select></label>
    <label>City search<input id="search" type="search" placeholder="Filter current city list"></label>
  </div>
  <label style="display:block;margin-top:14px">City<select id="city" size="10"></select></label>
  <div class="detail" id="detail">Select a city.</div>
</section>
<section class="card">
  <h2>Quality status: <span id="quality"></span></h2>
  <p id="summary"></p>
  <div class="scroll"><table><thead><tr><th>Kind</th><th>Type</th><th>Name</th><th>Detail</th></tr></thead><tbody id="findingRows"></tbody></table></div>
</section>
<script id="dataset" type="application/json">__PAYLOAD__</script>
<script>
const data=JSON.parse(document.getElementById('dataset').textContent);
const records=data.records;
const byType=t=>records.filter(x=>x.t===t);
const countries=byType('COUNTRY');
const subdivisions=byType('SUBDIVISION');
const cities=byType('CITY');
const country=document.getElementById('country');
const subdivision=document.getElementById('subdivision');
const city=document.getElementById('city');
const search=document.getElementById('search');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const options=values=>values.map(x=>`<option value="${x.id}">${esc(x.n)}</option>`).join('');
document.getElementById('countries').textContent=countries.length.toLocaleString();
document.getElementById('subdivisions').textContent=subdivisions.length.toLocaleString();
document.getElementById('cities').textContent=cities.length.toLocaleString();
country.innerHTML=options(countries);
function loadSubdivisions(){
  const id=Number(country.value);
  const values=subdivisions.filter(x=>x.pid===id);
  subdivision.innerHTML='<option value="direct">(Direct country cities)</option>'+options(values);
  subdivision.value=values.length?String(values[0].id):'direct';
  loadCities();
}
function currentCities(){
  const parent=subdivision.value==='direct'?Number(country.value):Number(subdivision.value);
  const query=search.value.trim().toLocaleLowerCase();
  return cities.filter(x=>x.pid===parent&&(!query||x.n.toLocaleLowerCase().includes(query)));
}
function loadCities(){
  const values=currentCities();
  city.innerHTML=options(values);
  document.getElementById('detail').textContent=values.length?`${values.length} cities in this list. Select one for source details.`:'No cities match this selection.';
}
function showCity(){
  const item=cities.find(x=>x.id===Number(city.value));
  if(!item)return;
  document.getElementById('detail').textContent=[
    item.n,
    `GeoNames ID: ${item.id}`,
    `Coordinates: ${item.lat}, ${item.lon}`,
    `Population: ${item.pop?.toLocaleString()??'unknown'}`,
    `Feature: ${item.fc}`,
    `Selected because: ${item.why}`
  ].join('\\n');
}
country.onchange=loadSubdivisions;
subdivision.onchange=loadCities;
search.oninput=loadCities;
city.onchange=showCity;
const quality=document.getElementById('quality');
quality.textContent=data.report.qualityStatus;
quality.className=data.report.qualityStatus==='PASS'?'ok':'bad';
document.getElementById('summary').textContent=`Removed ${data.report.duplicateCityOptionsRemoved} duplicate city options. ${data.findings.length} findings remain.`;
document.getElementById('findingRows').innerHTML=data.findings.length?data.findings.map(x=>`<tr><td>${esc(x.kind)}</td><td>${esc(x.type)}</td><td>${esc(x.name)}</td><td>${esc(x.detail)}</td></tr>`).join(''):'<tr><td colspan="4" class="ok">No unresolved findings.</td></tr>';
loadSubdivisions();
</script>
</main></body></html>
""".replace("__PAYLOAD__", payload)
    path.write_text(document, encoding="utf-8")


def write_outputs(
    output_dir: Path,
    records: Sequence[LocationRecord],
    findings: Sequence[ReviewFinding],
    report: dict,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = output_dir / DATASET_FILE_NAME
    sqlite_path = output_dir / SQLITE_FILE_NAME
    html_path = output_dir / HTML_FILE_NAME
    findings_path = output_dir / FINDINGS_FILE_NAME

    write_csv(dataset_path, records)
    write_findings_csv(findings_path, findings)
    write_sqlite(sqlite_path, records, report)
    write_review_html(html_path, records, findings, report)

    report["outputs"] = {
        DATASET_FILE_NAME: {"sha256": sha256_file(dataset_path)},
        SQLITE_FILE_NAME: {"sha256": sha256_file(sqlite_path)},
        HTML_FILE_NAME: {"sha256": sha256_file(html_path)},
        FINDINGS_FILE_NAME: {"sha256": sha256_file(findings_path)},
    }
    (output_dir / REPORT_FILE_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report
