from __future__ import annotations

import hashlib
import json
import shutil
import urllib.request
import zipfile
from collections.abc import Iterator, Mapping
from pathlib import Path

from .models import DatasetError


EXPECTED_ASSETS = {
    "countries",
    "subdivisions",
    "cities",
    "alternateNames",
    "chinaCities",
}


def load_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DatasetError(f"cannot read manifest: {path}") from error
    if not {"source", "sourceVersion", "assets"}.issubset(manifest):
        raise DatasetError("manifest is missing required fields")
    if set(manifest["assets"]) != EXPECTED_ASSETS:
        raise DatasetError("manifest assets do not match the source contract")
    for key, asset in manifest["assets"].items():
        if not {"fileName", "url", "size", "sha256"}.issubset(asset):
            raise DatasetError(f"manifest asset is incomplete: {key}")
    return manifest


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_path(manifest: Mapping, data_dir: Path, key: str) -> Path:
    return data_dir / manifest["assets"][key]["fileName"]


def verify_assets(manifest: Mapping, data_dir: Path) -> None:
    for asset in manifest["assets"].values():
        path = data_dir / asset["fileName"]
        if not path.is_file():
            raise DatasetError(f"missing asset: {path}")
        expected_size = int(asset["size"])
        if path.stat().st_size != expected_size:
            raise DatasetError(
                f"size mismatch for {path.name}: expected {expected_size}, "
                f"got {path.stat().st_size}"
            )
        expected_hash = str(asset["sha256"]).lower()
        actual_hash = sha256_file(path)
        if actual_hash != expected_hash:
            raise DatasetError(
                f"SHA256 mismatch for {path.name}: expected {expected_hash}, "
                f"got {actual_hash}"
            )


def download_assets(manifest: Mapping, data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for asset in manifest["assets"].values():
        target = data_dir / asset["fileName"]
        expected_hash = str(asset["sha256"]).lower()
        expected_size = int(asset["size"])
        if (
            target.is_file()
            and target.stat().st_size == expected_size
            and sha256_file(target) == expected_hash
        ):
            print(f"verified {target.name}")
            continue

        temporary = target.with_name(target.name + ".downloading")
        temporary.unlink(missing_ok=True)
        request = urllib.request.Request(
            asset["url"],
            headers={"User-Agent": "geonames-major-cities/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                with temporary.open("wb") as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
            if temporary.stat().st_size != expected_size:
                raise DatasetError(
                    f"downloaded size mismatch for {target.name}: expected "
                    f"{expected_size}, got {temporary.stat().st_size}"
                )
            actual_hash = sha256_file(temporary)
            if actual_hash != expected_hash:
                raise DatasetError(
                    f"downloaded SHA256 mismatch for {target.name}: "
                    f"expected {expected_hash}, got {actual_hash}"
                )
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        print(f"downloaded and verified {target.name}")


def text_lines(path: Path) -> Iterator[str]:
    with path.open("rt", encoding="utf-8") as source:
        yield from source


def zip_text_lines(path: Path) -> Iterator[str]:
    with zipfile.ZipFile(path) as archive:
        members = [name for name in archive.namelist() if not name.endswith("/")]
        expected = path.stem + ".txt"
        matches = [name for name in members if Path(name).name == expected]
        if len(matches) != 1:
            raise DatasetError(
                f"expected exactly one {expected} in {path.name}, got {members}"
            )
        with archive.open(matches[0]) as raw:
            for value in raw:
                yield value.decode("utf-8")
