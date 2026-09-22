const API_BASE = '/api';

async function fetchStatus() {
  try {
    const response = await fetch(`${API_BASE}/status`);
    const data = await response.json();
    document.getElementById('active-transfers').textContent = data.active_transfers;
    document.getElementById('queue-size').textContent = data.queue_size;
    document.getElementById('processed-today').textContent = data.processed_today;
    document.getElementById('dashboard-version').textContent = `v${data.version}`;
    cockpitState.version = data.version;
    document.getElementById('connection-status').textContent = 'Connected';
    document.getElementById('connection-status').className = 'badge badge-accept';

    const tsEl = document.getElementById('totalsegmentator-status');
    if (tsEl) {
      if (data.totalsegmentator_installed) {
        tsEl.innerHTML = '<span class="badge badge-accept" style="font-size:0.85rem;">● Installed &amp; Ready</span>';
      } else {
        tsEl.innerHTML = '<span class="badge badge-reject" style="font-size:0.85rem;">○ Not Installed</span>';
      }
    }
  } catch (error) {
    console.error('Failed to fetch status:', error);
    document.getElementById('connection-status').textContent = 'Offline';
    document.getElementById('connection-status').className = 'badge badge-reject';
  }
}

// ── Study table rendering ─────────────────────────────────────────
// Tracks which 4DCT groups are expanded in the table
const expandedGroups = new Set();

async function fetchStudies() {
  try {
    const response = await fetch(`${API_BASE}/studies`);
    const items = await response.json();
    const tbody = document.getElementById('study-table-body');
    tbody.innerHTML = '';

    items.forEach(item => {
      if (item.type === '4dct_group') {
        _render4DCTGroup(tbody, item);
      } else {
        _renderSeriesRow(tbody, item);
      }
    });
  } catch (error) {
    console.error('Failed to fetch studies:', error);
  }
}

function _renderSeriesRow(tbody, study) {
  const tr = document.createElement('tr');
  tr.innerHTML = `
    <td style="font-weight: 600;">${study.patient_name}</td>
    <td style="color: var(--text-muted); font-size: 0.875rem;">${study.protocol || '—'}</td>
    <td style="font-family: monospace; font-size: 0.75rem;">${(study.series_uid || '').substring(0, 16)}...</td>
    <td>${study.instance_count}</td>
    <td><span class="badge badge-${(study.status || 'pending').toLowerCase()}">${study.status}</span></td>
    <td>
      <div class="actions-cell">
        <button class="view-btn" onclick="viewStudy('${study.series_uid}')">View Report</button>
        <button class="view-btn" style="background: var(--secondary);" onclick="launchCockpit('${study.series_uid}')">View Scan</button>
        <button class="btn-segment" onclick="runSegmentation('${study.series_uid}', null)" title="Run full TotalSegmentator segmentation">🫁 Segment Full</button>
      </div>
    </td>
  `;
  tbody.appendChild(tr);
}

function _render4DCTGroup(tbody, group) {
  const isExpanded = expandedGroups.has(group.group_id);
  const escapedGroupId = encodeURIComponent(group.group_id);

  // ── Header row ───────────────────────────────────────────────────
  const headerTr = document.createElement('tr');
  headerTr.classList.add('fourdct-header-row');
  headerTr.dataset.groupId = group.group_id;
  headerTr.innerHTML = `
    <td style="font-weight: 700;">
      <span style="color: var(--primary); margin-right: 0.4rem;">⊕</span>
      ${group.patient_name}
    </td>
    <td style="color: var(--text-muted); font-size: 0.875rem;">
      <span style="font-weight:600; color: var(--primary);">4DCT</span>
      ${group.series_description ? '· ' + group.series_description : ''}
    </td>
    <td style="font-family: monospace; font-size: 0.75rem;">${group.phase_count} phases</td>
    <td>${group.instance_count}</td>
    <td><span class="badge badge-${(group.status || 'pending').toLowerCase()}">${group.status}</span></td>
    <td>
      <div class="actions-cell">
        <button class="view-btn" style="background: var(--secondary);"
          onclick="toggle4DCTGroup(this, '${group.group_id}')">
          ${isExpanded ? '▲ Collapse' : `▼ ${group.phase_count} Phases`}
        </button>
        <button class="btn-segment"
          onclick="runSegmentation('${group.reference_phase_uid}', '${escapedGroupId}')"
          title="Run TotalSegmentator on reference phase (${group.reference_phase_uid.substring(0, 12)}…)">
          🫁 Segment 4D Ref
        </button>
      </div>
    </td>
  `;
  tbody.appendChild(headerTr);

  // ── Phase sub-rows (hidden by default) ───────────────────────────
  (group.phases || []).forEach((phase, idx) => {
    const phaseTr = document.createElement('tr');
    phaseTr.classList.add('fourdct-phase-row');
    phaseTr.dataset.parentGroup = group.group_id;
    phaseTr.style.display = isExpanded ? '' : 'none';
    const isRef = (phase.series_uid === group.reference_phase_uid);
    phaseTr.innerHTML = `
      <td style="padding-left: 2.5rem; color: var(--text-muted); font-size: 0.85rem;">
        ${isRef ? '<span title="Reference phase for segmentation" style="color:var(--primary);">★ </span>' : ''}
        ${phase.phase_label}
      </td>
      <td style="font-size: 0.8rem; color: var(--text-muted);">Temporal pos. ${phase.temporal_position}</td>
      <td style="font-family: monospace; font-size: 0.7rem;">${(phase.series_uid || '').substring(0, 16)}…</td>
      <td>${phase.instance_count}</td>
      <td><span class="badge badge-${(phase.status || 'pending').toLowerCase()}">${phase.status}</span></td>
      <td>
        <button class="view-btn" style="background: var(--secondary); font-size: 0.8rem;"
          onclick="launchCockpit('${phase.series_uid}')">View Scan</button>
      </td>
    `;
    tbody.appendChild(phaseTr);
  });
}

function toggle4DCTGroup(btn, groupId) {
  const isNowExpanded = !expandedGroups.has(groupId);
  if (isNowExpanded) {
    expandedGroups.add(groupId);
  } else {
    expandedGroups.delete(groupId);
  }

  // Show/hide phase rows
  document.querySelectorAll(`[data-parent-group="${CSS.escape(groupId)}"]`).forEach(row => {
    row.style.display = isNowExpanded ? '' : 'none';
  });

  btn.textContent = isNowExpanded ? '▲ Collapse' : '▼ Phases';
}

async function runSegmentation(seriesUid, encodedGroupId) {
  if (!seriesUid) { alert('No series selected for segmentation.'); return; }

  const msg = encodedGroupId
    ? `Run full TotalSegmentator segmentation (task=total) on the 4DCT reference phase?\n\n(Phase UID: ${seriesUid.substring(0, 20)}…)`
    : `Run full TotalSegmentator segmentation (task=total) on this series?`;

  if (!confirm(msg)) return;

  const url = encodedGroupId
    ? `${API_BASE}/studies/group/${decodeURIComponent(encodedGroupId)}/segment`
    : `${API_BASE}/viewer/${seriesUid}/segment`;

  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task: 'total', device: 'cpu', fast: true, force: false }),
    });
    const data = await res.json();
    if (res.ok) {
      alert(data.message || 'Segmentation started in background.');
    } else if (res.status === 503) {
      alert('⚠ TotalSegmentator is not installed.\n\nInstall it with:\n  pip install TotalSegmentator torch');
    } else {
      alert(`Segmentation request failed: ${data.detail || res.statusText}`);
    }
  } catch (err) {
    alert(`Network error: ${err}`);
  }
}

async function _updateCockpitSegmentationStatus(seriesUid) {
  const statusEl = document.getElementById('cockpit-segment-status');
  const btn = document.getElementById('cockpit-segment-btn');
  if (!statusEl || !btn) return;

  try {
    const res = await fetch(`${API_BASE}/viewer/${seriesUid}/segmentation`);
    if (res.ok) {
      const data = await res.json();
      if (data.available) {
        statusEl.innerHTML = '<span class="badge badge-accept">● Segmentation Available</span>';
        btn.textContent = '🔄 Re-run Full Segmentation';
      } else if (!data.totalsegmentator_installed) {
        statusEl.innerHTML = '<span style="color:var(--text-muted)">TotalSegmentator not installed</span>';
        btn.textContent = '🫁 Run Full Segmentation';
      } else {
        statusEl.textContent = 'Ready to segment';
        btn.textContent = '🫁 Run Full Segmentation';
      }
    }
  } catch (e) {
    statusEl.textContent = '';
  }
}

async function runCockpitSegmentation() {
  const seriesUid = cockpitState.seriesUid;
  if (!seriesUid) return;
  const statusEl = document.getElementById('cockpit-segment-status');
  const btn = document.getElementById('cockpit-segment-btn');
  if (btn) btn.disabled = true;
  if (statusEl) statusEl.textContent = 'Starting full segmentation in background...';

  try {
    const res = await fetch(`${API_BASE}/viewer/${seriesUid}/segment`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task: 'total', device: 'cpu', fast: true, force: false }),
    });
    const data = await res.json();
    if (res.ok) {
      if (statusEl) statusEl.innerHTML = '<span class="badge badge-ingesting">● Processing segmentation...</span>';
      setTimeout(() => _updateCockpitSegmentationStatus(seriesUid), 5000);
    } else {
      if (statusEl) statusEl.textContent = data.detail || 'Segmentation failed to start';
    }
  } catch (err) {
    if (statusEl) statusEl.textContent = `Error: ${err.message}`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function viewStudy(seriesUid) {
  try {
    const response = await fetch(`${API_BASE}/studies/${seriesUid}`);
    const result = await response.json();
    
    const modal = document.getElementById('modal');
    const title = document.getElementById('modal-title');
    const body = document.getElementById('modal-body');

    title.textContent = `QA Report: ${result.patient_name}`;
    
    let flagsHtml = result.flags.map(flag => `
      <div class="flag-item">
        <div class="flag-icon" style="background: var(--${flag.status.toLowerCase()})"></div>
        <div>
          <div style="font-weight: 600;">${flag.name}</div>
          <div style="font-size: 0.875rem; color: var(--text-muted);">${flag.message || ''}</div>
        </div>
      </div>
    `).join('');

    body.innerHTML = `
      <div style="margin-bottom: 2rem; display: flex; justify-content: space-between; align-items: center;">
        <span class="badge badge-${result.status.toLowerCase()}" style="font-size: 1.25rem; padding: 0.5rem 1.5rem;">
          ${result.status}
        </span>
        <div style="text-align: right; color: var(--text-muted); font-size: 0.875rem;">
          <div>Protocol: ${result.protocol}</div>
          <div>UID: ${seriesUid}</div>
        </div>
      </div>
      
      <div class="qa-report-grid">
        <div>
          <h3 style="margin-bottom: 1rem;">Specialist Metrics</h3>
          <p><strong>Truncation:</strong> ${result.metrics.truncation_detected ? 'DETECTED' : 'CLEAR'}</p>
          <p><strong>Bkg Air Noise:</strong> ${result.metrics.background_air_sd.toFixed(2)} HU</p>
          <p><strong>Fluid Density:</strong> ${result.metrics.fluid_median_hu.toFixed(1)} HU</p>
          <p><strong>Gas Volume:</strong> ${result.metrics.gas_volume_cc.toFixed(1)} cc</p>
          <p><strong>Patient Tilt:</strong> ${result.metrics.max_tilt_deg ? result.metrics.max_tilt_deg.toFixed(1) : '0.0'}°</p>
          <p><strong>Slices:</strong> ${result.metrics.slice_count}</p>
        </div>
        <div>
          <h3 style="margin-bottom: 1rem;">Agent Findings</h3>
          ${flagsHtml || '<p style="color: var(--text-muted);">No issues detected.</p>'}
        </div>
      </div>
      <div style="margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--border); display: flex; gap: 1rem;">
        <a href="${API_BASE}/reports/${seriesUid}/pdf" target="_blank" class="view-btn" style="background: var(--success); text-decoration: none;">Download PDF</a>
        <button class="view-btn" style="background: var(--secondary);" onclick="rerunQA('${seriesUid}')">Re-run Analysis</button>
      </div>
    `;

    modal.style.display = 'flex';
  } catch (error) {
    console.error('Failed to fetch study detail:', error);
    alert('Failed to load report. Please ensure the analysis is complete.');
  }
}

async function rerunQA(seriesUid) {
  try {
    const response = await fetch(`${API_BASE}/validate/${seriesUid}`, { method: 'POST' });
    const data = await response.json();
    alert(data.message);
    closeModal();
    fetchStudies();
  } catch (error) {
    console.error('Failed to re-run QA:', error);
  }
}

// ── Cockpit state ────────────────────────────────────────────────
const cockpitState = {
  seriesUid: null,
  sliceIndex: 0,
  sliceCount: 0,
  wl_presets: {},
  loadTimer: null,
  zoom: 1.0,
  version: '0.0',
};

async function launchCockpit(seriesUid) {
  cockpitState.seriesUid = seriesUid;
  cockpitState.sliceIndex = 0;
  cockpitState.zoom = 1.0;
  _applyCockpitZoom();

  const overlay = document.getElementById('cockpit-overlay');
  overlay.classList.add('open');
  document.getElementById('cockpit-version').textContent = `v${cockpitState.version}`;

  // Disable buttons while loading
  _setCockpitButtonsEnabled(false);

  try {
    const res = await fetch(`${API_BASE}/viewer/${seriesUid}/info`);
    if (!res.ok) throw new Error(await res.text());
    const info = await res.json();

    cockpitState.sliceCount = info.slice_count;
    cockpitState.wl_presets = info.wl_presets || {};
    cockpitState.sliceIndex = Math.floor(info.slice_count / 2);

    document.getElementById('cockpit-patient-name').textContent = info.patient_name;
    document.getElementById('cockpit-protocol').textContent = info.protocol;

    // Handle RTSS info
    const rtssSection = document.getElementById('cockpit-rtss-section');
    const refPtArea = document.getElementById('cockpit-ref-pt');
    if (info.has_rtss) {
      rtssSection.style.display = 'block';
      const refPtCoords = document.getElementById('cockpit-ref-pt-coords');
      if (info.reference_point) {
        const rp = info.reference_point;
        refPtCoords.innerHTML = `${rp.name || 'Point'}<br>X: ${rp.x.toFixed(1)}, Y: ${rp.y.toFixed(1)}, Z: ${rp.z.toFixed(1)}`;

        if (info.ref_point_slice_idx !== null && info.ref_point_slice_idx !== undefined) {
          refPtArea.classList.add('clickable');
          refPtArea.onclick = () => jumpToSlice(info.ref_point_slice_idx + 1);
        } else {
          refPtArea.classList.remove('clickable');
          refPtArea.onclick = null;
        }
      } else {
        refPtCoords.textContent = 'None detected';
        refPtArea.classList.remove('clickable');
        refPtArea.onclick = null;
      }
    } else {
      rtssSection.style.display = 'none';
    }

    // Configure nav slider
    const navSlider = document.getElementById('cockpit-nav-slider');
    navSlider.min = 0;
    navSlider.max = Math.max(0, info.slice_count - 1);
    navSlider.value = cockpitState.sliceIndex;

    // Populate W/L preset dropdown
    const select = document.getElementById('cockpit-wl-preset');
    select.innerHTML = '<option value="">Manual</option>';
    for (const [name, vals] of Object.entries(cockpitState.wl_presets)) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = name.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
      select.appendChild(opt);
    }

    // Render QA flags
    const flagsEl = document.getElementById('cockpit-flags');
    if (info.flags && info.flags.length > 0) {
      const colours = { REJECT: '#ef4444', FAIL_CRITICAL: '#ef4444', CONDITIONAL: '#f59e0b', PASS_WITH_WARNING: '#f59e0b', ACCEPT: '#10b981', PASS: '#10b981', SKIPPED: '#64748b' };
      flagsEl.innerHTML = info.flags.map(f => {
        // Detect slice indicators like "(Slice 5)" or "(Slices 10-15)"
        const match = f.message ? f.message.match(/\(Slices?\s+(\d+)/) : null;
        const clickable = match ? 'clickable' : '';
        const onclick = match ? `onclick="jumpToSlice(${match[1]})"` : '';

        return `
          <div class="cockpit-flag ${clickable}" ${onclick}>
            <div class="cockpit-flag-dot" style="background:${colours[f.status] || '#94a3b8'}"></div>
            <div>
              <div class="cockpit-flag-name">${f.name}</div>
              <div class="cockpit-flag-msg">${f.message || ''}</div>
            </div>
          </div>
        `;
      }).join('');
    } else {
      flagsEl.innerHTML = '<p style="font-size:0.8rem;color:var(--text-muted);">No issues detected.</p>';
    }

    _setCockpitButtonsEnabled(true);
    refreshCockpitSlice();
    _updateCockpitSegmentationStatus(seriesUid);
  } catch (err) {
    console.error('Cockpit load failed:', err);
    document.getElementById('cockpit-patient-name').textContent = 'Error loading series';
  }
}

function closeCockpit() {
  document.getElementById('cockpit-overlay').classList.remove('open');
  const img = document.getElementById('cockpit-image');
  if (img.src && img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
  img.src = '';
  img.style.transform = 'scale(1)';
  cockpitState.seriesUid = null;
  cockpitState.zoom = 1.0;
}

function jumpToSlice(sliceNum) {
  const idx = parseInt(sliceNum, 10) - 1;
  if (idx >= 0 && idx < cockpitState.sliceCount) {
    cockpitState.sliceIndex = idx;
    refreshCockpitSlice();
  }
}

function refreshCockpitSlice() {
  const { seriesUid, sliceIndex } = cockpitState;
  if (!seriesUid) return;

  const ww = document.getElementById('cockpit-ww').value;
  const wl = document.getElementById('cockpit-wl').value;
  const metal = document.getElementById('cockpit-metal-toggle').checked;
  const mask = document.getElementById('cockpit-mask-toggle').checked;

  // Update slice label
  document.getElementById('cockpit-slice-label').textContent =
    `Slice ${sliceIndex + 1} / ${cockpitState.sliceCount}`;

  // Sync nav slider
  document.getElementById('cockpit-nav-slider').value = sliceIndex;

  const url = `${API_BASE}/viewer/${seriesUid}/slice/${sliceIndex}?ww=${ww}&wl=${wl}&metal=${metal}&mask=${mask}`;
  const loading = document.getElementById('cockpit-loading');
  loading.classList.add('visible');

  const img = document.getElementById('cockpit-image');
  // Use a temporary Image to avoid flicker
  const tmp = new window.Image();
  tmp.onload = () => {
    if (img.src && img.src.startsWith('blob:')) URL.revokeObjectURL(img.src);
    img.src = tmp.src;
    loading.classList.remove('visible');
  };
  tmp.onerror = () => loading.classList.remove('visible');
  tmp.src = url;
}

function onCockpitNavSlider() {
  cockpitState.sliceIndex = parseInt(document.getElementById('cockpit-nav-slider').value, 10);
  refreshCockpitSlice();
}

function onCockpitWLChange() {
  document.getElementById('cockpit-wl-preset').value = '';
  document.getElementById('cockpit-ww-val').textContent = document.getElementById('cockpit-ww').value;
  document.getElementById('cockpit-wl-val').textContent = document.getElementById('cockpit-wl').value;
  _debouncedRefresh();
}

function applyCockpitPreset() {
  const name = document.getElementById('cockpit-wl-preset').value;
  if (!name || !cockpitState.wl_presets[name]) return;
  const { window_width, window_level } = cockpitState.wl_presets[name];
  document.getElementById('cockpit-ww').value = window_width;
  document.getElementById('cockpit-wl').value = window_level;
  document.getElementById('cockpit-ww-val').textContent = window_width;
  document.getElementById('cockpit-wl-val').textContent = window_level;
  refreshCockpitSlice();
}

function _debouncedRefresh() {
  clearTimeout(cockpitState.loadTimer);
  cockpitState.loadTimer = setTimeout(refreshCockpitSlice, 120);
}

function _setCockpitButtonsEnabled(enabled) {
  ['cockpit-approve-btn', 'cockpit-reject-btn'].forEach(id => {
    document.getElementById(id).disabled = !enabled;
  });
}

async function cockpitApprove() {
  const { seriesUid } = cockpitState;
  if (!seriesUid) return;
  _setCockpitButtonsEnabled(false);
  try {
    const res = await fetch(`${API_BASE}/viewer/${seriesUid}/approve`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      closeCockpit();
      fetchStudies();
    } else {
      alert(data.detail || 'Approval failed');
      _setCockpitButtonsEnabled(true);
    }
  } catch (err) {
    alert('Approval request failed');
    _setCockpitButtonsEnabled(true);
  }
}

async function cockpitReject() {
  const { seriesUid } = cockpitState;
  if (!seriesUid) return;

  if (!confirm("Are you sure you want to REJECT and PERMANENTLY DELETE this series and all its results?")) {
    return;
  }

  _setCockpitButtonsEnabled(false);
  try {
    const res = await fetch(`${API_BASE}/viewer/${seriesUid}/reject`, { method: 'POST' });
    const data = await res.json();
    if (res.ok) {
      closeCockpit();
      fetchStudies();
    } else {
      alert(data.detail || 'Rejection failed');
      _setCockpitButtonsEnabled(true);
    }
  } catch (err) {
    alert('Rejection request failed');
    _setCockpitButtonsEnabled(true);
  }
}

// ── Zoom ─────────────────────────────────────────────────────────
function zoomCockpit(delta) {
  cockpitState.zoom = Math.min(4.0, Math.max(0.25, cockpitState.zoom + delta));
  _applyCockpitZoom();
}

function resetCockpitZoom() {
  cockpitState.zoom = 1.0;
  _applyCockpitZoom();
}

function _applyCockpitZoom() {
  const img = document.getElementById('cockpit-image');
  img.style.transform = `scale(${cockpitState.zoom})`;
  img.style.transformOrigin = 'center center';
  document.getElementById('cockpit-zoom-label').textContent = `${Math.round(cockpitState.zoom * 100)}%`;
}

// Mouse-wheel: Ctrl+scroll = zoom, plain scroll = slice nav
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('cockpit-image-pane').addEventListener('wheel', e => {
    e.preventDefault();
    if (!cockpitState.seriesUid) return;
    if (e.ctrlKey || e.metaKey) {
      zoomCockpit(e.deltaY < 0 ? 0.1 : -0.1);
    } else {
      if (e.deltaY > 0) {
        cockpitState.sliceIndex = Math.min(cockpitState.sliceCount - 1, cockpitState.sliceIndex + 1);
      } else {
        cockpitState.sliceIndex = Math.max(0, cockpitState.sliceIndex - 1);
      }
      refreshCockpitSlice();
    }
  }, { passive: false });
});

function closeModal() {
  document.getElementById('modal').style.display = 'none';
}

// ── Logs Modal Functions ──────────────────────────────────────────
let logFetchTimer = null;

function openLogsModal() {
  document.getElementById('logs-modal').style.display = 'flex';
  fetchLogs();
}

function closeLogsModal() {
  document.getElementById('logs-modal').style.display = 'none';
}

function resetLogFilters() {
  document.getElementById('log-filter-date').value = '';
  document.getElementById('log-filter-status').value = 'ALL';
  document.getElementById('log-filter-issue').value = 'ALL';
  document.getElementById('log-filter-search').value = '';
  fetchLogs();
}

function debouncedFetchLogs() {
  clearTimeout(logFetchTimer);
  logFetchTimer = setTimeout(fetchLogs, 250);
}

function _buildLogQueryParams() {
  const date = document.getElementById('log-filter-date').value;
  const status = document.getElementById('log-filter-status').value;
  const issue = document.getElementById('log-filter-issue').value;
  const search = document.getElementById('log-filter-search').value;

  const params = new URLSearchParams();
  if (date) params.append('date', date);
  if (status && status !== 'ALL') params.append('status', status);
  if (issue && issue !== 'ALL') params.append('issue_type', issue);
  if (search && search.trim()) params.append('search', search.trim());

  return params.toString();
}

async function fetchLogs() {
  try {
    const query = _buildLogQueryParams();
    const url = `${API_BASE}/logs${query ? '?' + query : ''}`;
    const response = await fetch(url);
    const logs = await response.json();
    const tbody = document.getElementById('logs-table-body');
    tbody.innerHTML = '';

    if (!logs || logs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color: var(--text-muted); padding: 2rem;">No matching log records found.</td></tr>';
      return;
    }

    logs.forEach(log => {
      const tr = document.createElement('tr');
      const timeFormatted = log.timestamp ? log.timestamp.replace('T', ' ').substring(0, 19) : log.date || '';

      let issuesHtml = '';
      if (log.issues && log.issues.length > 0) {
        issuesHtml = log.issues.map(iss => `<div style="font-size: 0.8rem; margin-bottom: 0.2rem; color: #fca5a5;">• ${iss}</div>`).join('');
      } else {
        issuesHtml = '<span style="font-size: 0.8rem; color: var(--text-muted);">No QA flags / issues</span>';
      }

      tr.innerHTML = `
        <td style="font-size: 0.75rem; font-family: monospace; white-space: nowrap;">${timeFormatted}</td>
        <td style="font-weight: 600;">
          <div>${log.patient_name || 'Unknown'}</div>
          <div style="font-size: 0.75rem; color: var(--text-muted);">ID: ${log.patient_id || 'N/A'}</div>
        </td>
        <td style="color: var(--text-muted); font-size: 0.85rem;">${log.protocol || 'Unknown'}</td>
        <td><span class="badge badge-${(log.status || 'unknown').toLowerCase()}">${log.status || 'UNKNOWN'}</span></td>
        <td style="max-width: 350px;">${issuesHtml}</td>
      `;
      tbody.appendChild(tr);
    });
  } catch (error) {
    console.error('Failed to fetch logs:', error);
  }
}

function downloadLogs(format) {
  const query = _buildLogQueryParams();
  const params = new URLSearchParams(query);
  params.append('format', format);
  window.open(`${API_BASE}/logs/download?${params.toString()}`, '_blank');
}

// Initial fetch and polling
fetchStatus();
fetchStudies();
setInterval(fetchStatus, 5000);
setInterval(fetchStudies, 5000);
