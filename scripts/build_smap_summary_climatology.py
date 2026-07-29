"""Build the 12-basin MERGE/INPE climatology for reviewed SMAP contours.

The input CSV is a local, pre-aggregated historical source and is intentionally
not vendored.  The generated bundle is the runtime asset.  For merged SMAP
basins, daily means are recomposed with the published historical-basin areas.
"""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path

from resumo_meteo.core.climatology import (
    aggregate_daily_basin_records,
    build_calendar_window_climatology,
    write_climatology_bundle_atomic,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src" / "resumo_meteo" / "core" / "data"
OUTPUT = DATA / "merge_inpe_2001_2020.climatology.json"
MANIFEST = DATA / "merge_inpe_2001_2020.manifest.json"

# Areas are the exact spherical areas (km²) of the historical masks whose daily
# means are present in MERGE_Daily_basin.  They are weights only; forecast
# aggregation always uses the reviewed SMAP contours.
MEMBERS = {
    "jacui": {"jacui": 13329.809},
    "uruguai": {"uruguai": 164690.296},
    "iguacu": {"iguacu": 67501.436},
    "parana": {"alto_parana": 103427.777, "baixo_parana": 245383.497},
    "paranapanema": {"paranapanema": 100872.033},
    "tiete": {"tiete": 62466.159},
    "grande": {"alto_grande": 51595.955, "baixo_grande": 87285.296},
    "paranaiba": {"alto_paranaiba": 94644.377, "baixo_paranaiba": 76754.679},
    "sao_francisco": {
        "alto_sao_francisco": 50694.663,
        "medio_sao_francisco": 448254.745,
        "baixo_sao_francisco": 131167.016,
    },
    "tocantins": {
        "alto_tocantins": 183178.325,
        "baixo_tocantins": 250487.158,
        "araguaia": 324359.019,
    },
    "madeira": {"madeira": 1504947.420},
    "xingu": {"xingu": 509991.452},
}


def main(source: Path) -> None:
    content = source.read_bytes()
    source_sha256 = hashlib.sha256(content).hexdigest()
    source_records = []
    with source.open("r", encoding="utf-8", newline="") as file_obj:
        for row in csv.DictReader(file_obj):
            observed_date = str(row["time"])
            if not "2001-01-01" <= observed_date <= "2020-12-31":
                continue
            for basin_id, value in row.items():
                if basin_id == "time":
                    continue
                source_records.append({"basin_id": basin_id, "date": observed_date, "mm": value})
    records = aggregate_daily_basin_records(source_records, basin_members=MEMBERS)
    bundle = build_calendar_window_climatology(
        records,
        climatology_id="merge-inpe-smap-aggregated-calendar-2001-2020-v2",
        version="2.0.0",
        reference_period="2001-2020",
        basin_definition_version="MERGE_DAILY_BASIN@historical-19-area-weighted-v2",
        # Leap-day windows occur in the five leap years of 2001--2020.
        minimum_complete_years=5,
        provider="MERGE_INPE",
        source_product="MERGE_DAILY_BASIN",
        source_path=source.name,
        source_sha256=source_sha256,
        source_mask_limitation=(
            "A climatologia MERGE/INPE foi recomposta a partir das 19 séries históricas "
            "pré-agregadas: nas cinco bacias SMAP agregadas, usa média diária ponderada "
            "pela área das máscaras históricas. A previsão e os mapas usam exclusivamente "
            "os contornos SMAP revisados; as máscaras históricas não foram disponibilizadas."
        ),
        expected_basin_ids=tuple(MEMBERS),
    )
    write_climatology_bundle_atomic(bundle, OUTPUT)
    output = OUTPUT.read_bytes()
    manifest = {
        "schema_version": 2,
        "asset_id": "merge-inpe-smap-aggregated-calendar-2001-2020-v2",
        "provider": "MERGE_INPE",
        "source_product": "MERGE_DAILY_BASIN",
        "reference_period": "2001-2020",
        "basin_definition_version": bundle["basin_definition_version"],
        "source_mask_limitation": bundle["source_mask_limitation"],
        "aggregation": {
            "method": "daily_area_weighted_mean_of_historical_basin_series",
            "historical_area_unit": "km2",
            "members": MEMBERS,
        },
        "bundle": {
            "path": "data/merge_inpe_2001_2020.climatology.json",
            "sha256": hashlib.sha256(output).hexdigest(),
            "size_bytes": len(output),
        },
        "source": {
            "filename": source.name,
            "sha256": source_sha256,
            "size_bytes": len(content),
            "reference_day_count": 7305,
        },
        "validation": {
            "basin_count": len(MEMBERS),
            "basin_ids": list(MEMBERS),
            "reference_value_count": len(records),
            "calendar_windows_per_basin": 732,
        },
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("uso: build_smap_summary_climatology.py /caminho/MERGE_Daily_basin_historical.csv")
    main(Path(sys.argv[1]).expanduser().resolve())
