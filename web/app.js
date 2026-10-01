(function () {
  'use strict';
  var token = null, page = 'home', audio = null, apps = [], favouritesOnly = false, favouritesBusy = false, toastTimer;
  var chat = [], chatXHR = null, desktopCredentials = null, outputSignature = '', appsBusy = false;
  var modelsBusy = false, comfyBusy = false, hardwareBusy = false, modelLoading = false;
  var localPC = false, remoteBusy = false, pendingSignature = '', dismissedSignature = '';
  var audioBusy = false, audioChanging = 0, audioVersion = 0, volumeEditing = false, connectionBusy = false, sessionAt = 0;
  var liveVoice = false, quickSignature = '', pendingModelSelection = '';
  var loadedModels = [], modelsReady = false;
  var modelSignature = '', availableSignature = '', remoteSignature = '', healthBusy = false;
  var expandedTarget = null, expansionFocus = null, fullscreenGeneration = 0, pendingFullscreen = null;
  var desktopWanted = false, desktopState = 'disconnected', desktopMode = 'control', desktopGeneration = 0, desktopRetry = 0, desktopRetryTimer = null, desktopWatchdog = null;
  var voice = window.createMaicVoice({liveMode: function () { return liveVoice; }, onError: function () { stopLive(); }, onIdle: function (reason) { if (liveVoice && reason !== 'transcribed') { stopLive(); } }, token: function () { return token; }, append: function (value) {
    var input = el('chatInput'); input.value = (input.value ? input.value + ' ' : '') + value; updateChatControls(); input.scrollIntoView({block: 'nearest'}); if (liveVoice) { sendChat({preventDefault: function () {}}); }
  }});
  function selectedVision() { var match = loadedModels.filter(function (model) { return model.id === el('model').value; })[0]; return match ? match.vision : null; }
  function sentImageCount() { return chat.reduce(function (total, item) { return total + (Array.isArray(item.content) ? item.content.filter(function (part) { return part.type === 'image_url'; }).length : 0); }, 0); }
  var attachments = window.createMaicAttachments({vision: selectedVision, sentCount: sentImageCount, onChange: updateChatControls});
  var reader = window.createMaicReader({token: function () { return token; }, get: get, onError: function () { stopLive(); }, onEnded: function () { if (liveVoice && page === 'chat' && !document.hidden) { voice.start(); } }});
  function el(id) { return document.getElementById(id); }
  function text(id, value) { el(id).textContent = value; }
  function showAccess() { el('remoteAccess').open = true; el('remoteAccess').scrollIntoView({block: 'start'}); }
  function each(selector, fn) { Array.prototype.forEach.call(document.querySelectorAll(selector), fn); }
  function updateChatControls() {
    var selected = modelsReady && loadedModels.some(function (model) { return model.id === el('model').value; });
    el('chatSend').disabled = !token || !selected || !!chatXHR || modelLoading || !!(attachments && attachments.busy()) || !(el('chatInput').value.trim() || attachments && attachments.get().length);
  }
  function toast(message) { text('toast', message); el('toast').style.display = 'block'; clearTimeout(toastTimer); toastTimer = setTimeout(function () { el('toast').style.display = 'none'; }, 6000); }
  function request(method, path, body, callback, privateRequest) {
    var xhr = new XMLHttpRequest(), done = false;
    function finish(error, data) { if (!done) { done = true; callback(error, data); } }
    if (privateRequest && !token) { finish('PC access needs approval. Tap Reconnect.'); return null; }
    xhr.open(method, path, true); xhr.timeout = path === '/api/lm/load' ? 180000 : path === '/api/lm/start' ? 25000 : 12000;
    if (body !== null) { xhr.setRequestHeader('Content-Type', 'application/json'); }
    if (privateRequest) { xhr.setRequestHeader('X-MAIC-Control', token); }
    xhr.onload = function () {
      var data; try { data = JSON.parse(xhr.responseText); } catch (e) {}
      if (data && data.code === 'REMOTE_PAIRING_REQUIRED' && location.protocol === 'https:') { location.replace('/'); return; }
      if (privateRequest && xhr.status === 403 && path !== '/api/control-session') { sessionAt = 0; text('connection', 'Reconnect to renew access'); }
      finish(xhr.status >= 200 && xhr.status < 300 && data && data.ok ? null : data && data.message || 'The PC could not complete that request.', data);
    };
    xhr.onerror = xhr.ontimeout = function () { document.body.setAttribute('data-online', 'false'); finish('Cannot reach the PC. Check your connection and keep the PC online.'); };
    xhr.send(body === null ? null : JSON.stringify(body)); return xhr;
  }
  function get(path, callback, privateRequest) { return request('GET', path, null, callback, privateRequest); }
  function post(path, body, callback, privateRequest) { return request('POST', path, body, callback, privateRequest); }
  function connect() {
    if (connectionBusy) { return; } connectionBusy = true;
    token = null; updateChatControls(); text('connection', 'Connecting…');
    get('/api/control-session', function (error, data) {
      connectionBusy = false; sessionAt = error ? 0 : Date.now(); document.body.setAttribute('data-online', String(!error));
      token = error ? null : data.token; text('connection', error ? 'PC controls need approval' : location.protocol === 'https:' ? 'Connected · secure HTTPS' : 'Connected · local Wi-Fi');
      localPC = !error && !!data.canManageDevices && location.protocol === 'http:';
      el('localApprovalHint').hidden = !localPC;
      text('audioMessage', error || 'Controls affect your Windows PC.');
      loadWebsites(); if (token) { loadAudio(); loadHardware(); loadRemote(); if (page === 'apps') { loadApps(); } if (page === 'chat') { loadModels(); reader.load(); } if (page === 'comfy') { loadComfy(); } }
      if (location.hash === '#access') { showAccess(); }
      updateChatControls(); if (!error) { checkConnection(); }
    });
  }
  function showPage(name) {
    fullscreenGeneration++; cancelFullscreenRequest(); exitExpanded(false);
    if (nativeFullscreen() && nativeFullscreen() !== document.documentElement) { exitNativeFullscreen(); }
    if (['home', 'apps', 'desktop', 'chat', 'comfy'].indexOf(name) < 0) { name = 'home'; }
    page = name; each('.page', function (item) { item.hidden = item.id !== page; });
    document.body.setAttribute('data-page', name);
    each('nav button', function (item) { var selected = item.getAttribute('data-page') === page; item.className = selected ? 'selected' : ''; if (selected) { item.setAttribute('aria-current', 'page'); } else { item.removeAttribute('aria-current'); } });
    if (location.hash !== '#' + name) { history.replaceState(null, '', '#' + name); }
    window.scrollTo(0, 0);
    if (page !== 'chat') { stopLive(); voice.cancel(); stopReading(); }
    if (desktopState === 'connected') { el('viewer').contentWindow.postMessage({type: page === 'desktop' && !document.hidden ? 'maic-viewer-resume' : 'maic-viewer-suspend', connectionId: desktopGeneration}, location.origin); }
    if (page !== 'comfy') { each('#comfyOutputs video', function (video) { video.pause(); }); }
    if (page === 'apps') { loadApps(); } if (page === 'chat') { loadModels(); reader.load(); } if (page === 'comfy') { loadComfy(); }
    if (page === 'desktop' && token) { get('/api/desktop/status', function (err, data) { if (!desktopWanted) { text('desktopMessage', err || data.message); } }, true); if (desktopWanted && desktopState === 'disconnected') { scheduleDesktopRetry(); } }
  }
  function loadAudio() {
    if (!token || audioBusy || audioChanging || volumeEditing) { return; } audioBusy = true; var version = audioVersion;
    get('/api/audio', function (error, data) {
      audioBusy = false; if (version !== audioVersion) { loadAudio(); return; } if (audioChanging || volumeEditing) { return; }
      if (error) { text('audioMessage', error); return; } audio = data;
      el('volume').disabled = !data.speaker.available; el('speakerMute').disabled = !data.speaker.available;
      el('micMute').disabled = !data.microphone.available;
      if (data.speaker.available) { if (document.activeElement !== el('volume')) { el('volume').value = Math.round(data.speaker.volume); text('volumeLabel', Math.round(data.speaker.volume) + '%'); } text('speakerMute', data.speaker.muted ? 'Unmute speaker' : 'Mute speaker'); el('speakerMute').setAttribute('aria-pressed', String(data.speaker.muted)); }
      text('micMute', data.microphone.muted ? 'Unmute PC mic' : 'Mute PC mic');
      el('micMute').setAttribute('aria-pressed', String(data.microphone.muted));
    });
  }
  function control(action, value) { audioVersion++; audioChanging++; post('/api/control', {action: action, value: value}, function (error, data) { audioChanging--; text('audioMessage', error || data.message || 'Updated.'); if (!error) { loadAudio(); } }, true); }
  function loadHardware() {
    if (!token || hardwareBusy) { return; } hardwareBusy = true; get('/api/hardware', function (error, data) {
      hardwareBusy = false;
      if (error) { text('gpuDetail', error); return; }
      text('pcName', data.pcName); text('cpu', data.cpu && data.cpu.available ? data.cpu.percent.toFixed(0) + '%' : 'Unavailable');
      text('ram', data.memory ? data.memory.usedPercent + '%' : 'Unavailable');
      var gpu = data.gpu && data.gpu.devices && data.gpu.devices[0];
      text('gpu', gpu && typeof gpu.utilization === 'number' ? gpu.utilization + '%' : 'Unavailable');
      text('gpuDetail', gpu ? gpu.name + ' · ' + (typeof gpu.memoryUsedMB === 'number' ? (gpu.memoryUsedMB / 1024).toFixed(1) : '—') + ' / ' + (typeof gpu.memoryTotalMB === 'number' ? (gpu.memoryTotalMB / 1024).toFixed(1) : '—') + ' GB · ' + (typeof gpu.temperatureC === 'number' ? gpu.temperatureC + '°C' : 'Temperature unavailable') : 'GPU monitoring is unavailable.');
    }, true);
  }
  function loadWebsites() {
    get('/api/apps', function (error, data) {
      var container = el('websites'); container.textContent = '';
      if (error || !data.apps.length) { container.textContent = error || 'Save a website to get started.'; return; }
      data.apps.forEach(function (site) { var a = document.createElement('a'); a.textContent = site.name; a.href = site.url; container.appendChild(a); });
    });
  }
  function canSwitch(app) { return app.running && app.hasWindow !== false; }
  function appStatus(app) { return app.active ? 'Active on Windows' : app.running ? app.hasWindow === false ? 'Running in background' : 'Running on Windows' : app.kind === 'packaged' ? 'Windows app' : 'Desktop app'; }
  function renderApps() {
    var query = el('appSearch').value.trim().toLowerCase(), container = el('appCards'); container.textContent = '';
    var filtered = apps.filter(function (app) { return (!favouritesOnly || app.favourite) && app.name.toLowerCase().indexOf(query) !== -1; });
    filtered.sort(function (a, b) { return Number(b.favourite) - Number(a.favourite) || a.name.localeCompare(b.name); });
    filtered.forEach(function (app) {
      var card = document.createElement('article'); card.className = 'card app-card';
      var heading = document.createElement('div'); heading.className = 'app-heading';
      var letter = document.createElement('span'); letter.className = 'app-letter'; letter.textContent = app.name.charAt(0).toUpperCase();
      var name = document.createElement('strong'); name.textContent = app.name; heading.appendChild(letter); heading.appendChild(name);
      var status = document.createElement('div'); status.className = 'status' + (app.active ? ' active-dot' : ''); status.textContent = appStatus(app); status.setAttribute('data-status-id', app.id);
      var row = document.createElement('div'); row.className = 'row';
      var open = document.createElement('button'); open.textContent = canSwitch(app) ? 'Switch to app' : 'Open on PC'; open.setAttribute('data-open-id', app.id);
      open.onclick = function () { open.disabled = true; post('/api/pc/action', {action: canSwitch(app) ? 'activate' : 'open', id: app.id}, function (error, data) { open.disabled = false; text('appMessage', error || data.message || 'Opened on Windows.'); if (error) { toast(error); } loadAppState(); }, true); };
      var star = document.createElement('button'); star.className = 'quiet star' + (app.favourite ? ' favourited' : ''); star.textContent = app.favourite ? '★' : '☆'; star.setAttribute('aria-label', (app.favourite ? 'Remove favourite ' : 'Favourite ') + app.name);
      star.disabled = favouritesBusy;
      star.onclick = function () {
        if (favouritesBusy) { return; } favouritesBusy = true; each('#appCards .star', function (button) { button.disabled = true; });
        var ids = apps.filter(function (item) { return item.id === app.id ? !item.favourite : item.favourite; }).map(function (item) { return item.id; });
        post('/api/pc/favourites', {ids: ids}, function (error) { favouritesBusy = false; if (error) { toast(error); each('#appCards .star', function (button) { button.disabled = false; }); } else { apps.forEach(function (item) { item.favourite = ids.indexOf(item.id) !== -1; }); renderApps(); } }, true);
      };
      row.appendChild(open); row.appendChild(star); card.appendChild(heading); card.appendChild(status); card.appendChild(row); container.appendChild(card);
    });
    if (!filtered.length) {
      var empty = document.createElement('article'); empty.className = 'card empty-state'; empty.setAttribute('role', 'status');
      empty.textContent = query ? 'No apps match your search. Try another name' + (favouritesOnly ? ' or show all apps.' : '.') : favouritesOnly ? 'No favourites yet. Tap a star in All apps.' : 'No PC apps found. Refresh to check again.'; container.appendChild(empty);
    }
    text('appMessage', filtered.length + (filtered.length === 1 ? ' app' : ' apps') + (favouritesOnly ? ' · favourites' : ''));
  }
  function loadApps() {
    if (!token || appsBusy) { return; } appsBusy = true;
    get('/api/pc/apps', function (error, data) { appsBusy = false; if (error) { text('appMessage', error); return; } apps = data.apps; renderApps(); }, true);
  }
  function loadAppState() {
    if (!token || appsBusy) { return; } appsBusy = true;
    get('/api/pc/state', function (error, data) {
      appsBusy = false; if (error) { return; }
      var byId = {}; data.apps.forEach(function (app) { byId[app.id] = app; });
      apps.forEach(function (app) { var latest = byId[app.id]; if (latest) { app.running = latest.running; app.active = latest.active; app.hasWindow = latest.hasWindow; } });
      each('[data-status-id]', function (item) { var app = byId[item.getAttribute('data-status-id')]; if (app) { item.textContent = appStatus(app); item.className = 'status' + (app.active ? ' active-dot' : ''); } });
      each('[data-open-id]', function (item) { var app = byId[item.getAttribute('data-open-id')]; if (app) { item.textContent = canSwitch(app) ? 'Switch to app' : 'Open on PC'; } });
    }, true);
  }
  function loadModels() {
    if (!token || chatXHR || modelsBusy || modelLoading) { return; } modelsBusy = true;
    get('/api/lm/models', function (error, data) {
      modelsBusy = false;
      if (chatXHR || modelLoading) { return; }
      text('lmMessage', error || data.message); var select = el('model');
      modelsReady = !error;
      if (error) { updateChatControls(); if (!chat.length) { text('chatStatus', error); } return; }
      loadedModels = data.models; var signature = JSON.stringify(data.models), selected = pendingModelSelection || select.value;
      if (signature !== modelSignature && document.activeElement !== select) {
        select.textContent = ''; modelSignature = signature;
        var placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = data.models.length ? 'Choose a loaded model…' : 'No model is loaded yet'; select.appendChild(placeholder);
        data.models.forEach(function (model) { var option = document.createElement('option'); option.value = model.id; option.textContent = model.name + (model.vision === true ? ' · Vision' : '') + (model.contextLength ? ' · ' + model.contextLength + ' context' : ''); select.appendChild(option); });
        if (data.models.some(function (model) { return model.id === selected; })) { select.value = selected; }
        pendingModelSelection = '';
      }
      attachments.modelChanged(); updateChatControls();
      if (!select.value && !chat.length) { text('chatStatus', data.models.length ? 'Choose a model to start chatting.' : 'Load a model from the choices above to start chatting.'); }
      var available = el('availableModel'), previous = available.value, installedSignature = JSON.stringify(data.availableModels || []);
      if (installedSignature !== availableSignature && document.activeElement !== available) {
        availableSignature = installedSignature; available.textContent = '';
        var first = document.createElement('option'); first.value = ''; first.textContent = 'Choose a model to load…'; available.appendChild(first);
        (data.availableModels || []).forEach(function (model) { var item = document.createElement('option'); item.value = model.id; item.textContent = model.name + (model.vision === true ? ' · Vision' : ''); available.appendChild(item); });
        if ((data.availableModels || []).some(function (model) { return model.id === previous; })) { available.value = previous; }
      }
      renderQuickModels(data.quickModels || []); el('loadModelPanel').hidden = !(data.availableModels || []).length; el('loadModel').disabled = !available.value;
    }, true);
  }
  function renderQuickModels(models) {
    var signature = JSON.stringify(models); if (quickSignature === signature) { return; } quickSignature = signature;
    var box = el('quickModels'); box.textContent = '';
    models.forEach(function (model) {
      var button = document.createElement('button'); button.type = 'button'; button.className = 'quiet'; button.textContent = 'Use ' + model.name; button.title = model.description;
      button.onclick = function () {
        if (chatXHR || modelLoading || liveVoice) { text('chatStatus', 'Stop the current conversation before choosing another model.'); return; }
        button.disabled = true; modelLoading = true; updateChatControls(); text('lmMessage', 'Loading ' + model.name + '…');
        post('/api/lm/load', {model: model.id}, function (error, result) {
          button.disabled = false; modelLoading = false; updateChatControls(); text('lmMessage', error || result.message);
          if (!error && result.selectedInstanceId) { pendingModelSelection = result.selectedInstanceId; }
          modelSignature = ''; loadModels();
        }, true);
      }; box.appendChild(button);
    });
  }
  function stopLive() {
    var wasLive = liveVoice; liveVoice = false; el('liveVoice').textContent = 'Live voice'; el('liveVoice').setAttribute('aria-pressed', 'false');
    if (wasLive) { voice.cancel(); stopReading(); }
  }
  el('liveVoice').onclick = function () {
    if (liveVoice) { stopLive(); stopChat(); text('chatStatus', 'Live conversation stopped.'); return; }
    if (!token || !modelsReady || !loadedModels.some(function (model) { return model.id === el('model').value; }) || chatXHR || modelLoading || attachments.busy()) { text('chatStatus', 'Choose a model and finish the current reply or image preparation first.'); return; }
    if (!reader.ready()) { reader.load(); text('chatStatus', 'Wait for the PC voice to be ready, then start live conversation.'); return; }
    liveVoice = true; this.textContent = 'Stop live'; this.setAttribute('aria-pressed', 'true'); voice.start();
  };
  function bubble(role, content) {
    el('chatEmpty').hidden = true; var item = document.createElement('div'); item.className = 'bubble ' + role;
    var label = document.createElement('div'); label.className = 'role'; label.textContent = role === 'user' ? 'You' : 'Local model'; item.appendChild(label);
    var message = document.createElement('div');
    if (Array.isArray(content)) { content.forEach(function (part) { if (part.type === 'text') { message.textContent = part.text; } else if (part.type === 'image_url') { var image = document.createElement('img'); image.src = part.image_url.url; image.alt = 'Image sent to the model'; image.className = 'chat-image'; item.appendChild(image); } }); }
    else { message.textContent = content; }
    item.appendChild(message); el('chatMessages').appendChild(item); return message;
  }
  function escapedMarker(value, index) { var count = 0; while (index > 0 && value.charAt(--index) === '\\') { count++; } return count % 2 === 1; }
  function inlineReply(parent, value) {
    var start = 0, index = 0;
    while (index < value.length) {
      var marker = value.substr(index, 2) === '**' ? '**' : value.charAt(index) === '`' ? '`' : '', end = -1;
      if (marker && escapedMarker(value, index)) { end = value.indexOf(marker, index + marker.length); if (end !== -1 && !/[\r\n]/.test(value.slice(index, end))) { index = end + marker.length; continue; } }
      if (marker && !escapedMarker(value, index) && value.charAt(index - 1) !== marker.charAt(0) && value.charAt(index + marker.length) !== marker.charAt(0)) {
        end = value.indexOf(marker, index + marker.length);
        while (end !== -1 && (escapedMarker(value, end) || value.charAt(end - 1) === marker.charAt(0) || value.charAt(end + marker.length) === marker.charAt(0))) { end = value.indexOf(marker, end + marker.length); }
        if (end > index + marker.length && !/[\r\n]/.test(value.slice(index, end)) && (marker !== '**' || !/^\s|\s$/.test(value.slice(index + marker.length, end)))) {
          parent.appendChild(document.createTextNode(value.slice(start, index)));
          var node = document.createElement(marker === '**' ? 'strong' : 'code'); node.textContent = value.slice(index + marker.length, end); parent.appendChild(node);
          index = end + marker.length; start = index; continue;
        }
      }
      index++;
    }
    parent.appendChild(document.createTextNode(value.slice(start)));
  }
  function formatReply(output, value) {
    output.textContent = ''; var opening = /^```[A-Za-z0-9_+.-]*[ \t]*(?:\r?\n|$)/gm, closing = /^```[ \t]*(?=\r?$)/gm, match, start = 0;
    while ((match = opening.exec(value))) {
      inlineReply(output, value.slice(start, match.index)); closing.lastIndex = opening.lastIndex; var end = closing.exec(value);
      if (!end) { output.appendChild(document.createTextNode(value.slice(match.index))); start = value.length; break; }
      var pre = document.createElement('pre'), code = document.createElement('code'); code.textContent = value.slice(opening.lastIndex, end.index); pre.appendChild(code); output.appendChild(pre);
      start = end.index + end[0].length; opening.lastIndex = start;
    }
    inlineReply(output, value.slice(start));
  }
  function nearChatBottom() { var box = el('chatMessages'); return box.scrollHeight - box.scrollTop - box.clientHeight < 180; }
  function scrollChat(force) { var box = el('chatMessages'); if (force || nearChatBottom()) { box.scrollTop = box.scrollHeight; } }
  function stopReading() { reader.stop(); }
  function readReply() { var replies = chat.filter(function (item) { return item.role === 'assistant'; }); if (replies.length) { reader.speak(replies[replies.length - 1].content); } }
  function stopChat() { if (chatXHR) { chatXHR.abort(); chatXHR = null; } el('chatStop').disabled = true; updateChatControls(); el('model').disabled = false; }
  function cannotSend(message, showSetup) { text('chatStatus', message); if (showSetup) { el('chatSetup').open = true; } if (liveVoice) { stopLive(); } }
  function sendChat(event) {
    event.preventDefault(); if (chatXHR) { return; }
    var model = el('model').value, input = el('chatInput').value.trim(), images = attachments.get();
    if (!token) { cannotSend('Reconnect to the PC before sending.'); return; }
    if (!model || !modelsReady || !loadedModels.some(function (item) { return item.id === model; })) { cannotSend('Choose an available model first.', true); return; }
    if (attachments.busy()) { cannotSend('Wait for image preparation to finish.'); return; }
    if (!input && !images.length) { return; }
    if ((images.length || sentImageCount()) && selectedVision() !== true) { cannotSend('Choose a vision model for this image conversation, or clear it to start a text chat.'); return; }
    stopReading(); var content = images.length ? [{type: 'text', text: input || 'Describe this image.'}].concat(images.map(function (image) { return {type: 'image_url', image_url: {url: image.url}}; })) : input;
    chat.push({role: 'user', content: content}); bubble('user', content); el('chatInput').value = ''; attachments.clear();
    var output = bubble('assistant', 'Waiting for the selected model…'), reply = '', read = 0, buffer = '', finished = false, hadError = false, receivedDone = false;
    el('chatSetup').open = false; scrollChat(true); el('chatMessages').scrollIntoView({block: 'nearest'});
    var xhr = new XMLHttpRequest(); chatXHR = xhr; xhr.open('POST', '/api/lm/chat', true);
    xhr.setRequestHeader('Content-Type', 'application/json'); xhr.setRequestHeader('X-MAIC-Control', token);
    el('chatSend').disabled = true; el('chatStop').disabled = false; el('model').disabled = true; text('chatStatus', 'Waiting for the selected model…');
    function finish(message) { if (finished) { return; } finished = true; if (reply) { var follow = nearChatBottom(); if (!hadError) { formatReply(output, reply); } chat.push({role: 'assistant', content: reply}); el('chatRead').disabled = false; scrollChat(follow); } else { chat.pop(); attachments.restore(images); output.textContent = message; if (!el('chatInput').value) { el('chatInput').value = input; } } chatXHR = null; updateChatControls(); el('chatStop').disabled = true; el('model').disabled = false; text('chatStatus', message); scrollChat(false); if (liveVoice) { if (reply && receivedDone && !hadError) { reader.speak(reply); } else { stopLive(); } } }
    function parse() {
      if (finished || chatXHR !== xhr || xhr.status !== 200) { return; }
      buffer += xhr.responseText.slice(read); read = xhr.responseText.length;
      var boundary; while ((boundary = buffer.indexOf('\n\n')) !== -1) {
        var block = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2); var line = block.split('\n').filter(function (value) { return value.indexOf('data: ') === 0; })[0]; if (!line) { continue; }
        var data; try { data = JSON.parse(line.slice(6)); } catch (e) { continue; }
        if (typeof data.text === 'string') { var follow = nearChatBottom(); reply += data.text; output.textContent = reply; text('chatStatus', 'Replying…'); scrollChat(follow); }
        if (data.error) { hadError = true; output.textContent = reply ? reply + '\n\n[Reply interrupted: ' + data.error + ']' : data.error; finish(data.error); }
        if (data.done) { receivedDone = true; finish('Reply complete.'); }
      }
    }
    xhr.onprogress = parse; xhr.onload = function () { parse(); var errorMessage = 'Chat could not start. Refresh model status.'; if (xhr.status !== 200) { hadError = true; try { errorMessage = JSON.parse(xhr.responseText).message || errorMessage; } catch (e) {} } finish(receivedDone ? 'Reply complete.' : xhr.status === 200 ? 'Reply ended early. The connection closed before completion.' : errorMessage); };
    xhr.onerror = function () { hadError = true; finish('Connection lost.'); }; xhr.onabort = function () { finish('Stopped.'); };
    xhr.send(JSON.stringify({model: model, messages: chat}));
  }
  function loadComfy() {
    if (!token || comfyBusy) { return; } comfyBusy = true; get('/api/comfy/status', function (error, data) {
      comfyBusy = false;
      text('comfyMessage', error || data.message); if (error) { return; }
      text('comfyRunning', data.available ? data.queue.running : '—'); text('comfyPending', data.available ? data.queue.pending : '—');
      var signature = JSON.stringify({available: data.available, outputs: data.outputs}); if (signature === outputSignature) { return; } outputSignature = signature;
      if (expandedTarget && document.body.getAttribute('data-expanded') === 'media') { outputSignature = ''; return; }
      var container = el('comfyOutputs'); if (container.querySelector('video') && Array.prototype.some.call(container.querySelectorAll('video'), function (v) { return !v.paused; })) { outputSignature = ''; return; } container.textContent = '';
      if (!data.outputs.length) { var empty = document.createElement('article'); empty.className = 'card empty-state'; empty.setAttribute('role', 'status'); empty.textContent = data.available ? 'No recent outputs yet. New ComfyUI results appear here.' : 'Start ComfyUI on your PC to see results.'; container.appendChild(empty); }
      data.outputs.forEach(function (item) {
        var card = document.createElement('article'); card.className = 'card';
        var media = document.createElement(item.type === 'video' ? 'video' : 'img'); media.src = item.previewUrl; media.setAttribute('aria-label', item.name);
        if (item.type === 'video') { media.controls = true; media.preload = 'none'; media.setAttribute('playsinline', ''); } else { media.alt = item.name; media.loading = 'lazy'; }
        var feedback = document.createElement('p'); feedback.className = 'feedback';
        if (item.type === 'video') {
          feedback.textContent = 'Tap Play. The first preview may take a moment to prepare.';
          media.onwaiting = function () { feedback.textContent = 'Preparing or buffering the compatible preview…'; };
          media.onplaying = function () { play.textContent = 'Pause video'; feedback.textContent = 'Playing · compatible preview'; each('#comfyOutputs video', function (other) { if (other !== media) { other.pause(); } }); };
          media.onpause = function () { play.textContent = media.ended ? 'Replay video' : 'Play video'; };
          media.onended = function () { play.textContent = 'Replay video'; feedback.textContent = 'Preview complete.'; };
          media.onerror = function () { feedback.textContent = 'Preview could not play. Tap Retry; the original output is preserved.'; retry.hidden = false; };
          var retry = document.createElement('button'); retry.className = 'quiet'; retry.textContent = 'Retry video'; retry.hidden = true;
          retry.onclick = function () { retry.hidden = true; feedback.textContent = 'Loading preview…'; media.load(); media.play().catch(function () { retry.hidden = false; }); }; card.appendChild(retry);
        }
        var a = document.createElement('a'); a.href = item.previewUrl; a.target = '_blank'; a.rel = 'noopener'; a.textContent = item.name; card.appendChild(media);
        var mediaActions = document.createElement('div'); mediaActions.className = 'row media-actions';
        if (item.type === 'video') { var play = document.createElement('button'); play.textContent = 'Play video'; play.onclick = function () { if (!media.paused) { media.pause(); return; } var result = media.play(); if (result && result.catch) { result.catch(function () { feedback.textContent = 'Playback did not start. Try again or open the output link.'; }); } }; mediaActions.appendChild(play); }
        var expand = document.createElement('button'); expand.className = 'quiet'; expand.textContent = 'Full screen'; expand.onclick = function () { toggleFullscreen(media); }; mediaActions.appendChild(expand); card.appendChild(mediaActions);
        if (item.type === 'video') { card.appendChild(feedback); } card.appendChild(a); container.appendChild(card);
      });
    }, true);
  }
  function loadRemote() {
    if (!token || remoteBusy) { return; } remoteBusy = true;
    get('/api/remote/info', function (error, data) {
      text('remoteMessage', error || (data.enabled ? 'Secure address configured. Approve new browsers from this PC.' : data.message));
      var link = el('remoteLink'); link.hidden = !!error || !data.url; if (!link.hidden) { link.href = data.url; link.title = data.url; link.textContent = 'Open secure dashboard ↗'; }
      var voiceLink = el('voiceSecureLink'); voiceLink.hidden = !!error || !data.url || location.protocol === 'https:'; if (!voiceLink.hidden) { voiceLink.href = data.url + '/#chat'; }
      if (error || !data.enabled || !localPC) { remoteBusy = false; el('pcApproval').hidden = true; remoteSignature = ''; return; }
      get('/api/remote/status', function (statusError, state) {
        remoteBusy = false;
        var container = el('remoteDevices'); if (statusError) { el('pcApproval').hidden = true; remoteSignature = ''; text('remoteMessage', statusError); return; }
        var signature = JSON.stringify(state); if (signature === remoteSignature) { return; } remoteSignature = signature; container.textContent = '';
        function deviceRow(item, pending, parent) {
          var row = document.createElement('div'); row.className = 'remote-device';
          var description = document.createElement('span'); description.textContent = pending ? 'Pairing code ' + item.code + ' · ' + (item.label || 'Browser') : item.label || 'Approved browser';
          var button = document.createElement('button'); button.className = 'quiet'; button.textContent = pending ? 'Approve this code' : 'Revoke access';
          button.onclick = function () { button.disabled = true; post('/api/remote/' + (pending ? 'approve' : 'revoke'), {id: item.id}, function (error, result) { text('remoteMessage', error || result.message); loadRemote(); }, true); };
          row.appendChild(description); row.appendChild(button);
          if (pending) { var reject = document.createElement('button'); reject.className = 'quiet'; reject.textContent = 'Reject'; reject.onclick = function () { reject.disabled = true; post('/api/remote/revoke', {id: item.id}, function (error) { if (error) { toast(error); } loadRemote(); }, true); }; row.appendChild(reject); }
          parent.appendChild(row);
        }
        var pending = state.pending || []; pendingSignature = pending.map(function (item) { return item.id; }).join(',');
        var panel = el('approvalRequests'); panel.textContent = '';
        pending.forEach(function (item) { deviceRow(item, true, container); deviceRow(item, true, panel); });
        (state.devices || []).forEach(function (item) { deviceRow(item, false, container); });
        if (!pending.length && !(state.devices || []).length) { container.textContent = 'Waiting for your MAIC. Tap Request connection on its secure page.'; }
        el('pcApproval').hidden = !pending.length || pendingSignature === dismissedSignature;
      }, true);
    }, true);
  }
  function sizeChrome() { document.documentElement.style.setProperty('--viewport-height', Math.round(window.visualViewport ? window.visualViewport.height : window.innerHeight || 600) + 'px'); document.documentElement.style.setProperty('--viewport-top', Math.round(window.visualViewport && window.visualViewport.offsetTop || 0) + 'px'); var chrome = document.querySelector('.app-chrome'); document.documentElement.style.setProperty('--chrome-height', chrome.offsetHeight + 'px'); }
  function standaloneMode() { return !!navigator.standalone || !!(window.matchMedia && window.matchMedia('(display-mode: standalone)').matches); }
  function nativeFullscreen() { return document.fullscreenElement || document.webkitFullscreenElement || document.webkitCurrentFullScreenElement; }
  function fullscreenMethod(target) { return document.fullscreenEnabled !== false && target.requestFullscreen || document.webkitFullscreenEnabled !== false && (target.webkitRequestFullscreen || target.webkitRequestFullScreen); }
  function fullscreenActive(request) { return nativeFullscreen() === request.target || request.video && (request.videoStarted || request.target.webkitDisplayingFullscreen || request.target.webkitPresentationMode === 'fullscreen'); }
  function cancelFullscreenRequest() {
    if (!pendingFullscreen) { return; } var request = pendingFullscreen; pendingFullscreen = null; clearTimeout(request.timer);
    if (request.videoHandler) { request.target.removeEventListener('webkitbeginfullscreen', request.videoHandler); }
  }
  function desktopExpanded() { return expandedTarget === el('desktopFrame') || nativeFullscreen() === el('desktopFrame'); }
  function syncViewerDisplay() {
    if (desktopWanted && el('viewer').src && el('viewer').src !== 'about:blank') {
      el('viewer').contentWindow.postMessage({type: 'maic-viewer-display', expanded: desktopExpanded(), connectionId: desktopGeneration}, location.origin);
    }
  }
  function fullscreenChanged() {
    if (pendingFullscreen && fullscreenActive(pendingFullscreen)) { cancelFullscreenRequest(); }
    if (expandedTarget && nativeFullscreen() === expandedTarget) { exitExpanded(false); }
    var active = nativeFullscreen(), appMode = !fullscreenMethod(document.documentElement);
    text('appFullscreen', active ? 'Exit full screen' : appMode ? 'App mode' : 'Full screen'); el('appFullscreen').setAttribute('aria-pressed', String(!!active));
    text('desktopFullscreen', desktopExpanded() ? 'Exit view' : 'Expand view'); el('desktopFullscreen').setAttribute('aria-pressed', String(desktopExpanded())); sizeChrome(); syncViewerDisplay();
  }
  function exitExpanded(restoreFocus) {
    if (!expandedTarget) { return; } var previous = expansionFocus;
    expandedTarget.className = expandedTarget.className.replace(/(?:^|\s)browser-expanded(?=\s|$)/g, '').trim(); expandedTarget = null; expansionFocus = null;
    document.body.removeAttribute('data-expanded'); el('expandedExit').hidden = true; fullscreenChanged();
    if (restoreFocus !== false && previous && previous.isConnected !== false && previous.focus) { previous.focus(); }
  }
  function expandInBrowser(target) {
    if (expandedTarget === target) { return; }
    var previous = document.activeElement; exitExpanded(false); expandedTarget = target; expansionFocus = previous;
    target.className = (target.className + ' browser-expanded').trim(); document.body.setAttribute('data-expanded', target === el('desktopFrame') ? 'desktop' : 'media');
    el('expandedExit').hidden = false; el('expandedExit').textContent = target === el('desktopFrame') ? 'Exit view' : 'Exit expanded view'; el('expandedExit').focus(); fullscreenChanged();
  }
  function showAppModeHelp(blocked) {
    showPage('home'); var help = el('appModeHelp'), status = document.getElementById('appModeStatus');
    if (status) { status.textContent = standaloneMode() ? 'Already running in app mode.' : blocked ? 'Full screen was blocked. Use the app-mode steps below.' : 'Use the steps below to open this page in app mode.'; }
    help.open = true; help.scrollIntoView({block: 'nearest'}); if (help.focus) { help.focus(); }
  }
  function exitNativeFullscreen() {
    var method = document.exitFullscreen || document.webkitExitFullscreen || document.webkitCancelFullScreen;
    function failed() { toast('Could not exit full screen. Try again or use the browser’s Escape/exit control.'); }
    if (!method) { failed(); return; }
    try { var result = method.call(document); if (result && result.catch) { result.catch(failed); } } catch (error) { failed(); }
  }
  function toggleFullscreen(target) {
    cancelFullscreenRequest(); var active = nativeFullscreen(), result, generation = ++fullscreenGeneration;
    if (expandedTarget === target) { exitExpanded(); return; }
    if (active && (target === document.documentElement || active === target)) { exitNativeFullscreen(); return; }
    if (target === document.documentElement && !fullscreenMethod(target)) { showAppModeHelp(false); return; }
    function failed(unavailable) {
      if (generation !== fullscreenGeneration) { return; }
      if (target === document.documentElement) { showAppModeHelp(true); toast(unavailable ? 'Full screen is unavailable in this browser. Use App mode below.' : 'Full screen was blocked by the browser. See App mode below.'); }
      else { expandInBrowser(target); toast(target === el('desktopFrame') ? 'Expanded within the browser. Use Exit view to return.' : 'Expanded within the browser. Use Exit expanded view to return.'); }
    }
    try {
      var method = fullscreenMethod(target), video = !method && target.tagName === 'VIDEO' && (target.webkitEnterFullscreen || target.webkitEnterFullScreen);
      if (!method && !video) { expandInBrowser(target); return; }
      var request = {target: target, generation: generation, video: !!video}; pendingFullscreen = request;
      request.check = function (error, unavailable) {
        if (pendingFullscreen !== request || generation !== fullscreenGeneration) { return; }
        if (fullscreenActive(request)) { cancelFullscreenRequest(); fullscreenChanged(); return; }
        if (error) { cancelFullscreenRequest(); failed(unavailable); }
      };
      if (video) { request.videoHandler = function () { request.videoStarted = true; request.check(false); }; target.addEventListener('webkitbeginfullscreen', request.videoHandler); }
      request.timer = setTimeout(function () { request.check(true, false); }, 1500);
      exitExpanded(false); result = (method || video).call(target); request.check(false);
      if (result && result.then) { result.then(function () { request.check(false); }, function () { request.check(true, false); }); }
      else if (result && result.catch) { result.catch(function () { request.check(true, false); }); }
    } catch (error) { cancelFullscreenRequest(); failed(true); }
  }
  function setDesktopExpanded(wanted) {
    if (wanted === desktopExpanded()) { syncViewerDisplay(); return; }
    var frame = el('desktopFrame');
    if (!wanted) {
      fullscreenGeneration++; cancelFullscreenRequest(); exitExpanded();
      if (nativeFullscreen() === frame) { exitNativeFullscreen(); }
      return;
    }
    // The view changes immediately, even when a mobile browser ignores native
    // fullscreen. Keep the iframe mounted so the desktop session survives.
    if (!nativeFullscreen()) { toggleFullscreen(frame); }
    if (nativeFullscreen() !== frame) { expandInBrowser(frame); }
  }
  function toggleDesktopFullscreen() { setDesktopExpanded(!desktopExpanded()); }
  function updateDesktopControls() {
    el('desktopConnect').disabled = desktopState === 'connecting';
    el('desktopConnectEmpty').disabled = desktopState === 'connecting';
    text('desktopConnect', desktopState === 'connecting' ? 'Connecting…' : desktopState === 'connected' ? 'Reconnect desktop' : 'Connect desktop');
    el('desktopDisconnect').disabled = !desktopWanted;
    el('desktopEmpty').hidden = desktopState === 'connected' || desktopState === 'connecting';
  }
  function disconnectDesktop() {
    desktopWanted = false; desktopState = 'disconnected'; desktopGeneration++; clearTimeout(desktopRetryTimer); clearTimeout(desktopWatchdog); desktopRetry = 0;
    el('viewer').src = 'about:blank'; desktopCredentials = null; text('desktopMessage', 'Desktop disconnected.'); updateDesktopControls();
  }
  function scheduleDesktopRetry() {
    clearTimeout(desktopRetryTimer);
    if (!desktopWanted || !el('desktopAutoReconnect').checked || document.hidden || page !== 'desktop' || desktopRetry >= 3) { return; }
    var delay = [2000, 5000, 10000][desktopRetry++];
    text('desktopMessage', 'Connection interrupted. Reconnecting in ' + delay / 1000 + ' seconds…');
    desktopRetryTimer = setTimeout(function () { if (desktopWanted && !document.hidden && page === 'desktop') { startDesktop(false); } }, delay);
  }
  function startDesktop(manual) {
    if (desktopState === 'connecting') { return; }
    if (manual) { desktopRetry = 0; } desktopWanted = true; desktopState = 'connecting';
    clearTimeout(desktopRetryTimer); clearTimeout(desktopWatchdog); var generation = ++desktopGeneration;
    desktopCredentials = null; el('viewer').src = 'about:blank'; updateDesktopControls(); text('desktopMessage', 'Connecting securely…');
    function failed(message) { if (generation !== desktopGeneration) { return; } desktopState = 'disconnected'; desktopCredentials = null; updateDesktopControls(); text('desktopMessage', message); scheduleDesktopRetry(); }
    get('/api/control-session', function (error, session) {
      if (generation !== desktopGeneration) { return; }
      if (error) { failed(error); return; } token = session.token; sessionAt = Date.now();
      post('/api/desktop/session', {}, function (error, data) {
        if (generation !== desktopGeneration) { return; }
        if (error) { failed(error); return; }
        desktopCredentials = {credentials: data.credentials, port: data.port}; el('viewer').src = '/desktop-viewer.html';
        desktopWatchdog = setTimeout(function () { if (generation === desktopGeneration && desktopState === 'connecting') { el('viewer').src = 'about:blank'; failed('The desktop connection timed out.'); } }, 20000);
      }, true);
    });
  }
  el('desktopConnect').onclick = el('desktopConnectEmpty').onclick = function () { startDesktop(true); };
  window.addEventListener('message', function (event) {
    if (event.origin !== window.location.origin || event.source !== el('viewer').contentWindow) { return; }
    if (event.data && event.data.type === 'maic-viewer-ready' && desktopCredentials && desktopCredentials.credentials) { event.source.postMessage({type: 'maic-viewer-connect', credentials: desktopCredentials.credentials, port: desktopCredentials.port, connectionId: desktopGeneration, mode: desktopMode}, window.location.origin); desktopCredentials.credentials = null; syncViewerDisplay(); }
    if (event.data && event.data.type === 'maic-viewer-status' && event.data.connectionId === desktopGeneration) {
      if (['control', 'pan', 'view'].indexOf(event.data.mode) >= 0) { desktopMode = event.data.mode; }
      text('desktopMessage', event.data.message);
      if (event.data.state === 'connected') { desktopState = 'connected'; desktopRetry = 0; clearTimeout(desktopWatchdog); updateDesktopControls(); if (page !== 'desktop' || document.hidden) { event.source.postMessage({type: 'maic-viewer-suspend', connectionId: desktopGeneration}, location.origin); } }
      if (event.data.state === 'disconnected') { desktopState = 'disconnected'; clearTimeout(desktopWatchdog); updateDesktopControls(); scheduleDesktopRetry(); }
    }
    if (event.data && event.data.type === 'maic-viewer-fullscreen' && desktopWanted && page === 'desktop') {
      if (event.data.connectionId === desktopGeneration && typeof event.data.expanded === 'boolean') { setDesktopExpanded(event.data.expanded); }
    }
  });
  el('desktopDisconnect').onclick = disconnectDesktop;
  el('desktopFullscreen').onclick = toggleDesktopFullscreen;
  el('appFullscreen').onclick = function () { toggleFullscreen(document.documentElement); };
  el('expandedExit').onclick = function () { fullscreenGeneration++; cancelFullscreenRequest(); exitExpanded(); };
  document.addEventListener('keydown', function (event) { if (event.key === 'Escape' || event.keyCode === 27) { fullscreenGeneration++; cancelFullscreenRequest(); if (expandedTarget) { event.preventDefault(); exitExpanded(); } } });
  el('desktopAutoReconnect').onchange = function () { if (!this.checked) { clearTimeout(desktopRetryTimer); } else if (desktopState === 'disconnected') { desktopRetry = 0; scheduleDesktopRetry(); } };
  document.addEventListener('fullscreenchange', fullscreenChanged); document.addEventListener('webkitfullscreenchange', fullscreenChanged);
  function fullscreenError(event) { var request = pendingFullscreen; if (request && (!event.target || event.target === document || event.target === request.target)) { request.check(true, false); } }
  document.addEventListener('fullscreenerror', fullscreenError); document.addEventListener('webkitfullscreenerror', fullscreenError);
  window.addEventListener('resize', sizeChrome); if (window.visualViewport) { window.visualViewport.addEventListener('resize', sizeChrome); window.visualViewport.addEventListener('scroll', sizeChrome); } if (window.ResizeObserver) { new ResizeObserver(sizeChrome).observe(document.querySelector('.app-chrome')); }
  function checkConnection() {
    if (healthBusy) { return; } healthBusy = true; var started = Date.now();
    get('/api/health', function (error) {
      healthBusy = false; document.body.setAttribute('data-online', String(!error));
      text('latency', error ? 'Offline' : Date.now() - started + ' ms');
      if (error) { text('connection', 'PC unreachable · retrying'); }
      else if (!sessionAt || Date.now() - sessionAt > 20 * 60 * 1000) { if (!connectionBusy && !chatXHR) { connect(); } }
      else { text('connection', location.protocol === 'https:' ? 'Connected · secure HTTPS' : 'Connected · local Wi-Fi'); }
    });
  }
  var timerTotal = 25 * 60, timerLeft = timerTotal, timerEnd = null;
  function renderTimer() { if (timerEnd) { timerLeft = Math.max(0, Math.ceil((timerEnd - Date.now()) / 1000)); if (!timerLeft) { timerEnd = null; toast('Timer complete.'); new Audio('/tone.wav').play().catch(function () {}); } } text('timerDisplay', Math.floor(timerLeft / 60) + ':' + ('0' + timerLeft % 60).slice(-2)); text('timerPause', timerEnd ? 'Pause' : 'Start'); }
  each('[data-minutes]', function (item) { item.onclick = function () { timerTotal = Number(item.getAttribute('data-minutes')) * 60; timerLeft = timerTotal; timerEnd = Date.now() + timerTotal * 1000; renderTimer(); }; });
  el('timerPause').onclick = function () { if (timerEnd) { renderTimer(); timerEnd = null; } else { if (!timerLeft) { timerLeft = timerTotal; } timerEnd = Date.now() + timerLeft * 1000; } renderTimer(); };
  el('timerReset').onclick = function () { timerEnd = null; timerLeft = timerTotal; renderTimer(); };
  each('nav button', function (item) { item.onclick = function () { showPage(item.getAttribute('data-page')); }; });
  each('[data-go]', function (item) { item.onclick = function () { showPage(item.getAttribute('data-go')); }; });
  each('[data-media]', function (item) { item.onclick = function () { control('media', item.getAttribute('data-media')); }; });
  each('[data-fixed]', function (item) { item.onclick = function () { control('open-app', item.getAttribute('data-fixed')); }; });
  el('volume').oninput = function () { volumeEditing = true; text('volumeLabel', this.value + '%'); }; el('volume').onchange = function () { volumeEditing = false; control('speaker-volume', Number(this.value)); };
  el('volume').onblur = function () { volumeEditing = false; loadAudio(); };
  el('volume').addEventListener('touchcancel', function () { volumeEditing = false; loadAudio(); });
  el('speakerMute').onclick = function () { if (audio) { control('speaker-mute', !audio.speaker.muted); } }; el('micMute').onclick = function () { if (audio) { control('microphone-mute', !audio.microphone.muted); } };
  el('bookmarkForm').onsubmit = function (event) { event.preventDefault(); post('/api/bookmarks', {name: el('bookmarkName').value, url: el('bookmarkUrl').value}, function (error, data) { text('bookmarkMessage', error || data.message); if (!error) { loadWebsites(); el('bookmarkForm').reset(); } }, true); };
  el('calculator').onsubmit = function (event) { event.preventDefault(); post('/api/calculate', {left: Number(el('left').value), right: Number(el('right').value), operation: el('operation').value}, function (error, data) { text('answer', error || data.result); }); };
  el('reconnect').onclick = connect; el('refreshApps').onclick = loadApps; el('appSearch').oninput = renderApps;
  el('favouritesOnly').onclick = function () { favouritesOnly = !favouritesOnly; this.setAttribute('aria-pressed', String(favouritesOnly)); renderApps(); };
  el('refreshModels').onclick = loadModels;
  el('model').onchange = function () { stopLive(); attachments.modelChanged(); updateChatControls(); text('chatStatus', this.value ? 'Ready to chat.' : 'Choose a model first.'); };
  el('availableModel').onchange = function () { el('loadModel').disabled = !this.value; };
  el('loadModel').onclick = function () {
    if (!el('availableModel').value) { return; }
    if (chatXHR || modelLoading || liveVoice) { text('chatStatus', 'Stop the current conversation before choosing another model.'); return; }
    modelLoading = true; updateChatControls(); this.disabled = true; text('lmMessage', 'Loading your selected model…');
    post('/api/lm/load', {model: el('availableModel').value}, function (error, data) { modelLoading = false; el('loadModel').disabled = false; updateChatControls(); text('lmMessage', error || data.message); if (!error) { if (data.selectedInstanceId) { pendingModelSelection = data.selectedInstanceId; } modelSignature = ''; loadModels(); } }, true);
  };
  el('lmOpen').onclick = function () { post('/api/lm/open', {}, function (error, data) { text('lmMessage', error || data.message); }, true); };
  el('lmStart').onclick = function () { el('lmStart').disabled = true; text('lmMessage', 'Starting localhost API…'); post('/api/lm/start', {}, function (error, data) { el('lmStart').disabled = false; text('lmMessage', error || data.message); if (!error) { loadModels(); } }, true); };
  el('chatForm').onsubmit = sendChat; el('chatStop').onclick = function () { stopLive(); stopChat(); };
  el('chatInput').oninput = updateChatControls;
  el('chatInput').onkeydown = function (event) { if ((event.key === 'Enter' || event.keyCode === 13) && (event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey && !event.isComposing && event.keyCode !== 229) { sendChat(event); } };
  el('chatRead').onclick = readReply; el('chatQuiet').onclick = function () { stopLive(); stopReading(); };
  el('chatClear').onclick = function () { stopLive(); stopChat(); stopReading(); voice.cancel(); attachments.clear(); chat = []; el('chatRead').disabled = true; el('chatMessages').textContent = ''; el('chatEmpty').hidden = false; el('chatInput').value = ''; updateChatControls(); text('chatStatus', 'Conversation cleared.'); };
  el('comfyRefresh').onclick = loadComfy;
  el('refreshRemote').onclick = loadRemote;
  el('dismissApproval').onclick = function () { dismissedSignature = pendingSignature; el('pcApproval').hidden = true; };
  el('touchTest').onclick = function () { request('POST', '/api/tap', null, function (error, data) { text('helpMessage', error || data.message); }); };
  el('soundTest').onclick = function () { new Audio('/tone.wav').play().then(function () { text('helpMessage', 'Played on this device.'); }).catch(function () { text('helpMessage', 'This browser could not play the test sound.'); }); };
  el('diagnostics').onclick = function () {
    var storage = false, previous = null, marker = String(Date.now()); try { previous = localStorage.getItem('maic-check'); localStorage.setItem('maic-check', marker); storage = true; } catch (e) {}
    var payload = {schemaVersion: 1, source: 'maic-user', userAgent: navigator.userAgent, platform: navigator.platform || '', language: navigator.language || '', clientTime: new Date().toISOString(), screen: {width: screen.width, height: screen.height, pixelRatio: window.devicePixelRatio || 1}, viewport: {width: innerWidth, height: innerHeight}, capabilities: {touchPoints: navigator.maxTouchPoints || 0, cookiesEnabled: navigator.cookieEnabled, localStorage: storage, webgl: false, webglRenderer: ''}, persistence: {available: storage, previousMarker: previous, currentMarker: marker}};
    post('/api/diagnostics', payload, function (error, data) { text('helpMessage', error || 'Browser report sent: ' + data.reportId); });
  };
  window.addEventListener('hashchange', function () { var name = location.hash.slice(1); showPage(name === 'access' ? 'home' : name); if (name === 'access') { showAccess(); } });
  window.addEventListener('online', checkConnection);
  window.addEventListener('offline', function () { document.body.setAttribute('data-online', 'false'); text('connection', 'Device is offline'); });
  document.addEventListener('visibilitychange', function () { if (document.hidden) { stopLive(); voice.cancel(); } if (desktopState === 'connected') { el('viewer').contentWindow.postMessage({type: !document.hidden && page === 'desktop' ? 'maic-viewer-resume' : 'maic-viewer-suspend', connectionId: desktopGeneration}, location.origin); } if (!document.hidden) { checkConnection(); if (localPC) { loadRemote(); } if (desktopWanted && desktopState === 'disconnected') { desktopRetry = 0; scheduleDesktopRetry(); } } });
  text('address', 'Dashboard: ' + window.location.origin + '/'); fullscreenChanged();
  if (location.hash && location.hash !== '#access') { showPage(location.hash.slice(1)); }
  connect(); renderTimer();
  setInterval(renderTimer, 500); setInterval(function () { if (document.hidden) { return; } if (page === 'apps') { loadAppState(); } }, 2000);
  setInterval(function () { if (document.hidden) { return; } if (page === 'home') { loadHardware(); loadAudio(); } if (page === 'comfy') { loadComfy(); } }, 5000);
  setInterval(function () { if (!document.hidden && page === 'chat') { loadModels(); } }, 8000);
  setInterval(function () { if (!document.hidden && localPC) { loadRemote(); } }, 3000);
  setInterval(function () { if (!document.hidden) { checkConnection(); } }, 15000);
  window.addEventListener('pagehide', function () { fullscreenGeneration++; cancelFullscreenRequest(); exitExpanded(false); stopLive(); voice.cancel(); stopChat(); stopReading(); disconnectDesktop(); });
}());
