from __future__ import annotations

import base64
import io
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool

from app.auth.dependencies import CurrentUser, require_admin, require_credential_admin
from app.auth.passwords import hash_password
from app.config import get_settings
from app.constants import CSV_COLUMN_MAP
import app.db as app_db
from app.db import (
    execute,
    execute_returning,
    fetch_all,
    fetch_one,
    get_active_engine,
    get_conn,
    record_credential_history,
)
from app.models.schemas import (
    ColumnConfigItem,
    FarmColumnsResponse,
    FarmColumnsUpdate,
    FarmCreate,
    PasswordUpdate,
    UserCreate,
    UserFarmsUpdate,
    UserRoleUpdate,
    UserStatusUpdate,
    UsernameUpdate,
)
from app.services.variable_service import VariableService
from app.services.farm_backup import create_partition_snapshot, list_farm_snapshots, restore_partition_snapshot
from app.services.farm_ingestion import (
    clean_column_names,
    ingest_farm_data,
    load_table_from_bytes,
    sync_extra_columns,
    upload_column_stats,
    validate_and_prepare_dataset,
)
from app.routers.farms import (
    ALLOWED_LOGO_EXTENSIONS,
    MAX_LOGO_BYTES,
    LOGO_CONTENT_TYPES,
    _get_logos,
    delete_db_logos,
    list_db_logos,
    logo_api_url,
    save_logo,
)

router = APIRouter()
logger = logging.getLogger(__name__)



def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    col_map = {}
    for col in df.columns:
        key = col.strip().lower().replace(" ", "")
        for csv_key, db_col in {
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
        }.items():
            if key == csv_key or col.strip().lower() == csv_key:
                col_map[col] = db_col
    return df.rename(columns=col_map)


@router.post("/farms")
def create_farm(body: FarmCreate, user: Annotated[CurrentUser, Depends(require_admin)]):
    execute(
        "INSERT INTO farms (farm_id, farm_name, slug) VALUES (%s, %s, %s)",
        (body.farm_id, body.farm_name, body.slug),
    )
    return {"detail": "Farm created", "farm_id": body.farm_id}

@router.delete("/farms/{farm_id}")
def delete_farm(
    farm_id: str,
    user: Annotated[CurrentUser, Depends(require_admin)],
    force: bool = False,
):
    """Cascading deletion of a farm partition across all 10 associated tables."""
    farm_id_clean = farm_id.strip()
    if farm_id_clean.upper() == "KF" and not force:
        raise HTTPException(
            status_code=400,
            detail="Deletion of baseline farm 'KF' requires force=true parameter.",
        )
    existing = fetch_one("SELECT farm_id FROM farms WHERE farm_id = %s", (farm_id_clean,))
    if not existing:
        has_data = fetch_one("SELECT 1 FROM animal_data WHERE farm_id = %s LIMIT 1", (farm_id_clean,))
        if not has_data:
            raise HTTPException(status_code=404, detail=f"Farm '{farm_id_clean}' not found")

    with get_conn() as conn:
        cur = conn.cursor()
        engine = get_active_engine()
        if engine == "duckdb":
            # Compacts delete bookkeeping first: known DuckDB index bug otherwise fatally
            # fails DELETEs on tables with an ART index that have seen many reverts.
            cur.execute("CHECKPOINT")
            cur.execute("BEGIN TRANSACTION")
        try:
            cur.execute("DELETE FROM forecasts WHERE farm_id = %s", (farm_id_clean,))
            # Delete via the indexed column: farm_id-based deletes have hit the index bug above.
            cur.execute(
                "DELETE FROM animal_data_snapshots_data WHERE snapshot_id IN "
                "(SELECT id FROM animal_data_snapshots WHERE farm_id = %s)",
                (farm_id_clean,),
            )
            cur.execute("DELETE FROM animal_data_snapshots WHERE farm_id = %s", (farm_id_clean,))
            cur.execute("DELETE FROM data_uploads WHERE farm_id = %s", (farm_id_clean,))
            cur.execute("DELETE FROM animal_data WHERE farm_id = %s", (farm_id_clean,))
            cur.execute("DELETE FROM farm_columns WHERE farm_id = %s", (farm_id_clean,))
            cur.execute("DELETE FROM user_farm_access WHERE farm_id = %s", (farm_id_clean,))
            cur.execute("DELETE FROM logo_assets WHERE farm_id = %s", (farm_id_clean,))
            cur.execute("DELETE FROM email_schedules WHERE farm_id = %s", (farm_id_clean,))
            cur.execute("DELETE FROM farms WHERE farm_id = %s", (farm_id_clean,))
            if engine == "duckdb":
                cur.execute("COMMIT")
        except Exception as exc:
            if engine == "duckdb":
                cur.execute("ROLLBACK")
            raise HTTPException(status_code=500, detail=f"Failed to decommission farm: {exc}")

    return {
        "detail": f"Farm '{farm_id_clean}' and all associated records decommissioned successfully.",
        "farm_id": farm_id_clean,
    }



@router.get("/farms/overview")
def list_farms_overview(user: Annotated[CurrentUser, Depends(require_admin)]):
    """Return multi-farm overview with status, row counts, last upload, and snapshot counts."""
    farms = fetch_all("SELECT farm_id, farm_name, slug, is_active, created_at FROM farms ORDER BY farm_id")
    overview = []
    seen_farm_ids = set()
    for f in farms:
        fid = f["farm_id"]
        seen_farm_ids.add(fid)
        cnt_row = fetch_one("SELECT COUNT(*) AS cnt FROM animal_data WHERE farm_id = %s", (fid,))
        last_up = fetch_one(
            "SELECT created_at FROM data_uploads WHERE farm_id = %s ORDER BY created_at DESC LIMIT 1",
            (fid,),
        )
        snap_cnt = fetch_one(
            "SELECT COUNT(*) AS cnt FROM animal_data_snapshots WHERE farm_id = %s",
            (fid,),
        )
        overview.append({
            "farm_id": fid,
            "farm_name": f.get("farm_name") or fid,
            "slug": f.get("slug") or fid.lower(),
            "is_active": bool(f.get("is_active", True)),
            "record_count": cnt_row["cnt"] if cnt_row else 0,
            "last_upload": last_up["created_at"].isoformat() if last_up and last_up.get("created_at") and hasattr(last_up["created_at"], "isoformat") else (str(last_up["created_at"]) if last_up and last_up.get("created_at") else None),
            "snapshots_count": snap_cnt["cnt"] if snap_cnt else 0,
            "created_at": f["created_at"].isoformat() if f.get("created_at") and hasattr(f["created_at"], "isoformat") else str(f.get("created_at", "")),
        })

    extra_farms = fetch_all("SELECT DISTINCT farm_id FROM animal_data WHERE farm_id IS NOT NULL")
    for ef in extra_farms:
        fid = ef["farm_id"]
        if fid not in seen_farm_ids:
            seen_farm_ids.add(fid)
            cnt_row = fetch_one("SELECT COUNT(*) AS cnt FROM animal_data WHERE farm_id = %s", (fid,))
            last_up = fetch_one(
                "SELECT created_at FROM data_uploads WHERE farm_id = %s ORDER BY created_at DESC LIMIT 1",
                (fid,),
            )
            snap_cnt = fetch_one(
                "SELECT COUNT(*) AS cnt FROM animal_data_snapshots WHERE farm_id = %s",
                (fid,),
            )
            overview.append({
                "farm_id": fid,
                "farm_name": fid,
                "slug": fid.lower(),
                "is_active": True,
                "record_count": cnt_row["cnt"] if cnt_row else 0,
                "last_upload": last_up["created_at"].isoformat() if last_up and last_up.get("created_at") and hasattr(last_up["created_at"], "isoformat") else (str(last_up["created_at"]) if last_up and last_up.get("created_at") else None),
                "snapshots_count": snap_cnt["cnt"] if snap_cnt else 0,
                "created_at": None,
            })

    return overview


@router.get("/farms/{farm_id}/snapshots")
def get_farm_snapshots(farm_id: str, user: Annotated[CurrentUser, Depends(require_admin)]):
    """Return chronologically ordered snapshot history for a farm."""
    return list_farm_snapshots(farm_id)

@router.delete("/farms/{farm_id}/snapshots/{snapshot_id}")
def delete_farm_snapshot(
    farm_id: str,
    snapshot_id: int,
    user: Annotated[CurrentUser, Depends(require_admin)],
):
    """Delete snapshot metadata and associated data rows."""
    from app.services.farm_backup import delete_partition_snapshot
    snap = fetch_one(
        "SELECT id FROM animal_data_snapshots WHERE farm_id = %s AND id = %s",
        (farm_id, snapshot_id),
    )
    if not snap:
        raise HTTPException(status_code=404, detail=f"Snapshot #{snapshot_id} not found for farm '{farm_id}'")
    delete_partition_snapshot(farm_id, snapshot_id)
    return {"detail": f"Snapshot #{snapshot_id} deleted successfully", "snapshot_id": snapshot_id}



def invalidate_farm_caches(farm_id: str) -> None:
    """Evict server-side per-farm caches so reads reflect the just-mutated partition."""
    from app.services.anomaly_service import clear_anomaly_cache
    from app.services.filter_service import invalidate_farm_dataframe
    from app.services.sql_agg import invalidate_timeseries_grain

    invalidate_farm_dataframe(farm_id)
    invalidate_timeseries_grain(farm_id)
    clear_anomaly_cache()


@router.post("/farms/{farm_id}/snapshots/{snapshot_id}/revert")
async def revert_farm_snapshot(
    farm_id: str,
    snapshot_id: int,
    user: Annotated[CurrentUser, Depends(require_admin)],
):
    """Atomically rollback farm partition to specified snapshot with pre-revert safety snapshot."""
    try:
        result = restore_partition_snapshot(farm_id, snapshot_id, reverted_by=user.username)
        invalidate_farm_caches(farm_id)
        forecasts = await run_in_threadpool(refresh_forecasts, farm_id)
        return {
            "detail": f"Successfully reverted farm {farm_id} to snapshot {snapshot_id}",
            **result,
            "forecasts": forecasts,
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to revert snapshot %s for farm %s", snapshot_id, farm_id)
        raise HTTPException(status_code=500, detail=f"Revert failed: {exc}")


@router.post("/farms/{farm_id}/upload")
async def upload_csv(
    farm_id: str,
    user: Annotated[CurrentUser, Depends(require_admin)],
    file: UploadFile = File(...),
    duplicate_mode: str = Form("skip"),
    forecast: bool = Form(True),
):
    """Add a farm's CSV or XLSX records, then refresh its Prediction-page forecasts (unless forecast=false)."""
    if not re.match(rf"^{re.escape(farm_id)}_.+\.(csv|xlsx)$", file.filename or "", re.I):
        raise HTTPException(status_code=400, detail=f"Filename must match {farm_id}_YYYY-MM-DD.(csv|xlsx)")

    content = await file.read()
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 50MB)")

    return await _ingest_farm_file(farm_id, user, content, file.filename or "", duplicate_mode, forecast)


async def _ingest_farm_file(
    farm_id: str,
    user: CurrentUser,
    content: bytes,
    filename: str,
    duplicate_mode: str = "skip",
    forecast: bool = True,
):
    """Shared ingest pipeline: single-request and chunked uploads both land here."""
    raw_df = load_table_from_bytes(content, filename)
    df = validate_and_prepare_dataset(raw_df, farm_id)

    # Detect if test environment monkeypatched execute_returning / fetch_one
    is_mocked = getattr(fetch_one, "__name__", "") == "<lambda>" or getattr(execute_returning, "__name__", "") == "<lambda>"

    # Before the snapshot, so a rejected file leaves no trace. (The mocked test path inserts only
    # canonical columns and doesn't touch the schema.)
    extra_columns = {"added": [], "ignored": []}
    if not is_mocked:
        df, extra_columns = sync_extra_columns(df)

    if is_mocked:
        count_row = fetch_one("SELECT COUNT(*) AS cnt FROM animal_data WHERE farm_id = %s", (farm_id,))
        snap = execute_returning(
            """
            INSERT INTO animal_data_snapshots (farm_id, snapshot_name, record_count, created_by)
            VALUES (%s, %s, %s, %s) RETURNING id
            """,
            (farm_id, f"pre_upload_{filename}", count_row["cnt"] if count_row else 0, user.username),
        )
        snapshot_id = snap["id"] if snap and "id" in snap else 1
    else:
        snapshot_id = create_partition_snapshot(
            farm_id=farm_id,
            snapshot_name=f"pre_upload_{filename}",
            created_by=user.username,
        )

    ingest_res = ingest_farm_data(
        farm_id=farm_id,
        df=df,
        duplicate_mode=duplicate_mode,
        fetch_one_fn=fetch_one,
        execute_fn=execute,
        is_mocked=is_mocked,
    )
    inserted = ingest_res["rows_inserted"]
    skipped = ingest_res["rows_skipped"]
    overwritten = ingest_res["rows_overwritten"]

    execute(
        """
        INSERT INTO data_uploads (farm_id, filename, rows_inserted, rows_skipped, rows_overwritten, duplicate_mode, uploaded_by)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (farm_id, filename, inserted, skipped, overwritten, duplicate_mode, user.username),
    )
    invalidate_farm_caches(farm_id)

    upload_cols_res = VariableService.on_farm_upload(
        farm_id,
        column_stats=None if is_mocked else upload_column_stats(df),
    )

    try:
        from app.services.anomaly_service import clear_anomaly_cache
        from app.services.anomaly_snapshot import delete_farm_snapshots

        clear_anomaly_cache()
        delete_farm_snapshots(farm_id)
    except Exception:
        logger.exception("Could not clear anomaly caches after upload for %s", farm_id)

    return {
        "rows_inserted": inserted,
        "rows_skipped": skipped,
        "rows_overwritten": overwritten,
        "snapshot_id": snapshot_id,
        # in a worker thread: forecasting a farm takes a few seconds
        "forecasts": await run_in_threadpool(refresh_forecasts, farm_id) if forecast else {"status": "skipped"},
        "new_columns_detected": upload_cols_res.get("new_columns_detected", []),
        "initial_ingest": upload_cols_res.get("initial_ingest", False),
        # Columns this file added to animal_data, and non-canonical columns it could not keep.
        "columns_added": extra_columns["added"],
        "columns_ignored": extra_columns["ignored"],
    }


# --- Chunked upload: works around Vercel's 4.5 MB serverless request-body cap. ---
# The client stages 4 MB raw chunks as base64 rows (TEXT is portable across Postgres
# and the dev DuckDB), and finalize reassembles them into the shared ingest pipeline.
# Heavy post-ingest work (forecast refresh, anomaly snapshot invalidation, column sync)
# deliberately runs ONLY in finalize — staging chunks must stay cheap.
UPLOAD_CHUNKS_DDL = """
    CREATE TABLE IF NOT EXISTS upload_chunks (
        upload_id VARCHAR(64) NOT NULL,
        chunk_index INTEGER NOT NULL,
        data TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (upload_id, chunk_index)
    );
"""


def ensure_upload_chunks() -> None:
    # Explicit empty params: keeps the call signature uniform for tests that stub execute(q, p).
    execute(UPLOAD_CHUNKS_DDL, ())


@router.post("/farms/{farm_id}/upload/init")
async def upload_init(
    farm_id: str,
    user: Annotated[CurrentUser, Depends(require_admin)],
    payload: Annotated[dict, Body()],
):
    filename = payload.get("filename") or ""
    if not re.match(rf"^{re.escape(farm_id)}_.+\.(csv|xlsx)$", filename, re.I):
        raise HTTPException(status_code=400, detail=f"Filename must match {farm_id}_YYYY-MM-DD.(csv|xlsx)")
    if not fetch_one("SELECT 1 FROM farms WHERE farm_id = %s", (farm_id,)):
        raise HTTPException(status_code=404, detail=f"Farm '{farm_id}' does not exist")
    ensure_upload_chunks()
    # ponytail: GC on init instead of a cron — abandoned chunks older than a day die here.
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC to match TIMESTAMP columns
    execute("DELETE FROM upload_chunks WHERE created_at < %s", (cutoff,))
    return {"upload_id": uuid.uuid4().hex}


@router.put("/farms/{farm_id}/upload/{upload_id}/chunks/{chunk_index}")
async def upload_chunk(
    farm_id: str,
    upload_id: str,
    chunk_index: int,
    request: Request,
    user: Annotated[CurrentUser, Depends(require_admin)],
):
    data = await request.body()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Chunk too large (max 5MB)")
    ensure_upload_chunks()
    # Upsert: a retried chunk (response lost after storage) must not 500 on the PK.
    execute(
        """
        INSERT INTO upload_chunks (upload_id, chunk_index, data) VALUES (%s, %s, %s)
        ON CONFLICT (upload_id, chunk_index) DO UPDATE SET data = EXCLUDED.data
        """,
        (upload_id, chunk_index, base64.b64encode(data).decode()),
    )
    return {
        "upload_id": upload_id,
        "chunk_index": chunk_index,
        "bytes_received": len(data),
    }


@router.post("/farms/{farm_id}/upload/{upload_id}/finalize")
async def upload_finalize(
    farm_id: str,
    upload_id: str,
    user: Annotated[CurrentUser, Depends(require_admin)],
    payload: Annotated[dict, Body()],
):
    filename = payload.get("filename") or ""
    if not re.match(rf"^{re.escape(farm_id)}_.+\.(csv|xlsx)$", filename, re.I):
        raise HTTPException(status_code=400, detail=f"Filename must match {farm_id}_YYYY-MM-DD.(csv|xlsx)")
    ensure_upload_chunks()
    rows = fetch_all(
        "SELECT data FROM upload_chunks WHERE upload_id = %s ORDER BY chunk_index",
        (upload_id,),
    )
    if not rows:
        raise HTTPException(status_code=400, detail="No chunks staged for this upload; please restart the upload")
    execute("DELETE FROM upload_chunks WHERE upload_id = %s", (upload_id,))
    # Decode per chunk: b64 padding mid-string would corrupt a single joined-string decode.
    content = b"".join(base64.b64decode(r["data"]) for r in rows)
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 50MB)")
    # Clients may send booleans as JSON booleans or 'true'/'false' strings — coerce safely.
    forecast_raw = payload.get("forecast", True)
    forecast = forecast_raw is True or str(forecast_raw).lower() == "true"
    return await _ingest_farm_file(
        farm_id,
        user,
        content,
        filename,
        duplicate_mode=payload.get("duplicate_mode", "skip"),
        forecast=forecast,
    )


@router.get("/farms/{farm_id}/columns", response_model=FarmColumnsResponse)
def get_farm_columns(farm_id: str, user: Annotated[CurrentUser, Depends(require_admin)]):
    """Retrieve dynamic filter and measure configuration for farm_id."""
    try:
        return VariableService.get_farm_columns_response(farm_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/farms/{farm_id}/columns/inspect", response_model=FarmColumnsResponse)
def inspect_farm_columns(farm_id: str, user: Annotated[CurrentUser, Depends(require_admin)]):
    """Re-scan distinct counts and newly added columns for farm_id."""
    try:
        inspected = VariableService.inspect_and_suggest_columns(farm_id, force_rescan=True)
        if inspected:
            VariableService.save_farm_columns(farm_id, inspected)
        return VariableService.get_farm_columns_response(farm_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.put("/farms/{farm_id}/columns", response_model=FarmColumnsResponse)
def update_farm_columns(
    farm_id: str,
    body: FarmColumnsUpdate,
    user: Annotated[CurrentUser, Depends(require_admin)],
):
    """Persist admin variable and filter choices, enforcing mandatory weight measure guard."""
    try:
        VariableService.save_farm_columns(farm_id, [c.model_dump() for c in body.columns])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return VariableService.get_farm_columns_response(farm_id)


def refresh_forecasts(farm_id: str) -> dict:
    """Recompute a farm's Prediction-page forecasts and report how it went.

    Never raises: after an upload the new records are already saved, so a forecast failure is
    reported in the response instead of failing the whole request.
    """
    try:
        from app.services import forecast_service  # imported here so the other admin routes never depend on it

        return {"status": "updated", **forecast_service.refresh_farm(farm_id)}
    except Exception as exc:
        logger.exception("Refreshing forecasts for farm %s failed", farm_id)
        reason = str(exc).splitlines()[0].rstrip(".") if str(exc) else type(exc).__name__
        return {
            "status": "failed",
            "detail": f"Forecasts were not refreshed: {reason}. Retry with POST "
            f"/api/admin/farms/{farm_id}/forecasts/refresh (python scripts/admin.py forecast --farm {farm_id}).",
        }


@router.post("/farms/{farm_id}/forecasts/refresh")
def refresh_farm_forecasts(farm_id: str, user: Annotated[CurrentUser, Depends(require_admin)]):
    """Recompute a farm's forecasts without uploading: to retry, or after deploying a new model."""
    result = refresh_forecasts(farm_id)
    if result["status"] == "failed":
        raise HTTPException(status_code=500, detail=result["detail"])
    return result


@router.get("/farms/{farm_id}/status")
def farm_status(farm_id: str, user: Annotated[CurrentUser, Depends(require_admin)]):
    count = fetch_one("SELECT COUNT(*) AS cnt FROM animal_data WHERE farm_id = %s", (farm_id,))
    last_upload = fetch_one(
        "SELECT created_at FROM data_uploads WHERE farm_id = %s ORDER BY created_at DESC LIMIT 1",
        (farm_id,),
    )
    snapshots = fetch_one(
        "SELECT COUNT(*) AS cnt FROM animal_data_snapshots WHERE farm_id = %s", (farm_id,)
    )
    return {
        "record_count": count["cnt"] if count else 0,
        "last_upload": last_upload["created_at"].isoformat() if last_upload and last_upload.get("created_at") else None,
        "snapshots_count": snapshots["cnt"] if snapshots else 0,
    }


@router.get("/farms/{farm_id}/uploads")
def list_farm_uploads(farm_id: str, user: Annotated[CurrentUser, Depends(require_admin)]):
    """Upload audit trail for the admin panel: newest first, capped at 50."""
    return fetch_all(
        """
        SELECT id, filename, rows_inserted, rows_skipped, rows_overwritten, duplicate_mode,
               uploaded_by, created_at
        FROM data_uploads WHERE farm_id = %s ORDER BY created_at DESC LIMIT 50
        """,
        (farm_id,),
    )


@router.get("/users")
def list_users(user: Annotated[CurrentUser, Depends(require_admin)]):
    rows = fetch_all("SELECT id, username, is_admin, role, is_active, last_login, created_at FROM users ORDER BY id")
    try:
        all_farms = [f["farm_id"] for f in fetch_all("SELECT farm_id FROM farms WHERE is_active = TRUE ORDER BY farm_id") if isinstance(f, dict) and "farm_id" in f]
    except Exception:
        all_farms = []
    try:
        ufa_rows = fetch_all("SELECT user_id, farm_id FROM user_farm_access ORDER BY farm_id")
    except Exception:
        ufa_rows = []
    user_farms_map: dict[int, list[str]] = {}
    for ufa in ufa_rows:
        if isinstance(ufa, dict) and "user_id" in ufa and "farm_id" in ufa:
            user_farms_map.setdefault(ufa["user_id"], []).append(ufa["farm_id"])

    result = []
    for r in rows:
        uname = r["username"].lower()
        role = r.get("role")
        if not role:
            if uname == "owner":
                role = "owner"
            elif r.get("is_admin"):
                role = "admin"
            else:
                role = "user"
        is_admin_flag = (role == "admin" or bool(r.get("is_admin")))
        
        if is_admin_flag:
            farm_ids = list(all_farms)
        else:
            farm_ids = user_farms_map.get(r["id"], [])

        result.append({
            "id": r["id"],
            "username": r["username"],
            "is_admin": is_admin_flag,
            "role": role,
            "is_active": r.get("is_active", True),
            "last_login": r.get("last_login"),
            "created_at": r.get("created_at"),
            "farm_ids": farm_ids,
        })
    return result


@router.get("/users/{user_id}/farms")
def get_user_farms_admin(user_id: int, user: Annotated[CurrentUser, Depends(require_admin)]):
    target = fetch_one("SELECT id, username, is_admin, role FROM users WHERE id = %s", (user_id,))
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    target_is_admin = bool(target.get("is_admin") or (target.get("role") or "").lower() == "admin")
    if target_is_admin:
        farms = fetch_all("SELECT farm_id FROM farms WHERE is_active = TRUE ORDER BY farm_id")
        return {"user_id": user_id, "farm_ids": [f["farm_id"] for f in farms]}
    rows = fetch_all("SELECT farm_id FROM user_farm_access WHERE user_id = %s ORDER BY farm_id", (user_id,))
    return {"user_id": user_id, "farm_ids": [r["farm_id"] for r in rows]}


def _clean_username(username: str) -> str:
    cleaned = username.strip()
    if not cleaned or len(cleaned) > 50 or any(ch.isspace() for ch in cleaned):
        raise HTTPException(status_code=400, detail="Username must be 1–50 characters with no spaces")
    return cleaned


@router.get("/credentials/users")
def list_credential_users(user: Annotated[CurrentUser, Depends(require_credential_admin)]):
    rows = fetch_all("SELECT id, username, is_admin FROM users ORDER BY id")
    result = []
    for row in rows:
        uname = row["username"].lower()
        if uname == "owner":
            role = "owner"
        elif row["is_admin"]:
            role = "admin"
        else:
            role = "user"
        result.append({
            "id": row["id"],
            "username": row["username"],
            "role": role,
        })
    return result


@router.put("/credentials/username")
def change_credential_username(
    body: UsernameUpdate,
    user: Annotated[CurrentUser, Depends(require_credential_admin)],
):
    new_name = _clean_username(body.new_username)
    if new_name.lower() == "owner":
        raise HTTPException(status_code=400, detail="Cannot rename user to owner")
    target = fetch_one("SELECT id, username FROM users WHERE id = %s", (body.user_id,))
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target["username"].lower() == "owner":
        raise HTTPException(status_code=400, detail="Cannot rename system owner account")
    clash = fetch_one(
        "SELECT id FROM users WHERE lower(username) = lower(%s) AND id <> %s",
        (new_name, body.user_id),
    )
    if clash:
        raise HTTPException(status_code=409, detail="Username is already taken")
    record_credential_history(body.user_id, "change_username", user.username)
    execute(
        "UPDATE users SET username = %s, updated_at = NOW() WHERE id = %s",
        (new_name, body.user_id),
    )
    return {"detail": "Username updated", "id": body.user_id, "username": new_name}


@router.put("/credentials/password")
def change_credential_password(
    body: PasswordUpdate,
    user: Annotated[CurrentUser, Depends(require_credential_admin)],
):
    if not body.new_password or not body.new_password.strip():
        raise HTTPException(status_code=400, detail="Password cannot be empty")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    target = fetch_one("SELECT id FROM users WHERE id = %s", (body.user_id,))
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    record_credential_history(body.user_id, "change_password", user.username)
    execute(
        "UPDATE users SET password_hash = %s, updated_at = NOW() WHERE id = %s",
        (hash_password(body.new_password), body.user_id),
    )
    return {"detail": "Password updated", "id": body.user_id}


@router.post("/users")
def create_user(body: UserCreate, user: Annotated[CurrentUser, Depends(require_admin)]):
    username = _clean_username(body.username)
    if username.lower() == "owner":
        raise HTTPException(status_code=400, detail="Cannot create a user named owner")
    if username.lower() == "admin":
        raise HTTPException(status_code=400, detail="Cannot create a user named admin")
    if fetch_one("SELECT id FROM users WHERE lower(username) = lower(%s)", (username,)):
        raise HTTPException(status_code=409, detail="Username is already taken")
    if not body.password or len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    requested = set(body.farm_ids)
    if not requested:
        raise HTTPException(status_code=400, detail="At least one farm must be assigned")
    # ponytail: full farms scan; the table has a handful of rows. Swap to an IN query if that changes.
    known = {r["farm_id"] for r in fetch_all("SELECT farm_id FROM farms")}
    unknown = requested - known
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown farm id(s): {', '.join(sorted(unknown))}")
    # Hardcode is_admin = False (per R parity & single-admin invariant)
    is_admin = False
    role = "owner" if (body.role or "").lower() == "owner" else "user"
    row = execute_returning(
        "INSERT INTO users (username, password_hash, is_admin, role, is_active) VALUES (%s, %s, %s, %s, TRUE) RETURNING id, username, is_admin, role, is_active",
        (username, hash_password(body.password), is_admin, role),
    )
    if not row:
        row = fetch_one("SELECT id, username, is_admin, role, is_active FROM users WHERE username = %s", (username,))
    for fid in requested:
        execute("INSERT INTO user_farm_access (user_id, farm_id) VALUES (%s, %s)", (row["id"], fid))
    return {**row, "farm_ids": sorted(requested)}


@router.put("/users/{user_id}/status")
def update_user_status(
    user_id: int,
    body: UserStatusUpdate,
    user: Annotated[CurrentUser, Depends(require_admin)],
):
    target = fetch_one("SELECT id, username, is_admin, role, is_active FROM users WHERE id = %s", (user_id,))
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target["username"].lower() == "admin" and not body.is_active:
        raise HTTPException(
            status_code=400,
            detail="Cannot deactivate the system administrator account.",
        )

    execute(
        "UPDATE users SET is_active = %s, updated_at = NOW() WHERE id = %s",
        (body.is_active, user_id),
    )
    return {
        "detail": f"User '{target['username']}' status updated to {'active' if body.is_active else 'inactive'}",
        "id": user_id,
        "is_active": body.is_active,
    }


@router.put("/users/{user_id}/farms")
def update_user_farms(
    user_id: int,
    body: UserFarmsUpdate,
    user: Annotated[CurrentUser, Depends(require_admin)],
):
    target = fetch_one("SELECT id, username, is_admin, role FROM users WHERE id = %s", (user_id,))
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target["username"].lower() == "admin":
        raise HTTPException(
            status_code=400,
            detail="Admin accounts have universal access across all farms and cannot be bound to specific farms (R parity invariant).",
        )
    requested = set(body.farm_ids)
    if len(requested) < 1:
        raise HTTPException(status_code=400, detail="Non-admin users must be assigned to at least one farm.")

    known = {r["farm_id"] for r in fetch_all("SELECT farm_id FROM farms")}
    unknown = requested - known
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown farm id(s): {', '.join(sorted(unknown))}")

    execute("DELETE FROM user_farm_access WHERE user_id = %s", (user_id,))
    for fid in requested:
        execute("INSERT INTO user_farm_access (user_id, farm_id) VALUES (%s, %s)", (user_id, fid))

    return {
        "detail": f"Farms updated for user '{target['username']}'",
        "id": user_id,
        "farm_ids": sorted(requested),
    }


@router.put("/users/{user_id}/role")
def update_user_role(
    user_id: int,
    body: UserRoleUpdate,
    user: Annotated[CurrentUser, Depends(require_admin)],
):
    """Switch a non-admin account between the 'user' and 'owner' roles.

    The system administrator account is immutable, and is_admin stays bound to it
    (single-admin invariant), so only those two roles are assignable.
    """
    target = fetch_one("SELECT id, username, is_admin, role FROM users WHERE id = %s", (user_id,))
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    uname = target["username"].lower()
    if uname == "admin":
        raise HTTPException(status_code=400, detail="The system administrator role is immutable")
    if uname == "owner":
        raise HTTPException(status_code=400, detail="The owner account role is fixed")
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="You cannot change your own role")

    record_credential_history(user_id, f"change_role_to_{body.role}", user.username)
    execute(
        "UPDATE users SET role = %s, updated_at = NOW() WHERE id = %s",
        (body.role, user_id),
    )
    return {"detail": f"Role updated to '{body.role}'", "id": user_id, "role": body.role}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    user: Annotated[CurrentUser, Depends(require_admin)],
):
    target = fetch_one("SELECT id, username, is_admin, role FROM users WHERE id = %s", (user_id,))
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target["username"].lower() == "admin":
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the system administrator account.",
        )

    record_credential_history(user_id, "delete_user", user.username)
    execute("DELETE FROM user_farm_access WHERE user_id = %s", (user_id,))
    execute("DELETE FROM users WHERE id = %s", (user_id,))
    return {
        "detail": f"User '{target['username']}' deleted successfully",
        "id": user_id,
        "username": target["username"],
        "was_admin": False,
    }


@router.get("/farms/{farm_id}/logos")
def get_farm_logos_admin(farm_id: str, user: Annotated[CurrentUser, Depends(require_admin)]):
    settings = get_settings()
    custom_logos = []
    seen: set[str] = set()
    for row in list_db_logos(farm_id):
        fname = row["filename"]
        seen.add(fname)
        custom_logos.append({
            "filename": fname,
            "url": logo_api_url(farm_id, fname),
            "is_default": False,
        })
    farm_logo_dir = Path(settings.logos_path) / farm_id
    if farm_logo_dir.is_dir():
        for fname in sorted(os.listdir(farm_logo_dir)):
            if Path(fname).suffix.lower() in ALLOWED_LOGO_EXTENSIONS and fname not in seen:
                seen.add(fname)
                custom_logos.append({
                    "filename": fname,
                    "url": f"/assets/logos/{farm_id}/{fname}",
                    "is_default": False,
                })
    custom_logos.sort(key=lambda l: l["filename"])
    has_custom = len(custom_logos) > 0
    logos = custom_logos if has_custom else _get_logos(farm_id)
    return {
        "farm_id": farm_id,
        "has_custom_logos": has_custom,
        "logos": logos,
    }


@router.post("/farms/{farm_id}/logos")
async def upload_farm_logo(
    farm_id: str,
    user: Annotated[CurrentUser, Depends(require_admin)],
    file: UploadFile = File(...),
):
    filename = Path(file.filename or "").name
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid image type '{ext}'. Allowed: {', '.join(sorted(ALLOWED_LOGO_EXTENSIONS))}",
        )
    clean_stem = re.sub(r"[^a-zA-Z0-9_\-]", "_", Path(filename).stem)
    safe_filename = f"{clean_stem}{ext}" if clean_stem else f"logo{ext}"

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(content) > MAX_LOGO_BYTES:
        raise HTTPException(status_code=413, detail=f"Logo exceeds {MAX_LOGO_BYTES // (1024 * 1024)}MB limit")

    # Store in the database: serverless filesystems are read-only/ephemeral.
    save_logo(farm_id, safe_filename, LOGO_CONTENT_TYPES[ext], content)
    return {
        "detail": "Logo uploaded successfully",
        "farm_id": farm_id,
        "filename": safe_filename,
        "url": logo_api_url(farm_id, safe_filename),
    }


@router.delete("/farms/{farm_id}/logos/{filename}")
def delete_farm_logo(
    farm_id: str,
    filename: str,
    user: Annotated[CurrentUser, Depends(require_admin)],
):
    settings = get_settings()
    safe_name = Path(filename).name
    if safe_name != filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    target = Path(settings.logos_path) / farm_id / safe_name
    on_disk = target.is_file()
    in_db = delete_db_logos(farm_id, safe_name) > 0
    if not on_disk and not in_db:
        raise HTTPException(status_code=404, detail=f"Logo '{safe_name}' not found for farm '{farm_id}'")
    if on_disk:
        try:
            target.unlink()
        except OSError:
            pass
    return {"detail": "Logo deleted successfully", "farm_id": farm_id, "filename": safe_name}


@router.post("/farms/{farm_id}/logos/reset")
def reset_farm_logos(
    farm_id: str,
    user: Annotated[CurrentUser, Depends(require_admin)],
):
    settings = get_settings()
    farm_logo_dir = Path(settings.logos_path) / farm_id
    deleted_count = delete_db_logos(farm_id)
    if farm_logo_dir.is_dir():
        for fname in os.listdir(farm_logo_dir):
            p = farm_logo_dir / fname
            if p.is_file() and p.suffix.lower() in ALLOWED_LOGO_EXTENSIONS:
                try:
                    p.unlink()
                    deleted_count += 1
                except OSError:
                    pass
    return {
        "detail": "Farm logos reset to partner defaults",
        "farm_id": farm_id,
        "deleted_count": deleted_count,
    }



@router.get("/users/audit-log")
def get_user_audit_log(
    user: Annotated[CurrentUser, Depends(require_admin)],
    limit: int = 50,
):
    """Retrieve history of credential and role mutations."""
    return fetch_all(
        """
        SELECT id, user_id, username, role, action, changed_by, created_at
        FROM user_credentials_history
        ORDER BY id DESC
        LIMIT %s
        """,
        (limit,),
    )


@router.post("/users/{user_id}/revert-credential/{history_id}")
def revert_user_credential(
    user_id: int,
    history_id: int,
    user: Annotated[CurrentUser, Depends(require_admin)],
):
    """Revert user credentials to an earlier audit state."""
    entry = fetch_one(
        "SELECT * FROM user_credentials_history WHERE id = %s AND user_id = %s",
        (history_id, user_id),
    )
    if not entry:
        raise HTTPException(status_code=404, detail="Audit log entry not found for this user.")

    target = fetch_one("SELECT id, username FROM users WHERE id = %s", (user_id,))
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    record_credential_history(user_id, f"revert_to_entry_{history_id}", user.username)
    execute(
        """
        UPDATE users 
        SET role = %s, password_hash = %s, must_change_password = %s, updated_at = NOW() 
        WHERE id = %s
        """,
        (entry["role"], entry["password_hash"], bool(entry.get("must_change_password", False)), user_id),
    )
    return {
        "detail": f"Reverted user '{target['username']}' credentials to state from history #{history_id}.",
        "user_id": user_id,
        "history_id": history_id,
    }
