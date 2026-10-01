// Exercise the actual viewer script with a bounded DOM/RFB model, without
// connecting to or sending input to the user's Windows session.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, '../web/desktop-viewer.html'), 'utf8');
const script = html.match(/<script type="module">([\s\S]*?)<\/script>/)[1].replace("import RFB from '/desktop/rfb.bundle.js';", '');
const vendorRoot = require('node:path').join(__dirname, '../web/vendor/novnc/core');
const nativeRfb = fs.readFileSync(require('node:path').join(vendorRoot, 'rfb.js'), 'utf8');
const nativeKeyboard = fs.readFileSync(require('node:path').join(vendorRoot, 'input/keyboard.js'), 'utf8');
const nativeGesture = fs.readFileSync(require('node:path').join(vendorRoot, 'input/gesturehandler.js'), 'utf8').replace('export default class GestureHandler', 'class GestureHandler');
function nativeMethod(source, name, target) {
  const match = source.match(new RegExp('^    ' + name + '\\([^]*?\\n    }', 'm'));
  assert.ok(match, 'Pinned noVNC method missing: ' + name);
  return target + '.prototype.' + name + ' = function' + match[0].slice(match[0].indexOf('(')) + ';';
}

function harness(https = false) {
  const elements = {}, windowListeners = {}, frames = new Map(), timers = new Map(), sent = [];
  let serial = 0, current, NativeGesture, previousTouches = [];
  class Element {
    constructor() { this.listeners = {}; this.attributes = {}; this.hidden = true; this.value = ''; this.style = {}; this.textContent = ''; }
    addEventListener(type, callback) { const values = this.listeners[type] || (this.listeners[type] = []); if (!values.includes(callback)) values.push(callback); }
    removeEventListener(type, callback) { this.listeners[type] = (this.listeners[type] || []).filter(value => value !== callback); }
    setAttribute(name, value) { this.attributes[name] = value; }
    getAttribute(name) { return this.attributes[name]; }
    focus() { if (document.activeElement === this) return; if (document.activeElement) document.activeElement.blur(); document.activeElement = this; this.dispatchEvent({type: 'focus'}); }
    blur() { if (document.activeElement === this) { document.activeElement = null; this.dispatchEvent({type: 'blur'}); } }
    dispatchEvent(event) { this.lastEvent = event; event.target = this; (this.listeners[event.type] || []).slice().forEach(callback => { if (!event.immediate) callback(event); }); }
  }
  for (const id of ['screen', 'status', 'control', 'scale', 'pan', 'viewOnly', 'rightClick', 'zoomLevel', 'gestureHint', 'zoomIn', 'zoomOut', 'zoomActual', 'quality', 'typing', 'keys', 'keyboard', 'scrollToggle', 'scroll', 'fullscreen', 'cad']) elements[id] = new Element();
  elements.quality.setAttribute('aria-pressed', 'false');
  const canvas = new Element(), viewport = {clientWidth: 1024, clientHeight: 420, style: {}, _left: 0, _top: 0};
  Object.defineProperties(viewport, {
    scrollLeft: {get() { return this._left; }, set(value) { this._left = Math.max(0, Math.min(Math.max(0, 1920 * current._display.scale - this.clientWidth), value)); }},
    scrollTop: {get() { return this._top; }, set(value) { this._top = Math.max(0, Math.min(Math.max(0, 1080 * current._display.scale - this.clientHeight), value)); }}
  });
  viewport.getBoundingClientRect = () => ({left: 0, top: 0, width: viewport.clientWidth, height: viewport.clientHeight});
  canvas.getBoundingClientRect = () => {
    const width = 1920 * current._display.scale, height = 1080 * current._display.scale;
    const left = Math.max(0, (viewport.clientWidth - width) / 2) - viewport.scrollLeft;
    const top = Math.max(0, (viewport.clientHeight - height) / 2) - viewport.scrollTop;
    return {left, top, right: left + width, bottom: top + height, width, height};
  };
  class FakeKeyboard {
    constructor() { this._target = canvas; this._keyDownList = {}; this._eventHandlers = {keydown() {}, keyup() {}, blur: () => this._allKeysUp()}; this.onkeyevent = (key, code, down) => current.sendKey(key, code, down); }
  }
  class RFB {
    constructor(target, url, options) { current = this; this.url = url; this.options = options; this._screen = viewport; this._canvas = canvas; this._display = {width: 1920, height: 1080, scale: 1}; this.listeners = {}; this.keys = []; this.trace = []; this.mouse = []; this._mouseButtonMask = 0; this._mouseMoveTimer = null; this._mousePos = {x: 0, y: 0}; this._keyboard = new FakeKeyboard(); this._gestures = new NativeGesture(); this._gestures.attach(canvas); this._rfbConnectionState = 'connecting'; ['gesturestart', 'gesturemove', 'gestureend'].forEach(type => canvas.addEventListener(type, event => this._handleGesture(event))); }
    _updateScale() { this._display.scale = this.scaleViewport ? Math.min(viewport.clientWidth / 1920, viewport.clientHeight / 1080) : 1; }
    set scaleViewport(value) { this._fit = value; this._updateScale(); }
    get scaleViewport() { return this._fit; }
    addEventListener(type, callback) { this.listeners[type] = callback; }
    get viewOnly() { return !!this._readonly; }
    set viewOnly(value) { this._readonly = value; this.publicViewOnlySet = true; if (this._keyboard) value ? this._keyboard.ungrab() : this._keyboard.grab(); }
    sendKey(key, code, down) { if (!this.viewOnly) { if (down === undefined) this.keys.push(key); this.trace.push({key, code, down}); } }
    sendCtrlAltDel() { if (!this.viewOnly) this.cad = true; }
    disconnect() { this.disconnected = true; }
    blur() { canvas.blur(); }
    _sendMouse(x, y, mask) { if (!this.viewOnly && this._rfbConnectionState === 'connected') this.mouse.push({x, y, mask}); }
    _fakeMouseMove(event, x, y) { this._mousePos = {x, y}; }
    _handleKeyEvent(key, code, down) { this.sendKey(key, code, down); }
  }
  const document = {activeElement: null, hidden: false, listeners: {}, addEventListener(type, callback) { this.listeners[type] = callback; }, getElementById: id => elements[id], querySelector: () => current ? canvas : null, querySelectorAll: () => []};
  const location = {protocol: https ? 'https:' : 'http:', host: https ? 'maic.example:443' : '192.0.2.10:8840', hostname: https ? 'maic.example' : '192.0.2.10', origin: https ? 'https://maic.example:443' : 'http://192.0.2.10:8840'};
  const parent = {postMessage: (message, origin) => sent.push({message, origin})};
  const window = {addEventListener(type, callback) { const values = windowListeners[type] || (windowListeners[type] = []); if (!values.includes(callback)) values.push(callback); }, removeEventListener(type, callback) { windowListeners[type] = (windowListeners[type] || []).filter(value => value !== callback); }, dispatchEvent(event) { (windowListeners[event.type] || []).slice().forEach(callback => callback(event)); if (event.type === 'mouseup') document.captureElement = null; }};
  class TestEvent { constructor(type, init) { this.type = type; Object.assign(this, init); } preventDefault() { this.prevented = true; } stopPropagation() { this.stopped = true; } stopImmediatePropagation() { this.stopped = true; this.immediate = true; } }
  const context = vm.createContext({RFB, FakeKeyboard, document, window, parent, location, Date, Math, String, Number, Log: {Debug() {}}, KeyTable: {XK_Control_L: 0xffe3}, clientToElement: (x,y,element) => { const rect = element.getBoundingClientRect(); return {x:x-rect.left,y:y-rect.top}; }, DOUBLE_TAP_TIMEOUT:1000, DOUBLE_TAP_THRESHOLD:50, GESTURE_SCRLSENS:50, GESTURE_ZOOMSENS:75, WheelEvent:TestEvent, MouseEvent:TestEvent, CustomEvent:TestEvent, setTimeout(callback) {timers.set(++serial,callback);return serial;}, clearTimeout(id) {timers.delete(id);}, requestAnimationFrame(callback) { frames.set(++serial, callback); return serial; }, cancelAnimationFrame(id) { frames.delete(id); }});
  vm.runInContext(nativeGesture, context); NativeGesture = vm.runInContext('GestureHandler',context);
  for (const name of ['_handleMouseButton', '_handleTapEvent', '_handleGesture']) vm.runInContext(nativeMethod(nativeRfb,name,'RFB'), context);
  for (const name of ['_sendKeyEvent','_allKeysUp','grab','ungrab']) vm.runInContext(nativeMethod(nativeKeyboard,name,'FakeKeyboard'), context);
  vm.runInContext(script, context);
  function message(data, origin = location.origin, source = parent) { windowListeners.message[0]({data, origin, source}); }
  function touch(type, points) {
    const touches = points.map((point,index) => ({clientX:point[0],clientY:point[1],identifier:point[2] || index+1}));
    const changedTouches = type === 'touchend' || type === 'touchcancel' ? previousTouches.filter(point => !touches.some(current => current.identifier === point.identifier)) : type === 'touchstart' ? touches.filter(point => !previousTouches.some(current => current.identifier === point.identifier)) : touches;
    const event = new TestEvent(type,{touches,changedTouches}); previousTouches = touches;
    (elements.screen.listeners[type] || []).forEach(callback => {if(!event.immediate)callback(event);});
    if(!event.immediate)canvas.dispatchEvent(event); return event;
  }
  function tick() { const callbacks = Array.from(frames.values()); frames.clear(); callbacks.forEach(callback => callback()); }
  function connected() { current._rfbConnectionState = 'connected'; current.listeners.connect(); }
  return {elements, canvas, viewport, frames, timers, sent, message, touch, tick, connected, windowListeners, window, document, TestEvent, rfb: () => current};
}
function close(actual, expected) { assert.ok(Math.abs(actual - expected) < 0.0001, actual + ' != ' + expected); }

const test = harness();
test.message({type: 'maic-viewer-connect', port: 8841}, 'https://unrelated.example');
assert.equal(test.rfb(), undefined);
test.message({type: 'maic-viewer-connect', port: 9000});
assert.equal(test.rfb(), undefined);
test.message({type: 'maic-viewer-connect', port: 8841, connectionId: 42, credentials: {password: 'test-memory-only'}});
const rfb = test.rfb();
test.connected();
assert.equal(test.sent[test.sent.length-1].message.state, 'connected');
assert.equal(test.sent[test.sent.length-1].message.connectionId, 42);
assert.equal(rfb.url, 'ws://192.0.2.10:8841/desktop/ws');
assert.ok(!rfb.url.includes('password'));
assert.equal(rfb.qualityLevel, 4);
close(rfb._display.scale, 420 / 1080);
assert.equal(test.touch('touchstart', [[400, 210]]).immediate, undefined, 'Control mode must reach the actual noVNC gesture handler');
test.touch('touchend', []);
test.elements.pan.onclick();
const start = test.touch('touchstart', [[400, 210], [624, 210]]);
assert.equal(start.prevented, true); assert.equal(start.stopped, true, 'Pan gestures must not reach remote Ctrl+wheel handling');
test.touch('touchmove', [[344, 210], [680, 210]]);
test.touch('touchmove', [[338, 210], [786, 210]]);
assert.equal(test.frames.size, 1, 'Multiple touch moves should share one render frame');
test.tick();
close(rfb._display.scale, 2 * 420 / 1080);
let rect = test.canvas.getBoundingClientRect();
close((562 - rect.left) / rfb._display.scale, 960);
close((210 - rect.top) / rfb._display.scale, 540);
const beforeDrag = test.viewport.scrollLeft;
test.touch('touchend', [[338, 210]]);
test.touch('touchmove', [[298, 210]]); test.tick();
close(test.viewport.scrollLeft, beforeDrag + 40);
test.touch('touchend', []);
test.elements.zoomActual.onclick(); close(rfb._display.scale, 1);
rfb._updateScale(); close(rfb._display.scale, 1, 'Zoom must survive noVNC resize callbacks');
test.touch('touchstart', [[450, 210], [574, 210]]);
test.touch('touchmove', [[0, 210], [1024, 210]]); test.tick();
close(rfb._display.scale, 3, 'Pinch should be bounded');
test.touch('touchcancel', []); assert.equal(test.frames.size, 0);
test.elements.scale.onclick();
close(rfb._display.scale, 420 / 1080); assert.equal(test.elements.pan.getAttribute('aria-pressed'), 'true', 'Fit preserves the chosen mode');
assert.equal(test.viewport.scrollLeft, 0); assert.equal(test.viewport.scrollTop, 0);
test.elements.zoomOut.onclick(); close(rfb._display.scale, 420 / 1080, 'Zoom out should stop at Fit');
test.elements.quality.onclick(); assert.equal(rfb.qualityLevel, 8); test.elements.quality.onclick(); assert.equal(rfb.qualityLevel, 4);
test.elements.fullscreen.onclick(); assert.equal(test.sent[test.sent.length - 1].message.type, 'maic-viewer-fullscreen');
test.elements.control.onclick();
test.elements.keys.value = 'A😀'; test.elements.typing.onsubmit({preventDefault() {}});
assert.deepEqual(rfb.keys, [65, 0x0101f600]);
test.elements.viewOnly.onclick(); assert.equal(rfb.viewOnly, true); assert.equal(rfb.publicViewOnlySet, true);
assert.equal(test.elements.viewOnly.getAttribute('aria-pressed'), 'true'); assert.equal(test.elements.keyboard.disabled, true);
test.elements.keys.value = 'blocked'; test.elements.typing.onsubmit({preventDefault() {}}); test.elements.cad.onclick();
assert.deepEqual(rfb.keys, [65, 0x0101f600]); assert.equal(rfb.cad, undefined);
test.elements.scale.onclick(); assert.equal(test.elements.viewOnly.getAttribute('aria-pressed'), 'true', 'Fit preserves View only');
assert.equal(test.elements.pan.getAttribute('aria-pressed'), 'false', 'Modes are mutually exclusive');
test.elements.zoomActual.onclick(); test.touch('touchstart', [[450, 210], [574, 210]]);
test.touch('touchmove', [[388, 210], [636, 210]]); test.tick(); close(rfb._display.scale, 2);
test.touch('touchend', []); test.elements.control.onclick(); assert.equal(rfb.viewOnly, false);
assert.equal(test.elements.pan.getAttribute('aria-pressed'), 'false'); assert.equal(test.elements.keyboard.disabled, false);

// Mode buttons are explicit and idempotent; zoom never silently disables input.
test.elements.control.onclick(); test.elements.control.onclick();
assert.equal(test.elements.control.getAttribute('aria-pressed'), 'true');
assert.equal(test.elements.viewOnly.getAttribute('aria-pressed'), 'false');
test.elements.zoomIn.onclick(); assert.equal(rfb.viewOnly, false);
test.elements.scale.onclick(); assert.equal(test.elements.control.getAttribute('aria-pressed'), 'true');
const clickCount = () => rfb.mouse.filter(event => event.mask === 1).length;
let clicks = clickCount();
for (let i=0;i<8;i++) { test.touch('touchstart', [[450,210,100+i]]); test.touch('touchend', []); }
assert.equal(clickCount(), clicks+8, 'Eight actual noVNC tap sequences must produce eight clicks');
assert.equal(rfb._mouseButtonMask, 0);

// These use the actual pinned keyboard and mouse-button methods. Verify release
// happens BEFORE viewOnly disables transmission, and native capture is released.
rfb._keyboard._sendKeyEvent(0xffe3,'ControlLeft',true);
rfb._handleMouseButton(400,200,true,1); test.document.captureElement = test.canvas;
test.elements.pan.onclick();
assert.equal(rfb._mouseButtonMask,0); assert.equal(rfb.mouse[rfb.mouse.length-1].mask,0);
assert.ok(rfb.trace.some(event=>event.code==='ControlLeft'&&event.down===false));
assert.equal(Object.keys(rfb._keyboard._keyDownList).length,0);
assert.equal(test.document.captureElement,null);
test.elements.control.onclick();

// Switch away while a native touch drag is active, then switch back before the
// old finger lifts. Its delayed end must neither click nor corrupt GestureHandler.
test.touch('touchstart', [[450,210,501]]); test.touch('touchmove', [[530,210,501]]);
assert.equal(rfb._mouseButtonMask,1);
test.elements.viewOnly.onclick(); test.elements.control.onclick();
let events = rfb.mouse.length;
test.touch('touchend', []); assert.equal(rfb.mouse.length,events);
test.touch('touchstart', [[450,210,502]]); test.touch('touchend', []);
assert.equal(rfb.mouse[rfb.mouse.length-2].mask,1); assert.equal(rfb._mouseButtonMask,0);
clicks=clickCount(); test.touch('touchstart',[[450,210,503]]); test.touch('touchcancel',[]);
assert.equal(clickCount(),clicks,'Cancelled touch must not become a click');
assert.equal(test.timers.size,0,'Interrupted gestures must not retain long-press timers');

// Native keys belong to the canvas, never to the local toolbar/text entry.
test.canvas.focus(); assert.equal((test.canvas.listeners.keydown||[]).length,1);
rfb._keyboard._sendKeyEvent(0xffe9,'AltLeft',true); test.elements.keys.focus();
assert.equal((test.canvas.listeners.keydown||[]).length,0);
assert.ok(rfb.trace.some(event=>event.code==='AltLeft'&&event.down===false));
test.canvas.focus(); assert.equal((test.canvas.listeners.keydown||[]).length,1);

test.message({type:'maic-viewer-suspend',connectionId:999}); assert.equal(rfb.viewOnly,false,'Stale parent generation must be ignored');
test.message({type:'maic-viewer-suspend',connectionId:42}); assert.equal(rfb.viewOnly,true);
assert.equal((test.canvas.listeners.keydown||[]).length,0);
test.message({type:'maic-viewer-resume',connectionId:42}); assert.equal(rfb.viewOnly,false);
test.elements.control.onclick(); test.elements.rightClick.onclick();
assert.equal(rfb.mouse[rfb.mouse.length-2].mask,4); assert.equal(rfb.mouse[rfb.mouse.length-1].mask,0);
rfb._handleMouseButton(300,200,true,1); test.window.dispatchEvent(new test.TestEvent('blur'));
assert.equal(rfb._mouseButtonMask,0);
test.windowListeners.pagehide[0](); assert.equal(rfb.disconnected, true); assert.equal(test.frames.size, 0);

const secure = harness(true);
secure.message({type: 'maic-viewer-connect', port: 8841, credentials: {password: 'memory-only'}});
  assert.equal(secure.rfb().url, 'wss://maic.example:443/desktop/ws');
  assert.ok(!secure.rfb().url.includes('memory-only'));
  for (const mode of ['control', 'pan', 'view', 'invalid']) {
    const reconnect = harness();
    reconnect.message({type: 'maic-viewer-connect', port: 8841, connectionId: 43, mode});
    const selected = mode === 'invalid' ? 'control' : mode;
    assert.equal(reconnect.sent.find(item => item.message.type === 'maic-viewer-status').message.mode, selected, 'Initial connecting status must preserve the validated parent mode');
    assert.equal(reconnect.rfb().viewOnly, true, 'Connecting desktop must reject input before handshake');
    reconnect.connected();
    assert.equal(reconnect.sent[reconnect.sent.length-1].message.mode, selected);
    assert.equal(reconnect.rfb().viewOnly, selected !== 'control', 'Reconnect preserves the mode and its Windows input gate');
    assert.equal(reconnect.elements[selected === 'view' ? 'viewOnly' : selected].getAttribute('aria-pressed'), 'true');
  }

// Control combines native Windows gestures with local two-finger navigation.
// Use the actual noVNC GestureHandler, including its pending long-press timers.
const combined = harness();
combined.message({type:'maic-viewer-connect',port:8841,connectionId:44,mode:'control'}); combined.connected();
const combinedRfb = combined.rfb(); combined.elements.zoomActual.onclick();
const remoteEvents = () => combinedRfb.mouse.length + combinedRfb.trace.length;
let baseline = remoteEvents();
combined.touch('touchstart',[[450,210,601],[574,210,602]]);
combined.touch('touchmove',[[388,210,601],[636,210,602]]); combined.tick();
close(combinedRfb._display.scale,2);
const panBefore = combined.viewport.scrollLeft;
combined.touch('touchmove',[[348,210,601],[596,210,602]]); combined.tick();
close(combined.viewport.scrollLeft,panBefore+40,'Two fingers pan in Control');
combined.touch('touchend',[[348,210,601]]);
combined.touch('touchmove',[[308,210,601]]); combined.tick();
combined.touch('touchend',[]);
assert.equal(remoteEvents(),baseline,'Two-finger pinch/pan and residual finger never send Windows mouse, wheel or Ctrl');
assert.equal(combined.elements.control.getAttribute('aria-pressed'),'true');
assert.equal(combinedRfb.viewOnly,false,'Control mode is unchanged after local navigation');
assert.equal(combined.timers.size,0);
combined.touch('touchstart',[[450,210,603]]); combined.touch('touchend',[]);
assert.equal(combinedRfb.mouse.filter(event=>event.mask===1).length,1,'Fresh single-finger tap works immediately after navigation');

// A second finger interrupts a pending single-finger tap before noVNC sees it.
baseline=remoteEvents();
combined.touch('touchstart',[[450,210,604]]); assert.ok(combined.timers.size>0);
combined.touch('touchstart',[[450,210,604],[574,210,605]]);
assert.equal(combined.timers.size,0,'Second finger cancels native long-press/two-touch timers');
combined.touch('touchend',[[450,210,604]]); combined.touch('touchend',[]);
assert.equal(remoteEvents(),baseline,'1→2→1→0 must not become a remote click');

// A second finger after a native drag releases the held Windows button once.
combined.touch('touchstart',[[450,210,606]]); combined.touch('touchmove',[[530,210,606]]);
assert.equal(combinedRfb._mouseButtonMask,1);
baseline=remoteEvents();
combined.touch('touchstart',[[530,210,606],[654,210,607]]);
assert.equal(combinedRfb._mouseButtonMask,0);
assert.equal(combinedRfb.mouse[combinedRfb.mouse.length-1].mask,0);
assert.equal(remoteEvents(),baseline+1,'Second finger sends only the required held-button release');
baseline=remoteEvents();
combined.touch('touchmove',[[490,210,606],[696,210,607]]); combined.tick();
combined.touch('touchend',[[490,210,606]]); combined.touch('touchend',[]);
assert.equal(remoteEvents(),baseline);

// Cancellation and lost focus cannot leak residual touches or poison the next tap.
for(const interruption of ['cancel','blur']) {
  baseline=remoteEvents();
  combined.touch('touchstart',[[450,210,608],[574,210,609]]);
  if(interruption==='cancel') combined.touch('touchcancel',[[450,210,608]]);
  else combined.window.dispatchEvent(new combined.TestEvent('blur'));
  combined.touch('touchmove',[[420,210,608]]); combined.touch('touchend',[]);
  assert.equal(remoteEvents(),baseline); assert.equal(combined.timers.size,0);
  combined.touch('touchstart',[[450,210,610]]); combined.touch('touchend',[]);
  assert.equal(remoteEvents(),baseline+2,'Fresh tap after '+interruption+' sends one down/up pair');
}
const ghost = new combined.TestEvent('mousedown',{button:0,sourceCapabilities:{firesTouchEvents:true}});
combined.elements.screen.dispatchEvent(ghost); assert.equal(ghost.immediate,true,'Touch-derived compatibility mouse is suppressed');
const mouse = new combined.TestEvent('mousedown',{button:0,sourceCapabilities:{firesTouchEvents:false}});
combined.elements.screen.dispatchEvent(mouse); assert.equal(mouse.immediate,undefined,'Real mouse stays available in Control');
console.log('Desktop viewer: actual noVNC repeated taps, combined Control pinch/pan, drag handoff, residual/cancelled touches, keys/focus, modes/reconnect, auth, HTTPS and fullscreen passed.');
