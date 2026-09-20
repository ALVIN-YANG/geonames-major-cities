from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .audit import run_source_audit
from .builder import build_dataset
from .models import DatasetError
from .sources import download_assets, load_manifest, verify_assets


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(
        prog="geonames-major-cities",
        description=(
            "Build a reproducible country, subdivision, and major-city dataset "
            "from checksum-pinned GeoNames sources."
        ),
    )
    subcommands = command.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("download", "download and checksum-verify pinned source assets"),
        ("verify", "verify already downloaded source assets"),
        ("build", "build CSV, SQLite, review HTML, and quality report"),
        ("all", "download sources and then build every output"),
    ):
        child = subcommands.add_parser(name, help=help_text)
        child.add_argument(
            "--manifest",
            type=Path,
            default=Path("manifests/geonames-2026-09-17.json"),
        )
        child.add_argument("--data-dir", type=Path, default=Path("data"))
        if name in {"build", "all"}:
            child.add_argument("--output-dir", type=Path, default=Path("output"))
    audit = subcommands.add_parser(
        "audit-sources",
        help="audit exact-ID multilingual coverage from supplementary sources",
    )
    audit.add_argument("--dataset", type=Path, default=Path("output/locations.sqlite3"))
    audit.add_argument(
        "--geonames-alternate-names",
        type=Path,
        required=True,
        help="GeoNames alternateNamesV2.zip used to recover wkdt links",
    )
    audit.add_argument("--cache-dir", type=Path, default=Path("audit-cache"))
    audit.add_argument("--output-dir", type=Path, default=Path("audit-output"))
    audit.add_argument("--refresh", action="store_true")
    audit.add_argument("--skip-wikidata", action="store_true")
    audit.add_argument("--skip-wof", action="store_true")
    audit.add_argument("--skip-overture", action="store_true")
    audit.add_argument("--wikidata-workers", type=int, default=4)
    audit.add_argument("--overture-release", default="2026-08-19.0")
    return command


def run(arguments: argparse.Namespace) -> int:
    if arguments.command == "audit-sources":
        report = run_source_audit(
            dataset=arguments.dataset,
            alternate_names=arguments.geonames_alternate_names,
            cache_dir=arguments.cache_dir,
            output_dir=arguments.output_dir,
            refresh=arguments.refresh,
            include_wikidata=not arguments.skip_wikidata,
            include_wof=not arguments.skip_wof,
            include_overture=not arguments.skip_overture,
            wikidata_workers=arguments.wikidata_workers,
            overture_release=arguments.overture_release,
        )
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        return 0

    manifest = load_manifest(arguments.manifest)
    if arguments.command in {"download", "all"}:
        download_assets(manifest, arguments.data_dir)
    if arguments.command == "verify":
        verify_assets(manifest, arguments.data_dir)
        print("all pinned source assets verified")
    if arguments.command in {"build", "all"}:
        report = build_dataset(manifest, arguments.data_dir, arguments.output_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report["qualityStatus"] != "PASS":
            print(
                f"quality review required: {arguments.output_dir / 'quality-findings.csv'}",
                file=sys.stderr,
            )
            return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(parser().parse_args(argv))
    except DatasetError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
