(function () {
  'use strict';
  window.createPCCompanion = function (options) {
    var agents = [], agentBusy = false, selected = '', conversations = {}, jobs = {}, sending = '', notices = {};
    var agentRows = {}, agentOrder = '';
    var awakeWanted = false, wakeLock = null, wakeGeneration = 0, wakePending = false;
    var folder = 'root', offset = 0, nextOffset = null, libraryGeneration = 0, libraryBusy = false;
    var avatarFiles = {hermes: 'moss', main: 'aqua', rion_local: 'coral', rios: 'amber'};
    function el(id) { return document.getElementById(id); }
    function setText(element, value) { if (element.textContent !== value) { element.textContent = value; } }
    function text(id, value) { setText(el(id), value); }
    function node(tag, label, className) { var value = document.createElement(tag); if (label !== undefined) { value.textContent = label; } if (className) { value.className = className; } return value; }
    function get(path, done) { return options.get(path, done, true); }
    function post(path, payload, done) { return options.post(path, payload, done, true); }
    function clock() {
      var now = new Date(); text('deviceTime', now.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}));
      text('deviceDate', now.toLocaleDateString([], {weekday: 'long', month: 'short', day: 'numeric'}));
    }
    function wakeUI(message) {
      text('keepAwake', awakeWanted ? 'Turn off keep awake' : 'Keep screen awake'); el('keepAwake').setAttribute('aria-pressed', String(awakeWanted));
      text('awakeStatus', message || (wakeLock ? 'This device will stay awake while this page is visible.' : 'Screen sleep follows this device’s settings.'));
    }
    function releaseWake(lock) {
      if (!lock) { return; }
      try { var result = lock.release(); if (result && result.catch) { result.catch(function () {}); } } catch (error) {}
    }
    function acquireWake() {
      if (!awakeWanted || document.hidden || !navigator.wakeLock || wakeLock || wakePending) { return; }
      var generation = ++wakeGeneration; wakePending = true;
      function denied() { if (generation !== wakeGeneration) { return; } wakePending = false; awakeWanted = false; wakeUI('Keep awake was unavailable or denied by this browser.'); }
      try {
        navigator.wakeLock.request('screen').then(function (lock) {
          if (!awakeWanted || generation !== wakeGeneration || document.hidden) { releaseWake(lock); return; }
          wakePending = false; wakeLock = lock; wakeUI();
          lock.addEventListener('release', function () { if (wakeLock === lock) { wakeLock = null; wakeUI(awakeWanted ? 'Keep awake paused by the browser. Return to this page or toggle it to retry.' : ''); } });
        }).catch(denied);
      } catch (error) { denied(); }
    }
    el('keepAwake').onclick = function () {
      if (el('keepAwake').disabled) { return; }
      awakeWanted = !awakeWanted; wakeGeneration++; wakePending = false;
      if (!awakeWanted && wakeLock) { var lock = wakeLock; wakeLock = null; releaseWake(lock); }
      wakeUI(); if (awakeWanted) { acquireWake(); }
    };
    if (!navigator.wakeLock || !window.isSecureContext) { el('keepAwake').disabled = true; wakeUI('Keep awake needs HTTPS and a supported browser.'); } else { wakeUI(); }
    function selectedAgent() { return agents.filter(function (agent) { return agent.id === selected; })[0]; }
    function canSend(agent) { return options.ready() && agent && agent.canSend === true && agent.status === 'active' && ['idle', 'working', 'waiting', 'thinking'].indexOf(agent.activity) >= 0; }
    function updateSend() {
      var agent = selectedAgent(), value = el('agentInput').value.trim(); el('agentSend').disabled = !canSend(agent) || !!sending || !!jobs[selected] || !value || value.length > 6000;
      text('agentSelectedName', agent ? agent.name : 'Choose an agent');
    }
    function renderConversation() {
      var container = el('agentConversation'); container.textContent = '';
      (conversations[selected] || []).forEach(function (item) { var bubble = node('article', undefined, 'bubble ' + (item.role === 'You' ? 'user' : '')); bubble.appendChild(node('div', item.role, 'role')); bubble.appendChild(node('div', item.text)); container.appendChild(bubble); });
      if (!container.children.length) { container.appendChild(node('p', selected ? 'Send a message or a task to this agent. It runs on your PC using its configured tools.' : 'Choose one of your agents above.', 'small')); }
      text('agentJobStatus', sending && sending === selected ? 'Sending…' : jobs[selected] ? jobs[selected].status : notices[selected] || 'Conversations stay in this page’s memory.'); updateSend();
    }
    function renderAgents() {
      var container = el('agentCards'), order = agents.map(function (agent) { return agent.id; }).join('|');
      agents.forEach(function (agent) {
        var row = agentRows[agent.id];
        if (!row) {
          var card = node('button', undefined, 'agent-card'); card.type = 'button';
          var avatar = node('span', undefined, 'agent-avatar agent-avatar--' + avatarFiles[agent.id]); avatar.setAttribute('aria-hidden', 'true');
          var img = node('img'); img.src = '/agent-avatars/' + avatarFiles[agent.id] + '.svg'; img.alt = ''; avatar.appendChild(img);
          row = {card: card, avatar: avatar, name: node('strong'), role: node('span', '', 'agent-role'), status: node('span', '', 'agent-state'), model: node('span', '', 'agent-model')};
          [row.avatar, row.name, row.role, row.status, row.model].forEach(function (child) { card.appendChild(child); });
          card.onclick = function () { if (selected !== agent.id) { selected = agent.id; el('agentInput').value = ''; } renderAgents(); renderConversation(); };
          agentRows[agent.id] = row;
        }
        var state = ['idle', 'working', 'waiting', 'thinking', 'attention', 'offline'].indexOf(agent.activity) >= 0 ? agent.activity : agent.activity === 'unavailable' ? 'attention' : 'unknown';
        var model = typeof agent.model === 'string' ? agent.model : agent.model && typeof agent.model.id === 'string' ? agent.model.id : '';
        var activityLabels = {idle: 'Ready', working: 'Working', waiting: 'Queued', thinking: 'Thinking', attention: 'Needs attention', offline: 'Offline', unavailable: 'Model unavailable'};
        row.card.setAttribute('aria-pressed', String(agent.id === selected)); row.avatar.setAttribute('data-state', state);
        setText(row.name, agent.name); setText(row.role, agent.role || 'Role not reported yet');
        setText(row.status, activityLabels[agent.activity] || 'Status unknown');
        setText(row.model, model); row.model.hidden = !model;
      });
      // Keep focused buttons and their children mounted across ordinary status polls.
      if (order !== agentOrder) { container.textContent = ''; agents.forEach(function (agent) { container.appendChild(agentRows[agent.id].card); }); agentOrder = order; }
      updateSend();
    }
    function refreshAgents() {
      if (!options.ready() || agentBusy) { return; } agentBusy = true;
      get('/api/agents/status', function (error, data) {
        agentBusy = false;
        if (error) { agents.forEach(function (agent) { agent.canSend = false; agent.activity = 'offline'; agent.status = 'Connection unavailable'; }); text('agentTeamStatus', error); text('homeTeamStatus', 'Agent hub unavailable'); renderAgents(); return; }
        var seen = {}; agents = (Array.isArray(data.agents) ? data.agents : []).filter(function (agent) { if (!agent || !Object.prototype.hasOwnProperty.call(avatarFiles, agent.id) || seen[agent.id]) { return false; } seen[agent.id] = true; return true; });
        if (!data.available) { agents.forEach(function (agent) { agent.canSend = false; }); }
        text('agentTeamStatus', data.message || 'Agent team connected.'); text('homeTeamStatus', data.available ? agents.length + ' agents · shared team hub' : data.message || 'Waiting for team setup');
        var removed = selected && !selectedAgent(); if (removed) { selected = ''; }
        renderAgents(); if (removed) { renderConversation(); } pollJobs();
      });
    }
    function pollJobs() {
      if (!options.ready()) { return; }
      Object.keys(jobs).forEach(function (agentId) {
        var job = jobs[agentId]; if (job.busy) { return; } job.busy = true;
        post('/api/agents/receipt', {receiptId: job.receiptId}, function (error, data) {
          if (jobs[agentId] !== job) { return; } job.busy = false;
          if (error) { job.status = error; if (data && data.status === 'unknown') { notices[agentId] = error; delete jobs[agentId]; } }
          else {
            if (data.agentId !== agentId || data.receiptId !== job.receiptId) { job.status = 'The receipt did not match this task. Check Team Hub.'; if (selected === agentId) { renderConversation(); } return; }
            job.status = data.status || 'Waiting for response';
            if (data.complete) { (conversations[agentId] || (conversations[agentId] = [])).push({role: job.name, text: data.reply || data.message || (data.status === 'completed' ? 'The task finished without a text reply.' : 'The task needs attention in Team Hub.')}); notices[agentId] = 'Task ' + job.status + '.'; delete jobs[agentId]; }
          }
          if (selected === agentId) { renderConversation(); }
        });
      });
    }
    el('agentInput').oninput = updateSend;
    el('agentForm').onsubmit = function (event) {
      event.preventDefault(); var agent = selectedAgent(), value = el('agentInput').value.trim();
      if (!canSend(agent) || !value || value.length > 6000 || jobs[selected] || sending) { return; }
      var agentId = selected; sending = agentId; notices[agentId] = ''; updateSend(); text('agentJobStatus', 'Sending to ' + agent.name + '…');
      post('/api/agents/send', {agentId: agentId, text: value}, function (error, data) {
        sending = '';
        if (error || !data || data.agentId !== agentId || !/^[A-Za-z0-9_-]{32}$/.test(data.receiptId || '')) { notices[agentId] = error || 'The request was not confirmed. Check Team Hub before trying again.'; if (selected === agentId) { text('agentJobStatus', notices[agentId]); } updateSend(); return; }
        (conversations[agentId] || (conversations[agentId] = [])).push({role: 'You', text: value});
        jobs[agentId] = {receiptId: data.receiptId, name: agent.name, status: data.status || 'Queued', busy: false};
        if (selected === agentId) { el('agentInput').value = ''; renderConversation(); } updateSend(); pollJobs();
      });
    };
    el('agentClear').onclick = function () { if (selected) { conversations[selected] = []; notices[selected] = ''; renderConversation(); } };
    el('agentsRefresh').onclick = refreshAgents;
    function isLibrary() { return el('comfySource').value === 'library'; }
    function refreshLibrary(reset) {
      el('libraryControls').hidden = !isLibrary(); if (!isLibrary() || !options.ready()) { return; }
      if (reset) { offset = 0; }
      var generation = ++libraryGeneration, requestedOffset = offset; libraryBusy = true; nextOffset = null; el('libraryPrevious').disabled = true; el('libraryNext').disabled = true; text('libraryStatus', 'Reading output library…');
      var url = '/api/comfy/library?folder=' + encodeURIComponent(folder) + '&search=' + encodeURIComponent(el('librarySearch').value.trim()) + '&media=' + encodeURIComponent(el('libraryType').value) + '&offset=' + offset + '&limit=24';
      get(url, function (error, data) {
        if (generation !== libraryGeneration || !isLibrary()) { return; } libraryBusy = false;
        if (error) { text('libraryStatus', error); text('libraryPage', ''); el('libraryPrevious').disabled = offset === 0; options.renderOutputs({available: false, outputs: [], emptyMessage: error}, true); return; }
        text('libraryStatus', data.message || 'Saved outputs');
        var folders = el('libraryFolders'); folders.textContent = '';
        (data.breadcrumbs || []).forEach(function (item) { var button = node('button', item.name, 'quiet'); button.onclick = function () { folder = item.id; refreshLibrary(true); }; folders.appendChild(button); });
        (data.folders || []).forEach(function (item) { var button = node('button', '▸ ' + item.name, 'quiet'); button.onclick = function () { folder = item.id; refreshLibrary(true); }; folders.appendChild(button); });
        offset = requestedOffset;
        nextOffset = typeof data.nextOffset === 'number' && Math.floor(data.nextOffset) === data.nextOffset && data.nextOffset > offset ? data.nextOffset : null;
        el('libraryPrevious').disabled = offset === 0; el('libraryNext').disabled = nextOffset === null;
        text('libraryPage', data.total ? (offset + 1) + '–' + Math.min(offset + (data.outputs || []).length, data.total) + ' of ' + data.total : 'No matches');
        options.renderOutputs({available: !!data.available, outputs: data.outputs || [], emptyMessage: data.available ? 'No saved outputs match these filters.' : data.message || 'The output library is unavailable.'}, true);
      });
    }
    el('comfySource').onchange = function () { libraryGeneration++; libraryBusy = false; folder = 'root'; offset = 0; nextOffset = null; el('libraryControls').hidden = !isLibrary(); if (isLibrary()) { refreshLibrary(); } else { options.showRecent(); } };
    el('librarySearchForm').onsubmit = function (event) { event.preventDefault(); refreshLibrary(true); };
    el('libraryType').onchange = function () { refreshLibrary(true); };
    el('libraryPrevious').onclick = function () { if (!libraryBusy && offset > 0) { offset = Math.max(0, offset - 24); refreshLibrary(); } };
    el('libraryNext').onclick = function () { if (!libraryBusy && nextOffset !== null && !el('libraryNext').disabled) { offset = nextOffset; refreshLibrary(); } };
    el('libraryOpen').onclick = function () { post('/api/comfy/library/open', {}, function (error, data) { text('libraryStatus', error || data.message); }); };
    document.addEventListener('visibilitychange', function () { if (!document.hidden) { clock(); acquireWake(); } else { wakeGeneration++; wakePending = false; var lock = wakeLock; wakeLock = null; releaseWake(lock); wakeUI(awakeWanted ? 'Keep awake resumes when this page is visible.' : ''); } });
    window.addEventListener('pagehide', function () { awakeWanted = false; wakeGeneration++; wakePending = false; libraryGeneration++; libraryBusy = false; var lock = wakeLock; wakeLock = null; releaseWake(lock); wakeUI(); });
    clock(); setInterval(function () { if (!document.hidden) { clock(); } }, 30000);
    setInterval(function () { if (!document.hidden && ['home', 'agents'].indexOf(options.page()) >= 0) { refreshAgents(); } }, 5000);
    return {connected: refreshAgents, onPage: function (name) { if (name === 'agents' || name === 'home') { refreshAgents(); } if (name === 'agents') { renderConversation(); } if (name === 'comfy' && isLibrary()) { refreshLibrary(); } else if (name !== 'comfy') { libraryGeneration++; libraryBusy = false; } }, isLibrary: isLibrary, refreshLibrary: refreshLibrary};
  };
}());
