"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const list = value => Array.isArray(value) ? value : [];
  const text = value => value == null ? "" : String(value);
  const number = value => Number.isFinite(Number(value)) && value !== null && value !== "" ? Number(value) : 0;
  const count = value => new Intl.NumberFormat("zh-CN").format(number(value));
  const el = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = text(content);
    return node;
  };
  const add = (node, ...children) => { node.append(...children.filter(Boolean)); return node; };
  const badge = (label, tone = "") => el("span", `tag ${tone}`, label);
  const button = (label, className, action) => {
    const node = el("button", className, label);
    node.type = "button";
    if (action) node.addEventListener("click", action);
    return node;
  };
  const safeHref = value => {
    if (!value || typeof value !== "string") return null;
    try {
      const url = new URL(value, location.href);
      if (!["http:", "https:", "file:"].includes(url.protocol) || url.origin !== location.origin) return null;
      return url.href;
    } catch { return null; }
  };
  const download = (label, value) => {
    const href = safeHref(value);
    if (!href) return null;
    const node = el("a", "download-link", label);
    node.href = href;
    node.download = "";
    return node;
  };
  const instant = value => {
    if (value === null || value === undefined || value === "") return null;
    const date = typeof value === "number" ? new Date(value > 1e11 ? value : value * 1000) : new Date(value);
    return Number.isFinite(date.getTime()) ? date : null;
  };
  const when = (value, short = false) => {
    const date = instant(value);
    if (!date) return "未记录";
    return new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", hour12: false,
      ...(short ? {} : {month: "2-digit", day: "2-digit"}), hour: "2-digit", minute: "2-digit", second: "2-digit"}).format(date);
  };
  const duration = value => {
    const seconds = Math.max(0, Math.floor(number(value)));
    return seconds >= 3600 ? `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`
      : `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  };
  const offset = value => {
    if (value === null || value === undefined) return "未记录";
    const seconds = Math.max(0, Math.floor(number(value)));
    return `${Math.floor(seconds / 3600).toString().padStart(2, "0")}:${Math.floor(seconds / 60 % 60).toString().padStart(2, "0")}:${(seconds % 60).toString().padStart(2, "0")}`;
  };
  const bytes = value => {
    const size = number(value);
    return size >= 1024 ** 3 ? `${(size / 1024 ** 3).toFixed(2)} GB` : `${(size / 1024 ** 2).toFixed(1)} MB`;
  };
  const statuses = {complete: "已完成", completed: "已结束", finished: "已结束", running: "运行中·快照",
    observing: "观察中·快照", live: "采集中·快照", recording: "采集中·快照", stopped: "已停止", cancelled: "已取消",
    skipped: "已跳过", error: "异常", failed: "失败", interrupted: "已中断", pending: "等待中", queued: "排队中",
    planning: "规划中", answering: "回答中", requested: "已请求", starting: "连接中·快照", recorded: "已记录",
    submission_uncertain: "提交未确认", idle: "空闲", unknown: "状态未记录"};
  const statusLabel = value => statuses[value] || text(value) || "状态未记录";
  const statusTone = value => ["error", "failed", "interrupted"].includes(value) ? "red"
    : ["skipped", "cancelled", "submission_uncertain"].includes(value) ? "amber"
      : ["complete", "completed", "finished", "recorded"].includes(value) ? "mint" : "purple";
  const originLabel = origin => origin === "planned" ? "规划提问" : origin === "manual" ? "手动提问" : "手动 / 历史";
  const candidateText = item => typeof item === "string" ? item : text(item && (item.text || item.content));
  let data;
  try {
    data = JSON.parse($("review-data").textContent);
    if (!data || !Array.isArray(data.visits)) throw new Error("invalid archive");
  } catch {
    $("load-error").textContent = "档案数据未能读取。请重新导出复盘页面，再通过本地静态服务打开。";
    $("load-error").hidden = false;
    $("run-state").textContent = "数据不可用";
    $("mode-note").hidden = true;
    return;
  }

  const visits = data.visits;
  const totals = data.totals || {};
  const state = {query: "", room: "", filter: "all", view: "grid", selected: null, tab: "rounds", qaOrigin: "all"};
  const extraRequests = new Map();
  const extraShown = new Map();
  const plannedIds = new Set(["213", "510", "21452505", "82568", "1713546334", ...list(data.room_plan).map(item => text(item.room_id))]);
  const searchable = new Map();
  visits.forEach(visit => {
    const fields = [visit.room_id, visit.canonical_room_id, visit.title, visit.area];
    list(visit.rounds).forEach(round => {
      fields.push(...list(round.candidates).map(candidateText));
      fields.push(...list(round.chorus && round.chorus.items).map(candidateText));
    });
    list(visit.questions).forEach(question => fields.push(question.question, question.answer));
    list(visit.sends).forEach(send => fields.push(send.text));
    list(visit.chorus_windows).forEach(window => fields.push(...list(window.items).map(candidateText)));
    list(visit.candidate_batches).forEach(batch => fields.push(...list(batch.items).map(candidateText)));
    searchable.set(visit, fields.map(text).join("\n").toLocaleLowerCase());
  });

  const captureStart = instant(data.capture_started_at);
  $("run-name").textContent = captureStart
    ? `${new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "long", day: "numeric"}).format(captureStart)} · 24 小时轮巡复盘`
    : "本轮直播复盘档案";
  $("generated-at").textContent = when(data.built_at);
  $("captured-until").textContent = when(data.snapshot_at);
  $("run-state").textContent = statusLabel(data.status);
  $("run-state").className = `tag ${statusTone(data.status)}`;
  if (data.deadline) document.querySelector(".snapshot-times").append(add(el("div"), el("dt", "", "计划结束"), el("dd", "", when(data.deadline))));
  $("room-count").textContent = `${count(totals.rooms)} 个房间 / ${count(totals.visits)} 次访问`;
  const metricData = [
    ["逛过的直播间", totals.rooms, "个", `${count(totals.visits)} 次访问记录`],
    ["模拟候选弹幕", totals.simulated_candidates, "条", `${count(totals.simulation_complete)} 轮完成 · ${count(totals.simulation_skipped)} 轮跳过`],
    ["问答已完成", totals.answers_complete, "次", `规划 ${count(totals.planned_questions)} · 手动 ${count(totals.manual_questions)}`],
    ["保留的录屏", bytes(totals.recording_bytes), "", "原始录像与对应内容时间一并留档"]
  ];
  for (const [label, value, unit, note] of metricData) {
    const valueNode = el("strong", "metric-value", typeof value === "number" ? count(value) : value);
    if (unit) valueNode.append(el("span", "metric-unit", unit));
    $("overview").append(add(el("article", "metric"), el("p", "metric-label", label), valueNode, el("p", "metric-note", note)));
  }
  const modeCopy = (data.config?.allow_manual_send === true
    ? "候选模拟不自动发送；允许用户手动发送，实际发送单独记录。历史访问按各自记录的权限复盘。"
    : "模拟轮次仅记录候选与现场复读，不向直播间发送。")
    + "各房间目标轮数以详情为准；错过的轮次如实标记为跳过。问答已完成表示已返回回答，不代表回答正确。";
  const sendCopy = number(totals.sent) > 0 ? ` 实际发送 ${count(totals.sent)} 条，其中手动 ${count(totals.manual_sent)} 条，见房间详情的发送记录。` : "";
  const policyCopy = number(totals.sending_policy_mismatches) > 0
    ? ` 注意：${count(totals.sending_policy_mismatches)} 次访问存在与当时权限不符或无法确认权限的发送记录，请核查。`
    : "";
  $("mode-note").querySelector("p").textContent = modeCopy + sendCopy + policyCopy;
  const footerStats = el("p", "muted-text", `原始生成档案：${count(totals.candidate_batches)} 批 / ${count(totals.candidate_total)} 条；模拟采样：${count(totals.simulated_candidates)} 条。两者分别统计，未合并充数。`);
  document.querySelector(".page-footer").insertBefore(footerStats, $("archive-links"));
  list(data.archive_links).forEach(link => {
    const node = download(link.label || "下载档案", link.url);
    if (node) $("archive-links").append(node);
  });
  $("search-input").placeholder = "搜索房间、模拟候选或问答…";
  document.querySelector(".filters").after(el("p", "search-scope", "搜索包含完整问答、模拟候选与采样复读；各房间的原始批次和全部复读档案加载后也可搜索。"));
  const roomIds = new Map();
  visits.forEach(visit => { if (!roomIds.has(text(visit.room_id))) roomIds.set(text(visit.room_id), text(visit.title)); });
  roomIds.forEach((title, id) => {
    const option = el("option", "", `${id} · ${title || "直播间"}`);
    option.value = id;
    $("room-filter").append(option);
  });
  const quickLinks = el("div", "filter-chips");
  ["82568", "1713546334"].filter(id => roomIds.has(id)).forEach(id => {
    const node = button(`直达 ${id}`, "filter-chip", () => {
      state.room = id; $("room-filter").value = id; renderRooms();
    });
    quickLinks.append(node);
  });
  if (quickLinks.childElementCount) {
    quickLinks.setAttribute("aria-label", "指定房间快捷筛选");
    quickLinks.style.marginBottom = "16px";
    $("room-results").before(quickLinks);
  }

  function hasIssues(visit) {
    return Boolean(visit.error) || ["failed", "error", "interrupted"].includes(visit.status)
      || visit.sending_policy_mismatch === true
      || number((visit.counts || {}).simulations_skipped) > 0
      || list(visit.questions).some(question => ["error", "cancelled", "submission_uncertain"].includes(question.status));
  }
  function selectedVisits() {
    const terms = state.query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    return visits.filter(visit => (!state.room || text(visit.room_id) === state.room)
      && (state.filter !== "qa" || number((visit.counts || {}).questions) > 0)
      && (state.filter !== "issues" || hasIssues(visit))
      && (state.filter !== "planned" || visit.is_requested === true || (typeof visit.is_requested !== "boolean" && plannedIds.has(text(visit.room_id))))
      && terms.every(term => searchable.get(visit).includes(term)));
  }
  function roomStats(visit) {
    const c = visit.counts || {};
    return [[`${count(c.simulations_complete)} / ${count(visit.target_rounds)}`, "模拟完成 / 目标", true],
      [count(c.questions), "真实问答", false], [count(c.candidate_batches), "原始批次", false]];
  }
  function makeCard(visit) {
    const card = el("article", "room-card");
    card.dataset.visitId = text(visit.id);
    const open = button("", "room-open", () => openDetail(visit));
    open.setAttribute("aria-label", `查看房间 ${visit.room_id}：${visit.title || "直播记录"}`);
    const thumb = el("div", "room-thumb");
    const thumbUrl = safeHref(visit.thumbnail);
    const placeholder = () => add(el("div", "thumb-placeholder"), el("strong", "", "AI 同担"), el("span", "", "暂无导出的预览画面"));
    if (thumbUrl) {
      const image = el("img"); image.src = thumbUrl; image.alt = `房间 ${visit.room_id} 的录屏画面`; image.loading = "lazy"; image.decoding = "async";
      image.addEventListener("error", () => image.replaceWith(placeholder()), {once: true});
      thumb.append(image);
    } else thumb.append(placeholder());
    add(thumb, add(el("div", "thumb-top"), badge(visit.area || "分区未记录"), badge(statusLabel(visit.status), statusTone(visit.status))),
      add(el("div", "thumb-bottom"), el("span", "", `${when(visit.started_at)} · ${duration(visit.duration_seconds)}`),
        el("span", "play-indicator", visit.preview && safeHref(visit.preview.url) ? "▶" : "↗")));
    const body = el("div", "room-body");
    add(body, el("h3", "room-title", visit.title || `直播间 ${visit.room_id}`),
      el("p", "room-subtitle", `房间 ${visit.room_id} · 访问 ${visit.id}`));
    const stats = el("div", "room-stats");
    roomStats(visit).forEach(([value, label, accent]) => stats.append(add(el("div", "room-stat"), el("strong", accent ? "accent" : "", value), el("span", "", label))));
    body.append(stats);
    const complete = number((visit.counts || {}).simulations_complete), skipped = number((visit.counts || {}).simulations_skipped), target = number(visit.target_rounds);
    if (target > 0) {
      const progress = el("div", "room-progress"); progress.setAttribute("aria-hidden", "true");
      const done = el("span", "progress-complete"), missed = el("span", "progress-skipped");
      done.style.width = `${Math.min(100, complete / target * 100)}%`;
      missed.style.width = `${Math.max(0, Math.min(100 - Math.min(100, complete / target * 100), skipped / target * 100))}%`;
      add(progress, done, missed); body.append(progress);
      body.append(add(el("div", "room-progress-note"), el("span", "", `完成 ${count(complete)} · 跳过 ${count(skipped)}`), el("span", "", `目标 ${count(target)} 轮`)));
    }
    if (visit.error) body.append(el("p", "room-alert", "存在异常记录 · 点开查看"));
    add(open, thumb, body); card.append(open); return card;
  }
  function makeTable(filtered) {
    const table = el("table", "room-table");
    const header = el("tr"); ["直播间 / 访问", "采集开始", "停留", "模拟完成 / 目标", "跳过", "问答", "快照状态"].forEach(label => header.append(el("th", "", label)));
    table.append(add(el("thead"), header));
    const body = el("tbody");
    filtered.forEach(visit => {
      const row = el("tr"); row.dataset.visitId = text(visit.id);
      row.append(add(el("td"), button(visit.title || `房间 ${visit.room_id}`, "table-title", () => openDetail(visit)),
        el("span", "table-small", `${visit.room_id} · ${visit.id}`)));
      [when(visit.started_at), duration(visit.duration_seconds), `${count((visit.counts || {}).simulations_complete)} / ${count(visit.target_rounds)}`,
        count((visit.counts || {}).simulations_skipped), count((visit.counts || {}).questions)].forEach(value => row.append(el("td", "", value)));
      row.append(add(el("td"), badge(statusLabel(visit.status), statusTone(visit.status)))); body.append(row);
    });
    table.append(body); return add(el("div", "table-wrap"), table);
  }
  function renderRooms() {
    const filtered = selectedVisits();
    $("room-results").replaceChildren();
    $("room-results").className = state.view === "grid" ? "room-grid" : "";
    if (state.view === "grid") filtered.forEach(visit => $("room-results").append(makeCard(visit)));
    else if (filtered.length) $("room-results").append(makeTable(filtered));
    $("empty-state").hidden = filtered.length > 0;
    $("search-status").textContent = `显示 ${filtered.length} / ${visits.length} 次访问`;
  }
  let searchTimer;
  $("search-input").addEventListener("input", event => {
    state.query = event.target.value; clearTimeout(searchTimer); searchTimer = setTimeout(renderRooms, 120);
  });
  $("room-filter").addEventListener("change", event => { state.room = event.target.value; renderRooms(); });
  document.querySelectorAll("[data-filter]").forEach(node => node.addEventListener("click", () => {
    state.filter = node.dataset.filter;
    document.querySelectorAll("[data-filter]").forEach(item => { const active = item === node; item.classList.toggle("is-active", active); item.setAttribute("aria-pressed", String(active)); });
    renderRooms();
  }));
  ["grid", "table"].forEach(view => $(`${view}-view`).addEventListener("click", () => {
    state.view = view;
    ["grid", "table"].forEach(item => { $(`${item}-view`).classList.toggle("is-active", view === item); $(`${item}-view`).setAttribute("aria-pressed", String(view === item)); });
    renderRooms();
  }));
  $("clear-filters").addEventListener("click", () => {
    clearTimeout(searchTimer); state.query = ""; state.room = ""; state.filter = "all";
    $("search-input").value = ""; $("room-filter").value = "";
    document.querySelectorAll("[data-filter]").forEach(item => { const active = item.dataset.filter === "all"; item.classList.toggle("is-active", active); item.setAttribute("aria-pressed", String(active)); });
    renderRooms(); $("search-input").focus();
  });
  document.addEventListener("keydown", event => {
    if (event.key === "/" && !$("room-dialog").open && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
      event.preventDefault(); $("search-input").focus();
    }
  });

  function showVideo(url, caption, start = 0) {
    const safe = safeHref(url);
    $("detail-media").replaceChildren();
    if (!safe) { $("detail-media").append(el("p", "video-placeholder", "本次访问尚无可播放的导出预览。原始录像状态见“录屏与下载”。")); return; }
    const video = el("video"); video.controls = true; video.preload = "metadata"; video.playsInline = true;
    if (state.selected && safeHref(state.selected.thumbnail)) video.poster = safeHref(state.selected.thumbnail);
    video.src = safe;
    if (start > 0) video.addEventListener("loadedmetadata", () => {
      if (Number.isFinite(video.duration) && start < video.duration) video.currentTime = start;
    }, {once: true});
    video.addEventListener("error", () => {
      const note = el("p", "video-placeholder", "浏览器未能播放此格式。可在“录屏与下载”中下载后用本地播放器打开。");
      if (!$("detail-media").querySelector(".video-placeholder")) $("detail-media").append(note);
    });
    add($("detail-media"), video, add(el("div", "video-caption"), el("p", "", caption), download("下载此片段 ↓", safe)));
  }
  function seekContent(visit, seconds) {
    const target = number(seconds);
    const media = list(visit.recordings).filter(recording => safeHref(recording.url));
    const session = instant(visit.session_started_at);
    const recording = media.find(item => {
      const start = instant(item.started_at), end = instant(item.ended_at);
      if (!start || !session) return false;
      const wall = session.getTime() + target * 1000;
      return wall >= start.getTime() && (!end || wall < end.getTime());
    });
    if (recording && session) {
      const position = Math.max(0, (session.getTime() + target * 1000 - instant(recording.started_at).getTime()) / 1000);
      showVideo(recording.url, `原始录像 · 会话 ${offset(target)}，按录制起点估算定位；实际画面可能存在偏差。`, position);
      $("detail-media").scrollIntoView({behavior: "smooth", block: "nearest"});
      return;
    }
    const preview = visit.preview;
    if (preview && safeHref(preview.url) && number(preview.start_seconds) <= target && target <= number(preview.start_seconds) + number(preview.duration_seconds)) {
      showVideo(preview.url, `预览片段 · 会话 ${offset(target)}；定位依据：${preview.timestamp_basis || "存档时间"}。`, target - number(preview.start_seconds));
      $("detail-media").scrollIntoView({behavior: "smooth", block: "nearest"});
    }
  }
  function contentLink(visit, seconds) {
    if (seconds === null || seconds === undefined) return null;
    const hasMedia = list(visit.recordings).some(recording => safeHref(recording.url)) || Boolean(visit.preview && safeHref(visit.preview.url));
    if (!hasMedia) return el("span", "muted-text", `内容时间 ${offset(seconds)} · 对应录像未导出`);
    return button(`定位内容 ${offset(seconds)} ↗`, "time-link", () => seekContent(visit, seconds));
  }
  function chorusList(chorus) {
    const wrap = el("div", "chorus-list");
    if (!chorus || chorus.recorded === false || !Array.isArray(chorus.items)) return add(wrap, el("p", "muted-text", "此时刻未记录复读窗口，不能据此判断是否有人复读。"));
    if (!chorus.items.length) return add(wrap, el("p", "muted-text", "本窗口没有达到检测阈值的复读内容。"));
    chorus.items.forEach(item => {
      let label = `${count(item.count)} 次`;
      if (item.total !== null && item.total !== undefined) label += ` / ${count(item.total)} 条`;
      if (item.window_seconds) label += ` · ${count(item.window_seconds)} 秒窗`;
      wrap.append(add(el("div", "chorus-line"), el("span", "chorus-text", item.text), el("span", "chorus-count", label)));
    });
    return wrap;
  }
  function candidateList(items) {
    const ul = el("ol", "candidate-list");
    list(items).forEach(item => ul.append(el("li", "candidate-line", candidateText(item))));
    return ul;
  }
  function extraControl(visit, section, panel) {
    const field = section === "chorus" ? "chorus_windows" : "candidate_batches";
    const title = section === "chorus" ? "全部复读档案" : "原始生成批次";
    if (Array.isArray(visit[field]) && extraShown.get(visit.id)?.has(section)) return;
    const url = text(visit.extra_url);
    if (!/^details\/[A-Za-z0-9][A-Za-z0-9._-]*\.js$/.test(url) || !safeHref(url)) {
      if (!Array.isArray(visit[field])) panel.append(el("p", "muted-text", `${title}未随本页面导出；本页只展示已保存的模拟采样。`));
      return;
    }
    const wrap = el("div", "extra-loader");
    const note = el("p", "muted-text", "按需读取本地档案，内容较多时可能需要片刻。不会访问外部服务。");
    const load = button(`加载${title} ↓`, "secondary-button", async () => {
      load.disabled = true; load.textContent = "正在读取完整档案…"; note.textContent = "正在读取本地文件。";
      try {
        if (!extraRequests.has(visit.id)) {
          extraRequests.set(visit.id, new Promise((resolve, reject) => {
            const script = document.createElement("script");
            script.src = safeHref(url); script.async = true;
            const timeout = setTimeout(() => { script.remove(); reject(new Error("archive_timeout")); }, 15000);
            script.onload = () => {
              clearTimeout(timeout); script.remove();
              const cache = window.PATROL_EXTRA;
              if (cache && Object.prototype.hasOwnProperty.call(cache, visit.id) && cache[visit.id] && typeof cache[visit.id] === "object") resolve(cache[visit.id]);
              else reject(new Error("archive_missing"));
            };
            script.onerror = () => { clearTimeout(timeout); script.remove(); reject(new Error("archive_unavailable")); };
            document.head.append(script);
          }));
        }
        const extra = await extraRequests.get(visit.id);
        if (!Array.isArray(extra[field])) throw new Error("archive_missing_section");
        for (const key of ["chorus_windows", "candidate_batches"]) if (Array.isArray(extra[key])) visit[key] = extra[key];
        const fields = [];
        list(extra.chorus_windows).forEach(window => fields.push(...list(window.items).map(candidateText)));
        list(extra.candidate_batches).forEach(batch => fields.push(...list(batch.items).map(candidateText)));
        searchable.set(visit, `${searchable.get(visit)}\n${fields.join("\n").toLocaleLowerCase()}`);
        if (!extraShown.has(visit.id)) extraShown.set(visit.id, new Set());
        extraShown.get(visit.id).add(section);
        if (state.selected === visit) renderTab();
        renderRooms();
      } catch {
        extraRequests.delete(visit.id); load.disabled = false; load.textContent = `重试加载${title}`;
        note.textContent = "本地档案暂时无法读取，请确认页面与 details 目录一起导出后重试。";
        note.setAttribute("role", "alert");
      }
    });
    add(wrap, load, note); panel.append(wrap);
  }
  function renderRounds(visit, panel) {
    panel.append(el("p", "panel-intro", `本次目标 ${count(visit.target_rounds)} 轮${visit.simulation_interval_seconds ? `，约每 ${(number(visit.simulation_interval_seconds) / 60).toFixed(1).replace(/\.0$/, "")} 分钟记录一次` : ""}。每轮候选和复读为当时存档，模拟本身不发送；跳过轮次不补造。${visit.allow_manual_send === true ? "用户手动发送另见发送记录，不计入模拟发送。" : ""}`));
    if (!list(visit.rounds).length) panel.append(el("p", "muted-text", "该次访问暂未导出模拟轮次记录。"));
    list(visit.rounds).forEach(round => {
      const card = el("article", `round-card${round.status === "skipped" ? " is-skipped" : ""}`);
      card.dataset.round = text(round.round);
      const header = add(el("div", "record-header"), el("h3", "", `第 ${round.round} 轮`),
        add(el("div", "record-meta"), el("span", "", `目标 ${when(round.target_at, true)}`), badge(statusLabel(round.status), statusTone(round.status))));
      const body = el("div", "record-body");
      if (round.reason) body.append(el("p", "record-note", round.reason));
      if (round.candidate_selection === "first_5_of_legacy_10" || number(round.source_candidate_count) > list(round.candidates).length) body.append(el("p", "record-note", `过渡记录：原生 ${count(round.source_candidate_count || 10)} 条候选中取前 ${list(round.candidates).length} 条，完整原始批次另存。`));
      add(body, el("h4", "subheading", `候选弹幕 · ${list(round.candidates).length} 条`), list(round.candidates).length ? candidateList(round.candidates) : el("p", "muted-text", "本轮没有记录候选，不用较晚批次补齐。"));
      const chorus = add(el("div", "round-chorus"), el("h4", "subheading", "同一轮 · 大家在刷"), chorusList(round.chorus));
      if (round.chorus_capture_note) chorus.append(el("p", "record-note", round.chorus_capture_note));
      else if (round.status === "skipped" && round.chorus && round.chorus.recorded) chorus.append(el("p", "muted-text", "跳过轮次中的复读是记录时快照，不视为过去目标时刻的弹幕。"));
      body.append(chorus);
      body.append(add(el("div", "qa-meta"), el("span", "", `实际记录 ${when(round.time)}`), contentLink(visit, round.snapshot_time)));
      add(card, header, body); panel.append(card);
    });
  }
  function renderQuestions(visit, panel) {
    panel.append(el("p", "panel-intro", "手动提问作为额外信息留档，不占规划提问计数。完整回答原文保留；“已完成”不代表事实正确，请结合录像核对。"));
    const controls = el("div", "qa-origin-filters");
    [["all", "全部问答"], ["planned", "规划提问"], ["manual", "手动提问"], ["history", "手动 / 历史"]].forEach(([origin, label]) => {
      const node = button(label, `filter-chip${state.qaOrigin === origin ? " is-active" : ""}`, () => {state.qaOrigin = origin; renderTab();});
      node.setAttribute("aria-pressed", String(state.qaOrigin === origin)); controls.append(node);
    });
    panel.append(controls);
    const questions = list(visit.questions).filter(question => state.qaOrigin === "all" || question.origin === state.qaOrigin || (state.qaOrigin === "history" && !["planned", "manual"].includes(question.origin)));
    panel.append(el("p", "qa-count-hint", `显示 ${questions.length} / ${list(visit.questions).length} 条问答`));
    questions.forEach((question, index) => {
      const card = el("article", "qa-card"); card.dataset.questionId = text(question.id);
      add(card, add(el("div", "record-header"), el("h3", "", `问答 ${question.id || index + 1}`), add(el("div", "record-meta"), badge(originLabel(question.origin), question.origin === "planned" ? "purple" : ""), badge(statusLabel(question.status), statusTone(question.status)))));
      const body = el("div", "record-body");
      body.append(el("p", "qa-question", question.question || "问题原文未记录"));
      body.append(add(el("div", "qa-meta"), el("span", "", `提问 ${when(question.requested_at)}`), el("span", "", question.answered_at ? `返回 ${when(question.answered_at)}` : "尚无回答完成记录"), contentLink(visit, question.snapshot_time)));
      if (question.error) body.append(el("p", "record-note", question.error));
      body.append(el("div", "qa-answer", question.answer || "没有已记录的回答正文。"));
      if (question.plan || list(question.evidence_times).length || list(question.replay_times).length) {
        const detail = add(el("details", "qa-plan"), el("summary", "", "检索计划与引用内容时间"));
        if (question.plan) detail.append(el("p", "", question.plan));
        const times = [...new Set([...list(question.replay_times), ...list(question.evidence_times)])];
        times.forEach(value => detail.append(add(el("p"), contentLink(visit, value)))); body.append(detail);
      }
      card.append(body); panel.append(card);
    });
    if (!questions.length) panel.append(el("p", "muted-text", "当前筛选下没有问答记录。"));
  }
  function renderChoruses(visit, panel) {
    const full = Array.isArray(visit.chorus_windows) && (!visit.extra_url || extraShown.get(visit.id)?.has("chorus"));
    const windows = full ? visit.chorus_windows : list(visit.rounds).filter(round => round.chorus && round.chorus.recorded !== false).map(round => ({...round.chorus, time: round.time, round: round.round, skipped: round.status === "skipped"}));
    panel.append(el("p", "panel-intro", full ? `这里保留全部 ${windows.length} 个复读检测窗口，包括空窗口。重复短句是现场观众记录，不是同担发送记录。` : `下方为模拟轮次所保存的复读快照。原始档案共 ${count((visit.counts || {}).chorus_windows)} 个窗口，可按需加载查看。`));
    if (!full) extraControl(visit, "chorus", panel);
    windows.forEach((window, index) => {
      const card = el("article", "chorus-card");
      add(card, add(el("div", "record-header"), el("h3", "", full ? `窗口 ${index + 1}` : `第 ${window.round} 轮快照`), el("span", "record-meta", when(window.updated_at || window.time))), add(el("div", "record-body"), chorusList(window)));
      if (window.skipped) card.append(el("p", "record-note", "该轮已跳过：此为记录时快照，不用于还原过去目标时刻。"));
      panel.append(card);
    });
    if (!windows.length) panel.append(el("p", "muted-text", "本页暂无复读快照。未导出记录不等于当时没有复读。"));
  }
  function renderRecordings(visit, panel) {
    panel.append(el("p", "panel-intro", "原始录像保留现场声音。浏览器不支持 MKV 时可下载后使用本地播放器；录制中或未导出的文件不会提供无效链接。"));
    list(visit.recordings).forEach(recording => {
      const card = el("article", "recording-card");
      add(card, el("h3", "", recording.name || "录像分段"), add(el("p", "record-meta"),
        el("span", "", bytes(recording.bytes)), el("span", "", recording.duration_seconds ? duration(recording.duration_seconds) : "时长未记录"), el("span", "", `${when(recording.started_at)} — ${recording.ended_at ? when(recording.ended_at) : "未结束"}`)));
      const actions = el("div", "recording-actions");
      const href = safeHref(recording.url);
      if (href) add(actions, button("播放此分段 ▶", "secondary-button", () => {
        showVideo(href, `${recording.name || "原始录像"} · 浏览器若无法播放，可下载后打开。`); $("detail-media").scrollIntoView({behavior: "smooth", block: "nearest"});
      }), download("下载原始录像 ↓", href));
      else actions.append(badge("录制中 / 未导出", "amber"));
      card.append(actions); panel.append(card);
    });
    if (!list(visit.recordings).length) panel.append(el("p", "muted-text", "该次访问没有已导出的录像记录。"));
    const links = el("div", "file-list");
    list(visit.archive_links).forEach(link => { const node = download(link.label || "下载原始档案", link.url); if (node) links.append(node); });
    if (links.childElementCount) panel.append(links);
    const batches = !visit.extra_url || extraShown.get(visit.id)?.has("candidates") ? list(visit.candidate_batches) : [];
    if (!batches.length) extraControl(visit, "candidates", panel);
    if (batches.length) {
      const section = el("section", "raw-batches");
      section.append(el("h3", "subheading", `原始生成批次 · ${batches.length} 批（与模拟采样分别统计）`));
      batches.forEach((batch, index) => section.append(add(el("details"), el("summary", "", `批次 ${index + 1} · ${when(batch.generated_at || batch.time)} · ${list(batch.items).length} 条`), candidateList(batch.items))));
      panel.append(section);
    }
  }
  function renderSends(visit, panel) {
    const counts = visit.counts || {};
    const policy = visit.allow_manual_send === true
      ? `此次访问允许用户手动发送，候选模拟不自动发送。${visit.allow_manual_send_since ? `权限自 ${when(visit.allow_manual_send_since)} 起生效。` : ""}`
      : "此次访问没有记录手动发送授权；当前设置不追溯授权历史访问。";
    panel.append(el("p", "panel-intro", policy + `实际发送 ${count(counts.sent)} 条，其中手动 ${count(counts.manual_sent)} 条；应用确认结果不代表主播看见。`));
    if (visit.sending_policy_mismatch === true) panel.append(el("p", "record-note", "此访问存在与当时权限不符或无法确认权限的发送记录，请结合原始报告核查。"));
    list(visit.sends).forEach(send => {
      const source = send.source === "manual" ? "手动发送" : send.source === "auto" ? "自动发送" : "来源未记录";
      panel.append(add(el("article", "round-card"),
        add(el("div", "record-header"), el("h3", "", source), el("span", "record-meta", when(send.sent_at))),
        add(el("div", "record-body"), el("p", "", send.text), contentLink(visit, send.offset))));
    });
    if (!list(visit.sends).length) panel.append(el("p", "muted-text", "暂无已确认发送的正文记录。"));
    if (number(counts.sent) > list(visit.sends).length) panel.append(el("p", "record-note", "发送计数大于正文记录数，部分详情没有被事件日志捕获。"));
  }
  const tabs = [["rounds", "模拟轮次"], ["questions", "真实问答"], ["chorus", "大家在刷"], ["sends", "发送记录"], ["recordings", "录屏与下载"]];
  function renderTab() {
    const visit = state.selected;
    const panel = $("detail-content"); panel.replaceChildren();
    $("detail-tabs").querySelectorAll("[role=tab]").forEach(node => {
      const active = node.dataset.tab === state.tab; node.setAttribute("aria-selected", String(active)); node.tabIndex = active ? 0 : -1;
    });
    panel.setAttribute("aria-labelledby", `tab-${state.tab}`);
    ({rounds: renderRounds, questions: renderQuestions, chorus: renderChoruses, sends: renderSends, recordings: renderRecordings})[state.tab](visit, panel);
  }
  function openDetail(visit) {
    state.selected = visit; state.tab = "rounds"; state.qaOrigin = "all";
    $("detail-kicker").textContent = `ROOM ${visit.room_id} / ${visit.id} · 快照档案`;
    $("detail-title").textContent = visit.title || `直播间 ${visit.room_id}`;
    $("detail-summary").replaceChildren(badge(statusLabel(visit.status), statusTone(visit.status)), badge(visit.area || "分区未记录"),
      badge(`停留 ${duration(visit.duration_seconds)}`), badge(`目标 ${count(visit.target_rounds)} 轮`, "purple"),
      badge(`规划问答 ${count((visit.counts || {}).planned_questions)} · 手动 ${count((visit.counts || {}).manual_questions)}`));
    if (visit.error) $("detail-summary").append(badge(`异常：${visit.error}`, "red"));
    if (visit.exit_reason) $("detail-summary").append(badge(`结束原因：${visit.exit_reason}`));
    const preview = visit.preview;
    showVideo(preview && preview.url, preview ? `本次录制的预览片段 · ${duration(preview.duration_seconds)}。完整录像见“录屏与下载”。` : "");
    $("detail-tabs").replaceChildren();
    tabs.forEach(([key, label]) => {
      const total = key === "rounds" ? list(visit.rounds).length : key === "questions" ? list(visit.questions).length : key === "chorus" ? number((visit.counts || {}).chorus_windows) : key === "sends" ? number((visit.counts || {}).sent) : list(visit.recordings).length;
      const node = button(`${label} ${count(total)}`, "detail-tab", () => {state.tab = key; renderTab();});
      node.id = `tab-${key}`; node.dataset.tab = key; node.setAttribute("role", "tab"); node.setAttribute("aria-controls", "detail-content");
      node.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault(); let index = tabs.findIndex(item => item[0] === state.tab);
        index = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
        state.tab = tabs[index][0]; renderTab(); $(`tab-${state.tab}`).focus();
      });
      $("detail-tabs").append(node);
    });
    renderTab(); $("room-dialog").showModal(); $("room-dialog").scrollTop = 0; $("close-detail").focus();
  }
  $("close-detail").addEventListener("click", () => $("room-dialog").close());
  $("room-dialog").addEventListener("close", () => {
    const video = $("detail-media").querySelector("video"); if (video) video.pause();
    const trigger = [...$("room-results").querySelectorAll("[data-visit-id]")].find(node => node.dataset.visitId === text(state.selected && state.selected.id));
    if (trigger) trigger.querySelector("button")?.focus({preventScroll: true});
  });
  $("room-dialog").addEventListener("click", event => {
    if (event.target !== $("room-dialog")) return;
    const box = $("room-dialog").getBoundingClientRect();
    if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) $("room-dialog").close();
  });
  renderRooms();
})();
