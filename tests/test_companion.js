// Controlled DOM, asynchronous HTTP callbacks, timers and wake-lock promises.
// No agent, ComfyUI, browser permission or operating-system action is invoked.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/companion.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; }
async function flush() { await Promise.resolve(); await Promise.resolve(); await Promise.resolve(); }
function event() { return {preventDefault() {}}; }
function agent(id, more = {}) { return Object.assign({id, name: id.toUpperCase(), canSend: true, status: 'active', activity: 'idle', role: 'Configured role', model: 'local-model'}, more); }
function team(agents = [agent('hermes'), agent('main')]) { return {ok: true, available: true, agents, message: 'Connected'}; }
function library(more = {}) { return Object.assign({ok: true, available: true, total: 1, offset: 0, nextOffset: null, outputs: [{id: 'image', name: 'image.png'}], folders: [], breadcrumbs: [{id: 'root', name: 'All outputs'}]}, more); }
function harness({wake, secure = true} = {}) {
  const elements = {}, requests = [], intervals = [], documentEvents = {}, windowEvents = {}, rendered = [];
  let ready = true, currentPage = 'home', recent = 0;
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.attributes = {}; this._text = ''; this.value = ''; this.disabled = false; this.hidden = false; this.replacements = 0; }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this._text = String(value); this.children = []; this.replacements++; }
    set innerHTML(value) { throw new Error('Use text and DOM nodes, not HTML'); }
    appendChild(child) { if (child.parentNode) child.parentNode.children = child.parentNode.children.filter(item => item !== child); child.parentNode = this; this.children.push(child); return child; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name]; }
    focus() { document.activeElement = this; }
  }
  for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"/gi)) elements[match[2]] = new Element(match[1]);
  elements.comfySource.value = 'recent'; elements.libraryType.value = 'all';
  const document = {hidden: false, activeElement: null, createElement: tag => new Element(tag),
    getElementById(id) { assert.ok(elements[id], 'Missing real element ' + id); return elements[id]; },
    addEventListener(name, callback) { (documentEvents[name] || (documentEvents[name] = [])).push(callback); }};
  const window = {isSecureContext: secure, addEventListener(name, callback) { (windowEvents[name] || (windowEvents[name] = [])).push(callback); }};
  function request(method, url, payload, done, privateRequest) {
    assert.equal(privateRequest, true, 'Every companion route must use authenticated requests');
    const record = {method, url, payload, settled: false,
      respond(data, error = null) { assert.equal(this.settled, false); this.settled = true; done(error || (data && data.ok === false ? data.message || 'Failed' : null), data); }};
    requests.push(record); return record;
  }
  vm.runInNewContext(source, {window, document, navigator: {wakeLock: wake}, Date, Promise, console, setInterval(fn, ms) { intervals.push({fn, ms}); }});
  const api = window.createPCCompanion({get: (url, done, auth) => request('GET', url, null, done, auth),
    post: (url, payload, done, auth) => request('POST', url, payload, done, auth), ready: () => ready, page: () => currentPage,
    renderOutputs(data, force) { assert.equal(force, true); rendered.push(data); }, showRecent() { recent++; }});
  function pending(prefix) { const result = requests.filter(item => !item.settled && item.url.startsWith(prefix)); assert.ok(result.length, 'Expected request ' + prefix); return result[result.length - 1]; }
  function refresh(data = team()) { elements.agentsRefresh.onclick(); pending('/api/agents/status').respond(data); }
  function select(id) { const button = elements.agentCards.children.find(item => item.children[1].textContent === id.toUpperCase()); assert.ok(button, 'Agent card ' + id); button.onclick(); return button; }
  function submit(value) { elements.agentInput.value = value; elements.agentInput.oninput(); elements.agentForm.onsubmit(event()); }
  function useLibrary() { elements.comfySource.value = 'library'; elements.comfySource.onchange(); return pending('/api/comfy/library?'); }
  return {api, elements, document, requests, intervals, rendered, pending, refresh, select, submit, useLibrary,
    count(url) { return requests.filter(item => item.url === url).length; },
    ready(value) { ready = value; },
    page(value) { currentPage = value; api.onPage(value); },
    visibility(hidden) { document.hidden = hidden; (documentEvents.visibilitychange || []).forEach(callback => callback()); },
    pagehide() { (windowEvents.pagehide || []).forEach(callback => callback()); },
    recentCount() { return recent; }};
}
function sentinel({rejectRelease = false} = {}) {
  const callbacks = []; return {released: 0, addEventListener(name, callback) { assert.equal(name, 'release'); callbacks.push(callback); },
    release() { this.released++; callbacks.forEach(callback => callback()); return rejectRelease ? Promise.reject(new Error('Already released')) : Promise.resolve(); },
    browserRelease() { callbacks.forEach(callback => callback()); }};
}

(async function () {
  const blocked = harness();
  blocked.submit('No selected agent'); assert.equal(blocked.count('/api/agents/send'), 0);
  blocked.refresh(team([agent('unknown'), agent('hermes', {canSend: false, activity: 'offline'})]));
  assert.equal(blocked.elements.agentCards.children.length, 1, 'Unknown IDs are not selectable');
  blocked.select('hermes'); blocked.submit('Offline'); assert.equal(blocked.elements.agentSend.disabled, true);
  blocked.refresh(team([agent('hermes', {canSend: true, status: 'unknown'})])); blocked.submit('Unknown');
  blocked.refresh(team([agent('hermes', {canSend: true, activity: 'unknown'})])); blocked.submit('Unknown activity');
  blocked.refresh(Object.assign(team(), {available: false})); blocked.submit('Offline hub');
  assert.equal(blocked.count('/api/agents/send'), 0, 'Unknown/offline states never dispatch');
  blocked.refresh(); blocked.select('hermes'); blocked.ready(false); blocked.submit('Approval expired');
  assert.equal(blocked.count('/api/agents/send'), 0);

  const stable = harness(); stable.refresh();
  const firstCard = stable.select('hermes'), firstAvatar = firstCard.children[0], firstLabel = firstCard.children[1];
  firstCard.focus(); const replacements = stable.elements.agentCards.replacements;
  stable.refresh(); assert.equal(stable.elements.agentCards.children[0], firstCard); assert.equal(stable.elements.agentCards.replacements, replacements);
  assert.equal(stable.document.activeElement, firstCard); assert.equal(firstCard.children[0], firstAvatar); assert.equal(firstCard.children[1], firstLabel);
  assert.equal(firstAvatar.children[0].src, '/agent-avatars/moss.svg');
  stable.refresh(team([agent('hermes', {activity: 'waiting', model: {id: 'new-model'}}), agent('main')]));
  assert.equal(firstAvatar.getAttribute('data-state'), 'waiting'); assert.equal(firstCard.children[3].textContent, 'Queued');
  assert.equal(firstCard.children[4].textContent, 'new-model'); assert.equal(stable.elements.agentCards.children[0], firstCard);
  stable.refresh(team([agent('hermes', {activity: 'unavailable'})])); assert.equal(firstAvatar.getAttribute('data-state'), 'attention');
  stable.elements.agentsRefresh.onclick(); stable.pending('/api/agents/status').respond(null, 'Disconnected');
  stable.submit('Cannot send while disconnected'); assert.equal(stable.count('/api/agents/send'), 0);

  const chat = harness(); chat.refresh(); chat.select('hermes'); chat.submit('  Explicit task  ');
  const send = chat.pending('/api/agents/send'); assert.equal(send.payload.agentId, 'hermes'); assert.equal(send.payload.text, 'Explicit task');
  chat.submit('Double click'); assert.equal(chat.count('/api/agents/send'), 1);
  chat.select('main'); chat.elements.agentInput.value = 'Draft for main';
  const receipt = 'a'.repeat(32);
  send.respond({ok: true, agentId: 'hermes', receiptId: receipt, status: 'queued'});
  assert.equal(chat.elements.agentInput.value, 'Draft for main'); assert.doesNotMatch(chat.elements.agentConversation.textContent, /Explicit task/);
  const firstPoll = chat.pending('/api/agents/receipt');
  firstPoll.respond({ok: true, agentId: 'main', receiptId: receipt, complete: true, reply: 'WRONG AGENT'});
  assert.doesNotMatch(chat.elements.agentConversation.textContent, /WRONG AGENT/);
  chat.refresh(team([agent('main')])); // Hermes disappears while its owned receipt is pending.
  chat.pending('/api/agents/receipt').respond({ok: true, agentId: 'hermes', receiptId: receipt, complete: true, status: 'completed', reply: 'Owned result'});
  assert.doesNotMatch(chat.elements.agentConversation.textContent, /Owned result/);
  chat.refresh(); chat.select('hermes'); assert.match(chat.elements.agentConversation.textContent, /Explicit task/); assert.match(chat.elements.agentConversation.textContent, /Owned result/);
  assert.doesNotMatch(chat.elements.agentConversation.textContent, /WRONG AGENT/);
  chat.elements.agentClear.onclick(); assert.doesNotMatch(chat.elements.agentConversation.textContent, /Owned result/);

  const failed = harness(); failed.refresh(); failed.select('hermes'); failed.submit('Task'); const failing = failed.pending('/api/agents/send');
  failed.select('main'); failed.elements.agentInput.value = 'Keep draft'; failed.page('comfy');
  failing.respond({ok: false, status: 'uncertain', message: 'Check Team Hub before retrying.'});
  assert.doesNotMatch(failed.elements.agentJobStatus.textContent, /Check Team Hub/); assert.equal(failed.elements.agentInput.value, 'Keep draft');
  failed.select('hermes'); assert.match(failed.elements.agentJobStatus.textContent, /Check Team Hub/); assert.equal(failed.count('/api/agents/send'), 1);
  failed.submit('Next explicit task'); failed.pending('/api/agents/send').respond({ok: true, agentId: 'hermes', receiptId: receipt, status: 'queued'});
  failed.pending('/api/agents/receipt').respond({ok: true, agentId: 'hermes', receiptId: receipt, complete: true, status: 'failed', message: 'Task needs attention'});
  assert.match(failed.elements.agentConversation.textContent, /Task needs attention/); assert.match(failed.elements.agentJobStatus.textContent, /failed/);
  failed.submit('Another task'); failed.pending('/api/agents/send').respond({ok: true, agentId: 'hermes', receiptId: receipt, status: 'queued'});
  failed.pending('/api/agents/receipt').respond({ok: false, status: 'unknown', message: 'Receipt expired'});
  assert.match(failed.elements.agentJobStatus.textContent, /Receipt expired/); failed.elements.agentInput.value = 'New'; failed.elements.agentInput.oninput(); assert.equal(failed.elements.agentSend.disabled, false);

  const media = harness(); const old = media.useLibrary();
  media.elements.librarySearch.value = 'new'; media.elements.librarySearchForm.onsubmit(event()); const current = media.pending('/api/comfy/library?');
  assert.match(current.url, /search=new/); current.respond(library({nextOffset: 24, total: 30}));
  const rendered = media.rendered.length; old.respond(library({outputs: [{name: 'STALE'}]})); assert.equal(media.rendered.length, rendered);
  assert.equal(media.elements.libraryNext.disabled, false); media.elements.libraryNext.onclick(); const second = media.pending('/api/comfy/library?'); assert.match(second.url, /offset=24&/);
  media.elements.libraryNext.onclick(); assert.equal(media.pending('/api/comfy/library?'), second, 'Busy next click must not duplicate a page request');
  second.respond(library({nextOffset: null, total: 30})); assert.equal(media.elements.libraryNext.disabled, true);
  media.elements.libraryPrevious.onclick(); assert.match(media.pending('/api/comfy/library?').url, /offset=0&/);
  const folderId = 'b'.repeat(32); media.pending('/api/comfy/library?').respond(library({folders: [{id: folderId, name: 'Folder'}]}));
  media.elements.libraryFolders.children[1].onclick(); const folderRequest = media.pending('/api/comfy/library?'); assert.match(folderRequest.url, new RegExp('folder=' + folderId));
  media.elements.libraryType.value = 'video'; media.elements.libraryType.onchange(); const typed = media.pending('/api/comfy/library?'); assert.match(typed.url, /media=video/);
  typed.respond(library({outputs: [{name: 'clip.mp4'}]})); folderRequest.respond(library({outputs: [{name: 'STALE FOLDER'}]}));
  assert.equal(media.rendered[media.rendered.length - 1].outputs[0].name, 'clip.mp4');
  media.api.refreshLibrary(); const afterSwitch = media.pending('/api/comfy/library?'); const beforeRecent = media.rendered.length;
  media.elements.comfySource.value = 'recent'; media.elements.comfySource.onchange(); afterSwitch.respond(library());
  assert.equal(media.rendered.length, beforeRecent); assert.equal(media.recentCount(), 1); assert.equal(media.elements.libraryControls.hidden, true);
  const hidden = media.useLibrary(); media.page('home'); hidden.respond(library()); assert.equal(media.rendered.length, beforeRecent);
  media.page('comfy'); media.pending('/api/comfy/library?').respond({ok: true, available: false, message: 'Configure output folder'});
  assert.equal(media.rendered[media.rendered.length - 1].available, false); assert.equal(media.elements.libraryNext.disabled, true);
  media.api.refreshLibrary(); media.pending('/api/comfy/library?').respond(null, 'Comfy request failed'); assert.equal(media.rendered[media.rendered.length - 1].available, false);
  media.elements.libraryOpen.onclick(); assert.equal(JSON.stringify(media.pending('/api/comfy/library/open').payload), '{}');

  const unsupported = harness(); assert.equal(unsupported.elements.keepAwake.disabled, true);
  const deniedRequest = deferred(), denied = harness({wake: {request: () => deniedRequest.promise}});
  denied.elements.keepAwake.onclick(); deniedRequest.reject(new Error('NotAllowedError')); await flush();
  assert.match(denied.elements.awakeStatus.textContent, /denied/); assert.equal(denied.elements.keepAwake.getAttribute('aria-pressed'), 'false');
  const thrown = harness({wake: {request() { throw new Error('Denied synchronously'); }}}); thrown.elements.keepAwake.onclick(); assert.match(thrown.elements.awakeStatus.textContent, /denied/);
  const staleRequest = deferred(), staleWake = harness({wake: {request: () => staleRequest.promise}}), staleLock = sentinel({rejectRelease: true});
  staleWake.elements.keepAwake.onclick(); staleWake.elements.keepAwake.onclick(); staleRequest.resolve(staleLock); await flush();
  assert.equal(staleLock.released, 1); assert.equal(staleWake.elements.keepAwake.getAttribute('aria-pressed'), 'false');
  const acquisitions = [], awake = harness({wake: {request(type) { assert.equal(type, 'screen'); const item = deferred(); acquisitions.push(item); return item.promise; }}});
  awake.elements.keepAwake.onclick(); awake.visibility(false); assert.equal(acquisitions.length, 1, 'Pending wake request is not duplicated');
  const held = sentinel(); acquisitions[0].resolve(held); await flush(); assert.match(awake.elements.awakeStatus.textContent, /stay awake/);
  held.browserRelease(); assert.match(awake.elements.awakeStatus.textContent, /paused/);
  awake.visibility(true); awake.visibility(false); assert.equal(acquisitions.length, 2);
  const resumed = sentinel(); acquisitions[1].resolve(resumed); await flush(); awake.visibility(true); assert.equal(resumed.released, 1);
  awake.visibility(false); const closing = sentinel(); awake.pagehide(); acquisitions[2].resolve(closing); await flush(); assert.equal(closing.released, 1);
  assert.equal(awake.elements.keepAwake.getAttribute('aria-pressed'), 'false');
  console.log('Companion behavior checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
