'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {session: {}, room: {}, playback: {}, observer: {}, observations: [], memory: {}, messages: [], danmaku: [], danmaku_status: {}, danmaku_send: {}, danmaku_candidates: {}, danmaku_auto: {}, model: {}, audio: {segments: [], speakers: []}};
  const statusNames = {idle: '尚未连接', connecting: '连接中', waiting: '等待开播', live: '直播中', stopped: '已断开', error: '连接异常', reconnecting: '重新连接中'};
  const observerNames = {waiting_frames: '等待下一帧', waiting_model: '未配置模型', retrying: '重试观察中', idle: '等待直播', waiting: '等待画面', running: '持续观察中', observing: '正在观察', thinking: '正在观察', active: '持续观察中', ready: '已就绪', connecting: '连接模型中', stopped: '观察已停止', error: '观察异常', unavailable: '模型不可用', disabled: '未配置模型', unconfigured: '未配置模型'};
  let roomInputDirty = false;
  let hls = null, playbackKey = '', playerMode = 'native', overlayEnabled = true, sourceBusy = false;
  let eventSource = null, refreshTimer = null, requestVersion = 0, lastEventId = null, modelChecking = false;
  let danmakuTotal = 0, danmakuLane = 0, activeTab = 'activity', submittingQuestion = false;
  let speakerActionBusy = false, speakerMergeOptionsSignature = '';
  let danmakuSending = false, danmakuCredentialsSaving = false, danmakuCandidatesSignature = '';
  let danmakuAutoBusy = '';
  let recentDanmaku = [];
  let understandingSample = null, understandingSourceWatermark = null;
  let audioFilter = '', audioSpeakerSignature = '', activeSpeakerId = null, activeSpeakerSession = null, activeReplayId = null, restoreVideoUnmuted = false;
  const audioRows = new Map(), audioSaving = new Set();
  const audioRoleNames = {unknown:'来源不确定',host:'主播',background:'背景视频',singing:'歌声'};
  const seenEvents = new Set();
  const pendingQuestions = new Map();
  const localDrafts = new Map();
  const questionPlans = new Map();
  const openedPlans = new Set();
  const openedAudioEvidence = new Set();
  const activeStatuses = new Set(['queued', 'pending', 'running', 'streaming', 'planning', 'thinking', 'answering']);
  const make = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = String(text); return node; };
  const isObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
  const list = (value) => Array.isArray(value) ? value : [];
  const plain = (value) => { if (value === null || value === undefined) return ''; if (typeof value === 'string') return value; if (Array.isArray(value)) return value.map(plain).filter(Boolean).join('；'); if (isObject(value)) return Object.entries(value).map(([k,v]) => `${k}: ${plain(v)}`).join('；'); return String(value); };
  const shortened = (value, max = 900) => { const text = plain(value); return text.length > max ? `${text.slice(0, max)}…` : text; };
  const valueText = (entry) => plain(typeof entry === 'string' ? entry : entry?.text ?? entry?.summary ?? entry?.description ?? entry?.content ?? entry?.delta ?? entry?.event ?? entry);
  function formatTime(value) {
    if (value === undefined || value === null || value === '') return '';
    if (typeof value === 'number' && value < 1000000000) { const total = Math.max(0, Math.floor(value)); const hours = Math.floor(total / 3600); const mins = Math.floor(total / 60) % 60; const secs = total % 60; return `${hours ? `${hours}:` : ''}${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`; }
    const date = new Date(typeof value === 'number' && value < 100000000000 ? value * 1000 : value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString('zh-CN', {hour12:false, hour:'2-digit', minute:'2-digit', second:'2-digit'});
  }
  function notice(message, error = false) { $('notice-text').textContent = message; $('notice').classList.toggle('error', error); $('notice').hidden = !message; }
  function setDot(node, kind) { node.className = `status-dot${kind ? ` ${kind}` : ''}`; }
  async function api(path, options = {}) {
    const response = await fetch(path, {...options, headers: {'Content-Type':'application/json', ...(options.headers || {})}});
    let body; try { body = await response.json(); } catch { body = {}; }
    if (!response.ok) throw new Error(plain(body.detail ?? body.error ?? body.message) || `请求失败（${response.status}）`);
    return body;
  }
  function resetSession() {
    pendingQuestions.clear(); localDrafts.clear(); questionPlans.clear(); openedPlans.clear(); openedAudioEvidence.clear(); danmakuTotal = 0;
    state.observations = []; state.messages = []; state.danmaku = []; state.memory = {}; state.observer = {};
    understandingSample = null; understandingSourceWatermark = null;
    state.danmaku_candidates = {}; state.danmaku_auto = {}; danmakuCandidatesSignature = ''; recentDanmaku = []; $('danmaku-copy-fallback').hidden = true;
    if ($('danmaku-auto-feedback')) $('danmaku-auto-feedback').hidden = true;
    $('danmaku-overlay').replaceChildren();
    stopAudioReplay(); audioRows.clear(); audioSaving.clear(); audioFilter = ''; audioSpeakerSignature = ''; state.audio = {segments: [], speakers: []};
    if ($('speaker-dialog').open) $('speaker-dialog').close(); activeSpeakerId = null; activeSpeakerSession = null;
    stopPlayer();
  }
  function applyState(incoming) {
    if (!incoming || !isObject(incoming)) return;
    const data = incoming.state && isObject(incoming.state) ? incoming.state : incoming;
    if (data.session?.id && state.session.id && data.session.id !== state.session.id) resetSession();
    for (const key of ['session','room','playback','observer','memory','model','danmaku_status','danmaku_send','danmaku_candidates','danmaku_auto','audio']) if (isObject(data[key])) state[key] = data[key];
    for (const key of ['observations','messages','danmaku']) if (Array.isArray(data[key])) state[key] = data[key];
    if (isObject(data.observer)) sampleUnderstandingState(data.observer,true);
    if (Array.isArray(data.observations)) rememberObservationSources(data.observations);
    for (const message of state.messages) { const qid = message.question_id; if (qid && message.role === 'assistant' && message.status && !activeStatuses.has(message.status)) { pendingQuestions.delete(qid); localDrafts.delete(qid); } }
    danmakuTotal = Math.max(danmakuTotal, state.danmaku.length);
    renderAll();
  }
  async function refreshState() {
    const version = ++requestVersion;
    try { const next = await api('/api/state'); if (version === requestVersion) applyState(next); }
    catch (error) { $('events-status').textContent = '工作台连接异常'; setDot($('events-dot'), 'bad'); }
  }
  function queueRefresh(delay = 700) { if (refreshTimer) return; refreshTimer = setTimeout(() => { refreshTimer = null; refreshState(); }, delay); }
  function sessionActive() { return Boolean(state.session.id) && !['idle','stopped','error'].includes(state.session.status); }
  function renderAll() { renderSession(); renderModel(); renderObserver(); renderObservations(); renderMemory(); renderMessages(); renderDanmaku(); renderDanmakuComposer(); renderAudio(); setupPlayer(); }
  function renderSession() {
    const status = state.session.status || 'idle';
    $('room-status').textContent = statusNames[status] || status;
    setDot($('room-dot'), status === 'live' ? 'good' : ['connecting','reconnecting'].includes(status) ? 'busy' : status === 'waiting' ? 'warn' : status === 'error' ? 'bad' : '');
    const roomId = state.room.room_id || state.room.id;
    let currentRoom = state.session.room_input || (roomId ? `https://live.bilibili.com/${roomId}` : '');
    if (/^\d+$/.test(currentRoom)) currentRoom = `https://live.bilibili.com/${currentRoom}`;
    if (currentRoom && !roomInputDirty && $('room-input').value !== currentRoom) $('room-input').value = currentRoom;
    $('room-id').textContent = roomId ? `房间 ${roomId}` : 'Bilibili';
    $('room-title').textContent = state.room.title || (status === 'waiting' ? '直播间尚未开播' : '等待连接直播间');
    if (roomId && /^\d+$/.test(String(roomId))) $('open-room').href = `https://live.bilibili.com/${roomId}`;
    $('connect-button').disabled = sourceBusy;
    $('connect-label').textContent = sourceBusy ? '正在连接…' : sessionActive() ? '切换直播' : '连接直播';
    $('stop-button').disabled = sourceBusy || !sessionActive();
    $('question-input').disabled = !sessionActive();
    $('send-button').disabled = !sessionActive() || !$('question-input').value.trim();
    $('composer-hint').textContent = sessionActive() ? 'Enter 发送 · Shift + Enter 换行' : '连接直播间后即可提问';
    const chatStatus = state.danmaku_status || {};
    $('danmaku-status').textContent = ({connected:'弹幕已连接', connecting:'弹幕连接中', disconnected:'弹幕未连接', reconnecting:'弹幕重连中', error:'弹幕连接异常'})[chatStatus.status] || (status === 'waiting' ? '等待开播' : '等待连接');
    $('danmaku-status').title = plain(chatStatus.reason);
    const titles = {idle:'连接直播，开始持续理解', connecting:'正在连接直播间', waiting:'直播间尚未开播', stopped:'直播连接已断开', error:'暂时无法连接直播间', reconnecting:'直播连接正在恢复', live:'正在加载直播画面'};
    const descriptions = {idle:'直播画面、实时弹幕，与始终在线的观察助手。', connecting:'正在解析直播地址，并建立画面与弹幕连接。', waiting:'工作台会等待开播，开播后自动开始观察。', stopped:'已有的观察与问答保留在右侧，可重新连接直播。', error:state.session.error || '请检查直播间地址，稍后重新连接。', reconnecting:'恢复连接后会继续观察和记录。', live:'已连接直播间，等待播放器接收视频。'};
    $('placeholder-title').textContent = titles[status] || titles.idle;
    $('placeholder-description').textContent = descriptions[status] || descriptions.idle;
    $('placeholder-note').textContent = status === 'waiting' ? '直播未开始时，不会生成画面观察' : '观察持续进行，随时都能提问';
  }
  function renderModel() {
    const model = state.model || {};
    if (model.name) $('model-name').textContent = model.name.replace(/^Qwen\//, '').replaceAll('-', ' · ');
    const ready = state.observer.model_ready ?? model.ready;
    const configured = model.configured;
    $('model-status').textContent = ready === true ? '已连接' : ready === false && configured ? '待就绪' : configured === false ? '未配置' : configured ? '已配置' : '待检查';
    setDot($('model-dot'), ready === true ? 'good' : configured === false ? 'warn' : configured ? 'busy' : '');
  }
  function renderObserver() {
    const observer = state.observer || {};
    const status = observer.status || 'idle';
    const running = ['running','observing','active','thinking'].includes(status);
    $('observer-status').textContent = observerNames[status] || status;
    $('observer-badge').classList.toggle('running', running);
    setDot($('observer-badge').querySelector('.status-dot'), running ? 'good' : status === 'error' ? 'bad' : ['waiting','waiting_model','retrying','unconfigured','disabled','unavailable'].includes(status) ? 'warn' : '');
    const frameCount = observer.adaptive_frame_count ?? observer.observation_frames;
    const batchInfo = frameCount ? `${frameCount} 帧窗口 · ${observer.observation_concurrency || 1} 路异步观察` : '观察任务与问答独立运行';
    const lag = Number(observer.completion_lag);
    $('observer-detail').textContent = observer.error ? shortened(observer.error, 160) : batchInfo + (observer.completion_lag != null && Number.isFinite(lag) ? ` · 处理滞后 ${lag.toFixed(1)} 秒` : '');
    $('observer-detail').title = plain(observer.error);
    $('dropped-frames').textContent = [observer.dropped_frames ? `跳过 ${observer.dropped_frames} 帧` : '', observer.stale_results ? `丢弃 ${observer.stale_results} 个迟到结果` : ''].filter(Boolean).join(' · ');
    const timestamp = observer.last_timestamp ?? contentRange([...list(state.observations)].sort(compareContentTime).at(-1))?.end;
    $('observation-time').textContent = timestamp !== undefined && timestamp !== null ? `内容 ${formatTime(timestamp)}` : '持续记录';
    renderUnderstandingTiming();
  }
  function understandingAge() {
    return understandingSample ? understandingSample.age + Math.max(0,performance.now() - understandingSample.sampledAt) / 1000 : null;
  }
  function sampleUnderstandingState(observer,complete = false) {
    if (!Object.hasOwn(observer,'understanding_age_seconds')) { if (complete) understandingSample = null; return; }
    const age = finiteTime(observer.understanding_age_seconds), source = finiteTime(observer.source_received_at);
    if (age === null) { understandingSample = null; return; }
    if (source !== null && understandingSample?.sourceReceivedAt != null && source < understandingSample.sourceReceivedAt) return;
    const previousAge = understandingAge();
    const sourceAgeFloor = source !== null && understandingSample?.sourceReceivedAt != null ? understandingSample.sourceReceivedAt + previousAge - source : 0;
    // Repeated status snapshots for one observation must not make it younger.
    const sameSource = understandingSample && source === understandingSample.sourceReceivedAt;
    // A delayed snapshot may describe a newer source while its sampled age is old.
    understandingSample = {age:Math.max(0,age,sourceAgeFloor,sameSource ? previousAge : 0),sampledAt:performance.now(),sourceReceivedAt:source};
    if (source !== null) understandingSourceWatermark = Math.max(understandingSourceWatermark ?? source,source);
  }
  function isVisualObservation(entry) { return entry.frame_count > 0 || (entry.kind === 'observation' && entry.frame_count !== 0); }
  function rememberObservationSources(entries) {
    for (const entry of entries) {
      const source = finiteTime(entry.source_received_at);
      if (isVisualObservation(entry) && source !== null) understandingSourceWatermark = Math.max(understandingSourceWatermark ?? source,source);
    }
  }
  function sampleUnderstandingObservation(entry,eventTime) {
    if (!isVisualObservation(entry)) return;
    const source = finiteTime(entry.source_received_at), generated = finiteTime(entry.generated_at);
    if (source === null || generated === null || source > generated || (understandingSourceWatermark !== null && source <= understandingSourceWatermark)) return;
    // Use server wall times only. The browser's clock may differ from the server's.
    const estimatedServerNow = understandingSample?.sourceReceivedAt == null ? generated : understandingSample.sourceReceivedAt + understandingAge();
    const age = Math.max(generated,finiteTime(eventTime) ?? generated,estimatedServerNow) - source;
    understandingSample = {age:Math.max(0,age),sampledAt:performance.now(),sourceReceivedAt:source};
    understandingSourceWatermark = source;
    if (finiteTime(entry.update_interval_seconds) !== null) state.observer.observed_update_interval_seconds = Number(entry.update_interval_seconds);
    if (finiteTime(entry.target_lag_seconds) !== null) state.observer.target_lag_seconds = Number(entry.target_lag_seconds);
  }
  function renderUnderstandingTiming() {
    const observer = state.observer || {}, age = understandingAge(), target = finiteTime(observer.target_lag_seconds) ?? 15;
    const running = sessionActive(), stopped = Boolean(state.session.id) && !running;
    const status = stopped ? 'stopped' : age === null ? 'waiting' : age > target ? 'overdue' : 'fresh';
    const node = $('understanding-timing');
    node.dataset.freshness = status;
    node.dataset.ageSeconds = age === null ? '' : age.toFixed(3);
    $('understanding-age').textContent = stopped ? '观察已停止 · 保留已有记录' : age === null ? `等待新的观察 · 目标 ${target} 秒` : `理解落后 ${age.toFixed(1)} 秒 · 目标 ${target} 秒${status === 'overdue' ? ' · 已超出目标' : ''}`;
    const interval = finiteTime(observer.observed_update_interval_seconds), frames = finiteTime(observer.adaptive_frame_count);
    $('understanding-cadence').textContent = [interval === null ? '更新间隔待统计' : `上次更新间隔 ${interval.toFixed(1)} 秒`,frames !== null ? `${observer.adaptive_observation ? '自适应 ' : ''}${frames} 帧观察` : ''].filter(Boolean).join(' · ');
    node.title = '理解落后：当前已输出观察对应末帧被本机收到后经过的时间；不使用媒体 PTS，也不包含平台上游延迟。上次更新间隔：最近两次画面理解实际输出的时间差。';
  }
  const sourceNames = {visual:'画面', audio:'声音', danmaku:'弹幕'};
  function sourceTags(entry) {
    const tags = list(entry?.source_tags).filter(tag => sourceNames[tag]);
    if (!tags.length) {
      if (entry?.visual_delta || entry?.frame_count) tags.push('visual');
      if (entry?.audio_delta) tags.push('audio');
      if (entry?.danmaku_delta) tags.push('danmaku');
    }
    return [...new Set(tags)];
  }
  function sourceChips(tags) { const node = make('div','source-tags'); tags.forEach(tag => node.append(make('span',`source-tag ${tag}`,sourceNames[tag]))); return node; }
  const finiteTime = value => value !== undefined && value !== null && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;
  function timeRange(start,end) {
    start = finiteTime(start); end = finiteTime(end);
    if (start === null && end === null) return null;
    start ??= end; end ??= start;
    return {start:Math.min(start,end),end:Math.max(start,end)};
  }
  function combineRanges(ranges) { const valid = ranges.filter(Boolean); return valid.length ? {start:Math.min(...valid.map(item => item.start)),end:Math.max(...valid.map(item => item.end))} : null; }
  function evidenceRange(entry,source) {
    if (source === 'visual') return timeRange(entry.window_start,entry.window_end);
    if (source === 'audio') return combineRanges(list(entry.audio_evidence).map(item => timeRange(item.start ?? item.t0,item.end ?? item.t1)));
    return combineRanges(list(entry.danmaku_evidence ?? entry.chat_evidence).map(item => timeRange(item.timeline_timestamp ?? item.timestamp ?? item.t,item.timeline_timestamp ?? item.timestamp ?? item.t)));
  }
  function contentRange(entry) {
    if (!isObject(entry)) return null;
    const explicit = timeRange(entry.source_start,entry.source_end);
    if (explicit) return explicit;
    if (entry.frame_count) return evidenceRange(entry,'visual') || timeRange(entry.timestamp ?? entry.t,entry.timestamp ?? entry.t);
    const evidence = combineRanges([evidenceRange(entry,'audio'),evidenceRange(entry,'danmaku')]);
    return evidence || timeRange(entry.t0 ?? entry.timestamp ?? entry.t ?? entry.time,entry.t1 ?? entry.timestamp ?? entry.t ?? entry.time);
  }
  function rangeText(range) { if (!range) return ''; const start = formatTime(range.start), end = formatTime(range.end); return start === end ? start : `${start}—${end}`; }
  function compareContentTime(a,b) {
    const diff = (contentRange(a)?.end ?? -Infinity) - (contentRange(b)?.end ?? -Infinity);
    return (Number.isNaN(diff) ? 0 : diff) || String(a.id ?? a.eid ?? '').localeCompare(String(b.id ?? b.eid ?? ''),undefined,{numeric:true});
  }
  function captureScrollAnchor(panel,followEnd=false) {
    const top = panel.getBoundingClientRect().top;
    const visible = [...panel.querySelectorAll('[data-timeline-key]')].find(node => { const rect = node.getBoundingClientRect(); return rect.bottom > top && rect.top < top + panel.clientHeight; });
    return {scrollTop:panel.scrollTop,atEnd:followEnd && panel.scrollHeight - panel.scrollTop - panel.clientHeight < 80,key:visible?.dataset.timelineKey,offset:visible ? visible.getBoundingClientRect().top - top : 0};
  }
  function restoreScrollAnchor(panel,anchor) {
    if (anchor.atEnd) { panel.scrollTop = panel.scrollHeight; return; }
    panel.scrollTop = anchor.scrollTop;
    const node = anchor.key && [...panel.querySelectorAll('[data-timeline-key]')].find(item => item.dataset.timelineKey === anchor.key);
    if (node) panel.scrollTop += node.getBoundingClientRect().top - panel.getBoundingClientRect().top - anchor.offset;
  }
  function audioAlignmentNote(entry) {
    const status = entry.audio_alignment?.status;
    const hasText = list(entry.audio_evidence).some(item => plain(item.text).trim());
    if (status === 'pending') return '同期音频待到 · 到达后按原时间补记';
    if (status === 'partial') return hasText ? '同期音频尚未齐 · 已到片段见下方' : '同期音频尚未齐 · 暂无可用转写';
    if (status === 'unavailable') return '同期音频暂无可用转写';
    if (status === 'complete' && !hasText) return '同期音频已处理 · 暂无可用转写';
    return '';
  }
  function renderObservations() {
    const entries = [...list(state.observations)].sort(compareContentTime).slice(-160);
    $('observation-count').textContent = String(state.observations.length);
    const panel = $('activity-panel');
    if (!entries.length) { if (!$('activity-empty')) { const empty = make('div','assistant-empty'); empty.id = 'activity-empty'; empty.append(make('span','empty-orbit','✧'), make('strong','','捕捉每一次变化'), make('p','','连接后，画面、声音和弹幕的变化会持续写入记忆。'), make('span','','回答你的问题时，观察也会继续。')); panel.replaceChildren(empty); } return; }
    const scrollAnchor = captureScrollAnchor(panel,true);
    const nodes = entries.map(entry => {
      const tags = sourceTags(entry);
      const contextOnly = ['context','context_delta'].includes(entry.kind) || entry.frame_count === 0;
      const node = make('article',`activity-item${contextOnly ? ' context-item' : ''}`); node.dataset.observationId = entry.id ?? ''; node.dataset.timelineKey = `observation:${entry.id ?? entry.timestamp}`; node.dataset.kind = entry.kind || 'observation';
      const meta = make('div','activity-meta');
      const sourceTime = contentRange(entry), windowTime = rangeText(sourceTime);
      if (sourceTime) { node.dataset.sourceStart = sourceTime.start; node.dataset.sourceEnd = sourceTime.end; }
      const kind = contextOnly ? `${tags.filter(tag => tag !== 'visual').map(tag => sourceNames[tag]).join(' / ') || '来源'}摘录` : entry.frame_count ? `${entry.frame_count} 帧${tags.length > 1 ? '综合观察' : '画面观察'}` : '画面观察';
      meta.append(make('time','',windowTime ? `内容 ${windowTime}` : '内容时间待定'), make('span','kind',kind));
      const timingDetails = [contextOnly ? '按原内容时间排列。声音为原音频时间，弹幕为接收时间；此卡为原文摘录。' : '按画面内容时间排列；声音与弹幕各自标注证据时间。'];
      if (entry.inference_seconds != null) timingDetails.push(`模型推理 ${Number(entry.inference_seconds).toFixed(2)} 秒`);
      if (entry.completion_lag != null) timingDetails.push(`从最后一帧接收到输出 ${Number(entry.completion_lag).toFixed(2)} 秒`);
      if (finiteTime(entry.update_interval_seconds) !== null) timingDetails.push(`距上一条画面理解更新 ${Number(entry.update_interval_seconds).toFixed(2)} 秒`);
      if (finiteTime(entry.recorded_at) !== null) timingDetails.push(`处理完成于会话 ${formatTime(entry.recorded_at)}`);
      if (entry.generated_at) timingDetails.push(`生成于 ${formatTime(entry.generated_at)}`);
      meta.title = timingDetails.join('；');
      node.append(meta);
      if (tags.length) node.append(sourceChips(tags));
      const alignmentNote = audioAlignmentNote(entry);
      if (alignmentNote) { const note = make('div','audio-alignment-note',alignmentNote); note.dataset.alignmentStatus = entry.audio_alignment.status; if (finiteTime(entry.audio_alignment.wait_seconds) !== null) note.title = `本窗口等待音频 ${Number(entry.audio_alignment.wait_seconds).toFixed(1)} 秒`; node.append(note); }
      const description = entry.description || entry.observation || entry.text;
      if (description) node.append(make('p','',description));
      let deltas = 0;
      for (const [source,key,title] of [['visual','visual_delta','画面变化'],['audio','audio_delta','声音变化'],['danmaku','danmaku_delta','弹幕变化']]) {
        if (entry[key] && plain(entry[key]).trim()) { const delta = make('div',`delta-block source-${source}`), evidenceTime = rangeText(evidenceRange(entry,source)); const label = `Δ ${title}` + (evidenceTime ? ` · ${source === 'danmaku' ? '接收 ' : ''}${evidenceTime}` : ''); delta.append(make('span','delta-label',label),document.createTextNode(plain(entry[key]))); node.append(delta); deltas++; }
      }
      if (!deltas && entry.delta && plain(entry.delta).trim()) { const delta = make('div','delta-block'); delta.append(make('span','delta-label','Δ 变化记录'),document.createTextNode(plain(entry.delta))); node.append(delta); }
      const entities = [...list(entry.entities), ...list(entry.actors), ...list(entry.objects)].slice(0,6).map(value => typeof value === 'string' ? value : value.name || value.label || plain(value));
      if (entities.length) { const tagNode = make('div','observation-tags'); entities.forEach(tag => tagNode.append(make('span','',tag))); node.append(tagNode); }
      return node;
    });
    panel.replaceChildren(...nodes); restoreScrollAnchor(panel,scrollAnchor);
  }
  function roleBasisLabel(basis) { return ({manual_segment:'用户标注 · 当前片段',manual_voice_reference:'用户绑定 · 同声纹来源参考'})[basis] || '尚未绑定来源'; }
  function memorySection(title,entries,empty) { const section = make('section','memory-section'); const heading = make('h3','',title); heading.append(make('span','',`${entries.length} 条`)); section.append(heading); if (!entries.length) section.append(make('p','memory-empty',empty)); return section; }
  function renderMemory() {
    const memory = state.memory || {};
    const panel = $('memory-panel'), scrollAnchor = captureScrollAnchor(panel);
    const nodes = []; let total = 0;
    const speakers = list(memory.speakers); total += speakers.length;
    const speakerSection = memorySection('说话人绑定',speakers,'在声音面板点击说话人，可设置名称和来源参考。'); speakerSection.dataset.memorySection = 'speakers';
    speakers.forEach(speaker => {
      const card = make('div','memory-card memory-speaker-card'); card.dataset.speakerId = String(speaker.id); card.dataset.timelineKey = `speaker:${speaker.id}`;
      const meta = make('div','memory-audio-meta'); const name = make('strong','',speakerName(speaker)); name.title = speakerName(speaker); meta.append(name,make('span',`audio-role ${audioRole(speaker.role)}`,audioRoleNames[audioRole(speaker.role)]));
      const button = make('button','memory-edit-speaker','编辑'); button.type = 'button'; button.title = '编辑名称 / 来源或合并重复说话人'; button.disabled = !speakerList().some(item => String(item.id) === String(speaker.id)); button.addEventListener('click',() => openSpeakerDialog(speaker.id)); meta.append(button); card.append(meta);
      const detail = make('div','memory-speaker-detail'); detail.append(make('span','memory-basis',roleBasisLabel(speaker.role_basis))); if (speaker.samples != null) detail.append(make('span','memory-samples',`${speaker.samples} 次声纹参考`)); card.append(detail); speakerSection.append(card);
    });
    if (speakers.length) speakerSection.append(make('p','memory-empty','绑定会更新声音记忆并供后续提问使用；已完成的回答保留当时记录。'));
    nodes.push(speakerSection);
    const audio = list(memory.audio); total += audio.length;
    const audioSection = memorySection('声音记忆',audio,'转写后的声音和手动来源标注将在这里保留。'); audioSection.dataset.memorySection = 'audio';
    audio.slice(-40).reverse().forEach(segment => {
      const card = make('div','memory-card memory-audio-card'); card.dataset.segmentId = String(segment.id); card.dataset.timelineKey = `audio:${segment.id}`;
      const meta = make('div','memory-audio-meta'); const time = `${formatTime(segment.start ?? segment.t0)}—${formatTime(segment.end ?? segment.t1)}`;
      meta.append(make('small','',time),make('span','audio-speaker-label',speakerName(segment)),make('span',`audio-role ${audioRole(segment.role)}`,audioRoleNames[audioRole(segment.role)])); card.append(meta,make('p','',valueText(segment)));
      const actions = make('div','memory-audio-actions'); actions.append(make('span','memory-basis',roleBasisLabel(segment.role_basis)));
      if (audioClipUrl(segment.audio_url)) { const button = make('button','audio-play-button','▷ 回听'); button.type = 'button'; button.addEventListener('click',() => { playAudioSegment(segment,String(segment.id)); $('audio-replay').scrollIntoView({behavior:'smooth',block:'nearest'}); }); actions.append(button); }
      card.append(actions); audioSection.append(card);
    });
    nodes.push(audioSection);
    const groups = [['working','工作记忆','最近的画面、声音与弹幕变化'],['episodes','事件记忆','连续事件的摘要'],['pins','关键记忆','值得长期保留的信息']];
    groups.forEach(([key,title,description]) => {
      const entries = key === 'working' ? [...list(memory[key])].sort(compareContentTime) : list(memory[key]); total += entries.length;
      const section = memorySection(title,entries,description + '将在这里积累'); section.dataset.memorySection = key;
      entries.slice(-40).forEach((entry,index) => { const card = make('div','memory-card'); card.dataset.timelineKey = `${key}:${entry?.id ?? entry?.eid ?? index}`; const range = contentRange(entry); if (range) { card.append(make('small','',`内容 ${rangeText(range)}`)); card.dataset.sourceEnd = range.end; } const tags = sourceTags(entry); if (tags.length) card.append(sourceChips(tags)); card.append(document.createTextNode(valueText(entry))); section.append(card); });
      nodes.push(section);
    });
    panel.replaceChildren(...nodes); restoreScrollAnchor(panel,scrollAnchor); $('memory-count').textContent = String(total);
  }
  function renderMessages() {
    const messages = [...list(state.messages)];
    for (const [qid,draft] of localDrafts) {
      const existing = messages.find(message => message.role === 'assistant' && message.question_id === qid);
      if (!existing) messages.push({id:`local-${qid}`, question_id:qid, role:'assistant', text:draft.text, status:draft.status});
      else if (activeStatuses.has(existing.status) && draft.text && draft.text.length > plain(existing.text).length) Object.assign(existing, {text:draft.text});
    }
    const panel = $('messages');
    if (!messages.length) { if (!$('chat-empty')) { const empty = make('div','chat-empty'); empty.id = 'chat-empty'; empty.append(make('p','','关于直播的任何问题，都可以从这里开始。')); panel.replaceChildren(empty); } $('chat-status').textContent = '共享直播记忆'; return; }
    const previousScrollTop = panel.scrollTop;
    const nearBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 100;
    const nodes = messages.map((message) => {
      const user = message.role === 'user'; const node = make('article',`message ${user ? 'user' : 'assistant'}`);
      const meta = make('div','message-meta'); meta.append(make('span','message-avatar',user ? '你' : 'Δ'), make('span','',user ? '你' : 'AI同担')); node.append(meta);
      const content = plain(message.text ?? message.content ?? message.answer);
      const working = activeStatuses.has(message.status);
      node.append(make('div','message-content',content || (working ? '正在结合当前画面与记忆思考…' : '')));
      if (!user && (message.status || pendingQuestions.has(message.question_id))) {
        const statusText = {queued:'问题已排队', pending:'准备回答', running:'正在思考', streaming:'正在回答', planning:'正在规划', thinking:'正在思考', answering:'正在回答', complete:'回答完成', completed:'回答完成', done:'回答完成', error:'回答失败', cancelled:'已停止回答', canceled:'已停止回答'};
        const status = make('div',`message-status${message.status === 'error' ? ' error' : ''}`,statusText[message.status] || message.status || '正在回答');
        if ((working || pendingQuestions.has(message.question_id)) && message.question_id) { const button = make('button','cancel-question','停止本次回答'); button.type = 'button'; button.addEventListener('click',() => cancelQuestion(message.question_id,button)); status.append(button); }
        node.append(status);
      }
      const plan = questionPlans.get(message.question_id);
      if (!user && plan?.plan) { const detail = make('details','question-plan'); detail.open = openedPlans.has(message.question_id); detail.addEventListener('toggle',() => { if (detail.open) openedPlans.add(message.question_id); else openedPlans.delete(message.question_id); }); const heading = make('summary','','回答计划'); detail.append(heading,make('p','',plan.plan)); if (list(plan.replay_times).length) detail.append(make('span','','回看画面 · ' + plan.replay_times.map(formatTime).join(' / '))); node.append(detail); }
      if (message.evidence && plain(message.evidence)) { const entries = list(message.evidence); const block = make('div','evidence-block','参考记录 · '); if (entries.length) entries.forEach((item,index) => { if (index) block.append(document.createTextNode(' · ')); const isAudio = item.type === 'audio' || item.source === 'audio' || /\.wav(?:$|\?)/i.test(item.url || ''); const point = item.timestamp ?? item.start ?? item.t0; const label = (isAudio ? '音频 ' : '') + (point !== undefined ? formatTime(point) : shortened(item.text ?? item,120)); const safe = safePlayerUrl(item.url); if (safe && new URL(safe).origin === location.origin && new URL(safe).pathname.startsWith('/api/sessions/')) { const link = make('a','evidence-link',label + ' ↗'); link.href = safe; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.title = isAudio ? '回听原始音频证据' : '查看原始证据帧'; block.append(link); } else block.append(document.createTextNode(label)); }); else block.append(document.createTextNode(shortened(message.evidence,700))); node.append(block); }
      if (!user && list(message.audio_evidence).length) node.append(buildAudioEvidence(message));
      return node;
    });
    panel.replaceChildren(...nodes); panel.scrollTop = nearBottom ? panel.scrollHeight : previousScrollTop;
    const active = messages.filter(m => m.role === 'assistant' && activeStatuses.has(m.status)).length;
    $('chat-status').textContent = active ? `${active} 个问题处理中 · 观察继续` : '共享直播记忆';
  }
  function buildAudioEvidence(message) {
    const entries = list(message.audio_evidence);
    const key = message.question_id || message.id;
    const detail = make('details','audio-evidence'); detail.open = openedAudioEvidence.has(key);
    detail.addEventListener('toggle',() => { if (detail.open) openedAudioEvidence.add(key); else openedAudioEvidence.delete(key); });
    detail.append(make('summary','',`声音证据 · ${entries.length} 段`));
    entries.forEach(segment => {
      const row = make('div','audio-evidence-item');
      const meta = make('div','audio-evidence-meta');
      const start = segment.start ?? segment.timestamp, end = segment.end;
      const time = formatTime(start) + (end !== undefined ? `—${formatTime(end)}` : '');
      const url = audioClipUrl(segment.audio_url || segment.url);
      if (url) { const button = make('button','audio-play-button',`▷ 回听 ${time}`); button.type = 'button'; button.title = '回听原始音频证据'; button.addEventListener('click',() => { playAudioSegment({...segment,audio_url:url},String(segment.id || `evidence-${key}-${start}`)); $('audio-replay').scrollIntoView({behavior:'smooth',block:'nearest'}); }); meta.append(button); }
      else meta.append(make('time','',time));
      meta.append(make('span','',speakerName({...segment,speaker_id:segment.speaker_id ?? null})));
      if (segment.role && segment.role !== 'unknown') meta.append(make('span','',audioRoleNames[audioRole(segment.role)]));
      row.append(meta,make('p','',plain(segment.text) || '此音频片段没有可用的转写文字'));
      detail.append(row);
    });
    detail.append(make('p','audio-evidence-note','转写和说话人匹配可能有误；音频过期后仍保留文字记录。'));
    return detail;
  }
  function speakerList() {
    const speakers = state.audio?.speakers;
    return Array.isArray(speakers) ? speakers : isObject(speakers) ? Object.entries(speakers).map(([id, value]) => ({id, ...(isObject(value) ? value : {label: String(value)})})) : [];
  }
  function knownSpeaker(id) { return id !== null && id !== undefined && !['', 'unknown', 'unassigned', '-1'].includes(String(id).toLowerCase()); }
  function speakerName(segment) {
    const id = Object.hasOwn(segment, 'speaker_id') ? segment.speaker_id : segment.id;
    if (!knownSpeaker(id)) return '未知说话人';
    const speaker = speakerList().find(item => String(item.id) === String(id));
    const label = plain(segment.speaker_label || segment.label || speaker?.label);
    return label ? /^[A-Z]$/.test(label) ? `说话人 ${label}` : label : `说话人 ${id}`;
  }
  function audioRole(role) { return audioRoleNames[role] ? role : 'unknown'; }
  function audioClipUrl(value) { const url = safePlayerUrl(value); return url && new URL(url).origin === location.origin ? url : ''; }
  function renderAudio() {
    const audio = state.audio || {};
    const status = audio.status || 'idle';
    const names = {idle:'等待音轨', waiting:'等待音轨', waiting_audio:'等待音轨', no_audio_stream:'未发现直播音轨', waiting_model:'等待语音模型', connecting:'连接语音服务', starting:'启动语音识别', loading:'加载语音模型', running:'持续转写中', live:'持续转写中', ready:'语音服务就绪', processing:'正在转写', transcribing:'正在转写', listening:'接收音频中', error:'语音识别异常', retrying:'语音识别重试中', stopped:'转写已停止', disabled:'未启用语音识别', unavailable:'语音模型不可用'};
    const audioError = audio.error || audio.capture_error;
    const running = !audioError && ['live','running','processing','transcribing','listening'].includes(status);
    $('audio-status').textContent = names[status] || status;
    $('audio-badge').classList.toggle('running',running);
    setDot($('audio-badge').querySelector('.status-dot'), running ? 'good' : audioError || status === 'error' ? 'bad' : ['waiting_model','unavailable','retrying','no_audio_stream'].includes(status) ? 'warn' : '');
    const segments = list(audio.segments);
    $('audio-count').textContent = String(segments.filter(segment => segment.is_final !== false && segment.status !== 'partial').length);
    const metrics = [];
    if (audio.received_segments != null) metrics.push(`接收 ${audio.received_segments} 段`);
    if (audio.processed_segments != null) metrics.push(`已转写 ${audio.processed_segments} 段`);
    if (audio.inference_seconds != null && Number.isFinite(Number(audio.inference_seconds))) metrics.push(`最近推理 ${Number(audio.inference_seconds).toFixed(1)} 秒`);
    $('audio-detail').textContent = audioError ? shortened(audioError,200) : metrics.join(' · ') || '语音识别与画面观察独立进行';
    $('audio-detail').title = plain(audioError);
    $('audio-dropped').textContent = audio.dropped_segments ? `跳过 ${audio.dropped_segments} 段` : '';
    const speakers = speakerList().filter(speaker => knownSpeaker(speaker.id));
    const speakerSignature = JSON.stringify(speakers.map(({id,label,role,role_basis}) => ({id,label,role,role_basis})));
    if (speakerSignature !== audioSpeakerSignature) {
      audioSpeakerSignature = speakerSignature;
      const pills = speakers.map(speaker => { const button = make('button','speaker-pill'); button.type = 'button'; button.dataset.speakerId = String(speaker.id); button.title = '设置同声纹来源参考；不能确定实时麦克风或录播'; button.append(make('span','',speakerName(speaker)),make('span','speaker-role-text',audioRoleNames[audioRole(speaker.role)])); button.addEventListener('click',() => openSpeakerDialog(speaker.id)); return button; });
      $('audio-speakers').replaceChildren(...(pills.length ? pills : [make('span','audio-speakers-empty','等待识别说话人')]));
      const options = [make('option','','全部声音'),make('option','','未知说话人')]; options[0].value = ''; options[1].value = '__unknown__';
      speakers.forEach(speaker => { const option = make('option','',speakerName(speaker)); option.value = String(speaker.id); options.push(option); });
      $('audio-filter').replaceChildren(...options);
      if (audioFilter && audioFilter !== '__unknown__' && !speakers.some(speaker => String(speaker.id) === audioFilter)) audioFilter = '';
      $('audio-filter').value = audioFilter;
    }
    if ($('speaker-dialog').open) refreshSpeakerMergeTargets();
    const filtered = segments.filter(segment => !audioFilter || (audioFilter === '__unknown__' ? !knownSpeaker(segment.speaker_id) : String(segment.speaker_id) === audioFilter)).slice(-180);
    const panel = $('audio-segments');
    const scroll = panel.scrollTop, atEnd = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 80;
    const wanted = new Set();
    if (!filtered.length) {
      audioRows.clear(); const empty = make('div','audio-empty');
      empty.append(make('span','','♫'),make('strong','',audioFilter ? '还没有这位说话人的字幕' : audioError ? '语音识别暂不可用' : status === 'no_audio_stream' ? '直播流未提供可识别的音轨' : ['waiting_model','disabled','unavailable'].includes(status) ? '等待语音模型就绪' : '听见直播里的声音'),make('p','',audioError ? shortened(audioError,160) : audioFilter ? '新的转写结果将持续显示。' : '转写完成后，字幕和说话人分组会显示在这里。'));
      panel.replaceChildren(empty); return;
    }
    panel.querySelector('.audio-empty')?.remove();
    filtered.forEach(segment => {
      const id = String(segment.id ?? `${segment.start}-${segment.end}`); wanted.add(id);
      const signature = JSON.stringify([segment, knownSpeaker(segment.speaker_id) ? speakerName(segment) : '', audioSaving.has(id)]);
      let row = audioRows.get(id);
      if (!row || row.dataset.signature !== signature) {
        const next = buildAudioSegment(segment,id); next.dataset.signature = signature;
        if (row) row.replaceWith(next); else panel.append(next);
        audioRows.set(id,next); row = next;
      }
      row.classList.toggle('playing',activeReplayId === id);
    });
    for (const [id,row] of audioRows) if (!wanted.has(id)) { row.remove(); audioRows.delete(id); }
    panel.scrollTop = atEnd ? panel.scrollHeight : scroll;
  }
  function buildAudioSegment(segment,id) {
    const partial = segment.is_final === false || segment.status === 'partial';
    const role = audioRole(segment.role);
    const row = make('article',`audio-segment${partial ? ' partial' : ''}`); row.dataset.segmentId = id;
    const meta = make('div','audio-segment-meta');
    const time = `${formatTime(segment.start ?? segment.t0)}—${formatTime(segment.end ?? segment.t1)}`;
    meta.append(make('time','',time),make('span','audio-speaker-label',knownSpeaker(segment.speaker_id) ? speakerName(segment) : '未知说话人'),make('span',`audio-role ${role}`,audioRoleNames[role]));
    if (partial) meta.append(make('span','audio-uncertain','临时转写'));
    const qualityNames = {too_short:'片段过短 · 声纹待定', merged_vad_regions:'多段语音合并 · 说话人待定', merged_regions:'多段语音合并 · 说话人待定', unstable_voice:'声纹不稳定 · 说话人待定'};
    if (qualityNames[segment.speaker_quality]) meta.append(make('span','audio-uncertain',qualityNames[segment.speaker_quality]));
    else if (segment.speaker_uncertain || (segment.speaker_uncertain === undefined && segment.uncertain)) meta.append(make('span','audio-uncertain',segment.speaker_uncertain ? '声纹匹配待确认' : '待确认'));
    if (segment.overlap === true || ['detected','overlap','possible','yes'].includes(segment.overlap)) meta.append(make('span','audio-uncertain','可能多人同声'));
    const basis = segment.role_basis === 'manual_segment' ? '本段标记' : segment.role_basis === 'manual_voice_reference' ? '同声纹参考' : '';
    if (basis) meta.append(make('span','audio-mark-basis',basis));
    row.append(meta,make('p','audio-segment-text',segment.text || (partial ? '正在转写…' : '此片段没有识别到文字')));
    const actions = make('div','audio-segment-actions');
    const play = make('button','audio-play-button','▷ 回听'); play.type = 'button'; play.disabled = !audioClipUrl(segment.audio_url); play.title = play.disabled ? '此片段暂无可回听音频' : '回听原始音轨'; play.addEventListener('click',() => playAudioSegment(segment,id)); actions.append(play);
    const source = make('label','audio-source-control','本段来源'); const select = make('select','audio-segment-role'); select.setAttribute('aria-label',`${time} 片段来源`); select.dataset.segmentId = id;
    Object.entries(audioRoleNames).forEach(([key,name]) => { const option = make('option','',key === 'unknown' ? '不确定 / 清除归属' : name); option.value = key; select.append(option); });
    select.value = role; select.disabled = partial || audioSaving.has(id); select.addEventListener('change',() => labelAudioSegment(segment,id,select.value)); source.append(select); actions.append(source); row.append(actions);
    if (segment.inference_seconds != null) row.title = `本段转写耗时 ${Number(segment.inference_seconds).toFixed(2)} 秒；音源归属以手动标记为参考`;
    return row;
  }
  function consumeAudioResult(result) {
    if (result?.session || result?.state) applyState(result);
    else if (isObject(result?.audio)) { state.audio = result.audio; renderAudio(); }
    else if (Array.isArray(result?.segments)) { state.audio = {...state.audio,...result}; renderAudio(); }
    else if (result?.id !== undefined && result?.text !== undefined) handleAudioEvent('audio_segment',result);
    else queueRefresh(80);
  }
  async function labelAudioSegment(segment,id,role) {
    if (audioSaving.has(id) || !state.session.id) return;
    const sid = state.session.id; audioSaving.add(id); renderAudio();
    try { const result = await api(`/api/audio/segments/${encodeURIComponent(id)}/label`,{method:'POST',body:JSON.stringify({session_id:sid,role})}); if (state.session.id !== sid) return; consumeAudioResult(result); await refreshState(); }
    catch (error) { notice(`片段来源标记失败：${error.message}`,true); }
    finally { audioSaving.delete(id); renderAudio(); }
  }
  function openSpeakerDialog(id) {
    if (!knownSpeaker(id)) return;
    const speaker = speakerList().find(item => String(item.id) === String(id)); if (!speaker) return;
    activeSpeakerId = String(id); activeSpeakerSession = state.session.id;
    $('speaker-dialog-name').textContent = speakerName(speaker); $('speaker-label').value = speaker.label || speakerName(speaker);
    $('speaker-role').value = audioRole(speaker.role); $('speaker-dialog-error').hidden = true; $('speaker-save').disabled = speakerActionBusy;
    $('speaker-merge-error').hidden = true; $('speaker-merge-target').value = ''; refreshSpeakerMergeTargets();
    if (!$('speaker-dialog').open) $('speaker-dialog').showModal();
  }
  function refreshSpeakerMergeTargets() {
    const select = $('speaker-merge-target'), previous = select.value;
    const targets = speakerList().filter(speaker => String(speaker.id) !== activeSpeakerId && knownSpeaker(speaker.id));
    const signature = JSON.stringify([activeSpeakerId,...targets.map(({id,label,role}) => ({id,label,role}))]);
    if (signature !== speakerMergeOptionsSignature) {
      speakerMergeOptionsSignature = signature;
      const placeholder = make('option','',targets.length ? '选择要保留的说话人' : '尚无其他说话人'); placeholder.value = '';
      select.replaceChildren(placeholder);
      targets.forEach(speaker => { const option = make('option','',`${speakerName(speaker)} · ${audioRoleNames[audioRole(speaker.role)]}`); option.value = String(speaker.id); select.append(option); });
      select.value = targets.some(speaker => String(speaker.id) === previous) ? previous : '';
    }
    select.disabled = !targets.length || speakerActionBusy;
    renderSpeakerMergePreview();
  }
  function renderSpeakerMergePreview() {
    const source = speakerList().find(item => String(item.id) === activeSpeakerId);
    const target = speakerList().find(item => String(item.id) === $('speaker-merge-target').value);
    const button = $('speaker-merge-button'), preview = $('speaker-merge-preview');
    button.disabled = speakerActionBusy || !source || !target || activeSpeakerSession !== state.session.id;
    button.textContent = target ? `合并进「${speakerName(target)}」` : '合并当前说话人'; preview.classList.remove('conflict');
    if (!source || !target) { preview.textContent = '仅影响当前会话中的声纹分组与声音记忆，已完成的回答保持原样。'; return; }
    const sourceRole = audioRole(source.role), targetRole = audioRole(target.role);
    if (sourceRole !== 'unknown' && targetRole !== 'unknown' && sourceRole !== targetRole) { preview.textContent = `两组已保存的来源参考不同（${audioRoleNames[sourceRole]} / ${audioRoleNames[targetRole]}）。请先核对并修改来源参考，再合并。`; preview.classList.add('conflict'); button.disabled = true; return; }
    const entries = list(state.audio.segments).filter(segment => String(segment.speaker_id) === activeSpeakerId);
    const manual = entries.filter(segment => segment.role_basis === 'manual_segment').length;
    const resultingRole = targetRole === 'unknown' ? sourceRole : targetRole;
    preview.textContent = `将把「${speakerName(source)}」的 ${entries.length} 条可用片段归入「${speakerName(target)}」，保留目标名称及“${audioRoleNames[resultingRole]}”来源参考${manual ? `；${manual} 条单独标注的片段保留各自来源` : '；单个片段的手动标注保持不变'}。当前组会从列表移除，后续声纹匹配使用合并后的组。按已保存的设置执行，已完成的回答不改。`;
  }
  async function saveSpeakerRole(event) {
    event.preventDefault(); if (speakerActionBusy || !knownSpeaker(activeSpeakerId) || activeSpeakerSession !== state.session.id) return;
    const id = activeSpeakerId, sid = activeSpeakerSession, label = $('speaker-label').value.trim();
    if (!label) { $('speaker-dialog-error').textContent = '请输入显示名称。'; $('speaker-dialog-error').hidden = false; return; }
    speakerActionBusy = true; $('speaker-save').disabled = true; $('speaker-dialog-error').hidden = true; renderSpeakerMergePreview();
    try { const result = await api(`/api/audio/speakers/${encodeURIComponent(id)}/label`,{method:'POST',body:JSON.stringify({session_id:sid,role:$('speaker-role').value,label})}); if (sid !== state.session.id) return; consumeAudioResult(result); await refreshState(); if (activeSpeakerId === id && activeSpeakerSession === sid) $('speaker-dialog').close(); }
    catch (error) { $('speaker-dialog-error').textContent = error.message; $('speaker-dialog-error').hidden = false; }
    finally { speakerActionBusy = false; $('speaker-save').disabled = false; refreshSpeakerMergeTargets(); }
  }
  async function mergeSpeaker() {
    if (speakerActionBusy || $('speaker-merge-button').disabled || activeSpeakerSession !== state.session.id) return;
    const id = activeSpeakerId, sid = activeSpeakerSession, targetId = $('speaker-merge-target').value;
    if (!targetId || targetId === id) return;
    speakerActionBusy = true; $('speaker-save').disabled = true; $('speaker-merge-error').hidden = true; renderSpeakerMergePreview();
    try { const result = await api(`/api/audio/speakers/${encodeURIComponent(id)}/merge`,{method:'POST',body:JSON.stringify({session_id:sid,target_id:targetId})}); if (sid !== state.session.id) return; consumeAudioResult(result); await refreshState(); if (activeSpeakerId === id && activeSpeakerSession === sid) $('speaker-dialog').close(); notice('说话人已合并，声音记忆与后续问答已更新。'); }
    catch (error) { $('speaker-merge-error').textContent = error.message; $('speaker-merge-error').hidden = false; }
    finally { speakerActionBusy = false; $('speaker-save').disabled = false; refreshSpeakerMergeTargets(); }
  }
  function stopAudioReplay() {
    const audio = $('audio-replay-player'); audio.pause(); audio.removeAttribute('src'); audio.load(); activeReplayId = null; $('audio-replay').hidden = true; $('audio-replay-error').hidden = true; restoreLiveSound();
    for (const row of audioRows.values()) row.classList.remove('playing');
  }
  function restoreLiveSound() { if (restoreVideoUnmuted) { $('live-video').muted = false; restoreVideoUnmuted = false; } }
  function playAudioSegment(segment,id) {
    const url = audioClipUrl(segment.audio_url); if (!url) return;
    const audio = $('audio-replay-player'); activeReplayId = id; $('audio-replay').hidden = false; $('audio-replay-error').hidden = true;
    $('audio-replay-title').textContent = `回听 ${formatTime(segment.start ?? segment.t0)}—${formatTime(segment.end ?? segment.t1)} · ${knownSpeaker(segment.speaker_id) ? speakerName(segment) : '未知说话人'}`;
    audio.src = url; audio.play().catch(error => { $('audio-replay-error').textContent = error.name === 'NotAllowedError' ? '请点击音频播放器中的播放按钮。' : '该片段暂时无法回听，可能已经过期。'; $('audio-replay-error').hidden = false; });
    for (const [rowId,row] of audioRows) row.classList.toggle('playing',rowId === id);
  }
  function handleAudioEvent(type,data) {
    if (type === 'audio_segment') {
      const segment = data.segment || data; if (segment.id === undefined) return;
      const entries = list(state.audio.segments); const index = entries.findIndex(item => String(item.id) === String(segment.id));
      if (index >= 0) { if (Number(segment.revision || 0) < Number(entries[index].revision || 0)) return; entries[index] = {...entries[index],...segment}; } else entries.push(segment);
      state.audio.segments = entries.sort((a,b) => Number(a.start ?? a.t0 ?? 0) - Number(b.start ?? b.t0 ?? 0)).slice(-200);
    } else if (type === 'audio_status') state.audio = {...state.audio,...(data.audio || data)};
    else if (type === 'audio_speakers') state.audio.speakers = data.speakers || data;
    renderAudio(); queueRefresh(1200);
  }

  function renderDanmaku() {
    const entries = list(state.danmaku).slice(-150);
    $('danmaku-count').textContent = String(danmakuTotal);
    if (!entries.length) { if (!$('danmaku-empty')) { const empty = make('div','inline-empty'); empty.id = 'danmaku-empty'; empty.append(make('span','empty-lines','≋'), make('p','','来自直播间的弹幕会显示在这里')); $('danmaku-list').replaceChildren(empty); } return; }
    const panel = $('danmaku-list'), previousScrollTop = panel.scrollTop, nearBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 80;
    const nodes = entries.map(entry => { const row = make('div','danmaku-row'); row.append(make('time','danmaku-time',formatTime(entry.timestamp ?? entry.time ?? entry.t)), make('span','danmaku-user',entry.user_name ?? entry.user ?? entry.username ?? entry.uname ?? entry.name ?? '观众'), make('span','danmaku-text',entry.text ?? entry.content ?? entry.message ?? '')); return row; });
    panel.replaceChildren(...nodes); panel.scrollTop = nearBottom ? panel.scrollHeight : previousScrollTop;
  }
  function danmakuFeedback(message,error=false) { const node = $('danmaku-send-feedback'); node.textContent = message; node.hidden = !message; node.classList.toggle('error',error); }
  function renderDanmakuComposer() {
    const sending = state.danmaku_send || {}, candidates = state.danmaku_candidates || {};
    const maxLength = Math.max(1,Math.min(1000,Number(sending.max_length) || 20));
    $('danmaku-send-input').maxLength = maxLength;
    $('danmaku-send-status').textContent = danmakuSending ? '正在发送…' : sending.error ? '发送受限 · 请查看提示' : sending.can_send ? '本人登录已配置' : sending.configured ? '登录已配置 · 等待直播' : '配置本人登录后可发送';
    $('danmaku-send-status').title = plain(sending.error || sending.message);
    $('danmaku-send-button').disabled = danmakuSending || !sessionActive() || !sending.can_send || !$('danmaku-send-input').value.trim();
    $('danmaku-send-button').textContent = danmakuSending ? '发送中…' : '发送弹幕';
    $('danmaku-copy-recent').disabled = !sessionActive();
    $('danmaku-login-toggle').textContent = sending.configured ? '更新本人登录' : '配置本人登录';
    const items = list(candidates.items).filter(item => plain(item.text).trim()).slice(0,3), signature = JSON.stringify(items);
    if (signature !== danmakuCandidatesSignature) {
      danmakuCandidatesSignature = signature;
      const nodes = items.map(item => { const button = make('button','danmaku-candidate',item.text); button.type = 'button'; button.dataset.candidateId = String(item.id ?? ''); button.title = '只填入输入框，点击发送弹幕后才会发送'; button.addEventListener('click',() => { $('danmaku-send-input').value = plain(item.text); renderDanmakuComposer(); $('danmaku-send-input').focus(); }); return button; });
      $('danmaku-candidates').replaceChildren(...(nodes.length ? nodes : [make('span','danmaku-candidate-empty','理解直播内容后，这里会更新 3 条候选。')]));
    }
    const updated = items.length && candidates.generated_at ? `${formatTime(candidates.generated_at)} 更新` : '';
    $('danmaku-candidates-time').textContent = [candidates.status === 'deferred' ? '优先跟上直播，候选稍后更新' : candidates.status === 'generating' ? '正在更新' : candidates.status === 'error' ? '更新暂缓' : '',updated].filter(Boolean).join(' · ');
    $('danmaku-candidates-time').title = plain(candidates.error);
    renderDanmakuAuto();
  }
  function renderDanmakuAuto() {
    const panel = $('danmaku-auto'); if (!panel) return;
    const auto = state.danmaku_auto || {}, active = auto.status === 'active';
    const status = auto.status || 'idle', sent = Number(auto.sent_count) || 0, limit = Number(auto.max_messages) || 6;
    panel.dataset.status = status;
    $('danmaku-auto-status').textContent = ({active:`运行中 · 已发送 ${sent}/${limit} 条`,stopped:`已停止 · 已发送 ${sent}/${limit} 条`,completed:`已结束 · 已发送 ${sent}/${limit} 条`,error:`发送已停止 · 已发送 ${sent}/${limit} 条`,idle:'未启动'})[status] || '未启动';
    const detail = active ? [`每 ${Math.round((Number(auto.interval_seconds) || 300) / 60)} 分钟一条`,auto.next_send_at ? `下次计划 ${formatTime(auto.next_send_at)}` : '等待新候选',auto.ends_at ? `最迟 ${formatTime(auto.ends_at)} 结束` : '',Number(auto.skipped_count) ? `已跳过 ${Number(auto.skipped_count)} 次` : '',plain(auto.reason)] : [plain(auto.error || auto.reason) || '首条新候选就绪后发送，此后每 5 分钟一条；最多 30 分钟、6 条。',Number(auto.skipped_count) ? `已跳过 ${Number(auto.skipped_count)} 次` : ''];
    $('danmaku-auto-detail').textContent = detail.filter(Boolean).join(' · ');
    $('danmaku-auto-last').textContent = auto.last_text ? `最近发送${auto.last_sent_at ? ` ${formatTime(auto.last_sent_at)}` : ''}：${plain(auto.last_text)}` : '';
    $('danmaku-auto-last').hidden = !auto.last_text;
    $('danmaku-auto-start').hidden = active;
    $('danmaku-auto-start').disabled = danmakuAutoBusy || state.session.status !== 'live' || !state.danmaku_send.can_send;
    $('danmaku-auto-start').title = state.session.status !== 'live' ? '直播中才可启动自动弹幕' : !state.danmaku_send.can_send ? '请先配置本人登录' : '向当前直播间自动发送，最多 6 条';
    $('danmaku-auto-start').textContent = danmakuAutoBusy === 'start' ? '正在启动…' : '启动自动发送 · 30 分钟';
    $('danmaku-auto-stop').hidden = !active;
    $('danmaku-auto-stop').disabled = danmakuAutoBusy;
    $('danmaku-auto-stop').textContent = danmakuAutoBusy === 'stop' ? '正在停止…' : '立即停止';
  }
  async function setDanmakuAuto(start) {
    if (danmakuAutoBusy || !state.session.id) return;
    if (start && (state.session.status !== 'live' || !state.danmaku_send.can_send || state.danmaku_auto.status === 'active')) return;
    const sid = state.session.id, feedback = $('danmaku-auto-feedback');
    danmakuAutoBusy = start ? 'start' : 'stop'; feedback.hidden = true; renderDanmakuAuto();
    try {
      const body = start ? {session_id:sid,interval_seconds:300,duration_seconds:1800,max_messages:6} : {session_id:sid};
      const result = await api(`/api/danmaku/auto/${start ? 'start' : 'stop'}`,{method:'POST',body:JSON.stringify(body)});
      if (state.session.id !== sid) return;
      const auto = result.danmaku_auto || result;
      if (isObject(auto) && auto.status) state.danmaku_auto = auto;
      await refreshState();
    } catch (error) {
      if (state.session.id === sid) { feedback.textContent = `${start ? '启动' : '停止'}失败：${error.message}`; feedback.classList.add('error'); feedback.hidden = false; }
    } finally { danmakuAutoBusy = ''; renderDanmakuAuto(); }
  }
  async function sendDanmaku(event) {
    event.preventDefault(); const input = $('danmaku-send-input'), text = input.value.trim();
    if (!text || danmakuSending || !sessionActive() || !state.danmaku_send.can_send) return;
    const sid = state.session.id; danmakuSending = true; danmakuFeedback(''); renderDanmakuComposer();
    try {
      const result = await api('/api/danmaku/send',{method:'POST',body:JSON.stringify({session_id:sid,text})});
      if (state.session.id !== sid) return;
      if (result.sent === false || result.ok === false) throw new Error(plain(result.message || result.error) || '弹幕未发送，请稍后重试。');
      if (input.value.trim() === text) input.value = '';
      danmakuFeedback(plain(result.message) || '弹幕已发送。'); queueRefresh(100);
    } catch (error) { if (state.session.id === sid) danmakuFeedback(`发送失败：${error.message}`,true); }
    finally { danmakuSending = false; renderDanmakuComposer(); }
  }
  async function saveDanmakuCredentials(event) {
    event.preventDefault(); if (danmakuCredentialsSaving) return;
    const cookie = $('danmaku-cookie').value.trim(); if (!cookie) { danmakuFeedback('请填入本人的登录 Cookie。',true); return; }
    $('danmaku-cookie').value = ''; danmakuCredentialsSaving = true; $('danmaku-login-save').disabled = true; danmakuFeedback('');
    try { const result = await api('/api/danmaku/credentials',{method:'POST',body:JSON.stringify({cookie})}); if (isObject(result.danmaku_send)) state.danmaku_send = result.danmaku_send; else if ('configured' in result) state.danmaku_send = {...state.danmaku_send,...result}; $('danmaku-login-form').hidden = true; $('danmaku-login-toggle').setAttribute('aria-expanded','false'); danmakuFeedback(plain(result.message) || '登录配置已保存，正在更新发送状态。'); await refreshState(); }
    catch (error) { danmakuFeedback(`登录配置失败：${error.message}`,true); }
    finally { danmakuCredentialsSaving = false; $('danmaku-login-save').disabled = false; renderDanmakuComposer(); }
  }
  async function copyRecentDanmaku() {
    const now = performance.now();
    recentDanmaku = recentDanmaku.filter(item => now - item.received <= 2000);
    const entries = recentDanmaku;
    $('danmaku-copy-fallback').hidden = true;
    if (!entries.length) { danmakuFeedback('最近 2 秒没有收到可复制的弹幕。'); return; }
    const text = entries[Math.floor(Math.random() * entries.length)].text;
    try { if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable'); await navigator.clipboard.writeText(text); danmakuFeedback(`已复制：${shortened(text,100)}`); }
    catch { $('danmaku-copy-text').value = text; $('danmaku-copy-fallback').hidden = false; $('danmaku-copy-text').focus(); $('danmaku-copy-text').select(); danmakuFeedback('未能自动复制，文本已选中，请手动复制。'); }
  }
  function flyDanmaku(entry) {
    if (!overlayEnabled || playerMode !== 'native' || $('live-video').hidden || document.hidden) return;
    const overlay = $('danmaku-overlay'); if (overlay.childElementCount > 35) return;
    const node = make('span','flying-danmaku',entry.text ?? entry.content ?? entry.message ?? '');
    if (!node.textContent) return; node.style.top = `${7 + (danmakuLane++ % 7) * 9}%`; node.addEventListener('animationend',() => node.remove(),{once:true}); overlay.append(node); setTimeout(() => node.remove(),12000);
  }
  function safePlayerUrl(value) {
    if (!value) return '';
    try { const url = new URL(value, location.href); if (!['http:','https:'].includes(url.protocol)) return ''; return url.href; } catch { return ''; }
  }
  function stopPlayer() {
    if (hls) { hls.destroy(); hls = null; }
    const video = $('live-video'); video.pause(); video.removeAttribute('src'); video.load(); video.hidden = true;
    $('official-player').removeAttribute('src'); $('official-player').hidden = true; playbackKey = '';
    $('player-placeholder').hidden = false; $('playback-error').hidden = true;
  }
  function playbackError(message) { $('playback-error-text').textContent = message; $('playback-error').hidden = false; }
  function setupPlayer() {
    const playback = state.playback || {};
    const status = state.session.status || 'idle';
    const roomId = state.room.room_id || state.room.id;
    const liveRoom = Number(state.room.live_status) === 1;
    const playable = playerMode === 'official' ? liveRoom && sessionActive() : (status === 'live' || status === 'reconnecting') && playback.ready === true;
    const nativeUrl = safePlayerUrl(playback.url || playback.play_url || playback.stream_url);
    const embedUrl = safePlayerUrl(playback.embed_url) || (roomId && /^\d+$/.test(String(roomId)) ? `https://www.bilibili.com/blackboard/live/live-activity-player.html?cid=${roomId}&quality=4&autoplay=1` : '');
    const url = playerMode === 'official' ? embedUrl : nativeUrl;
    const key = playable && url ? `${state.session.id}:${playerMode}:${url}` : '';
    $('native-mode').classList.toggle('active',playerMode === 'native'); $('native-mode').setAttribute('aria-pressed',String(playerMode === 'native'));
    $('official-mode').classList.toggle('active',playerMode === 'official'); $('official-mode').setAttribute('aria-pressed',String(playerMode === 'official'));
    $('overlay-toggle').disabled = playerMode === 'official';
    $('danmaku-overlay').hidden = !overlayEnabled || playerMode === 'official';
    $('playback-info').textContent = playable ? playerMode === 'official' ? '官方播放器 · 弹幕由官方播放器显示' : '直播画面 · 默认静音，可在播放器内开启声音' : status === 'waiting' ? '等待开播 · 自动尝试恢复连接' : '连接后自动获取直播画面与弹幕';
    if (!key) { if (playbackKey) stopPlayer(); if (liveRoom && sessionActive() && playerMode === 'native' && state.session.error) playbackError(plain(state.session.error)); else if (playable && !nativeUrl && playerMode === 'native') playbackError('暂未获取可播放的直播流，可尝试官方播放器。'); return; }
    if (key === playbackKey) return;
    stopPlayer(); playbackKey = key;
    $('player-placeholder').hidden = true;
    if (playerMode === 'official') { $('official-player').src = url; $('official-player').hidden = false; return; }
    const video = $('live-video'); video.hidden = false;
    if (window.Hls?.isSupported()) {
      hls = new window.Hls({enableWorker:true, lowLatencyMode:true, backBufferLength:30, maxBufferLength:20, liveSyncDurationCount:3});
      hls.loadSource(url); hls.attachMedia(video);
      hls.on(window.Hls.Events.MANIFEST_PARSED,() => { video.play().catch(() => playbackError('浏览器暂停了自动播放，请点击播放器内的播放按钮。')); });
      hls.on(window.Hls.Events.ERROR,(_event,data) => { if (data.fatal) playbackError('直播流暂时无法播放。可切换官方播放器，或等待自动重连。'); });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) { video.src = url; video.play().catch(() => playbackError('浏览器暂停了自动播放，请点击播放器内的播放按钮。')); }
    else { playbackError('当前浏览器无法直接播放此直播流，请切换官方播放器。'); }
  }
  function answerMessage(qid) {
    let message = state.messages.find(m => m.role === 'assistant' && m.question_id === qid);
    if (!message) { message = {id:`answer-${qid}`,question_id:qid,role:'assistant',text:'',status:'running'}; state.messages.push(message); }
    return message;
  }
  function handleEvent(raw, fallbackType) {
    let envelope; try { envelope = JSON.parse(raw); } catch { return; }
    const type = envelope.type || fallbackType || 'state';
    const data = envelope.data ?? envelope;
    if (envelope.id !== undefined) { const key = String(envelope.id); if (seenEvents.has(key)) return; seenEvents.add(key); lastEventId = key; if (seenEvents.size > 1500) seenEvents.delete(seenEvents.values().next().value); }
    if (envelope.session_id && state.session.id && envelope.session_id !== state.session.id && !['state','session'].includes(type)) return;
    if (['audio_segment','audio_status','audio_speakers'].includes(type)) { handleAudioEvent(type,data); return; }
    if (type === 'danmaku_candidates') { state.danmaku_candidates = data.danmaku_candidates || data; renderDanmakuComposer(); return; }
    if (type === 'danmaku_send_status') { state.danmaku_send = {...state.danmaku_send,...(data.danmaku_send || data)}; renderDanmakuComposer(); return; }
    if (type === 'danmaku_auto') { const auto = data.danmaku_auto || data; if (!auto.session_id || auto.session_id === state.session.id) { state.danmaku_auto = auto; renderDanmakuAuto(); } return; }
    if (type === 'state') { applyState(data); return; }
    if (type === 'session') { queueRefresh(50); return; }
    if (type === 'observation') {
      const entry = data.observation || data;
      const existing = entry.id && state.observations.find(value => value.id === entry.id);
      if (!existing) sampleUnderstandingObservation(entry,envelope.time);
      if (existing) Object.assign(existing,entry); else state.observations.push(entry);
      const retention = Math.max(1,Math.floor(Number(state.memory?.retention?.max_observations) || 400));
      state.observations = state.observations.slice(-retention); renderObservations(); renderUnderstandingTiming();
    } else if (type === 'memory') { state.memory = data.memory || data; renderMemory(); }
    else if (type === 'observer') { const observer = data.observer || data; state.observer = {...state.observer,...observer}; sampleUnderstandingState(observer); renderObserver(); renderModel(); }
    else if (type === 'danmaku') { const entry = data.danmaku || data; state.danmaku.push(entry); state.danmaku = state.danmaku.slice(-150); const now = performance.now(), text = plain(entry.text ?? entry.content ?? entry.message).trim(); recentDanmaku = recentDanmaku.filter(item => now - item.received <= 2000); if (text) recentDanmaku.push({text,received:now}); recentDanmaku = recentDanmaku.slice(-200); danmakuTotal++; renderDanmaku(); flyDanmaku(entry); return; }
    else if (type === 'playback') { state.playback = {...state.playback,...(data.playback || data)}; setupPlayer(); }
    else if (type === 'plan') { if (data.question_id) { questionPlans.set(data.question_id,data); answerMessage(data.question_id).status = 'answering'; renderMessages(); } }
    else if (type === 'question_started') { const qid = data.question_id || data.id; if (qid) { pendingQuestions.set(qid,true); answerMessage(qid).status = 'running'; renderMessages(); } }
    else if (type === 'answer_delta') {
      const qid = data.question_id || data.id;
      if (qid) { const message = answerMessage(qid); const delta = data.delta ?? data.text ?? data.content ?? ''; message.text = plain(message.text) + plain(delta); message.status = 'streaming'; localDrafts.set(qid,{text:message.text,status:'streaming'}); renderMessages(); }
      return;
    } else if (type === 'answer_done') {
      const qid = data.question_id || data.id;
      if (qid) { const message = answerMessage(qid); message.text = data.text ?? data.answer ?? data.content ?? message.text; message.status = data.status || 'done'; if (data.evidence) message.evidence = data.evidence; if (data.audio_evidence) message.audio_evidence = data.audio_evidence; pendingQuestions.delete(qid); localDrafts.delete(qid); renderMessages(); }
    } else if (type === 'error') { notice(plain(data.message || data.error || data.detail || data),true); }
    queueRefresh();
  }
  function connectEvents() {
    if (eventSource) eventSource.close();
    eventSource = new EventSource('/api/events');
    eventSource.onopen = () => { recentDanmaku = []; $('events-status').textContent = '工作台已连接'; setDot($('events-dot'),'good'); refreshState(); };
    eventSource.onerror = (event) => { if (event.data) return; $('events-status').textContent = '实时通道重连中'; setDot($('events-dot'),'warn'); };
    eventSource.onmessage = (event) => handleEvent(event.data);
    ['state','session','observation','memory','observer','danmaku','danmaku_candidates','danmaku_send_status','danmaku_auto','question_started','plan','answer_delta','answer_done','error','playback','audio_segment','audio_status','audio_speakers'].forEach(type => eventSource.addEventListener(type,event => { if (event.data) handleEvent(event.data,type); }));
  }
  async function connectRoom(event) {
    event.preventDefault(); const room = $('room-input').value.trim(); if (!room || sourceBusy) return;
    sourceBusy = true; renderSession(); notice('');
    try { const result = await api('/api/session',{method:'POST',body:JSON.stringify({room})}); if ($('room-input').value.trim() === room) roomInputDirty = false; applyState(result); if (!result.session && !result.state) await refreshState(); }
    catch (error) { notice(error.message,true); }
    finally { sourceBusy = false; renderSession(); }
  }
  async function stopSession() {
    if (!sessionActive() || sourceBusy) return;
    sourceBusy = true; renderSession();
    try { const result = await api('/api/session/stop',{method:'POST',body:JSON.stringify({session_id:state.session.id})}); if (result.session || result.state) applyState(result); else await refreshState(); }
    catch (error) { notice(error.message,true); }
    finally { sourceBusy = false; renderSession(); }
  }
  async function askQuestion(event) {
    event.preventDefault(); const text = $('question-input').value.trim(); if (!text || !sessionActive() || submittingQuestion) return;
    submittingQuestion = true;
    const sessionId = state.session.id; $('send-button').disabled = true;
    try {
      const result = await api('/api/questions',{method:'POST',body:JSON.stringify({session_id:sessionId,text})});
      if (state.session.id !== sessionId) return;
      $('question-input').value = ''; $('question-input').style.height = '';
      const qid = result.question_id || result.id;
      if (qid) { pendingQuestions.set(qid,true); if (!state.messages.some(m => m.question_id === qid && m.role === 'user')) state.messages.push({id:`user-${qid}`,question_id:qid,role:'user',text}); const message = answerMessage(qid); if (!message.text) message.status = 'queued'; }
      renderMessages(); $('messages').scrollTop = $('messages').scrollHeight; queueRefresh(100);
    } catch (error) { notice(error.message,true); }
    finally { submittingQuestion = false; renderSession(); $('question-input').focus(); }
  }
  async function cancelQuestion(id, button) {
    button.disabled = true;
    try { await api(`/api/questions/${encodeURIComponent(id)}/cancel`,{method:'POST',body:JSON.stringify({session_id:state.session.id})}); pendingQuestions.delete(id); localDrafts.delete(id); const message = state.messages.find(m => m.question_id === id && m.role === 'assistant'); if (message) message.status = 'cancelled'; renderMessages(); queueRefresh(100); }
    catch (error) { button.disabled = false; notice(error.message,true); }
  }
  async function checkModel() {
    if (modelChecking) return;
    modelChecking = true; if (!$('model-dialog').open) $('model-dialog').showModal();
    $('model-dialog-text').textContent = '正在请求模型服务，请稍候…'; $('model-dialog-detail').hidden = true; $('model-retry').disabled = true;
    try {
      const result = await api('/api/model/check',{method:'POST',body:'{}'});
      const ready = result.ok ?? result.ready ?? result.available ?? result.model?.ready;
      $('model-dialog-text').textContent = ready === false ? '模型服务尚未就绪。' : '模型服务检查已完成。';
      if (result.message || result.error || result.detail) $('model-dialog-text').textContent = plain(result.message || result.error || result.detail);
      $('model-dialog-detail').textContent = JSON.stringify(result,null,2); $('model-dialog-detail').hidden = false;
      if (ready !== undefined) { state.observer.model_ready = Boolean(ready); renderModel(); }
      queueRefresh();
    } catch (error) { $('model-dialog-text').textContent = `模型检查失败：${error.message}`; }
    finally { modelChecking = false; $('model-retry').disabled = false; }
  }
  function switchTab(tab) {
    activeTab = tab; for (const name of ['activity','memory']) { const active = name === tab; $(`${name}-tab`).classList.toggle('active',active); $(`${name}-tab`).setAttribute('aria-selected',String(active)); $(`${name}-tab`).tabIndex = active ? 0 : -1; $(`${name}-panel`).hidden = !active; }
  }
  $('audio-filter').addEventListener('change',() => { audioFilter = $('audio-filter').value; renderAudio(); });
  $('speaker-label-form').addEventListener('submit',saveSpeakerRole);
  $('speaker-merge-target').addEventListener('change',renderSpeakerMergePreview);
  $('speaker-merge-button').addEventListener('click',mergeSpeaker);
  $('speaker-dialog-close').addEventListener('click',() => $('speaker-dialog').close());
  $('audio-replay-close').addEventListener('click',stopAudioReplay);
  $('audio-replay-player').addEventListener('play',() => { const video = $('live-video'); if (!video.muted) { restoreVideoUnmuted = true; video.muted = true; } });
  $('audio-replay-player').addEventListener('pause',restoreLiveSound);
  $('audio-replay-player').addEventListener('ended',restoreLiveSound);
  $('audio-replay-player').addEventListener('error',() => { if (activeReplayId) { $('audio-replay-error').textContent = '该片段暂时无法回听，可能已经过期。'; $('audio-replay-error').hidden = false; restoreLiveSound(); } });
  $('room-input').addEventListener('input',() => { roomInputDirty = true; });
  $('danmaku-send-form').addEventListener('submit',sendDanmaku);
  $('danmaku-send-input').addEventListener('input',renderDanmakuComposer);
  $('danmaku-copy-recent').addEventListener('click',copyRecentDanmaku);
  $('danmaku-auto-start')?.addEventListener('click',() => setDanmakuAuto(true));
  $('danmaku-auto-stop')?.addEventListener('click',() => setDanmakuAuto(false));
  $('danmaku-login-form').addEventListener('submit',saveDanmakuCredentials);
  $('danmaku-login-toggle').addEventListener('click',() => { const form = $('danmaku-login-form'); form.hidden = !form.hidden; $('danmaku-login-toggle').setAttribute('aria-expanded',String(!form.hidden)); if (!form.hidden) $('danmaku-cookie').focus(); });
  $('room-form').addEventListener('submit',connectRoom);
  $('stop-button').addEventListener('click',stopSession);
  $('question-form').addEventListener('submit',askQuestion);
  $('question-input').addEventListener('input',() => { $('send-button').disabled = !sessionActive() || !$('question-input').value.trim(); $('question-input').style.height = 'auto'; $('question-input').style.height = `${Math.min($('question-input').scrollHeight,140)}px`; });
  $('question-input').addEventListener('keydown',event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); askQuestion(event); } });
  document.querySelectorAll('[data-question]').forEach(button => button.addEventListener('click',() => { if (!sessionActive()) { notice('请先连接直播间，再开始提问。'); $('room-input').focus(); return; } $('question-input').value = button.dataset.question; $('question-input').dispatchEvent(new Event('input')); $('question-input').focus(); }));
  $('overlay-toggle').addEventListener('click',() => { overlayEnabled = !overlayEnabled; $('overlay-toggle').classList.toggle('active',overlayEnabled); $('overlay-toggle').setAttribute('aria-pressed',String(overlayEnabled)); $('danmaku-overlay').hidden = !overlayEnabled || playerMode === 'official'; if (!overlayEnabled) $('danmaku-overlay').replaceChildren(); });
  const mode = (value) => { playerMode = value; $('danmaku-overlay').replaceChildren(); setupPlayer(); };
  $('native-mode').addEventListener('click',() => mode('native')); $('official-mode').addEventListener('click',() => mode('official')); $('fallback-button').addEventListener('click',() => mode('official'));
  $('activity-tab').addEventListener('click',() => switchTab('activity')); $('memory-tab').addEventListener('click',() => switchTab('memory'));
  document.querySelector('.understanding-tabs').addEventListener('keydown',event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); switchTab(activeTab === 'activity' ? 'memory' : 'activity'); $(`${activeTab}-tab`).focus(); } });
  $('notice-close').addEventListener('click',() => notice(''));
  $('model-check').addEventListener('click',checkModel); $('model-retry').addEventListener('click',checkModel); $('model-dialog-close').addEventListener('click',() => $('model-dialog').close());
  $('live-video').addEventListener('playing',() => { $('playback-error').hidden = true; });
  $('live-video').addEventListener('error',() => { if (playbackKey && playerMode === 'native') { const code = $('live-video').error?.code; playbackError(({2:'直播流读取中断，可等待重连或切换官方播放器。',3:'浏览器解码直播画面失败，可尝试官方播放器。',4:'浏览器暂时无法读取此视频格式，可尝试官方播放器。'})[code] || '播放器暂时无法播放，可以尝试官方播放器。'); } });
  document.addEventListener('visibilitychange',() => { if (!document.hidden) refreshState(); });
  window.addEventListener('pageshow',event => { if (event.persisted) connectEvents(); });
  window.addEventListener('pagehide',() => { if (eventSource) eventSource.close(); if (hls) hls.destroy(); });
  switchTab('activity'); renderAll(); refreshState(); connectEvents();
  setInterval(() => { if (!document.hidden && !refreshTimer) refreshState(); },15000);
  setInterval(() => { if (!document.hidden) renderUnderstandingTiming(); },1000);
})();
