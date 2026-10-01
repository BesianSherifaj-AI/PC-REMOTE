/* Local image preparation and PC-generated read-aloud audio; Chrome 78 compatible. */
(function () {
  'use strict';
  window.createMaicAttachments = function (options) {
    var input = document.getElementById('chatImages'), list = document.getElementById('imageAttachments');
    var status = document.getElementById('imageStatus'), items = [], generation = 0, working = false;
    function render() {
      var capability = options.vision(); input.disabled = working || capability !== true;
      list.textContent = '';
      items.forEach(function (item, index) {
        var card = document.createElement('div'), image = document.createElement('img'), remove = document.createElement('button');
        card.className = 'attachment'; image.src = item.url; image.alt = item.name;
        remove.type = 'button'; remove.className = 'quiet'; remove.textContent = 'Remove'; remove.setAttribute('aria-label', 'Remove ' + item.name);
        remove.onclick = function () { items.splice(index, 1); render(); };
        card.appendChild(image); card.appendChild(remove); list.appendChild(card);
      });
      status.textContent = working ? 'Preparing image…' : capability === false ? 'This model accepts text only.' : capability !== true ? 'Choose a vision model to add images.' : items.length ? items.length + (items.length === 1 ? ' image ready' : ' images ready') + ' · press Send to share.' : 'JPG, PNG or WebP · up to 3 images.';
      if (options.onChange) { options.onChange(); }
    }
    function prepare(file) {
      return new Promise(function (resolve, reject) {
        if (!/^(image\/(jpeg|png|webp))$/.test(file.type) || !file.size || file.size > 12 * 1024 * 1024) { reject(new Error('Choose a JPEG, PNG or WebP image under 12 MB.')); return; }
        var reader = new FileReader();
        reader.onerror = function () { reject(new Error('This image could not be read.')); };
        reader.onload = function () {
          var image = new Image();
          image.onerror = function () { reject(new Error('This browser could not open the image. Try a JPEG or screenshot.')); };
          image.onload = function () {
            try {
              if (!image.width || !image.height || image.width * image.height > 32000000) { throw new Error('This image is too large. Choose a smaller photo or screenshot.'); }
              var scale = Math.min(1, 1536 / Math.max(image.width, image.height)), canvas = document.createElement('canvas');
              canvas.width = Math.max(1, Math.round(image.width * scale)); canvas.height = Math.max(1, Math.round(image.height * scale));
              var context = canvas.getContext('2d'); context.fillStyle = '#fff'; context.fillRect(0, 0, canvas.width, canvas.height); context.drawImage(image, 0, 0, canvas.width, canvas.height);
              var url = canvas.toDataURL('image/jpeg', 0.86); canvas.width = canvas.height = 1;
              if (url.length > 2796200) { throw new Error('This image is still too large. Choose a smaller image.'); }
              resolve({url: url, name: file.name || 'Attached image'});
            } catch (error) { reject(error); }
          };
          image.src = reader.result;
        };
        reader.readAsDataURL(file);
      });
    }
    input.onchange = function () {
      var files = Array.prototype.slice.call(input.files || []); input.value = '';
      if (!files.length || working) { return; }
      var capability = options.vision();
      if (capability !== true) { status.textContent = 'Choose a vision model to add images.'; return; }
      if (files.length + items.length + options.sentCount() > 3) { status.textContent = 'Maximum 3 images per conversation. Remove an attachment or clear the conversation.'; return; }
      var attempt = ++generation, prepared = [], chain = Promise.resolve(); working = true; input.disabled = true; render();
      files.forEach(function (file) { chain = chain.then(function () { if (attempt !== generation) { return; } return prepare(file).then(function (item) { prepared.push(item); }); }); });
      chain.then(function () { if (attempt !== generation) { return; } items = items.concat(prepared); working = false; input.disabled = false; render(); }, function (error) { if (attempt !== generation) { return; } working = false; input.disabled = false; render(); status.textContent = error.message || 'Image preparation failed.'; });
    };
    return {
      busy: function () { return working; },
      get: function () { return items.slice(); },
      clear: function () { generation++; items = []; working = false; input.disabled = false; input.value = ''; render(); },
      restore: function (previous) { if (!items.length) { items = previous.slice(); render(); } },
      modelChanged: render
    };
  };
  window.createMaicReader = function (options) {
    var player = document.getElementById('replyAudio'), select = document.getElementById('ttsVoice'), rate = document.getElementById('ttsRate');
    var status = document.getElementById('ttsStatus'), readStatus = document.getElementById('readStatus'), stopButton = document.getElementById('chatQuiet'), request = null, generation = 0, url = null, available = false, maxChars = 3000, loading = false;
    function message(value) { status.textContent = value; if (readStatus) { readStatus.textContent = value; } }
    function fail(value) { if (options.onError) { options.onError(); } message(value); }
    function stop() {
      generation++; if (request) { var old = request; request = null; old.onload = old.onerror = old.ontimeout = null; old.abort(); }
      player.pause(); player.hidden = true; player.removeAttribute('src'); player.load();
      if (url) { URL.revokeObjectURL(url); url = null; } stopButton.disabled = true; if (readStatus) { readStatus.textContent = ''; }
    }
    function load() {
      if (loading || !options.token()) { return; } loading = true;
      options.get('/api/tts/status', function (error, data) {
        loading = false; available = !error && !!data.available; maxChars = data && data.maxChars || 3000;
        var previous = select.value; select.textContent = '';
        (data && data.voices || []).forEach(function (voice) { var item = document.createElement('option'); item.value = voice.id; item.textContent = voice.name + ' · ' + voice.language; select.appendChild(item); });
        if ((data && data.voices || []).some(function (voice) { return voice.id === previous; })) { select.value = previous; }
        else if (data && data.defaultVoice) { select.value = data.defaultVoice; }
        select.disabled = !available; status.textContent = error || data.message;
      }, true);
    }
    function speak(value) {
      stop();
      if (!available) { load(); fail('PC voice is not ready. Refresh voice status and try again.'); return; }
      if (!value || !options.token()) { fail('Connect and get a model reply first.'); return; }
      if (value.length > maxChars) { fail('Read-aloud supports replies up to ' + maxChars + ' characters. Ask the model for a shorter answer.'); return; }
      var attempt = generation, xhr = new XMLHttpRequest(); request = xhr; stopButton.disabled = false;
      xhr.open('POST', '/api/tts/speak', true); xhr.responseType = 'blob'; xhr.timeout = 180000;
      xhr.setRequestHeader('Content-Type', 'application/json'); xhr.setRequestHeader('X-MAIC-Control', options.token());
      message('Preparing audio…');
      xhr.onload = function () {
        if (attempt !== generation || request !== xhr) { return; } request = null;
        if (xhr.status !== 200) {
          stopButton.disabled = true; fail('PC speech could not be generated. Check the selected voice and try again.');
          var errorGeneration = generation, reader = new FileReader(); reader.onload = function () { if (errorGeneration !== generation) { return; } try { var data = JSON.parse(reader.result); message(data.message || 'PC speech is unavailable.'); } catch (error) {} }; reader.readAsText(xhr.response); return;
        }
        url = URL.createObjectURL(xhr.response); player.src = url; player.hidden = false; player.load();
        message('Audio ready.');
        var playback = player.play(); if (playback && playback.catch) { playback.catch(function () { if (attempt === generation) { message('Audio ready. Tap Play to listen.'); } }); }
      };
      xhr.onerror = xhr.ontimeout = function () { if (attempt !== generation || request !== xhr) { return; } request = null; stopButton.disabled = true; fail('Could not reach the PC voice. Try again.'); };
      xhr.send(JSON.stringify({text: value, voice: select.value, speed: Number(rate.value) || 1}));
    }
    player.onended = function () { if (!url || !player.ended) { return; } stopButton.disabled = true; message('Finished reading. Replay below.'); if (options.onEnded) { options.onEnded(); } };
    player.onplay = function () { if (url) { stopButton.disabled = false; message('Reading reply…'); } };
    player.onerror = function () { if (url) { fail('This browser could not play the PC audio. Try again.'); } };
    document.getElementById('ttsRefresh').onclick = load;
    return {load: load, speak: speak, stop: stop, ready: function () { return available; }};
  };
}());
