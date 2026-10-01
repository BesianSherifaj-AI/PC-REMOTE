// Run the complete dashboard IIFE against controlled DOM/XHR/timers. No network,
// microphone, desktop input or real model is used.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');

function harness({hash = '', standalone = false, displayMode = false, configure} = {}) {
  const elements = {}, requests = [], timers = new Map(), intervals = new Map(), windowEvents = {}, documentEvents = {};
  let serial = 0;
  class Element {
    constructor(tag = 'div', id = '') {
      this.tagName = tag.toUpperCase(); this.id = id; this.className = ''; this.children = []; this.attributes = {}; this.listeners = {};
      this.hidden = false; this.disabled = false; this.checked = false; this._value = ''; this._text = ''; this.replacements = 0;
      this.style = {setProperty(name, value) { this[name] = value; }}; this.offsetHeight = 104;
      this.scrollHeight = 200; this.scrollTop = 0; this.clientHeight = 300;
    }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this._text = String(value); this.children = []; this.replacements++; if (this.tagName === 'SELECT') this._value = ''; }
    set innerHTML(_value) { throw new Error('Dashboard must use text/DOM nodes, never HTML strings'); }
    get value() { return this._value; }
    set value(value) { this._value = String(value); }
    appendChild(child) { this.children.push(child); child.parentNode = this; return child; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    getAttribute(name) { return this.attributes[name] || null; }
    removeAttribute(name) { delete this.attributes[name]; }
    addEventListener(name, callback) { (this.listeners[name] || (this.listeners[name] = [])).push(callback); }
    removeEventListener(name, callback) { this.listeners[name] = (this.listeners[name] || []).filter(listener => listener !== callback); }
    querySelectorAll(selector) { const found = []; this.children.forEach(child => { if (child.tagName.toLowerCase() === selector) found.push(child); found.push(...child.querySelectorAll(selector)); }); return found; }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    scrollIntoView() {} reset() {} pause() { this.paused = true; }
    focus() { document.activeElement = this; }
  }
  for (const match of html.matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"/gi)) elements[match[2]] = new Element(match[1], match[2]);
  for (const id of ['model', 'availableModel']) { const option = new Element('option'); option.value = ''; elements[id].appendChild(option); }
  const pages = ['home', 'apps', 'desktop', 'chat', 'comfy', 'agents'];
  const nav = pages.map(name => { const button = new Element('button'); button.setAttribute('data-page', name); return button; });
  const chrome = new Element(), body = new Element('body'), root = new Element('html');
  const sent = []; elements.viewer.contentWindow = {postMessage(data, origin) { sent.push({data, origin}); }};
  elements.desktopAutoReconnect.checked = true;
  const document = {
    body, documentElement: root, activeElement: body, hidden: false,
    getElementById(id) { assert.ok(elements[id], 'Missing real-page element: ' + id); return elements[id]; },
    createElement: tag => new Element(tag),
    createTextNode(value) { const node = new Element('#text'); node.textContent = value; return node; },
    querySelector: selector => selector === '.app-chrome' ? chrome : null,
    querySelectorAll(selector) {
      if (selector === '.page') return pages.map(id => elements[id]);
      if (selector === 'nav button') return nav;
      if (selector === '#comfyOutputs video') return elements.comfyOutputs.querySelectorAll('video');
      if (selector === '#appCards .star') return elements.appCards.querySelectorAll('button').filter(button => button.className.split(' ').includes('star'));
      return [];
    },
    addEventListener(name, callback) { (documentEvents[name] || (documentEvents[name] = [])).push(callback); }
  };
  const location = {origin: 'http://fixture.invalid:8840', protocol: 'http:', hash, search: ''};
  let voiceOptions, attachmentOptions, readerOptions;
  const voice = {starts: 0, cancels: 0, start() { this.starts++; }, cancel() { this.cancels++; }, status() {},
    transcript(value) { voiceOptions.append(value); voiceOptions.onIdle('transcribed'); }, fail() { voiceOptions.onError('Synthetic failure'); },
    idle(reason) { voiceOptions.onIdle(reason); }, live: () => voiceOptions.liveMode()};
  const attachments = {items: [], isBusy: false, busy() { return this.isBusy; }, get() { return this.items.slice(); },
    clear() { this.items = []; this.isBusy = false; attachmentOptions.onChange(); },
    restore(previous) { if (!this.items.length) this.items = previous.slice(); attachmentOptions.onChange(); }, modelChanged() { attachmentOptions.onChange(); },
    setBusy(value) { this.isBusy = value; attachmentOptions.onChange(); }, sentCount: () => attachmentOptions.sentCount()};
  const reader = {loaded: 0, stopped: 0, spoken: [], ready: () => true, load() { this.loaded++; }, stop() { this.stopped++; },
    speak(value) { this.spoken.push(value); }, ended() { readerOptions.onEnded(); }, fail() { readerOptions.onError(); }};
  const window = {createPCCompanion: () => ({connected() {}, onPage() {}, isLibrary: () => false, refreshLibrary() {}}),
    location, scrollTo() {}, matchMedia: () => ({matches: displayMode}), createMaicVoice(options) { voiceOptions = options; return voice; },
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
  if (configure) configure({window, document, root, elements});
  const context = {window, document, location, navigator: {language: 'en', standalone}, XMLHttpRequest,
    history: {replaceState(_a, _b, value) { location.hash = value; }},
    setTimeout: timeout, clearTimeout: id => timers.delete(id),
    setInterval(fn, ms) { intervals.set(++serial, {fn, ms}); return serial; }, clearInterval: id => intervals.delete(id),
    Audio: class {play() { return Promise.resolve(); }}, console};
  vm.runInNewContext(script, context, {filename: 'web/app.js'});
  pending('/api/control-session').respond({ok: true, token: 'synthetic-memory-session', canManageDevices: false});
  function page(name) { nav.find(button => button.getAttribute('data-page') === name).onclick(); }
  function models(value) { pending('/api/lm/models').respond(value); }
  function message(data, origin = location.origin, source = elements.viewer.contentWindow) { windowEvents.message.forEach(callback => callback({data, origin, source})); }
  return {elements, requests, timers, intervals, document, root, sent, page, pending, models, message, voice, attachments, reader,
    emitDocument(name, event = {}) { documentEvents[name].forEach(callback => callback(event)); },
    navigateHash(value) { location.hash = value; windowEvents.hashchange.forEach(callback => callback()); },
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

  function formatted(value, ending = 'done') {
    const test = chatReady(); test.elements.chatInput.value = 'Formatting fixture'; test.elements.chatForm.onsubmit(event());
    const xhr = test.pending('/api/lm/chat'); progress(xhr, {text: value}); const output = test.elements.chatMessages.children[1].children[1];
    assert.equal(output.textContent, value); assert.equal(output.querySelectorAll('strong').length, 0); assert.equal(output.querySelectorAll('code').length, 0, 'Streaming must remain plaintext');
    if (ending === 'error') progress(xhr, {error: 'Synthetic failure'}); else if (ending === 'partial') test.elements.chatStop.onclick(); else progress(xhr, {done: true});
    return {test, output};
  }
  const rich = formatted('Colors **blue** and **red**.\nUse `a < b && c > d`.');
  assert.equal(rich.output.textContent, 'Colors blue and red.\nUse a < b && c > d.');
  assert.deepEqual(rich.output.querySelectorAll('strong').map(node => node.textContent), ['blue', 'red']);
  assert.equal(rich.output.querySelector('code').textContent, 'a < b && c > d');
  rich.test.elements.chatInput.value = 'Next'; rich.test.elements.chatForm.onsubmit(event());
  assert.equal(JSON.parse(rich.test.pending('/api/lm/chat').body).messages[1].content, 'Colors **blue** and **red**.\nUse `a < b && c > d`.', 'Formatting must preserve the actual model conversation text');
  const fenced = formatted('Before\n```python\nif (a < b) {\n  **literal** & "quoted"\n}\n```\nAfter **bold**');
  assert.equal(fenced.output.querySelectorAll('pre').length, 1);
  assert.equal(fenced.output.querySelector('pre').querySelector('code').textContent, 'if (a < b) {\n  **literal** & "quoted"\n}\n');
  assert.equal(fenced.output.querySelectorAll('strong').length, 1, 'Code-block contents must stay literal');
  const crlf = formatted('Top\r\n```js\r\nx < y\r\n```\r\nBottom');
  assert.equal(crlf.output.textContent, 'Top\r\nx < y\r\n\r\nBottom'); assert.equal(crlf.output.querySelector('code').textContent, 'x < y\r\n');
  const literalText = '**unfinished and `unfinished <script>alert(1)</script> &amp;\n[link](javascript:alert(1))';
  const literal = formatted(literalText); assert.equal(literal.output.textContent, literalText);
  assert.equal(literal.output.querySelectorAll('script').length, 0); assert.equal(literal.output.querySelectorAll('a').length, 0);
  const escaped = formatted('\\**literal** and \\`literal` with **bold**');
  assert.equal(escaped.output.textContent, '\\**literal** and \\`literal` with bold'); assert.equal(escaped.output.querySelectorAll('code').length, 0);
  const unclosedFence = 'Before\n```python\n**unfinished block**\n<img src=x onerror=alert(1)>';
  const unmatched = formatted(unclosedFence); assert.equal(unmatched.output.textContent, unclosedFence); assert.equal(unmatched.output.querySelectorAll('strong').length, 0); assert.equal(unmatched.output.querySelectorAll('img').length, 0);
  const partial = formatted('**Partial** reply', 'partial'); assert.equal(partial.output.querySelector('strong').textContent, 'Partial');
  const errored = formatted('**Partial** reply', 'error'); assert.equal(errored.output.querySelectorAll('strong').length, 0); assert.match(errored.output.textContent, /\*\*Partial\*\* reply\n\n\[Reply interrupted:/);

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
  assert.equal(count(imageChat, '/api/lm/chat'), 1); assert.match(imageChat.elements.chatStatus.textContent, /vision model/);
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

  const controls = chatReady(); assert.equal(controls.elements.chatSend.disabled, true, 'An empty composer must not advertise sending');
  controls.elements.chatInput.value = 'Prepared message'; controls.elements.chatInput.oninput(); assert.equal(controls.elements.chatSend.disabled, false);
  controls.attachments.setBusy(true); assert.equal(controls.elements.chatSend.disabled, true);
  controls.elements.chatForm.onsubmit(event()); assert.equal(count(controls, '/api/lm/chat'), 0, 'Image preparation must finish before sending');
  controls.attachments.setBusy(false); assert.equal(controls.elements.chatSend.disabled, false);
  controls.elements.chatInput.value = ''; controls.elements.chatInput.oninput(); controls.attachments.items = [image]; controls.attachments.setBusy(false);
  assert.equal(controls.elements.chatSend.disabled, false, 'A prepared image can be sent without text');
  controls.attachments.clear(); assert.equal(controls.elements.chatSend.disabled, true);
  controls.voice.transcript('Dictated text'); assert.equal(controls.elements.chatSend.disabled, false, 'Manual dictation must update composer controls');

  const keyboard = chatReady(); keyboard.elements.chatInput.value = 'First line\nSecond line'; keyboard.elements.chatInput.oninput(); let prevented = 0;
  function key(overrides) { return {key: 'Enter', keyCode: 13, preventDefault() { prevented++; }, ...overrides}; }
  for (const newline of [{}, {shiftKey: true}, {ctrlKey: true, isComposing: true}, {ctrlKey: true, keyCode: 229}, {ctrlKey: true, shiftKey: true}]) keyboard.elements.chatInput.onkeydown(key(newline));
  assert.equal(prevented, 0); assert.equal(count(keyboard, '/api/lm/chat'), 0, 'Plain/Shift Enter and composition must keep their native text behavior');
  keyboard.elements.chatInput.onkeydown(key({ctrlKey: true})); assert.equal(prevented, 1); assert.equal(JSON.parse(keyboard.pending('/api/lm/chat').body).messages[0].content, 'First line\nSecond line');
  keyboard.elements.chatStop.onclick(); keyboard.elements.chatClear.onclick(); keyboard.elements.chatInput.value = 'Mac send'; keyboard.elements.chatInput.onkeydown(key({metaKey: true}));
  assert.equal(count(keyboard, '/api/lm/chat'), 2);

  const history = chatReady(); history.elements.chatInput.value = 'Keep this'; history.elements.chatForm.onsubmit(event());
  const historyChat = history.pending('/api/lm/chat'); progress(historyChat, {text: 'Retained answer'}); progress(historyChat, {done: true});
  const firstBubble = history.elements.chatMessages.children[0]; history.elements.chatMessages.scrollTop = 23; history.elements.chatMessages.scrollHeight = 1200;
  history.elements.chatSetup.open = false; history.page('home'); history.page('chat'); history.models(catalog());
  assert.equal(history.elements.chatMessages.children[0], firstBubble); assert.equal(history.elements.chatMessages.scrollTop, 23, 'Reopening Chat must preserve the history reading position');
  assert.equal(history.elements.chatSetup.open, false); assert.equal(history.elements.chatStatus.textContent, 'Reply complete.');
  const noModels = {...catalog(), models: []}; history.elements.refreshModels.onclick(); history.models(noModels);
  assert.equal(history.elements.chatSetup.open, false, 'Background model changes must not reopen collapsed setup');
  assert.equal(history.elements.chatStatus.textContent, 'Reply complete.', 'Polling must not overwrite completed conversation feedback');
  history.elements.chatInput.value = 'Another message'; history.elements.chatInput.oninput(); assert.equal(history.elements.chatSend.disabled, true);
  history.elements.chatForm.onsubmit(event()); assert.equal(history.elements.chatSetup.open, true, 'Explicit send without an available model can reveal setup');
  const initial = harness(); initial.page('chat'); initial.elements.chatSetup.open = false; initial.models(noModels); assert.equal(initial.elements.chatSetup.open, false);
  const missingLiveModel = chatReady(); missingLiveModel.elements.liveVoice.onclick(); missingLiveModel.elements.refreshModels.onclick(); missingLiveModel.models(noModels);
  missingLiveModel.voice.transcript('Keep my dictated words'); assert.equal(missingLiveModel.voice.live(), false, 'A disappeared model must end the live cycle instead of leaving it stalled');
  assert.equal(count(missingLiveModel, '/api/lm/chat'), 0); assert.equal(missingLiveModel.elements.chatInput.value, 'Keep my dictated words');
  const follow = chatReady(); follow.elements.chatInput.value = 'Long reply'; follow.elements.chatForm.onsubmit(event());
  const followBox = follow.elements.chatMessages, followOutput = followBox.children[1].children[1];
  Object.defineProperty(followOutput, 'textContent', {set(value) { this._text = value; followBox.scrollHeight += 600; }});
  followBox.scrollHeight = 600; followBox.scrollTop = 300; followBox.clientHeight = 300;
  progress(follow.pending('/api/lm/chat'), {text: 'A large streamed chunk'}); assert.equal(followBox.scrollTop, 1200, 'A large chunk must follow a reader who was already at the bottom');
  followBox.scrollTop = 17; progress(follow.pending('/api/lm/chat'), {text: 'More streamed words'}); assert.equal(followBox.scrollTop, 17, 'Streaming must preserve an older history reading position');

  const stars = harness(); stars.page('apps'); stars.pending('/api/pc/apps').respond({ok: true, apps: [{id: 'a', name: 'A', favourite: false}, {id: 'b', name: 'B', favourite: false}]});
  function buttons() { return stars.elements.appCards.querySelectorAll('button').filter(button => button.className.split(' ').includes('star')); }
  const oldStars = buttons(); oldStars[0].onclick(); oldStars[1].onclick(); assert.equal(count(stars, '/api/pc/favourites'), 1, 'Rapid favourite taps must not send overlapping stale snapshots');
  assert.ok(buttons().every(button => button.disabled)); assert.deepEqual(JSON.parse(stars.pending('/api/pc/favourites').body).ids, ['a']);
  stars.pending('/api/pc/favourites').respond({ok: true}); assert.ok(buttons().every(button => !button.disabled)); buttons()[1].onclick();
  assert.deepEqual(JSON.parse(stars.pending('/api/pc/favourites').body).ids, ['a', 'b'], 'The next favourite edit must include the completed prior save');
  stars.pending('/api/pc/favourites').respond({ok: false, message: 'Synthetic failure'}, 400); assert.ok(buttons().every(button => !button.disabled));
  stars.elements.appSearch.value = '  unmatched query  '; stars.elements.appSearch.oninput();
  assert.match(stars.elements.appCards.textContent, /No apps match your search/); assert.equal(stars.elements.appCards.children[0].getAttribute('role'), 'status');
  stars.elements.appSearch.value = '   '; stars.elements.appSearch.oninput(); assert.equal(stars.elements.appCards.children.length, 2, 'Whitespace-only search must show the app list');
  const emptyApps = harness(); emptyApps.page('apps'); emptyApps.pending('/api/pc/apps').respond({ok: true, apps: []});
  assert.match(emptyApps.elements.appCards.textContent, /Refresh to check again/);
  emptyApps.elements.favouritesOnly.onclick(); assert.match(emptyApps.elements.appCards.textContent, /Tap a star in All apps/);
  assert.match(emptyApps.elements.appCards.children[0].className, /empty-state/);
  const gallery = harness(); gallery.page('comfy');
  const idleComfy = {ok: true, available: true, queue: {running: 0, pending: 0}, outputs: [], message: 'Ready fixture'};
  gallery.pending('/api/comfy/status').respond(idleComfy); assert.match(gallery.elements.comfyOutputs.textContent, /No recent outputs yet/);
  assert.match(gallery.elements.comfyOutputs.children[0].className, /empty-state/);
  gallery.elements.comfyRefresh.onclick(); gallery.pending('/api/comfy/status').respond({...idleComfy, available: false});
  assert.match(gallery.elements.comfyOutputs.textContent, /Start ComfyUI on your PC/);
  gallery.elements.comfyRefresh.onclick(); gallery.pending('/api/comfy/status').respond(idleComfy);
  assert.match(gallery.elements.comfyOutputs.textContent, /No recent outputs yet/, 'Availability changes must replace the otherwise-identical empty gallery');
  assert.ok(gallery.requests.filter(request => request.url === '/api/comfy/status').every(request => request.method === 'GET'));
  const initialAccess = harness({hash: '#access'}); assert.equal(initialAccess.elements.remoteAccess.open, true, 'Initial device-approval deep link must open its collapsed settings');
  const linkedAccess = harness(); linkedAccess.elements.remoteAccess.open = false; linkedAccess.navigateHash('#access'); assert.equal(linkedAccess.elements.remoteAccess.open, true);

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

  const noNative = harness(); assert.equal(noNative.elements.appFullscreen.textContent, 'App mode');
  function activationWatchdog(test) { const timer = Array.from(test.timers.values()).find(item => item.ms === 1500); assert.ok(timer, 'Expected a bounded fullscreen activation check'); return timer; }
  noNative.page('apps'); noNative.elements.appFullscreen.onclick(); assert.equal(noNative.document.body.getAttribute('data-page'), 'home');
  assert.equal(noNative.elements.appModeHelp.open, true); assert.equal(noNative.document.activeElement, noNative.elements.appModeHelp);
  assert.equal(noNative.document.body.getAttribute('data-expanded'), null); assert.equal(noNative.elements.appFullscreen.getAttribute('aria-pressed'), 'false');
  for (const mode of [{standalone: true}, {displayMode: true}]) {
    const installed = harness(mode);
    assert.equal(installed.elements.appFullscreen.textContent, 'App mode'); installed.elements.appFullscreen.onclick();
    assert.match(installed.elements.appModeStatus.textContent, /Already running in app mode/);
    let calls = 0; const installedPC = harness({...mode, configure({root}) { root.requestFullscreen = () => { calls++; }; }});
    assert.equal(installedPC.elements.appFullscreen.textContent, 'Full screen'); installedPC.elements.appFullscreen.onclick(); assert.equal(calls, 1, 'Installed desktop apps must retain supported native fullscreen');
    assert.notEqual(installedPC.elements.appModeHelp.open, true);
  }
  let forbidden = 0; const denied = harness({configure({root, document}) { document.fullscreenEnabled = false; root.requestFullscreen = () => { forbidden++; }; }});
  denied.elements.appFullscreen.onclick(); assert.equal(forbidden, 0); assert.equal(denied.elements.appModeHelp.open, true);
  const browser = harness(); browser.page('desktop'); browser.elements.desktopFullscreen.focus(); browser.elements.desktopFullscreen.onclick();
  assert.equal(browser.document.body.getAttribute('data-expanded'), 'desktop'); assert.match(browser.elements.desktopFrame.className, /browser-expanded/);
  assert.equal(browser.elements.expandedExit.hidden, false); assert.equal(browser.document.activeElement, browser.elements.expandedExit);
  assert.equal(browser.elements.appFullscreen.getAttribute('aria-pressed'), 'false', 'Browser expansion must not claim native fullscreen');
  browser.elements.expandedExit.onclick(); assert.equal(browser.document.body.getAttribute('data-expanded'), null); assert.equal(browser.elements.expandedExit.hidden, true);
  assert.equal(browser.document.activeElement, browser.elements.desktopFullscreen, 'Exit must restore the initiating focus');
  browser.elements.desktopFullscreen.onclick(); let escape = false; browser.emitDocument('keydown', {key: 'Escape', preventDefault() { escape = true; }});
  assert.equal(escape, true); assert.equal(browser.document.body.getAttribute('data-expanded'), null);
  browser.elements.desktopFullscreen.onclick(); browser.page('home'); assert.equal(browser.document.body.getAttribute('data-expanded'), null, 'Navigation must exit browser expansion');

  let entered = 0, exited = 0; const native = harness({configure({root, document}) { root.requestFullscreen = () => { entered++; return Promise.resolve(); }; document.exitFullscreen = () => { exited++; return Promise.resolve(); }; }});
  assert.equal(native.elements.appFullscreen.textContent, 'Full screen'); native.elements.appFullscreen.onclick(); assert.equal(entered, 1);
  const nativeWatchdog = activationWatchdog(native);
  assert.equal(native.elements.appFullscreen.textContent, 'Full screen', 'A request alone must not claim fullscreen succeeded');
  native.document.fullscreenElement = native.root; native.emitDocument('fullscreenchange'); assert.equal(native.elements.appFullscreen.textContent, 'Exit full screen');
  assert.ok(!Array.from(native.timers.values()).includes(nativeWatchdog), 'Genuine fullscreen must cancel its activation check'); nativeWatchdog.fn(); assert.notEqual(native.elements.appModeHelp.open, true);
  native.elements.appFullscreen.onclick(); assert.equal(exited, 1); native.document.fullscreenElement = null; native.emitDocument('fullscreenchange'); assert.equal(native.elements.appFullscreen.textContent, 'Full screen');
  native.document.fullscreenElement = native.root; native.emitDocument('fullscreenchange'); native.document.exitFullscreen = () => Promise.reject(new Error('Synthetic exit denial'));
  native.elements.appFullscreen.onclick(); await ready(); assert.match(native.elements.toast.textContent, /Could not exit full screen/); assert.equal(native.elements.appFullscreen.getAttribute('aria-pressed'), 'true');
  let prefixedEntry = 0, prefixedExit = 0; const prefix = harness({configure({root, document}) { root.webkitRequestFullscreen = () => { prefixedEntry++; }; document.webkitExitFullscreen = () => { prefixedExit++; }; }});
  prefix.elements.appFullscreen.onclick(); assert.equal(prefixedEntry, 1); prefix.document.webkitFullscreenElement = prefix.root; prefix.emitDocument('webkitfullscreenchange');
  prefix.elements.appFullscreen.onclick(); assert.equal(prefixedExit, 1);
  const rejected = harness({configure({root}) { root.requestFullscreen = () => Promise.reject(new Error('Synthetic restriction')); }});
  rejected.elements.appFullscreen.onclick(); await ready(); assert.match(rejected.elements.toast.textContent, /blocked by the browser/); assert.equal(rejected.elements.appModeHelp.open, true);
  rejected.page('desktop'); rejected.elements.desktopFrame.requestFullscreen = () => Promise.reject(new Error('Synthetic restriction')); rejected.elements.desktopFullscreen.onclick(); await ready();
  assert.equal(rejected.document.body.getAttribute('data-expanded'), 'desktop'); assert.match(rejected.elements.toast.textContent, /Expanded within the browser/);
  rejected.elements.expandedExit.onclick(); rejected.root.requestFullscreen = () => { throw new Error('Synthetic unsupported fullscreen'); };
  rejected.elements.appFullscreen.onclick(); assert.match(rejected.elements.toast.textContent, /unavailable/);
  const late = harness(); let rejectLate; late.page('desktop'); late.elements.desktopFrame.requestFullscreen = () => new Promise((_resolve, reject) => { rejectLate = reject; });
  late.elements.desktopFullscreen.onclick(); late.page('apps'); rejectLate(new Error('Late denial')); await ready(); assert.equal(late.document.body.getAttribute('data-expanded'), null);
  const silentRoot = harness({configure({root}) { root.webkitRequestFullscreen = function () {}; }}); silentRoot.elements.appFullscreen.onclick();
  assert.equal(silentRoot.elements.appFullscreen.textContent, 'Full screen'); activationWatchdog(silentRoot).fn(); assert.equal(silentRoot.elements.appModeHelp.open, true);
  assert.equal(silentRoot.elements.appFullscreen.getAttribute('aria-pressed'), 'false', 'A silently ignored request must not report native fullscreen');
  const resolved = harness({configure({elements}) { elements.desktopFrame.requestFullscreen = () => Promise.resolve(); }}); resolved.page('desktop'); resolved.elements.desktopFullscreen.onclick(); await ready();
  assert.equal(resolved.document.body.getAttribute('data-expanded'), 'desktop', 'Desktop expansion must be immediate, before native fullscreen responds'); activationWatchdog(resolved).fn(); assert.equal(resolved.document.body.getAttribute('data-expanded'), 'desktop', 'A resolved promise without native activation must retain browser expansion');
  resolved.document.fullscreenElement = resolved.elements.desktopFrame; resolved.emitDocument('fullscreenchange'); assert.equal(resolved.document.body.getAttribute('data-expanded'), null, 'Late actual native success must clear overlapping browser expansion');
  const errors = harness({configure({elements}) { elements.desktopFrame.webkitRequestFullscreen = function () {}; }}); errors.page('desktop'); errors.elements.desktopFullscreen.onclick();
  const errorWatchdog = activationWatchdog(errors); errors.emitDocument('webkitfullscreenerror', {target: errors.elements.chatInput}); assert.ok(Array.from(errors.timers.values()).includes(errorWatchdog));
  errors.emitDocument('webkitfullscreenerror', {target: errors.elements.desktopFrame}); assert.equal(errors.document.body.getAttribute('data-expanded'), 'desktop');
  assert.ok(!Array.from(errors.timers.values()).includes(errorWatchdog)); errors.elements.expandedExit.onclick(); errorWatchdog.fn(); assert.equal(errors.document.body.getAttribute('data-expanded'), null, 'A stale watchdog after user Exit must not reopen expansion');
  errors.elements.desktopFullscreen.onclick(); const navigationWatchdog = activationWatchdog(errors); errors.page('home'); navigationWatchdog.fn(); errors.emitDocument('fullscreenerror', {target: errors.elements.desktopFrame});
  assert.equal(errors.document.body.getAttribute('data-expanded'), null, 'Navigation must cancel both activation timeout and error fallback');
  const trueState = harness({configure({elements}) { elements.desktopFrame.requestFullscreen = function () {}; }}); trueState.page('desktop'); trueState.elements.desktopFullscreen.onclick();
  trueState.document.fullscreenElement = trueState.elements.desktopFrame; trueState.emitDocument('fullscreenerror', {target: trueState.elements.desktopFrame});
  assert.equal(trueState.document.body.getAttribute('data-expanded'), null, 'An error event must check genuine native state before falling back');
  function connectedViewer(options) {
    const test = harness(options); test.page('desktop'); test.elements.desktopConnect.onclick();
    test.pending('/api/control-session').respond({ok: true, token: 'synthetic-view-session'});
    test.pending('/api/desktop/session').respond({ok: true, credentials: {}, port: 8841}); test.message({type: 'maic-viewer-ready'});
    test.connectionId = test.sent.find(item => item.data.type === 'maic-viewer-connect').data.connectionId;
    test.display = () => test.sent.filter(item => item.data.type === 'maic-viewer-display').at(-1).data;
    test.requestView = expanded => test.message({type: 'maic-viewer-fullscreen', expanded, connectionId: test.connectionId});
    return test;
  }
  const iosView = connectedViewer(); assert.equal(iosView.display().expanded, false);
  const viewerSource = iosView.elements.viewer.src, viewRequests = iosView.requests.length;
  iosView.requestView(true); assert.equal(iosView.document.body.getAttribute('data-expanded'), 'desktop'); assert.equal(iosView.display().expanded, true);
  assert.equal(iosView.elements.desktopFullscreen.textContent, 'Exit view'); assert.equal(iosView.elements.expandedExit.textContent, 'Exit view');
  iosView.requestView(true); assert.equal(iosView.display().expanded, true, 'Repeated enter requests must not accidentally exit');
  iosView.requestView(false); assert.equal(iosView.document.body.getAttribute('data-expanded'), null); assert.equal(iosView.display().expanded, false);
  iosView.requestView(false); assert.equal(iosView.display().expanded, false, 'Repeated exits must not reopen');
  iosView.elements.desktopFullscreen.onclick(); assert.equal(iosView.display().expanded, true, 'Outer controls must update the iframe state');
  iosView.elements.expandedExit.onclick(); assert.equal(iosView.display().expanded, false);
  assert.equal(iosView.elements.viewer.src, viewerSource); assert.equal(iosView.requests.length, viewRequests, 'Expanding and exiting must preserve the existing desktop session');
  const command = {type: 'maic-viewer-fullscreen', expanded: true, connectionId: iosView.connectionId};
  iosView.message(command, 'https://wrong.invalid'); iosView.message(command, undefined, {});
  iosView.message({...command, connectionId: -1}); iosView.message({type: command.type});
  assert.equal(iosView.document.body.getAttribute('data-expanded'), null, 'Spoofed, stale and unversioned commands must not expand the desktop');
  iosView.page('home'); iosView.message(command); assert.equal(iosView.document.body.getAttribute('data-expanded'), null, 'A delayed viewer command must not expand a hidden Desktop page');
  iosView.page('desktop'); iosView.elements.desktopDisconnect.onclick(); iosView.message(command); assert.equal(iosView.document.body.getAttribute('data-expanded'), null);
  const pendingView = connectedViewer({configure({elements}) { elements.desktopFrame.requestFullscreen = function () {}; }});
  pendingView.requestView(true); const viewWatchdog = activationWatchdog(pendingView); pendingView.requestView(false); viewWatchdog.fn();
  assert.equal(pendingView.document.body.getAttribute('data-expanded'), null, 'Exit must cancel an unresolved native request fallback');
  const rootFullscreen = connectedViewer(); rootFullscreen.document.fullscreenElement = rootFullscreen.root;
  rootFullscreen.requestView(true); assert.equal(rootFullscreen.document.body.getAttribute('data-expanded'), 'desktop');
  rootFullscreen.requestView(false); assert.equal(rootFullscreen.document.fullscreenElement, rootFullscreen.root, 'Exit view restores the dashboard within existing app fullscreen');
  let desktopNativeExits = 0;
  const nativeViewer = connectedViewer({configure({document}) { document.exitFullscreen = () => { desktopNativeExits++; }; }});
  nativeViewer.document.fullscreenElement = nativeViewer.elements.desktopFrame; nativeViewer.emitDocument('fullscreenchange');
  assert.equal(nativeViewer.display().expanded, true); nativeViewer.requestView(false); assert.equal(desktopNativeExits, 1);
  nativeViewer.document.fullscreenElement = null; nativeViewer.emitDocument('fullscreenchange'); assert.equal(nativeViewer.display().expanded, false);
  nativeViewer.document.fullscreenElement = nativeViewer.elements.desktopFrame; nativeViewer.page('apps'); assert.equal(desktopNativeExits, 2, 'Leaving Desktop must exit native viewer fullscreen');

  const media = harness(); media.page('comfy'); media.pending('/api/comfy/status').respond({...idleComfy, outputs: [{id: 'image', name: 'Image', type: 'image', previewUrl: '/api/comfy/output/image'}]});
  const mediaCard = media.elements.comfyOutputs.children[0], imageElement = mediaCard.querySelector('img'); mediaCard.querySelector('button').focus(); mediaCard.querySelector('button').onclick();
  assert.equal(media.document.body.getAttribute('data-expanded'), 'media'); assert.match(imageElement.className, /browser-expanded/);
  media.elements.comfyRefresh.onclick(); media.pending('/api/comfy/status').respond(idleComfy); assert.equal(media.elements.comfyOutputs.children[0], mediaCard, 'Polling must preserve expanded media until Exit');
  media.elements.expandedExit.onclick(); media.elements.comfyRefresh.onclick(); media.pending('/api/comfy/status').respond(idleComfy); assert.match(media.elements.comfyOutputs.textContent, /No recent outputs/);
  media.elements.comfyRefresh.onclick(); media.pending('/api/comfy/status').respond({...idleComfy, outputs: [{id: 'video', name: 'Video', type: 'video', previewUrl: '/api/comfy/output/video'}]});
  const videoCard = media.elements.comfyOutputs.children[0], video = videoCard.querySelector('video'); let videoNative = 0;
  video.webkitEnterFullscreen = () => { videoNative++; }; videoCard.querySelectorAll('button').find(button => button.textContent === 'Full screen').onclick();
  assert.equal(videoNative, 1); assert.equal(media.document.body.getAttribute('data-expanded'), null, 'Native video player must remain separate from browser expansion');
  video.webkitEnterFullscreen = null; video.webkitEnterFullScreen = () => { videoNative++; }; videoCard.querySelectorAll('button').find(button => button.textContent === 'Full screen').onclick(); assert.equal(videoNative, 2);
  const videoWatchdog = activationWatchdog(media); video.listeners.webkitbeginfullscreen[0](); assert.ok(!Array.from(media.timers.values()).includes(videoWatchdog));
  videoWatchdog.fn(); assert.equal(media.document.body.getAttribute('data-expanded'), null, 'The separate native video player must cancel the document-fullscreen watchdog');
  const viewportEvents = {}; const viewport = harness({configure({window}) { window.visualViewport = {height: 390.5, offsetTop: 20.6, addEventListener(name, callback) { viewportEvents[name] = callback; }}; }});
  assert.equal(viewport.root.style['--viewport-top'], '21px'); assert.equal(viewport.root.style['--viewport-height'], '391px'); assert.ok(viewportEvents.scroll);
  console.log('Dashboard UI: polling/history, composer/keyboard, image guards/restoration, model selection, live voice, favourite serialization, desktop and fullscreen passed.');
}()).catch(error => { console.error(error); process.exitCode = 1; });
