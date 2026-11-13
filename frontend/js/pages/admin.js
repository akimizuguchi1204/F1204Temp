// Admin panel: mirrors scripts/admin.py — upload CSVs, refresh forecasts, watch farm status.

// The refresh endpoint returns {status,...} at the top level; upload nests it under .forecasts.
function formatForecastOutcome(f) {
  if (f.status === 'updated') {
    return `Forecasts refreshed: ${f.animals} animals, data through ${f.data_through}, model ${f.model_version} (${f.seconds}s).`;
  }
  if (f.status === 'skipped') return 'Forecasts not refreshed (skip requested). Use Refresh Forecasts above.';
  if (f.status === 'no_data') return f.detail || 'No forecastable data for this farm.';
  return f.detail || 'The server did not report refreshing forecasts. Use Refresh Forecasts above.';
}

// Shared scaffold: fetch a section's data and paint it, unless the farm switched mid-flight.
async function loadSection(id, path, paint) {
  const el = document.getElementById(id);
  if (!el) return;
  const farmId = getFarmId();
  try {
    const data = await apiFetch(path);
    if (getFarmId() !== farmId) return;
    paint(el, data);
  } catch (err) {
    el.innerHTML = `<div class="alert alert-warning">${escapeHtml(err.message)}</div>`;
  }
}

function formatUserFriendlyError(rawMessage, context = 'data') {
  if (!rawMessage) return 'An unexpected error occurred. Please check your file and try again.';
  const msg = String(rawMessage);

  if (context === 'logo') {
    if (msg.includes('413') || msg.toLowerCase().includes('exceeds') || msg.toLowerCase().includes('too large')) {
      return 'Image file is too large (maximum size is 2 MB). Please select a smaller image.';
    }
    if (msg.toLowerCase().includes('invalid image type') || msg.toLowerCase().includes('allowed:')) {
      return 'Unsupported image format. Please upload a PNG, JPG, or SVG image.';
    }
    if (msg.toLowerCase().includes('empty')) {
      return 'The selected image file is empty. Please choose a valid image file.';
    }
  }

  // File size / payload ceiling
  if (msg.includes('413') || msg.toLowerCase().includes('exceeds') || msg.toLowerCase().includes('too large')) {
    return 'The file exceeds the 4.5 MB cloud upload limit. Please upload a smaller batch file (under 4.5 MB) or split your records.';
  }

  // Filename format
  if (msg.toLowerCase().includes('filename must match') || msg.toLowerCase().includes('cross-farm')) {
    return 'The file name does not match the active farm. Please rename the file using the format: FARM_YYYY-MM-DD.csv (e.g. KF_2026-12-01.csv).';
  }

  // Empty file
  if (msg.toLowerCase().includes('empty') || msg.toLowerCase().includes('0 rows') || msg.toLowerCase().includes('0-byte')) {
    return 'The uploaded file contains no data rows. Please ensure your file has header rows and animal records.';
  }

  // Missing EID or Date columns
  if (msg.toLowerCase().includes('eid and date') || msg.toLowerCase().includes('must contain eid')) {
    return 'Required columns are missing: the dataset must include an "EID" column and a "Date" column.';
  }

  // Missing weight measures
  if (msg.toLowerCase().includes('at least one') && msg.toLowerCase().includes('weight')) {
    return 'Missing weight measurements: the dataset must contain at least one valid weight column (such as "weight", "Pweight", or "FinalPweight").';
  }

  // Date parsing
  if (msg.toLowerCase().includes('parseable date') || msg.toLowerCase().includes('invalid date')) {
    return 'Date formatting issue: please ensure dates are formatted as DD/MM/YYYY.';
  }

  return msg;
}

// Shared scaffold: disable button while fn runs; success message goes to the result line + toast.
function wireAction(btnId, resultId, busy, failLabel, fn) {
  const btn = document.getElementById(btnId);
  const resultEl = document.getElementById(resultId);
  if (!btn || !resultEl) return;
  btn.onclick = async () => {
    btn.disabled = true;
    resultEl.className = 'data-mgmt-meta';
    resultEl.textContent = busy;
    try {
      const msg = await fn();
      resultEl.className = 'data-mgmt-meta';
      resultEl.textContent = msg;
      showToast(msg, 'success');
    } catch (err) {
      const friendly = formatUserFriendlyError(err.message, btnId.includes('logo') ? 'logo' : 'data');
      resultEl.className = 'alert alert-danger';
      resultEl.style.marginTop = '8px';
      resultEl.textContent = friendly;
      showToast(friendly, 'error');
    } finally {
      btn.disabled = false;
    }
  };
}

// Mutation handlers schedule card loads through this: rapid actions (upload -> revert ->
// upload) coalesce into one fetch per card instead of stacking duplicate GETs.
const _pendingCardLoads = {};
function loadCard(key) {
  const loaders = {
    status: loadAdminStatus,
    uploads: loadAdminUploads,
    snapshots: loadAdminSnapshots,
    users: loadAdminUsers,
    branding: loadAdminBranding,
    columns: loadAdminColumns,
  };
  clearTimeout(_pendingCardLoads[key]);
  _pendingCardLoads[key] = setTimeout(loaders[key], 150);
}

function loadAdminStatus() {
  loadSection('admin-status', `/admin/farms/${getFarmId()}/status`, (el, s) => {
    el.innerHTML = `
      <div class="cohort-stat-strip">
        <div class="cohort-stat-item primary">
          <span class="stat-label">Records</span>
          <span class="stat-val">${(s.record_count || 0).toLocaleString()}</span>
        </div>
        <span class="cohort-stat-divider" aria-hidden="true"></span>
        <div class="cohort-stat-item">
          <span class="stat-label">Snapshot backups</span>
          <span class="stat-val">${(s.snapshots_count || 0).toLocaleString()}</span>
        </div>
        <span class="cohort-stat-divider" aria-hidden="true"></span>
        <div class="cohort-stat-item">
          <span class="stat-label">Last upload</span>
          <span class="stat-val">${s.last_upload ? escapeHtml(new Date(s.last_upload).toLocaleString()) : 'Never'}</span>
        </div>
      </div>`;
  });
}

function loadAdminUploads() {
  loadSection('admin-uploads', `/admin/farms/${getFarmId()}/uploads`, (el, uploads) => {
    if (!uploads.length) {
      el.innerHTML = '<p class="data-mgmt-meta">No uploads recorded for this farm yet.</p>';
      return;
    }
    el.innerHTML = `
      <div class="table-wrap admin-table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <th>When</th>
              <th>File</th>
              <th>Inserted</th>
              <th>Skipped</th>
              <th>Overwritten</th>
              <th>Duplicates</th>
              <th>By</th>
            </tr>
          </thead>
          <tbody>
            ${uploads.map(u => `
              <tr>
                <td>${u.created_at ? escapeHtml(new Date(u.created_at).toLocaleString()) : ''}</td>
                <td>${escapeHtml(u.filename)}</td>
                <td>${u.rows_inserted ?? 0}</td>
                <td>${u.rows_skipped ?? 0}</td>
                <td>${u.rows_overwritten ?? 0}</td>
                <td>${escapeHtml(u.duplicate_mode || '')}</td>
                <td>${escapeHtml(u.uploaded_by || '')}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  });
}

function loadAdminSnapshots() {
  loadSection('admin-snapshots', `/admin/farms/${getFarmId()}/snapshots`, (el, snapshots) => {
    const badge = document.getElementById('admin-snapshots-count');
    if (badge) badge.textContent = `${snapshots.length} snapshot${snapshots.length === 1 ? '' : 's'}`;

    if (!snapshots.length) {
      el.innerHTML = '<p class="data-mgmt-meta">No partition snapshots recorded for this farm yet.</p>';
      return;
    }
    const farmId = getFarmId();
    el.innerHTML = `
      <div class="table-wrap admin-table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>When</th>
              <th>Snapshot name</th>
              <th>Records</th>
              <th>Created by</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            ${snapshots.map(s => `
              <tr>
                <td>#${s.id}</td>
                <td>${s.created_at ? escapeHtml(new Date(s.created_at).toLocaleString()) : ''}</td>
                <td>${escapeHtml(s.snapshot_name)}</td>
                <td>${(s.record_count || 0).toLocaleString()}</td>
                <td>${escapeHtml(s.created_by || 'system')}</td>
                <td>
                  <button type="button" class="btn btn-secondary btn-sm btn-revert-snapshot" data-snap-id="${s.id}" data-snap-count="${s.record_count || 0}">
                    <i class="fa fa-rotate-left" aria-hidden="true"></i> Revert
                  </button>
                </td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>`;

    el.querySelectorAll('.btn-revert-snapshot').forEach(btn => {
      btn.addEventListener('click', async () => {
        const snapId = btn.dataset.snapId;
        const count = btn.dataset.snapCount;
        const ok = window.confirm(`Revert farm ${farmId} to snapshot #${snapId} (${count} records)?\n\nA safety snapshot will be created automatically before reverting.`);
        if (!ok) return;

        btn.disabled = true;
        btn.textContent = 'Reverting…';
        try {
          await apiFetch(`/admin/farms/${farmId}/snapshots/${snapId}/revert`, { method: 'POST' });
          showToast(`Successfully reverted to snapshot #${snapId}`, 'success');
          loadCard('status');
          loadCard('uploads');
          loadCard('snapshots');
        } catch (err) {
          showToast(`Revert failed: ${err.message}`, 'error');
          btn.disabled = false;
          btn.innerHTML = '<i class="fa fa-rotate-left" aria-hidden="true"></i> Revert';
        }
      });
    });
  });
}

let adminFarmColumns = [];
let adminModalColumns = [];

async function loadAdminColumns() {
  const farmId = getFarmId();
  const wrap = document.getElementById('admin-columns-table-wrap');
  if (!wrap) return;
  wrap.innerHTML = '<div class="alert alert-muted">Loading variables…</div>';
  try {
    const data = await apiFetch(`/admin/farms/${farmId}/columns`);
    if (getFarmId() !== farmId) return;
    adminFarmColumns = data.columns || [];
    renderColumnsTable(adminFarmColumns, wrap, false);
    updateColumnsCounter(adminFarmColumns, false);
  } catch (err) {
    wrap.innerHTML = `<div class="alert alert-warning">${escapeHtml(err.message)}</div>`;
  }
}

function updateColumnsCounter(columns, isModal) {
  const fCount = (columns || []).filter(c => c.is_filter && c.kind === 'categorical').length;
  const mCount = (columns || []).filter(c => c.is_shown && c.kind === 'numeric').length;
  const targetId = isModal ? 'modal-columns-counter' : 'admin-columns-status-strip';
  const el = document.getElementById(targetId);
  if (!el) return;
  if (isModal) {
    el.textContent = `${fCount} filters, ${mCount} measures selected`;
  } else {
    el.innerHTML = `
      <div class="cohort-stat-strip" style="margin-bottom:12px;">
        <div class="cohort-stat-item primary">
          <span class="stat-label">Active filters</span>
          <span class="stat-val">${fCount}</span>
        </div>
        <span class="cohort-stat-divider" aria-hidden="true"></span>
        <div class="cohort-stat-item">
          <span class="stat-label">Active measures</span>
          <span class="stat-val">${mCount}</span>
        </div>
      </div>
    `;
  }
}

function renderColumnsTable(columns, container, isModal) {
  if (!columns || !columns.length) {
    container.innerHTML = '<p class="data-mgmt-meta" style="padding:12px;">No columns found for this farm.</p>';
    return;
  }
  const prefix = isModal ? 'mcol' : 'pcol';
  container.innerHTML = `
    <table class="data-table" style="width:100%;">
      <thead>
        <tr>
          <th>Column name</th>
          <th>Type</th>
          <th>Distinct count</th>
          <th style="text-align:center;">Filter dropdown</th>
          <th style="text-align:center;">Production measure</th>
        </tr>
      </thead>
      <tbody>
        ${columns.map((c, idx) => {
          const isFilterDisabled = c.kind !== 'categorical' || c.is_reserved;
          const isShownDisabled = c.kind !== 'numeric';
          const filterTitle = c.is_reserved ? 'Reserved system column' : (c.kind !== 'categorical' ? 'Only categorical columns can be filters' : '');
          const shownTitle = c.kind !== 'numeric' ? 'Only numeric columns can be measures' : '';
          return `
            <tr>
              <td><strong>${escapeHtml(c.column_name)}</strong>${c.is_reserved ? ' <span class="badge-secondary" style="font-size:11px;">reserved</span>' : ''}</td>
              <td><span class="badge-secondary" style="text-transform:lowercase;">${escapeHtml(c.kind)}</span></td>
              <td>${c.distinct_count ?? 0}</td>
              <td style="text-align:center;">
                <input type="checkbox" id="${prefix}-filter-${idx}" ${c.is_filter ? 'checked' : ''} ${isFilterDisabled ? 'disabled' : ''} title="${filterTitle}" aria-label="Toggle filter for ${escapeHtml(c.column_name)}">
              </td>
              <td style="text-align:center;">
                <input type="checkbox" id="${prefix}-shown-${idx}" ${c.is_shown ? 'checked' : ''} ${isShownDisabled ? 'disabled' : ''} title="${shownTitle}" aria-label="Toggle measure for ${escapeHtml(c.column_name)}">
              </td>
            </tr>
          `;
        }).join('')}
      </tbody>
    </table>
  `;

  columns.forEach((c, idx) => {
    const fBox = document.getElementById(`${prefix}-filter-${idx}`);
    if (fBox && !fBox.disabled) {
      fBox.onchange = () => {
        c.is_filter = fBox.checked;
        updateColumnsCounter(columns, isModal);
      };
    }
    const sBox = document.getElementById(`${prefix}-shown-${idx}`);
    if (sBox && !sBox.disabled) {
      sBox.onchange = () => {
        c.is_shown = sBox.checked;
        updateColumnsCounter(columns, isModal);
      };
    }
  });
}

function wireColumnsCard() {
  wireAction('btn-inspect-columns', 'admin-columns-result', 'Auto-detecting…', 'Auto-detect failed', async () => {
    const farmId = getFarmId();
    const data = await apiFetch(`/admin/farms/${farmId}/columns/inspect`, { method: 'POST' });
    adminFarmColumns = data.columns || [];
    const wrap = document.getElementById('admin-columns-table-wrap');
    if (wrap) renderColumnsTable(adminFarmColumns, wrap, false);
    updateColumnsCounter(adminFarmColumns, false);
    return `Auto-detected ${data.filters_count ?? 0} filters and ${data.measures_count ?? 0} measures.`;
  });

  wireAction('btn-save-columns', 'admin-columns-result', 'Saving…', 'Save failed', async () => {
    const farmId = getFarmId();
    const shownNumeric = adminFarmColumns.filter(c => c.is_shown && (c.kind === 'numeric' || ['weight', 'pweight', 'finalpweight'].includes(c.column_name.toLowerCase())));
    const hasWeight = shownNumeric.some(c => ['weight', 'pweight', 'finalpweight'].includes(c.column_name.toLowerCase()));
    if (!hasWeight) {
      throw new Error("Configuration must include at least one weight measure ('weight', 'pweight', or 'finalpweight')");
    }
    const data = await apiFetch(`/admin/farms/${farmId}/columns`, {
      method: 'PUT',
      body: JSON.stringify({ columns: adminFarmColumns }),
    });
    adminFarmColumns = data.columns || [];
    const wrap = document.getElementById('admin-columns-table-wrap');
    if (wrap) renderColumnsTable(adminFarmColumns, wrap, false);
    updateColumnsCounter(adminFarmColumns, false);
    if (typeof refreshFarmFilterMetadata === 'function') {
      await refreshFarmFilterMetadata(farmId);
    }
    return `Saved ${data.filters_count ?? 0} filters and ${data.measures_count ?? 0} measures successfully.`;
  });
}

function openColumnsModal(columns, title, desc, onConfirm) {
  const modal = document.getElementById('modal-farm-columns');
  if (!modal) return;
  adminModalColumns = JSON.parse(JSON.stringify(columns));
  const titleEl = document.getElementById('modal-columns-title');
  if (titleEl && title) titleEl.textContent = title;
  const descEl = document.getElementById('modal-columns-desc');
  if (descEl && desc) descEl.textContent = desc;
  const errEl = document.getElementById('modal-columns-error');
  if (errEl) {
    errEl.textContent = '';
    errEl.classList.add('hidden');
  }
  const bodyEl = document.getElementById('modal-columns-body');
  if (bodyEl) renderColumnsTable(adminModalColumns, bodyEl, true);
  updateColumnsCounter(adminModalColumns, true);
  modal.classList.remove('hidden');

  const confirmBtn = document.getElementById('btn-confirm-columns-modal');
  if (confirmBtn) {
    confirmBtn.onclick = async () => {
      if (errEl) {
        errEl.textContent = '';
        errEl.classList.add('hidden');
      }

      // Validate weight measure invariant before sending request
      const weightMeasures = ['weight', 'pweight', 'finalpweight'];
      const shownWeight = adminModalColumns.some(c => c.is_shown && weightMeasures.includes(String(c.column_name).toLowerCase()));
      if (!onConfirm && !shownWeight) {
        const msg = "Configuration must include at least one weight measure ('weight', 'pweight', or 'finalpweight')";
        if (errEl) {
          errEl.textContent = msg;
          errEl.classList.remove('hidden');
        }
        showToast(msg, 'error');
        return;
      }

      confirmBtn.disabled = true;
      try {
        if (onConfirm) {
          await onConfirm(adminModalColumns);
        } else {
          const farmId = getFarmId();
          await apiFetch(`/admin/farms/${farmId}/columns`, {
            method: 'PUT',
            body: JSON.stringify({ columns: adminModalColumns }),
          });
        }
        modal.classList.add('hidden');
        showToast('Variables and filters configured successfully', 'success');
        loadAdminColumns();
      } catch (err) {
        const errorMsg = err.message || 'Save failed';
        if (errEl) {
          errEl.textContent = errorMsg;
          errEl.classList.remove('hidden');
        }
        showToast(`Save failed: ${errorMsg}`, 'error');
      } finally {
        confirmBtn.disabled = false;
      }
    };
  }
}

function wireColumnsModal() {
  const modal = document.getElementById('modal-farm-columns');
  const closeBtn = document.getElementById('btn-close-columns-modal');
  const cancelBtn = document.getElementById('btn-cancel-columns-modal');
  if (!modal) return;
  const closeModal = () => modal.classList.add('hidden');
  closeBtn?.addEventListener('click', closeModal);
  cancelBtn?.addEventListener('click', closeModal);
  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeModal();
  });
}

function wireDeleteFarmModal() {
  const triggerBtn = document.getElementById('admin-delete-farm-btn');
  const modal = document.getElementById('modal-delete-farm');
  const closeBtn = document.getElementById('btn-close-delete-farm-modal');
  const cancelBtn = document.getElementById('btn-cancel-delete-farm');
  const submitBtn = document.getElementById('btn-submit-delete-farm');
  const confirmText = document.getElementById('delete-farm-confirm-text');
  const kfGuard = document.getElementById('delete-farm-kf-guard');
  const forceCb = document.getElementById('delete-farm-force-cb');
  const confirmInput = document.getElementById('delete-farm-confirm-input');
  const expectedCodeSpan = document.getElementById('delete-farm-expected-code');
  const errorEl = document.getElementById('delete-farm-error');

  if (!modal) return;

  const validateGuard = () => {
    const currentFarm = getFarmId();
    const isKF = currentFarm === 'KF';
    const val = (confirmInput?.value || '').trim().toUpperCase();
    const matchesTarget = val === currentFarm.toUpperCase() || val === `DELETE ${currentFarm.toUpperCase()}` || val === 'DELETE';
    
    if (isKF) {
      const isForceChecked = Boolean(forceCb?.checked);
      const isValid = matchesTarget && isForceChecked;
      if (submitBtn) submitBtn.disabled = !isValid;
      return isValid;
    }

    if (submitBtn) submitBtn.disabled = !matchesTarget;
    return matchesTarget;
  };

  const openModal = () => {
    const currentFarm = getFarmId();
    const farmName = typeof farmNameFor === 'function' ? farmNameFor(currentFarm) : currentFarm;
    if (confirmText) {
      confirmText.textContent = `Are you sure you want to decommission farm "${farmName}" (${currentFarm})?`;
    }
    if (expectedCodeSpan) {
      expectedCodeSpan.textContent = currentFarm;
    }
    if (confirmInput) {
      confirmInput.value = '';
      confirmInput.placeholder = currentFarm;
    }
    if (errorEl) errorEl.textContent = '';
    if (forceCb) forceCb.checked = false;

    const isKF = currentFarm === 'KF';
    if (kfGuard) {
      kfGuard.classList.toggle('hidden', !isKF);
    }
    if (submitBtn) submitBtn.disabled = true;
    modal.classList.remove('hidden');
    setTimeout(() => confirmInput?.focus(), 50);
  };

  const closeModal = () => {
    modal.classList.add('hidden');
  };

  triggerBtn?.addEventListener('click', openModal);
  closeBtn?.addEventListener('click', closeModal);
  cancelBtn?.addEventListener('click', closeModal);

  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeModal();
  });

  confirmInput?.addEventListener('input', () => {
    if (errorEl) errorEl.textContent = '';
    validateGuard();
  });

  confirmInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !submitBtn?.disabled) {
      e.preventDefault();
      submitBtn?.click();
    }
  });

  forceCb?.addEventListener('change', () => {
    if (errorEl) errorEl.textContent = '';
    validateGuard();
  });

  submitBtn?.addEventListener('click', async () => {
    const currentFarm = getFarmId();
    const isKF = currentFarm === 'KF';

    if (!validateGuard()) {
      if (isKF && !forceCb?.checked) {
        if (errorEl) errorEl.textContent = 'Please check the confirmation box and type the farm code to confirm.';
      } else {
        if (errorEl) errorEl.textContent = `Please type "${currentFarm}" to confirm decommissioning.`;
      }
      return;
    }

    const isForce = isKF && forceCb?.checked;

    if (submitBtn) submitBtn.disabled = true;
    if (errorEl) errorEl.textContent = 'Decommissioning farm partition…';

    try {
      const url = `/admin/farms/${encodeURIComponent(currentFarm)}${isForce ? '?force=true' : ''}`;
      const res = await apiFetch(url, { method: 'DELETE' });
      showToast(res.detail || `Farm ${currentFarm} decommissioned successfully`, 'success');
      closeModal();
      
      const remainingFarms = await apiFetch('/farms');
      availableAdminFarms = remainingFarms;
      const nextFarm = remainingFarms.length > 0 ? remainingFarms[0].farm_id : 'KF';
      if (typeof window.initFarmSelector === 'function') {
        await window.initFarmSelector(nextFarm);
      }
      if (typeof renderCurrentPage === 'function') {
        renderCurrentPage();
      }
    } catch (err) {
      if (errorEl) errorEl.textContent = `Decommission failed: ${err.message}`;
    } finally {
      validateGuard();
    }
  });
}

function wireCreateFarmModal() {
  const triggerBtn = document.getElementById('admin-new-farm-btn');
  const modal = document.getElementById('modal-new-farm');
  const closeBtn = document.getElementById('btn-close-new-farm-modal');
  const cancelBtn = document.getElementById('btn-cancel-new-farm');
  const form = document.getElementById('form-create-new-farm');
  const errorEl = document.getElementById('new-farm-error');

  if (!modal) return;

  const openModal = () => {
    modal.classList.remove('hidden');
    if (errorEl) errorEl.textContent = '';
    const idInput = document.getElementById('new-farm-id');
    if (idInput) {
      idInput.value = '';
      idInput.focus();
    }
    const nameInput = document.getElementById('new-farm-name');
    if (nameInput) nameInput.value = '';
    const slugInput = document.getElementById('new-farm-slug');
    if (slugInput) slugInput.value = '';
  };

  const closeModal = () => {
    modal.classList.add('hidden');
  };

  triggerBtn?.addEventListener('click', openModal);
  closeBtn?.addEventListener('click', closeModal);
  cancelBtn?.addEventListener('click', closeModal);

  modal.addEventListener('click', (e) => {
    if (e.target === modal) closeModal();
  });

  form?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const farmId = (document.getElementById('new-farm-id')?.value || '').trim().toUpperCase();
    const farmName = (document.getElementById('new-farm-name')?.value || '').trim();
    let slug = (document.getElementById('new-farm-slug')?.value || '').trim().toLowerCase();
    if (!slug) slug = farmId.toLowerCase();

    if (!farmId || !farmName) {
      if (errorEl) errorEl.textContent = 'Farm ID and farm name are required';
      return;
    }

    const submitBtn = document.getElementById('btn-submit-new-farm');
    if (submitBtn) submitBtn.disabled = true;
    if (errorEl) errorEl.textContent = 'Creating farm…';

    try {
      await apiFetch('/admin/farms', {
        method: 'POST',
        body: JSON.stringify({ farm_id: farmId, farm_name: farmName, slug }),
      });
      showToast(`Farm ${farmId} created successfully`, 'success');
      closeModal();
      if (typeof window.initFarmSelector === 'function') {
        await window.initFarmSelector(farmId);
      }
      loadCard('status');
      loadCard('uploads');
      loadCard('snapshots');
      apiFetch('/farms').then((farms) => {
        availableAdminFarms = farms;
        renderFarmDropdown();
      }).catch(() => {});
    } catch (err) {
      if (errorEl) errorEl.textContent = `Creation failed: ${err.message}`;
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  });
}

let currentEditingUser = null;
let cachedAdminUsers = [];
let availableAdminFarms = [];
let selectedAdminFarmIds = [];
let selectedAdminRole = 'user';

function renderFarmDropdown() {
  const container = document.getElementById('picker-user-farms');
  if (!container) return;

  let summaryText = 'Select farms';
  if (selectedAdminFarmIds.length === availableAdminFarms.length && availableAdminFarms.length > 0) {
    summaryText = `All farms (${availableAdminFarms.length})`;
  } else if (selectedAdminFarmIds.length === 1) {
    const f = availableAdminFarms.find(item => item.farm_id === selectedAdminFarmIds[0]);
    summaryText = f ? `${f.farm_name} (${f.farm_id})` : selectedAdminFarmIds[0];
  } else if (selectedAdminFarmIds.length > 1) {
    summaryText = `${selectedAdminFarmIds.length} farms selected`;
  }

  const optionsHtml = availableAdminFarms.map(f => {
    const isChecked = selectedAdminFarmIds.includes(f.farm_id);
    return `
      <div class="picker-option-item" style="padding: 6px 12px;">
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer;width:100%;margin:0;">
          <input type="checkbox" class="admin-farm-checkbox" value="${escapeHtml(f.farm_id)}" ${isChecked ? 'checked' : ''}>
          <span>${escapeHtml(f.farm_name)} (${escapeHtml(f.farm_id)})</span>
        </label>
      </div>`;
  }).join('');

  container.innerHTML = `
    <button type="button" class="picker-trigger" id="picker-btn-user-farms" aria-haspopup="listbox" aria-expanded="false">
      <span class="picker-trigger-text">${escapeHtml(summaryText)}</span>
      <i class="fa fa-chevron-down picker-chevron" aria-hidden="true"></i>
    </button>
    <div class="picker-menu" role="listbox" id="picker-menu-user-farms" style="max-height: 200px; overflow-y: auto;">
      <div class="picker-options-list">
        ${optionsHtml}
      </div>
    </div>
  `;

  const trigger = container.querySelector('.picker-trigger');
  trigger?.addEventListener('click', (e) => {
    e.stopPropagation();
    const isOpen = container.classList.contains('open');
    document.querySelectorAll('.custom-picker.open').forEach(p => {
      if (p !== container) {
        p.classList.remove('open', 'dropup');
        p.querySelector('.picker-trigger')?.setAttribute('aria-expanded', 'false');
      }
    });
    container.classList.toggle('open', !isOpen);
    trigger.setAttribute('aria-expanded', !isOpen ? 'true' : 'false');
  });

  container.querySelectorAll('.admin-farm-checkbox').forEach(cb => {
    cb.addEventListener('change', (e) => {
      e.stopPropagation();
      selectedAdminFarmIds = [...container.querySelectorAll('.admin-farm-checkbox:checked')].map(c => c.value);
      let newSummary = 'Select farms';
      if (selectedAdminFarmIds.length === availableAdminFarms.length && availableAdminFarms.length > 0) {
        newSummary = `All farms (${availableAdminFarms.length})`;
      } else if (selectedAdminFarmIds.length === 1) {
        const f = availableAdminFarms.find(item => item.farm_id === selectedAdminFarmIds[0]);
        newSummary = f ? `${f.farm_name} (${f.farm_id})` : selectedAdminFarmIds[0];
      } else if (selectedAdminFarmIds.length > 1) {
        newSummary = `${selectedAdminFarmIds.length} farms selected`;
      }
      const textSpan = container.querySelector('.picker-trigger-text');
      if (textSpan) textSpan.textContent = newSummary;
    });
  });
}

function renderRoleDropdown(currentVal = 'user', options = [{ value: 'user', label: 'User' }, { value: 'owner', label: 'Owner' }], disabled = false) {
  selectedAdminRole = currentVal;
  if (typeof renderSingleSelectPicker === 'function') {
    renderSingleSelectPicker('picker-user-role', 'user_role', options, selectedAdminRole, (val) => {
      selectedAdminRole = val;
    }, false);
  }
  const el = document.getElementById('picker-user-role');
  if (el) el.classList.toggle('disabled', disabled);
}

function getRoleBadgeHtml(roleLabel, isImmutable = false) {
  const text = isImmutable ? `${roleLabel} (Immutable)` : roleLabel;
  if (roleLabel === 'Admin') {
    return `<span class="badge badge-role-admin" title="Universal administrative access">${escapeHtml(text)}</span>`;
  }
  if (roleLabel === 'Owner') {
    return `<span class="badge badge-role-owner" title="Farm owner access">${escapeHtml(text)}</span>`;
  }
  return `<span class="badge badge-role-user" title="Standard staff access">${escapeHtml(text)}</span>`;
}

function setCreateUserMode() {
  currentEditingUser = null;
  document.querySelectorAll('.admin-user-row').forEach(r => r.classList.remove('selected'));

  const heading = document.getElementById('user-card-heading');
  if (heading) {
    heading.innerHTML = '<i class="fa fa-user-plus" aria-hidden="true" id="user-card-icon"></i> <span id="user-card-title">Create User</span>';
  }

  const actionContainer = document.getElementById('user-card-header-action');
  if (actionContainer) {
    actionContainer.innerHTML = '<span class="badge-primary" id="user-card-badge">Min 8 chars</span>';
  }

  const usernameInput = document.getElementById('admin-user-username');
  if (usernameInput) {
    usernameInput.value = '';
    usernameInput.disabled = false;
    usernameInput.title = '';
  }

  const pwdLabel = document.getElementById('admin-user-password-label');
  if (pwdLabel) pwdLabel.textContent = 'Password';

  const pwdInput = document.getElementById('admin-user-password');
  if (pwdInput) {
    pwdInput.value = '';
    pwdInput.placeholder = 'At least 8 characters';
  }

  renderRoleDropdown('user');
  selectedAdminFarmIds = availableAdminFarms.map(f => f.farm_id);
  renderFarmDropdown();

  const submitBtn = document.getElementById('admin-user-submit-btn');
  if (submitBtn) {
    submitBtn.innerHTML = '<i class="fa fa-user-plus" aria-hidden="true"></i> <span id="admin-user-submit-label">Create User</span>';
  }

  const resultEl = document.getElementById('admin-user-result');
  if (resultEl) resultEl.textContent = '';
}

function setEditUserMode(user) {
  currentEditingUser = user;
  document.querySelectorAll('.admin-user-row').forEach(r => {
    r.classList.toggle('selected', Number(r.dataset.userId) === user.id);
  });

  const heading = document.getElementById('user-card-heading');
  if (heading) {
    heading.innerHTML = `<i class="fa fa-user-pen" aria-hidden="true" id="user-card-icon"></i> <span id="user-card-title">Edit User: ${escapeHtml(user.username)}</span>`;
  }

  const isAdminUser = Boolean(user.is_admin || (user.role || '').toLowerCase() === 'admin' || String(user.username).toLowerCase() === 'admin');
  const isActive = user.is_active !== false;

  const actionContainer = document.getElementById('user-card-header-action');
  if (actionContainer) {
    actionContainer.innerHTML = `
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <button type="button" class="btn ${isActive ? 'btn-secondary' : 'btn-success'} btn-sm" id="btn-toggle-status" ${isAdminUser ? 'disabled title="Cannot deactivate the system administrator account"' : ''}>
          <i class="fa ${isActive ? 'fa-ban' : 'fa-check'}" aria-hidden="true"></i> ${isActive ? 'Deactivate Account' : 'Activate Account'}
        </button>
        <button type="button" class="btn btn-danger btn-sm" id="btn-delete-user" ${isAdminUser ? 'disabled title="Cannot delete the system administrator account"' : ''}>
          <i class="fa fa-trash" aria-hidden="true"></i> Delete User
        </button>
        <button type="button" class="btn btn-secondary btn-sm" id="btn-switch-create">
          <i class="fa fa-plus" aria-hidden="true"></i> Create New User
        </button>
      </div>
    `;
    document.getElementById('btn-switch-create')?.addEventListener('click', setCreateUserMode);

    if (!isAdminUser) {
      document.getElementById('btn-toggle-status')?.addEventListener('click', async () => {
        try {
          const nextActive = !isActive;
          const res = await apiFetch(`/admin/users/${user.id}/status`, {
            method: 'PUT',
            body: JSON.stringify({ is_active: nextActive }),
          });
          user.is_active = nextActive;
          showToast(res.detail || 'Status updated', 'success');
          loadCard('users');
          setEditUserMode(user);
        } catch (err) {
          showToast(err.message, 'error');
        }
      });

      document.getElementById('btn-delete-user')?.addEventListener('click', async () => {
        if (!confirm(`Are you sure you want to permanently delete user '${user.username}'?`)) return;
        try {
          const res = await apiFetch(`/admin/users/${user.id}`, {
            method: 'DELETE',
          });
          showToast(res.detail || 'User deleted', 'success');
          setCreateUserMode();
          loadCard('users');
        } catch (err) {
          showToast(err.message, 'error');
        }
      });
    }
  }

  const usernameInput = document.getElementById('admin-user-username');
  if (usernameInput) {
    usernameInput.value = user.username;
    if (String(user.username).toLowerCase() === 'owner') {
      usernameInput.disabled = true;
      usernameInput.title = 'System owner account username cannot be renamed';
    } else {
      usernameInput.disabled = false;
      usernameInput.title = '';
    }
  }

  const pwdLabel = document.getElementById('admin-user-password-label');
  if (pwdLabel) pwdLabel.textContent = 'New Password (optional)';

  const pwdInput = document.getElementById('admin-user-password');
  if (pwdInput) {
    pwdInput.value = '';
    pwdInput.placeholder = 'Leave blank to keep current';
  }

  // Role: editable dropdown for non-admin accounts; immutable accounts get a disabled one.
  const pickerRole = document.getElementById('picker-user-role');
  if (pickerRole) {
    if (isAdminUser) {
      renderRoleDropdown('admin', [{ value: 'admin', label: 'Admin' }], true);
    } else {
      const currentRole = String(user.role || '').toLowerCase() === 'owner' ? 'owner' : 'user';
      renderRoleDropdown(currentRole);
    }
  }

  // Pre-check farm assignments
  selectedAdminFarmIds = Array.isArray(user.farm_ids) ? [...user.farm_ids] : [];
  renderFarmDropdown();

  const submitBtn = document.getElementById('admin-user-submit-btn');
  if (submitBtn) {
    submitBtn.innerHTML = '<i class="fa fa-check" aria-hidden="true"></i> <span id="admin-user-submit-label">Save Changes</span>';
  }

  const resultEl = document.getElementById('admin-user-result');
  if (resultEl) resultEl.textContent = '';
}

let adminUserFarmFilter = 'ALL';

function loadAdminUsers() {
  loadSection('admin-users', '/admin/users', (el, users) => {
    cachedAdminUsers = users;
    const countBadge = document.getElementById('admin-users-count');
    if (countBadge) {
      countBadge.textContent = `${users.length} account${users.length === 1 ? '' : 's'}`;
    }

    const activeAdmins = users.filter(u => Boolean((u.is_admin || (u.role || '').toLowerCase() === 'admin' || String(u.username).toLowerCase() === 'admin') && u.is_active !== false));
    const anomalyBanner = activeAdmins.length > 1
      ? `<div class="alert alert-danger" style="margin-bottom:12px;"><i class="fa fa-triangle-exclamation" aria-hidden="true"></i> <strong>Security Anomaly:</strong> Multiple active administrators detected (${activeAdmins.map(a => escapeHtml(a.username)).join(', ')}). The system invariant permits exactly 1 active administrator.</div>`
      : '';

    const allFarmIds = Array.from(new Set(availableAdminFarms.map(f => f.farm_id).concat(
      users.flatMap(u => Array.isArray(u.farm_ids) ? u.farm_ids : [])
    ))).sort();

    const getFilteredUsers = () => {
      return users.filter(u => {
        if (adminUserFarmFilter === 'ALL') return true;
        const isAdminUser = Boolean(u.is_admin || (u.role || '').toLowerCase() === 'admin' || String(u.username).toLowerCase() === 'admin');
        if (isAdminUser) return true;
        if (adminUserFarmFilter === 'UNASSIGNED') return !u.farm_ids || u.farm_ids.length === 0;
        return Array.isArray(u.farm_ids) && u.farm_ids.includes(adminUserFarmFilter);
      });
    };

    const buildTableRowsHtml = (filteredUsers) => {
      if (!filteredUsers.length) {
        return '<tr><td colspan="7" class="text-muted" style="text-align:center;padding:16px;">No users found for selected farm filter.</td></tr>';
      }
      return filteredUsers.map(u => {
        const isAdminUser = Boolean(u.is_admin || (u.role || '').toLowerCase() === 'admin' || String(u.username).toLowerCase() === 'admin');
        const isOwner = !isAdminUser && (String(u.role || '').toLowerCase() === 'owner' || String(u.username).toLowerCase() === 'owner');
        const roleLabel = isAdminUser ? 'Admin' : (isOwner ? 'Owner' : 'User');
        const isActive = u.is_active !== false;
        const isSelected = currentEditingUser && currentEditingUser.id === u.id;
        const lastLoginStr = u.last_login ? escapeHtml(new Date(u.last_login).toLocaleString()) : '<span class="text-muted">Never</span>';

        let farmBadgesHtml = '';
        if (isAdminUser) {
          farmBadgesHtml = '<span class="badge badge-role-admin" title="Universal bypass across all farms">All farms</span>';
        } else if (Array.isArray(u.farm_ids) && u.farm_ids.length > 0) {
          farmBadgesHtml = u.farm_ids.map(fid => `<span class="badge badge-farm-tag">${escapeHtml(fid)}</span>`).join('');
        } else {
          farmBadgesHtml = '<span class="badge badge-inactive" title="Unassigned: user fails closed on login">Unassigned</span>';
        }

        return `
          <tr class="admin-user-row ${isSelected ? 'selected' : ''}" data-user-id="${u.id}" title="Click to edit user">
            <td>${u.id}</td>
            <td><strong>${escapeHtml(u.username)}</strong></td>
            <td>${getRoleBadgeHtml(roleLabel)}</td>
            <td>${farmBadgesHtml}</td>
            <td><span class="badge ${isActive ? 'badge-active' : 'badge-inactive'}">${isActive ? 'Active' : 'Inactive'}</span></td>
            <td>${lastLoginStr}</td>
            <td>${u.created_at ? escapeHtml(new Date(u.created_at).toLocaleDateString()) : ''}</td>
          </tr>`;
      }).join('');
    };

    const initialFiltered = getFilteredUsers();

    el.innerHTML = `
      ${anomalyBanner}
      <div class="admin-users-toolbar" style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;gap:8px;flex-wrap:wrap;">
        <div style="display:flex;align-items:center;gap:8px;">
          <label style="font-size:12px;font-weight:600;margin:0;"><i class="fa fa-filter" aria-hidden="true"></i> View by farm:</label>
          <div class="custom-picker" id="picker-users-filter-farm" style="min-width:180px;"></div>
        </div>
        <span id="admin-users-filter-count" style="font-size:11px;color:var(--text-secondary);">Showing ${initialFiltered.length} of ${users.length} accounts</span>
      </div>
      <div class="table-wrap admin-table-scroll">
        <table class="data-table">
          <thead>
            <tr>
              <th>ID</th>
              <th>Username</th>
              <th>Role</th>
              <th>Assigned farms</th>
              <th>Status</th>
              <th>Last Login</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody id="admin-users-tbody">
            ${buildTableRowsHtml(initialFiltered)}
          </tbody>
        </table>
      </div>`;

    const wireRowClicks = () => {
      el.querySelectorAll('.admin-user-row').forEach(row => {
        row.addEventListener('click', () => {
          const uid = Number(row.dataset.userId);
          const user = cachedAdminUsers.find(u => u.id === uid);
          if (user) setEditUserMode(user);
        });
      });
    };

    wireRowClicks();

    const farmFilterOptions = [
      { value: 'ALL', label: `All users (${users.length})` },
      ...allFarmIds.map(fid => {
        const count = users.filter(u => Boolean(u.is_admin || (u.farm_ids && u.farm_ids.includes(fid)))).length;
        return { value: fid, label: `${fid} (${count})` };
      }),
      { value: 'UNASSIGNED', label: `Unassigned (${users.filter(u => !u.is_admin && (!u.farm_ids || u.farm_ids.length === 0)).length})` }
    ];

    if (typeof renderSingleSelectPicker === 'function') {
      renderSingleSelectPicker('picker-users-filter-farm', 'users_filter_farm', farmFilterOptions, adminUserFarmFilter, (val) => {
        adminUserFarmFilter = val;
        const updated = getFilteredUsers();
        const tbody = el.querySelector('#admin-users-tbody');
        if (tbody) tbody.innerHTML = buildTableRowsHtml(updated);
        const countEl = el.querySelector('#admin-users-filter-count');
        if (countEl) countEl.textContent = `Showing ${updated.length} of ${users.length} accounts`;
        wireRowClicks();
      }, false);
    } else {
      const pickerEl = el.querySelector('#picker-users-filter-farm');
      if (pickerEl) {
        pickerEl.innerHTML = `
          <select id="filter-users-by-farm-fallback" class="form-control" style="height:32px;font-size:12px;">
            ${farmFilterOptions.map(o => `<option value="${escapeHtml(o.value)}" ${adminUserFarmFilter === o.value ? 'selected' : ''}>${escapeHtml(o.label)}</option>`).join('')}
          </select>`;
        pickerEl.querySelector('select')?.addEventListener('change', (e) => {
          adminUserFarmFilter = e.target.value;
          const updated = getFilteredUsers();
          const tbody = el.querySelector('#admin-users-tbody');
          if (tbody) tbody.innerHTML = buildTableRowsHtml(updated);
          const countEl = el.querySelector('#admin-users-filter-count');
          if (countEl) countEl.textContent = `Showing ${updated.length} of ${users.length} accounts`;
          wireRowClicks();
        });
      }
    }
  });
}

function wireUserForm() {
  apiFetch('/farms').then((farms) => {
    availableAdminFarms = farms;
    selectedAdminFarmIds = farms.map(f => f.farm_id);
    renderFarmDropdown();
  }).catch(() => {});

  renderRoleDropdown('user');

  wireAction('admin-user-submit-btn', 'admin-user-result', 'Saving…', 'Action failed', async () => {
    const username = document.getElementById('admin-user-username').value.trim();
    const password = document.getElementById('admin-user-password').value;

    if (!currentEditingUser) {
      if (!username || !password) throw new Error('Username and password are required');
      if (password.length < 8) throw new Error('Password must be at least 8 characters');
      if (!selectedAdminFarmIds.length) throw new Error('Select at least one farm');
      const created = await apiFetch('/admin/users', {
        method: 'POST',
        body: JSON.stringify({
          username,
          password,
          is_admin: false,
          role: selectedAdminRole,
          farm_ids: selectedAdminFarmIds,
        }),
      });
      setCreateUserMode();
      loadCard('users');
      return `Created user ${created.username} (id ${created.id}).`;
    } else {
      if (!username) throw new Error('Username cannot be empty');
      const updates = [];

      if (username !== currentEditingUser.username) {
        if (String(currentEditingUser.username).toLowerCase() === 'owner') {
          throw new Error('Cannot rename system owner account');
        }
        const userRes = await apiFetch('/admin/credentials/username', {
          method: 'PUT',
          body: JSON.stringify({ user_id: currentEditingUser.id, new_username: username }),
        });
        const sessionUser = typeof getUser === 'function' ? getUser() : null;
        if (sessionUser && sessionUser.id === currentEditingUser.id) {
          sessionUser.username = userRes.username;
          sessionStorage.setItem('user', JSON.stringify(sessionUser));
        }
        currentEditingUser.username = userRes.username;
        updates.push('username');
      }

      if (password) {
        if (password.length < 8) throw new Error('Password must be at least 8 characters');
        await apiFetch('/admin/credentials/password', {
          method: 'PUT',
          body: JSON.stringify({ user_id: currentEditingUser.id, new_password: password }),
        });
        document.getElementById('admin-user-password').value = '';
        updates.push('password');
      }

      // Check role change (non-admin accounts only)
      const isAdminTarget = Boolean(currentEditingUser.is_admin || (currentEditingUser.role || '').toLowerCase() === 'admin' || String(currentEditingUser.username).toLowerCase() === 'admin');
      if (!isAdminTarget) {
        const currentRole = String(currentEditingUser.role || '').toLowerCase() === 'owner' ? 'owner' : 'user';
        if (selectedAdminRole !== currentRole) {
          const roleRes = await apiFetch(`/admin/users/${currentEditingUser.id}/role`, {
            method: 'PUT',
            body: JSON.stringify({ role: selectedAdminRole }),
          });
          currentEditingUser.role = roleRes.role;
          updates.push(`role to '${roleRes.role}'`);
        }
      }

      // Check farm assignments
      const oldFarms = Array.isArray(currentEditingUser.farm_ids) ? [...currentEditingUser.farm_ids].sort() : [];
      const newFarms = [...selectedAdminFarmIds].sort();
      const farmsChanged = JSON.stringify(oldFarms) !== JSON.stringify(newFarms);

      if (farmsChanged) {
        const isAdminUser = Boolean(currentEditingUser.is_admin || (currentEditingUser.role || '').toLowerCase() === 'admin' || String(currentEditingUser.username).toLowerCase() === 'admin');
        if (!isAdminUser && !selectedAdminFarmIds.length) {
          throw new Error('Non-admin users must be assigned to at least one farm');
        }
        await apiFetch(`/admin/users/${currentEditingUser.id}/farms`, {
          method: 'PUT',
          body: JSON.stringify({ farm_ids: selectedAdminFarmIds }),
        });
        currentEditingUser.farm_ids = [...selectedAdminFarmIds];
        updates.push('farm assignments');
      }

      if (!updates.length) {
        return 'No changes made.';
      }

      loadCard('users');
      const updatedName = currentEditingUser.username;
      const heading = document.getElementById('user-card-heading');
      if (heading) {
        heading.innerHTML = `<i class="fa fa-user-pen" aria-hidden="true" id="user-card-icon"></i> <span id="user-card-title">Edit User: ${escapeHtml(updatedName)}</span>`;
      }
      return `Updated ${updates.join(' and ')} for user ${updatedName}.`;
    }
  });
}

// Branding & logos card: uploads/deletes/reset via /admin/farms/{id}/logos (moved from the old popup).
function brandingMessage(text, isError = false) {
  const el = document.getElementById('admin-branding-result');
  if (!el) return;
  if (!text) {
    el.textContent = '';
    el.className = 'data-mgmt-meta';
    return;
  }
  if (isError) {
    el.className = 'alert alert-danger';
    el.style.marginTop = '8px';
  } else {
    el.className = 'data-mgmt-meta';
    el.style.marginTop = '4px';
  }
  el.textContent = text;
}

function loadAdminBranding() {
  loadSection('admin-branding-gallery', `/admin/farms/${getFarmId()}/logos`, (el, data) => {
    const logos = data.logos || [];
    if (!logos.length) {
      el.innerHTML = '<p class="data-mgmt-meta">No logos configured. Using system fallback.</p>';
      return;
    }
    el.innerHTML = `
      <div class="branding-gallery">${logos.map((l) => `
        <div class="branding-logo-item">
          <img src="${escapeHtml(resolveAssetUrl(l.url))}" alt="${escapeHtml(l.filename)}" class="branding-logo-img">
          <div class="branding-logo-details">
            <span class="branding-logo-filename" title="${escapeHtml(l.filename)}">${escapeHtml(l.filename)}</span>
            <span class="branding-logo-type">${l.is_default ? 'Partner default' : 'Farm custom'}</span>
          </div>
          <button type="button" class="btn btn-secondary btn-sm btn-admin-delete-logo" data-filename="${escapeHtml(l.filename)}" title="Delete logo">Delete</button>
        </div>`).join('')}
      </div>`;
    el.querySelectorAll('.btn-admin-delete-logo').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const fname = btn.dataset.filename;
        if (!fname) return;
        try {
          await apiFetch(`/admin/farms/${getFarmId()}/logos/${encodeURIComponent(fname)}`, { method: 'DELETE' });
          brandingMessage(`Logo '${fname}' removed`);
          if (typeof loadFarmLogos === 'function') loadFarmLogos(getFarmId());
          loadCard('branding');
        } catch (err) {
          const friendly = formatUserFriendlyError(err.message, 'logo');
          brandingMessage(friendly, true);
          showToast(friendly, 'error');
        }
      });
    });
  });
}

function wireAdminBranding() {
  const fileInput = document.getElementById('admin-logo-file');
  const fileNameSpan = document.getElementById('admin-logo-file-name');
  const uploadBtn = document.getElementById('admin-logo-upload');

  fileInput?.addEventListener('change', () => {
    const file = fileInput.files?.[0];
    fileNameSpan.textContent = file ? file.name : 'No file chosen';
    fileNameSpan.title = file ? file.name : '';
    uploadBtn.disabled = !file;
    brandingMessage('');
  });

  uploadBtn?.addEventListener('click', async () => {
    const file = fileInput.files?.[0];
    if (!file) return;
    const farmId = getFarmId();
    if (file.size > 2 * 1024 * 1024) {
      const friendly = 'Image file exceeds 2 MB. Please select a smaller logo.';
      brandingMessage(friendly, true);
      showToast(friendly, 'error');
      return;
    }
    const formData = new FormData();
    formData.append('file', file);
    uploadBtn.disabled = true;
    try {
      await apiFetch(`/admin/farms/${farmId}/logos`, { method: 'POST', body: formData });
      brandingMessage('Logo uploaded successfully', false);
      showToast('Logo uploaded', 'success');
      fileInput.value = '';
      fileNameSpan.textContent = 'No file chosen';
      fileNameSpan.title = '';
      if (typeof loadFarmLogos === 'function') loadFarmLogos(farmId);
      loadCard('branding');
    } catch (err) {
      const friendly = formatUserFriendlyError(err.message, 'logo');
      brandingMessage(friendly, true);
      showToast(friendly, 'error');
    } finally {
      uploadBtn.disabled = false;
    }
  });

  document.getElementById('admin-logo-reset')?.addEventListener('click', async () => {
    const farmId = getFarmId();
    try {
      await apiFetch(`/admin/farms/${farmId}/logos/reset`, { method: 'POST' });
      brandingMessage('Reverted farm branding to partner defaults', false);
      showToast('Branding reset', 'success');
      if (typeof loadFarmLogos === 'function') loadFarmLogos(farmId);
      loadCard('branding');
    } catch (err) {
      const friendly = formatUserFriendlyError(err.message, 'logo');
      brandingMessage(friendly, true);
      showToast(friendly, 'error');
    }
  });
}

async function renderAdmin(container) {
  const allowAdmin = typeof isAdmin === 'function' ? isAdmin() : Boolean(getUser()?.is_admin);
  if (!allowAdmin) {
    container.innerHTML = `<div class="alert alert-warning"><strong>Admin only.</strong> This page is restricted to administrators.</div>`;
    return;
  }
  currentEditingUser = null;
  const farmId = getFarmId();
  container.innerHTML = `
    <div class="admin-panel-root">
      <div class="module-header">
        <div class="admin-header-title-wrap">
          <div>
            <h2>Admin Panel</h2>
            <p>Manage data, forecasts, users, and branding for ${escapeHtml(typeof farmNameFor === 'function' ? farmNameFor(farmId) : farmId)}.</p>
          </div>
          <div style="display:flex;gap:8px;align-items:center;">
            <button type="button" class="btn btn-secondary btn-sm" id="admin-new-farm-btn">
              <i class="fa fa-plus" aria-hidden="true"></i> New farm
            </button>
            <button type="button" class="btn btn-danger btn-sm" id="admin-delete-farm-btn" title="Decommission the currently selected farm">
              <i class="fa fa-trash" aria-hidden="true"></i> Decommission farm
            </button>
          </div>
        </div>
        <div id="admin-status"><span class="data-mgmt-meta">Loading status…</span></div>
      </div>

      <div class="card admin-section-card">
        <div class="reports-card-header">
          <h3><i class="fa fa-upload" aria-hidden="true"></i> Upload data &amp; forecasts</h3>
          <button type="button" class="btn btn-secondary btn-sm" id="admin-forecast-btn" title="Recompute 182-day weight forecasts for this farm">
            <i class="fa fa-rotate" aria-hidden="true"></i> Refresh Forecasts
          </button>
        </div>
        <p class="data-mgmt-meta">Upload records (.csv, .xlsx) for this farm. Forecasts refresh automatically unless skipped.</p>
        <div class="admin-upload-controls">
          <input type="file" id="admin-upload-file" class="branding-file-input" accept=".csv,.xlsx">
          <label for="admin-upload-file" class="btn btn-secondary btn-sm" style="cursor: pointer;">Choose file (.csv, .xlsx)...</label>
          <span id="admin-upload-file-name" class="data-mgmt-meta admin-file-name">No file chosen</span>
          <select id="admin-duplicate-mode" aria-label="Duplicate handling" class="hidden">
            <option value="skip" selected>Skip duplicates</option>
            <option value="overwrite">Overwrite duplicates</option>
          </select>
          <div class="custom-picker admin-duplicate-picker" id="picker-admin-duplicate-mode"></div>
          <label class="pill-toggle">
            <input type="checkbox" id="admin-skip-forecast">
            <span class="pill-toggle-label">Skip forecast refresh</span>
          </label>
          <button type="button" class="btn btn-primary btn-sm" id="admin-upload-btn" style="margin-left: auto;"><i class="fa fa-upload" aria-hidden="true"></i> Upload</button>
        </div>
        <p class="data-mgmt-meta" id="admin-upload-result" role="status"></p>
        <p class="data-mgmt-meta" id="admin-forecast-result" role="status"></p>
        <div class="admin-subsection-divider"></div>
        <div id="admin-uploads"><div class="alert alert-muted">Loading…</div></div>
      </div>

      <div class="card admin-section-card" id="admin-snapshots-card">
        <div class="reports-card-header">
          <h3><i class="fa fa-clock-rotate-left" aria-hidden="true"></i> Snapshot history &amp; rollback</h3>
          <span class="badge-primary" id="admin-snapshots-count">0 snapshots</span>
        </div>
        <p class="data-mgmt-meta">Partition snapshots captured before uploads and rollbacks. Reverting restores the full farm partition atomically.</p>
        <div id="admin-snapshots"><div class="alert alert-muted">Loading snapshots…</div></div>
      </div>

      <div class="card admin-section-card" id="admin-columns-card">
        <div class="reports-card-header">
          <div>
            <h3><i class="fa fa-sliders" aria-hidden="true"></i> Dashboard variables &amp; filters</h3>
            <p class="data-mgmt-meta">Configure which columns appear as dropdown filters and production measures for this farm.</p>
          </div>
          <div style="display:flex;gap:8px;">
            <button type="button" class="btn btn-secondary btn-sm" id="btn-inspect-columns">
              <i class="fa fa-wand-magic-sparkles" aria-hidden="true"></i> Auto-detect suggestions
            </button>
            <button type="button" class="btn btn-primary btn-sm" id="btn-save-columns">
              <i class="fa fa-check" aria-hidden="true"></i> Save changes
            </button>
          </div>
        </div>
        <div id="admin-columns-status-strip"></div>
        <div id="admin-columns-table-wrap" class="table-wrap admin-table-scroll"></div>
        <p class="data-mgmt-meta" id="admin-columns-result" role="status"></p>
      </div>

      <div class="reports-grid">
        <div class="card reports-card">
          <div class="reports-card-header">
            <h3><i class="fa fa-users" aria-hidden="true"></i> Users</h3>
            <span class="badge-primary" id="admin-users-count">Loading…</span>
          </div>
          <div id="admin-users"><div class="alert alert-muted">Loading…</div></div>
        </div>

        <div class="card reports-card" id="admin-user-card">
          <div class="reports-card-header">
            <h3 id="user-card-heading"><i class="fa fa-user-plus" aria-hidden="true" id="user-card-icon"></i> <span id="user-card-title">Create User</span></h3>
            <div id="user-card-header-action">
              <span class="badge-primary" id="user-card-badge">Min 8 chars</span>
            </div>
          </div>
          <div class="grid-2">
            <div class="form-group">
              <label for="admin-user-username">Username</label>
              <input type="text" id="admin-user-username" class="form-control" placeholder="Username" maxlength="50" autocomplete="off">
            </div>
            <div class="form-group">
              <label for="admin-user-password" id="admin-user-password-label">Password</label>
              <input type="password" id="admin-user-password" class="form-control" placeholder="At least 8 characters" autocomplete="new-password">
            </div>
          </div>
          <div class="grid-2">
            <div class="form-group">
              <label>Role</label>
              <div class="custom-picker" id="picker-user-role"></div>
            </div>
            <div class="form-group">
              <label>Farm Access</label>
              <div class="custom-picker" id="picker-user-farms"></div>
            </div>
          </div>
          <div class="reports-card-actions">
            <button type="button" class="btn btn-primary" id="admin-user-submit-btn"><i class="fa fa-user-plus" aria-hidden="true"></i> <span id="admin-user-submit-label">Create User</span></button>
            <span class="data-mgmt-meta" id="admin-user-result" role="status"></span>
          </div>
        </div>
      </div>

      <div class="card admin-section-card">
        <div class="reports-card-header">
          <h3><i class="fa fa-images" aria-hidden="true"></i> Branding &amp; Logos</h3>
          <button type="button" class="btn btn-secondary btn-sm" id="admin-logo-reset" title="Revert to default partner logos">
            Reset to defaults
          </button>
        </div>
        <p class="data-mgmt-meta">Logos shown in the sidebar and reports. PNG, JPG, or SVG; deleting a partner default falls back to the system image.</p>
        <div id="admin-branding-gallery"><div class="alert alert-muted">Loading…</div></div>
        <div class="branding-controls">
          <input type="file" id="admin-logo-file" class="branding-file-input" accept=".png,.jpg,.jpeg,.svg">
          <label for="admin-logo-file" class="btn btn-secondary btn-sm" style="cursor: pointer;">Choose image...</label>
          <span id="admin-logo-file-name" class="data-mgmt-meta admin-file-name">No file chosen</span>
          <button type="button" class="btn btn-primary btn-sm" id="admin-logo-upload" disabled style="margin-left: auto;">Upload logo</button>
        </div>
        <p class="data-mgmt-meta" id="admin-branding-result" role="status"></p>
      </div>

      <div class="modal-overlay hidden" id="modal-new-farm" role="dialog" aria-modal="true" aria-labelledby="modal-new-farm-title">
        <div class="modal" style="max-width: 450px;">
          <div class="reports-card-header" style="margin-bottom: 16px;">
            <h3 id="modal-new-farm-title"><i class="fa fa-plus" aria-hidden="true"></i> Create new farm</h3>
            <button type="button" class="btn btn-secondary btn-sm" id="btn-close-new-farm-modal" aria-label="Close modal">&times;</button>
          </div>
          <form id="form-create-new-farm">
            <div class="form-group" style="margin-bottom: 12px;">
              <label for="new-farm-id">Farm ID <span style="color: var(--danger, #b91c1c);">*</span></label>
              <input type="text" id="new-farm-id" class="form-control" placeholder="e.g. WOW" required maxlength="50" style="text-transform: uppercase;">
            </div>
            <div class="form-group" style="margin-bottom: 12px;">
              <label for="new-farm-name">Farm name <span style="color: var(--danger, #b91c1c);">*</span></label>
              <input type="text" id="new-farm-name" class="form-control" placeholder="e.g. Wonga Farm" required maxlength="100">
            </div>
            <div class="form-group" style="margin-bottom: 16px;">
              <label for="new-farm-slug">Slug (URL path)</label>
              <input type="text" id="new-farm-slug" class="form-control" placeholder="e.g. wow" maxlength="100">
            </div>
            <p class="data-mgmt-meta" id="new-farm-error" style="color: var(--danger, #b91c1c); margin-bottom: 12px;"></p>
            <div class="modal-actions" style="display: flex; justify-content: flex-end; gap: 8px;">
              <button type="button" class="btn btn-secondary" id="btn-cancel-new-farm">Cancel</button>
              <button type="submit" class="btn btn-primary" id="btn-submit-new-farm">Create farm</button>
            </div>
          </form>
        </div>
      </div>

            <div class="modal-overlay hidden" id="modal-delete-farm" role="dialog" aria-modal="true" aria-labelledby="modal-delete-farm-title">
        <div class="modal" style="max-width: 480px;">
          <div class="reports-card-header" style="margin-bottom: 16px;">
            <h3 id="modal-delete-farm-title" style="color: var(--danger, #b91c1c);"><i class="fa fa-trash" aria-hidden="true"></i> Decommission Farm</h3>
            <button type="button" class="btn btn-secondary btn-sm" id="btn-close-delete-farm-modal" aria-label="Close modal">&times;</button>
          </div>
          <div style="margin-bottom: 16px;">
            <p style="margin-bottom: 12px; font-weight: 500;" id="delete-farm-confirm-text"></p>
            <div class="alert alert-warning" style="font-size: 12px; line-height: 1.5; margin-bottom: 12px;">
              <strong>Warning:</strong> Decommissioning permanently purges all records across 10 tables: animal telemetry, snapshot partition backups, 182-day ML forecasts, batch upload logs, logo assets, and variable configurations.
            </div>
            <div id="delete-farm-kf-guard" class="hidden" style="margin-bottom: 12px; background: rgba(185, 28, 28, 0.08); padding: 10px; border-radius: 6px; border: 1px solid rgba(185, 28, 28, 0.3);">
              <label style="display: flex; align-items: flex-start; gap: 8px; cursor: pointer; font-size: 12px; font-weight: 600; color: var(--danger, #b91c1c);">
                <input type="checkbox" id="delete-farm-force-cb" style="margin-top: 2px;">
                <span>Confirm forced deletion of primary baseline holding KF</span>
              </label>
            </div>
            <div style="margin-bottom: 12px;">
              <label for="delete-farm-confirm-input" id="delete-farm-confirm-label" style="font-size: 12px; font-weight: 600; display: block; margin-bottom: 6px;">
                To confirm, type <span id="delete-farm-expected-code" style="font-family: monospace; background: rgba(0,0,0,0.06); padding: 2px 6px; border-radius: 4px;"></span> below:
              </label>
              <input type="text" id="delete-farm-confirm-input" class="form-control" placeholder="" autocomplete="off" style="text-transform: uppercase;">
            </div>
            <p class="data-mgmt-meta" id="delete-farm-error" style="color: var(--danger, #b91c1c); margin-bottom: 12px;"></p>
          </div>
          <div class="modal-actions" style="display: flex; justify-content: flex-end; gap: 8px;">
            <button type="button" class="btn btn-secondary" id="btn-cancel-delete-farm">Cancel</button>
            <button type="button" class="btn btn-danger" id="btn-submit-delete-farm" disabled>Confirm Decommission</button>
          </div>
        </div>
      </div>

      <div class="modal-overlay hidden" id="modal-farm-columns" role="dialog" aria-modal="true" aria-labelledby="modal-columns-title">
        <div class="modal" style="max-width: 760px; border-radius: 8px;">
          <div class="reports-card-header">
            <div>
              <h3 id="modal-columns-title">Review dashboard variables</h3>
              <p class="data-mgmt-meta" id="modal-columns-desc">Review suggested filter dropdowns and production measures.</p>
            </div>
            <button type="button" class="btn btn-secondary btn-sm" id="btn-close-columns-modal" aria-label="Close dialog">&times;</button>
          </div>
          <div id="modal-columns-error" class="alert alert-danger hidden" style="margin: 8px 0; border-radius: 6px;"></div>
          <div id="modal-columns-body" class="table-wrap admin-table-scroll" style="max-height: 360px; margin: 12px 0;"></div>
          <div style="display: flex; justify-content: space-between; align-items: center; padding-top: 12px; border-top: 1px solid var(--color-border, #E2E8F0);">
            <span class="data-mgmt-meta" id="modal-columns-counter"></span>
            <div style="display: flex; gap: 8px;">
              <button type="button" class="btn btn-secondary btn-sm" id="btn-cancel-columns-modal">Dismiss (keep defaults)</button>
              <button type="button" class="btn btn-primary btn-sm" id="btn-confirm-columns-modal">Confirm &amp; apply</button>
            </div>
          </div>
        </div>
      </div>
    </div>`;

  const uploadFileInput = document.getElementById('admin-upload-file');
  const uploadFileName = document.getElementById('admin-upload-file-name');
  uploadFileInput?.addEventListener('change', () => {
    const file = uploadFileInput.files?.[0];
    if (uploadFileName) {
      uploadFileName.textContent = file ? file.name : 'No file chosen';
      uploadFileName.title = file ? file.name : '';
    }
  });

  if (typeof renderSingleSelectPicker === 'function') {
    renderSingleSelectPicker('picker-admin-duplicate-mode', 'admin_duplicate_mode', [
      { value: 'skip', label: 'Skip duplicates' },
      { value: 'overwrite', label: 'Overwrite duplicates' },
    ], 'skip', (val) => {
      const sel = document.getElementById('admin-duplicate-mode');
      if (sel) sel.value = val;
    }, false);
  } else {
    document.getElementById('admin-duplicate-mode')?.classList.remove('hidden');
  }

  loadAdminStatus();
  loadAdminUploads();
  loadAdminSnapshots();
  loadAdminColumns();
  wireColumnsCard();
  wireColumnsModal();
  wireCreateFarmModal();
  wireDeleteFarmModal();
  loadAdminUsers();
  wireUserForm();
  loadAdminBranding();
  wireAdminBranding();

  document.getElementById('admin-upload-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('admin-upload-btn');
    const resultEl = document.getElementById('admin-upload-result');
    const file = document.getElementById('admin-upload-file').files?.[0];
    if (!file) {
      showToast('Choose a CSV or Excel file first', 'error');
      return;
    }
    if (file.size > 50 * 1024 * 1024) {
      const tooBig = `File is ${(file.size / (1024 * 1024)).toFixed(1)} MB; the maximum upload size is 50 MB. Please split your records.`;
      resultEl.textContent = tooBig;
      showToast(tooBig, 'error');
      return;
    }
    const totalBytes = file.size;
    const setProgress = (done) => {
      const mb = (n) => (n / 1048576).toFixed(1);
      const pct = Math.min(100, Math.round((done / totalBytes) * 100));
      resultEl.textContent = done >= totalBytes
        ? 'All data sent — processing on server…'
        : `Uploading ${mb(done)} / ${mb(totalBytes)} MB (${pct}%)…`;
    };
    btn.disabled = true;
    setProgress(0);
    try {
    const wantForecast = !document.getElementById('admin-skip-forecast').checked;
    // Forecasts are a second request. A new farm's first upload used to build 182 days
    // of predictions inside the same call that stored the rows, and that call did not return.
    const fields = {
      filename: file.name,
      duplicate_mode: document.getElementById('admin-duplicate-mode').value,
      forecast: false,
    };
    let res;
    if (file.size > 4 * 1024 * 1024 && window.location.hostname.includes('vercel.app')) {
      // Vercel caps serverless bodies at 4.5 MB: stage 4 MB chunks, then finalize.
      res = await apiUploadLarge(`/admin/farms/${farmId}/upload`, file, fields, setProgress);
    } else {
      res = await apiUploadSingle(`/admin/farms/${farmId}/upload`, file, fields, setProgress);
    }
    const added = res.columns_added || [];
    const ignored = res.columns_ignored || [];
    const columnsNote = (added.length ? ` New columns added: ${added.join(', ')}.` : '')
      + (ignored.length ? ` Columns not kept (empty or unusable): ${ignored.join(', ')}.` : '');
    const saved = `Inserted ${res.rows_inserted ?? 0}, skipped ${res.rows_skipped ?? 0}, overwritten ${res.rows_overwritten ?? 0}.${columnsNote}`;
    resultEl.textContent = wantForecast ? `${saved} Refreshing forecasts…` : saved;
    loadCard('status');
    loadCard('uploads');
    loadCard('snapshots');
    loadCard('columns');
    if (wantForecast) {
      try {
        res.forecasts = await apiFetch(`/admin/farms/${farmId}/forecasts/refresh`, { method: 'POST' });
      } catch (err) {
        res.forecasts = { status: 'failed', detail: err.message };
      }
    }
    if (typeof refreshFarmFilterMetadata === 'function') {
      refreshFarmFilterMetadata(farmId);
    }

    if (res.initial_ingest) {
      apiFetch(`/admin/farms/${farmId}/columns`).then(colData => {
        openColumnsModal(
          colData.columns || [],
          'Initial ingest: review variables',
          'Review suggested filter dropdowns and production measures for the new farm.'
        );
      }).catch(err => {
        const friendly = formatUserFriendlyError(err.message, 'data');
        showToast(`Failed loading suggested columns: ${friendly}`, 'error');
      });
    } else if (res.new_columns_detected && res.new_columns_detected.length > 0) {
      apiFetch(`/admin/farms/${farmId}/columns`).then(colData => {
        const allCols = colData.columns || [];
        const newItems = allCols.filter(c => res.new_columns_detected.includes(c.column_name));
        openColumnsModal(
          newItems,
          'New columns detected in upload',
          `The uploaded dataset contains ${res.new_columns_detected.length} new column(s). Review their suggested settings below.`,
          async (updatedNewItems) => {
            const updatedMap = new Map(updatedNewItems.map(c => [c.column_name, c]));
            const merged = allCols.map(c => updatedMap.get(c.column_name) || c);
            await apiFetch(`/admin/farms/${farmId}/columns`, {
              method: 'PUT',
              body: JSON.stringify({ columns: merged }),
            });
          }
        );
      }).catch(err => {
        const friendly = formatUserFriendlyError(err.message, 'data');
        showToast(`Failed loading new columns: ${friendly}`, 'error');
      });
    }

    const done = `${saved} ${formatForecastOutcome(res.forecasts || {})}`;
    resultEl.textContent = done;
    showToast('Upload complete', 'success');
  } catch (err) {
    resultEl.textContent = err.message;
    showToast(err.message, 'error');
  } finally {
    btn.disabled = false;
  }
  });

  wireAction('admin-forecast-btn', 'admin-forecast-result', 'Refreshing…', 'Refresh failed', async () => {
    const res = await apiFetch(`/admin/farms/${farmId}/forecasts/refresh`, { method: 'POST' });
    return formatForecastOutcome(res);
  });
}

window.renderAdmin = renderAdmin;
