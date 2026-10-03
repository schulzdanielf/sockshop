const steps = Array.from(document.querySelectorAll('.step'));
const stepIndicator = document.getElementById('stepIndicator');
let currentStep = 1;
let currentRunId = null;
let pollTimer = null;
let telemetryRuns = [];
let telemetryRunId = null;
let telemetryBusy = false;

function showStep(step) {
  currentStep = Math.max(1, Math.min(6, step));
  steps.forEach((s) => s.classList.remove('active'));
  document.querySelector(`.step[data-step="${currentStep}"]`).classList.add('active');
  stepIndicator.textContent = `Step ${currentStep} of 6`;
  if (currentStep === 6) {
    document.getElementById('specPreview').value = JSON.stringify(buildSpec(), null, 2);
  }
}

function val(id) {
  return document.getElementById(id).value;
}

function num(id) {
  return Number(val(id));
}

function buildSpec() {
  return {
    schema_version: '1.0.0',
    experiment: {
      id: val('expId'),
      name: val('expName'),
      description_manual: val('expDescription'),
      hypothesis: val('expHypothesis'),
      tags: ['chaos', 'platform-v1']
    },
    scope: {
      environment: 'staging',
      namespace: val('chaosNamespace') || 'sock-shop'
    },
    timeline: {
      baseline_seconds: num('tBaseline'),
      warmup_seconds: num('tWarmup'),
      fault_duration_seconds: num('tFault'),
      post_recovery_observation_seconds: num('tPost'),
      sampling_interval_seconds: num('tSample')
    },
    load_profile: {
      provider: val('loadProvider'),
      command: val('loadCommand'),
      locust_file: val('loadFile'),
      host: val('loadHost'),
      users: num('loadUsers'),
      spawn_rate: num('loadSpawn'),
      grace_seconds: 30,
      extra_args: []
    },
    chaos_profile: {
      provider: val('chaosProvider'),
      namespace: val('chaosNamespace'),
      manifest_path: val('chaosManifest') || null,
      chaos_engine: val('chaosEngine') || null,
      chaos_result: val('chaosResult') || null
    },
    observability: {
      metrics: {
        provider: 'mcp-prometheus',
        mcp_sse_url: 'http://127.0.0.1:18080/sse',
        timeout_seconds: 10,
        queries: [
          { id: 'traffic', query: 'sum(rate(istio_requests_total{destination_workload_namespace="sock-shop",destination_workload!="unknown"}[5m])) by (destination_workload)' },
          { id: 'error_rate', query: '100 * (sum(rate(istio_requests_total{destination_workload_namespace="sock-shop",destination_workload!="unknown",response_code=~"5.."}[5m])) by (destination_workload) or on(destination_workload) 0 * sum(rate(istio_requests_total{destination_workload_namespace="sock-shop",destination_workload!="unknown"}[5m])) by (destination_workload)) / clamp_min(sum(rate(istio_requests_total{destination_workload_namespace="sock-shop",destination_workload!="unknown"}[5m])) by (destination_workload), 0.0001)' },
          { id: 'latency_p95', query: 'histogram_quantile(0.95, sum(rate(istio_request_duration_milliseconds_bucket{destination_workload_namespace="sock-shop",destination_workload!="unknown"}[5m])) by (le,destination_workload))' }
        ]
      },
      traces: {
        provider: 'mcp-tempo',
        mcp_sse_url: 'http://127.0.0.1:18080/sse',
        timeout_seconds: 20,
        query: '{ status = error }',
        service_name: 'catalogue',
        limit: 10,
        use_llm: true,
        max_new_tokens: 256
      }
    },
    analysis: {
      slo: {
        error_rate_threshold: num('sloErrorRate'),
        latency_p95_threshold_ms: num('sloP95'),
        recovery_tolerance_pct: 0.20,
        max_recovery_seconds: 600
      },
      classification: {
        resilient_rule: 'no_slo_violation_or_recovery<=120s',
        degraded_recoverable_rule: 'slo_violation_and_recovery<=600s',
        degraded_persistent_rule: 'recovery>600s_or_not_recovered'
      }
    },
    governance: {
      requires_approval: val('requiresApproval') === 'true',
      risk_level: val('riskLevel')
    }
  };
}

async function api(path, options = {}) {
  const role = (val('userRole') || 'operator').trim();
  const defaultHeaders = {
    'Content-Type': 'application/json',
    'X-User-Role': role
  };
  const res = await fetch(path, {
    headers: {
      ...defaultHeaders,
      ...(options.headers || {})
    },
    ...options
  });
  if (!res.ok) {
    throw new Error(await res.text());
  }
  return res.json();
}

async function saveExperiment() {
  const body = {
    initiated_by: val('initiatedBy') || 'operator',
    spec: buildSpec()
  };
  const result = await api('/api/experiments', {
    method: 'POST',
    body: JSON.stringify(body)
  });
  alert(`Saved ${result.experiment_id} v${result.version}`);
}

async function startRun() {
  const spec = buildSpec();
  await saveExperiment();
  const result = await api('/api/runs/start', {
    method: 'POST',
    body: JSON.stringify({
      experiment_id: spec.experiment.id,
      initiated_by: val('initiatedBy') || 'operator'
    })
  });
  currentRunId = result.run_id;
  document.getElementById('runIdInput').value = currentRunId;
  await loadRun();
  loadHistory();
}

async function loadRun() {
  const runId = document.getElementById('runIdInput').value.trim();
  if (!runId) return;
  currentRunId = runId;

  const run = await api(`/api/runs/${runId}`);
  document.getElementById('runStatus').textContent = JSON.stringify(run, null, 2);

  const events = await api(`/api/runs/${runId}/events`);
  document.getElementById('eventsBox').textContent = events.map((e) => `${e.ts} | ${e.event_type} | ${JSON.stringify(e.payload)}`).join('\n');

  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const liveRun = await api(`/api/runs/${runId}`);
    document.getElementById('runStatus').textContent = JSON.stringify(liveRun, null, 2);

    const liveEvents = await api(`/api/runs/${runId}/events`);
    document.getElementById('eventsBox').textContent = liveEvents.map((e) => `${e.ts} | ${e.event_type} | ${JSON.stringify(e.payload)}`).join('\n');

    if (['completed', 'failed', 'stopped'].includes(liveRun.status)) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }, 3000);
}

async function stopRun() {
  if (!currentRunId) return;
  await api(`/api/runs/${currentRunId}/stop`, {
    method: 'POST',
    body: JSON.stringify({ requested_by: val('initiatedBy') || 'operator', reason: 'manual_stop' })
  });
  await loadRun();
}

async function compareRuns() {
  const a = val('compareA').trim();
  const b = val('compareB').trim();
  if (!a || !b) return;
  const data = await api(`/api/runs/compare/${a}/${b}`);
  document.getElementById('compareBox').textContent = JSON.stringify(data, null, 2);
}

async function saveManualConclusion() {
  if (!currentRunId) return;
  const conclusion = val('manualConclusion').trim();
  if (!conclusion) return;
  await api(`/api/runs/${currentRunId}/manual-conclusion`, {
    method: 'POST',
    body: JSON.stringify({ conclusion })
  });
  await loadRun();
}

document.getElementById('prevStepBtn').addEventListener('click', () => {
  showStep(currentStep - 1);
});
document.getElementById('nextStepBtn').addEventListener('click', () => {
  showStep(currentStep + 1);
  if (currentStep === 3 && chaosCatalog.length === 0) loadCatalog(false);
});
document.getElementById('saveExperimentBtn').addEventListener('click', saveExperiment);
document.getElementById('startRunBtn').addEventListener('click', startRun);
document.getElementById('loadRunBtn').addEventListener('click', loadRun);
document.getElementById('stopRunBtn').addEventListener('click', stopRun);
document.getElementById('compareBtn').addEventListener('click', compareRuns);
document.getElementById('saveConclusionBtn').addEventListener('click', saveManualConclusion);

// ── Chaos Catalog ────────────────────────────────────────────────────────────
let chaosCatalog = [];

function renderCatalog(items) {
  const container = document.getElementById('catalogList');
  if (!items.length) {
    container.innerHTML = '<span style="color:#555;font-size:0.82rem;">Nenhum engine encontrado.</span>';
    return;
  }
  container.innerHTML = items.map((item, idx) => {
    const tags = (item.experiment_types || []).map(
      (t) => `<span class="exp-tag">${t}</span>`
    ).join(' ');
    const wf = item.workflow_name ? `<b>${item.workflow_name}</b>` : item.engine_name;
    const meta = `${item.engine_namespace} · ${item.app_namespace || '?'} · ${item.app_label || '?'}`;
    return `
      <div class="catalog-item" data-idx="${idx}" onclick="selectCatalogItem(${idx})">
        <div style="flex:1;min-width:0;">
          <span style="color:#e0e0e0;">${wf}</span>
          <span style="color:#555;margin:0 4px;">›</span>
          ${tags}
          <div class="meta">${meta}</div>
        </div>
        <span style="color:#444;font-size:0.7rem;">${item.engine_state || ''}</span>
      </div>`;
  }).join('');
}

function selectCatalogItem(idx) {
  const item = chaosCatalog[idx];
  if (!item) return;

  document.querySelectorAll('.catalog-item').forEach((el) => el.classList.remove('selected'));
  const el = document.querySelector(`.catalog-item[data-idx="${idx}"]`);
  if (el) el.classList.add('selected');

  // Auto-fill chaos config fields
  document.getElementById('chaosNamespace').value = item.app_namespace || 'sock-shop';
  // chaos_engine stores the Litmus workflow template name used by inject()
  document.getElementById('chaosEngine').value = item.workflow_name || '';
  document.getElementById('chaosManifest').value = '';
  // chaosResult not needed for Argo Workflow mode (resolved at runtime)
  document.getElementById('chaosResult').value = '';
}

async function loadCatalog(sync) {
  const statusEl = document.getElementById('catalogStatus');
  statusEl.textContent = sync ? 'Sincronizando…' : 'Carregando…';
  try {
    const url = sync ? '/api/providers/chaos/catalog?sync=true' : '/api/providers/chaos/catalog';
    chaosCatalog = await api(url);
    statusEl.textContent = `${chaosCatalog.length} engine(s) encontrado(s)`;
    renderCatalog(chaosCatalog);
  } catch (err) {
    statusEl.textContent = `Erro: ${err.message}`;
  }
}

document.getElementById('loadCatalogBtn').addEventListener('click', () => loadCatalog(true));
document.getElementById('refreshHistoryBtn').addEventListener('click', loadHistory);
document.getElementById('historyDisagreementOnly')?.addEventListener('change', loadHistory);

// ── MCP telemetry chat ──────────────────────────────────────────────────────
function addChatMessage(role, text, meta = '') {
  const container = document.getElementById('telemetryChatMessages');
  container.querySelector('.chat-empty')?.remove();
  const item = document.createElement('div');
  item.className = `chat-message ${role}`;
  const label = role === 'user' ? 'Você' : 'MCP / Qwen';
  item.innerHTML = `<div class="chat-message-label">${label}</div><div class="chat-message-body"></div>${meta ? `<div class="chat-message-meta">${meta}</div>` : ''}`;
  item.querySelector('.chat-message-body').textContent = text;
  container.appendChild(item);
  container.scrollTop = container.scrollHeight;
}

function formatDuration(seconds) {
  const totalSeconds = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(totalSeconds / 60);
  const remainingSeconds = totalSeconds % 60;
  return minutes ? `${minutes} min ${remainingSeconds} s` : `${remainingSeconds} s`;
}

function formatTelemetryWindow(result) {
  const { start, end } = result.window;
  const durationSeconds = (Date.parse(end) - Date.parse(start)) / 1000;
  const scopeLabel = result.window_scope === 'run' ? 'run completo' : 'fase da falha';
  const details = [
    `MCP consultou ${scopeLabel} (${formatDuration(durationSeconds)}): ${start} → ${end}.`
  ];
  if (result.baseline_window) {
    details.push(`Baseline de comparação: ${result.baseline_window.start} → ${result.baseline_window.end}.`);
  }
  const fault = result.phases?.fault;
  if (fault) {
    const phaseSeconds = (Date.parse(fault.end) - Date.parse(fault.start)) / 1000;
    const source = result.phase_sources?.fault;
    const sourceLabels = {
      manifest_observed: 'timestamps observados no manifest',
      litmus_result_created_at_plus_configured_duration: 'estimativa: criação do ChaosResult + duração configurada',
      planned_phase_fallback: 'fase planejada, sem confirmação da injeção real'
    };
    details.push(`Fase fault registrada: ${formatDuration(phaseSeconds)} (${sourceLabels[source] || source || 'origem desconhecida'}).`);
  }
  if (result.configured_chaos_duration_seconds != null) {
    const configuredDuration = formatDuration(result.configured_chaos_duration_seconds);
    if (result.fault_injection_duration_seconds == null) {
      details.push(`Duração configurada do caos: ${configuredDuration}; duração real não confirmada pelo backend.`);
    } else {
      details.push(`Duração configurada do caos: ${configuredDuration}; duração registrada: ${formatDuration(result.fault_injection_duration_seconds)}.`);
    }
  }
  return details.join(' ');
}

function setTelemetryRun(runId) {
  telemetryRunId = runId || null;
  const input = document.getElementById('telemetryChatInput');
  const send = document.getElementById('telemetryChatSendBtn');
  const rca = document.getElementById('telemetryRcaBtn');
  input.disabled = !telemetryRunId || telemetryBusy;
  send.disabled = !telemetryRunId || telemetryBusy;
  rca.disabled = !telemetryRunId || telemetryBusy;
  const run = telemetryRuns.find((item) => item.run_id === telemetryRunId);
  document.getElementById('chatWindowBadge').textContent = run
    ? `${run.run_id} · ${run.status}`
    : 'Nenhum run selecionado';
}

async function loadTelemetryRuns() {
  const select = document.getElementById('telemetryRunSelect');
  telemetryRuns = (await api('/api/runs')).filter((run) => ['completed', 'failed', 'stopped'].includes(run.status));
  renderTelemetryRuns();
}

function renderTelemetryRuns() {
  const select = document.getElementById('telemetryRunSelect');
  const filter = document.getElementById('telemetryRunFilter').value.trim().toLowerCase();
  const matchingRuns = telemetryRuns.filter((run) =>
    `${run.run_id} ${run.experiment_id} ${run.status}`.toLowerCase().includes(filter)
  );
  const previousRunId = telemetryRunId;
  const selectedRunMatches = matchingRuns.some((run) => run.run_id === previousRunId);
  const nextRunId = selectedRunMatches
    ? previousRunId
    : filter && matchingRuns.length === 1
      ? matchingRuns[0].run_id
      : '';
  document.getElementById('telemetryRunFilterCount').textContent = filter
    ? `${matchingRuns.length} de ${telemetryRuns.length} runs`
    : `${telemetryRuns.length} runs disponíveis`;
  select.innerHTML = `<option value="">${matchingRuns.length ? 'Selecione um run concluído' : 'Nenhum run correspondente'}</option>` + matchingRuns.map((run) =>
    `<option value="${run.run_id}">${run.experiment_id} · ${run.run_id} · ${run.status}</option>`
  ).join('');

  select.value = nextRunId;
  setTelemetryRun(select.value);
  if (previousRunId && previousRunId !== select.value) {
    document.getElementById('telemetryChatMessages').innerHTML =
      '<div class="chat-empty">Selecione um run visível no filtro para continuar.</div>';
  }
}

document.getElementById('telemetryRunSelect').addEventListener('change', (event) => {
  setTelemetryRun(event.target.value);
  if (event.target.value) {
    document.getElementById('telemetryChatMessages').innerHTML = '<div class="chat-empty">Janela carregada. Faça uma pergunta sobre este run.</div>';
  }
});
document.getElementById('telemetryRunFilter').addEventListener('input', renderTelemetryRuns);
document.getElementById('refreshTelemetryRunsBtn').addEventListener('click', loadTelemetryRuns);
document.querySelectorAll('[data-chat-question]').forEach((button) => button.addEventListener('click', () => {
  const input = document.getElementById('telemetryChatInput');
  input.value = button.dataset.chatQuestion;
  input.focus();
}));
document.getElementById('telemetryChatForm').addEventListener('submit', async (event) => {
  event.preventDefault();
  const input = document.getElementById('telemetryChatInput');
  const question = input.value.trim();
  if (!telemetryRunId || !question) return;
  addChatMessage('user', question);
  input.value = '';
  telemetryBusy = true;
  setTelemetryRun(telemetryRunId);
  try {
    const result = await api(`/api/runs/${telemetryRunId}/telemetry-chat`, {
      method: 'POST',
      body: JSON.stringify({
        query: question,
        use_model: true,
        window_scope: document.getElementById('telemetryWindowScope').value
      })
    });
    let windowDetails = result;
    if (!result.phases) {
      try {
        const observationWindow = await api(
          `/api/runs/${telemetryRunId}/observation-window`
        );
        windowDetails = { ...result, ...observationWindow };
      } catch {
        // Keep the answer usable if optional window details are unavailable.
      }
    }
    if (windowDetails.configured_chaos_duration_seconds == null) {
      const selectedRun = telemetryRuns.find((run) => run.run_id === telemetryRunId);
      if (selectedRun?.experiment_id) {
        try {
          const experiment = await api(
            `/api/experiments/${encodeURIComponent(selectedRun.experiment_id)}`
          );
          windowDetails.configured_chaos_duration_seconds =
            experiment.spec?.chaos_profile?.fault_duration_seconds;
        } catch {
          // The answer and observed window remain useful without experiment metadata.
        }
      }
    }
    addChatMessage('assistant', result.answer, formatTelemetryWindow(windowDetails));
  } catch (err) {
    addChatMessage('error', `Não foi possível consultar a telemetria: ${err.message}`);
  } finally {
    telemetryBusy = false;
    setTelemetryRun(telemetryRunId);
  }
});

document.getElementById('telemetryRcaBtn').addEventListener('click', async () => {
  if (!telemetryRunId || telemetryBusy) return;
  const button = document.getElementById('telemetryRcaBtn');
  const originalLabel = button.textContent;
  addChatMessage('user', 'Investigue a causa raiz deste run.');
  telemetryBusy = true;
  setTelemetryRun(telemetryRunId);
  button.textContent = 'Analisando…';
  try {
    const result = await api(`/api/runs/${telemetryRunId}/rca`, {
      method: 'POST',
      body: JSON.stringify({})
    });
    const tools = (result.tools_used || []).join(', ') || 'nenhuma tool';
    addChatMessage(
      'assistant',
      result.answer,
      `RCA · ${result.iterations} iterações · ${tools} · ${result.stop_reason}`
    );
  } catch (err) {
    addChatMessage('error', `Não foi possível investigar a causa raiz: ${err.message}`);
  } finally {
    telemetryBusy = false;
    button.textContent = originalLabel;
    setTelemetryRun(telemetryRunId);
  }
});
loadTelemetryRuns().catch(() => {});

// ── Run History ──────────────────────────────────────────────────────────────
const VERDICT_LABELS = ['resilient', 'degraded_recoverable', 'degraded_persistent', 'unsure'];

function verdictBadge(v) {
  if (!v) return '<span class="badge badge-none">—</span>';
  return `<span class="badge badge-${v}" title="${v}">${v.replace('degraded_', 'deg.')}</span>`;
}

async function loadHistory() {
  const el = document.getElementById('historyList');
  const disagreementOnly = document.getElementById('historyDisagreementOnly')?.checked;
  try {
    const [runs, verdicts] = await Promise.all([
      api('/api/runs'),
      api(`/api/verdicts?limit=500${disagreementOnly ? '&disagreement_only=true' : ''}`)
    ]);
    const vMap = Object.fromEntries(verdicts.map((v) => [v.run_id, v]));
    let rows = runs;
    if (disagreementOnly) {
      const allowed = new Set(verdicts.map((v) => v.run_id));
      rows = runs.filter((r) => allowed.has(r.run_id));
    }
    if (!rows.length) {
      el.innerHTML = '<span style="color:var(--muted);font-size:0.85rem;">Nenhum run encontrado.</span>';
      return;
    }
    el.innerHTML = `
      <table class="history-table">
        <thead><tr>
          <th>Experiment ID</th>
          <th>Run ID</th>
          <th>Status</th>
          <th title="Veredito heurístico">Heur.</th>
          <th title="Veredito do LLM">🤖 LLM</th>
          <th title="Rótulo do operador">👤 Op.</th>
          <th>Início</th>
          <th></th>
        </tr></thead>
        <tbody>
          ${rows.map((r) => {
            const v = vMap[r.run_id] || {};
            const disagreeMark = v.disagreement ? '<span title="LLM/heurística/operador divergem" style="color:#e87b35;margin-left:4px;">⚠</span>' : '';
            const llmConf = (v.llm_confidence != null) ? ` (${Math.round(v.llm_confidence * 100)}%)` : '';
            const labelMenu = VERDICT_LABELS.map((lbl) => `<option value="${lbl}"${v.operator_label === lbl ? ' selected' : ''}>${lbl}</option>`).join('');
            return `
            <tr>
              <td>${r.experiment_id}</td>
              <td>${r.run_id}</td>
              <td><span class="badge badge-${r.status}">${r.status}</span></td>
              <td>${verdictBadge(r.verdict)}${disagreeMark}</td>
              <td>${verdictBadge(v.llm_verdict)}<span style="color:var(--muted);font-size:0.75rem;">${llmConf}</span></td>
              <td>
                <select class="op-label-select" data-run-id="${r.run_id}" style="font-size:0.78rem;">
                  <option value="">—</option>${labelMenu}
                </select>
              </td>
              <td>${r.started_at ? r.started_at.slice(0, 19).replace('T', ' ') : '—'}</td>
              <td>
                <button class="btn-sm" onclick="reRun('${r.experiment_id}')">▶</button>
                <button class="btn-sm" style="margin-left:4px;background:var(--accent-2);" onclick="openRun('${r.run_id}')">👁</button>
                <button class="btn-sm" style="margin-left:4px;" onclick="runLlmAnalysis('${r.run_id}')" title="Gerar veredito do LLM">🤖</button>
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>`;
    el.querySelectorAll('.op-label-select').forEach((sel) => {
      sel.addEventListener('change', (ev) => saveOperatorLabel(ev.target.dataset.runId, ev.target.value));
    });
  } catch (err) {
    el.innerHTML = `<span style="color:red;font-size:0.85rem;">Erro: ${err.message}</span>`;
  }
}

async function saveOperatorLabel(runId, label) {
  if (!label) return;
  try {
    await api(`/api/runs/${runId}/operator-label`, {
      method: 'POST',
      body: JSON.stringify({ label, by: val('initiatedBy') || 'operator' })
    });
    await loadHistory();
  } catch (err) {
    alert(`Erro ao salvar rótulo: ${err.message}`);
  }
}

async function runLlmAnalysis(runId) {
  if (!confirm(`Gerar veredito do LLM para ${runId}? Pode levar alguns segundos.`)) return;
  try {
    const res = await api(`/api/runs/${runId}/llm-analysis?force=true`, { method: 'POST' });
    const a = res.analysis || {};
    alert(`LLM verdict: ${a.verdict || '—'}\nConfiança: ${a.confidence ?? '—'}\nCitações: ${(a.citations || []).join(', ')}\n\n${a.reasoning || ''}`);
    await loadHistory();
  } catch (err) {
    alert(`Erro no LLM: ${err.message}`);
  }
}

async function reRun(experimentId) {
  if (!confirm(`Re-executar experimento "${experimentId}" com os mesmos parâmetros?`)) return;
  try {
    const result = await api('/api/runs/start', {
      method: 'POST',
      body: JSON.stringify({
        experiment_id: experimentId,
        initiated_by: val('initiatedBy') || 'operator'
      })
    });
    currentRunId = result.run_id;
    document.getElementById('runIdInput').value = currentRunId;
    await loadRun();
    await loadHistory();
    document.querySelector('.monitor').scrollIntoView({ behavior: 'smooth' });
  } catch (err) {
    alert(`Erro ao re-executar: ${err.message}`);
  }
}

function openRun(runId) {
  currentRunId = runId;
  document.getElementById('runIdInput').value = runId;
  loadRun();
  document.querySelector('.monitor').scrollIntoView({ behavior: 'smooth' });
}

loadHistory();
showStep(1);
