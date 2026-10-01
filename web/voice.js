/* PCM recording works on Chrome 78 without MediaRecorder codec dependencies. */
(function () {
  'use strict';
  window.createMaicVoice = function (options) {
    var stream = null, context = null, source = null, processor = null, gain = null, request = null;
    var chunks = [], sampleCount = 0, timer = null, started = 0, generation = 0, recording = false, busy = false;
    var liveRecording = false, speechRun = 0, speechHeard = false, silenceRun = 0;
    var button = document.getElementById('chatSpeak'), cancel = document.getElementById('voiceCancel');
    var status = document.getElementById('voiceStatus'), language = document.getElementById('voiceLanguage');
    function message(value) { status.textContent = value; }
    function failure(value) { message(value); if (options.onError) { options.onError(value); } }
    function idle(reason) { if (options.onIdle) { options.onIdle(reason); } }
    function close() {
      clearInterval(timer); timer = null;
      if (processor) { processor.onaudioprocess = null; }
      [processor, source, gain].forEach(function (node) { if (node) { try { node.disconnect(); } catch (error) {} } });
      if (stream) { stream.getTracks().forEach(function (track) { track.onended = null; try { track.stop(); } catch (error) {} }); }
      if (context) { try { var closing = context.close(); if (closing && closing.catch) { closing.catch(function () {}); } } catch (error) {} }
      processor = source = gain = stream = context = null; recording = false;
      button.textContent = 'Speak'; button.setAttribute('aria-pressed', 'false'); cancel.hidden = true;
    }
    function wav(rate) {
      var all = new Float32Array(sampleCount), at = 0;
      chunks.forEach(function (chunk) { all.set(chunk, at); at += chunk.length; }); chunks = [];
      var count = Math.min(480000, Math.floor(all.length * 16000 / rate));
      var buffer = new ArrayBuffer(44 + count * 2), view = new DataView(buffer);
      function label(offset, text) { for (var k = 0; k < text.length; k++) { view.setUint8(offset + k, text.charCodeAt(k)); } }
      label(0, 'RIFF'); view.setUint32(4, 36 + count * 2, true); label(8, 'WAVE'); label(12, 'fmt ');
      view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
      view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
      label(36, 'data'); view.setUint32(40, count * 2, true);
      // Average each source interval before downsampling, limiting high-frequency aliasing.
      for (var i = 0; i < count; i++) {
        var first = Math.floor(i * rate / 16000), last = Math.max(first + 1, Math.floor((i + 1) * rate / 16000)), sum = 0;
        for (var j = first; j < last; j++) { sum += all[Math.min(j, all.length - 1)]; }
        var sample = Math.max(-1, Math.min(1, sum / (last - first)));
        view.setInt16(44 + i * 2, sample < 0 ? sample * 32768 : sample * 32767, true);
      }
      return buffer;
    }
    function stop(submit, reason) {
      var attempt = ++generation;
      if (request) { var previous = request; request = null; previous.onload = previous.onerror = previous.ontimeout = null; previous.abort(); }
      var rate = context ? context.sampleRate : 16000, wasLive = liveRecording;
      var canSend = recording && submit && sampleCount >= rate / 5 && (!wasLive || speechHeard);
      close();
      if (!canSend) { chunks = []; busy = false; button.disabled = false;
        if (!submit) { failure(reason || 'Dictation cancelled.'); }
        else if (wasLive) { failure('No clear speech heard. Live voice paused; start it again when ready.'); }
        else { message('Speak a little longer.'); idle('short'); } return;
      }
      var payload;
      try { payload = wav(rate); } catch (error) { chunks = []; busy = false; button.disabled = false; failure('The recording could not be prepared. Try a shorter clip.'); return; }
      var xhr = new XMLHttpRequest(); request = xhr; busy = true; button.disabled = true; cancel.hidden = false; cancel.textContent = 'Cancel dictation';
      message('Transcribing on your PC…');
      xhr.open('POST', '/api/speech/transcribe', true); xhr.timeout = 120000;
      xhr.setRequestHeader('Content-Type', 'audio/wav'); xhr.setRequestHeader('X-MAIC-Control', options.token());
      xhr.setRequestHeader('X-MAIC-Language', language.value);
      xhr.onload = function () {
        if (attempt !== generation || request !== xhr) { return; }
        request = null; busy = false; button.disabled = false; cancel.hidden = true; var result;
        try { result = JSON.parse(xhr.responseText); } catch (e) {}
        if (xhr.status < 200 || xhr.status >= 300 || !result || !result.ok) { failure(result && result.message || 'Dictation failed. Try again or type your message.'); return; }
        if (result.text && typeof result.text === 'string' && result.text.trim()) { message(result.message); options.append(result.text); idle('transcribed'); }
        else if (wasLive) { failure(result.message || 'No speech heard. Live voice paused; start it again when ready.'); }
        else { message(result.message || 'No speech heard. Try again.'); idle('silent'); }
      };
      xhr.onerror = xhr.ontimeout = function () { if (attempt !== generation || request !== xhr) { return; } request = null; busy = false; button.disabled = false; cancel.hidden = true; failure('Could not reach local dictation. Your typed message is still here.'); };
      try { xhr.send(payload); } catch (error) { xhr.onerror(); }
    }
    function start() {
      if (busy) { return; } if (recording) { stop(true); return; }
      if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        failure('This HTTP address cannot use the microphone. Open the secure HTTPS dashboard first. If your browser still blocks it, use keyboard dictation.'); return;
      }
      if (!options.token()) { failure('Reconnect to the PC first.'); return; }
      var AudioContext = window.AudioContext || window.webkitAudioContext;
      if (!AudioContext) { failure('This browser does not support microphone recording. Use keyboard dictation.'); return; }
      var attempt = ++generation; busy = true; button.disabled = true; cancel.hidden = false;
      cancel.textContent = 'Cancel recording';
      message('Allow microphone access when the browser asks…');
      try { context = new AudioContext(); context.resume().catch(function () {
        if (attempt !== generation) { return; } stop(false, 'Audio recording could not start. Try Speak again or use keyboard dictation.');
      }); }
      catch (error) { close(); busy = false; button.disabled = false; failure('Audio recording is unavailable in this browser. Use keyboard dictation.'); return; }
      navigator.mediaDevices.getUserMedia({audio: {echoCancellation: true, noiseSuppression: true}, video: false}).then(function (result) {
        if (attempt !== generation) { result.getTracks().forEach(function (track) { track.stop(); }); return; }
        stream = result; source = context.createMediaStreamSource(stream); processor = context.createScriptProcessor(4096, 1, 1);
        gain = context.createGain(); gain.gain.value = 0; chunks = []; sampleCount = 0; started = Date.now(); recording = true; busy = false;
        liveRecording = !!(options.liveMode && options.liveMode()); speechRun = 0; speechHeard = false; silenceRun = 0;
        stream.getTracks().forEach(function (track) { track.onended = function () {
          if (attempt !== generation || !recording) { return; } stop(false, 'The microphone stopped. Try Speak again or use keyboard dictation.');
        }; });
        processor.onaudioprocess = function (event) {
          if (!recording) { return; }
          var samples = new Float32Array(event.inputBuffer.getChannelData(0)); chunks.push(samples); sampleCount += samples.length;
          if (liveRecording) {
            var energy = 0; for (var i = 0; i < samples.length; i++) { energy += samples[i] * samples[i]; }
            if (Math.sqrt(energy / samples.length) >= 0.015) { speechRun += samples.length; silenceRun = 0; if (speechRun >= context.sampleRate * 0.3) { speechHeard = true; } }
            else { if (!speechHeard) { speechRun = 0; } silenceRun += samples.length; }
            if (speechHeard && silenceRun >= context.sampleRate * 1.2) { stop(true); return; }
          }
          if (sampleCount >= context.sampleRate * 30) { stop(true); }
        };
        source.connect(processor); processor.connect(gain); gain.connect(context.destination);
        button.disabled = false; button.textContent = 'Finish speaking'; button.setAttribute('aria-pressed', 'true');
        message((liveRecording ? 'Live listening' : 'Listening') + ' · 0 / 30 seconds');
        timer = setInterval(function () { var seconds = Math.floor((Date.now() - started) / 1000); message((liveRecording ? 'Live listening' : 'Listening') + ' · ' + seconds + ' / 30 seconds'); if (seconds >= 30) { stop(true); } }, 500);
      }).catch(function (error) {
        if (attempt !== generation) { return; } close(); busy = false; button.disabled = false;
        var name = error && error.name || 'Unavailable';
        failure(name === 'NotAllowedError' ? 'Microphone permission was denied. Allow it in this browser’s site settings. Some MAIC browsers cannot grant it; keyboard dictation may work.' : 'No microphone is available to this browser (' + name + ').');
      });
    }
    button.onclick = start; cancel.onclick = function () { stop(false); };
    return {start: function () { if (!recording && !busy) { start(); } },
      cancel: function () { if (recording || stream || context || request || busy) { stop(false); } }, status: message};
  };
}());
