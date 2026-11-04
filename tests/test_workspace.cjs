// Exercise the workspace flow and overlapping responses without a browser dependency.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(text = '') {
    this.textContent = text;
    this.innerHTML = text;
    this.children = [];
    this.style = {};
    this.attributes = {};
    this.events = {};
    const names = new Set();
    this.classList = {
      add: (...values) => values.forEach(value => names.add(value)),
      remove: (...values) => values.forEach(value => names.delete(value)),
      toggle: (value, enabled) => enabled ? names.add(value) : names.delete(value),
      contains: value => names.has(value),
    };
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, handler) { this.events[name] = handler; }
  scrollIntoView() {}
  focus() { this.focused = true; }
  select() { this.selected = true; }
}
const response = data => ({ok: true, headers: {get: () => 'application/json'}, json: async () => data});
function workspace(fetch, clipboard = {writeText: async () => {}}) {
  const nodes = new Map();
  const get = selector => {
    if (!nodes.has(selector)) nodes.set(selector, new Element());
    return nodes.get(selector);
  };
  get('#sample-requests').textContent = fs.readFileSync('data/sample-requests.json', 'utf8');
  const context = vm.createContext({document: {querySelector: get, createElement: () => new Element()}, fetch,
    navigator: {clipboard}, AbortController, TextDecoder, setTimeout, console: {error() {}}});
  vm.runInContext(fs.readFileSync('app.js', 'utf8'), context);
  return {context, get, read: code => vm.runInContext(code, context)};
}
const forecast = {total: 140, source: 'model', points: Array.from({length: 7}, () => ({value: 20}))};

test('queue sorts probabilities including 0%, with failed calls left unranked', async () => {
  const risks = [0, 85, 20, 99, null];
  let index = 0;
  const app = workspace(async () => {
    const risk = risks[index++];
    if (risk === null) throw Error('Endpoint unavailable');
    return response({risk, mode: 'model'});
  });
  await app.context.scoreQueue();
  const rows = app.get('#queue-rows').children;
  assert.match(rows[0].children[0].textContent, /Streetlight outage/);
  assert.equal(rows[3].children[1].textContent, '0%');
  assert.equal(rows[4].children[1].textContent, 'Unavailable');
  assert.match(app.get('#queue-status').textContent, /4 of 5.*Live model/);
});

test('selecting a scored row reuses its prediction and forecasts the selected group', async () => {
  const calls = [];
  const app = workspace(async (url, options) => { calls.push([url, JSON.parse(options.body)]); return response(forecast); });
  app.read('sampleRequests[3].prediction = {risk: 91, mode: "model", outcome: "sla_miss", sla_days: 6}');
  await app.context.selectSampleRequest('DEMO-104');
  assert.equal(app.read('payload().service'), 'Streetlight outage');
  assert.equal(app.read('payload().neighborhood'), 'West Philadelphia');
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/api/forecast');
  assert.equal(calls[0][1].priority, 'High');
  assert.equal(app.get('#risk').textContent, '91%');
  assert.match(app.get('#decision-summary').textContent, /Review for possible service delay/);
  assert.match(app.get('#selected-request').textContent, /DEMO-104/);
  assert.match(app.get('#request-facts').textContent, /Phone.*High/);
  assert.equal(app.get('#generate-brief').disabled, false);
});

test('changing a request clears the brief and ignores older scoring responses', async () => {
  const pending = [];
  const app = workspace((url, options) => new Promise(resolve => pending.push({url, resolve, request: JSON.parse(options.body)})));
  const first = app.context.selectSampleRequest('DEMO-101');
  const second = app.context.selectSampleRequest('DEMO-102');
  assert.equal(app.get('#generate-brief').disabled, true);
  assert.equal(app.get('#handling-brief').hidden, true);
  assert.equal(app.get('#decision-summary').textContent, 'Preparing the review…');
  assert.match(app.get('.results-title h2').textContent, /Pothole/);
  pending[2].resolve(response({risk: 10, mode: 'model', outcome: 'within_sla'}));
  pending[3].resolve(response(forecast));
  await second;
  pending[0].resolve(response({risk: 90, mode: 'model', outcome: 'sla_miss'}));
  pending[1].resolve(response(forecast));
  await first;
  assert.equal(app.get('#risk').textContent, '10%');
  assert.equal(app.read('lastAnalysis.request.service'), 'Pothole');
  assert.match(app.get('.results-title h2').textContent, /Pothole/);
});

test('brief uses the selected request and reports absent sources without claiming grounding', async () => {
  const calls = [];
  const app = workspace(async (url, options) => {
    calls.push([url, JSON.parse(options.body)]);
    if (url === '/api/forecast') return response(forecast);
    return response({handling_steps: ['Confirm the location.'], resident_reply: 'Please share the nearest address.', sources: [], grounding: 'none'});
  });
  app.read('sampleRequests[0].prediction = {risk: 75, mode: "model", outcome: "sla_miss", sla_days: 4}');
  await app.context.selectSampleRequest('DEMO-101');
  await app.get('#generate-brief').events.click();
  assert.equal(calls[1][0], '/api/brief');
  assert.equal(calls[1][1].service, 'Illegal dumping');
  assert.equal(calls[1][1].risk, undefined);
  assert.equal(app.get('#handling-brief').hidden, false);
  assert.equal(app.get('#resident-draft').value, 'Please share the nearest address.');
  assert.match(app.get('#brief-status').textContent, /No references returned/);
  app.read('sampleRequests[1].prediction = {risk: 20, mode: "model"}');
  await app.context.selectSampleRequest('DEMO-102');
  assert.equal(app.get('#handling-brief').hidden, true);
});

test('a failed forecast preserves risk and keeps policy guidance available', async () => {
  const app = workspace(async () => { throw Error('Forecast offline'); });
  app.read('selectedRequest = "DEMO-101"');
  await app.context.refresh({risk: 75, mode: 'model'});
  assert.equal(app.get('#risk').textContent, '75%');
  assert.equal(app.get('#generate-brief').disabled, false);
  assert.equal(app.get('#forecast-total').textContent, '—');
  assert.match(app.get('#forecast-note').textContent, /Forecast offline/);
  assert.equal(app.get('#retry-analysis').hidden, false);
});

test('a late handling brief cannot replace guidance for a newly selected report', async () => {
  let finishBrief;
  const app = workspace(async url => url === '/api/forecast' ? response(forecast) :
    new Promise(resolve => { finishBrief = resolve; }));
  app.read('sampleRequests[0].prediction = {risk: 75, mode: "model", outcome: "sla_miss"}');
  await app.context.selectSampleRequest('DEMO-101');
  const pending = app.get('#generate-brief').events.click();
  app.read('sampleRequests[1].prediction = {risk: 20, mode: "model"}');
  await app.context.selectSampleRequest('DEMO-102');
  finishBrief(response({handling_steps: ['Old advice'], resident_reply: 'Old reply', sources: [], grounding: 'none'}));
  await pending;
  assert.equal(app.get('#handling-brief').hidden, true);
  assert.equal(app.get('#generate-brief').disabled, false);
  assert.match(app.get('#brief-status').textContent, /Ready to prepare/);
});

test('a late follow-up answer cannot overwrite the newly selected request', async () => {
  let finishAnswer;
  const app = workspace(url => url === '/api/forecast' ? Promise.resolve(response(forecast)) : new Promise(resolve => { finishAnswer = resolve; }));
  app.read('sampleRequests[0].prediction = sampleRequests[1].prediction = {risk: 20, mode: "model"}');
  await app.context.selectSampleRequest('DEMO-101');
  const pending = app.get('#ask-form').events.submit({preventDefault() {}});
  await app.context.selectSampleRequest('DEMO-102');
  finishAnswer(response({answer: 'Old request answer', sources: [], grounding: 'none'}));
  await pending;
  assert.equal(app.get('#answer').textContent, 'Ask how this request should be handled.');
});


test('copy response uses the edited draft rather than the original generated text', async () => {
  const copied = [];
  const app = workspace(async () => response(forecast), {writeText: async text => copied.push(text)});
  app.get('#resident-draft').value = 'An edited reply for the resident.';
  app.get('#resident-draft').events.input();
  assert.equal(app.get('#copy-response').disabled, false);
  await app.get('#copy-response').events.click();
  assert.deepEqual(copied, ['An edited reply for the resident.']);
  assert.match(app.get('#copy-status').textContent, /Response copied.*Paste into your service system/);
  app.read('sampleRequests[0].prediction = {risk: 20, mode: "model"}');
  await app.context.selectSampleRequest('DEMO-101');
  assert.equal(app.get('#resident-draft').value, '');
  assert.equal(app.get('#copy-response').disabled, true);
});

test('clipboard denial selects the draft for manual copying', async () => {
  const app = workspace(async () => response(forecast), {writeText: async () => { throw Error('denied'); }});
  app.get('#resident-draft').value = 'Please provide a location.';
  await app.context.copyResidentResponse();
  assert.equal(app.get('#resident-draft').selected, true);
  assert.match(app.get('#copy-status').textContent, /copy it manually/);
  app.get('#resident-draft').value = '  ';
  app.get('#resident-draft').events.input();
  assert.equal(app.get('#copy-response').disabled, true);
});

test('follow-up source status does not replace the handling brief source status', async () => {
  const app = workspace(async () => response(forecast));
  app.get('#brief-status').textContent = 'Prepared with source links returned by the agent.';
  app.context.renderAnswer({answer: 'A follow-up answer.', sources: [], grounding: 'none'});
  assert.match(app.get('.grounded').textContent, /No references returned/);
  assert.equal(app.get('#brief-status').textContent, 'Prepared with source links returned by the agent.');
});


test('a risk failure preserves demand and offers guidance with an honest review message', async () => {
  const app = workspace(async url => {
    if (url === '/api/predict') throw Error('Risk offline');
    return response(forecast);
  });
  app.read('selectedRequest = "DEMO-101"');
  await app.context.refresh();
  assert.equal(app.get('#risk').textContent, '—');
  assert.equal(app.get('#forecast-total').textContent, 140);
  assert.equal(app.get('#generate-brief').disabled, false);
  assert.match(app.get('#decision-summary').textContent, /Risk is unavailable.*service guidance/);
  assert.match(app.get('#risk-note').textContent, /Risk offline/);
});

test('predicted misses are not displayed as confirmed outcomes, including sample estimates', () => {
  const app = workspace(async () => response(forecast));
  app.context.renderPrediction({mode: 'model', risk: 75, outcome: 'sla_miss', sla_days: 4});
  assert.equal(app.get('#days').textContent, 'May miss target');
  assert.equal(app.get('#risk-label').textContent, 'PREDICTED');
  app.context.renderPrediction({mode: 'sample', risk: 31, sla_days: 4});
  assert.equal(app.get('#days').textContent, 'Likely within target');
  assert.match(app.get('#days-caption').textContent, /4 days/);
  assert.match(app.get('#risk-note').textContent, /Illustrative/);
});

test('a failed regeneration preserves the edited draft and its evidence', async () => {
  const app = workspace(async url => url === '/api/forecast' ? response(forecast) :
    {ok: false, json: async () => ({error: 'Agent offline'})});
  app.read('sampleRequests[0].prediction = {risk: 75, mode: "model"}');
  await app.context.selectSampleRequest('DEMO-101');
  app.get('#handling-brief').hidden = false;
  app.get('#resident-draft').value = 'Edited response to keep.';
  const source = new Element('Existing source');
  app.get('#brief-sources').replaceChildren(source);
  await app.get('#generate-brief').events.click();
  assert.equal(app.get('#resident-draft').value, 'Edited response to keep.');
  assert.equal(app.get('#handling-brief').hidden, false);
  assert.equal(app.get('#brief-sources').children[0], source);
  assert.equal(app.get('#copy-response').disabled, false);
  assert.equal(app.get('#resident-draft').disabled, false);
  assert.match(app.get('#brief-status').textContent, /preserved/);
});

test('source links use safe HTTP URLs and cannot execute script URLs', () => {
  const app = workspace(async () => response(forecast));
  const links = app.context.sourceElements([
    {agency: 'City', title: 'Service guidance', url: 'https://city.example/guidance'},
    {agency: 'Unknown', title: 'Unsafe link', url: 'javascript:alert(1)'},
    {agency: 'Unknown', title: 'Missing URL'},
  ]);
  assert.equal(links.length, 1);
  assert.equal(links[0].textContent, 'City: Service guidance');
  assert.equal(links[0].href, 'https://city.example/guidance');
  assert.equal(links[0].rel, 'noopener noreferrer');
});

test('all scoring failures still allow policy guidance with an explicit retry path', async () => {
  const app = workspace(async () => { throw Error('Scoring offline'); });
  app.read('selectedRequest = "DEMO-101"');
  await app.context.refresh();
  assert.equal(app.get('#risk').textContent, '—');
  assert.equal(app.get('#forecast-total').textContent, '—');
  assert.equal(app.get('#generate-brief').disabled, false);
  assert.equal(app.get('#question').disabled, false);
  assert.equal(app.get('#retry-analysis').hidden, false);
  assert.match(app.get('#decision-summary').textContent, /Risk is unavailable/);
});

test('refreshing the queue does not silently overwrite the selected outlook or edited draft', async () => {
  const app = workspace(async url => url === '/api/forecast' ? response(forecast) : response({risk: 88, mode: 'model'}));
  app.read('sampleRequests[0].prediction = {risk: 75, mode: "model", outcome: "sla_miss"}');
  await app.context.selectSampleRequest('DEMO-101');
  app.get('#resident-draft').value = 'Edited reply to preserve.';
  await app.context.scoreQueue();
  assert.equal(app.get('#risk').textContent, '75%');
  assert.equal(app.get('#resident-draft').value, 'Edited reply to preserve.');
  assert.match(app.get('.updated').textContent, /Queue refreshed.*again/);
});

test('rounded probability does not override an explicit predicted class in the review action', () => {
  const app = workspace(async () => response(forecast));
  app.context.renderDecisionSummary({prediction: {risk: 50, outcome: 'within_sla'}});
  assert.match(app.get('#decision-summary').textContent, /routine service review/);
});

test('cached risk is visible immediately while only demand remains loading', async () => {
  let finishForecast;
  const calls = [];
  const app = workspace((url, options) => {
    calls.push([url, JSON.parse(options.body)]);
    return new Promise(resolve => { finishForecast = resolve; });
  });
  app.read('sampleRequests[3].prediction = {risk: 91, mode: "model", outcome: "sla_miss", sla_days: 6}');
  const pending = app.context.selectSampleRequest('DEMO-104');
  assert.equal(app.get('#risk').textContent, '91%');
  assert.equal(app.get('article.risk').classList.contains('loading'), false);
  assert.equal(app.get('article.risk').attributes['aria-busy'], 'false');
  assert.equal(app.get('article.forecast').classList.contains('loading'), true);
  assert.equal(app.get('#forecast-total').textContent, '—');
  assert.match(app.get('.updated').textContent, /Risk ready.*Loading demand/);
  assert.equal(app.get('#generate-brief').disabled, false);
  assert.equal(app.get('#question').disabled, false);
  assert.deepEqual(calls.map(call => call[0]), ['/api/forecast']);
  app.get('#brief-status').textContent = 'Preparing a handling brief…';
  finishForecast(response(forecast));
  await pending;
  assert.equal(app.get('#forecast-total').textContent, 140);
  assert.equal(app.get('article.forecast').classList.contains('loading'), false);
  assert.equal(app.get('#brief-status').textContent, 'Preparing a handling brief…');
});

test('an uncached prediction appears as soon as scoring finishes, before demand', async () => {
  const pending = {};
  const app = workspace(url => new Promise(resolve => { pending[url] = resolve; }));
  const review = app.context.selectSampleRequest('DEMO-101');
  pending['/api/predict'](response({risk: 75, mode: 'model', outcome: 'sla_miss'}));
  await new Promise(setImmediate);
  assert.equal(app.get('#risk').textContent, '75%');
  assert.equal(app.get('article.risk').classList.contains('loading'), false);
  assert.equal(app.get('article.forecast').classList.contains('loading'), true);
  assert.equal(app.get('#generate-brief').disabled, false);
  pending['/api/forecast'](response(forecast));
  await review;
});

test('a late forecast cannot replace or stop loading the newly selected report', async () => {
  const pending = [];
  const app = workspace(() => new Promise(resolve => pending.push(resolve)));
  app.read('sampleRequests[0].prediction = {risk: 75, mode: "model"}; sampleRequests[1].prediction = {risk: 91, mode: "model"}');
  const first = app.context.selectSampleRequest('DEMO-101');
  const second = app.context.selectSampleRequest('DEMO-102');
  pending[0](response({...forecast, total: 999}));
  await first;
  assert.equal(app.get('#risk').textContent, '91%');
  assert.equal(app.get('#forecast-total').textContent, '—');
  assert.equal(app.get('article.forecast').classList.contains('loading'), true);
  assert.equal(app.get('article.forecast').attributes['aria-busy'], 'true');
  pending[1](response(forecast));
  await second;
  assert.equal(app.get('#forecast-total').textContent, 140);
  assert.match(app.get('#forecast-note').textContent, /Pothole/);
});

test('queue reports authentication failures instead of claiming probabilities are missing', async () => {
  const app = workspace(async () => responseError('tabular_scoring endpoint returned HTTP 401'));
  function responseError(error) { return {ok: false, json: async () => ({error})}; }
  await app.context.scoreQueue();
  assert.match(app.get('#queue-status').textContent, /Queue scoring unavailable.*access was rejected.*credentials/);
  assert.doesNotMatch(app.get('#queue-status').textContent, /class labels|No probability/);
  assert.equal(app.get('#score-queue').disabled, false);
  assert.equal(app.get('#queue-rows').children[0].children[1].textContent, 'Unavailable');
});

test('successful class-only queue responses explicitly explain why ranking is unavailable', async () => {
  const app = workspace(async () => response({mode: 'model', outcome: 'sla_miss', sla_days: 4}));
  await app.context.scoreQueue();
  assert.match(app.get('#queue-status').textContent, /class labels without probabilities/);
  assert.doesNotMatch(app.get('#queue-status').textContent, /credentials|scoring unavailable/);
});


test('policy answers distinguish agent references from citations and missing evidence', () => {
  const app = workspace(async () => response(forecast));
  const sources = [{agency: 'City', title: '311', url: 'https://www.phila.gov/311/'}];
  app.context.renderAnswer({answer: 'Contact 311.', sources, grounding: 'references'});
  assert.match(app.get('.grounded').textContent, /Agent references.*verify/);
  assert.equal(app.get('#sources').children.length, 1);
  assert.doesNotMatch(app.get('.grounded').textContent, /citations|Grounded/);
  app.context.renderAnswer({answer: 'Contact 311.', sources, grounding: 'citations'});
  assert.match(app.get('.grounded').textContent, /Agent citations/);
  app.context.renderAnswer({answer: 'Verify the routing.', sources: [], grounding: 'none'});
  assert.match(app.get('.grounded').textContent, /No references returned.*verify/);
  assert.equal(app.get('#sources').children.length, 0);
});
