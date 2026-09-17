from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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
    return command


def run(arguments: argparse.Namespace) -> int:
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
