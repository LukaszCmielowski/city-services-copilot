let channel = 'Mobile app';
let priority = 'Standard';
const $ = selector => document.querySelector(selector);

$('.drivers .eyebrow').textContent = 'MODEL-WIDE VALIDATION CHECK';
$('.drivers h3').textContent = 'What data helps the model most?';
$('.nav-links a[href="#model"]').textContent = 'How it works';
$('.nav-links a[href="README.md"]').href = '/setup';
$('.rag .eyebrow').textContent = 'AUTORAG · SERVICE GUIDANCE';
$('.rag h3').textContent = 'How should this request be handled?';
$('.question').textContent = '“How should an operator route an illegal-dumping report?”';
$('#question').value = 'How should an operator route an illegal-dumping report?';

function payload() {
  return { service: $('#service').value, neighborhood: $('#neighborhood').value, channel, priority };
}

async function post(url, data) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
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

function drawForecast(data) {
  $('#forecast-total').textContent = data.total;
  $('#chart').innerHTML = data.points.map(point => `<i style="height:${Math.round(point.value / 80 * 100)}%"></i>`).join('');
  $('#labels').innerHTML = data.points.map(point => `<span>${point.label}</span>`).join('');
}

function renderPrediction(data) {
  $('#risk').textContent = `${data.risk}%`;
  $('#days').textContent = `${data.days} days`;
  $('#risk-label').textContent = data.band.toUpperCase();
  $('#risk-meter').style.width = `${data.risk}%`;
  $('#drivers').innerHTML = data.drivers.length
    ? data.drivers.map(driver => `<div class="driver">${driver[0]}<b>${driver[1]}</b></div>`).join('')
    : '<div class="driver">Feature importance<b>Available after tabular deployment</b></div>';
  $('.results-title h2').textContent = `${$('#service').value} · ${$('#neighborhood').value}`;
}

function renderAnswer(data) {
  $('#answer').textContent = data.answer;
  $('#sources').innerHTML = data.sources.map(source => `<a class="source" href="${source.url}" target="_blank">${source.agency}: ${source.title}</a>`).join('');
}

async function refresh() {
  const button = $('#analyze');
  button.disabled = true;
  setUpdated('● Updating live scoring…');
  try {
    const [prediction, forecast] = await Promise.all([
      post('/api/predict', payload()),
      post('/api/forecast', payload()),
    ]);
    renderPrediction(prediction);
    drawForecast(forecast);
    setUpdated('● Updated now');
  } catch (error) {
    console.error(error);
    setUpdated(`Scoring unavailable: ${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

for (const id of ['channels', 'priorities']) {
  $(`#${id}`).addEventListener('click', event => {
    if (event.target.tagName !== 'BUTTON') return;
    [...event.currentTarget.children].forEach(button => button.classList.remove('selected'));
    event.target.classList.add('selected');
    if (id === 'channels') channel = event.target.textContent;
    else priority = event.target.textContent;
  });
}

$('#analyze').addEventListener('click', refresh);
$('#service').addEventListener('change', refresh);
$('#ask-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    renderAnswer(await post('/api/ask', { question: $('#question').value, ...payload() }));
  } catch (error) {
    console.error(error);
    $('#answer').textContent = `Guidance unavailable: ${error.message}`;
  }
});

refresh();
post('/api/ask', { question: $('#question').value }).then(renderAnswer).catch(error => {
  console.error(error);
  $('#answer').textContent = `Guidance unavailable: ${error.message}`;
});
