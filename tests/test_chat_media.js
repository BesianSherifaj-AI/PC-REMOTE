// Real browser module with controlled files, decoding, canvas, XHR and audio.
// No device capture, filesystem images, network or installed model is used.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../web/chat-media.js'), 'utf8');
async function flush() { for (let i = 0; i < 8; i++) await Promise.resolve(); }
const photo = (overrides = {}) => ({name: 'fixture.png', type: 'image/png', size: 1024, ...overrides});

function harness() {
  const elements = {}, files = [], images = [], canvases = [], requests = [], statusRequests = [], created = [], revoked = [];
  let vision = true, sent = 0, token = 'synthetic-memory-session', canvasURL = 'data:image/jpeg;base64,/9j/fixture', errors = 0, ended = 0, stopOnError = false;
  const attachmentChanges = [];
  class Element {
    constructor(tag = 'div') { this.tagName = tag; this.children = []; this.disabled = false; this.hidden = false; this.value = ''; this._text = ''; this.attributes = {}; this.ended = false; this.paused = true; }
    set textContent(value) { this._text = String(value); this.children = []; }
    get textContent() { return this._text; }
    appendChild(child) { this.children.push(child); return child; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
    removeAttribute(name) { delete this.attributes[name]; if (name === 'src') this.src = ''; }
    pause() { this.paused = true; }
    load() { this.ended = false; }
    play() { this.paused = false; return Promise.resolve(); }
  }
  for (const id of ['chatImages', 'imageAttachments', 'imageStatus', 'replyAudio', 'ttsVoice', 'ttsRate', 'ttsStatus', 'readStatus', 'chatQuiet', 'ttsRefresh']) elements[id] = new Element();
  elements.ttsRate.value = '1.2';
  const document = {getElementById: id => elements[id], createElement(tag) {
    const element = new Element(tag);
    if (tag === 'canvas') {
      const draws = [], fills = [];
      element.getContext = () => ({set fillStyle(value) { fills.push(value); }, fillRect(...args) { fills.push(args); }, drawImage(...args) { draws.push(args); }});
      element.toDataURL = (type, quality) => { canvases.push({width: element.width, height: element.height, type, quality, draws, fills, element}); return canvasURL; };
    }
    return element;
  }};
  class FileReader {
    readAsDataURL(file) { this.file = file; this.kind = 'image'; files.push(this); }
    readAsText(blob) { this.blob = blob; this.kind = 'text'; files.push(this); }
    complete(result = 'data:image/png;base64,fixture') { this.result = result; this.onload(); }
  }
  class Image {
    set src(value) { this.url = value; images.push(this); }
    complete(width = 3072, height = 2048) { this.width = width; this.height = height; this.onload(); }
  }
  class XMLHttpRequest {
    constructor() { this.headers = {}; requests.push(this); }
    open(method, url) { this.method = method; this.url = url; }
    setRequestHeader(name, value) { this.headers[name] = value; }
    send(body) { this.body = body; }
    abort() { this.aborted = true; }
    respond(status = 200, response = {kind: 'synthetic-audio-blob'}) { this.status = status; this.response = response; if (this.onload) this.onload(); }
  }
  const window = {};
  vm.runInNewContext(source, {window, document, FileReader, Image, XMLHttpRequest, Promise,
    URL: {createObjectURL(blob) { const url = 'blob:fixture/' + (created.length + 1); created.push({blob, url}); return url; }, revokeObjectURL(url) { revoked.push(url); }}}, {filename: 'web/chat-media.js'});
  const attachments = window.createMaicAttachments({vision: () => vision, sentCount: () => sent, onChange() { attachmentChanges.push({busy: attachments.busy(), count: attachments.get().length}); }});
  const reader = window.createMaicReader({token: () => token, get(url, callback, privateRequest) { statusRequests.push({url, callback, privateRequest}); }, onError() { errors++; if (stopOnError) reader.stop(); }, onEnded() { ended++; }});
  return {elements, files, images, canvases, requests, statusRequests, created, revoked, attachments, reader, attachmentChanges,
    capability(value) { vision = value; attachments.modelChanged(); }, sent(value) { sent = value; }, token(value) { token = value; }, outputURL(value) { canvasURL = value; },
    errors: () => errors, ended: () => ended,
    stopOnError(value) { stopOnError = value; },
    async choose(value) { elements.chatImages.files = value; elements.chatImages.value = 'synthetic'; elements.chatImages.onchange(); await flush(); },
    async decode(width, height) { const file = files.find(file => file.kind === 'image' && !file.read); assert.ok(file, 'Expected an image read');
      file.read = true; file.complete(); images[images.length - 1].complete(width, height); await flush(); },
    voiceReady(maxChars = 3000) { reader.load(); const request = statusRequests[statusRequests.length - 1]; assert.equal(request.url, '/api/tts/status'); assert.equal(request.privateRequest, true);
      request.callback(null, {ok: true, available: true, maxChars, message: 'Ready fixture', defaultVoice: 'voice-a', voices: [{id: 'voice-a', name: 'Voice A', language: 'en'}, {id: 'voice-b', name: 'Voice B', language: 'de'}]}); }
  };
}

(async function () {
  const idle = harness(); idle.capability(true); assert.equal(idle.elements.imageStatus.textContent, 'JPG, PNG or WebP · up to 3 images.');
  idle.capability(null); assert.equal(idle.elements.imageStatus.textContent, 'Choose a vision model to add images.');
  idle.capability(false); assert.equal(idle.elements.imageStatus.textContent, 'This model accepts text only.');
  const resize = harness(); await resize.choose([photo()]); assert.equal(resize.attachments.busy(), true); assert.equal(resize.elements.chatImages.disabled, true);
  await resize.decode(); const prepared = resize.attachments.get();
  assert.deepEqual(resize.attachmentChanges, [{busy: true, count: 0}, {busy: false, count: 1}], 'The composer must be notified when preparation starts and finishes');
  assert.equal(prepared.length, 1); assert.equal(prepared[0].name, 'fixture.png'); assert.match(prepared[0].url, /^data:image\/jpeg;base64,/);
  const canvas = resize.canvases[0]; assert.equal(canvas.width, 1536); assert.equal(canvas.height, 1024); assert.equal(canvas.type, 'image/jpeg'); assert.equal(canvas.quality, 0.86);
  assert.equal(canvas.fills[0], '#fff'); assert.deepEqual(canvas.fills[1], [0, 0, 1536, 1024]); assert.equal(canvas.draws.length, 1);
  assert.equal(canvas.element.width, 1, 'Preparation must release the full canvas allocation'); assert.equal(resize.attachments.busy(), false);
  prepared.pop(); assert.equal(resize.attachments.get().length, 1, 'get() must return a copy');
  resize.elements.imageAttachments.children[0].children[1].onclick(); assert.equal(resize.attachments.get().length, 0);
  assert.deepEqual(resize.attachmentChanges[2], {busy: false, count: 0});

  for (const invalid of [photo({type: 'image/svg+xml'}), photo({type: 'image/PNG'}), photo({size: 0}), photo({size: 12 * 1024 * 1024 + 1})]) {
    const test = harness(); await test.choose([invalid]); assert.equal(test.files.length, 0); assert.equal(test.attachments.busy(), false); assert.match(test.elements.imageStatus.textContent, /JPEG, PNG or WebP/);
  }
  for (const capability of [false, null, 'true']) { const test = harness(); test.capability(capability); await test.choose([photo()]); assert.equal(test.files.length, 0); assert.equal(test.elements.chatImages.disabled, true); }
  const count = harness(); count.sent(2); await count.choose([photo(), photo()]); assert.equal(count.files.length, 0); assert.match(count.elements.imageStatus.textContent, /Maximum 3/);
  const huge = harness(); await huge.choose([photo()]); await huge.decode(8000, 5000); assert.equal(huge.attachments.get().length, 0); assert.match(huge.elements.imageStatus.textContent, /too large/);
  const encoded = harness(); encoded.outputURL('data:image/jpeg;base64,' + 'A'.repeat(2796200)); await encoded.choose([photo()]); await encoded.decode(); assert.equal(encoded.attachments.get().length, 0); assert.match(encoded.elements.imageStatus.textContent, /still too large/);
  const unreadable = harness(); await unreadable.choose([photo()]); unreadable.files[0].onerror(); await flush(); assert.equal(unreadable.attachments.busy(), false); assert.match(unreadable.elements.imageStatus.textContent, /could not be read/);
  const decodeError = harness(); await decodeError.choose([photo()]); decodeError.files[0].complete(); decodeError.images[0].onerror(); await flush(); assert.match(decodeError.elements.imageStatus.textContent, /could not open/);
  const atomic = harness(); await atomic.choose([photo(), photo({type: 'image/svg+xml'})]); await atomic.decode(); assert.equal(atomic.attachments.get().length, 0, 'A failed batch must not leave a partially prepared image batch');

  const cancelled = harness(); await cancelled.choose([photo()]); cancelled.attachments.clear(); await cancelled.decode();
  assert.equal(cancelled.attachments.get().length, 0, 'Clear must invalidate the in-flight preparation'); assert.equal(cancelled.elements.chatImages.disabled, false);
  const race = harness(); await race.choose([photo({name: 'old.png'})]); race.attachments.clear(); await race.choose([photo({name: 'new.png'})]);
  await race.decode(); assert.equal(race.attachments.busy(), true); await race.decode(); assert.equal(race.attachments.get()[0].name, 'new.png');
  const changed = harness(); await changed.choose([photo()]); changed.capability(false); await changed.decode();
  assert.equal(changed.elements.chatImages.disabled, true, 'Async completion must respect a changed model capability');
  changed.attachments.clear(); assert.equal(changed.elements.chatImages.disabled, true, 'Clear must preserve the nonvision input guard');
  changed.attachments.restore([{url: 'data:image/jpeg;base64,fixture', name: 'restored'}]); assert.equal(changed.elements.chatImages.disabled, true);
  changed.capability(true); assert.equal(changed.elements.chatImages.disabled, false);

  const unavailable = harness(); unavailable.reader.speak('Reply'); assert.equal(unavailable.errors(), 1, 'An unavailable voice must end a live cycle'); assert.equal(unavailable.requests.length, 0);
  const limits = harness(); limits.voiceReady(10); limits.reader.speak('A reply that exceeds the limit'); assert.equal(limits.errors(), 1); assert.equal(limits.requests.length, 0); assert.match(limits.elements.ttsStatus.textContent, /shorter answer/);
  limits.token(null); limits.reader.speak('Reply'); assert.equal(limits.errors(), 2); assert.equal(limits.requests.length, 0);
  const audio = harness(); audio.voiceReady(); audio.elements.ttsVoice.value = 'voice-b'; audio.voiceReady(); assert.equal(audio.elements.ttsVoice.value, 'voice-b');
  assert.equal(audio.elements.readStatus.textContent, '', 'Idle voice status must stay inside the settings');
  audio.reader.speak('Hello world'); const first = audio.requests[0]; assert.equal(first.url, '/api/tts/speak'); assert.equal(first.method, 'POST'); assert.equal(first.responseType, 'blob');
  assert.equal(audio.elements.readStatus.textContent, 'Preparing audio…', 'Generating feedback must be available outside collapsed voice settings');
  assert.deepEqual(JSON.parse(first.body), {text: 'Hello world', voice: 'voice-b', speed: 1.2}); assert.ok(first.headers['X-MAIC-Control']);
  const oldLoad = first.onload; audio.reader.stop(); assert.equal(first.aborted, true); assert.equal(first.onload, null); first.status = 200; first.response = {}; oldLoad(); assert.equal(audio.created.length, 0, 'A stopped XHR must not create a playback URL');
  assert.equal(audio.elements.readStatus.textContent, '', 'Stop must clear visible reading feedback');
  audio.elements.replyAudio.ended = true; audio.elements.replyAudio.onended(); assert.equal(audio.ended(), 0, 'An end event after Stop must not restart live listening');
  audio.reader.speak('Fresh reply'); const second = audio.requests[1]; second.respond(); assert.equal(audio.elements.replyAudio.hidden, false); assert.equal(audio.created.length, 1);
  audio.elements.replyAudio.onended(); assert.equal(audio.ended(), 0, 'A queued end event from an older audio must not finish new playback');
  audio.elements.replyAudio.ended = true; audio.elements.replyAudio.onended(); assert.equal(audio.ended(), 1);
  assert.match(audio.elements.readStatus.textContent, /Finished reading/);
  audio.reader.speak('Replacement reply'); assert.deepEqual(audio.revoked, ['blob:fixture/1']); assert.equal(audio.elements.replyAudio.hidden, true);
  const replacement = audio.requests[2]; replacement.respond(); audio.reader.stop(); assert.deepEqual(audio.revoked, ['blob:fixture/1', 'blob:fixture/2']); assert.equal(audio.elements.replyAudio.src, '');

  const blocked = harness(); blocked.voiceReady(); blocked.elements.replyAudio.play = () => Promise.reject(new Error('Synthetic autoplay restriction'));
  blocked.reader.speak('Autoplay fixture'); blocked.requests[0].respond(); await flush(); assert.match(blocked.elements.ttsStatus.textContent, /Tap Play/); assert.equal(blocked.errors(), 0); assert.equal(blocked.elements.replyAudio.hidden, false);
  const pendingPlay = harness(); pendingPlay.voiceReady(); let rejectPlay; pendingPlay.elements.replyAudio.play = () => new Promise((_resolve, reject) => { rejectPlay = reject; });
  pendingPlay.reader.speak('Old audio'); pendingPlay.requests[0].respond(); pendingPlay.reader.speak('New audio'); const latestStatus = pendingPlay.elements.ttsStatus.textContent;
  rejectPlay(new Error('Late autoplay rejection')); await flush(); assert.equal(pendingPlay.elements.ttsStatus.textContent, latestStatus, 'Late playback rejection must not overwrite a newer request');
  const serverError = harness(); serverError.voiceReady(); serverError.reader.speak('Reply'); serverError.requests[0].respond(503, {});
  assert.equal(serverError.errors(), 1); serverError.files[0].complete(JSON.stringify({message: 'Synthetic voice unavailable'})); assert.equal(serverError.elements.ttsStatus.textContent, 'Synthetic voice unavailable');
  const network = harness(); network.voiceReady(); network.reader.speak('Reply'); network.requests[0].onerror(); assert.equal(network.errors(), 1); assert.equal(network.elements.chatQuiet.disabled, true);
  assert.match(network.elements.readStatus.textContent, /Could not reach/);
  const liveError = harness(); liveError.stopOnError(true); liveError.voiceReady(); liveError.reader.speak('Reply'); liveError.requests[0].respond(503, {});
  assert.match(liveError.elements.readStatus.textContent, /could not be generated/, 'Stopping the live cycle from onError must preserve its visible error');
  liveError.files[0].complete(JSON.stringify({message: 'Synthetic detailed error'})); assert.equal(liveError.elements.readStatus.textContent, 'Synthetic detailed error');
  liveError.reader.speak('Next reply'); liveError.files[0].complete(JSON.stringify({message: 'Stale error'})); assert.equal(liveError.elements.readStatus.textContent, 'Preparing audio…');
  console.log('Chat media: asynchronous image bounds/resizing/cancel/model guards, TTS request/cleanup/races/limits and blocked-autoplay feedback passed.');
}()).catch(error => { console.error(error); process.exitCode = 1; });
