from __future__ import annotations

import io
import logging
import re
from typing import Any, Callable

import pandas as pd
from fastapi import HTTPException

from app.constants import CSV_COLUMN_MAP
from app.db import execute, fetch_all, fetch_one, get_active_engine, get_conn, table_column_types

logger = logging.getLogger(__name__)

CANONICAL_COL_MAP = {
    "eid": "eid",
    "date": "date",
    "breed": "breed",
    "treatment": "treatment",
    "mob": "mob",
    "sex": "sex",
    "weight": "weight",
    "pweight": "pweight",
    "growthpbs": "growthpbs",
    "finalpweight": "finalpweight",
    "finalgrowthpbs": "finalgrowthpbs",
    "finaldailygrowth": "finaldailygrowth",
    "feedintakekgd": "feedintakekgd",
    "feedintakepct": "feedintakepct",
    "methane": "methane",
    "animalvalue": "animalvalue",
    "animalprod": "animalprod",
    "feedintakekgdsum": "feedintakekgdsum",
    "finalgrowthpbssum": "finalgrowthpbssum",
    "animalprodsum": "animalprodsum",
    "methanesum": "methanesum",
    "methanesupplsum": "methanesupplsum",
    "carcassweight": "carcassweight",
    "dressedcarcass": "dressedcarcass",
}

NUMERIC_COLS = [c for c in CANONICAL_COL_MAP.values() if c not in ("eid", "date", "breed", "treatment", "mob", "sex")]
WEIGHT_MEASURE_COLS = {"weight", "pweight", "finalpweight"}
BASE_INSERT_COLS = ["farm_id", "eid", "date", "breed", "treatment", "mob", "sex"] + NUMERIC_COLS

# Upload columns outside the canonical set are kept as extra animal_data columns (sync_extra_columns).
# Cleaned headers are already lowercase identifiers; 63 is PostgreSQL's identifier limit, and
# snapshot_id would clash with the snapshot storage table's own key.
EXTRA_COLUMN_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
NON_EXTRA_COLUMN_NAMES = {"snapshot_id"}
# More than this many brand-new columns in one file is far more likely a wrong file than new variables.
MAX_NEW_COLUMNS_PER_UPLOAD = 50


def clean_column_names(col_names: list[str]) -> list[str]:
    """Clean column names to match R's farm_utils clean_column_names function."""
    cleaned: list[str] = []
    for idx, c in enumerate(col_names, start=1):
        s = str(c).strip().lower()
        s = re.sub(r"[^a-z0-9_]+", "_", s)
        s = re.sub(r"_+", "_", s)
        s = s.strip("_")
        if s and s[0].isdigit():
            s = f"x{s}"
        if not s:
            s = f"column_{idx}"
        cleaned.append(s)
    return cleaned


def parse_australian_dates(series: pd.Series) -> pd.Series:
    """Parse dates with DD/MM/YYYY priority, fallback to YYYY-MM-DD, then MM/DD/YYYY."""
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series)

    s = series.astype(str).str.strip()

    # 1. Australian standard: DD/MM/YYYY
    parsed = pd.to_datetime(s, format="%d/%m/%Y", errors="coerce")

    # 2. ISO standard: YYYY-MM-DD
    mask_nat = parsed.isna() & s.notna() & ~s.isin(["", "nan", "None", "nat", "NaT"])
    if mask_nat.any():
        parsed_iso = pd.to_datetime(s[mask_nat], format="%Y-%m-%d", errors="coerce")
        parsed = parsed.fillna(parsed_iso)

    # 3. Fallback: MM/DD/YYYY
    mask_nat = parsed.isna() & s.notna() & ~s.isin(["", "nan", "None", "nat", "NaT"])
    if mask_nat.any():
        parsed_mdy = pd.to_datetime(s[mask_nat], format="%m/%d/%Y", errors="coerce")
        parsed = parsed.fillna(parsed_mdy)

    # 4. Final heuristic with dayfirst=True
    mask_nat = parsed.isna() & s.notna() & ~s.isin(["", "nan", "None", "nat", "NaT"])
    if mask_nat.any():
        parsed_dayfirst = pd.to_datetime(s[mask_nat], dayfirst=True, errors="coerce")
        parsed = parsed.fillna(parsed_dayfirst)

    return parsed


def _reject_ragged_csv(content: bytes) -> None:
    """pandas 3 pads/truncates ragged rows silently, which corrupts column alignment.

    Reject the file up front with the offending line number instead of ingesting
    shifted data. Quote-aware so commas inside quoted fields don't miscount.
    """
    text = content.decode("utf-8", errors="replace")
    expected = None
    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line:
            continue
        fields, in_quotes = 1, False
        for ch in line:
            if ch == '"':
                in_quotes = not in_quotes
            elif ch == "," and not in_quotes:
                fields += 1
        if expected is None:
            expected = fields
        elif fields != expected:
            raise HTTPException(
                status_code=400,
                detail=f"Malformed CSV: line {line_no} has {fields} fields, expected {expected}. "
                       "Fix or remove the extra commas on that line and re-upload.",
            )


def load_table_from_bytes(content: bytes, filename: str) -> pd.DataFrame:
    """Load DataFrame from raw bytes supporting both CSV and Excel format."""
    is_excel = filename.lower().endswith(".xlsx") or content.startswith(b"PK\x03\x04")
    if is_excel:
        return pd.read_excel(io.BytesIO(content))
    _reject_ragged_csv(content)
    return pd.read_csv(io.BytesIO(content))


def validate_and_prepare_dataset(df: pd.DataFrame, farm_id: str) -> pd.DataFrame:
    """Validate mandatory fields, apply cleaning and column mapping, and format types."""
    if df.empty:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    original_cols = list(df.columns)
    cleaned_headers = clean_column_names(original_cols)

    # Map cleaned headers to canonical database columns. Two headers can collapse onto the same
    # canonical name (AnimalValue and Animal_Value both mean animalvalue), so the first one keeps
    # the canonical column and the rest fall back to their own cleaned name as extra columns.
    # Without this, the frame ends up with duplicate column names and the numeric cast fails.
    col_mapping: dict[str, str] = {}
    used_targets: set[str] = set()
    for orig, cleaned in zip(original_cols, cleaned_headers):
        collapsed = cleaned.replace("_", "")
        if cleaned in CANONICAL_COL_MAP:
            target = CANONICAL_COL_MAP[cleaned]
        elif collapsed in CANONICAL_COL_MAP:
            target = CANONICAL_COL_MAP[collapsed]
        else:
            target = cleaned
        if target in used_targets:
            base = target = cleaned
            suffix = 2
            while target in used_targets:
                target = f"{base}_{suffix}"
                suffix += 1
        used_targets.add(target)
        col_mapping[orig] = target

    df_norm = df.rename(columns=col_mapping)

    # Two headers that clean to the same name (weight + Weight, pweight + p_weight)
    # make df[col] a DataFrame. pd.to_numeric then raises TypeError instead of a 400.
    duplicated = list(dict.fromkeys(df_norm.columns[df_norm.columns.duplicated()]))
    if duplicated:
        names = ", ".join(str(name) for name in duplicated)
        raise HTTPException(
            status_code=400,
            detail=(
                f"File has duplicate columns after header cleanup: {names}. "
                "Rename or remove the extra column and re-upload."
            ),
        )

    # Check mandatory columns
    if "eid" not in df_norm.columns or "date" not in df_norm.columns:
        raise HTTPException(status_code=400, detail="CSV must contain EID and Date columns")

    # Check weight measure requirement
    available_measures = [c for c in WEIGHT_MEASURE_COLS if c in df_norm.columns]
    if not available_measures:
        raise HTTPException(
            status_code=400,
            detail="File must contain at least one numeric weight measure ('weight' or 'pweight')",
        )

    # Process and drop invalid EIDs
    df_norm["eid"] = df_norm["eid"].astype(str).str.strip()
    valid_eid = df_norm["eid"].notna() & (df_norm["eid"] != "") & (df_norm["eid"].str.lower() != "nan")

    # Process and drop invalid dates
    df_norm["date"] = parse_australian_dates(df_norm["date"])
    valid_date = df_norm["date"].notna()

    df_ready = df_norm[valid_eid & valid_date].copy()
    if df_ready.empty:
        raise HTTPException(status_code=400, detail="File contains no rows with both a valid EID and parseable Date")

    df_ready["farm_id"] = farm_id

    if "sex" not in df_ready.columns:
        df_ready["sex"] = "Unknown"
    else:
        df_ready["sex"] = df_ready["sex"].fillna("Unknown").astype(str).str.strip()
        df_ready.loc[df_ready["sex"] == "", "sex"] = "Unknown"

    if "treatment" not in df_ready.columns:
        df_ready["treatment"] = None
    else:
        df_ready["treatment"] = df_ready["treatment"].replace({"": None, "nan": None, "None": None})

    for c in ["breed", "mob"]:
        if c in df_ready.columns:
            df_ready[c] = df_ready[c].replace({"": None, "nan": None, "None": None})
        else:
            df_ready[c] = None

    # Cast numeric columns
    for num_col in NUMERIC_COLS:
        if num_col in df_ready.columns:
            df_ready[num_col] = pd.to_numeric(df_ready[num_col], errors="coerce")
        else:
            df_ready[num_col] = None

    # Convert date to Python date objects
    df_ready["date"] = df_ready["date"].dt.date

    # Deduplicate within the uploaded file itself
    df_ready = df_ready.drop_duplicates(subset=["farm_id", "eid", "date"], keep="last")

    return df_ready


def _text_or_none(val: Any) -> str | None:
    if val is None or (isinstance(val, float) and val != val):
        return None
    text = str(val).strip()
    return None if text in ("", "nan", "None") else text


def sync_extra_columns(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, list[str]]]:
    """Keep an upload's non-canonical columns instead of dropping them.

    New columns are added to animal_data (and to snapshot storage, so reverts keep their data):
    numeric when every non-empty value parses as a number, otherwise text. Columns that already
    exist keep their type and the upload's values are coerced to it. Empty columns, unusable names
    and columns of other types are left out. Returns the prepared frame and {"added", "ignored"}.
    """
    from app.services.farm_backup import ensure_snapshot_tables
    from app.services.variable_service import VariableService

    extras = [c for c in df.columns if c not in BASE_INSERT_COLS]
    if not extras:
        return df, {"added": [], "ignored": []}

    existing = {
        r["column_name"]: VariableService.classify_data_type(r["column_name"], r["data_type"])
        for r in table_column_types("animal_data")
    }
    kinds: dict[str, str] = {}
    ignored: list[str] = []
    for col in extras:
        values = df[col].map(_text_or_none).dropna()
        usable_name = EXTRA_COLUMN_NAME.match(col) and col not in NON_EXTRA_COLUMN_NAMES
        if not usable_name or values.empty:
            ignored.append(col)
        elif col in existing:
            if existing[col] in ("numeric", "categorical"):
                kinds[col] = existing[col]
            else:
                ignored.append(col)
        else:
            is_numeric = pd.to_numeric(values, errors="coerce").notna().all()
            kinds[col] = "numeric" if is_numeric else "categorical"

    added = [c for c in kinds if c not in existing]
    if len(added) > MAX_NEW_COLUMNS_PER_UPLOAD:
        raise HTTPException(
            status_code=400,
            detail=f"File adds {len(added)} new columns (max {MAX_NEW_COLUMNS_PER_UPLOAD} per upload); check it is the right file",
        )
    if added:
        ensure_snapshot_tables()
    for col in added:
        sql_type = "DOUBLE PRECISION" if kinds[col] == "numeric" else "VARCHAR"
        execute(f'ALTER TABLE animal_data ADD COLUMN IF NOT EXISTS "{col}" {sql_type}')
        try:
            execute(f'ALTER TABLE animal_data_snapshots_data ADD COLUMN IF NOT EXISTS "{col}" {sql_type}')
        except Exception as exc:
            # Snapshots then skip this column (farm_backup copies the columns both tables share).
            logger.warning("Could not add %s to snapshot storage: %s", col, exc)

    out = df.drop(columns=ignored)
    for col, kind in kinds.items():
        if kind == "numeric":
            out[col] = pd.to_numeric(out[col], errors="coerce")
        else:
            mapped = out[col].map(_text_or_none)
            out[col] = mapped.astype(object).where(mapped.notna(), None)
    return out, {"added": added, "ignored": ignored}


def ingest_farm_data(
    farm_id: str,
    df: pd.DataFrame,
    duplicate_mode: str = "skip",
    fetch_one_fn: Callable[..., Any] | None = None,
    execute_fn: Callable[..., Any] | None = None,
    is_mocked: bool = False,
) -> dict[str, int]:
    """Execute bulk upsert with duplicate skip/overwrite support across DuckDB and Postgres."""
    if df.empty:
        return {"rows_inserted": 0, "rows_skipped": 0, "rows_overwritten": 0}

    # If running in a test where fetch_one / execute have been monkeypatched
    if is_mocked and fetch_one_fn and execute_fn:
        inserted = skipped = overwritten = 0
        numeric_cols = [c for c in NUMERIC_COLS if c in df.columns]
        for _, row in df.iterrows():
            key = (farm_id, str(row["eid"]), row["date"])
            existing = fetch_one_fn(
                "SELECT eid FROM animal_data WHERE farm_id = %s AND eid = %s AND date = %s",
                key,
            )
            if existing:
                if duplicate_mode == "overwrite":
                    sets = ", ".join(f"{c} = %s" for c in numeric_cols if c in row.index)
                    vals = [row[c] if pd.notna(row[c]) else None for c in numeric_cols]
                    execute_fn(
                        f"UPDATE animal_data SET breed=%s, treatment=%s, mob=%s, sex=%s, {sets} "
                        "WHERE farm_id = %s AND eid = %s AND date = %s",
                        (
                            row.get("breed"),
                            row.get("treatment"),
                            row.get("mob"),
                            row.get("sex", "Unknown"),
                            *vals,
                            *key,
                        ),
                    )
                    overwritten += 1
                else:
                    skipped += 1
                continue

            cols = ["farm_id", "eid", "date", "breed", "treatment", "mob", "sex"] + [
                c for c in numeric_cols if c in row.index
            ]
            placeholders = ", ".join(["%s"] * len(cols))
            vals = [farm_id, str(row["eid"]), row["date"]]
            for c in ["breed", "treatment", "mob", "sex"]:
                vals.append(row.get(c) if c != "sex" else row.get(c, "Unknown"))
            for c in numeric_cols:
                if c in row.index:
                    vals.append(row[c] if pd.notna(row[c]) else None)
            execute_fn(f"INSERT INTO animal_data ({', '.join(cols)}) VALUES ({placeholders})", tuple(vals))
            inserted += 1
        return {"rows_inserted": inserted, "rows_skipped": skipped, "rows_overwritten": overwritten}

    # Vectorized bulk upsert. Any extra columns were added to animal_data by sync_extra_columns;
    # names are quoted because an uploaded header can be an SQL keyword (e.g. "order").
    insert_cols = BASE_INSERT_COLS + [c for c in df.columns if c not in BASE_INSERT_COLS]
    staged_df = df.reindex(columns=insert_cols)
    cols_joined = ", ".join(f'"{c}"' for c in insert_cols)
    total_staged = len(staged_df)

    engine = get_active_engine()
    with get_conn() as conn:
        cur = conn.cursor()
        if engine == "duckdb":
            cur.execute("BEGIN TRANSACTION")
            try:
                cur.register("temp_staged", staged_df)
                dup_row = cur.execute(
                    """
                    SELECT COUNT(*) AS cnt
                    FROM temp_staged s
                    JOIN animal_data a ON a.farm_id = s.farm_id AND a.eid = s.eid AND a.date = s.date
                    """
                ).fetchone()
                duplicate_count = dup_row[0] if dup_row else 0

                if duplicate_mode == "overwrite":
                    cur.execute(
                        """
                        DELETE FROM animal_data a
                        USING temp_staged s
                        WHERE a.farm_id = s.farm_id AND a.eid = s.eid AND a.date = s.date
                        """
                    )
                    cur.execute(f"INSERT INTO animal_data ({cols_joined}) SELECT {cols_joined} FROM temp_staged")
                    rows_overwritten = duplicate_count
                    rows_inserted = total_staged - duplicate_count
                    rows_skipped = 0
                else:
                    cur.execute(
                        f"""
                        INSERT INTO animal_data ({cols_joined})
                        SELECT {cols_joined}
                        FROM temp_staged s
                        WHERE NOT EXISTS (
                            SELECT 1 FROM animal_data a
                            WHERE a.farm_id = s.farm_id AND a.eid = s.eid AND a.date = s.date
                        )
                        """
                    )
                    rows_skipped = duplicate_count
                    rows_inserted = total_staged - duplicate_count
                    rows_overwritten = 0

                cur.unregister("temp_staged")
                cur.execute("COMMIT")
            except Exception:
                cur.execute("ROLLBACK")
                raise
        else:
            # PostgreSQL vectorized path
            from psycopg2.extras import execute_values

            cur.execute(
                f"""
                CREATE TEMP TABLE temp_staged (LIKE animal_data INCLUDING DEFAULTS) ON COMMIT DROP;
                """
            )
            data_tuples = [tuple(r) for r in staged_df.to_dict("split")["data"]]
            execute_values(cur, f"INSERT INTO temp_staged ({cols_joined}) VALUES %s", data_tuples, page_size=5000)

            cur.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM temp_staged s
                JOIN animal_data a ON a.farm_id = s.farm_id AND a.eid = s.eid AND a.date = s.date
                """
            )
            dup_row = cur.fetchone()
            duplicate_count = dup_row["cnt"] if isinstance(dup_row, dict) else (dup_row[0] if dup_row else 0)

            if duplicate_mode == "overwrite":
                cur.execute(
                    """
                    DELETE FROM animal_data a
                    USING temp_staged s
                    WHERE a.farm_id = s.farm_id AND a.eid = s.eid AND a.date = s.date
                    """
                )
                cur.execute(f"INSERT INTO animal_data ({cols_joined}) SELECT {cols_joined} FROM temp_staged")
                rows_overwritten = duplicate_count
                rows_inserted = total_staged - duplicate_count
                rows_skipped = 0
            else:
                cur.execute(
                    f"""
                    INSERT INTO animal_data ({cols_joined})
                    SELECT {cols_joined}
                    FROM temp_staged s
                    WHERE NOT EXISTS (
                        SELECT 1 FROM animal_data a
                        WHERE a.farm_id = s.farm_id AND a.eid = s.eid AND a.date = s.date
                    )
                    """
                )
                rows_skipped = duplicate_count
                rows_inserted = total_staged - duplicate_count
                rows_overwritten = 0

    return {
        "rows_inserted": max(0, rows_inserted),
        "rows_skipped": max(0, rows_skipped),
        "rows_overwritten": max(0, rows_overwritten),
    }

