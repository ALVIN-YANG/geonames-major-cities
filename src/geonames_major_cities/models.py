from __future__ import annotations

import unicodedata
from dataclasses import dataclass


LOCATION_TYPES = ("COUNTRY", "SUBDIVISION", "CITY")
OUTPUT_COLUMNS = (
    "type",
    "sourceId",
    "parentType",
    "parentSourceId",
    "code",
    "name",
    "countryCode",
    "admin1Code",
    "latitude",
    "longitude",
    "population",
    "featureCode",
    "selectionReason",
)
LOCALIZED_NAME_COLUMNS = (
    "type",
    "sourceId",
    "languageCode",
    "name",
    "isPreferred",
)


class DatasetError(RuntimeError):
    """Raised when pinned sources cannot produce a trustworthy dataset."""


@dataclass(frozen=True)
class LocationRecord:
    type: str
    sourceId: int
    parentType: str | None
    parentSourceId: int | None
    code: str | None
    name: str
    countryCode: str
    admin1Code: str | None
    latitude: str | None
    longitude: str | None
    population: int | None
    featureCode: str | None
    selectionReason: str


@dataclass(frozen=True)
class LocalizedName:
    type: str
    sourceId: int
    languageCode: str
    name: str
    isPreferred: bool


@dataclass(frozen=True)
class ReviewFinding:
    kind: str
    type: str
    sourceId: int
    parentSourceId: int | None
    name: str
    detail: str


def normalized_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return "".join(character for character in normalized if character.isalnum())
