from __future__ import annotations

import logging
import re
import time
from typing import Any

from app.constants import CSV_COLUMN_MAP, MEASURE_CHOICES
from app.db import FARM_COLUMNS_DDL, execute, fetch_all, fetch_one, get_conn, table_column_types

logger = logging.getLogger(__name__)

NUMERIC_TYPE_PREFIXES = (
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "FLOAT",
    "REAL",
    "DOUBLE",
    "DECIMAL",
    "NUMERIC",
    "INT",
    "INT2",
    "INT4",
    "INT8",
    "FLOAT4",
    "FLOAT8",
)
CATEGORICAL_TYPE_PREFIXES = ("VARCHAR", "TEXT", "CHAR", "BOOLEAN", "BOOL", "CHARACTER")
RESERVED_FILTER_NAMES = {
    "year",
    "month",
    "day",
    "eid",
    "date",
    "measure",
    "measures",
    "group",
    "orig_eid",
    "active_only",
    "active_days",
    "active_reference_mode",
    "farm_id",
}
REQUIRED_WEIGHT_MEASURES = {"weight", "pweight", "finalpweight"}

# Filters with dedicated FilterState fields and group-comparison support; any other enabled
# categorical column is an "extra" filter that only narrows the data (FilterState.extra_filters).
BUILTIN_FILTER_COLUMNS = ("sex", "treatment", "breed", "mob")
# What a farm gets before an admin has saved a configuration for it.
DEFAULT_FILTER_COLUMNS = list(BUILTIN_FILTER_COLUMNS)
DEFAULT_MEASURE_COLUMNS = list(MEASURE_CHOICES)

# Column names are interpolated into SQL, so anything else is never used as a filter or measure.
SAFE_COLUMN_NAME = re.compile(r"^[a-z_][a-z0-9_]*$")

# Per-instance cache of each farm's enabled columns; every analytical request reads it via
# FilterState. Saving a configuration clears it here; other warm instances catch up within the TTL.
_ACTIVE_COLUMNS_CACHE: dict[str, tuple[float, dict[str, list[str]]]] = {}
_ACTIVE_COLUMNS_TTL_SEC = 60.0


def invalidate_active_columns(farm_id: str | None = None) -> None:
    if farm_id is None:
        _ACTIVE_COLUMNS_CACHE.clear()
    else:
        _ACTIVE_COLUMNS_CACHE.pop(farm_id, None)


_CANONICAL_COLUMNS = list(CSV_COLUMN_MAP)
# Default measure when the farm hasn't enabled finalpweight: observed weight before the smoothed pweight.
_DEFAULT_MEASURE_PREFERENCE = ("finalpweight", "weight", "pweight")


def _measure_sort_key(col: str) -> tuple[int, int, str]:
    """The original seven first, then other canonical columns in schema order, then the rest by name."""
    if col in MEASURE_CHOICES:
        return (0, MEASURE_CHOICES.index(col), col)
    if col in _CANONICAL_COLUMNS:
        return (1, _CANONICAL_COLUMNS.index(col), col)
    return (2, 0, col)


def _filter_sort_key(col: str) -> tuple[int, int, str]:
    if col in BUILTIN_FILTER_COLUMNS:
        return (0, BUILTIN_FILTER_COLUMNS.index(col), col)
    return (1, 0, col)


class VariableService:
    @classmethod
    def ensure_schema(cls) -> None:
        """Ensure farm_columns table and indices exist across active database engines."""
        try:
            execute(FARM_COLUMNS_DDL)
        except Exception as exc:
            logger.warning("Could not execute FARM_COLUMNS_DDL: %s", exc)

    @staticmethod
    def classify_data_type(column_name: str, data_type: str) -> str:
        """Classify column into id, date, numeric, categorical, or other."""
        col = column_name.lower().strip()
        dt = data_type.upper().strip()
        if col in ("eid", "farm_id"):
            return "id"
        if col == "date":
            return "date"
        if any(dt.startswith(p) for p in NUMERIC_TYPE_PREFIXES):
            return "numeric"
        if any(dt.startswith(p) for p in CATEGORICAL_TYPE_PREFIXES):
            return "categorical"
        return "other"

    @staticmethod
    def _animal_data_column_types() -> list[dict[str, Any]]:
        """animal_data's columns and SQL types, in table order ([] if the schema can't be read)."""
        try:
            return table_column_types("animal_data")
        except Exception as exc:
            logger.warning("Failed querying information_schema for animal_data: %s", exc)
            return []

    @classmethod
    def inspect_and_suggest_columns(
        cls,
        farm_id: str,
        force_rescan: bool = False,
        column_stats: dict[str, tuple[int, bool]] | None = None,
    ) -> list[dict[str, Any]]:
        """Inspect animal_data schema for farm_id and calculate distinct counts with NULL weighting.

        Suggestions (for columns without a saved setting, or all of them on force_rescan):
        categorical columns with 2-50 distinct values become filters; of the numeric columns only the
        original seven measures (MEASURE_CHOICES) are shown, and only when the farm has data in them.
        Other numeric columns stay available for an admin to opt into.
        """
        cls.ensure_schema()
        type_rows = cls._animal_data_column_types()
        if not type_rows:
            return []

        saved_rows = fetch_all(
            "SELECT column_name, kind, is_filter, is_shown, distinct_count FROM farm_columns WHERE farm_id = %s",
            (farm_id,),
        )
        saved_map = {r["column_name"]: r for r in saved_rows}

        measurable = [
            r["column_name"]
            for r in type_rows
            if cls.classify_data_type(r["column_name"], r["data_type"]) in ("categorical", "numeric")
            and SAFE_COLUMN_NAME.match(r["column_name"])
        ]
        # An upload passes counts from the file it just stored. Otherwise one query covers
        # this farm; animal_data also holds every other farm's columns.
        stats = column_stats if column_stats is not None else cls._value_stats(farm_id, measurable)

        results: list[dict[str, Any]] = []
        for r in type_rows:
            col_name = r["column_name"]
            kind = cls.classify_data_type(col_name, r["data_type"])
            is_reserved = col_name.lower() in RESERVED_FILTER_NAMES

            distinct_count = 0
            has_data = False
            if col_name in stats:
                distinct_count, has_data = stats[col_name]
            elif column_stats is not None and col_name in saved_map:
                # This file did not include the column. Keep the count already stored for it.
                distinct_count = int(saved_map[col_name].get("distinct_count") or 0)

            suggested = col_name not in saved_map or force_rescan
            if not suggested:
                is_filter = bool(saved_map[col_name]["is_filter"]) and kind == "categorical" and not is_reserved
                is_shown = bool(saved_map[col_name]["is_shown"]) and kind == "numeric"
            else:
                is_filter = (
                    kind == "categorical"
                    and 2 <= distinct_count <= 50
                    and not is_reserved
                )
                is_shown = kind == "numeric" and col_name in MEASURE_CHOICES and has_data

            results.append(
                {
                    "farm_id": farm_id,
                    "column_name": col_name,
                    "kind": kind,
                    "distinct_count": distinct_count,
                    "is_filter": is_filter,
                    "is_shown": is_shown,
                    "is_reserved": is_reserved,
                    # Internal: whether this farm has any values in the column (not part of the API).
                    "has_data": has_data,
                }
            )

        # Saving requires a weight measure, and finalpweight may be missing from a farm's data:
        # then suggest its first weight measure that has data (weight before pweight).
        # If no weight measure has data (e.g. cold-start or empty farm), fall back to enabling
        # the first preferred numeric weight measure so save_farm_columns invariants are satisfied.
        if not any(c["is_shown"] and c["column_name"] in REQUIRED_WEIGHT_MEASURES for c in results):
            by_name = {c["column_name"]: c for c in results}
            for name in _DEFAULT_MEASURE_PREFERENCE:
                col = by_name.get(name)
                if col and col["kind"] == "numeric" and col["has_data"]:
                    col["is_shown"] = True
                    break
            else:
                for name in _DEFAULT_MEASURE_PREFERENCE:
                    col = by_name.get(name)
                    if col and col["kind"] == "numeric":
                        col["is_shown"] = True
                        break
                else:
                    for col in results:
                        if col["kind"] == "numeric" and col["column_name"] in REQUIRED_WEIGHT_MEASURES:
                            col["is_shown"] = True
                            break

        return results

    @classmethod
    def _value_stats(cls, farm_id: str, columns: list[str]) -> dict[str, tuple[int, bool]]:
        """Distinct count (nulls count as one extra value) and whether the column has any value.

        One query for the whole farm. Falls back to a query per column if that statement fails,
        so one unreadable column does not zero the rest.
        """
        safe = [name for name in columns if SAFE_COLUMN_NAME.match(name)]
        if not safe:
            return {}
        selects = ["COUNT(*) AS row_cnt"]
        for name in safe:
            selects.append(f'COUNT(DISTINCT "{name}") AS "{name}__d"')
            selects.append(f'COUNT("{name}") AS "{name}__n"')
        try:
            row = fetch_one(
                f"SELECT {', '.join(selects)} FROM animal_data WHERE farm_id = %s",
                (farm_id,),
            )
        except Exception as exc:
            logger.warning("Combined column stats failed for farm %s: %s", farm_id, exc)
            return cls._value_stats_one_by_one(farm_id, safe)
        if not row:
            return {name: (0, False) for name in safe}
        lowered = {str(key).lower(): value for key, value in row.items()}
        total = int(lowered.get("row_cnt") or 0)
        stats: dict[str, tuple[int, bool]] = {}
        for name in safe:
            distinct = int(lowered.get(f"{name}__d") or 0)
            present = int(lowered.get(f"{name}__n") or 0)
            stats[name] = (distinct + (1 if total > present else 0), distinct > 0)
        return stats

    @classmethod
    def _value_stats_one_by_one(cls, farm_id: str, columns: list[str]) -> dict[str, tuple[int, bool]]:
        stats: dict[str, tuple[int, bool]] = {}
        for name in columns:
            try:
                cnt_row = fetch_one(
                    f"""
                    SELECT
                        COUNT(DISTINCT "{name}") AS dist_cnt,
                        CASE WHEN COUNT(*) > COUNT("{name}") THEN 1 ELSE 0 END AS has_null
                    FROM animal_data
                    WHERE farm_id = %s
                    """,
                    (farm_id,),
                )
            except Exception as exc:
                logger.warning("Could not calculate distinct count for %s on farm %s: %s", name, farm_id, exc)
                continue
            if not cnt_row:
                stats[name] = (0, False)
                continue
            distinct = int(cnt_row.get("dist_cnt") or 0)
            stats[name] = (distinct + int(cnt_row.get("has_null") or 0), distinct > 0)
        return stats

    @classmethod
    def save_farm_columns(cls, farm_id: str, columns: list[dict[str, Any]]) -> None:
        """Validate mandatory weight measure invariant and persist configurations atomically.

        Column names must be real animal_data columns, and each column's kind comes from the live
        schema rather than the request, so a client can't mark a text column as a measure.
        """
        cls.ensure_schema()
        schema_kinds = {
            r["column_name"]: cls.classify_data_type(r["column_name"], r["data_type"])
            for r in cls._animal_data_column_types()
        }
        if not schema_kinds:
            raise ValueError("Could not read the animal_data schema; nothing was saved")
        unknown = [
            c["column_name"]
            for c in columns
            if not SAFE_COLUMN_NAME.match(c["column_name"]) or c["column_name"] not in schema_kinds
        ]
        if unknown:
            raise ValueError(f"Unknown animal_data column(s): {', '.join(sorted(unknown))}")

        def kind_of(col: dict[str, Any]) -> str:
            return schema_kinds[col["column_name"]]

        shown_numeric = {
            c["column_name"].lower()
            for c in columns
            if bool(c.get("is_shown")) and kind_of(c) == "numeric"
        }
        if not (shown_numeric & REQUIRED_WEIGHT_MEASURES):
            raise ValueError(
                f"Configuration must include at least one weight measure ({', '.join(sorted(REQUIRED_WEIGHT_MEASURES))})"
            )

        pending: list[tuple[Any, ...]] = []
        for col in columns:
            col_name = col["column_name"]
            kind = kind_of(col)
            is_reserved = col_name.lower() in RESERVED_FILTER_NAMES
            is_filter = (
                bool(col.get("is_filter")) and kind == "categorical" and not is_reserved
            )
            is_shown = bool(col.get("is_shown")) and kind == "numeric"
            d_cnt = int(col.get("distinct_count") or 0)
            pending.append((farm_id, col_name, kind, is_filter, is_shown, d_cnt))

        with get_conn() as conn:
            with conn.cursor() as cur:
                for params in pending:
                    cur.execute(
                        """
                        INSERT INTO farm_columns (farm_id, column_name, kind, is_filter, is_shown, distinct_count, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, now())
                        ON CONFLICT (farm_id, column_name) DO UPDATE SET
                            kind = EXCLUDED.kind,
                            is_filter = EXCLUDED.is_filter,
                            is_shown = EXCLUDED.is_shown,
                            distinct_count = EXCLUDED.distinct_count,
                            updated_at = now()
                        """,
                        params,
                    )

        invalidate_active_columns(farm_id)
        # The pandas path selects enabled extra filter columns, so its cached frame may lack one.
        from app.services.filter_service import invalidate_farm_dataframe

        invalidate_farm_dataframe(farm_id)

    @classmethod
    def get_active_columns(cls, farm_id: str) -> dict[str, list[str]]:
        """The farm's enabled filter and measure columns ({"filters": [...], "measures": [...]}).

        Falls back to the canonical defaults when the farm has no saved configuration, or when
        farm_columns can't be read, so a missing table never takes the dashboard down.
        """
        now = time.monotonic()
        cached = _ACTIVE_COLUMNS_CACHE.get(farm_id)
        if cached and now - cached[0] < _ACTIVE_COLUMNS_TTL_SEC:
            return cached[1]

        try:
            rows = fetch_all(
                "SELECT column_name, kind, is_filter, is_shown FROM farm_columns WHERE farm_id = %s",
                (farm_id,),
            )
        except Exception as exc:
            logger.warning("Could not read farm_columns for %s: %s", farm_id, exc)
            rows = []

        if rows:
            filters = [
                r["column_name"]
                for r in rows
                if bool(r.get("is_filter"))
                and r.get("kind") == "categorical"
                and r["column_name"].lower() not in RESERVED_FILTER_NAMES
                and SAFE_COLUMN_NAME.match(r["column_name"])
            ]
            measures = [
                r["column_name"]
                for r in rows
                if bool(r.get("is_shown"))
                and r.get("kind") == "numeric"
                and SAFE_COLUMN_NAME.match(r["column_name"])
            ]
            active = {
                "filters": sorted(filters, key=_filter_sort_key),
                "measures": sorted(measures, key=_measure_sort_key),
            }
        else:
            active = {"filters": list(DEFAULT_FILTER_COLUMNS), "measures": list(DEFAULT_MEASURE_COLUMNS)}

        _ACTIVE_COLUMNS_CACHE[farm_id] = (now, active)
        return active

    @staticmethod
    def default_measure(measures: list[str]) -> str:
        """finalpweight, else weight, else pweight when enabled; otherwise the first enabled measure."""
        for m in _DEFAULT_MEASURE_PREFERENCE:
            if m in measures:
                return m
        return measures[0] if measures else "finalpweight"

    @classmethod
    def restrict_filter_values(cls, data: dict[str, Any]) -> dict[str, Any]:
        """Limit raw FilterState input to what the admin enabled for its farm.

        Disabled measures are dropped (falling back to the farm's default measure, as unknown
        measures always have), disabled built-in filters reset to Overall, and extra filters keep
        only enabled columns.
        """
        active = cls.get_active_columns(str(data.get("farm_id")))
        allowed_measures = active["measures"]
        enabled_filters = set(active["filters"])

        measures = [m for m in data.get("measures") or [] if m in allowed_measures]
        data["measures"] = measures or [cls.default_measure(allowed_measures)]
        data["measure"] = data["measures"][0]

        for col in BUILTIN_FILTER_COLUMNS:
            if col not in enabled_filters:
                data[col] = ["Overall"]

        extra = data.get("extra_filters") or {}
        data["extra_filters"] = {
            col: vals
            for col, vals in extra.items()
            if col in enabled_filters and col not in BUILTIN_FILTER_COLUMNS
        }
        return data

    @classmethod
    def on_farm_upload(
        cls, farm_id: str, column_stats: dict[str, tuple[int, bool]] | None = None
    ) -> dict[str, Any]:
        """Triggered on farm upload: seeds defaults if new; registers newly discovered columns.

        column_stats comes from the uploaded frame. Passing it skips the table scan that
        otherwise recounts every shared column before the response can return.
        """
        cls.ensure_schema()
        inspected = cls.inspect_and_suggest_columns(farm_id, column_stats=column_stats)
        saved = fetch_all(
            "SELECT column_name FROM farm_columns WHERE farm_id = %s", (farm_id,)
        )
        saved_cols = {r["column_name"] for r in saved}
        is_initial = len(saved_cols) == 0

        unsaved = [c["column_name"] for c in inspected if c["column_name"] not in saved_cols]
        if is_initial or unsaved:
            cls.save_farm_columns(farm_id, inspected)

        # animal_data's columns are shared by all farms, so only report the ones this farm has data in
        # (not, say, a column another farm's upload added).
        new_cols = [
            c["column_name"] for c in inspected if c["column_name"] in unsaved and c.get("has_data")
        ]
        return {
            "new_columns_detected": new_cols,
            "initial_ingest": is_initial,
        }

    @classmethod
    def seed_farm_columns_if_missing(cls, farm_id: str) -> bool:
        """Seed default suggestions if no farm_columns exist for this farm."""
        cls.ensure_schema()
        saved = fetch_all(
            "SELECT column_name FROM farm_columns WHERE farm_id = %s LIMIT 1", (farm_id,)
        )
        if not saved:
            inspected = cls.inspect_and_suggest_columns(farm_id)
            if inspected:
                cls.save_farm_columns(farm_id, inspected)
                return True
        return False

    @classmethod
    def get_farm_columns_response(cls, farm_id: str) -> dict[str, Any]:
        """Format column configurations for FarmColumnsResponse."""
        cls.ensure_schema()
        # Seed if completely missing
        cls.seed_farm_columns_if_missing(farm_id)

        rows = fetch_all(
            """
            SELECT farm_id, column_name, kind, is_filter, is_shown, distinct_count
            FROM farm_columns
            WHERE farm_id = %s
            ORDER BY column_name
            """,
            (farm_id,),
        )
        if not rows:
            # Fall back to live inspection if DB table had no rows
            inspected = cls.inspect_and_suggest_columns(farm_id)
            if inspected:
                cls.save_farm_columns(farm_id, inspected)
                rows = fetch_all(
                    """
                    SELECT farm_id, column_name, kind, is_filter, is_shown, distinct_count
                    FROM farm_columns
                    WHERE farm_id = %s
                    ORDER BY column_name
                    """,
                    (farm_id,),
                )

        columns = []
        active_filters = []
        active_measures = []

        for r in rows:
            col_name = r["column_name"]
            kind = r["kind"]
            is_reserved = col_name.lower() in RESERVED_FILTER_NAMES
            is_filter = bool(r["is_filter"]) and kind == "categorical" and not is_reserved
            is_shown = bool(r["is_shown"]) and kind == "numeric"
            d_cnt = int(r.get("distinct_count") or 0)

            if is_filter:
                active_filters.append(col_name)
            if is_shown:
                active_measures.append(col_name)

            columns.append(
                {
                    "column_name": col_name,
                    "kind": kind,
                    "is_filter": is_filter,
                    "is_shown": is_shown,
                    "distinct_count": d_cnt,
                    "is_reserved": is_reserved,
                }
            )

        return {
            "farm_id": farm_id,
            "filters_count": len(active_filters),
            "measures_count": len(active_measures),
            "active_filters": active_filters,
            "active_measures": active_measures,
            "columns": columns,
        }
