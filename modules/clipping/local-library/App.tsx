"use client";

/* eslint-disable @next/next/no-img-element */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowDownToLine, ArrowUpRight, Check, ChevronDown, Clapperboard, Clock3, Copy, Film, Heart, Link2, LoaderCircle, Play, Radio, RefreshCw, Search, Sparkles, WifiOff, X } from "lucide-react";

type Asset = { url: string; sha256: string; size: number };
type Streamer = { uid: string; name: string; room_id: string; profile_url: string; live_url: string };
type Clip = {
  id: string; title: string; streamer: Streamer; date: string; created_at: string;
  captured_at: string; duration: number; score: number; warning: string;
  review_required: boolean; status: string; tags: string[]; description: string;
  status_label?: string; status_detail?: string;
  review?: { status: string; label: string; source: string; reason_code?: string };
  source_url: string; publication: null | { status: string; bvid: string; url: string };
  subtitle_review?: { applied: boolean; aligned: boolean };
  files: Record<string, Asset>;
};
type Library = {
  version: number; generated_at: string; clips: Clip[];
  patrol: { status: string; current_streamer: Streamer | null; live_following_count: number; auto_render: boolean; auto_publish: boolean; send_danmaku: boolean; updated_at: string };
  counts: { clips: number; streamers: number; review_required: number; ready: number; published: number };
};

function safeUrl(value: string | undefined) {
  if (!value) return "";
  if (/^\.\.\/\.\.\/api\/exports\/[a-f0-9]{16}\/(clip\.mp4|cover\.jpg|subtitles\.srt)$/.test(value)) return value;
  if (value.startsWith("/") && !value.startsWith("//")) return value;
  try { const url = new URL(value); return url.protocol === "https:" ? url.href : ""; } catch { return ""; }
}
function durationText(seconds: number) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "--:--";
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}
function dateText(value: string) {
  if (!value || Number.isNaN(new Date(value).getTime())) return "时间未记录";
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}
function isPublished(clip: Clip) { return clip.publication?.status === "published"; }
function isSubmitted(clip: Clip) { return ["published", "submitted", "transcoding", "reviewing", "private", "rejected"].includes(clip.publication?.status ?? ""); }
function isUncertain(clip: Clip) { return clip.status === "publication_uncertain" || clip.publication?.status === "uncertain"; }
function isReviewBlocked(clip: Clip) { return !isPublished(clip) && ["held", "rejected"].includes(clip.status); }
function statusText(clip: Clip) { return clip.status_label || (isPublished(clip) ? "已公开" : clip.review_required ? "审核状态待更新" : "待投稿"); }
function statusClass(clip: Clip) { return isPublished(clip) ? "published" : ["held", "publication_paused", "publication_failed", "publication_uncertain", "rejected"].includes(clip.status) ? "attention" : isSubmitted(clip) || clip.status === "ready" ? "ready" : ""; }
function streamerKey(clip: Clip) { return clip.streamer.uid || clip.streamer.name; }
function assetUrl(clip: Clip, filename: string) { return safeUrl(clip.files?.[filename]?.url); }

const STATUS_FILTERS: { id: string; label: string; matches: (clip: Clip) => boolean; always?: boolean }[] = [
  { id: "all", label: "全部切片", matches: () => true, always: true },
  { id: "blocked", label: "未通过", matches: isReviewBlocked, always: true },
  { id: "published", label: "已投稿", matches: isSubmitted, always: true },
  { id: "pending", label: "未投稿", matches: (clip) => !isSubmitted(clip) && !isUncertain(clip), always: true },
  { id: "ready", label: "通过待投稿", matches: (clip) => clip.status === "ready", always: true },
  { id: "not_reviewed", label: "未审稿", matches: (clip) => ["pending_review", "not_reviewed"].includes(clip.status), always: true },
  { id: "reviewing", label: "审稿中", matches: (clip) => clip.status === "reviewing" },
  { id: "short", label: "时长不足", matches: (clip) => clip.status === "below_minimum_duration", always: true },
  { id: "paused", label: "批次暂停", matches: (clip) => clip.status === "publication_paused", always: true },
  { id: "publishing", label: "投稿中", matches: (clip) => clip.status === "publishing" },
  { id: "platform_review", label: "平台审核", matches: (clip) => clip.status === "submitted" },
  { id: "public", label: "已公开", matches: isPublished },
  { id: "private", label: "仅自己可见", matches: (clip) => clip.status === "private" },
  { id: "failed", label: "投稿失败", matches: (clip) => clip.status === "publication_failed" },
  { id: "uncertain", label: "结果待确认", matches: isUncertain },
];

function ReviewBadge({ clip }: { clip: Clip }) {
  return isReviewBlocked(clip) ? <span className="review-failed-badge" title={clip.status_detail || statusText(clip)}><X size={13} strokeWidth={2.8} aria-hidden="true" />未通过</span> : null;
}

function Cover({ clip }: { clip: Clip }) {
  const [failed, setFailed] = useState(false);
  return assetUrl(clip, "cover.jpg") && !failed ? (
    <img src={assetUrl(clip, "cover.jpg")} alt="" loading="lazy" onError={() => setFailed(true)} />
  ) : <div className="cover-fallback"><Film size={30} strokeWidth={1.4} /><span>封面暂不可用</span></div>;
}

function ClipPreview({ clip, autoPlay }: { clip: Clip; autoPlay: boolean }) {
  const [videoError, setVideoError] = useState(false);
  const [copyMessage, setCopyMessage] = useState("");
  const previewPath = `index.html?clip=${encodeURIComponent(clip.id)}`;
  const copyLink = async () => {
    try {
      await navigator.clipboard.writeText(new URL(previewPath, window.location.href).href);
      setCopyMessage("预览链接已复制");
    } catch {
      setCopyMessage("请右键或长按下方预览链接复制");
    }
  };
  const downloads = [
    { filename: "clip.mp4", label: "MP4 视频", hint: "视频文件" },
    { filename: "subtitles.srt", label: "SRT 字幕", hint: "字幕文件" },
    { filename: "cover.jpg", label: "封面图片", hint: "JPG 图片" },
    { filename: "package.zip", label: "完整素材包", hint: "ZIP 压缩包" },
  ].filter((file) => assetUrl(clip, file.filename));
  return (
    <div className="inline-preview">
        <div className="dialog-topline"><span><Clapperboard size={17} /> 网页预览</span><span className="preview-hint">字幕已嵌入画面 · 支持全屏</span></div>
        <div className="dialog-content">
          <div className="dialog-main">
            <div className="video-frame">{assetUrl(clip, "clip.mp4") ? <video src={assetUrl(clip, "clip.mp4")} aria-label={`切片播放器：${clip.title}`} controls playsInline autoPlay={autoPlay} preload="metadata" poster={assetUrl(clip, "cover.jpg") || undefined} onError={() => setVideoError(true)} onCanPlay={() => setVideoError(false)}>浏览器暂不支持视频播放，请下载 MP4 查看。</video> : <p>这条切片的视频暂不可用。</p>}</div>
            {videoError && <p className="video-error" role="alert">视频暂时加载失败，可刷新页面重试，或下载 MP4 查看。</p>}
            <div className="dialog-title-row"><ReviewBadge clip={clip} /><span className={`status-pill ${statusClass(clip)}`}>{isPublished(clip) ? <Check size={12} /> : <span className="tiny-dot" />}{statusText(clip)}</span><span className="muted">{durationText(clip.duration)}</span></div>
            <h2 id="preview-title">{clip.title}</h2>
            <div className="dialog-author"><span className="avatar">{clip.streamer.name.slice(0, 1)}</span><strong>{clip.streamer.name}</strong><span className="meta-separator">·</span><span>{dateText(clip.created_at)} 生成</span></div>
            <p className="subtitle-review-note">{clip.subtitle_review?.aligned && clip.subtitle_review.applied ? "字幕已完成整段复听与时间对齐。" : "字幕尚未完成整段复听，当前使用直播分段识别结果。"}{clip.review?.label && <><br />内容审核：{clip.review.label}。</>}</p>
            {(clip.status_detail || clip.warning) && <div className={isPublished(clip) || clip.status === "ready" ? "clip-status-note" : "clip-warning"}><span className="tiny-dot" /><p>{clip.status_detail || clip.warning}</p></div>}
            {clip.description && <div className="clip-description"><h3>稿件简介</h3><p>{clip.description}</p></div>}
            {clip.tags?.length > 0 && <div className="tag-list" aria-label="稿件标签">{clip.tags.map((tag) => <span key={tag}># {tag}</span>)}</div>}
          </div>
          <aside className="dialog-sidebar">
            <h3>就在网页里看</h3><p>点击播放，也可以拖动进度条查看后面的内容。</p>
            <button className="primary-button copy-preview" onClick={() => void copyLink()}><Copy size={15} />复制预览链接</button>
            <a className="preview-permalink" href={previewPath}><Link2 size={14} />这条切片的预览页</a>
            <span className="copy-message" role="status">{copyMessage}</span>
            <h3 className="download-heading">下载切片</h3>
            <div className="download-list">{downloads.map((file) => <a href={`${assetUrl(clip, file.filename)}?download=1`} key={file.filename} download={file.filename}><span>{file.label}</span><ArrowDownToLine size={17} /></a>)}</div>
            {downloads.length === 0 && <p className="muted">暂无可下载文件</p>}
            <div className="source-links">
              {safeUrl(clip.source_url || clip.streamer.live_url) && <a href={safeUrl(clip.source_url || clip.streamer.live_url)} target="_blank" rel="noopener noreferrer"><Radio size={16} /> 前往主播直播间 <ArrowUpRight size={15} /></a>}
              {safeUrl(clip.publication?.url) && <a href={safeUrl(clip.publication?.url)} target="_blank" rel="noopener noreferrer"><Play size={16} /> 查看 B 站稿件 <ArrowUpRight size={15} /></a>}
            </div>
            <div className="review-note"><Heart size={16} /><p>字幕复听与内容审核分别展示。<br /><span>发现具体问题时，可返回工作台核对片段。</span></p></div>
          </aside>
        </div>
    </div>
  );
}

export default function Home() {
  const [library, setLibrary] = useState<Library | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [online, setOnline] = useState(true);
  const [query, setQuery] = useState("");
  const [streamer, setStreamer] = useState("all");
  const [tab, setTab] = useState("all");
  const [sort, setSort] = useState("newest");
  const [selected, setSelected] = useState<Clip | null>(null);
  const [previewId, setPreviewId] = useState<string | null>(null);
  const [autoPlay, setAutoPlay] = useState(false);
  const previewRef = useRef<HTMLElement>(null);
  const requestRef = useRef(false);
  const controllerRef = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    if (requestRef.current) return;
    requestRef.current = true;
    setRefreshing(true);
    const controller = new AbortController();
    controllerRef.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch("./library.json", { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error("暂时读取不到本地切片库");
      const data = await response.json() as Partial<Library>;
      if (controllerRef.current !== controller) return;
      if (!Array.isArray(data.clips) || !data.patrol || !data.counts) throw new Error("切片列表暂时无法读取");
      setLibrary(data as Library);
      const age = Date.now() - Date.parse(data.generated_at || "");
      setError(!Number.isFinite(age) || age > 120000 ? "列表更新暂时中断，当前显示上次保存的切片" : "");
      setOnline(true);
    } catch (err) {
      if (controllerRef.current !== controller) return;
      setError(err instanceof Error && err.name !== "AbortError" ? err.message : "连接超时，请稍后重试");
    } finally {
      window.clearTimeout(timeout);
      if (controllerRef.current === controller) {
        requestRef.current = false;
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => { if (document.visibilityState === "visible") void refresh(); }, 30000);
    const onOnline = () => { setOnline(true); void refresh(); };
    const onOffline = () => setOnline(false);
    const onVisible = () => { if (document.visibilityState === "visible") void refresh(); };
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(interval);
      controllerRef.current?.abort();
      controllerRef.current = null;
      requestRef.current = false;
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh]);

  const clips = useMemo(() => library?.clips ?? [], [library]);
  useEffect(() => {
    const readLocation = () => {
      setPreviewId(new URL(window.location.href).searchParams.get("clip") || "");
      setSelected(null);
      setAutoPlay(false);
    };
    readLocation();
    window.addEventListener("popstate", readLocation);
    return () => window.removeEventListener("popstate", readLocation);
  }, []);
  useEffect(() => {
    if (previewId === null || selected || !clips.length) return;
    const clip = previewId ? clips.find((item) => item.id === previewId) : clips[0];
    if (clip) setSelected(clip);
  }, [clips, previewId, selected]);
  const missingPreview = !loading && !error && Boolean(previewId) && !selected && !clips.some((clip) => clip.id === previewId);
  const selectPreview = (clip: Clip) => {
    setSelected(clip);
    setPreviewId(clip.id);
    setAutoPlay(true);
    const url = new URL(window.location.href);
    if (url.searchParams.get("clip") !== clip.id) {
      url.searchParams.set("clip", clip.id);
      window.history.pushState(null, "", url);
    }
    window.requestAnimationFrame(() => {
      previewRef.current?.focus({ preventScroll: true });
      previewRef.current?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block: "start" });
    });
  };
  const streamers = useMemo(() => {
    const map = new Map<string, { name: string; count: number }>();
    for (const clip of clips) { const key = streamerKey(clip); map.set(key, { name: clip.streamer.name, count: (map.get(key)?.count ?? 0) + 1 }); }
    return [...map.entries()].sort((a, b) => b[1].count - a[1].count);
  }, [clips]);
  const pending = clips.filter((clip) => !isSubmitted(clip) && !isUncertain(clip)).length;
  const scopedClips = useMemo(() => clips.filter((clip) => {
    if (streamer !== "all" && streamerKey(clip) !== streamer) return false;
    if (query && !`${clip.title} ${clip.streamer.name} ${(clip.tags || []).join(" ")}`.toLowerCase().includes(query.trim().toLowerCase())) return false;
    return true;
  }), [clips, streamer, query]);
  const statusFilters = useMemo(() => STATUS_FILTERS.map((item) => ({ ...item, count: scopedClips.filter(item.matches).length }))
    .filter((item) => item.always || item.count > 0 || item.id === tab), [scopedClips, tab]);
  const filtered = useMemo(() => {
    const matches = STATUS_FILTERS.find((item) => item.id === tab)?.matches || (() => true);
    return scopedClips.filter(matches).sort((a, b) => sort === "longest" ? b.duration - a.duration : (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0));
  }, [scopedClips, tab, sort]);
  const patrolActive = ["running", "recording", "active", "capturing", "monitoring", "watching"].includes(library?.patrol.status ?? "");
  const patrolTitle = patrolActive ? "小担正在巡航" : library?.patrol.status === "stopped" ? "巡航已暂停" : library ? "巡航状态待更新" : "正在连接切片库";
  const disconnected = !online || Boolean(error);
  const resetFilters = () => { setQuery(""); setStreamer("all"); setTab("all"); };

  return (
    <div className="site-shell">
      <header className="site-header">
        <a className="brand" href="./index.html" aria-label="小担切片库首页"><span className="brand-mark"><img src="./xiaodan.png" alt="" width={44} height={44} /><span /></span><span>小担<span className="brand-light">切片库</span></span></a>
        <div className="header-middle"><span className="nav-active">我的切片</span><span className="header-divider" /><a className="private-label" href="../../">返回剪辑工作台</a></div>
        <div className={`sync-status ${disconnected ? "sync-warning" : ""}`}><span className="sync-dot" /><span>{disconnected ? "等待重新连接" : "每 30 秒自动更新"}</span></div>
      </header>
      <main className="main-container">
        <section className="hero" aria-labelledby="page-title">
          <div className="hero-copy"><div className="eyebrow"><span /> XIAODAN&apos;S CLIP ROOM</div><h1 id="page-title">今天的好戏，<br className="mobile-break" /><span>都在这里。</span><Sparkles className="title-sparkle" size={30} strokeWidth={1.5} /></h1><p>小担逛直播间，把值得回看的瞬间留给你。</p><div className="hero-stats"><span><strong>{loading ? "—" : clips.length}</strong> 条精彩切片</span><i /><span><strong>{loading ? "—" : streamers.length}</strong> 个来源</span><i /><span><strong>{loading ? "—" : pending}</strong> 条未投稿</span></div></div>
          <aside className="patrol-card"><div className="patrol-heading"><span className={`patrol-icon ${patrolActive ? "active" : ""}`}><Radio size={20} /></span><span className="patrol-status-label">{patrolActive ? "LIVE PATROL" : "PATROL STATUS"}</span><span className={`patrol-dot ${patrolActive ? "active" : ""}`} /></div><h2>{patrolTitle}</h2><p>{patrolActive && library?.patrol.current_streamer?.name ? `正在蹲守 ${library.patrol.current_streamer.name}` : patrolActive ? "在关注的直播间寻找有趣瞬间" : library?.patrol.status === "stopped" ? "已产生的切片仍可播放与下载" : "新切片会自动出现在这里"}</p><div className="patrol-footer"><span className="tiny-bars"><i /><i /><i /><i /><i /></span><span>{typeof library?.patrol.live_following_count === "number" ? `${library.patrol.live_following_count} 个关注直播间正在开播` : "有趣的瞬间，不想错过"}</span></div></aside>
        </section>
        {disconnected && <div className="connection-notice" role="status"><WifiOff size={17} /><span>{!online ? "网络连接已断开。" : `${error}。`}{library ? "你仍可查看上次同步的切片。" : "连接恢复后将自动更新。"}</span><button onClick={() => void refresh()} disabled={refreshing}>重新连接</button></div>}
        {(selected || missingPreview) && <section ref={previewRef} id="clip-preview" className="preview-section" aria-label="切片网页预览" tabIndex={-1}>
          {selected ? <ClipPreview key={selected.id} clip={clips.find((clip) => clip.id === selected.id) || selected} autoPlay={autoPlay} /> : <div className="preview-missing" role="status"><Film size={24} /><div><h2>这条切片暂时不在切片库中</h2><p>可能尚未同步或已替换，请在下方选择其他切片预览。</p></div></div>}
        </section>}
        <section className="library-section" aria-labelledby="library-title">
          <div className="library-heading"><div><h2 id="library-title">切片库 <span>{loading ? "—" : clips.length}</span></h2><p>每一个瞬间，都有点意思。</p></div><div className="last-sync"><Clock3 size={13} /><span>{library?.generated_at ? `最近同步 ${dateText(library.generated_at)}` : "等待首次同步"}</span><button className={`icon-button refresh-button ${refreshing ? "spinning" : ""}`} aria-label="刷新切片库" title="刷新切片库" disabled={refreshing} onClick={() => void refresh()}><RefreshCw size={15} /></button></div></div>
          <div className="library-toolbar"><div className="status-tabs" aria-label="按稿件状态筛选">{statusFilters.map((item) => <button type="button" key={item.id} data-status-filter={item.id} className={`${tab === item.id ? "selected" : ""} ${item.id === "blocked" ? "blocked-filter" : ""}`} onClick={() => setTab(item.id)} aria-pressed={tab === item.id}>{item.label}<span>{item.count}</span></button>)}</div><div className="search-sort"><label className="search-field"><Search size={17} /><input aria-label="搜索标题或来源" placeholder="搜索标题、来源…" value={query} onChange={(event) => setQuery(event.target.value)} />{query && <button aria-label="清空搜索" onClick={() => setQuery("")}><X size={14} /></button>}</label><label className="sort-field"><span className="sr-only">切片排序</span><select value={sort} onChange={(event) => setSort(event.target.value)}><option value="newest">最新生成</option><option value="longest">时长优先</option></select><ChevronDown size={14} /></label></div></div>
          {streamers.length > 0 && <div className="streamer-filters"><span className="filter-caption">来源</span><button aria-pressed={streamer === "all"} className={streamer === "all" ? "selected" : ""} onClick={() => setStreamer("all")}>全部来源</button>{streamers.map(([key, value], index) => <button key={key} aria-pressed={streamer === key} className={streamer === key ? "selected" : ""} onClick={() => setStreamer(key)}><span className={`streamer-dot hue-${index % 4}`} />{value.name}<span className="streamer-count">{value.count}</span></button>)}</div>}
          {loading ? <div className="clips-grid" aria-label="正在加载切片" aria-busy="true">{[0, 1, 2].map((index) => <div className="skeleton-card" key={index}><div /><span /><span /><span /></div>)}</div> : filtered.length > 0 ? <div className="clips-grid">{filtered.map((clip) => <article className={`clip-card ${selected?.id === clip.id ? "preview-selected" : ""}`} key={clip.id}><button className="cover-button" onClick={() => selectPreview(clip)} aria-label={`网页预览：${clip.title}`} aria-pressed={selected?.id === clip.id}><Cover clip={clip} /><ReviewBadge clip={clip} /><span className={`status-pill ${statusClass(clip)}`}>{isPublished(clip) ? <Check size={12} /> : <span className="tiny-dot" />}{statusText(clip)}</span><span className="cover-play"><Play size={24} fill="currentColor" /></span><span className="duration">{durationText(clip.duration)}</span></button><div className="card-content"><button className="card-title" onClick={() => selectPreview(clip)}>{clip.title}</button>{clip.status_detail && !isSubmitted(clip) && <p className="card-status-detail">{clip.status_detail}</p>}<div className="card-author"><span className={`avatar hue-${Math.max(0, streamers.findIndex(([key]) => key === streamerKey(clip))) % 4}`}>{clip.streamer.name.slice(0, 1)}</span><span>{clip.streamer.name}</span><span className="streamer-series">切片系列</span></div><div className="card-footer"><time dateTime={clip.created_at}>{dateText(clip.created_at)} 生成</time><button onClick={() => selectPreview(clip)} aria-label={`预览切片：${clip.title}`}>{selected?.id === clip.id ? "正在预览" : "网页预览"} <Play size={14} /></button></div></div></article>)}</div> : <div className="empty-state"><span className="empty-icon">{error && !library ? <WifiOff size={32} /> : clips.length ? <Search size={32} /> : <Clapperboard size={32} />}</span><h3>{error && !library ? "暂时没能连接上" : clips.length ? "没有找到这段精彩" : "好戏正在路上"}</h3><p>{error && !library ? "稍后会自动重试，也可以现在刷新。" : clips.length ? "换个关键词，或看看其他主播的切片。" : "小担产生新的切片后，会自动收进这里。"}</p>{clips.length > 0 ? <button className="primary-button" onClick={resetFilters}>查看全部切片</button> : error ? <button className="primary-button" disabled={refreshing} onClick={() => void refresh()}>{refreshing ? <LoaderCircle className="spin" size={16} /> : <RefreshCw size={16} />}重新加载</button> : <span className="waiting-label"><span className="sync-dot" /> 等待新切片</span>}</div>}
          {!loading && filtered.length > 0 && <div className="list-end"><span /><p>已展示 {filtered.length} 条切片 · 下一段精彩，正在发生</p><span /></div>}
        </section>
      </main>
      <footer className="site-footer"><span className="footer-brand"><Clapperboard size={16} /> 小担，认真做一个管人痴。</span><span>时间均为北京时间 <span className="footer-dot">·</span> 新切片自动收录</span></footer>
    </div>
  );
}
