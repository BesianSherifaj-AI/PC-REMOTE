// Real browser recorder logic with synthetic PCM and mocked browser permission.
// No microphone is opened and no HTTP request leaves this test.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/voice.js'), 'utf8');
function harness({secure = true, brokenAudio = false, pendingPermission = false, brokenResume = false, brokenDisconnect = false, live = false} = {}) {
  const elements = {}, requests = [], appended = [], audio = [], streams = [], errors = [], idle = [];
  let grant;
  for (const id of ['chatSpeak', 'voiceCancel', 'voiceStatus', 'voiceLanguage']) elements[id] = {textContent: '', hidden: true, disabled: false, value: 'auto', setAttribute(name, value) { this[name] = value; }};
  function node() { return {connect() {}, disconnect() { if (brokenDisconnect) throw new Error('synthetic disconnected node'); }}; }
  class AudioContext {
    constructor() { if (brokenAudio) throw new Error('synthetic audio initialization failure'); this.sampleRate = 48000; audio.push(this); }
    resume() { return brokenResume ? Promise.reject(new Error('synthetic suspended audio')) : Promise.resolve(); }
    close() { this.closed = true; return Promise.resolve(); }
    createMediaStreamSource() { return node(); }
    createScriptProcessor() { this.processor = node(); return this.processor; }
    createGain() { const gain = node(); gain.gain = {}; return gain; }
  }
  function stream() { const track = {stop() { this.stopped = true; }}; const result = {getTracks: () => [track], track}; streams.push(result); return result; }
  class XMLHttpRequest {
    constructor() { this.status = 200; requests.push(this); }
    open(method, path) { this.method = method; this.path = path; }
    setRequestHeader(name, value) { (this.headers || (this.headers = {}))[name] = value; }
    send(payload) { this.payload = payload; }
    abort() { this.aborted = true; }
  }
  const window = {isSecureContext: secure, AudioContext};
  const navigator = {mediaDevices: {getUserMedia() { return pendingPermission ? new Promise(resolve => { grant = () => resolve(stream()); }) : Promise.resolve(stream()); }}};
  vm.runInNewContext(source, {window, navigator, document: {getElementById: id => elements[id]}, XMLHttpRequest,
    Float32Array, ArrayBuffer, DataView, Date, Math, setInterval: () => 1, clearInterval() {}});
  const voice = window.createMaicVoice({token: () => 'test-memory-token', append: value => appended.push(value), liveMode: () => live,
    onError: value => errors.push(value), onIdle: reason => idle.push(reason)});
  return {elements, requests, appended, audio, streams, errors, idle, voice, grant: () => grant(),
    record(value = 0.1, count = 3) { const samples = new Float32Array(4096); samples.fill(value); for (let i = 0; i < count; i++) {
      const callback = audio[audio.length - 1].processor.onaudioprocess; if (!callback) break;
      callback({inputBuffer: {getChannelData: () => samples}});
    } }};
}
async function ready() { await Promise.resolve(); await Promise.resolve(); }
(async function () {
  const test = harness(); test.elements.chatSpeak.onclick(); await ready();
  assert.equal(test.elements.chatSpeak.textContent, 'Finish speaking');
  test.record(); test.elements.chatSpeak.onclick();
  const xhr = test.requests[0], view = new DataView(xhr.payload);
  assert.equal(xhr.path, '/api/speech/transcribe'); assert.equal(xhr.headers['Content-Type'], 'audio/wav');
  assert.equal(view.getUint32(24, true), 16000); assert.equal(view.getUint16(22, true), 1); assert.equal(view.getUint16(34, true), 16);
  assert.equal(view.getUint32(40, true), 8192); assert.equal(xhr.payload.byteLength, 8236);
  assert.ok(test.streams[0].track.stopped); assert.ok(test.audio[0].closed);
  assert.equal(test.elements.voiceCancel.hidden, false, 'Upload must remain cancellable');
  const late = xhr.onload; test.voice.cancel();
  assert.ok(xhr.aborted); assert.equal(test.elements.chatSpeak.disabled, false);
  xhr.responseText = JSON.stringify({ok: true, text: 'stale transcript', message: 'done'}); late();
  assert.deepEqual(test.appended, [], 'A cancelled result must not enter the next conversation');
  test.elements.chatSpeak.onclick(); await ready(); test.record(); test.elements.chatSpeak.onclick();
  const successful = test.requests[1]; successful.responseText = JSON.stringify({ok: true, text: 'new transcript', message: 'Review text'}); successful.onload();
  assert.deepEqual(test.appended, ['new transcript']); assert.equal(test.elements.voiceCancel.hidden, true);
  test.elements.chatSpeak.onclick(); await ready(); test.record(); test.elements.chatSpeak.onclick();
  const denied = test.requests[2]; denied.status = 403; denied.responseText = JSON.stringify({ok: true, text: 'rejected text', message: 'Access expired'}); denied.onload();
  assert.deepEqual(test.appended, ['new transcript']); assert.match(test.elements.voiceStatus.textContent, /Access expired/);

  const pending = harness({pendingPermission: true}); pending.elements.chatSpeak.onclick(); pending.voice.cancel(); pending.grant(); await ready();
  assert.ok(pending.streams[0].track.stopped); assert.equal(pending.elements.chatSpeak.disabled, false);
  assert.equal(pending.requests.length, 0);
  const broken = harness({brokenAudio: true}); broken.elements.chatSpeak.onclick();
  assert.equal(broken.elements.chatSpeak.disabled, false); assert.match(broken.elements.voiceStatus.textContent, /unavailable/);
  const insecure = harness({secure: false}); insecure.elements.chatSpeak.onclick();
  assert.equal(insecure.audio.length, 0); assert.match(insecure.elements.voiceStatus.textContent, /HTTPS/);
  const ended = harness(); ended.elements.chatSpeak.onclick(); await ready();
  ended.streams[0].track.onended();
  assert.ok(ended.streams[0].track.stopped); assert.ok(ended.audio[0].closed);
  assert.equal(ended.elements.chatSpeak.disabled, false); assert.match(ended.elements.voiceStatus.textContent, /microphone stopped/);
  assert.equal(ended.requests.length, 0);
  const disconnect = harness({brokenDisconnect: true}); disconnect.elements.chatSpeak.onclick(); await ready(); disconnect.voice.cancel();
  assert.ok(disconnect.streams[0].track.stopped, 'Node disconnection errors must not leave the microphone open');
  assert.ok(disconnect.audio[0].closed);
  const suspended = harness({brokenResume: true, pendingPermission: true}); suspended.elements.chatSpeak.onclick(); await ready();
  assert.equal(suspended.elements.chatSpeak.disabled, false); assert.match(suspended.elements.voiceStatus.textContent, /could not start/);
  suspended.grant(); await ready(); assert.ok(suspended.streams[0].track.stopped); assert.equal(suspended.requests.length, 0);
  const maximum = harness(); maximum.elements.chatSpeak.onclick(); await ready();
  const samples = new Float32Array(4096); samples.fill(0.1);
  while (maximum.audio[0].processor.onaudioprocess) maximum.audio[0].processor.onaudioprocess({inputBuffer: {getChannelData: () => samples}});
  assert.equal(maximum.requests.length, 1); assert.equal(maximum.requests[0].payload.byteLength, 960044);
  assert.ok(maximum.streams[0].track.stopped); assert.ok(maximum.audio[0].closed);

  const live = harness({live: true}); assert.equal(live.audio.length, 0, 'Live voice must not open a microphone before start');
  live.voice.start(); await ready(); live.voice.start(); assert.equal(live.audio.length, 1, 'Repeated start must not finish or reopen an active recording');
  live.record(0.1, 4); live.record(0, 14); assert.equal(live.requests.length, 0, 'Silence under 1.2 seconds must not finish the utterance');
  live.record(0, 1); assert.equal(live.requests.length, 1, 'At least 300 ms speech followed by 1.2 seconds silence should transcribe');
  assert.ok(live.streams[0].track.stopped); assert.ok(live.audio[0].closed);
  const liveResult = live.requests[0]; liveResult.responseText = JSON.stringify({ok: true, text: 'Live transcript', message: 'Ready'}); liveResult.onload();
  assert.deepEqual(live.appended, ['Live transcript']); assert.deepEqual(live.idle, ['transcribed']);
  assert.equal(live.audio.length, 1, 'Successful transcription must wait for the caller to start the next recording');
  live.voice.start(); await ready(); assert.equal(live.audio.length, 2); live.voice.cancel(); assert.equal(live.errors.length, 1);
  const shortLive = harness({live: true}); shortLive.voice.start(); await ready();
  shortLive.record(0.1, 3); shortLive.record(0, 20); assert.equal(shortLive.requests.length, 0, 'A short noise burst must not qualify as speech');
  shortLive.voice.cancel(); assert.ok(shortLive.streams[0].track.stopped);
  const intermittent = harness({live: true}); intermittent.voice.start(); await ready();
  intermittent.record(0.1, 3); intermittent.record(0, 1); intermittent.record(0.1, 3); intermittent.record(0, 20);
  assert.equal(intermittent.requests.length, 0, 'Separated noise bursts must not accumulate into qualifying speech'); intermittent.voice.cancel();
  for (const amplitude of [0, 0.006]) {
    const quiet = harness({live: true}); quiet.voice.start(); await ready(); quiet.record(amplitude, 352);
    assert.equal(quiet.requests.length, 0, 'Silence/background noise must not be posted');
    assert.ok(quiet.streams[0].track.stopped); assert.ok(quiet.audio[0].closed); assert.equal(quiet.errors.length, 1);
    assert.match(quiet.elements.voiceStatus.textContent, /Live voice paused/);
  }
  const manual = harness(); manual.voice.start(); await ready(); manual.record(0.1, 4); manual.record(0, 16);
  assert.equal(manual.requests.length, 0, 'Manual dictation must not auto-submit on silence'); manual.elements.chatSpeak.onclick();
  assert.equal(manual.requests.length, 1); manual.voice.cancel(); assert.ok(manual.requests[0].aborted);
  console.log('Voice: PCM16 WAV, opt-in live VAD/silence/noise bounds, manual dictation, callbacks, cancellation/stale results and microphone cleanup passed.');
}()).catch(error => { console.error(error); process.exitCode = 1; });
