'use strict';
const $ = id => document.getElementById(id);
const icons = {
 layers:'<path d="m3 7 9-5 9 5-9 5-9-5Z"/><path d="m3 12 9 5 9-5M3 17l9 5 9-5"/>',
 sparkles:'<path d="m12 3 2.2 6.8L21 12l-6.8 2.2L12 21l-2.2-6.8L3 12l6.8-2.2L12 3ZM20 2v4M18 4h4"/>',
 book:'<path d="M12 5v16M3 3h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5v16h-5a4 4 0 0 0-4 2 4 4 0 0 0-4-2H3V3Z"/>',
 plus:'<path d="M12 5v14M5 12h14"/>',radio:'<circle cx="12" cy="12" r="2"/><path d="M7 7a7 7 0 0 0 0 10M17 7a7 7 0 0 1 0 10M4 4a11 11 0 0 0 0 16M20 4a11 11 0 0 1 0 16"/>',
 scissors:'<circle cx="6" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="m8.1 8.1 12.9 12.9M8.1 15.9 12 12m3-3 6-6"/>',
 list:'<path d="M8 5h13M8 12h13M8 19h13M3 5h.01M3 12h.01M3 19h.01"/>',crop:'<path d="M6 3v15h15M3 6h15v15"/>',flag:'<path d="M4 22V3c5-4 11 4 16 0v11c-5 4-11-4-16 0"/>',
 activity:'<path d="M2 12h4l3-8 6 16 3-8h4"/>',download:'<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',cursor:'<path d="m4 3 16 10-8 2-4 7-4-19Z"/>',monitor:'<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8m-4-4v4"/>',phone:'<rect x="6" y="2" width="12" height="20" rx="2"/><path d="M11 18h2"/>',
 check:'<path d="m4 12 5 5L20 6"/>',wand:'<path d="m3 21 14-14 4 4L7 25M14 10l4 4M4 3v4M2 5h4M17 1v4M15 3h4"/>',x:'<path d="m6 6 12 12M6 18 18 6"/>',upload:'<path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/>',arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',external:'<path d="M14 3h7v7m0-7L10 14M10 3H3v18h18v-7"/>',video:'<rect x="3" y="3" width="18" height="18" rx="3"/><path d="m10 8 6 4-6 4V8Z"/>'
};
const icon = name => `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.video}</svg>`;
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function hydrateIcons(root=document) { root.querySelectorAll('[data-icon]').forEach(el => { el.innerHTML=icon(el.dataset.icon); }); }
hydrateIcons();
const app = {state:{sessions:[],anchors:[],jobs:[],publications:[],styles:[],references:[],health:{}},detail:null,sid:null,aid:null,filter:'all',source:'live',aspect:'landscape',selectedEvents:new Set(),region:null,dirty:false,lastMedia:'',hls:null,liveRefresh:0,stateBusy:false,detailBusy:false,sourceBusy:false,previewEnd:null,dragging:false,regionMode:false,selectedFile:null};
const publishing = {mode:'job',jobId:null,sessionId:null,autoEnable:false,ready:false,busy:false,account:null,requestId:0,refreshing:new Set(),settingsBusy:new Set()};
const player=$('player');
function time(seconds, decimals=false){const n=Math.max(0,Number(seconds)||0);const h=Math.floor(n/3600),m=Math.floor(n%3600/60),s=Math.floor(n%60);return (h?String(h).padStart(2,'0')+':':'')+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0')+(decimals?'.'+Math.floor(n%1*10):'');}
function parseTime(value){const p=String(value).trim().split(':').map(Number);if(p.some(n=>!Number.isFinite(n)||n<0)||p.length>3)return NaN;return p.reduce((a,n)=>a*60+n,0);}
function duration(){return Math.max(1,Number(app.detail?.session?.duration)||0,Number.isFinite(player.duration)?player.duration:0,...(app.detail?.segments||[]).map(s=>Number(s.end)||0),...(app.detail?.events||[]).map(e=>Number(e.end)||Number(e.start)||0));}
function session(){return app.detail?.session || app.state.sessions.find(s=>s.id===app.sid);}
function anchor(){return (app.detail?.anchors||app.state.anchors).find(a=>a.id===app.aid);}
const liveStatuses=['live','recording','connecting','running'];
const analysisBusy=['running','transcribing','pending','analyzing','分析中','正在提取画面与语音'];
function isLive(s){return !!s&&liveStatuses.includes(s.status);}
function statusLabel(s){return ({live:'直播中',recording:'录制中',connecting:'连接中',running:'运行中',stopped:'已结束',imported:'已导入',ready:'可编辑',error:'连接异常',failed:'失败',completed:'已完成',complete:'已完成',finished:'已完成'})[s]||s||'已导入';}
function toast(message,type=''){const el=document.createElement('div');el.className='toast '+type;el.textContent=message;$('toastStack').append(el);setTimeout(()=>el.remove(),type==='error'?8500:4300);}
async function api(path,method='GET',body){const opt={method,headers:{}};if(body!==undefined){opt.headers['Content-Type']='application/json';opt.body=JSON.stringify(body);}const r=await fetch(path,opt);let d;try{d=await r.json();}catch{d={detail:r.statusText};}if(!r.ok){let error=d.detail||d.error||d.message||`请求失败 (${r.status})`;if(typeof error!=='string')error=JSON.stringify(error);throw new Error(error);}return d;}
function setDirty(){app.dirty=true;$('editStatus').textContent='未保存';$('editStatus').classList.add('dirty');renderRange();}
function clearDirty(){app.dirty=false;$('editStatus').textContent=app.aid?'已保存':'待选择';$('editStatus').classList.remove('dirty');}
async function refreshState(){if(app.stateBusy)return;app.stateBusy=true;try{app.state=await api('/api/state');app.state.sessions ||= [];app.state.anchors ||= [];app.state.jobs ||= [];app.state.publications ||= [];app.state.references ||= [];$('connectionStatus').className='connection';$('connectionStatus').innerHTML='<i></i>本地服务已连接';renderLibrary();renderHealth();renderReferences();if(!app.sid&&app.state.sessions.length){await selectSession(app.state.sessions[0].id);}else if(app.sid){const s=app.state.sessions.find(s=>s.id===app.sid);if(s&&app.detail){app.detail.session={...app.detail.session,...s};renderSessionInfo();}if(app.detail){app.detail.anchors=app.state.anchors.filter(a=>a.session_id===app.sid);app.detail.jobs=app.state.jobs.filter(j=>j.session_id===app.sid);renderAnchors();renderJobs();}if(!app.detail||Date.now()-app.liveRefresh>4000)await refreshDetail();}}catch(e){$('connectionStatus').className='connection disconnected';$('connectionStatus').innerHTML='<i></i>连接暂时中断';$('healthText').textContent='服务暂未连接，正在重试';$('healthDot').classList.add('warning');}finally{app.stateBusy=false;}}
function renderHealth(){const h=app.state.health||{};const bits=[];if(h.ffmpeg)bits.push('视频引擎就绪');else bits.push('视频引擎未就绪');bits.push(h.model_configured?'语义分析已连接':'规则检测可用');if(h.audio_configured)bits.push('语音服务已连接');$('healthText').textContent=bits.join('  ·  ');$('healthDot').classList.toggle('warning',!h.ffmpeg);}
function renderLibrary(){const ss=app.state.sessions||[];$('sessionCount').textContent=ss.length;$('liveCount').textContent=ss.filter(isLive).length;const filtered=ss.filter(s=>app.filter!=='live'||isLive(s));$('sessionList').innerHTML=filtered.length?filtered.map(s=>`<button class="session-card ${s.id===app.sid?'selected':''}" data-session="${esc(s.id)}"><span class="session-thumb ${isLive(s)?'live':''}">${icon(isLive(s)?'radio':'video')}</span><div><h3>${esc(s.title||'未命名素材')}</h3><p>${time(s.duration)} · ${esc(isLive(s)?'B站直播':'录像素材')}${isLive(s)?'<span class="live-dot">LIVE</span>':''}</p></div></button>`).join(''):'<div class="library-empty">'+(app.filter==='live'?'还没有正在录制的直播':'素材库还是空的')+'<p>添加直播或录像，开始发现亮点。</p></div>';}
async function selectSession(id){if(app.sid===id&&app.detail)return;app.sid=id;app.aid=null;app.detail=null;app.region=null;app.selectedEvents.clear();app.dirty=false;app.lastMedia='';if(app.hls){app.hls.destroy();app.hls=null;}player.pause();player.removeAttribute('src');player.load();renderLibrary();renderEditor();await refreshDetail(true);}
async function refreshDetail(force=false){if(!app.sid||app.detailBusy)return;app.detailBusy=true;const sid=app.sid;try{const d=await api('/api/sessions/'+encodeURIComponent(sid));if(app.sid!==sid)return;app.detail=d;app.detail.anchors ||= [];app.detail.jobs ||= [];app.detail.events ||= [];app.liveRefresh=Date.now();renderSessionInfo();setMedia(d.media_url,d.frame_url);renderTimeline();renderAnchors();renderJobs();if(!app.aid&&d.anchors.length)selectAnchor(d.anchors[0].id,false);else if(app.aid){if(!anchor()){app.aid=null;renderEditor();}else if(!app.dirty)renderEvents();}}catch(e){if(force)toast(e.message,'error');}finally{app.detailBusy=false;}}
function renderSessionInfo(){const s=session();if(!s)return;$('sessionTitle').textContent=s.title||'未命名素材';$('sessionPlatform').textContent=s.mode==='live'?'B站直播':'录像素材';$('sessionBadge').className='badge '+(isLive(s)?'live':s.status==='error'?'error':'');$('sessionBadge').textContent=statusLabel(s.status);const as=({analyzing:'正在分析亮点…',rules:'候选亮点已整理，请审核',partial:'部分亮点分析完成',waiting_for_events:'等待转写或画面信息',waiting_for_media:'等待录像片段',running:'正在分析亮点…',transcribing:'正在转写语音…',completed:'亮点分析完成',complete:'亮点分析完成',failed:'分析失败',error:'分析异常',pending:'等待分析'})[s.analysis_status] || (/^[\u4e00-\u9fff]/.test(s.analysis_status||'')?s.analysis_status:'');$('sessionMeta').textContent=`${time(s.duration)}  ·  ${as||'保留完整语境，让每个笑点都有来由'}`;$('sessionActions').classList.remove('hidden');$('stopButton').classList.toggle('hidden',!isLive(s));$('markButton').disabled=false;$('addAnchor').disabled=false;$('regionButton').disabled=!app.detail?.media_url;$('analyzeButton').disabled=analysisBusy.includes(s.analysis_status);$('analyzeButton').innerHTML=icon('sparkles')+(analysisBusy.includes(s.analysis_status)?'分析中…':'分析亮点');$('autoRenderInput').disabled=false;$('thresholdInput').disabled=false;if(document.activeElement!==$('autoRenderInput'))$('autoRenderInput').checked=!!s.auto_render;if(document.activeElement!==$('thresholdInput')){$('thresholdInput').value=s.minimum_score??88;$('thresholdValue').textContent=s.minimum_score??88;}const warning=[s.warning,s.analysis_warning].filter(Boolean).join(' · ');$('sessionWarning').textContent=warning;$('sessionWarning').classList.toggle('hidden',!warning);$('previewLabel').textContent=isLive(s)?'直播录像预览':'原始录像预览';renderPublishingSettings();}
function setMedia(url,frame){if(url&&url!==app.lastMedia){app.lastMedia=url;if(app.hls){app.hls.destroy();app.hls=null;}const isHls=/\.m3u8(?:\?|$)/.test(url);if(isHls&&window.Hls?.isSupported()){app.hls=new Hls({maxBufferLength:30});app.hls.loadSource(url);app.hls.attachMedia(player);}else player.src=url;if(frame)player.poster=frame;$('videoStage').classList.add('has-video');$('videoPlaceholder').classList.add('hidden');$('previewWatermark').classList.remove('hidden');}else if(!url){$('videoStage').classList.remove('has-video');$('videoPlaceholder').classList.remove('hidden');$('previewWatermark').classList.add('hidden');if(app.sid){$('videoPlaceholder').querySelector('h3').textContent=isLive(session())?'正在收集直播画面':'视频预览暂不可用';$('videoPlaceholder').querySelector('p').textContent=isLive(session())?'录制片段生成后，会在这里出现':'你仍然可以查看锚点和编辑时间范围';$('emptyAdd').classList.add('hidden');}}}
function mediaToSession(seconds){const segments=app.detail?.playback_segments||[];if(!segments.length)return seconds;const s=segments.find(s=>seconds>=s.media_start&&seconds<s.media_end);if(s)return s.start+Math.min(s.end-s.start,seconds-s.media_start);if(seconds<segments[0].media_start)return segments[0].start;return segments[segments.length-1].end;}
function sessionToMedia(seconds){const segments=app.detail?.playback_segments||[];if(!segments.length)return{time:seconds,gap:false};const s=segments.find(s=>seconds>=s.start&&seconds<s.end);if(s)return{time:s.media_start+Math.min(s.media_end-s.media_start,seconds-s.start),gap:false};const next=segments.find(s=>s.start>seconds);if(next)return{time:next.media_start,gap:true,at:next.start};const last=segments[segments.length-1];return{time:Math.max(last.media_start,last.media_end-.05),gap:seconds>last.end,at:last.end};}
function currentTime(){return mediaToSession(Number(player.currentTime)||0);}
function seek(t){app.previewEnd=null;if(app.detail?.media_url){const target=sessionToMedia(Math.max(0,Math.min(duration(),Number(t)||0)));player.currentTime=target.time;if(target.gap)toast('此处没有已保存的画面，已定位到 '+time(target.at));updatePlayhead();}}
function renderTimeline(){const total=duration(),heat=app.detail?.heatmap||[];const bins=72;let values=Array.from({length:bins},()=>({score:0,count:0}));heat.forEach(h=>{const i=Math.min(bins-1,Math.floor((Number(h.time)||0)/total*bins));values[i].score=Math.max(values[i].score,Number(h.score)||0);values[i].count+=Number(h.count)||0;});const maxCount=Math.max(1,...values.map(v=>v.count));$('heatmap').innerHTML=values.map(v=>{const strength=Math.max(v.score/100,v.count/maxCount*.65);return `<i class="heat-bar ${strength>.7?'high':strength>.35?'medium':''}" style="height:${Math.max(5,strength*100)}%"></i>`;}).join('');$('timelineEmpty').classList.toggle('hidden',heat.some(h=>Number(h.score)>0||Number(h.count)>0));$('timelineTicks').innerHTML=Array.from({length:5},(_,i)=>`<span>${app.sid?time(total*i/4):(i?'--:--':'00:00')}</span>`).join('');$('timelineMarkers').innerHTML=(app.detail?.anchors||[]).map(a=>`<button title="${esc(a.title)}" class="timeline-marker" data-anchor="${esc(a.id)}" style="left:${Math.min(100,100*(a.timestamp??a.start)/total)}%"></button>`).join('');renderRange();updatePlayhead();}
function updatePlayhead(){const t=currentTime(),d=duration();if(app.previewEnd!==null&&t>=app.previewEnd){player.pause();app.previewEnd=null;}$('playhead').style.left=Math.min(100,t/d*100)+'%';$('timelineTime').textContent=time(t)+' / '+time(app.sid?d:0);}
function renderRange(){const a=anchor();$('rangeSelection').classList.toggle('hidden',!a);if(!a)return;const start=parseTime($('startInput').value),end=parseTime($('endInput').value);if(!Number.isFinite(start)||!Number.isFinite(end))return;const d=duration();$('rangeSelection').style.left=Math.max(0,Math.min(100,start/d*100))+'%';$('rangeSelection').style.width=Math.max(.15,Math.min(100,(end-start)/d*100))+'%';$('clipLength').textContent=Math.max(0,Math.round((end-start)*10)/10)+' 秒';}
function renderAnchors(){const aa=app.detail?.anchors||[];$('anchorCount').textContent=aa.length;$('anchorList').innerHTML=aa.length?aa.map(a=>`<button class="anchor-card ${a.id===app.aid?'selected':''}" data-anchor="${esc(a.id)}"><div class="anchor-card-top"><i class="anchor-dot"></i><span class="anchor-time">${time(a.start)} — ${time(a.end)}</span><span>${a.origin==='manual'?'手动':a.status==='candidate'?'候选':'自动'}</span><span class="score-chip">${a.origin==='manual'?'已标记':a.score==null?'':Math.round(a.score)+' 分'}</span></div><h4>${esc(a.title||'未命名锚点')}</h4><p>${esc(a.summary||a.reason||'点击编辑这个瞬间')}</p>${a.review_reason?`<p>${esc(a.review_reason)}</p>`:''}</button>`).join(''):'<div class="empty-list">'+(app.sid?'还没有锚点。分析亮点，或在喜欢的画面按 M 打点。':'你的第一条精彩切片，从一份素材开始。')+'</div>';renderTimeline();}
function selectAnchor(id,doSeek=true){app.aid=id;app.regionMode=false;$('regionLayer').classList.add('hidden');renderEditor();renderAnchors();if(doSeek&&anchor())seek(anchor().start);}
function renderEditor(){const a=anchor();$('editor').classList.toggle('hidden',!a);$('inspectorEmpty').classList.toggle('hidden',!!a);if(!a){clearDirty();$('rangeSelection').classList.add('hidden');return;}$('anchorOrigin').textContent=a.origin==='manual'?'手动锚点':'自动发现';$('anchorScore').innerHTML=a.origin==='manual'?'由你发现':a.score==null?'':`<small>精彩指数</small>${Math.round(a.score)}`;$('anchorReason').textContent=[a.reason||a.summary||'调整片段，保留话题的起因、发展与收尾。',a.review_reason].filter(Boolean).join(' ');$('titleInput').value=a.title||'';$('startInput').value=time(a.start,true);$('endInput').value=time(a.end,true);$('coverInput').value=a.cover_text||'';$('styleInput').value=a.style||session()?.style||'reaction';app.region=a.focus_region||null;app.selectedEvents=new Set(a.focus_event_ids||[]);$('titleVariants').innerHTML=(a.title_variants||[]).slice(0,3).map(v=>{const title=typeof v==='string'?v:(v.title||v.text||'');return `<button class="variant-button" title="${esc(title)}" data-title="${esc(title)}">${esc(title)}</button>`;}).join('');clearDirty();renderEvents();renderRegion();renderRange();}
function renderEvents(){const a=anchor();if(!a)return;const start=parseTime($('startInput').value),end=parseTime($('endInput').value);let events=(app.detail?.events||[]).filter(e=>['transcript','danmaku','observation'].includes(e.kind)&&Number(e.start)<=end&&Number(e.end??e.start)>=start);events.sort((a,b)=>Number(a.start)-Number(b.start));const selectedFirst=events.filter(e=>app.selectedEvents.has(e.id)),rest=events.filter(e=>!app.selectedEvents.has(e.id));if(events.length>30)events=[...selectedFirst,...rest.slice(0,30-selectedFirst.length)].sort((a,b)=>Number(a.start)-Number(b.start));$('eventSelectionCount').textContent=app.selectedEvents.size+' 条';$('eventList').innerHTML=events.length?events.map(e=>`<label class="event-card ${app.selectedEvents.has(e.id)?'selected':''}">${e.kind==='danmaku'?`<input type="checkbox" data-event="${esc(e.id)}" ${app.selectedEvents.has(e.id)?'checked':''}>`:`<span class="event-context-icon">${icon(e.kind==='transcript'?'list':'video')}</span>`}<span class="event-copy"><span>${e.kind==='danmaku'?'弹幕':e.kind==='transcript'?'语音原文':'画面观察'} · ${time(e.start)}</span><p>${esc(e.text)}</p></span></label>`).join(''):'<div class="events-empty">此片段还没有转写或弹幕。分析素材，或导入带时间的事件 JSON。</div>';}
function renderRegion(){const r=app.region;$('regionSummary').classList.toggle('hidden',!r);if(r)$('regionText').textContent=`已框选 ${Math.round(r.w*100)}% × ${Math.round(r.h*100)}% · ${time(r.start)}—${time(r.end)}`;}
const publicationLabels = {queued:'等待上传',uploading:'上传中',submitting:'正在提交审核',submitted:'已提交 · 审核中',published:'审核通过 · 已公开',private:'仅自己可见',rejected:'审核未通过',failed:'投稿失败',uncertain:'结果待核实'};
function jobPublications(jobId){return (app.state.publications||[]).filter(p=>p.job_id===jobId);}
function publicationBlocksRetry(jobId){return jobPublications(jobId).some(p=>p.status!=='failed');}
function publicationLink(publication){
  try { const url=new URL(publication.url||''); return ['https:','http:'].includes(url.protocol)?url.href:''; } catch { return ''; }
}
function renderSeries(series){
  if(!series||series.status==='skipped')return '';
  const labels={added:'已归入系列',waiting_for_publication:'审核通过后归档',pending:'等待系列归档',uncertain:'系列结果待核实',failed:'系列归档未完成',blocked:'系列归档待处理',busy:'正在整理系列'};
  let link='';
  try{
    const url=new URL(series.url||'');
    if(url.protocol==='https:'&&url.hostname==='space.bilibili.com'&&!url.username&&!url.password)
      link=` <a href="${esc(url.href)}" target="_blank" rel="noopener">主页查看系列 ${icon('external')}</a>`;
  }catch{}
  return `<p class="publication-series">${esc(labels[series.status]||'系列归档待核实')}${series.name?' · '+esc(series.name):''}${link}</p>${series.error?`<p class="job-warning">${esc(series.error)}</p>`:''}`;
}
function renderPublication(p){
  const label=publicationLabels[p.status]||'等待核实状态',busy=['queued','uploading','submitting'].includes(p.status);
  const progress=Math.max(0,Math.min(100,Number(p.progress)<=1?Number(p.progress)*100:Number(p.progress)))||0;
  const help=p.status==='private'?'稿件已设为仅自己可见，可在 B 站创作中心查看。':p.status==='submitted'?'平台已接收，审核通过后才会公开。':p.status==='uncertain'?'请先刷新结果或前往 B 站创作中心核实，请勿重复投稿。':p.status==='rejected'?'请到 B 站创作中心查看原因并修改稿件。':'';
  const url=publicationLink(p);
  return `<div class="publication-record publication-${esc(p.status)}" data-publication="${esc(p.id)}"><div class="publication-heading"><strong>B 站 · ${esc(label)}${busy?' · '+Math.round(progress)+'%':''}</strong><div class="publication-actions">${url?`<a href="${esc(url)}" target="_blank" rel="noopener">${p.status==='published'?'查看作品':'查看稿件'} ${icon('external')}</a>`:''}<button type="button" class="text-button" data-refresh-publication="${esc(p.id)}" ${publishing.refreshing.has(p.id)?'disabled':''}>${publishing.refreshing.has(p.id)?'刷新中…':'刷新结果'}</button></div></div>${busy?`<div class="job-progress"><i style="width:${progress}%"></i></div>`:''}${p.bvid?`<p class="publication-id">${esc(p.bvid)}</p>`:''}${help?`<p>${esc(help)}</p>`:''}${p.error||p.platform_message?`<p class="${['failed','rejected'].includes(p.status)?'job-error':''}">${esc(p.error||p.platform_message)}</p>`:''}${renderSeries(p.series)}</div>`;
}
function renderJobs(){
  const jj=app.detail?.jobs||[];$('jobCount').textContent=jj.length;
  $('jobList').innerHTML=jj.length?jj.slice().reverse().map(j=>{
    const a=(app.detail?.anchors||[]).find(a=>a.id===j.anchor_id);
    const done=!!j.video_url||['done','completed','complete','ready','finished'].includes(j.status),error=['error','failed'].includes(j.status);
    const progress=Number(j.progress)||0,publications=jobPublications(j.id),blocked=publicationBlocksRetry(j.id);
    return `<div class="job-card"><div class="job-main"><div class="job-cover">${j.cover_url?`<img src="${esc(j.cover_url)}" alt="切片封面" loading="lazy">`:icon('video')}</div><div class="job-body"><strong>${esc(a?.title||j.title||'切片制作任务')}</strong><p class="${error?'job-error':''}">${error?esc(j.error||'制作失败'):done?'制作完成':`${({queued:'排队中',pending:'等待制作',running:'制作中',rendering:'渲染中'})[j.status]||'处理中'} · ${Math.round(progress<=1?progress*100:progress)}%`}</p>${j.warning?`<p class="job-warning">${esc(j.warning)}</p>`:''}${!done&&!error?`<div class="job-progress"><i style="width:${Math.min(100,progress<=1?progress*100:progress)}%"></i></div>`:''}</div><div class="job-actions">${j.video_url?`<a href="${esc(j.video_url)}" download>视频</a>`:''}${j.cover_url?`<a href="${esc(j.cover_url)}" target="_blank" rel="noopener">封面</a>`:''}${j.package_url?`<a href="${esc(j.package_url)}" download>素材包</a>`:''}${done?`<button type="button" class="button small secondary publish-job" data-publish-job="${esc(j.id)}" ${blocked?'disabled':''}>${icon('upload')}${blocked?'已发起投稿':publications.length?'重试投稿':'投稿 B 站'}</button>`:''}</div></div>${publications.map(renderPublication).join('')}</div>`;
  }).join(''):'<div class="empty-list">做好的片段会出现在这里，随时预览与下载。</div>';
  updatePublishSubmit();
}
function updatePublishSubmit(){
  if(!$('publishDialog').open)return;
  const blocked=publishing.mode==='job'&&publicationBlocksRetry(publishing.jobId);
  const accountRequired=publishing.mode==='job'||publishing.autoEnable;
  $('publishSubmit').disabled=!publishing.ready||publishing.busy||blocked||(accountRequired&&!publishing.account?.logged_in);
  $('publishSubmit').textContent=publishing.busy?'正在提交…':publishing.mode==='profile'?(publishing.autoEnable?'保存并开启自动投稿':'保存投稿设置'):blocked?'已发起投稿':'提交 B 站审核';
}
function publishError(message){$('publishError').textContent=message||'';$('publishError').classList.toggle('hidden',!message);}
function updatePublishSource(){
  const reprint=$('publishCopyright').value==='2';
  $('publishSourceField').classList.toggle('hidden',!reprint);$('publishSource').required=reprint;
}
function setPublishAccount(account){
  publishing.account=account;
  $('publishAccountName').textContent=account.logged_in?`投稿账号：${account.name||'B 站账号'}${account.uid?' · UID '+account.uid:''}`:account.configured?'投稿账号暂未登录':'尚未配置投稿账号';
  $('publishAccountHint').textContent=account.logged_in?'已登录，投稿将发送到此账号':account.error||(account.configured?'请在服务端更新账号登录状态后重新检测。':'请在服务端配置投稿账号后重新检测。');
  $('publishAccountName').classList.toggle('account-ready',!!account.logged_in);updatePublishSubmit();
}
async function refreshPublishAccount(requestId=publishing.requestId){
  $('refreshPublishAccount').disabled=true;
  try{const account=await api('/api/publishing/account');if(requestId===publishing.requestId)setPublishAccount(account);}
  catch(e){if(requestId===publishing.requestId)setPublishAccount({configured:true,logged_in:false,error:e.message});}
  finally{if(requestId===publishing.requestId)$('refreshPublishAccount').disabled=false;}
}
function preparePublishDialog(mode){
  publishing.mode=mode;publishing.ready=false;publishing.busy=false;publishing.account=null;publishing.requestId++;
  const profile=mode==='profile';
  $('publishForm').reset();publishError('');$('publishWarnings').classList.add('hidden');$('reloadPublishPreview').classList.add('hidden');
  $('publishFields').disabled=true;$('publishLoading').classList.remove('hidden');
  $('publishLoading').textContent=profile?'正在加载素材投稿设置…':'正在生成投稿信息…';
  $('publishHeading').textContent=profile?'本素材投稿设置':'投稿 B 站';
  $('publishIntro').textContent=profile?'后续成片使用这些设置自动生成标题、简介与标签。':'确认标题与内容后，视频和已生成的封面将提交至 B 站审核。';
  $('publishFootnote').textContent=profile?(publishing.autoEnable?'保存后开启此素材的自动投稿，后续成片将直接提交审核。':'设置仅用于当前素材之后的投稿。'):'提交成功后仍需等待平台审核；通过后才会公开。';
  $('publishTitleField').classList.toggle('hidden',profile);$('publishTitle').required=!profile;
  $('publishProfileFields').classList.toggle('hidden',!profile);$('publishTags').required=!profile;
  $('publishDescriptionLabel').textContent=profile?'简介补充说明':'简介';$('publishTagsLabel').textContent=profile?'基础标签（可选）':'标签';
  $('publishAccountName').textContent='正在检查投稿账号…';$('publishAccountName').classList.remove('account-ready');$('publishAccountHint').textContent='使用服务端配置的 B 站账号';
  if(!$('publishDialog').open)$('publishDialog').showModal();updatePublishSubmit();return publishing.requestId;
}
async function loadPublishDialog(jobId=null,profileSession=null,autoEnable=false){
  if(publishing.busy)return;
  if(jobId&&publicationBlocksRetry(jobId)){toast('此成片已有投稿，请先查看或刷新结果');return;}
  publishing.jobId=jobId;publishing.sessionId=profileSession?.id||app.sid;publishing.autoEnable=autoEnable;
  publishing.profilePreferences={};
  for(const key of ['follow_creator','description_style']){
    if(profileSession?.publish_profile?.[key]!=null)publishing.profilePreferences[key]=profileSession.publish_profile[key];
  }
  const requestId=preparePublishDialog(profileSession?'profile':'job');
  const accountTask=refreshPublishAccount(requestId);
  try{
    const [categoryData,preview]=await Promise.all([
      api('/api/publishing/categories'),
      profileSession?Promise.resolve({metadata:{...profileSession.publish_profile,source:profileSession.publish_profile?.source||profileSession.source_url||'',copyright:profileSession.publish_profile?.copyright||2}}):api('/api/jobs/'+encodeURIComponent(jobId)+'/publication-preview')
    ]);
    if(requestId!==publishing.requestId||!$('publishDialog').open)return;
    const categories=categoryData.categories||[],metadata=preview.metadata||{};
    if(!categories.length)throw new Error('暂时没有可用投稿分区，请重新加载。');
    $('publishCategory').innerHTML='<option value="">请选择分区</option>'+categories.map(c=>`<option value="${esc(c.id)}">${esc((c.parent?c.parent+' / ':'')+c.name)}</option>`).join('');
    $('publishTitle').value=metadata.title||'';$('publishDescription').value=metadata.description||'';$('publishTags').value=(metadata.tags||[]).join(', ');
    $('publishCategory').value=String(metadata.tid||'');$('publishCopyright').value=String(metadata.copyright||2);$('publishSource').value=metadata.source||'';
    $('publishCreator').value=metadata.creator_name||'';$('publishTitlePrefix').value=metadata.title_prefix||'';
    const warnings=(preview.warnings||[]).slice();
    if(metadata.tid&&!$('publishCategory').value)warnings.push('原先的分区当前不可用，请重新选择。');
    $('publishWarnings').innerHTML=warnings.map(w=>`<p>${esc(w)}</p>`).join('');$('publishWarnings').classList.toggle('hidden',!warnings.length);
    publishing.ready=true;$('publishFields').disabled=false;updatePublishSource();
  }catch(e){if(requestId===publishing.requestId){publishError(e.message);$('reloadPublishPreview').classList.remove('hidden');}}
  finally{if(requestId===publishing.requestId){$('publishLoading').classList.add('hidden');updatePublishSubmit();}}
  await accountTask;
}
function publishMetadata(){
  const title=$('publishTitle').value.trim(),description=$('publishDescription').value.trim();
  const tags=[...new Set($('publishTags').value.split(/[,，\n]+/).map(t=>t.trim()).filter(Boolean))];
  const tid=Number($('publishCategory').value),copyright=Number($('publishCopyright').value),source=copyright===2?$('publishSource').value.trim():'';
  if(publishing.mode==='job'&&(!title||[...title].length>80))throw new Error('请填写 1–80 字的投稿标题。');
  if([...description].length>2000)throw new Error('简介最多 2000 字。');
  if((publishing.mode==='job'&&!tags.length)||tags.length>10||tags.some(t=>[...t].length>20))throw new Error('请填写最多 10 个标签，每个标签最多 20 字。');
  if(!Number.isInteger(tid)||tid<=0)throw new Error('请选择投稿分区。');
  if(copyright===2){try{const url=new URL(source);if(!['http:','https:'].includes(url.protocol))throw new Error();}catch{throw new Error('转载内容请填写有效的原始来源 URL。');}}
  return {title,description,tags,tid,copyright,source};
}
function storePublication(publication){
  if(!publication?.id)return;
  app.state.publications ||= [];const index=app.state.publications.findIndex(p=>p.id===publication.id);
  if(index<0)app.state.publications.push(publication);else app.state.publications[index]=publication;renderJobs();
}
async function submitPublication(event){
  event.preventDefault();if(publishing.busy||!publishing.ready)return;
  if(publishing.mode==='job'&&publicationBlocksRetry(publishing.jobId)){publishError('此成片已经发起投稿，请关闭窗口查看结果。');updatePublishSubmit();return;}
  try{
    const metadata=publishMetadata();publishing.busy=true;publishError('');updatePublishSubmit();$('publishFields').disabled=true;
    if(publishing.mode==='profile'){
      const {title,...profile}=metadata;profile.creator_name=$('publishCreator').value.trim();profile.title_prefix=$('publishTitlePrefix').value.trim();
      Object.assign(profile,publishing.profilePreferences);
      const patch={publish_profile:profile,auto_publish:publishing.autoEnable};
      const result=await api('/api/sessions/'+encodeURIComponent(publishing.sessionId),'PATCH',patch);
      applyPublishingSettings(publishing.sessionId,result.session||result,patch);
      $('publishDialog').close();toast(publishing.autoEnable?'自动投稿已开启，后续成片将提交 B 站审核':'本素材投稿设置已保存','success');
    }else{
      const result=await api('/api/jobs/'+encodeURIComponent(publishing.jobId)+'/publish','POST',metadata);
      storePublication(result.publication||result);$('publishDialog').close();toast('已加入投稿队列，可在成片下查看审核进度','success');await refreshState();
    }
  }catch(e){publishError(e.message);}
  finally{publishing.busy=false;$('publishFields').disabled=!publishing.ready;updatePublishSubmit();}
}
async function refreshPublication(id){
  if(publishing.refreshing.has(id))return;publishing.refreshing.add(id);renderJobs();
  try{const result=await api('/api/publications/'+encodeURIComponent(id)+'/refresh','POST',{});storePublication(result.publication||result);}
  catch(e){toast(e.message,'error');}
  finally{publishing.refreshing.delete(id);renderJobs();}
}
function renderPublishingSettings(){
  const s=session(),busy=publishing.settingsBusy.has(app.sid);
  $('autoPublishInput').disabled=!s||busy;$('publishSettingsButton').disabled=!s||busy;
  if(!busy)$('autoPublishInput').checked=!!s?.auto_publish;
  $('publishSettingsSummary').textContent=s?.auto_publish?'已开启：后续成片使用本素材投稿设置，直接提交 B 站审核。':'使用当前素材的投稿设置；开启前请填写分区和素材来源。';
}
function applyPublishingSettings(id,result,patch){
  const s=app.state.sessions.find(s=>s.id===id);if(s)Object.assign(s,patch,result.id?result:{});
  if(app.detail?.session?.id===id)Object.assign(app.detail.session,patch,result.id?result:{});
  renderPublishingSettings();
}
async function toggleAutoPublish(){
  const s=session();if(!s)return;const enabled=$('autoPublishInput').checked,profile=s.publish_profile||{};
  if(enabled&&(!profile.tid||![1,2].includes(profile.copyright)||(profile.copyright===2&&!profile.source&&!s.source_url))){
    $('autoPublishInput').checked=false;await loadPublishDialog(null,s,true);return;
  }
  publishing.settingsBusy.add(s.id);renderPublishingSettings();
  try{const patch={auto_publish:enabled},result=await api('/api/sessions/'+encodeURIComponent(s.id),'PATCH',patch);applyPublishingSettings(s.id,result.session||result,patch);toast(enabled?'自动投稿已开启':'自动投稿已关闭','success');}
  catch(e){toast(e.message,'error');}
  finally{publishing.settingsBusy.delete(s.id);renderPublishingSettings();}
}
async function saveAnchor(showToast=true){if(!app.aid)throw new Error('请先选择一个锚点');const start=parseTime($('startInput').value),end=parseTime($('endInput').value);if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start)throw new Error('请填写有效范围，终点需要晚于起点');if(end>duration()+.25)throw new Error('片段终点超过素材时长');if(end-start>600)throw new Error('一条切片最长 10 分钟，请缩短片段范围');const title=$('titleInput').value.trim();if(!title)throw new Error('请给切片填写一个标题');if(title.length>120)throw new Error('切片标题最多 120 个字');const data={title,start,end,cover_text:$('coverInput').value.trim(),style:$('styleInput').value,focus_event_ids:[...app.selectedEvents],focus_region:app.region};await api('/api/anchors/'+encodeURIComponent(app.aid),'PATCH',data);const a=anchor();if(a)Object.assign(a,data);clearDirty();renderAnchors();if(showToast)toast('片段设置已保存','success');return data;}
async function manualAnchor(){if(!app.sid)return;const timestamp=currentTime(),total=duration();const start=Math.max(0,timestamp-15),end=Math.min(total,Math.max(start+1,timestamp+25));try{const r=await api('/api/anchors','POST',{session_id:app.sid,timestamp,start,end,title:'刚才这个瞬间',style:session()?.style||'reaction'});await refreshDetail(true);const a=r.anchor||r;if(a.id)selectAnchor(a.id,false);toast('已记下这个瞬间，调整范围就能制作','success');}catch(e){toast(e.message,'error');}}
async function updateSession(patch){if(!app.sid)return;try{await api('/api/sessions/'+encodeURIComponent(app.sid),'PATCH',patch);Object.assign(app.detail.session,patch);toast('自动切片设置已更新','success');}catch(e){toast(e.message,'error');renderSessionInfo();}}
function openSource(mode='live'){if(app.sourceBusy)return;setSource(mode);$('sourceError').classList.add('hidden');$('sourceDialog').showModal();}
function setSource(mode){app.source=mode;document.querySelectorAll('[data-source]').forEach(b=>b.classList.toggle('selected',b.dataset.source===mode));['live','file','path'].forEach(n=>$('source'+n[0].toUpperCase()+n.slice(1)).classList.toggle('hidden',n!==mode));$('sourceSubmit').innerHTML=({live:'开始接入',file:'上传并导入',path:'导入录像'})[mode]+icon('arrow');}
function chooseFile(file){if(!file)return;app.selectedFile=file;$('uploadFilename').textContent=file.name;$('uploadProgress').classList.add('hidden');}
function uploadVideo(file){return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open('POST','/api/uploads?filename='+encodeURIComponent(file.name));xhr.setRequestHeader('Content-Type',file.type||'application/octet-stream');xhr.upload.onprogress=e=>{if(e.lengthComputable){const pct=Math.round(e.loaded/e.total*100);$('uploadProgress').classList.remove('hidden');$('uploadProgressBar').style.width=pct+'%';$('uploadProgressText').textContent=pct===100?'上传完成，正在准备素材…':`正在上传 ${pct}% · ${(e.loaded/1048576).toFixed(1)} / ${(e.total/1048576).toFixed(1)} MB`;}};xhr.onload=()=>{let d;try{d=JSON.parse(xhr.responseText);}catch{d={detail:'服务器返回了无法解析的响应'};}if(xhr.status>=200&&xhr.status<300)resolve(d);else reject(new Error(typeof d.detail==='string'?d.detail:'上传失败，请检查视频文件'));};xhr.onerror=()=>reject(new Error('上传连接中断，请重试'));xhr.send(file);});}
async function submitSource(e){e.preventDefault();if(app.sourceBusy)return;let task;try{if(app.source==='live'){const room=$('roomInput').value.trim();if(!room)throw new Error('请输入 B 站直播间链接或房间号');if(/douyin|tiktok/i.test(room))throw new Error('这里接入 B 站直播，请输入 B 站直播间');task=()=>api('/api/sessions/live','POST',{room,auto_render:$('liveAutoInput').checked,minimum_score:88,style:'reaction'});}else if(app.source==='path'){const path=$('pathInput').value.trim();if(!path)throw new Error('请填写服务器上的视频路径');task=()=>api('/api/sessions/import','POST',{path,title:$('importTitle').value.trim()||undefined,events_path:$('eventsPathInput').value.trim()||undefined});}else{const file=app.selectedFile||$('videoFileInput').files[0];if(!file)throw new Error('请先选择一个视频文件');if(file.size>2*1024**3)throw new Error('网页上传上限为 2 GB，更大的录像请通过服务器文件路径导入');task=()=>uploadVideo(file);}app.sourceBusy=true;$('sourceSubmit').disabled=true;$('sourceSubmit').textContent=app.source==='file'?'正在上传…':'正在准备…';$('sourceError').classList.add('hidden');const r=await task();const s=r.session||r;$('sourceDialog').close();await refreshState();if(s.id)await selectSession(s.id);toast(app.source==='live'?'直播任务已创建，开始寻找亮点':'录像已导入，可以分析亮点了','success');}catch(error){$('sourceError').textContent=error.message;$('sourceError').classList.remove('hidden');}finally{app.sourceBusy=false;$('sourceSubmit').disabled=false;setSource(app.source);}}
function renderReferences(){const rr=app.state.references||[];$('referenceList').innerHTML=rr.length?rr.map((r,i)=>`<a class="reference-item" href="${esc(r.url||'https://space.bilibili.com/'+r.uid)}" target="_blank" rel="noopener"><span class="reference-avatar">${String(i+1).padStart(2,'0')}</span><div><strong>${esc(r.name||r.uid)}</strong><p>${typeof r.samples==='number'?r.samples+' 条投稿参考':Array.isArray(r.samples)?r.samples.length+' 条投稿参考':'标题 · 封面 · 剪辑表达'}</p></div>${icon('external')}</a>`).join(''):'<div class="empty-list">参考档案正在整理，稍后可在研究记录中查看。</div>';}
function relativePoint(e,el){const rect=el.getBoundingClientRect();return{x:Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width)),y:Math.max(0,Math.min(1,(e.clientY-rect.top)/rect.height))};}
function installRangeDrag(id,side){$(id).addEventListener('pointerdown',e=>{e.preventDefault();e.stopPropagation();if(!anchor())return;app.dragging=true;$(id).setPointerCapture(e.pointerId);const move=ev=>{const point=relativePoint(ev,$('timeline'));const seconds=point.x*duration();const start=parseTime($('startInput').value),end=parseTime($('endInput').value);if(side==='start')$('startInput').value=time(Math.max(0,Math.min(seconds,end-.5)),true);else $('endInput').value=time(Math.min(duration(),Math.max(seconds,start+.5)),true);setDirty();};const finish=ev=>{app.dragging=false;$(id).removeEventListener('pointermove',move);$(id).removeEventListener('pointerup',finish);$(id).removeEventListener('pointercancel',finish);renderEvents();};$(id).addEventListener('pointermove',move);$(id).addEventListener('pointerup',finish);$(id).addEventListener('pointercancel',finish);});}
installRangeDrag('startHandle','start');installRangeDrag('endHandle','end');
$('timeline').addEventListener('click',e=>{if(e.target.closest('.range-handle')||app.dragging)return;const marker=e.target.closest('[data-anchor]');if(marker){selectAnchor(marker.dataset.anchor);return;}seek(relativePoint(e,$('timeline')).x*duration());});
$('sessionList').addEventListener('click',e=>{const b=e.target.closest('[data-session]');if(b)selectSession(b.dataset.session);});
$('anchorList').addEventListener('click',e=>{const b=e.target.closest('[data-anchor]');if(b)selectAnchor(b.dataset.anchor);});
$('titleVariants').addEventListener('click',e=>{const b=e.target.closest('[data-title]');if(b){$('titleInput').value=b.dataset.title;setDirty();}});
$('eventList').addEventListener('change',e=>{const el=e.target;if(!el.dataset.event)return;if(el.checked&&app.selectedEvents.size>=4){el.checked=false;toast('每段最多强调 4 条弹幕，留些空间给主播反应');return;}if(el.checked)app.selectedEvents.add(el.dataset.event);else app.selectedEvents.delete(el.dataset.event);el.closest('.event-card').classList.toggle('selected',el.checked);$('eventSelectionCount').textContent=app.selectedEvents.size+' 条';setDirty();});
['titleInput','coverInput','styleInput','startInput','endInput'].forEach(id=>$(id).addEventListener('input',setDirty));['startInput','endInput'].forEach(id=>$(id).addEventListener('change',()=>{renderEvents();renderRange();}));
$('aspectSelector').addEventListener('click',e=>{const b=e.target.closest('[data-aspect]');if(!b)return;app.aspect=b.dataset.aspect;document.querySelectorAll('[data-aspect]').forEach(el=>el.classList.toggle('selected',el===b));});
$('saveAnchor').addEventListener('click',async()=>{try{await saveAnchor();}catch(e){toast(e.message,'error');}});
$('renderButton').addEventListener('click',async()=>{if(!app.aid)return;const button=$('renderButton');button.disabled=true;button.innerHTML=icon('scissors')+'正在提交…';try{await saveAnchor(false);await api('/api/anchors/'+encodeURIComponent(app.aid)+'/render','POST',{aspect:app.aspect,resolution:Number($('resolutionInput').value),cover_mode:$('coverModeInput').value,captions:$('captionsCheck').checked,emphasis:$('emphasisCheck').checked,cover:$('coverCheck').checked,focus_region:app.region});await refreshDetail();toast('已加入制作队列，完成后可下载视频与封面','success');}catch(e){toast(e.message,'error');}finally{button.disabled=false;button.innerHTML=icon('scissors')+'制作切片';}});
$('analyzeButton').addEventListener('click',async()=>{if(!app.sid)return;try{$('analyzeButton').disabled=true;await api('/api/sessions/'+encodeURIComponent(app.sid)+'/analyze','POST',{transcribe:true});toast('开始分析语音与观众反应','success');await refreshDetail();}catch(e){toast(e.message,'error');$('analyzeButton').disabled=false;}});
$('stopButton').addEventListener('click',async()=>{if(!app.sid)return;try{$('stopButton').disabled=true;await api('/api/sessions/'+encodeURIComponent(app.sid)+'/stop','POST',{});await refreshDetail();toast('直播录制已停止，已有素材和锚点会保留','success');}catch(e){toast(e.message,'error');}finally{$('stopButton').disabled=false;}});
$('autoRenderInput').addEventListener('change',()=>updateSession({auto_render:$('autoRenderInput').checked}));$('thresholdInput').addEventListener('input',()=>{$('thresholdValue').textContent=$('thresholdInput').value;});$('thresholdInput').addEventListener('change',()=>updateSession({minimum_score:Number($('thresholdInput').value)}));
$('jobList').addEventListener('click',event=>{
  const publishButton=event.target.closest('[data-publish-job]'),refreshButton=event.target.closest('[data-refresh-publication]');
  if(publishButton&&!publishButton.disabled)loadPublishDialog(publishButton.dataset.publishJob);
  if(refreshButton&&!refreshButton.disabled)refreshPublication(refreshButton.dataset.refreshPublication);
});
$('publishForm').addEventListener('submit',submitPublication);
$('publishCopyright').addEventListener('change',updatePublishSource);
$('refreshPublishAccount').addEventListener('click',()=>refreshPublishAccount());
$('reloadPublishPreview').addEventListener('click',()=>{
  if(publishing.mode==='profile'){const s=app.state.sessions.find(s=>s.id===publishing.sessionId);if(s)loadPublishDialog(null,s,publishing.autoEnable);}
  else loadPublishDialog(publishing.jobId);
});
$('publishDialog').addEventListener('cancel',event=>{if(publishing.busy)event.preventDefault();});
$('publishSettingsButton').addEventListener('click',()=>{const s=session();if(s)loadPublishDialog(null,s,!!s.auto_publish);});
$('autoPublishInput').addEventListener('change',toggleAutoPublish);
['markButton','addAnchor'].forEach(id=>$(id).addEventListener('click',manualAnchor));['openSource','addLibrary','emptyAdd'].forEach(id=>$(id).addEventListener('click',()=>openSource()));
['referenceNav','referenceButton'].forEach(id=>$(id).addEventListener('click',()=>{$('referenceDialog').showModal();}));
document.querySelectorAll('.close-dialog').forEach(el=>el.addEventListener('click',()=>{if(el.closest('#sourceDialog')&&app.sourceBusy){toast('素材正在上传，请等待完成');return;}if(el.closest('#publishDialog')&&publishing.busy){toast('正在提交，请等待请求完成');return;}el.closest('dialog').close();}));
$('sourceDialog').addEventListener('cancel',e=>{if(app.sourceBusy)e.preventDefault();});document.querySelectorAll('[data-source]').forEach(el=>el.addEventListener('click',()=>{if(!app.sourceBusy)setSource(el.dataset.source);}));document.querySelectorAll('[data-filter]').forEach(el=>el.addEventListener('click',()=>{app.filter=el.dataset.filter;document.querySelectorAll('[data-filter]').forEach(b=>b.classList.toggle('selected',b===el));renderLibrary();}));
$('sourceForm').addEventListener('submit',submitSource);$('videoFileInput').addEventListener('change',()=>chooseFile($('videoFileInput').files[0]));
$('importEvents').addEventListener('click',()=>$('eventsFileInput').click());$('eventsFileInput').addEventListener('change',async()=>{const file=$('eventsFileInput').files[0];if(!file||!app.sid)return;try{const text=await file.text();let parsed;if(file.name.endsWith('.jsonl'))parsed=text.split(/\r?\n/).filter(s=>s.trim()).map(s=>JSON.parse(s));else parsed=JSON.parse(text);const events=Array.isArray(parsed)?parsed:parsed.events;if(!Array.isArray(events)||!events.length)throw new Error('JSON 需要包含非空 events 数组，或直接是事件数组');await api('/api/sessions/'+encodeURIComponent(app.sid)+'/events','POST',{events});await refreshDetail();renderEvents();toast(`已导入 ${events.length} 条转写 / 弹幕`,'success');}catch(e){toast(e.message,'error');}finally{$('eventsFileInput').value='';}});
$('regionButton').addEventListener('click',()=>{if(!app.aid){toast('先选择或添加一个锚点，再框选放大区域');return;}app.regionMode=!app.regionMode;$('regionLayer').classList.toggle('hidden',!app.regionMode);$('regionButton').classList.toggle('secondary',app.regionMode);if(app.regionMode)player.pause();});
$('regionLayer').addEventListener('pointerdown',e=>{if(!app.regionMode||!app.aid)return;e.preventDefault();const layer=$('regionLayer'),start=relativePoint(e,layer);layer.setPointerCapture(e.pointerId);const box=$('regionBox');box.classList.remove('hidden');let bounds={x:start.x,y:start.y,w:0,h:0};const move=ev=>{const p=relativePoint(ev,layer);bounds={x:Math.min(p.x,start.x),y:Math.min(p.y,start.y),w:Math.abs(p.x-start.x),h:Math.abs(p.y-start.y)};Object.assign(box.style,{left:bounds.x*100+'%',top:bounds.y*100+'%',width:bounds.w*100+'%',height:bounds.h*100+'%'});};const end=ev=>{move(ev);layer.removeEventListener('pointermove',move);layer.removeEventListener('pointerup',end);layer.removeEventListener('pointercancel',end);if(bounds.w<.03||bounds.h<.03){toast('放大区域太小，请重新框选');return;}const clipStart=parseTime($('startInput').value),clipEnd=parseTime($('endInput').value),at=Math.max(clipStart,Math.min(currentTime(),clipEnd-1));app.region={...bounds,start:at,end:Math.min(clipEnd,at+5)};app.regionMode=false;layer.classList.add('hidden');$('regionButton').classList.remove('secondary');renderRegion();setDirty();toast('已添加 5 秒画面放大，可随片段一起导出','success');};layer.addEventListener('pointermove',move);layer.addEventListener('pointerup',end);layer.addEventListener('pointercancel',end);});
$('previewClipButton').addEventListener('click',async()=>{const start=parseTime($('startInput').value),end=parseTime($('endInput').value);if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start){toast('请先填写有效的片段范围','error');return;}seek(start);app.previewEnd=end;try{await player.play();}catch(e){app.previewEnd=null;toast('当前素材暂时无法播放，仍可保存和导出','error');}});
$('clearRegion').addEventListener('click',()=>{app.region=null;renderRegion();setDirty();});
function fitRegionLayer(){const stage=$('videoStage'),r=stage.getBoundingClientRect(),vw=player.videoWidth||16,vh=player.videoHeight||9;const scale=Math.min(r.width/vw,r.height/vh),w=vw*scale,h=vh*scale;Object.assign($('regionLayer').style,{inset:'auto',left:(r.width-w)/2+'px',top:(r.height-h)/2+'px',width:w+'px',height:h+'px'});}new ResizeObserver(fitRegionLayer).observe($('videoStage'));player.addEventListener('loadedmetadata',fitRegionLayer);
player.addEventListener('timeupdate',updatePlayhead);player.addEventListener('loadedmetadata',renderTimeline);player.addEventListener('error',()=>{if(app.lastMedia){$('previewLabel').textContent='预览加载失败，仍可编辑与导出';}});
document.addEventListener('keydown',e=>{if(e.key.toLowerCase()==='m'&&!e.ctrlKey&&!e.metaKey&&!e.altKey&&!e.target.closest('input,textarea,select,dialog')&&app.sid){e.preventDefault();manualAnchor();}});
let dragDepth=0;document.addEventListener('dragenter',e=>{if(e.dataTransfer?.types.includes('Files')){dragDepth++;document.body.classList.add('drag-over');}});document.addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('Files'))e.preventDefault();});document.addEventListener('dragleave',()=>{dragDepth=Math.max(0,dragDepth-1);if(!dragDepth)document.body.classList.remove('drag-over');});document.addEventListener('drop',e=>{e.preventDefault();dragDepth=0;document.body.classList.remove('drag-over');const file=e.dataTransfer?.files[0];if(file&&!app.sourceBusy){if(!$('sourceDialog').open)openSource('file');else setSource('file');chooseFile(file);}});
renderLibrary();renderAnchors();renderJobs();renderHealth();refreshState();setInterval(refreshState,3000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshState();});
