const $ = selector => document.querySelector(selector);
const sampleRequests = JSON.parse($('#sample-requests').textContent);
let selectedRequest = null;
let analysisVersion = 0;
let lastAnalysis = null;
let briefAbort = null;
function payload() {
  const sample = sampleRequests.find(request => request.id === selectedRequest);
  if (!sample) return null;
  const { id, service, neighborhood, channel, priority } = sample;
  return { id, service, neighborhood, channel, priority };
}

async function post(url, data, signal) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
    signal,
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || `Request failed (HTTP ${response.status})`);
  return result;
}

function setUpdated(message, isError = false) {
  const updated = $('.updated');
  if (!updated) return;
  updated.textContent = message;
  updated.style.color = isError ? '#b3452b' : '';
}

function describeForecast(data, request = payload()) {
  const who = `${request.service} · ${request.neighborhood}`;
  if (data.source === 'sample') return `${who}. Illustrative total over seven days.`;
  return `${who}. Forecast total over seven days.`;
}

function drawForecast(data, request = payload()) {
  $('#forecast-total').textContent = data.total;
  $('#forecast-note').textContent = describeForecast(data, request);
  const maximum = Math.max(1, ...data.points.map(point => point.value));
  $('#chart').replaceChildren(...data.points.map((point, index) => {
    const bar = document.createElement('i');
    bar.style.height = `${Math.max(0, Math.round(point.value / maximum * 100))}%`;
    bar.title = `Day ${index + 1}: ${point.value} requests`;
    bar.setAttribute('role', 'img');
    bar.setAttribute('aria-label', bar.title);
    bar.setAttribute('tabindex', '0');
    return bar;
  }));
  $('#labels').replaceChildren(...data.points.map((point, index) => {
    const span = document.createElement('span');
    span.textContent = `Day ${index + 1}`;
    return span;
  }));
}

function setRiskTone(tone) {
  const card = document.querySelector('article.risk');
  card.classList.remove('miss', 'within');
  if (tone) card.classList.add(tone);
}

function renderPrediction(data) {
  const missed = data.outcome === 'sla_miss' || (!data.outcome && data.risk >= 50);
  const model = data.mode === 'model' || data.outcome === 'sla_miss' || data.outcome === 'within_sla';
  setRiskTone(missed ? 'miss' : 'within');
  const hasRisk = Number.isFinite(data.risk);
  $('#risk').classList.toggle('class-label', model && !hasRisk);
  $('#risk-label').textContent = 'PREDICTED';
  if (model && !hasRisk) {
    $('#risk').textContent = missed ? 'May miss' : 'Likely within';
    $('#days').textContent = 'Service target';
    $('#days-caption').textContent = Number.isFinite(data.sla_days) ? `${data.sla_days} days` : 'Target unavailable';
    $('#risk-meter').style.width = '0%';
    $('#risk-note').textContent = 'Classifier prediction. A probability was not returned.';
  } else if (hasRisk) {
    $('#risk').textContent = `${data.risk}%`;
    $('#days').textContent = missed ? 'May miss target' : 'Likely within target';
    $('#days-caption').textContent = Number.isFinite(data.sla_days) ? `Demo target: ${data.sla_days} days` : 'Service target unavailable';
    $('#risk-meter').style.width = `${data.risk}%`;
    $('#risk-note').textContent = data.mode === 'sample' ? 'Illustrative chance of missing the service target.' : 'Live model estimate: chance of missing the service target.';
  } else {
    $('#risk').textContent = '—';
    $('#days').textContent = '—';
    $('#days-caption').textContent = '';
    $('#risk-meter').style.width = '0%';
    $('#risk-note').textContent = 'Scoring response did not include a class or a probability.';
  }
  const drivers = Array.isArray(data.drivers) ? data.drivers : [];
  $('#drivers').innerHTML = drivers.length
    ? drivers.map(driver => `<div class="driver">${driver[0]}<b>${driver[1]}</b></div>`).join('')
    : '<div class="driver">Feature importance<b>Not returned for this model</b></div>';
}

function clearRisk(message) {
  setRiskTone('');
  $('#risk').textContent = '—';
  $('#risk').classList.remove('class-label');
  $('#days').textContent = '—';
  $('#days-caption').textContent = '';
  $('#risk-label').textContent = '';
  $('#risk-meter').style.width = '0%';
  $('#risk-note').textContent = message;
  $('#drivers').replaceChildren();
}

function clearForecast(message) {
  $('#forecast-total').textContent = '—';
  $('#forecast-note').textContent = message;
  $('#chart').replaceChildren();
  $('#labels').replaceChildren();
}

let guidanceAbort = null;

function resetGuidance() {
  if (guidanceAbort) guidanceAbort.abort();
  if (briefAbort) briefAbort.abort();
  $('.rag').classList.remove('preparing');
  $('#resident-draft').value = '';
  $('#resident-draft').disabled = false;
  $('#copy-response').disabled = true;
  $('#copy-status').textContent = '';
  $('#brief-action-note').hidden = true;
  $('#generate-brief').innerHTML = 'Generate handling brief <span>→</span>';
  $('#ask-form button').disabled = !lastAnalysis;
  $('#question').disabled = !lastAnalysis;
  $('#generate-brief').disabled = !lastAnalysis;
  $('#handling-brief').hidden = true;
  $('#brief-sources').replaceChildren();
  $('#brief-status').textContent = lastAnalysis ? 'Ready to prepare guidance for this request.' : 'Select a report from the queue to prepare a response.';
  $('#answer').textContent = 'Ask how this request should be handled.';
  $('#sources').replaceChildren();
  $('.grounded').textContent = 'Service policy for the selected request';
}

function sourceElements(sources) {
  return sources.filter(source => /^https?:\/\//i.test(source.url || '')).map(source => {
    const link = document.createElement('a');
    link.className = 'source';
    link.textContent = `${source.agency}: ${source.title}`;
    link.href = source.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    return link;
  });
}

function renderAnswer(data) {
  $('#answer').textContent = data.answer;
  $('#sources').replaceChildren(...sourceElements(data.sources));
  const labels = {
    citations: '● Agent citations · review before use',
    references: '● Agent references · verify before use',
    sample: '● Bundled sample guidance',
    none: '● No references returned · verify before use',
  };
  $('.grounded').textContent = labels[data.grounding] || labels.none;
}

function renderDecisionSummary({ prediction }) {
  if (!prediction) {
    $('#decision-summary').textContent = 'Risk is unavailable. You can still review service guidance and prepare a resident response.';
    return;
  }
  const higherRisk = prediction.outcome ? prediction.outcome === 'sla_miss' : prediction.risk >= 50;
  const review = higherRisk ?
    'Review for possible service delay.' : 'Use the routine service review workflow.';
  const source = prediction.mode === 'sample' ? 'Illustrative review: ' : '';
  $('#decision-summary').textContent = `${source}${review} Confirm the handling steps and prepare a resident response below.`;
}

function renderQueue() {
  const ranked = [...sampleRequests].sort((a, b) =>
    (Number.isFinite(b.prediction?.risk) ? b.prediction.risk : -1) -
    (Number.isFinite(a.prediction?.risk) ? a.prediction.risk : -1));
  $('#queue-rows').replaceChildren(...ranked.map(request => {
    const row = document.createElement('tr');
    row.classList.toggle('active', request.id === selectedRequest);
    const risk = Number.isFinite(request.prediction?.risk) ? `${request.prediction.risk}%` :
      request.error ? 'Unavailable' : request.prediction ? 'Class only' : '—';
    for (const text of [request.service, risk]) {
      const cell = document.createElement('td');
      cell.textContent = text;
      if (text === request.service) {
        const meta = document.createElement('small');
        meta.className = 'request-meta';
        meta.textContent = `${request.id} · ${request.neighborhood} · Priority: ${request.priority}`;
        cell.append(meta);
      }
      row.append(cell);
    }
    const cell = document.createElement('td');
    const button = document.createElement('button');
    button.className = 'review-request';
    button.textContent = request.id === selectedRequest ? 'Selected' : 'Review';
    button.setAttribute('aria-label', `Review ${request.id}: ${request.service} in ${request.neighborhood}`);
    button.setAttribute('aria-pressed', String(request.id === selectedRequest));
    button.addEventListener('click', () => selectSampleRequest(request.id));
    cell.append(button);
    row.append(cell);
    return row;
  }));
}

async function scoreQueue() {
  const button = $('#score-queue');
  button.disabled = true;
  button.textContent = 'Scoring five requests…';
  $('#queue-status').textContent = 'Calculating SLA-miss risk for each sample report…';
  const results = await Promise.allSettled(sampleRequests.map(request => post('/api/predict', request)));
  results.forEach((result, index) => {
    sampleRequests[index].prediction = result.status === 'fulfilled' ? result.value : null;
    sampleRequests[index].error = result.status === 'rejected' ? result.reason?.message || 'Scoring request failed' : null;
  });
  const scored = sampleRequests.filter(request => Number.isFinite(request.prediction?.risk));
  const live = scored.length && scored.every(request => request.prediction.mode === 'model');
  const errors = [...new Set(sampleRequests.map(request => request.error).filter(Boolean))];
  const problem = errors.some(error => /HTTP 401|HTTP 403/.test(error)) ?
    'Model access was rejected. Refresh the scoring connection credentials and retry.' :
    errors.join('; ');
  $('#queue-status').textContent = scored.length ?
    `${scored.length} of 5 ranked · ${live ? 'Live model' : 'Illustrative sample'} estimates. Select a request.${problem ? ` Unavailable reports: ${problem}` : ''}` :
    errors.length ? `Queue scoring unavailable. ${problem}` :
    'The model returned class labels without probabilities. Review a report individually.';

  if (selectedRequest && lastAnalysis) {
    setUpdated('Queue refreshed. Select this request again to update its outlook.');
  }
  renderQueue();
  button.disabled = false;
  button.textContent = 'Rescore sample requests';
}

function selectSampleRequest(id) {
  const request = sampleRequests.find(item => item.id === id);
  if (!request) return;
  selectedRequest = id;
  $('#selected-request').textContent = `${id} · sample report`;
  $('#request-facts').textContent = `Intake: ${request.channel} · Assigned priority: ${request.priority}`;
  $('#question').value = 'What information should the resident provide for this service?';
  renderQueue();
  const pending = refresh(request.prediction);
  if (typeof window !== 'undefined' && window.matchMedia('(max-width: 720px)').matches) {
    $('.results').scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' });
  }
  $('#request-title').focus?.({ preventScroll: true });
  return pending;
}

function setCardLoading(selector, loading) {
  const card = $(selector);
  card.classList.toggle('loading', loading);
  card.setAttribute('aria-busy', String(loading));
}

async function refresh(cachedPrediction = null) {
  const request = payload();
  if (!request) return;
  const version = ++analysisVersion;
  lastAnalysis = null;
  resetGuidance();
  clearRisk('Estimating this request’s chance of missing its target…');
  clearForecast('Forecasting matching demand…');
  lastAnalysis = { request, prediction: null, forecast: null };
  $('#brief-status').textContent = 'Preparing this request’s outlook…';
  $('.results-title h2').textContent = `${request.service} · ${request.neighborhood}`;
  $('#decision-summary').textContent = 'Preparing the review…';
  const results = $('.results');
  results.classList.add('scoring');
  setCardLoading('article.risk', !cachedPrediction);
  setCardLoading('article.forecast', true);
  setUpdated(cachedPrediction ? 'Risk ready · Loading demand forecast…' : 'Loading risk and demand forecast…');
  $('#retry-analysis').hidden = true;

  function showRisk(prediction, error) {
    lastAnalysis.prediction = prediction;
    if (prediction) renderPrediction(prediction);
    else clearRisk(`Risk unavailable: ${error?.message || 'No prediction returned'}`);
    renderDecisionSummary(lastAnalysis);
    $('#generate-brief').disabled = false;
    $('#ask-form button').disabled = false;
    $('#question').disabled = false;
    $('#brief-status').textContent = 'Ready to prepare guidance for this request.';
    setCardLoading('article.risk', false);
  }

  // A queue prediction is already available: show it before starting any network work.
  if (cachedPrediction) showRisk(cachedPrediction);
  const riskTask = cachedPrediction ? Promise.resolve() : post('/api/predict', request).then(
    prediction => {
      if (version !== analysisVersion) return;
      showRisk(prediction);
      if ($('article.forecast').classList.contains('loading')) setUpdated('Risk ready · Loading demand forecast…');
    },
    error => {
      if (version !== analysisVersion) return;
      showRisk(null, error);
      setUpdated('Risk unavailable · Loading demand forecast…', true);
    },
  );
  const forecastTask = post('/api/forecast', request).then(
    forecast => {
      if (version !== analysisVersion) return;
      lastAnalysis.forecast = forecast;
      drawForecast(forecast, request);
      if ($('article.risk').classList.contains('loading')) setUpdated('Demand ready · Loading risk estimate…');
    },
    error => {
      if (version !== analysisVersion) return;
      clearForecast(`Demand unavailable: ${error.message || 'No forecast returned'}`);
      setUpdated('Demand unavailable · Loading risk estimate…', true);
    },
  ).finally(() => {
    if (version === analysisVersion) setCardLoading('article.forecast', false);
  });
  await Promise.allSettled([riskTask, forecastTask]);
  if (version !== analysisVersion) return;
  const failures = [!lastAnalysis.prediction && 'Risk unavailable', !lastAnalysis.forecast && 'Demand unavailable'].filter(Boolean);
  setUpdated(failures.length ? failures.join(' · ') : 'Outlook ready', failures.length > 0);
  $('#retry-analysis').hidden = !failures.length;
  results.classList.remove('scoring');
  renderQueue();
}

$('#score-queue').addEventListener('click', scoreQueue);
$('#retry-analysis').addEventListener('click', () => refresh());
$('#generate-brief').disabled = true;
$('#question').disabled = true;
$('#ask-form button').disabled = true;
$('#generate-brief').addEventListener('click', async () => {
  if (!lastAnalysis) return;
  if (briefAbort) briefAbort.abort();
  const controller = new AbortController();
  briefAbort = controller;
  const version = analysisVersion;
  const button = $('#generate-brief');
  const hadBrief = !$('#handling-brief').hidden && Boolean($('#resident-draft').value);
  button.disabled = true;
  $('.rag').classList.add('preparing');
  $('#resident-draft').disabled = true;
  $('#copy-response').disabled = true;
  $('#copy-status').textContent = '';
  $('#brief-status').textContent = 'Retrieving guidance and preparing a response draft…';
  try {
    const result = await post('/api/brief', lastAnalysis.request, controller.signal);
    if (controller.signal.aborted || version !== analysisVersion) return;
    $('#handling-steps').replaceChildren(...result.handling_steps.map(step => {
      const item = document.createElement('li');
      item.textContent = step;
      return item;
    }));
    $('#resident-draft').value = result.resident_reply;
    $('#copy-response').disabled = false;
    $('#brief-sources').replaceChildren(...sourceElements(result.sources));
    const labels = {
      citations: 'Prepared with agent citations. Review the supporting sources before use.',
      references: 'Prepared with agent references. Verify the linked guidance before use.',
      sample: 'Prepared from bundled sample guidance.',
      none: 'No references returned. Verify this draft before use.',
    };
    $('#brief-status').textContent = labels[result.grounding] || labels.none;
    $('#handling-brief').hidden = false;
    $('#brief-action-note').hidden = false;
    button.innerHTML = 'Regenerate brief <span>→</span>';
  } catch (error) {
    if (error.name === 'AbortError' || version !== analysisVersion) return;
    $('#brief-status').textContent = `Handling brief unavailable: ${error.message}${hadBrief ? '. Your previous brief and edits are preserved.' : '. Retry or ask about service policy below.'}`;
  } finally {
    if (briefAbort === controller) {
      briefAbort = null;
      $('.rag').classList.remove('preparing');
      $('#resident-draft').disabled = false;
      $('#copy-response').disabled = !$('#resident-draft').value.trim();
      button.disabled = !lastAnalysis;
    }
  }
});

async function copyResidentResponse() {
  const draft = $('#resident-draft');
  const text = draft.value;
  if (!text.trim()) return;
  const version = analysisVersion;
  try {
    await navigator.clipboard.writeText(text);
    if (version === analysisVersion && draft.value === text) $('#copy-status').textContent = 'Response copied. Paste into your service system when ready.';
  } catch (error) {
    if (version !== analysisVersion || draft.value !== text) return;
    draft.focus();
    draft.select();
    $('#copy-status').textContent = 'Copy is unavailable. The draft is selected so you can copy it manually.';
  }
}

$('#copy-response').addEventListener('click', copyResidentResponse);
$('#resident-draft').addEventListener('input', () => {
  $('#copy-response').disabled = !$('#resident-draft').value.trim();
  $('#copy-status').textContent = 'Draft edited. Copy when ready.';
});
$('#ask-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (!lastAnalysis) return;
  if (guidanceAbort) guidanceAbort.abort();
  const controller = new AbortController();
  guidanceAbort = controller;
  const button = $('#ask-form button');
  button.disabled = true;
  $('#answer').textContent = 'Retrieving service guidance…';
  $('.grounded').textContent = 'Preparing an answer';
  $('#sources').replaceChildren();
  try {
    const result = await post('/api/ask', { question: $('#question').value, ...lastAnalysis.request }, controller.signal);
    if (!controller.signal.aborted) renderAnswer(result);
  } catch (error) {
    if (controller.signal.aborted) return;
    $('#answer').textContent = `Guidance unavailable: ${error.message}`;
    $('.grounded').textContent = 'Guidance unavailable';
  } finally {
    if (guidanceAbort === controller) {
      button.disabled = !lastAnalysis;
      guidanceAbort = null;
    }
  }
});
renderQueue();
