from __future__ import annotations

import pandas as pd
import pytest
from fastapi import HTTPException

from app.services.farm_ingestion import upload_column_stats, validate_and_prepare_dataset


def test_duplicate_headers_are_rejected_before_numeric_cast():
    """weight and Weight both clean to weight; selecting that column is a DataFrame, which to_numeric rejects."""
    raw = pd.DataFrame(
        [["A1", "01/02/2024", "10.5", "11.0"]],
        columns=["EID", "Date", "weight", "Weight"],
    )

    with pytest.raises(HTTPException) as exc_info:
        validate_and_prepare_dataset(raw, "KFE")

    assert exc_info.value.status_code == 400
    assert "duplicate columns" in exc_info.value.detail
    assert "weight" in exc_info.value.detail


def test_collapsed_alias_collision_is_rejected():
    """pweight and p_weight collapse to the same canonical column."""
    raw = pd.DataFrame(
        [["A1", "01/02/2024", "10", "4"]],
        columns=["EID", "Date", "pweight", "p_weight"],
    )

    with pytest.raises(HTTPException) as exc_info:
        validate_and_prepare_dataset(raw, "KFE")

    assert exc_info.value.status_code == 400
    assert "pweight" in exc_info.value.detail


def test_empty_canonical_header_keeps_filled_alias():
    """AnimalValue is blank and Animal_Value holds the figures; keep those figures as animalvalue."""
    raw = pd.DataFrame(
        [["A1", "01/02/2024", "400", None, "12.5", None, "3.5"]],
        columns=["EID", "Date", "weight", "AnimalValue", "Animal_Value", "AnimalProd", "Animal_Prod"],
    )

    out = validate_and_prepare_dataset(raw, "WOW")

    assert list(out.columns).count("animalvalue") == 1
    assert list(out.columns).count("animalprod") == 1
    assert "animal_value" not in out.columns
    assert "animal_prod" not in out.columns
    assert out["animalvalue"].iloc[0] == pytest.approx(12.5)
    assert out["animalprod"].iloc[0] == pytest.approx(3.5)


def test_all_empty_alias_pair_collapses_to_one_column():
    raw = pd.DataFrame(
        [["A1", "01/02/2024", "400", None, None]],
        columns=["EID", "Date", "weight", "AnimalValue", "Animal_Value"],
    )

    out = validate_and_prepare_dataset(raw, "WOW")

    assert list(out.columns).count("animalvalue") == 1
    assert pd.isna(out["animalvalue"].iloc[0])


def test_upload_column_stats_count_values_and_blanks():
    frame = pd.DataFrame(
        {
            "breed": ["AA", "AA", None],
            "weight": [400.0, None, 410.0],
            "mob": ["Q3", "Q3", "Q4"],
        }
    )

    stats = upload_column_stats(frame)

    # One breed plus a blank, so the null counts as its own value.
    assert stats["breed"] == (2, True)
    assert stats["weight"] == (3, True)
    assert stats["mob"] == (2, True)
