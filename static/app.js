'use strict';
(() => {
  const $ = (id) => document.getElementById(id);
  const state = {session: {}, room: {}, playback: {}, observer: {}, observations: [], memory: {}, messages: [], danmaku: [], sent_danmaku: [], danmaku_status: {}, danmaku_send: {}, danmaku_candidates: {}, danmaku_chorus: {}, danmaku_auto: {}, danmaku_style: {value:30,revision:0,mode:'builtin'}, danmaku_corpus: {}, model: {}, audio: {segments: [], speakers: []}};
  const statusNames = {idle: '尚未连接', connecting: '连接中', waiting: '等待开播', live: '直播中', stopping: '正在断开', stopping_failed: '断开异常', stopped: '已断开', error: '连接异常', reconnecting: '重新连接中'};
  const observerNames = {waiting_frames: '等待下一帧', waiting_model: '未配置模型', retrying: '重试观察中', idle: '等待直播', waiting: '等待画面', running: '持续观察中', observing: '正在观察', thinking: '正在观察', active: '持续观察中', ready: '已就绪', connecting: '连接模型中', stopped: '观察已停止', error: '观察异常', unavailable: '模型不可用', disabled: '未配置模型', unconfigured: '未配置模型'};
  let roomInputDirty = false;
  let hls = null, playbackKey = '', playerMode = 'native', overlayEnabled = true, sourceBusy = false, playbackWatchdog = null;
  let eventSource = null, refreshTimer = null, requestVersion = 0, lastEventId = null, modelChecking = false;
  let refreshInFlight = null, refreshQueuedOptions = null;
  let connectionStyleResetPending = false;
  let danmakuTotal = 0, danmakuLane = 0, activeTab = 'activity', submittingQuestion = false;
  let speakerActionBusy = false, speakerMergeOptionsSignature = '';
  let danmakuSending = false, danmakuCredentialsSaving = false, danmakuCandidatesSignature = '';
  let danmakuTransportBusy = false, danmakuTransportRequested = null, danmakuLocalAction = '';
  let danmakuPairing = null;
  let sentDanmakuSignature = '', danmakuChorusSignature = '';
  let danmakuAutoBusy = '';
  let danmakuStyleEditing = false, danmakuStylePending = false, danmakuStyleRequestVersion = 0;
  let danmakuStyleSaveChain = Promise.resolve();
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
    const method = (options.method || 'GET').toUpperCase(), controller = new AbortController();
    const {timeoutMs = path === '/api/model/check' ? 100000 : path === '/api/danmaku/send' ? 65000 : method === 'GET' ? 15000 : 35000, ...requestOptions} = options;
    const timer = setTimeout(() => controller.abort(),timeoutMs);
    try {
      const response = await fetch(path, {...requestOptions, signal:controller.signal, headers: {'Content-Type':'application/json', ...(options.headers || {})}});
      let body; try { body = await response.json(); } catch (error) { if (controller.signal.aborted) throw error; body = {}; }
      if (!response.ok) {
        const error = new Error(plain(body.detail ?? body.error ?? body.message) || `请求失败（${response.status}）`);
        if (path === '/api/danmaku/send' && body.code === 'send_unknown') error.outcomeUncertain = true;
        throw error;
      }
      return body;
    } catch (error) {
      if (!controller.signal.aborted) throw error;
      const message = path === '/api/danmaku/send' ? '请求超时，弹幕是否发送成功尚未确认。请先到官方直播间核对，避免重复发送。' : method === 'GET' ? '工作台响应超时，请检查连接。' : '请求超时，操作结果尚未确认。请先查看最新状态，再决定是否重试。';
      const timeoutError = new Error(message); timeoutError.outcomeUncertain = method !== 'GET'; throw timeoutError;
    } finally { clearTimeout(timer); }
  }
  function resetSession() {
    pendingQuestions.clear(); localDrafts.clear(); questionPlans.clear(); openedPlans.clear(); openedAudioEvidence.clear(); danmakuTotal = 0;
    state.observations = []; state.messages = []; state.danmaku = []; state.sent_danmaku = []; sentDanmakuSignature = ''; state.memory = {}; state.observer = {};
    understandingSample = null; understandingSourceWatermark = null;
    state.danmaku_candidates = {}; state.danmaku_chorus = {}; danmakuChorusSignature = ''; state.danmaku_auto = {}; danmakuCandidatesSignature = ''; recentDanmaku = []; $('danmaku-copy-fallback').hidden = true;
    state.danmaku_style = {value:30,revision:0,mode:'builtin'}; state.danmaku_corpus = {}; danmakuStyleEditing = false; danmakuStylePending = false; danmakuStyleRequestVersion++; danmakuStyleSaveChain = Promise.resolve();
    if ($('danmaku-auto-feedback')) $('danmaku-auto-feedback').hidden = true;
    if ($('danmaku-style-feedback')) $('danmaku-style-feedback').hidden = true;
    $('danmaku-overlay').replaceChildren();
    stopAudioReplay(); audioRows.clear(); audioSaving.clear(); audioFilter = ''; audioSpeakerSignature = ''; state.audio = {segments: [], speakers: []};
    if ($('speaker-dialog').open) $('speaker-dialog').close(); activeSpeakerId = null; activeSpeakerSession = null;
    stopPlayer();
  }
  function applyState(incoming, options = {}) {
    if (!incoming || !isObject(incoming)) return;
    // An SSE snapshot or mutation response supersedes any older HTTP snapshot.
    requestVersion++;
    const data = incoming.state && isObject(incoming.state) ? incoming.state : incoming;
    const sessionChanged = data.session?.id && state.session.id && data.session.id !== state.session.id;
    if (sessionChanged) resetSession();
    for (const key of ['session','room','playback','observer','memory','model','danmaku_status','danmaku_send','danmaku_chorus','danmaku_auto','audio']) if (isObject(data[key])) state[key] = data[key];
    const styleCurrent = sessionChanged || options.styleRequestVersion === undefined || options.styleRequestVersion === danmakuStyleRequestVersion;
    if (styleCurrent && !danmakuStylePending) {
      applyDanmakuStyle(data.danmaku_style,Boolean(options.resetStyle || sessionChanged || connectionStyleResetPending));
      if (isObject(data.danmaku_style)) connectionStyleResetPending = false;
      applyDanmakuCandidates(data.danmaku_candidates);
      if (isObject(data.danmaku_corpus)) state.danmaku_corpus = data.danmaku_corpus;
    }
    for (const key of ['observations','messages','danmaku','sent_danmaku']) if (Array.isArray(data[key])) state[key] = data[key];
    if (isObject(data.observer)) sampleUnderstandingState(data.observer,true);
    if (Array.isArray(data.observations)) rememberObservationSources(data.observations);
    for (const message of state.messages) { const qid = message.question_id; if (qid && message.role === 'assistant' && message.status && !activeStatuses.has(message.status)) { pendingQuestions.delete(qid); localDrafts.delete(qid); } }
    danmakuTotal = Math.max(danmakuTotal, state.danmaku.length);
    renderAll();
  }
  function refreshState(options = {}) {
    if (document.hidden) return Promise.resolve();
    // A mutation can request a fresh read while a previous read is in flight.
    // Drain that request serially instead of opening competing connections.
    if (refreshInFlight) { refreshQueuedOptions = {...refreshQueuedOptions,...options}; return refreshInFlight; }
    refreshQueuedOptions = options;
    refreshInFlight = (async () => {
      do {
        const nextOptions = refreshQueuedOptions; refreshQueuedOptions = null;
        const version = ++requestVersion, styleRequestVersion = danmakuStyleRequestVersion;
        try { const next = await api('/api/state'); if (version === requestVersion && !document.hidden) applyState(next,{...nextOptions,styleRequestVersion}); }
        catch (error) { if (version === requestVersion && !document.hidden) { $('events-status').textContent = '工作台连接异常'; setDot($('events-dot'), 'bad'); } }
      } while (refreshQueuedOptions && !document.hidden);
    })().finally(() => { refreshInFlight = null; refreshQueuedOptions = null; });
    return refreshInFlight;
  }
  function queueRefresh(delay = 700) { if (document.hidden || refreshTimer) return; refreshTimer = setTimeout(() => { refreshTimer = null; refreshState(); }, delay); }
  function sessionActive() { return Boolean(state.session.id) && !['idle','stopping','stopping_failed','stopped','error'].includes(state.session.status); }
  function renderAll() { renderSession(); renderModel(); renderObserver(); renderObservations(); renderMemory(); renderMessages(); renderDanmaku(); renderDanmakuComposer(); renderAudio(); setupPlayer(); }
  function renderSession() {
    const status = state.session.status || 'idle';
    $('room-status').textContent = statusNames[status] || status;
    setDot($('room-dot'), status === 'live' ? 'good' : ['connecting','reconnecting','stopping'].includes(status) ? 'busy' : status === 'waiting' ? 'warn' : ['error','stopping_failed'].includes(status) ? 'bad' : '');
    const roomId = state.room.room_id || state.room.id;
    let currentRoom = state.session.room_input || (roomId ? `https://live.bilibili.com/${roomId}` : '');
    if (/^\d+$/.test(currentRoom)) currentRoom = `https://live.bilibili.com/${currentRoom}`;
    if (currentRoom && !roomInputDirty && $('room-input').value !== currentRoom) $('room-input').value = currentRoom;
    $('room-id').textContent = roomId ? `房间 ${roomId}` : 'Bilibili';
    $('room-title').textContent = state.room.title || (status === 'waiting' ? '直播间尚未开播' : '等待连接直播间');
    if (roomId && /^\d+$/.test(String(roomId))) $('open-room').href = `https://live.bilibili.com/${roomId}`;
    $('connect-button').disabled = sourceBusy || status === 'stopping';
    $('connect-label').textContent = status === 'stopping' ? '正在断开…' : sourceBusy ? '正在连接…' : sessionActive() ? '切换直播' : '连接直播';
    $('stop-button').disabled = sourceBusy || (!sessionActive() && status !== 'stopping_failed');
    $('question-input').disabled = !sessionActive();
    $('send-button').disabled = !sessionActive() || !$('question-input').value.trim();
    $('composer-hint').textContent = sessionActive() ? 'Enter 发送 · Shift + Enter 换行' : '连接直播间后即可提问';
    const chatStatus = state.danmaku_status || {};
    $('danmaku-status').textContent = ({connected:'弹幕已连接', connecting:'弹幕连接中', disconnected:'弹幕未连接', reconnecting:'弹幕重连中', error:'弹幕连接异常'})[chatStatus.status] || (status === 'waiting' ? '等待开播' : '等待连接');
    $('danmaku-status').title = plain(chatStatus.reason);
    const titles = {idle:'连接直播，开始持续理解', connecting:'正在连接直播间', waiting:'直播间尚未开播', stopping:'正在断开直播连接', stopping_failed:'直播连接未能正常断开', stopped:'直播连接已断开', error:'暂时无法连接直播间', reconnecting:'直播连接正在恢复', live:'正在加载直播画面'};
    const descriptions = {idle:'直播画面、实时弹幕，与始终在线的观察助手。', connecting:'正在解析直播地址，并建立画面与弹幕连接。', waiting:'工作台会等待开播，开播后自动开始观察。', stopping:'正在释放上一个直播间的连接，请稍候。', stopping_failed:state.session.error || '连接清理遇到异常，请重新连接直播间。', stopped:'已有的观察与问答保留在右侧，可重新连接直播。', error:state.session.error || '请检查直播间地址，稍后重新连接。', reconnecting:'恢复连接后会继续观察和记录。', live:'已连接直播间，等待播放器接收视频。'};
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
    renderAudio();
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
  function localDanmakuMode() { return (danmakuTransportRequested || state.danmaku_send.transport) === 'local'; }
  function danmakuCandidateOnly() { return state.danmaku_send.candidate_only === true || state.session.candidate_only === true; }
  function danmakuManualOnly() { return state.danmaku_send.status === 'manual_only' || danmakuCandidateOnly() && state.danmaku_send.allow_manual_send === true; }
  function danmakuCanSend() { return !danmakuTransportBusy && !danmakuLocalAction && state.danmaku_send.can_send === true && (!danmakuCandidateOnly() || danmakuManualOnly()) && (!localDanmakuMode() || state.danmaku_send.local_connected === true); }
  function danmakuCanAutoSend() {
    // Candidate-only patrols never gain automatic sending from a manual grant,
    // including when talking to a server that predates can_auto_send.
    return !danmakuCandidateOnly() && !danmakuManualOnly() && (state.danmaku_send.can_auto_send === undefined || state.danmaku_send.can_auto_send === true) && danmakuCanSend();
  }
  function danmakuAutoDisabledReason() {
    const sending = state.danmaku_send;
    if (danmakuCandidateOnly() || danmakuManualOnly()) return plain(sending.auto_disabled_reason) || (danmakuManualOnly() ? '当前轮巡仅允许手动发送，自动发送已禁用。' : '当前会话只生成候选，自动发送已禁用。');
    if (sending.can_auto_send === false && sending.auto_disabled_reason) return plain(sending.auto_disabled_reason);
    if (state.session.status !== 'live') return '直播中才可启动一键融入';
    if (danmakuTransportBusy || danmakuLocalAction) return '请等待发送位置或本机助手更新完成';
    if (localDanmakuMode() && sending.local_connected !== true) return '请先连接本机助手并在自己的电脑配置登录';
    if (!danmakuCanAutoSend()) return plain(sending.error || sending.reason) || (localDanmakuMode() ? '本机助手暂不可发送，请检查连接和登录' : sending.configured ? '自动发送暂不可用，请查看发送状态' : '请先配置服务器登录');
    return '';
  }
  function transportFeedback(message,error=false) { const node = $('danmaku-transport-feedback'); node.textContent = message; node.hidden = !message; node.classList.toggle('error',error); }
  function applyDanmakuSendResult(result) {
    const sending = result?.danmaku_send || result;
    if (isObject(sending) && ('transport' in sending || 'can_send' in sending || 'configured' in sending)) state.danmaku_send = {...state.danmaku_send,...sending};
    if (isObject(result?.danmaku_auto)) state.danmaku_auto = result.danmaku_auto;
  }
  function renderDanmakuTransport() {
    const sending = state.danmaku_send || {}, local = localDanmakuMode(), connected = sending.local_connected === true;
    $('danmaku-transport').value = local ? 'local' : 'server';
    $('danmaku-transport').disabled = danmakuTransportBusy || Boolean(danmakuLocalAction) || danmakuSending || danmakuCredentialsSaving;
    $('danmaku-transport-help').textContent = danmakuTransportBusy ? '正在切换发送位置…' : local ? '通过我的电脑发送；助手断开时暂停发送，不会改由服务器代发。切换位置会停止一键融入。' : '使用服务器保存的登录信息发送。切换发送位置会停止一键融入。';
    $('danmaku-local-panel').hidden = !local;
    $('danmaku-login-toggle').hidden = local;
    $('danmaku-login-toggle').disabled = danmakuTransportBusy;
    if (local) { $('danmaku-login-form').hidden = true; $('danmaku-cookie').value = ''; $('danmaku-login-toggle').setAttribute('aria-expanded','false'); }
    $('danmaku-local-status').textContent = connected ? '本机助手已连接' : sending.local_paired ? '本机助手已断开' : '等待本机助手';
    $('danmaku-local-status').dataset.connected = String(connected);
    $('danmaku-local-pair').hidden = connected;
    $('danmaku-local-pair').disabled = danmakuTransportBusy || Boolean(danmakuLocalAction);
    $('danmaku-local-pair').textContent = danmakuLocalAction === 'pair' ? '正在生成配对码…' : danmakuPairing ? '重新生成配对码' : '连接本机助手';
    $('danmaku-local-disconnect').hidden = !connected && !sending.local_paired;
    $('danmaku-local-disconnect').disabled = danmakuTransportBusy || Boolean(danmakuLocalAction) || danmakuSending;
    $('danmaku-local-disconnect').textContent = danmakuLocalAction === 'disconnect' ? '正在断开…' : '断开本机助手';
    if (connected || !local) danmakuPairing = null;
    renderDanmakuPairing();
  }
  function renderDanmakuPairing() {
    const left = danmakuPairing ? Math.max(0,Math.ceil((danmakuPairing.expiresAt - performance.now()) / 1000)) : 0;
    if (danmakuPairing && !left) { danmakuPairing = null; transportFeedback('配对码已过期，请点击“连接本机助手”重新生成。'); $('danmaku-local-pair').textContent = '连接本机助手'; }
    $('danmaku-local-pairing').hidden = !danmakuPairing;
    $('danmaku-local-code').textContent = danmakuPairing ? danmakuPairing.code : '';
    $('danmaku-local-expiry').textContent = danmakuPairing ? `有效期剩余 ${Math.floor(left / 60)}:${String(left % 60).padStart(2,'0')} · 刷新网页后需重新生成` : '';
  }
  async function setDanmakuTransport() {
    if (danmakuTransportBusy || danmakuLocalAction || danmakuSending || danmakuCredentialsSaving) return;
    const mode = $('danmaku-transport').value;
    if (!['server','local'].includes(mode)) { renderDanmakuTransport(); return; }
    if (mode === (state.danmaku_send.transport || 'server')) return;
    danmakuTransportBusy = true; danmakuTransportRequested = mode; danmakuPairing = null; transportFeedback(''); renderDanmakuComposer();
    try {
      const result = await api('/api/danmaku/transport',{method:'POST',body:JSON.stringify({mode})});
      applyDanmakuSendResult(result);
      if (state.danmaku_send.transport !== mode) throw new Error('尚未确认发送位置，请查看刷新后的状态。');
      transportFeedback(danmakuCandidateOnly() || danmakuManualOnly() ? `已选择${mode === 'local' ? '我的电脑' : '服务器'}。${danmakuAutoDisabledReason()}` : mode === 'local' ? '已选择我的电脑。连接助手后，再手动发送或开启一键融入。' : '已选择服务器。需要时请重新开启一键融入。');
      await refreshState();
    } catch (error) { transportFeedback(`切换未完成：${error.message}`,true); await refreshState(); }
    finally { danmakuTransportBusy = false; danmakuTransportRequested = null; renderDanmakuComposer(); }
  }
  async function pairLocalDanmaku() {
    if (!localDanmakuMode() || danmakuTransportBusy || danmakuLocalAction || danmakuSending) return;
    danmakuLocalAction = 'pair'; danmakuPairing = null; transportFeedback(''); renderDanmakuComposer();
    try {
      const result = await api('/api/danmaku/local/pairing',{method:'POST',body:'{}'});
      if (!/^\d{8}$/.test(String(result.code || '')) || !Number.isFinite(Number(result.expires_in)) || Number(result.expires_in) <= 0) throw new Error('未获得有效配对码，请手动重试。');
      if (localDanmakuMode()) danmakuPairing = {code:String(result.code),expiresAt:performance.now() + Math.min(300,Number(result.expires_in)) * 1000};
    } catch (error) { transportFeedback(`连接准备失败：${error.message}`,true); }
    finally { danmakuLocalAction = ''; renderDanmakuComposer(); }
  }
  async function disconnectLocalDanmaku() {
    if (!localDanmakuMode() || danmakuTransportBusy || danmakuLocalAction || danmakuSending) return;
    danmakuLocalAction = 'disconnect'; transportFeedback(''); renderDanmakuComposer();
    try {
      const result = await api('/api/danmaku/local/disconnect',{method:'POST',body:'{}'});
      applyDanmakuSendResult(result); danmakuPairing = null;
      await refreshState();
      transportFeedback(state.danmaku_send.local_connected ? '尚未确认断开，请查看本机助手状态。' : '本机助手已断开。重新连接后才能继续发送。',state.danmaku_send.local_connected === true);
    } catch (error) { transportFeedback(`断开未完成：${error.message}`,true); await refreshState(); }
    finally { danmakuLocalAction = ''; renderDanmakuComposer(); }
  }
  function renderDanmakuComposer() {
    const sending = state.danmaku_send || {}, candidates = state.danmaku_candidates || {};
    renderDanmakuTransport();
    const maxLength = Math.max(1,Math.min(1000,Number(sending.max_length) || 20));
    $('danmaku-send-input').maxLength = maxLength;
    const manualOnly = danmakuManualOnly();
    const availability = localDanmakuMode() ? sending.local_connected !== true ? '等待本机助手连接' : danmakuCanSend() ? '我的电脑 · 可以发送' : '我的电脑 · 暂不可发送' : sending.error ? '发送受限 · 请查看提示' : danmakuCanSend() ? '服务器 · 本人登录已配置' : sending.configured ? '服务器 · 等待直播' : '配置服务器登录后可发送';
    $('danmaku-send-status').textContent = danmakuCandidateOnly() && !manualOnly ? '仅生成候选 · 不发送' : danmakuSending ? '正在发送…' : manualOnly && danmakuCanSend() ? '仅手动发送 · 自动发送已禁用' : availability + (manualOnly ? ' · 自动发送已禁用' : '');
    $('danmaku-send-status').title = plain(sending.reason || sending.error || sending.message);
    $('danmaku-send-button').disabled = danmakuSending || !sessionActive() || !danmakuCanSend() || !$('danmaku-send-input').value.trim();
    $('danmaku-send-button').textContent = danmakuSending ? '发送中…' : '发送弹幕';
    $('danmaku-copy-recent').disabled = !sessionActive();
    $('danmaku-login-toggle').textContent = sending.configured ? '更新本人登录' : '配置本人登录';
    const candidateCount = Math.max(1,Math.min(10,Number(state.session.candidate_count) || 5));
    const items = list(candidates.items).filter(item => plain(item.text).trim()).slice(0,candidateCount), signature = JSON.stringify([candidateCount,items]);
    if (signature !== danmakuCandidatesSignature) {
      danmakuCandidatesSignature = signature;
      const nodes = items.map(item => {
        const button = make('button','danmaku-candidate',item.text);
        button.type = 'button'; button.dataset.candidateId = String(item.id ?? '');
        button.title = '只填入输入框，点击发送弹幕后才会发送';
        button.addEventListener('click',() => { $('danmaku-send-input').value = plain(item.text); renderDanmakuComposer(); $('danmaku-send-input').focus(); });
        return button;
      });
      $('danmaku-candidates').replaceChildren(...(nodes.length ? nodes : [make('span','danmaku-candidate-empty',`理解直播内容后，这里会更新 ${candidateCount} 条候选。`)]));
    }
    const updated = items.length && candidates.generated_at ? `${formatTime(candidates.generated_at)} 更新` : '';
    const references = items.length && (candidates.reference_mode === 'local' || state.danmaku_style?.mode === 'local' && candidates.reference_mode === 'builtin') ? Number(candidates.reference_count) > 0 && candidates.reference_mode === 'local' ? `本轮参考 ${Number(candidates.reference_count)} 条` : '本轮使用内置规则' : '';
    $('danmaku-candidates-time').textContent = [candidates.status === 'deferred' ? '优先跟上直播，候选稍后更新' : candidates.status === 'generating' ? '正在更新' : candidates.status === 'error' ? '更新暂缓' : '',updated,references].filter(Boolean).join(' · ');
    $('danmaku-candidates-time').title = plain(candidates.error);
    renderDanmakuStyle();
    renderDanmakuAuto();
    renderSentDanmaku();
    renderDanmakuChorus();
  }
  function renderDanmakuChorus() {
    const chorus = state.danmaku_chorus || {};
    const items = chorus.session_id && chorus.session_id !== state.session.id ? [] : list(chorus.items).filter(item => plain(item.text).trim()).slice(0,3);
    const signature = JSON.stringify(items);
    if (signature === danmakuChorusSignature) return;
    danmakuChorusSignature = signature;
    const nodes = items.map(item => {
      const button = make("button","danmaku-chorus-item");
      button.type = "button"; button.title = "点击填入原文，确认后发送";
      button.append(make("span","danmaku-chorus-text",item.text));
      const count = Number(item.count);
      if (Number.isFinite(count) && count > 0) button.append(make("span","danmaku-chorus-count",`近 30 秒 ×${Math.floor(count)}`));
      button.addEventListener("click",() => { $("danmaku-send-input").value = plain(item.text); renderDanmakuComposer(); $("danmaku-send-input").focus(); });
      return button;
    });
    $("danmaku-chorus-items").replaceChildren(...nodes);
    $("danmaku-chorus-empty").hidden = items.length > 0;
  }
  function renderSentDanmaku() {
    const container = $('danmaku-sent-list');
    const entries = list(state.sent_danmaku).filter(item => isObject(item) && typeof item.text === 'string' && item.text.trim()).slice(-30).reverse();
    const signature = JSON.stringify(entries);
    if (signature === sentDanmakuSignature) return;
    sentDanmakuSignature = signature;
    $('danmaku-sent-count').textContent = String(entries.length);
    $('danmaku-sent-empty').hidden = Boolean(entries.length);
    container.hidden = !entries.length;
    const scrollTop = container.scrollTop;
    const rows = entries.map(item => {
      const row = make('li','danmaku-sent-row'), meta = make('div','danmaku-sent-meta');
      const sentAt = typeof item.sent_at === 'number' && Number.isFinite(item.sent_at) && item.sent_at > 0 ? item.sent_at : null;
      const time = make('time','danmaku-sent-time',sentAt === null ? formatTime(item.timestamp) : formatTime(sentAt));
      if (sentAt !== null) {
        const date = new Date(sentAt * 1000);
        if (!Number.isNaN(date.getTime())) { time.dateTime = date.toISOString(); time.title = date.toLocaleString('zh-CN',{hour12:false}); }
      } else time.title = '本次直播会话时间';
      const source = make('span','danmaku-sent-source',item.source === 'auto' ? '一键融入' : item.source === 'manual' ? '手动发送' : '已发送');
      source.dataset.source = item.source === 'auto' ? 'auto' : 'manual';
      meta.append(time,source);
      row.append(meta,make('p','danmaku-sent-text',item.text));
      return row;
    });
    container.replaceChildren(...rows);
    container.scrollTop = scrollTop;
  }
  function danmakuStyleLabel(value) { return value <= 20 ? '纯良' : value <= 40 ? '轻松接梗' : value <= 60 ? '玩梗' : value <= 80 ? '吐槽' : '开串'; }
  function applyDanmakuStyle(style, reset = false) {
    if (!isObject(style)) return;
    if (!reset && Number(style.revision || 0) < Number(state.danmaku_style?.revision || 0)) return;
    const mode = style.mode === 'local' ? 'local' : 'builtin';
    if (!danmakuStylePending && (style.value !== state.danmaku_style?.value || mode !== state.danmaku_style?.mode)) $('danmaku-style-feedback').hidden = true;
    state.danmaku_style = {...style,mode};
  }
  function applyDanmakuCandidates(candidates) {
    if (!isObject(candidates)) return;
    if (candidates.session_id && candidates.session_id !== state.session.id) return;
    if (candidates.style_revision !== undefined && Number(candidates.style_revision) < Number(state.danmaku_style?.revision || 0)) return;
    state.danmaku_candidates = candidates;
  }
  function renderDanmakuStyle() {
    const input = $('danmaku-style-range'), mode = $('danmaku-style-mode'); if (!input || !mode) return;
    if (!danmakuStyleEditing && !danmakuStylePending) {
      const value = Number(state.danmaku_style?.value);
      input.value = String(Number.isInteger(value) && value >= 0 && value <= 100 ? value : 30);
      mode.value = state.danmaku_style?.mode === 'local' ? 'local' : 'builtin';
    }
    input.disabled = !state.session.id; mode.disabled = !state.session.id;
    const label = danmakuStyleLabel(Number(input.value));
    $('danmaku-style-label').textContent = label;
    input.setAttribute('aria-valuetext',label);
    const local = mode.value === 'local', corpus = state.danmaku_corpus || {};
    $('danmaku-reference-help').textContent = local ? '用你准备的真实弹幕参考语气，每轮仍围绕当前直播接话。' : '开箱即用，无需准备 JSON；结合当前直播和弹幕接话。';
    $('danmaku-corpus-detail').hidden = !local;
    const fileCount = Math.max(0,Number(corpus.file_count) || 0), count = Math.max(0,Number(corpus.indexed_count) || 0), skipped = Math.max(0,Number(corpus.skipped_files) || 0);
    const messages = {not_loaded:'尚未加载本地语料，暂用内置规则。',loading:'正在更新本地语料，直播理解照常进行。',ready:`已加载 ${fileCount} 个文件 / ${count.toLocaleString('zh-CN')} 条风格参考。`,empty:'暂无可用语料，使用内置规则。',error:'本地语料暂不可用，使用内置规则。'};
    $('danmaku-corpus-status').textContent = (messages[corpus.status] || messages.not_loaded) + (skipped ? ` 已跳过 ${skipped} 个不可用文件。` : '');
  }
  async function setDanmakuStyle() {
    const input = $('danmaku-style-range'), modeInput = $('danmaku-style-mode'), feedback = $('danmaku-style-feedback');
    if (!input || !state.session.id) return;
    const value = Number(input.value);
    if (!Number.isInteger(value) || value < 0 || value > 100) { danmakuStyleEditing = false; renderDanmakuStyle(); return; }
    const sid = state.session.id, version = ++danmakuStyleRequestVersion, mode = modeInput?.value === 'local' ? 'local' : 'builtin';
    danmakuStyleEditing = false; danmakuStylePending = true;
    feedback.textContent = '正在更新候选风格…'; feedback.classList.remove('error'); feedback.hidden = false;
    state.danmaku_candidates = {status:'waiting',items:[]}; renderDanmakuComposer();
    // Serialize writes so a slow earlier request cannot overwrite the latest
    // choice on the server. Coalesce changes which have not been sent yet.
    const previousSave = danmakuStyleSaveChain;
    let finishSave;
    danmakuStyleSaveChain = new Promise(resolve => { finishSave = resolve; });
    try {
      await previousSave;
      if (state.session.id !== sid || version !== danmakuStyleRequestVersion) return;
      const result = await api('/api/danmaku/style',{method:'POST',body:JSON.stringify({session_id:sid,style_value:value,mode})});
      if (state.session.id !== sid || version !== danmakuStyleRequestVersion) return;
      applyDanmakuStyle(result.danmaku_style);
      applyDanmakuCandidates(result.danmaku_candidates);
      if (isObject(result.danmaku_corpus)) state.danmaku_corpus = result.danmaku_corpus;
      const actualMode = state.danmaku_style?.mode === 'local' ? '本地语料' : '内置风格';
      feedback.textContent = `下轮候选采用「${actualMode} · ${danmakuStyleLabel(state.danmaku_style?.value ?? value)}」。`;
    } catch (error) {
      if (state.session.id !== sid || version !== danmakuStyleRequestVersion) return;
      feedback.textContent = '风格更新失败，请稍后重试。'; feedback.classList.add('error');
      danmakuStylePending = false;
      await refreshState();
    } finally {
      finishSave();
      if (state.session.id === sid && version === danmakuStyleRequestVersion) { danmakuStylePending = false; renderDanmakuComposer(); if (connectionStyleResetPending) queueRefresh(0); }
    }
  }
  function danmakuIntervalValue() {
    const input = $('danmaku-auto-interval'), raw = input?.value.trim() || '';
    const value = raw === '' ? NaN : Number(raw);
    return !input?.validity.badInput && Number.isInteger(value) && value >= 5 && value <= 3600 ? value : null;
  }
  function danmakuCountValue() {
    const input = $('danmaku-auto-count'), raw = input?.value.trim() || '';
    const value = raw === '' ? NaN : Number(raw);
    return !input?.validity.badInput && Number.isInteger(value) && value >= 1 && value <= 1000 ? value : null;
  }
  function danmakuSeconds(value) { return Number(value).toLocaleString('zh-CN',{maximumFractionDigits:1,useGrouping:false}); }
  function renderDanmakuAuto() {
    const panel = $('danmaku-auto'), input = $('danmaku-auto-interval'), countInput = $('danmaku-auto-count'); if (!panel || !input || !countInput) return;
    const auto = state.danmaku_auto || {}, active = auto.status === 'active';
    const autoDisabledReason = danmakuAutoDisabledReason(), policyBlocked = danmakuCandidateOnly() || danmakuManualOnly();
    const status = auto.status || 'idle', sent = Number(auto.sent_count) || 0, limit = Number(auto.max_messages) || 6;
    const effectiveInterval = finiteTime(auto.interval_seconds);
    // Only a running trial owns these fields. State refreshes must preserve
    // settings the user is still editing for the next trial.
    if (active && effectiveInterval !== null && effectiveInterval >= 5 && effectiveInterval <= 3600) input.value = String(effectiveInterval);
    if (active && Number.isInteger(limit) && limit >= 1 && limit <= 1000) countInput.value = String(limit);
    input.disabled = active || Boolean(danmakuAutoBusy);
    countInput.disabled = active || Boolean(danmakuAutoBusy);
    const interval = danmakuIntervalValue(), count = danmakuCountValue(), serverJitter = finiteTime(auto.jitter_ratio);
    const jitter = active && serverJitter !== null && serverJitter >= 0 && serverJitter <= 1 ? serverJitter : 0.3;
    input.setAttribute('aria-invalid',String(interval === null));
    countInput.setAttribute('aria-invalid',String(count === null));
    $('danmaku-auto-count-help').textContent = count === null ? '请输入 1–1000 之间的整数条数' : '填写 1–1000 的整数，默认 6 条';
    $('danmaku-auto-limit-note').textContent = policyBlocked ? danmakuManualOnly() ? '候选仍会持续生成；选择并点击“发送弹幕”后才发送。' : '候选仍会持续生成，本轮不发送弹幕。' : `${count === null ? '发满所填条数' : `发满 ${count} 条`}后结束，不设运行时限。切房或下播时停止，重启后需重新启动。`;
    $('danmaku-auto-range').textContent = interval === null ? '请输入 5–3600 之间的整数秒数' : `随机 ±${danmakuSeconds(jitter * 100)}% · 每次 ${danmakuSeconds(interval * (1 - jitter))}–${danmakuSeconds(interval * (1 + jitter))} 秒`;
    panel.dataset.status = status;
    $('danmaku-auto-status').textContent = policyBlocked ? '自动发送已禁用' : state.danmaku_send.can_auto_send === false ? '自动发送暂不可用' : ({active:`运行中 · 已发送 ${sent}/${limit} 条`,stopped:`已停止 · 已发送 ${sent}/${limit} 条`,completed:`已结束 · 已发送 ${sent}/${limit} 条`,error:`发送已停止 · 已发送 ${sent}/${limit} 条`,idle:'未启动'})[status] || '未启动';
    const nextInterval = finiteTime(auto.next_interval_seconds);
    const detail = active ? [interval === null ? '' : `基准间隔 ${danmakuSeconds(interval)} 秒`,nextInterval !== null && nextInterval >= 0 ? `本次间隔 ${danmakuSeconds(nextInterval)} 秒` : '',auto.next_send_at ? `下次计划 ${formatTime(auto.next_send_at)}` : '等待新候选',Number(auto.skipped_count) ? `已跳过 ${Number(auto.skipped_count)} 次` : '',plain(auto.reason)] : [plain(auto.error || auto.reason) || '首条新候选就绪后发送，此后按随机间隔接话。',Number(auto.skipped_count) ? `已跳过 ${Number(auto.skipped_count)} 次` : ''];
    $('danmaku-auto-detail').textContent = autoDisabledReason || detail.filter(Boolean).join(' · ');
    $('danmaku-auto-last').textContent = auto.last_text ? `最近发送${auto.last_sent_at ? ` ${formatTime(auto.last_sent_at)}` : ''}：${plain(auto.last_text)}` : '';
    $('danmaku-auto-last').hidden = !auto.last_text;
    $('danmaku-auto-start').hidden = active;
    $('danmaku-auto-start').disabled = danmakuAutoBusy || interval === null || count === null || state.session.status !== 'live' || !danmakuCanAutoSend();
    $('danmaku-auto-start').title = autoDisabledReason || (interval === null ? '请填写有效的基准间隔' : count === null ? '请填写有效的发送数量' : `使用当前间隔向直播间发送，发满 ${count} 条后结束，不设运行时限`);
    $('danmaku-auto-start').textContent = danmakuAutoBusy === 'start' ? '正在融入…' : '一键融入';
    $('danmaku-auto-stop').hidden = !active;
    $('danmaku-auto-stop').disabled = danmakuAutoBusy;
    $('danmaku-auto-stop').textContent = danmakuAutoBusy === 'stop' ? '正在停止…' : '停止融入';
  }
  async function setDanmakuAuto(start) {
    if (danmakuAutoBusy || !state.session.id) return;
    if (start && (state.session.status !== 'live' || !danmakuCanAutoSend() || state.danmaku_auto.status === 'active')) return;
    const sid = state.session.id, feedback = $('danmaku-auto-feedback');
    const interval = danmakuIntervalValue(), count = danmakuCountValue();
    if (start && interval === null) { feedback.textContent = '请填写 5–3600 之间的整数秒数。'; feedback.classList.add('error'); feedback.hidden = false; renderDanmakuAuto(); return; }
    if (start && count === null) { feedback.textContent = '请填写 1–1000 之间的整数条数。'; feedback.classList.add('error'); feedback.hidden = false; renderDanmakuAuto(); return; }
    danmakuAutoBusy = start ? 'start' : 'stop'; feedback.hidden = true; renderDanmakuAuto();
    try {
      const body = start ? {session_id:sid,interval_seconds:interval,max_messages:count} : {session_id:sid};
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
    if (!text || danmakuSending || !sessionActive() || !danmakuCanSend()) return;
    const sid = state.session.id; danmakuSending = true; danmakuFeedback(''); renderDanmakuComposer();
    try {
      const result = await api('/api/danmaku/send',{method:'POST',body:JSON.stringify({session_id:sid,text})});
      if (state.session.id !== sid) return;
      if (result.sent !== true || result.ok === false) { const error = new Error(plain(result.message || result.error) || '未收到明确的成功回执，请先在官方直播间核对，避免重复发送。'); error.outcomeUncertain = true; throw error; }
      if (input.value.trim() === text) input.value = '';
      danmakuFeedback(plain(result.message) || '弹幕已发送。'); queueRefresh(100);
    } catch (error) { if (state.session.id === sid) danmakuFeedback(`${error.outcomeUncertain ? '发送结果未确认' : '发送失败'}：${error.message}`,true); }
    finally { danmakuSending = false; renderDanmakuComposer(); }
  }
  async function saveDanmakuCredentials(event) {
    event.preventDefault(); if (danmakuCredentialsSaving || danmakuTransportBusy || localDanmakuMode()) return;
    const cookie = $('danmaku-cookie').value.trim(); if (!cookie) { danmakuFeedback('请填入本人的登录 Cookie。',true); return; }
    $('danmaku-cookie').value = ''; danmakuCredentialsSaving = true; $('danmaku-login-save').disabled = true; danmakuFeedback('');
    try { const result = await api('/api/danmaku/credentials',{method:'POST',body:JSON.stringify({cookie})}); if (isObject(result.danmaku_send)) state.danmaku_send = result.danmaku_send; else if ('configured' in result) state.danmaku_send = {...state.danmaku_send,...result}; $('danmaku-login-form').hidden = true; $('danmaku-login-toggle').setAttribute('aria-expanded','false'); danmakuFeedback(plain(result.message) || '登录配置已保存，正在更新发送状态。'); await refreshState(); }
    catch (error) { danmakuFeedback(`登录配置失败：${error.message}`,true); }
    finally { danmakuCredentialsSaving = false; $('danmaku-login-save').disabled = false; renderDanmakuComposer(); }
  }
  async function copyRecentDanmaku() {
    const sid = state.session.id;
    const now = performance.now();
    recentDanmaku = recentDanmaku.filter(item => now - item.received <= 2000);
    const entries = recentDanmaku;
    $('danmaku-copy-fallback').hidden = true;
    if (!entries.length) { danmakuFeedback('最近 2 秒没有收到可复制的弹幕。'); return; }
    const text = entries[Math.floor(Math.random() * entries.length)].text;
    $('danmaku-send-input').value = text;
    renderDanmakuComposer();
    $('danmaku-send-input').focus();
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(text);
      if (state.session.id === sid) danmakuFeedback(`已填入发送框并复制：${shortened(text,100)}`);
    } catch {
      if (state.session.id !== sid) return;
      $('danmaku-copy-text').value = text; $('danmaku-copy-fallback').hidden = false;
      $('danmaku-copy-text').focus(); $('danmaku-copy-text').select();
      danmakuFeedback('已填入发送框；未能自动复制，下方文本已选中，可手动复制。');
    }
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
    clearTimeout(playbackWatchdog); playbackWatchdog = null;
    if (hls) { hls.destroy(); hls = null; }
    const video = $('live-video'); video.pause(); video.removeAttribute('src'); video.load(); video.hidden = true;
    $('official-player').removeAttribute('src'); $('official-player').hidden = true; playbackKey = '';
    $('player-placeholder').hidden = false; $('playback-error').hidden = true;
  }
  function playbackError(message) { $('playback-error-text').textContent = message; $('playback-error').hidden = false; }
  function watchPlayback() {
    if (playbackWatchdog || !playbackKey || playerMode !== 'native') return;
    const key = playbackKey;
    playbackWatchdog = setTimeout(() => {
      playbackWatchdog = null;
      if (key === playbackKey && playerMode === 'native' && $('live-video').readyState < 3) playbackError('直播画面超过 30 秒未能就绪。直播可能已结束或连接中断；可检查官方直播间，或重新连接。');
    },30000);
  }
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
    watchPlayback();
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
    if (type === 'danmaku_candidates') { if (!danmakuStylePending) applyDanmakuCandidates(data.danmaku_candidates || data); renderDanmakuComposer(); return; }
    if (type === 'danmaku_chorus') { state.danmaku_chorus = data.danmaku_chorus || data; renderDanmakuChorus(); return; }
    if (type === 'danmaku_send_status') { state.danmaku_send = {...state.danmaku_send,...(data.danmaku_send || data)}; renderDanmakuComposer(); return; }
    if (type === 'sent_danmaku') { if (Array.isArray(data.items)) { state.sent_danmaku = data.items; renderSentDanmaku(); } return; }
    if (type === 'danmaku_auto') { const auto = data.danmaku_auto || data; if (!auto.session_id || auto.session_id === state.session.id) { state.danmaku_auto = auto; renderDanmakuAuto(); } return; }
    if (type === 'danmaku_style') { if (!danmakuStylePending) applyDanmakuStyle(data.danmaku_style || data); renderDanmakuStyle(); return; }
    if (type === 'danmaku_corpus') { state.danmaku_corpus = data.danmaku_corpus || data; renderDanmakuStyle(); return; }
    if (type === 'state') { clearTimeout(refreshTimer); refreshTimer = null; applyState(data); return; }
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
  }
  function disconnectEvents() {
    if (eventSource) eventSource.close();
    eventSource = null;
    clearTimeout(refreshTimer); refreshTimer = null;
    refreshQueuedOptions = null; requestVersion++;
  }
  function connectEvents() {
    if (document.hidden || eventSource) return;
    connectionStyleResetPending = true;
    const source = new EventSource('/api/events'); eventSource = source;
    source.onopen = () => { if (eventSource !== source) return; connectionStyleResetPending = true; seenEvents.clear(); lastEventId = null; recentDanmaku = []; $('events-status').textContent = '工作台已连接'; setDot($('events-dot'),'good'); };
    source.onerror = (event) => { if (event.data || eventSource !== source) return; $('events-status').textContent = '实时通道重连中'; setDot($('events-dot'),'warn'); queueRefresh(); };
    source.onmessage = (event) => { if (eventSource === source) handleEvent(event.data); };
    ['state','session','observation','memory','observer','danmaku','danmaku_candidates','danmaku_chorus','danmaku_send_status','sent_danmaku','danmaku_auto','danmaku_style','danmaku_corpus','question_started','plan','answer_delta','answer_done','error','playback','audio_segment','audio_status','audio_speakers'].forEach(type => source.addEventListener(type,event => { if (eventSource === source && event.data) handleEvent(event.data,type); }));
    // The server sends an authoritative snapshot first, including on reconnect.
    // Fall back to HTTP if the event channel cannot deliver that initial state.
    queueRefresh(3000);
  }
  async function connectRoom(event) {
    event.preventDefault(); const room = $('room-input').value.trim(); if (!room || sourceBusy) return;
    sourceBusy = true; renderSession(); notice('');
    try { const result = await api('/api/session',{method:'POST',body:JSON.stringify({room})}); if ($('room-input').value.trim() === room) roomInputDirty = false; applyState(result); if (!result.session && !result.state) await refreshState(); }
    catch (error) { notice(error.message,true); }
    finally { sourceBusy = false; renderSession(); }
  }
  async function stopSession() {
    if (sourceBusy || (!sessionActive() && state.session.status !== 'stopping_failed')) return;
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
  $('danmaku-auto-interval')?.addEventListener('input',renderDanmakuAuto);
  $('danmaku-auto-count')?.addEventListener('input',renderDanmakuAuto);
  $('danmaku-style-range')?.addEventListener('pointerdown',() => { danmakuStyleEditing = true; });
  $('danmaku-style-range')?.addEventListener('input',() => { danmakuStyleEditing = true; renderDanmakuStyle(); });
  $('danmaku-style-range')?.addEventListener('change',setDanmakuStyle);
  $('danmaku-style-range')?.addEventListener('blur',() => { danmakuStyleEditing = false; renderDanmakuStyle(); });
  $('danmaku-style-mode')?.addEventListener('change',setDanmakuStyle);
  $('danmaku-transport').addEventListener('change',setDanmakuTransport);
  $('danmaku-local-pair').addEventListener('click',pairLocalDanmaku);
  $('danmaku-local-disconnect').addEventListener('click',disconnectLocalDanmaku);
  $('danmaku-login-form').addEventListener('submit',saveDanmakuCredentials);
  $('danmaku-login-toggle').addEventListener('click',() => { if (localDanmakuMode() || danmakuTransportBusy) return; const form = $('danmaku-login-form'); form.hidden = !form.hidden; $('danmaku-login-toggle').setAttribute('aria-expanded',String(!form.hidden)); if (!form.hidden) $('danmaku-cookie').focus(); });
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
  $('live-video').addEventListener('playing',() => { clearTimeout(playbackWatchdog); playbackWatchdog = null; $('playback-error').hidden = true; });
  $('live-video').addEventListener('waiting',watchPlayback);
  $('live-video').addEventListener('stalled',watchPlayback);
  $('live-video').addEventListener('error',() => { if (playbackKey && playerMode === 'native') { const code = $('live-video').error?.code; playbackError(({2:'直播流读取中断，可等待重连或切换官方播放器。',3:'浏览器解码直播画面失败，可尝试官方播放器。',4:'浏览器暂时无法读取此视频格式，可尝试官方播放器。'})[code] || '播放器暂时无法播放，可以尝试官方播放器。'); } });
  document.addEventListener('visibilitychange',() => { if (document.hidden) disconnectEvents(); else connectEvents(); });
  window.addEventListener('pageshow',event => { if (event.persisted) connectEvents(); });
  window.addEventListener('pagehide',() => { clearTimeout(playbackWatchdog); playbackWatchdog = null; disconnectEvents(); if (hls) { hls.destroy(); hls = null; playbackKey = ''; } });
  switchTab('activity'); renderAll(); connectEvents();
  setInterval(() => { if (!document.hidden && !refreshTimer) refreshState(); },15000);
  setInterval(() => { if (!document.hidden) { renderUnderstandingTiming(); if (danmakuPairing) renderDanmakuPairing(); } },1000);
})();
