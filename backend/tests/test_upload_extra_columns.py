"""Uploads keep non-canonical columns, and measure suggestions stick to the original seven."""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest
from fastapi import HTTPException

from app.db import execute, fetch_all, table_column_types
from app.services import farm_ingestion
from app.services.farm_backup import create_partition_snapshot, ensure_snapshot_tables, restore_partition_snapshot
from app.services.farm_ingestion import ingest_farm_data, sync_extra_columns
from app.services.variable_service import VariableService, invalidate_active_columns

FARM = "XTRA_TEST"
# Columns these tests may add; dropped again afterwards so the shared dev DB keeps its schema.
TEST_COLUMNS = ("zz_bodyscore", "zz_paddock", "zz_blank", "zz_order", "zz_extra_0", "zz_extra_1", "zz_extra_2")


def _columns(table: str) -> dict[str, str]:
    return {r["column_name"]: r["data_type"].upper() for r in table_column_types(table)}


@pytest.fixture(autouse=True)
def _clean_schema_and_rows():
    ensure_snapshot_tables()
    yield
    execute("DELETE FROM animal_data WHERE farm_id = %s", (FARM,))
    execute("DELETE FROM farm_columns WHERE farm_id = %s", (FARM,))
    execute("DELETE FROM animal_data_snapshots_data WHERE farm_id = %s", (FARM,))
    execute("DELETE FROM animal_data_snapshots WHERE farm_id = %s", (FARM,))
    # DuckDB can't drop a column from an indexed table, so drop the snapshot index around it.
    execute("DROP INDEX IF EXISTS idx_snapshots_data_id")
    for table in ("animal_data", "animal_data_snapshots_data"):
        for col in TEST_COLUMNS:
            execute(f'ALTER TABLE {table} DROP COLUMN IF EXISTS "{col}"')
    ensure_snapshot_tables()
    invalidate_active_columns()


def _upload_frame(**extra_cols) -> pd.DataFrame:
    base = pd.DataFrame(
        {
            "farm_id": FARM,
            "eid": ["E1", "E2", "E3"],
            "date": [date(2023, 1, 1), date(2023, 1, 2), date(2023, 1, 3)],
            "sex": "Steer",
            "breed": None,
            "treatment": None,
            "mob": None,
            "weight": [400.0, 410.0, 420.0],
        }
    )
    for name, values in extra_cols.items():
        base[name] = values
    return base


def test_new_columns_added_with_inferred_types():
    df = _upload_frame(
        zz_bodyscore=["3", "3.5", None],
        zz_paddock=["P1", "P2", " "],
        zz_blank=[None, "", "nan"],
    )
    out, res = sync_extra_columns(df)

    assert res["added"] == ["zz_bodyscore", "zz_paddock"]
    assert res["ignored"] == ["zz_blank"]
    assert "zz_blank" not in out.columns
    live, stored = _columns("animal_data"), _columns("animal_data_snapshots_data")
    assert live["zz_bodyscore"].startswith("DOUBLE") and live["zz_paddock"] == "VARCHAR"
    assert "zz_bodyscore" in stored and "zz_paddock" in stored
    assert out["zz_bodyscore"].tolist()[:2] == [3.0, 3.5]
    assert out["zz_paddock"].tolist() == ["P1", "P2", None]


def test_existing_column_keeps_its_type():
    sync_extra_columns(_upload_frame(zz_paddock=["P1", "P2", "P3"]))
    # A later file with only numeric-looking paddocks still stores them as text.
    out, res = sync_extra_columns(_upload_frame(zz_paddock=[1, 2, 3]))
    assert res["added"] == []
    assert out["zz_paddock"].tolist() == ["1", "2", "3"]
    assert _columns("animal_data")["zz_paddock"] == "VARCHAR"


def test_sql_keyword_column_is_stored_and_queryable():
    out, _ = sync_extra_columns(_upload_frame(zz_order=[1.0, 2.0, 3.0]))
    ingest_farm_data(FARM, out)
    rows = fetch_all('SELECT SUM("zz_order") AS s FROM animal_data WHERE farm_id = %s', (FARM,))
    assert rows[0]["s"] == 6.0


def test_too_many_new_columns_rejected_before_altering(monkeypatch):
    monkeypatch.setattr(farm_ingestion, "MAX_NEW_COLUMNS_PER_UPLOAD", 2)
    df = _upload_frame(zz_extra_0=[1, 2, 3], zz_extra_1=[1, 2, 3], zz_extra_2=[1, 2, 3])
    with pytest.raises(HTTPException) as exc:
        sync_extra_columns(df)
    assert exc.value.status_code == 400
    assert not {"zz_extra_0", "zz_extra_1", "zz_extra_2"} & set(_columns("animal_data"))


def test_extra_column_data_survives_snapshot_revert():
    out, _ = sync_extra_columns(_upload_frame(zz_paddock=["P1", "P2", "P3"]))
    ingest_farm_data(FARM, out)
    snapshot_id = create_partition_snapshot(FARM, "before_change")
    execute("UPDATE animal_data SET zz_paddock = 'CHANGED' WHERE farm_id = %s", (FARM,))

    restore_partition_snapshot(FARM, snapshot_id)

    rows = fetch_all("SELECT zz_paddock FROM animal_data WHERE farm_id = %s ORDER BY eid", (FARM,))
    assert [r["zz_paddock"] for r in rows] == ["P1", "P2", "P3"]


def test_suggestions_show_only_original_measures_with_data():
    """finalpweight has no data here, so weight (a weight measure with data) is shown instead."""
    out, _ = sync_extra_columns(_upload_frame(zz_bodyscore=[3, 4, 5], zz_paddock=["P1", "P2", "P2"]))
    out["methane"] = [100.0, 110.0, 120.0]
    ingest_farm_data(FARM, out)

    cols = {c["column_name"]: c for c in VariableService.inspect_and_suggest_columns(FARM, force_rescan=True)}
    shown = {name for name, c in cols.items() if c["is_shown"]}
    assert shown == {"methane", "weight"}
    assert cols["zz_paddock"]["is_filter"] is True

    res = VariableService.on_farm_upload(FARM)
    assert res["initial_ingest"] is True
    # Only columns this farm has data in are reported, not every column in the shared table.
    assert "zz_bodyscore" in res["new_columns_detected"]
    assert "carcassweight" not in res["new_columns_detected"]


def test_headers_that_collapse_to_the_same_canonical_column_stay_separate():
    """AnimalValue and Animal_Value both collapse to animalvalue: the frame must not carry duplicates.

    Regression for the ABC_2026-07-15.csv upload, which failed with "arg must be a list, tuple,
    1-d array, or Series" because two columns renamed to the same canonical name and pd.to_numeric
    was handed a DataFrame instead of a Series.
    """
    df = pd.DataFrame(
        {
            "EID": ["951 000327788544"],
            "Date": ["01/07/2026"],
            "weight": [486.0],
            "AnimalValue": [2371.42],
            "Animal_Value": [2371.42],
            "AnimalProd": [1.83],
            "Animal_Prod": [1.83],
        }
    )

    out = farm_ingestion.validate_and_prepare_dataset(df, FARM)

    cols = list(out.columns)
    assert len(cols) == len(set(cols)), f"duplicate columns after mapping: {cols}"
    assert {"animalvalue", "animal_value"} <= set(cols)
    assert {"animalprod", "animal_prod"} <= set(cols)
    assert len(out) == 1
