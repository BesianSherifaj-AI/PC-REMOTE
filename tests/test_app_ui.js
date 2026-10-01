// Run the complete dashboard IIFE against controlled DOM/XHR/timers. No network,
// microphone, desktop input or real model is used.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');

function harness({codex = false} = {}) {
  const elements = {}, requests = [], timers = new Map(), intervals = new Map(), windowEvents = {}, documentEvents = {};
  let serial = 0;
  class Element {
    constructor(tag = 'div', id = '') {
      this.tagName = tag.toUpperCase(); this.id = id; this.children = []; this.attributes = {}; this.listeners = {};
      this.hidden = false; this.disabled = false; this.checked = false; this._value = ''; this._text = ''; this.replacements = 0;
      this.style = {setProperty(name, value) { this[name] = value; }}; this.offsetHeight = 104;
      this.scrollHeight = 200; this.scrollTop = 0; this.clientHeight = 300;
    }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this._text = String(value); this.children = []; this.replacements++; if (this.tagName === 'SELECT') this._value = ''; }
    get value() { return this._value; }
    set value(value) { this._value = String(value); }
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] || null; }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(name, callback) { (this.listeners[name] || (this.listeners[name] = [])).push(callback); }
    querySelectorAll(selector) { const found = []; this.children.forEach(child => { if (child.tagName.toLowerCase() === selector) found.push(child); found.push(...child.querySelectorAll(selector)); }); return found; }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    scrollIntoView() {} reset() {} pause() { this.paused = true; }
  }
  for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"/gi)) elements[match[2]] = new Element(match[1], match[2]);
  for (const id of ['model', 'availableModel']) { const option = new Element('option'); option.value = ''; elements[id].appendChild(option); }
  const nav = ['home', 'apps', 'desktop', 'chat', 'codex', 'comfy'].map(name => { const button = new Element('button'); button.setAttribute('data-page', name); return button; });
  const chrome = new Element(), body = new Element('body'), root = new Element('html');
  const sent = []; elements.viewer.contentWindow = {postMessage(data, origin) { sent.push({data, origin}); }};
  elements.desktopAutoReconnect.checked = true;
  const document = {
    body, documentElement: root, activeElement: body, hidden: false,
    getElementById(id) { assert.ok(elements[id], 'Missing real-page element: ' + id); return elements[id]; },
    createElement: tag => new Element(tag),
    querySelector: selector => selector === '.app-chrome' ? chrome : null,
    querySelectorAll(selector) {
      if (selector === '.page') return ['home', 'apps', 'desktop', 'chat', 'codex', 'comfy'].map(id => elements[id]);
      if (selector === 'nav button') return nav;
      if (selector === '#comfyOutputs video') return elements.comfyOutputs.querySelectorAll('video');
      return [];
    },
    addEventListener(name, callback) { (documentEvents[name] || (documentEvents[name] = [])).push(callback); }
  };
  const location = {origin: 'http://fixture.invalid:8840', protocol: 'http:', hash: '', search: codex ? '?workspace=codex' : ''};
  let voiceOptions, attachmentOptions, readerOptions;
  const voice = {starts: 0, cancels: 0, start() { this.starts++; }, cancel() { this.cancels++; }, status() {},
    transcript(value) { voiceOptions.append(value); voiceOptions.onIdle('transcribed'); }, fail() { voiceOptions.onError('Synthetic failure'); },
    idle(reason) { voiceOptions.onIdle(reason); }, live: () => voiceOptions.liveMode()};
  const attachments = {items: [], busy: () => false, get() { return this.items.slice(); }, clear() { this.items = []; },
    restore(previous) { if (!this.items.length) this.items = previous.slice(); }, modelChanged() {}, sentCount: () => attachmentOptions.sentCount()};
  const reader = {loaded: 0, stopped: 0, spoken: [], ready: () => true, load() { this.loaded++; }, stop() { this.stopped++; },
    speak(value) { this.spoken.push(value); }, ended() { readerOptions.onEnded(); }, fail() { readerOptions.onError(); }};
  const window = {
    location, scrollTo() {}, createMaicVoice(options) { voiceOptions = options; return voice; },
    createMaicAttachments(options) { attachmentOptions = options; return attachments; },
    createMaicReader(options) { readerOptions = options; return reader; },
    addEventListener(name, callback) { (windowEvents[name] || (windowEvents[name] = [])).push(callback); }
  };
  class XMLHttpRequest {
    constructor() { this.status = 0; this.responseText = ''; this.headers = {}; requests.push(this); }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers[name] = value; }
    send(body) { this.body = body; this.sent = true; }
    abort() { this.aborted = true; this.status = 0; if (this.onabort) this.onabort(); }
    respond(data, status = 200) { this.settled = true; this.status = status; this.responseText = JSON.stringify(data); if (this.onload) this.onload(); }
  }
  function timeout(fn, ms) { timers.set(++serial, {fn, ms}); return serial; }
  function pending(url) { const found = requests.filter(request => request.sent && !request.settled && !request.aborted && request.url === url); assert.ok(found.length, 'Expected pending request: ' + url); return found[found.length - 1]; }
  const context = {window, document, location, navigator: {language: 'en'}, XMLHttpRequest,
    history: {replaceState(_a, _b, value) { location.hash = value; }},
    setTimeout: timeout, clearTimeout: id => timers.delete(id),
    setInterval(fn, ms) { intervals.set(++serial, {fn, ms}); return serial; }, clearInterval: id => intervals.delete(id),
    Audio: class {play() { return Promise.resolve(); }}, console};
  vm.runInNewContext(script, context, {filename: 'web/app.js'});
  pending('/api/control-session').respond({ok: true, token: 'synthetic-memory-session', canManageDevices: false});
  function page(name) { nav.find(button => button.getAttribute('data-page') === name).onclick(); }
  function models(value) { pending('/api/lm/models').respond(value); }
  function message(data) { windowEvents.message.forEach(callback => callback({data, origin: location.origin, source: elements.viewer.contentWindow})); }
  return {elements, requests, timers, intervals, document, root, sent, page, pending, models, message, voice, attachments, reader,
    visibility(hidden) { document.hidden = hidden; documentEvents.visibilitychange.forEach(callback => callback()); }};
}

const catalog = () => ({ok: true, available: true, message: 'Models ready',
  models: [{id: 'a', name: 'Model A', contextLength: 8192, vision: true}, {id: 'b', name: 'Model B', contextLength: 8192, vision: false}],
  availableModels: [{id: 'installed-a', name: 'Installed A'}, {id: 'installed-b', name: 'Installed B'}]});
const audio = volume => ({ok: true, speaker: {available: true, volume, muted: false}, microphone: {available: true, muted: false}});
function count(test, url) { return test.requests.filter(request => request.sent && request.url === url).length; }
function event() { return {preventDefault() {}}; }
function progress(xhr, data) { xhr.status = 200; xhr.responseText += 'data: ' + JSON.stringify(data) + '\n\n'; xhr.onprogress(); }
function chatReady() { const test = harness(); test.page('chat'); test.models(catalog()); test.elements.model.value = 'a'; test.elements.model.onchange(); return test; }
async function ready() { await Promise.resolve(); await Promise.resolve(); }

(async function () {
  const model = harness(); model.page('chat'); model.models(catalog());
  const select = model.elements.model, installed = model.elements.availableModel;
  select.value = 'a'; installed.value = 'installed-a'; select.onchange();
  const firstLoaded = select.children[1], firstInstalled = installed.children[1];
  model.elements.refreshModels.onclick(); select.value = 'b'; installed.value = 'installed-b'; model.models(catalog());
  assert.equal(select.value, 'b', 'Async response must preserve the latest user selection');
  assert.equal(installed.value, 'installed-b');
  assert.equal(select.children[1], firstLoaded, 'Unchanged loaded catalog must retain option nodes');
  assert.equal(installed.children[1], firstInstalled, 'Unchanged installed catalog must retain option nodes');
  const changed = catalog(); changed.models.push({id: 'c', name: 'Model C'});
  model.document.activeElement = select; model.elements.refreshModels.onclick(); model.models(changed);
  assert.equal(select.children.length, 3, 'A focused selector must defer catalog replacement');
  model.document.activeElement = model.document.body; model.elements.refreshModels.onclick(); model.models(changed);
  assert.equal(select.children.length, 4, 'Deferred catalog must apply after focus leaves'); assert.equal(select.value, 'b');
  const moreInstalled = catalog(); moreInstalled.models = changed.models; moreInstalled.availableModels.push({id: 'installed-c', name: 'Installed C'});
  model.document.activeElement = installed; model.elements.refreshModels.onclick(); model.models(moreInstalled);
  assert.equal(installed.children[1], firstInstalled); assert.equal(installed.children.length, 3);
  model.document.activeElement = model.document.body; model.elements.refreshModels.onclick(); model.models(moreInstalled);
  assert.equal(installed.children.length, 4); assert.equal(installed.value, 'installed-b');

  const volume = harness(), slider = volume.elements.volume;
  slider.value = '73'; volume.document.activeElement = slider; slider.oninput();
  volume.pending('/api/audio').respond(audio(20));
  assert.equal(slider.value, '73', 'Incoming poll must not overwrite a drag'); assert.equal(volume.elements.volumeLabel.textContent, '73%');
  slider.onchange(); assert.equal(JSON.parse(volume.pending('/api/control').body).value, 73);
  volume.pending('/api/control').respond({ok: true, message: 'Updated'}); volume.pending('/api/audio').respond(audio(73));
  assert.equal(slider.value, '73');
  volume.document.activeElement = volume.document.body; slider.onblur(); volume.pending('/api/audio').respond(audio(44));
  assert.equal(slider.value, '44', 'Blur must permit the next actual audio state to refresh');

  const oldAudio = harness(), oldSlider = oldAudio.elements.volume;
  const stalePoll = oldAudio.pending('/api/audio');
  oldSlider.value = '61'; oldAudio.document.activeElement = oldSlider; oldSlider.oninput(); oldSlider.onchange();
  oldAudio.pending('/api/control').respond({ok: true, message: 'Volume set'});
  oldAudio.document.activeElement = oldAudio.document.body; oldSlider.onblur(); stalePoll.respond(audio(20));
  assert.equal(oldSlider.value, '61', 'A pre-mutation poll must not overwrite volume after the mutation finishes and focus leaves');
  oldAudio.pending('/api/audio').respond(audio(61)); assert.equal(oldSlider.value, '61');

  const chat = harness(); chat.page('chat'); chat.models(catalog()); chat.elements.model.value = 'a'; chat.elements.model.onchange();
  chat.elements.chatInput.value = 'First message'; chat.elements.chatForm.onsubmit(event());
  const firstChat = chat.pending('/api/lm/chat'); progress(firstChat, {text: 'First partial'}); chat.elements.chatStop.onclick();
  const stopped = chat.elements.chatStatus.textContent; progress(firstChat, {text: 'stale after stop'});
  assert.equal(chat.elements.chatStatus.textContent, stopped, 'Late stopped progress must not change status');
  chat.elements.chatClear.onclick(); const cleared = chat.elements.chatStatus.textContent;
  progress(firstChat, {text: 'stale after clear'}); firstChat.onload();
  assert.equal(chat.elements.chatStatus.textContent, cleared); assert.equal(chat.elements.chatMessages.children.length, 0);
  chat.elements.chatInput.value = 'Second message'; chat.elements.chatForm.onsubmit(event());
  const secondChat = chat.pending('/api/lm/chat'); const waiting = chat.elements.chatStatus.textContent;
  progress(firstChat, {text: 'stale while next request waits'}); firstChat.onerror();
  assert.equal(chat.elements.chatStatus.textContent, waiting); assert.equal(chat.elements.chatSend.disabled, true);
  assert.equal(JSON.parse(secondChat.body).messages.length, 1, 'Clear must remove the previous conversation from the next request');
  progress(secondChat, {text: 'Fresh answer'}); progress(secondChat, {done: true});
  assert.equal(chat.elements.chatStatus.textContent, 'Reply complete.');

  const imageChat = chatReady(), image = {name: 'fixture.png', url: 'data:image/jpeg;base64,/9j/fixture'};
  imageChat.attachments.items = [image]; imageChat.elements.chatInput.value = 'What is shown?'; imageChat.elements.chatForm.onsubmit(event());
  const imageRequest = imageChat.pending('/api/lm/chat'), imagePayload = JSON.parse(imageRequest.body);
  assert.equal(imagePayload.model, 'a');
  assert.deepEqual(imagePayload.messages, [{role: 'user', content: [{type: 'text', text: 'What is shown?'}, {type: 'image_url', image_url: {url: image.url}}]}]);
  assert.equal(imageChat.attachments.items.length, 0, 'Sending must consume the prepared attachments');
  imageRequest.respond({ok: false, message: 'Synthetic vision failure'}, 400);
  assert.equal(imageChat.attachments.items[0], image, 'Failed chat must restore the original prepared attachment');
  assert.equal(imageChat.elements.chatInput.value, 'What is shown?'); assert.equal(imageChat.attachments.sentCount(), 0);
  imageChat.elements.model.value = 'b'; imageChat.elements.chatForm.onsubmit(event());
  assert.equal(count(imageChat, '/api/lm/chat'), 1); assert.match(imageChat.elements.chatStatus.textContent, /vision-capable/);
  imageChat.elements.model.value = 'a'; imageChat.elements.chatForm.onsubmit(event());
  const retryImage = imageChat.pending('/api/lm/chat'); assert.equal(JSON.parse(retryImage.body).messages.length, 1);
  progress(retryImage, {text: 'A screenshot.'}); progress(retryImage, {done: true}); assert.equal(imageChat.attachments.sentCount(), 1);
  imageChat.elements.model.value = 'b'; imageChat.elements.chatInput.value = 'Continue'; imageChat.elements.chatForm.onsubmit(event());
  assert.equal(count(imageChat, '/api/lm/chat'), 2, 'A text follow-up to an image conversation also requires a vision model');
  imageChat.elements.chatClear.onclick(); imageChat.elements.chatForm.onsubmit(event());
  assert.equal(imageChat.attachments.sentCount(), 0);
  const unreported = chatReady(), unknown = catalog(); unknown.models[0].vision = null;
  unreported.elements.refreshModels.onclick(); unreported.models(unknown); unreported.attachments.items = [image];
  unreported.elements.chatForm.onsubmit(event()); assert.equal(count(unreported, '/api/lm/chat'), 0, 'Unknown vision support must not be treated as true');

  const quick = harness(), quickCatalog = catalog(); quickCatalog.quickModels = [{id: 'installed-fast', name: 'Fast fixture', vision: true, description: 'Fixture'}];
  quick.page('chat'); quick.models(quickCatalog); assert.equal(quick.elements.model.value, '', 'Catalog must not silently select a model');
  quick.elements.quickModels.children[0].onclick(); assert.deepEqual(JSON.parse(quick.pending('/api/lm/load').body), {model: 'installed-fast'});
  quick.document.activeElement = quick.elements.model;
  quick.pending('/api/lm/load').respond({ok: true, selectedInstanceId: 'fast-instance', message: 'Loaded fixture'});
  const fastCatalog = {...quickCatalog, models: [...quickCatalog.models, {id: 'fast-instance', name: 'Fast fixture', vision: true}]};
  quick.models(fastCatalog); assert.equal(quick.elements.model.value, '', 'Explicit load selection must wait while the selector is focused');
  quick.document.activeElement = quick.document.body; quick.elements.refreshModels.onclick(); quick.models(fastCatalog);
  assert.equal(quick.elements.model.value, 'fast-instance', 'Deferred model refresh must apply the verified loaded instance id');

  const live = chatReady(); live.elements.liveVoice.onclick(); assert.equal(live.voice.starts, 1); assert.equal(live.voice.live(), true);
  live.voice.transcript('A spoken question'); assert.equal(count(live, '/api/lm/chat'), 1); assert.equal(live.voice.live(), true, 'Successful transcription must keep live mode enabled');
  const spokenChat = live.pending('/api/lm/chat'); assert.equal(JSON.parse(spokenChat.body).messages[0].content, 'A spoken question');
  progress(spokenChat, {text: 'Spoken answer'}); progress(spokenChat, {done: true}); assert.deepEqual(live.reader.spoken, ['Spoken answer']);
  assert.equal(live.voice.starts, 1, 'Listening must wait for playback to finish'); live.reader.ended(); assert.equal(live.voice.starts, 2);
  live.elements.chatStop.onclick(); assert.equal(live.voice.live(), false); const startsAfterStop = live.voice.starts;
  live.reader.ended(); assert.equal(live.voice.starts, startsAfterStop, 'Stopping must prevent late playback events from restarting live capture');
  live.elements.liveVoice.onclick(); live.visibility(true); assert.equal(live.voice.live(), false); live.reader.ended(); live.visibility(false);
  assert.equal(live.voice.starts, startsAfterStop + 1, 'Returning to a visible page must not silently resume capture');
  live.elements.liveVoice.onclick(); live.voice.fail(); assert.equal(live.voice.live(), false, 'Capture cancellation/failure must leave live mode');
  live.elements.liveVoice.onclick(); live.voice.idle('silent'); assert.equal(live.voice.live(), false);
  live.elements.liveVoice.onclick(); live.reader.fail(); assert.equal(live.voice.live(), false, 'Voice playback failure must leave live mode');
  live.elements.liveVoice.onclick(); live.voice.transcript('Cancel this request'); const cancelLive = live.pending('/api/lm/chat'); live.elements.chatClear.onclick();
  assert.equal(cancelLive.aborted, true); assert.equal(live.voice.live(), false); assert.equal(live.elements.chatMessages.children.length, 0);
  progress(cancelLive, {text: 'Late answer'}); progress(cancelLive, {done: true}); assert.deepEqual(live.reader.spoken, ['Spoken answer']);

  const workspace = harness({codex: true}); assert.equal(workspace.document.body.getAttribute('data-workspace'), 'codex');
  const codexApps = {ok: true, integrations: {codexAppId: 'codex-fixture'}, apps: [{id: 'codex-fixture', name: 'Codex', running: true, hasWindow: true}]};
  workspace.pending('/api/pc/apps').respond(codexApps);
  assert.deepEqual(JSON.parse(workspace.pending('/api/pc/action').body), {id: 'codex-fixture', action: 'activate'});
  workspace.pending('/api/pc/action').respond({ok: true, message: 'Activated fixture'});
  workspace.pending('/api/control-session').respond({ok: true, token: 'synthetic-viewer-session'});
  workspace.pending('/api/desktop/session').respond({ok: true, credentials: {password: 'synthetic'}, port: 8841});
  workspace.elements.reconnect.onclick(); workspace.pending('/api/control-session').respond({ok: true, token: 'synthetic-renewal'});
  assert.equal(count(workspace, '/api/pc/action'), 1, 'Renewing access must not activate Codex a second time');
  assert.equal(count(workspace, '/api/pc/apps'), 1, 'Codex workspace discovery must run once automatically');
  workspace.elements.codexFocus.onclick(); workspace.pending('/api/pc/apps').respond(codexApps);
  assert.equal(count(workspace, '/api/pc/action'), 2, 'The explicit focus button remains available after automatic activation');

  const desktop = harness(); desktop.page('desktop'); desktop.elements.desktopConnect.onclick();
  desktop.pending('/api/control-session').respond({ok: true, token: 'synthetic-desktop-session'});
  desktop.pending('/api/desktop/session').respond({ok: true, credentials: {password: 'synthetic-not-a-real-secret'}, port: 8841});
  desktop.message({type: 'maic-viewer-ready'}); const connectionId = desktop.sent[0].data.connectionId;
  desktop.message({type: 'maic-viewer-status', state: 'connected', message: 'Connected fixture', connectionId});
  desktop.message({type: 'maic-viewer-status', state: 'disconnected', message: 'Lost fixture', connectionId});
  const retry = Array.from(desktop.timers.values()).find(timer => timer.ms === 2000);
  assert.ok(retry, 'Unexpected disconnection must schedule bounded automatic retry');
  desktop.elements.desktopDisconnect.onclick(); const requestsBefore = desktop.requests.length;
  assert.ok(!Array.from(desktop.timers.values()).some(timer => timer.ms === 2000)); retry.fn();
  assert.equal(desktop.requests.length, requestsBefore, 'A queued retry must not reconnect after manual Disconnect');
  desktop.message({type: 'maic-viewer-status', state: 'connected', message: 'Stale connection', connectionId});
  assert.equal(desktop.elements.desktopMessage.textContent, 'Desktop disconnected.'); assert.equal(desktop.elements.viewer.src, 'about:blank');
  desktop.elements.desktopConnect.onclick(); const delayedSession = desktop.pending('/api/control-session'); desktop.elements.desktopDisconnect.onclick();
  const sessionRequests = count(desktop, '/api/desktop/session'); delayedSession.respond({ok: true, token: 'obsolete-memory-session'});
  assert.equal(count(desktop, '/api/desktop/session'), sessionRequests, 'Cancelled connection negotiation must not create a desktop session');

  const fullscreen = harness(); fullscreen.root.requestFullscreen = () => Promise.reject(new Error('synthetic browser restriction'));
  fullscreen.elements.appFullscreen.onclick(); await ready(); assert.match(fullscreen.elements.toast.textContent, /blocked by the browser/);
  fullscreen.elements.desktopFrame.requestFullscreen = () => Promise.reject(new Error('synthetic browser restriction'));
  fullscreen.elements.desktopFullscreen.onclick(); await ready(); assert.match(fullscreen.elements.toast.textContent, /blocked by the browser/);
  fullscreen.root.requestFullscreen = () => { throw new Error('synthetic unsupported fullscreen'); };
  fullscreen.elements.appFullscreen.onclick(); assert.match(fullscreen.elements.toast.textContent, /unavailable/);
  console.log('Dashboard UI: polling races, image payload/restoration/vision guards, explicit fast-model selection, live voice lifecycle, once-only Codex activation, desktop and fullscreen passed.');
}()).catch(error => { console.error(error); process.exitCode = 1; });
