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
from .models import LOCALIZED_NAME_COLUMNS, LocalizedName
from .sources import sha256_file


DATASET_FILE_NAME = "locations.csv.gz"
LOCALIZED_NAMES_FILE_NAME = "location-names.csv.gz"
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


def write_localized_names_csv(
    path: Path, names: Sequence[LocalizedName]
) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as zipped:
            with io.TextIOWrapper(zipped, encoding="utf-8", newline="") as text:
                writer = csv.DictWriter(text, fieldnames=LOCALIZED_NAME_COLUMNS)
                writer.writeheader()
                writer.writerows(asdict(name) for name in names)


def write_findings_csv(path: Path, findings: Sequence[ReviewFinding]) -> None:
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(
            target,
            fieldnames=("kind", "type", "sourceId", "parentSourceId", "name", "detail"),
        )
        writer.writeheader()
        writer.writerows(asdict(finding) for finding in findings)


def write_sqlite(
    path: Path,
    records: Sequence[LocationRecord],
    names: Sequence[LocalizedName],
    report: Mapping,
) -> None:
    path.unlink(missing_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;
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

            CREATE TABLE location_names (
                location_type TEXT NOT NULL,
                source_id INTEGER NOT NULL,
                language_code TEXT NOT NULL,
                name TEXT NOT NULL,
                is_preferred INTEGER NOT NULL CHECK (is_preferred IN (0, 1)),
                PRIMARY KEY (location_type, source_id, language_code),
                FOREIGN KEY (location_type, source_id)
                    REFERENCES locations (type, source_id)
                    ON DELETE CASCADE
            ) WITHOUT ROWID;

            CREATE INDEX location_names_language_idx
                ON location_names (language_code, name);

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

            CREATE VIEW localized_city_names AS
            SELECT names.language_code,
                   names.name AS localized_city_name,
                   city.source_id AS city_source_id,
                   city.name AS default_city_name,
                   city.country_code,
                   city.admin1_code,
                   names.is_preferred
            FROM location_names AS names
            JOIN locations AS city
              ON city.type = names.location_type
             AND city.source_id = names.source_id
            WHERE names.location_type = 'CITY';

            CREATE VIEW language_coverage AS
            SELECT language_code,
                   COUNT(*) AS location_count
            FROM location_names
            GROUP BY language_code
            ORDER BY location_count DESC, language_code;
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
        connection.executemany(
            """
            INSERT INTO location_names (
                location_type, source_id, language_code, name, is_preferred
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    name.type,
                    name.sourceId,
                    name.languageCode,
                    name.name,
                    1 if name.isPreferred else 0,
                )
                for name in names
            ],
        )
        metadata = {
            "source": str(report["source"]),
            "sourceVersion": str(report["sourceVersion"]),
            "qualityStatus": str(report["qualityStatus"]),
            "countryCount": str(report["counts"]["COUNTRY"]),
            "subdivisionCount": str(report["counts"]["SUBDIVISION"]),
            "cityCount": str(report["counts"]["CITY"]),
            "localizedNameCount": str(report["localizedNameCount"]),
            "languageCount": str(report["languageCount"]),
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
    names: Sequence[LocalizedName],
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
            "names": [
                [name.sourceId, name.languageCode, name.name]
                for name in names
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
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ccircle cx='16' cy='16' r='10' fill='none' stroke='%23285f96' stroke-width='2'/%3E%3Cpath d='M16 2v28M2 16h28' stroke='%2314253d'/%3E%3C/svg%3E">
<title>GeoNames Major Cities Review</title>
<style>
:root{--paper:#f7f9fc;--sheet:#fff;--ink:#14253d;--muted:#607087;--hair:#cbd5e1;--accent:#285f96;--pale:#eaf1f8;--warn:#a24332;--ok:#176b55}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,select{font:inherit}main{width:min(1120px,calc(100% - 32px));margin:0 auto;padding:34px 0 72px}.mast{border-top:3px solid var(--ink);border-bottom:1px solid var(--hair);padding:13px 0 30px}.kicker,.field-label,.micro{font:600 11px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.09em;text-transform:uppercase;color:var(--muted)}.coordinate-rule{display:flex;justify-content:space-between;gap:16px;margin-bottom:38px}.coordinate-rule span+span{border-left:1px solid var(--hair);padding-left:16px}h1{font:600 clamp(38px,7vw,78px)/.98 "Avenir Next Condensed","Arial Narrow",sans-serif;letter-spacing:-.035em;max-width:760px;margin:0}.lede{max-width:690px;margin:22px 0 0;color:var(--muted);font-size:17px}.ledger{display:flex;flex-wrap:wrap;gap:8px 32px;margin:30px 0 0;padding:18px 0 0;border-top:1px solid var(--hair)}.ledger div{display:flex;align-items:baseline;gap:8px}.ledger b{font:600 23px/1 ui-monospace,SFMono-Regular,Menlo,monospace}.ledger span{color:var(--muted)}.workspace{display:grid;grid-template-columns:300px 1fr;border:1px solid var(--hair);background:var(--sheet);margin-top:28px;min-height:560px}.controls{padding:25px;border-right:1px solid var(--hair)}.controls h2,.results h2,.quality h2{font:600 22px/1.1 "Avenir Next Condensed","Arial Narrow",sans-serif;margin:0 0 22px}.field{display:block;margin:0 0 18px}.field-label{display:block;margin-bottom:7px}select,input{width:100%;border:1px solid #aebbc9;border-radius:2px;background:#fff;color:var(--ink);padding:10px 11px;outline:none}select:focus,input:focus{border-color:var(--accent);box-shadow:0 0 0 3px #285f961a}.coverage{font-size:12px;color:var(--muted);margin:-8px 0 20px}.results{padding:25px;display:grid;grid-template-rows:auto minmax(220px,1fr) auto}.city-list{min-height:220px}.detail{margin-top:22px;border-top:1px solid var(--hair);padding:18px 0 0;display:grid;grid-template-columns:1fr auto;gap:20px}.place-name{font:600 30px/1.1 "Avenir Next Condensed","Arial Narrow",sans-serif;margin:0}.place-meta{color:var(--muted);margin:7px 0 0}.coords{font:500 13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;text-align:right;color:var(--accent)}.quality{margin-top:34px;border-top:1px solid var(--ink);padding-top:24px}.quality-head{display:flex;align-items:baseline;justify-content:space-between;gap:20px}.ok{color:var(--ok)}.bad{color:var(--warn)}.scroll{overflow:auto;max-height:320px}table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--hair)}th{font:600 11px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}.empty{color:var(--muted)}@media(max-width:760px){main{width:min(100% - 22px,1120px);padding-top:18px}.coordinate-rule{margin-bottom:28px}.coordinate-rule span:nth-child(2){display:none}.workspace{grid-template-columns:1fr}.controls{border-right:0;border-bottom:1px solid var(--hair)}.detail{grid-template-columns:1fr}.coords{text-align:left}.quality-head{display:block}}
</style>
</head>
<body><main>
<header class="mast">
  <div class="coordinate-rule kicker"><span>GeoNames / generated index</span><span>Country · subdivision · major city</span><span id="version">Snapshot</span></div>
  <h1>Browse the generated location index.</h1>
  <p class="lede">Switch the name language, inspect the hierarchy, and verify why each city was included. This file is self-contained: double-click it, no server or network request required.</p>
  <div class="ledger">
    <div><b id="countries">–</b><span>countries</span></div>
    <div><b id="subdivisions">–</b><span>subdivisions</span></div>
    <div><b id="cities">–</b><span>cities</span></div>
    <div><b id="languages">–</b><span>languages</span></div>
  </div>
</header>
<section class="workspace" aria-label="Location browser">
  <div class="controls">
    <h2>Choose a place</h2>
    <label class="field"><span class="field-label">Name language</span><select id="language"></select></label>
    <p class="coverage" id="coverage"></p>
    <label class="field"><span class="field-label">Country</span><select id="country"></select></label>
    <label class="field"><span class="field-label">Subdivision</span><select id="subdivision"></select></label>
    <label class="field"><span class="field-label">Filter cities</span><input id="search" type="search" placeholder="Type a city name"></label>
  </div>
  <div class="results">
    <h2>City</h2>
    <select class="city-list" id="city" size="12" aria-label="Cities"></select>
    <div class="detail" id="detail"><div><p class="place-name">Select a city</p><p class="place-meta">Source details will appear here.</p></div></div>
  </div>
</section>
<section class="quality">
  <div class="quality-head"><h2>Build quality</h2><p class="micro">Status / <strong id="quality"></strong></p></div>
  <p id="summary"></p>
  <div class="scroll"><table><thead><tr><th>Kind</th><th>Type</th><th>Name</th><th>Detail</th></tr></thead><tbody id="findingRows"></tbody></table></div>
</section>
<script id="dataset" type="application/json">__PAYLOAD__</script>
<script>
const data=JSON.parse(document.getElementById('dataset').textContent);
const records=data.records;
const namesById=new Map();
for(const [id,tag,value] of data.names){if(!namesById.has(id))namesById.set(id,new Map());namesById.get(id).set(tag,value);}
const byType=t=>records.filter(x=>x.t===t);
const countries=byType('COUNTRY');
const subdivisions=byType('SUBDIVISION');
const cities=byType('CITY');
const language=document.getElementById('language');
const country=document.getElementById('country');
const subdivision=document.getElementById('subdivision');
const city=document.getElementById('city');
const search=document.getElementById('search');
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const total=records.length;
let displayNames;
try{displayNames=new Intl.DisplayNames([navigator.language||'en'],{type:'language'});}catch(_error){displayNames=null;}
const languageLabel=tag=>{try{return displayNames?.of(tag)||tag;}catch(_error){return tag;}};
const localizedName=item=>{
  if(language.value==='default')return item.n;
  const names=namesById.get(item.id);
  const tag=language.value;
  const base=tag.split('-')[0];
  return names?.get(tag)||names?.get(base)||names?.get('en')||item.n;
};
const compareNames=(a,b)=>{try{return a.localeCompare(b,language.value==='default'?'en':language.value);}catch(_error){return a.localeCompare(b);}};
const options=values=>[...values].sort((a,b)=>compareNames(localizedName(a),localizedName(b))).map(x=>`<option value="${x.id}">${esc(localizedName(x))}</option>`).join('');
const coverageEntries=Object.entries(data.report.languageCoverage);
language.innerHTML='<option value="default">Default · English-preferred</option>'+coverageEntries.map(([tag,count])=>`<option value="${esc(tag)}">${esc(languageLabel(tag))} · ${esc(tag)} · ${count.toLocaleString()}</option>`).join('');
const browserTag=(navigator.language||'en').toLowerCase();
const initialTag=coverageEntries.map(([tag])=>tag).find(tag=>tag.toLowerCase()===browserTag)||coverageEntries.map(([tag])=>tag).find(tag=>tag.toLowerCase()===browserTag.split('-')[0]);
language.value=initialTag||'default';
document.getElementById('countries').textContent=countries.length.toLocaleString();
document.getElementById('subdivisions').textContent=subdivisions.length.toLocaleString();
document.getElementById('cities').textContent=cities.length.toLocaleString();
document.getElementById('languages').textContent=data.report.languageCount.toLocaleString();
document.getElementById('version').textContent=`Snapshot / ${data.report.sourceVersion}`;
function updateCoverage(){
  if(language.value==='default'){document.getElementById('coverage').textContent='English alternate name when available; source name otherwise.';return;}
  const count=data.report.languageCoverage[language.value]||0;
  document.getElementById('coverage').textContent=`Direct names for ${count.toLocaleString()} of ${total.toLocaleString()} locations; the rest use the documented fallback.`;
}
function loadCountries(selectedId,selectedSubdivisionId,selectedCityId){
  country.innerHTML=options(countries);
  if(selectedId&&countries.some(x=>x.id===selectedId))country.value=String(selectedId);
  loadSubdivisions(selectedSubdivisionId,selectedCityId);
}
function loadSubdivisions(selectedId,selectedCityId){
  const id=Number(country.value);
  const values=subdivisions.filter(x=>x.pid===id);
  subdivision.innerHTML='<option value="direct">(Direct country cities)</option>'+options(values);
  subdivision.value=selectedId&&values.some(x=>x.id===selectedId)?String(selectedId):(values.length?String(values[0].id):'direct');
  loadCities(selectedCityId);
}
function currentCities(){
  const parent=subdivision.value==='direct'?Number(country.value):Number(subdivision.value);
  const query=search.value.trim().toLocaleLowerCase();
  return cities.filter(x=>x.pid===parent&&(!query||localizedName(x).toLocaleLowerCase().includes(query)||x.n.toLocaleLowerCase().includes(query)));
}
function loadCities(selectedId){
  const values=currentCities();
  city.innerHTML=options(values);
  if(selectedId&&values.some(x=>x.id===selectedId))city.value=String(selectedId);
  document.getElementById('detail').innerHTML=values.length?`<div><p class="place-name">${values.length.toLocaleString()} cities</p><p class="place-meta">Select one to inspect its source record.</p></div>`:'<div><p class="place-name">No match</p><p class="place-meta">Change the subdivision or clear the city filter.</p></div>';
}
function showCity(){
  const item=cities.find(x=>x.id===Number(city.value));
  if(!item)return;
  const shown=localizedName(item);
  const defaultNote=shown===item.n?'':`Default: ${item.n} · `;
  document.getElementById('detail').innerHTML=`<div><p class="place-name">${esc(shown)}</p><p class="place-meta">${esc(defaultNote)}GeoNames ${item.id} · ${item.fc} · population ${item.pop?.toLocaleString()??'unknown'}<br>${esc(item.why)}</p></div><div class="coords">LAT ${esc(item.lat)}<br>LON ${esc(item.lon)}</div>`;
}
language.onchange=()=>{const countryId=Number(country.value);const subdivisionId=subdivision.value==='direct'?null:Number(subdivision.value);const cityId=Number(city.value);updateCoverage();loadCountries(countryId,subdivisionId,cityId);showCity();};
country.onchange=()=>loadSubdivisions();
subdivision.onchange=()=>loadCities();
search.oninput=()=>loadCities();
city.onchange=showCity;
const quality=document.getElementById('quality');
quality.textContent=data.report.qualityStatus;
quality.className=data.report.qualityStatus==='PASS'?'ok':'bad';
document.getElementById('summary').textContent=`${data.report.localizedNameCount.toLocaleString()} localized names across ${data.report.languageCount.toLocaleString()} language tags. Removed ${data.report.duplicateCityOptionsRemoved} duplicate city options; ${data.findings.length} findings remain.`;
document.getElementById('findingRows').innerHTML=data.findings.length?data.findings.map(x=>`<tr><td>${esc(x.kind)}</td><td>${esc(x.type)}</td><td>${esc(x.name)}</td><td>${esc(x.detail)}</td></tr>`).join(''):'<tr><td colspan="4" class="ok">No unresolved findings.</td></tr>';
updateCoverage();
loadCountries();
</script>
</main></body></html>
""".replace("__PAYLOAD__", payload)
    path.write_text(document, encoding="utf-8")


def write_outputs(
    output_dir: Path,
    records: Sequence[LocationRecord],
    names: Sequence[LocalizedName],
    findings: Sequence[ReviewFinding],
    report: dict,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = output_dir / DATASET_FILE_NAME
    localized_names_path = output_dir / LOCALIZED_NAMES_FILE_NAME
    sqlite_path = output_dir / SQLITE_FILE_NAME
    html_path = output_dir / HTML_FILE_NAME
    findings_path = output_dir / FINDINGS_FILE_NAME

    write_csv(dataset_path, records)
    write_localized_names_csv(localized_names_path, names)
    write_findings_csv(findings_path, findings)
    write_sqlite(sqlite_path, records, names, report)
    write_review_html(html_path, records, names, findings, report)

    report["outputs"] = {
        DATASET_FILE_NAME: {"sha256": sha256_file(dataset_path)},
        LOCALIZED_NAMES_FILE_NAME: {"sha256": sha256_file(localized_names_path)},
        SQLITE_FILE_NAME: {"sha256": sha256_file(sqlite_path)},
        HTML_FILE_NAME: {"sha256": sha256_file(html_path)},
        FINDINGS_FILE_NAME: {"sha256": sha256_file(findings_path)},
    }
    (output_dir / REPORT_FILE_NAME).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report
