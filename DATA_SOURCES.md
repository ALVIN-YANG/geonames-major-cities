# Data sources and attribution

This repository separates the software license from the licenses of its inputs and generated data.

## GeoNames

- Source: [GeoNames export dumps](https://download.geonames.org/export/dump/)
- Files used: `countryInfo.txt`, `admin1CodesASCII.txt`, `cities500.zip`, and `alternateNamesV2.zip`
- License: [Creative Commons Attribution 4.0](https://creativecommons.org/licenses/by/4.0/)
- Required attribution: data from [GeoNames](https://www.geonames.org/)

GeoNames IDs are retained in every generated row so records remain traceable to the source. The dated manifest pins the exact bytes used for a build.

## Chinese prefecture-level city policy

- Source repository: [`kk-418/cn-division`](https://github.com/kk-418/cn-division)
- Pinned commit: [`88f2021ea21769bdb95d93699d8a625fcd9165ef`](https://github.com/kk-418/cn-division/tree/88f2021ea21769bdb95d93699d8a625fcd9165ef)
- Pinned file: `dist/code/cities.json`
- Repository license: MIT
- Upstream description: the repository describes its data as based on the latest annual data from the Ministry of Civil Affairs place-name service (`dmfw`)

This input is used only as a deterministic inclusion policy for Chinese prefecture-level cities. The output records themselves are matched back to GeoNames and retain GeoNames IDs, names, coordinates, and population fields.

## Generated outputs

The source code is covered by this repository's MIT License. Generated datasets are derivative database outputs containing GeoNames data; redistributors must retain GeoNames attribution and comply with CC BY 4.0. The pinned Chinese policy source also remains attributed here under its MIT license.

This project does not imply endorsement by GeoNames, the `cn-division` maintainers, or any government agency.

## Audited supplementary sources

The optional `audit-sources` command measures, but does not merge, candidate names from:

- [Wikidata](https://www.wikidata.org/), licensed CC0. Exact QIDs and entity revision IDs are retained in the local audit cache.
- [Who's On First](https://www.whosonfirst.org/), whose records and upstream properties have mixed licenses. Its denormalized name columns do not expose sufficient field-level provenance for automatic merging, so results remain audit-only.
- [Overture Maps Divisions](https://docs.overturemaps.org/guides/divisions/), distributed under ODbL 1.0 with source details on individual properties. Overture results must remain separate from the MIT-licensed core unless an ODbL-compliant output is intentionally produced.

These sources are joined only by GeoNames IDs or Wikidata QIDs. A matching name alone is never accepted as identity evidence.
