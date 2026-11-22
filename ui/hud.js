// agentbus 悬浮窗：收起是一颗胶囊，点开是面板；有任务开始/完成时自己弹出来，过几秒收回去。
// 顶部停靠是又宽又矮的长条，侧边停靠是窄长条；两种都能切“头像 / 场景”视图，额度显示在各自队员名下。
(() => {
  const { U, icon, robot, esc, avatar, brandMark, STATE, clock, costText } = AB;
  const $ = s => document.querySelector(s);
  const T = window.__TAURI__;
  const report = msg => fetch("/api/hudlog?m=" + encodeURIComponent(msg)).catch(() => {});
  const invoke = (cmd, args) => (T && T.core ? T.core.invoke(cmd, args).catch(e => report(cmd + " 失败：" + e)) : Promise.resolve());
  const PAD = 14;
  // 鼠标穿透：收起时透明留白处让点击落到下面的窗口（由 __hudHover 按光标落点开关）
  let through = false;
  const setThrough = v => { if (v !== through) { through = v; invoke("hud_through", { on: v }); } };
  const island = $("#island"), pill = $("#pill"), panel = $("#panel"), row = $("#row"), troupeEl = $("#troupe"), askEl = $("#ask");
  // 「小人」样式（view=scene）：收起时只有一颗 logo 球 + 在干活的小人，全透明；点球展开面板，展开后一直开着，再点才收
  const troupe = () => view === "scene";
  // 办公室：每个在用 bus 的 agent 一间。两间以上同时在干活时，收起状态换成办公室图标（下面一小排正在干活的头像）
  // 只算真正在用 bus 的窗口；没走 bus、直接在用的 agent 不算一间（它们照样在小人里出现）
  const offices = () => ((S && S.offices) || []).filter(o => o.groups.some(g => g.active && !g.direct));
  const multi = () => offices().length >= 2;
  let joinNote = null; // 点了「接入」之后的反馈卡：这个窗口怎么拿到握手（装了钩子 / 兜底选项）
  const asking = () => !!(joinNote || (S && (S.join_ask || []).length));
  // 专注模式：只保持折叠；鼠标移上去（或点一下）右边弹出一张进度小卡片
  const focus = () => !!(S && S.settings.hud.focus);
  // 鼠标移到收起的悬浮窗上：旁边弹出一颗专注圆按钮，移开就收。
  // 窗口两边一直留着放按钮的透明空间（顶部 / 底部 / 悬浮时两边各留，胶囊保持居中），这块空间点得穿
  const PEEK = 96;
  let peekOn = false, peekT = null;
  const peekEl = $("#peek"); // 有新对话在问接不接入：收起时也用透明横排，卡片和胶囊并排
  let officeSel = null; // 在面板里看哪一间办公室
  const LOGO = `<span class="logo"><img src="assets/app-icon-128.png" alt=""></span>`;

  let openMsg = null;
  let S = null, open = false, pinned = false, closeTimer = null, doneCard = null, known = null, dock = "top", edge = "top", view = "avatar";
  // 空闲：没人干活 3 分钟缩成小球，20 分钟自己藏起来；有活开工或从菜单栏叫出来就醒
  let lastBusy = Date.now(), ball = false, idleHidden = false;
  // edge = 贴在屏幕哪条边（top / bottom / left / right）；dock = 排版用：底部和顶部一样是宽条
  const A = id => (S.agents.find(a => a.id === id) || { id, name: (S.names || {})[id] || id, color: "#888", acts: [], state: "off" });
  const ring = (a, size) => `<span class="ring ${a.state === "working" ? "working" : ""}">${avatar(a, size)}<i class="sd" style="background:${(STATE[a.state] || STATE.off).c}"></i></span>`;
  const qcls = a => a.billing === "deepseek" ? "gold" : a.billing === "codex" ? "hp" : "";
  const crew = () => S.agents.filter(a => a.place === "office");
  const runOf = id => S.running.find(r => r.agent === id);
  const shortQ = t => String(t || "").replace("体力 ", "⚡").replace(" · 周 ", " 周");

  // ---------- 窗口尺寸 ----------
  let lastWin = null, dragOff = null;
  const offset = () => dragOff ?? (S ? Number(S.settings.hud[dock === "top" ? "offset_top" : "offset_side"] || 0) : 0);
  const fpos = () => S ? { cx: Number(S.settings.hud.float_x) || null, cy: Number(S.settings.hud.float_y) || null } : {};
  const anchorSize = () => dock === "top" ? 0 : (pill.scrollHeight || 120) + PAD * 2; // 侧边：以收起时的胶囊中心为准
  // 收起时窗口一直留着放专注按钮的透明空间：鼠标移上去只让按钮淡入，窗口不变大小——
  // 悬停时再改窗口大小会触发移出 / 移入，来回抽搐、要悬停好几次才出来
  function winSize(w, h) {
    const ex = !open ? (dock === "top" || edge === "float" ? PEEK * 2 : PEEK) : 0;
    return dock === "top" ? { w: w + PAD * 2 + ex, h: Math.max(h + PAD + 4, !open ? 44 : 0) } : { w: w + PAD + 6 + ex, h: Math.max(h + PAD * 2, !open ? 48 : 0) };
  }
  function setWindow(w, h) {
    const s = winSize(w, h);
    const f = fpos();
    const key = `${edge}:${Math.ceil(s.w)}x${Math.ceil(s.h)}:${offset()}:${f.cx},${f.cy}`;
    if (key === lastWin || dragging) return;
    lastWin = key;
    invoke("hud_resize", { w: Math.ceil(s.w), h: Math.ceil(s.h), dock: edge, offset: offset(), anchor: edge === "float" ? 34 + PAD + 4 : anchorSize(), cx: f.cx, cy: f.cy });
  }
  function measure() {
    if (!open && (troupe() || multi() || asking())) return { w: Math.max(38, row.scrollWidth), h: Math.max(38, row.scrollHeight) };
    if (!open && ball) return dock === "top" ? { w: 34, h: 34 } : { w: 40, h: 40 };
    if (!open) return dock === "top" ? { w: Math.min(560, pill.scrollWidth), h: 34 } : { w: 48, h: pill.scrollHeight }; // 窗体贴合胶囊：多出来的深色底会被专注按钮压住
    return { w: panel.scrollWidth, h: panel.scrollHeight };
  }
  let shrinkT = null, winBox = { w: 0, h: 0 };
  function layout() {
    const t = measure();
    const cur = island.getBoundingClientRect();
    // 收起状态（小人 / 办公室图标）：变大马上变，变小等内容稳定 1.5 秒再缩，免得小人退场时窗口一抽一抽
    if (!open && (troupe() || multi() || asking())) {
      island.style.width = t.w + "px";
      island.style.height = t.h + "px";
      clearTimeout(layout.t);
      if (t.w >= winBox.w && t.h >= winBox.h) { clearTimeout(shrinkT); shrinkT = null; winBox = t; setWindow(t.w, t.h); }
      else if (!shrinkT) shrinkT = setTimeout(() => { shrinkT = null; const n = measure(); winBox = n; setWindow(n.w, n.h); }, 1500);
      return;
    }
    winBox = t;
    // 先按新旧两个尺寸里大的撑开窗口，动画放完再收到新尺寸
    setWindow(Math.max(t.w, cur.width), Math.max(t.h, cur.height));
    clearTimeout(layout.t);
    layout.t = setTimeout(() => setWindow(t.w, t.h), 400);
    island.style.width = t.w + "px";
    island.style.height = t.h + "px";
  }
  function setOpen(v, opts = {}) {
    clearTimeout(closeTimer);
    if (v) { hidePeek(true); setThrough(false); }
    if (opts.pin !== undefined) pinned = opts.pin;
    if (v !== open) {
      open = v;
      island.classList.toggle("open", v);
      pill.setAttribute("aria-expanded", v);
      if (v) { openedAt = Date.now(); invoke("hud_front"); }
    }
    render();
    if (v && opts.auto && !pinned) { const stay = S.settings.hud.stay; if (stay > 0) closeTimer = setTimeout(() => { doneCard = null; setOpen(false); }, stay * 1000); }
  }

  // ---------- 内容 ----------
  const hasCost = r => !!(r && r.live && typeof r.live === "object" && r.live.text); // 这一轮的消耗（对象）；别的字段名撞上也不至于整个面板白屏
  const liveText = r => hasCost(r) ? r.live.text + (r.live.rate ? ` · ${r.live.rate}` : "") : "";
  // 胶囊里只放一句短话：长路径只留文件名，按显示宽度截（中文算 2），不让原文把悬浮窗撑长
  function clip(t, max = 26) {
    t = String(t || "").replace(/\s+/g, " ").replace(/(?:\/[^\s/]+)+\/([^\s/]+)/g, "$1").trim();
    let w = 0, out = "";
    for (const c of t) { w += c.charCodeAt(0) > 0x2e80 ? 2 : 1; if (w > max) return out.replace(/[，,、 ]+$/, "") + "…"; out += c; }
    return out;
  }
  function pillHTML() {
    const run = S.running, cr = crew();
    const BALL = troupe() || multi() ? "" : `<span class="logo-ball"><img src="assets/app-icon-128.png" alt=""><span class="badge"></span></span>`; // 小人样式不要 logo
    if (multi()) { // 好几个 agent 同时在用 bus：一间办公室一个图标，下面一小排正在干活的头像（按窗口分组）
      const tiny = id => { const a = A(id); return a.avatar ? `<img src="${esc(a.avatar)}" alt="">` : `<b style="background:${esc(a.color)}"></b>`; };
      return BALL + `<span class="offs">${offices().map(o => {
        const gs = o.groups.filter(g => g.active);
        return `<span class="oi" data-office="${esc(o.id)}" title="${esc(o.name)}：${gs.length} 个窗口在干活">
          <span class="roof">${avatar(A(o.agent), 22)}</span>
          <span class="om">${gs.map(g => `<span class="og">${g.members.map(tiny).join("")}</span>`).join("")}</span></span>`;
      }).join("")}</span>`;
    }
    if (troupe()) return ""; // 小人样式：没有球，点小人展开
    if (ball && !run.length) { // 小球：只留主位的头像和状态点
      const a = A(S.lead) || cr[0] || {};
      return `<span class="faces ball-face">${ring(a, dock === "top" ? 26 : 32)}</span>`;
    }
    const faces = (run.length ? run.map(r => ({ ...A(r.agent), state: "working" })) : cr).slice(0, dock === "top" ? 5 : 6);
    // 收起时右边放一句工作简报（额度点开就能看到）：谁在排队 / 最近谁完成了什么
    const q = run.filter(r => r.queued).length, last = (S.recent || [])[0];
    const ago = t => { const m = Math.round((Date.now() - new Date((t || "").replace(" ", "T")).getTime()) / 60000); return m < 1 ? "刚刚" : m < 60 ? m + " 分钟前" : m < 720 ? Math.round(m / 60) + " 小时前" : ""; };
    const brief = run.length ? (q ? `⏳ ${q} 个在排队` : run.length > 1 ? `共 ${run.length} 个活在跑` : "")
      : last && ago(last.t) ? `${A(last.agent).name} ${ago(last.t)}完成：${String(last.text || "").replace(/\s+/g, " ").slice(0, 18)}` : "今天还没派活";
    const meta = brief ? `<span class="meta brief">${esc(brief)}</span>` : "";
    const size = dock === "top" ? 22 : 30;
    if (run.length) { // 好几个在干活：每 4 秒轮流显示一个，带它到现在花了多少
      const r = run[Math.floor(Date.now() / 4000) % run.length], a = A(r.agent), act = r.queued ? "⏳ " + clip(r.queued) : r.title || clip(r.task);
      const cost = liveText(r);
      return `<span class="faces">${faces.map(f => ring(f, size)).join("")}</span>
        <span class="txt">${esc(a.name)}${run.length > 1 ? `<em class="rot">${run.indexOf(r) + 1}/${run.length}</em>` : ""} <span>${esc(act)}</span></span>
        <span class="clock" data-since="${r.start}"></span><span class="vnum">${run.length}</span>${cost ? `<span class="meta cost">${esc(cost)}</span>` : ""}`;
    }
    const live = S.agents.filter(a => a.state === "working"); // 没派活、但你正直接在用的 agent（看它的日志在动）
    if (live.length) {
      const a = live[Math.floor(Date.now() / 4000) % live.length], act = clip((a.acts && a.acts[0] && a.acts[0].plain) || "在干活");
      return `<span class="faces">${live.slice(0, 6).map(f => ring(f, size)).join("")}</span>
        <span class="txt">${esc(a.name)}${live.length > 1 ? `<em class="rot">${live.indexOf(a) + 1}/${live.length}</em>` : ""} <span>${esc(act)}</span></span>`;
    }
    return `<span class="faces">${faces.map(f => ring(f, size)).join("")}</span>
      <span class="txt">${cr.length} 位待命</span>${meta}`;
  }
  // 还剩多少：订阅额度画一条剩余百分比的进度条；余额只写金额；看不到的只写一句
  function meterHTML(a) {
    const m = a.meter;
    if (!m) return "";
    if (m.kind === "none") return `<span class="mt none">${esc(m.main)}</span>`;
    if (m.kind === "money") return `<span class="mt money" title="${esc(m.sub || "")}"><em>${esc(m.main)}</em></span>`; // 余额只写金额，不画条
    if (m.kind === "usage") return `<span class="mt usage" title="${esc(m.sub || "")}"><em>${esc(m.main)}</em></span>`; // 只读得到用量（剩余读不到）：写用了多少
    const bar = (w, label) => { const pct = Math.max(0, Math.min(100, Math.round((w.remain || 0) * 100)));
      return `<span class="mt pct ${w.tone || ""}">${label ? `<u>${esc(label)}</u>` : ""}<i><b style="width:${pct}%"></b></i><em>${pct}%</em></span>`; };
    const ws = m.windows || [{ remain: m.remain, tone: m.tone }];
    const short = { "5 小时": "5h", "每周": "周" }; // 卡片窄：用服务端给的短名（最多 6 个字符），完整说法在鼠标提示里
    for (const w of ws) if (w.label) short[w.label] = w.short || short[w.label] || String(w.label).split("/")[0].slice(0, 6);
    return `<span class="mts" title="${esc(m.sub || "")}${m.stale ? "（读数是缓存）" : ""}">${ws.map(w => bar(w, ws.length > 1 || w.label ? short[w.label] || w.label : "")).join("")}</span>`;
  }
  function memHTML(a) {
    const r = runOf(a.id), task = r ? r.task : a.task;
    return `<div class="mem ${a.state}" data-role="${esc(a.id)}">${ring(a, dock === "top" ? 40 : 34)}
      <div class="tx"><div class="l1"><b>${esc(a.name)}</b><span class="st">${r ? `<span class="clock" data-since="${r.start}"></span>` : STATE[a.state].t}</span></div>
        ${hasCost(r) ? `<span class="qt live ${qcls(a)}" title="这一轮到现在的消耗，每 1–2 分钟更新">${esc(r.live.text.replace(/（.*$/, ""))}${r.live.rate ? " · " + esc(r.live.rate) : ""}</span>` : ""}
        ${meterHTML(a)}
        ${dock !== "top" && a.state === "working" && task ? `<span class="task">${U(task)}</span>` : ""}</div></div>`;
  }
  function actorHTML(a) {
    const p = { working: 0, manual: 0, break: 1 }[a.state] ?? 2;
    const fig = a.char && S.art.desk
      ? `<div class="lay v2"><div class="ch p${p}" style="background-image:url(${esc(a.char)})">${a.api && a.generic ? brandMark(a, "bd-ch") : ""}</div><img class="fr" src="${esc(p === 1 ? S.art.desk_nomug || S.art.desk : p === 2 ? S.art.desk_sleep || S.art.desk : S.art.desk_work || S.art.desk)}" alt="">${a.api && p === 0 ? brandMark(a, "bd-lid") : ""}</div>`
      : a.desk ? `<div class="lay"><div class="ch p${p}" style="background-image:url(${esc(a.desk)})"></div></div>`
      : `<div class="lay bare">${a.avatar ? `<img src="${esc(a.avatar)}" alt="">` : robot(a.color)}</div>`;
    return `<div class="actor ${a.state}" data-role="${esc(a.id)}">${fig}<span class="nm"><b><i style="background:${STATE[a.state].c}"></i>${esc(a.name)}</b>${a.quota_text ? `<small>${esc(shortQ(a.quota_text))}</small>` : ""}</span></div>`;
  }
  function sideHTML() {
    const done = doneCard ? [doneCard] : [];
    const recent = S.recent.filter(r => !doneCard || r.t !== doneCard.t || r.agent !== doneCard.agent).slice(0, 8);
    const q = (key, a, r1, r2, cls = "", tail = "") => `<div class="q ${cls} ${openMsg === key ? "open" : ""}" data-msg="${esc(key)}">${avatar(a, 22)}<div class="m"><div class="r1"><b>${esc(a.name)}</b>${r1}</div><div class="r2">${esc(r2)}</div></div>${tail}</div>`;
    const cost = r => costText(r.cost) ? `<span class="cost ${r.cost.cny != null ? "gold" : ""}">${costText(r.cost)}</span>` : "";
    const rows = done.map(r => q("d" + r.t, A(r.agent), cost(r), r.text, "done", `<span class="check">${icon("check")}</span>`))
      .concat(dock === "top" ? S.running.map(r => q("r" + r.id, A(r.agent), `<span class="clock" data-since="${r.start}"></span>`, (r.queued ? "⏳ " : "") + (r.retry ? `[重试 ${r.retry}] ` : "") + r.task)) : [])
      .concat(recent.map(r => q("c" + r.t + r.agent, A(r.agent), cost(r), r.text)));
    return rows.length ? `<div class="sec">${S.running.length && dock === "top" ? "进行中 / 刚完成" : "刚完成"}</div>` + rows.join("") : `<div class="idle">派活后会自动弹出来</div>`;
  }
  // ---------- 标签页：队员 / 任务 / 战报；点角色 → 快查卡 ----------
  let tab = "crew", role = null, quests = null, bill = null, fetchedAt = 0;
  const catName = {};
  fetch("/api/catalog").then(r => r.json()).then(c => c.categories.forEach(x => (catName[x.id] = x.name))).catch(() => {});
  async function refreshData(force) {
    if (!force && Date.now() - fetchedAt < 12000) return;
    fetchedAt = Date.now();
    const [q, b] = await Promise.all([fetch("/api/quests").then(r => r.json()).catch(() => []),
      fetch("/api/bill?batch=" + encodeURIComponent(S.batch || "")).then(r => r.json()).catch(() => null)]);
    quests = q; bill = b; if (open) render();
  }
  const today = () => new Date().toISOString().slice(0, 10);
  const mins = s => s == null ? "—" : s < 60 ? Math.round(s) + " 秒" : s < 3600 ? Math.round(s / 60) + " 分" : (s / 3600).toFixed(1) + " 时";
  const money = v => "¥" + Number(v || 0).toFixed(2);
  function crewTab() {
    const cr = crew(), bg = S.art[dock === "top" ? "hud_wide" : "hud_tall"];
    const more = dock !== "top" && cr.length > 6 ? `<div class="more-hint">往下滑还有 ${cr.length - 6} 位</div>` : "";
    return `<div class="crew">${cr.map(memHTML).join("")}</div><div class="side">${sideHTML()}</div>`;
  }
  function questsTab() {
    const list = (quests || []).slice(0, 30);
    const row = (key, a, r1, r2, extra = "") => `<div class="q ${openMsg === key ? "open" : ""}" data-msg="${esc(key)}">${avatar(a, 22)}<div class="m"><div class="r1"><b>${esc(a.name)}</b>${r1}</div><div class="r2">${esc(r2)}</div>${extra}</div></div>`;
    const run = S.running.map(r => { const a = A(r.agent); return row("r" + r.id, a, `<span class="clock" data-since="${r.start}"></span>${hasCost(r) ? `<span class="cost ${r.live.cny != null ? "gold" : ""}">${esc(liveText(r))}</span>` : ""}`, (r.queued ? "⏳ " : "") + (r.retry ? `[重试 ${r.retry}] ` : "") + r.task,
      a.acts && a.acts[0] ? `<div class="r3">${esc(a.acts[0].plain)}</div>` : ""); }).join("");
    const done = list.filter(q => q.done).map(q => row("q" + q.run, A(q.agent), `<span class="cost ${q.cost && q.cost.cny != null ? "gold" : ""}">${esc(costText(q.cost) || mins(q.seconds))}</span>`,
      (q.task || "").split("\n")[0], openMsg === "q" + q.run && q.reply ? `<div class="r3">${U(q.reply)}</div>` : "")).join("");
    return `<div class="list">${run ? `<div class="sec">进行中</div>${run}` : ""}${done ? `<div class="sec">已完成</div>${done}` : quests ? "" : `<div class="idle">读取中…</div>`}${!run && !done && quests ? `<div class="idle">这个项目还没有任务</div>` : ""}</div>`;
  }
  function reportTab() {
    if (!bill) return `<div class="list"><div class="idle">读取中…</div></div>`;
    const per = bill.per_agent || [];
    const sum = k => per.reduce((s, r) => s + (r[k] || 0), 0);
    const runs = sum("runs"), mvp = per.slice().sort((x, y) => y.runs - x.runs)[0], max = Math.max(1, ...per.map(r => r.runs));
    const tile = (k, v, cls = "") => `<div class="tl ${cls}"><span>${k}</span><b>${v}</b></div>`;
    return `<div class="rep"><div class="tiles">${tile("金币", money(bill.total_cny), "gold")}${tile("GPT 体力", sum("weekly") ? "-" + sum("weekly").toFixed(0) + "%" : "—", "hp")}${tile("任务", runs)}${tile("用时", mins(sum("seconds")))}</div>
      ${sum("credits") > 0.01 ? `<div class="warn">付费 credits -${sum("credits").toFixed(1)}</div>` : ""}
      ${mvp ? `<div class="mvp">👑 MVP · ${esc(A(mvp.agent).name)}<span>${mvp.runs} 个任务</span></div>` : `<div class="idle">${S.batch ? "这一批" : "这个项目"}还没有记账的任务</div>`}
      <div class="bars">${per.slice(0, 6).map(r => { const a = A(r.agent); return `<div class="br">${avatar(a, 18)}<span class="n">${esc(a.name)}</span><i style="width:${r.runs / max * 100}%"></i><span class="v">${r.cny ? money(r.cny) : r.weekly ? "-" + r.weekly.toFixed(0) + "%" : r.runs + " 次"}</span></div>`; }).join("")}</div>
      <button class="more" data-go="report">完整战报 ${icon("chev")}</button></div>`;
  }
  function roleTab(a) {
    const r = runOf(a.id), mine = (quests || []).filter(q => q.agent === a.id);
    const todayDone = mine.filter(q => q.done && (q.done || "").slice(0, 10) === today());
    const b = ((bill && bill.per_agent) || []).find(x => x.agent === a.id) || {}; // 合并计算过的账单，不会重复算
    const skills = Object.entries(a.jobs || {}).filter(([, v]) => v !== "avoid").slice(0, 3).map(([k]) => `<span class="chip">${esc(k.startsWith("cat:") ? (catName[k.slice(4)] || k.slice(4)) + "·整类" : k.slice(4))}</span>`).join("");
    const now = r ? `<div class="now"><span class="clock" data-since="${r.start}"></span><b>${U(r.task)}</b>${a.acts && a.acts[0] ? `<span>${esc(a.acts[0].plain)}</span>` : ""}</div>`
      : `<div class="now idle-now"><b>${STATE[a.state].t}</b>${a.acts && a.acts[0] ? `<span>上一步：${esc(a.acts[0].plain)}</span>` : ""}</div>`;
    const acts = [a.open && a.open.app ? `<button class="fb primary" data-openrole="app">${icon("open")}桌面端</button>` : "",
      a.open && a.open.cli && !r ? `<button class="fb" data-openrole="cli">${icon("quests")}CLI</button>` : "",
      a.installed ? `<button class="fb" data-moverole="${a.place === "office" ? "lounge" : "office"}">${a.place === "office" ? "去休息" : "上阵"}</button>` : "",
      `<button class="fb" data-go="hero/${encodeURIComponent(a.id)}">战备 ${icon("chev")}</button>`].join("");
    return `<div class="role"><button class="back" data-back title="返回">${icon("chev")}</button>
      <div class="rh">${ring(a, dock === "top" ? 46 : 40)}<div><b>${a.id === S.lead ? "👑 " : ""}${esc(a.name)}</b><span>${esc(a.vendor || "")}${a.place === "office" ? " · 上阵中" : " · 休息室"}</span>
        ${a.quota_text ? `<span class="qt ${qcls(a)}">${esc(shortQ(a.quota_text))}</span>` : ""}</div></div>
      ${now}
      <div class="today"><div><span>今天</span><b>${todayDone.length} 个</b></div><div><span>用时</span><b>${mins(todayDone.reduce((s, q) => s + (q.seconds || 0), 0))}</b></div><div><span>${S.batch ? "本批消耗" : "项目消耗"}</span><b>${a.billing === "deepseek" ? money(b.cny) : a.billing === "codex" ? (b.weekly ? "-" + b.weekly.toFixed(0) + "%" : b.runs ? "<1%" : "—") : "—"}</b></div></div>
      ${r ? jobHTML(a, r) : lastHTML(a)}${skills ? `<div class="skills">${skills}</div>` : ""}<div class="ra">${acts}</div></div>`;
  }
  // 这单的工作过程：从它自己的实时输出里读（所有队员通用），可以补充、停下改、停止、接管（在终端里接着这个会话聊）
  let jobView = null, jobLast = null, jobLastFor = null, jobLastT = 0, jobDraft = "", jobCut = false;
  const JK = { 想: "think", 做: "do", 说: "say", 结果: "ok", 错: "err", 补充: "me" };
  // 时间线：最新的在最上面
  const evRows = evs => evs.slice(-30).reverse().map(e => `<div class="jv-e ${JK[e.k] || ""}" ${e.detail ? `title="${esc(e.detail.slice(0, 600))}"` : ""}><span>${esc(e.t)}</span><i>${esc(e.k)}</i><p data-notr>${esc(e.text)}</p></div>`).join("");
  // 底下永远有一个输入框 + 发送：在干活时 = 补充给它（勾上“立刻打断”就马上停下这一轮按你说的改）；闲着时 = 直接派一单新活给它
  function composer(a, r) {
    const can = a.installed && a.billing !== "manual" && !a.sealed;
    if (!can) return `<div class="jv-note">${a.sealed ? `${esc(a.name)} 封印中，不接活` : `${esc(a.name)} 不能在这里派活`}</div>${noteHTML2()}`;
    return `<div class="jv-in"><input id="job-text" placeholder="${r ? `对 ${esc(a.name)} 补充…（回车发送，这一轮做完就照着改）` : `给 ${esc(a.name)} 派个活…（回车发送）`}" value="${esc(jobDraft)}">
        <button class="send" data-jsend>发送</button>${r ? `<button class="stop" data-jstop title="整单停掉，主位会收到“主人停了”">${sure("stop") ? "再点确认" : "停止"}</button>` : ""}</div>${noteHTML2()}
      ${r ? `<div class="jv-opt"><label><input type="checkbox" id="job-cut" ${jobCut ? "checked" : ""}> 立刻打断，按这句马上改</label>${(jobView && jobView.id === r.id && jobView.can_takeover) ? `<button data-jtake title="停掉后台这单，在终端里接着它的会话亲自聊">${sure("take") ? "再点确认" : "在终端接管"}</button>` : ""}</div>` : ""}`;
  }
  function jobHTML(a, r) {
    const j = jobView && jobView.id === r.id ? jobView : null;
    const evs = j ? j.events : null;
    const list = !evs ? `<div class="jv-empty">读取中…</div>` : evs.length ? evRows(evs)
      : `<div class="jv-empty">${j && !j.streams ? `${esc(a.name)} 跑完才出结果，过程看不到；可以照常补充或停止` : "还没有动静"}</div>`;
    const retry = j && j.retry && j.stalled ? `<div class="jv-note"><b>看门狗第 ${j.retry} 次重试</b> <span data-notr>${esc(j.stalled)}</span></div>` : "";
    return `<div class="jv"><div class="jv-h"><b>工作过程</b>${j && j.brain ? `<em>${esc(j.brain)}</em>` : ""}${j && j.round > 1 ? `<em>第 ${j.round} 轮</em>` : ""}</div>
      ${composer(a, r)}${retry}<div class="jv-l">${list}</div></div>`;
  }
  // 没在干活：给看它上一单的过程（只读），输入框直接派新活
  function lastHTML(a) {
    const l = jobLast && jobLast.agent === a.id ? jobLast : null;
    const head = l ? `<b>上一单的过程</b><em>${new Date(l.end * 1000).toLocaleTimeString().slice(0, 5)} 结束</em>` : `<b>工作过程</b>`;
    const body = !l ? `<div class="jv-empty">${jobLast === false ? "还没派过活" : "读取中…"}</div>`
      : (l.events || []).length ? evRows(l.events) : `<div class="jv-empty">这一单没有留下过程记录</div>`;
    const reply = l && l.kind === "codebuddy" && l.reply ? `<details class="jv-reply"><summary>查看本单回复</summary><p data-notr>${esc(l.reply)}</p></details>` : "";
    return `<div class="jv ${l ? "done" : ""}"><div class="jv-h">${head}</div>${composer(a, null)}${reply}<div class="jv-l">${body}</div></div>`;
  }
  async function loadJob() {
    const r = open && role ? runOf(role) : null;
    if (!r) {
      if (!(open && role) || (jobLast !== null && jobLastFor === role && Date.now() - jobLastT < 15000)) return;
      const d = await fetch("/api/job?agent=" + encodeURIComponent(role)).then(x => x.json()).catch(() => null);
      if (!d || !(open && role)) return;
      const same = JSON.stringify(d.last || false) === JSON.stringify(jobLast) && jobLastFor === role;
      jobLast = d.last || false; jobLastFor = role; jobLastT = Date.now();
      if (same) return;
      const had = document.activeElement && document.activeElement.id === "job-text", pos = had ? document.activeElement.selectionStart : 0;
      panel.dataset.html = ""; render();
      const i = document.getElementById("job-text"); if (had && i) { i.focus(); i.setSelectionRange(pos, pos); }
      return;
    }
    const d = await fetch("/api/job?id=" + encodeURIComponent(r.id)).then(x => x.json()).catch(() => null);
    if (!d) return;
    const before = JSON.stringify(jobView);
    jobView = d.job;
    if (JSON.stringify(jobView) === before || !(open && role)) return;
    const had = document.activeElement && document.activeElement.id === "job-text", pos = had ? document.activeElement.selectionStart : 0;
    const l0 = panel.querySelector(".jv-l"), keep = l0 ? l0.scrollTop : 0; // 最新在上：用户往下翻旧记录时别把他拽回顶部
    panel.dataset.html = ""; render();
    const l = panel.querySelector(".jv-l"); if (l) l.scrollTop = keep;
    const i = document.getElementById("job-text"); if (had && i) { i.focus(); i.setSelectionRange(pos, pos); }
  }
  setInterval(loadJob, 2000);
  panel.addEventListener("toggle", e => { if (open && e.target.matches(".jv-reply")) layout(); }, true);
  // 提示和“再点确认”都存在变量里、跟着重画：面板每 2 秒刷新也不会把它们冲掉
  let jobNote = null, jobSure = null;
  function jobMsg(t, issue = null) {
    const duration = issue ? 20000 : 5000;
    jobNote = { t, issue, until: Date.now() + duration };
    panel.dataset.html = ""; render();
    setTimeout(() => { if (jobNote && Date.now() >= jobNote.until) { jobNote = null; panel.dataset.html = ""; render(); } }, duration + 100);
  }
  const noteHTML2 = () => jobNote && Date.now() < jobNote.until ? `<div class="jv-msg" role="status">${esc(jobNote.t)} ${AB.issueAction(jobNote.issue, true)}</div>` : "";
  const sure = k => jobSure && jobSure.k === k && Date.now() - jobSure.t < 3000;
  async function jobAct(path, body, done) {
    const r = runOf(role); if (!r) return;
    const res = await fetch(path, { method: "POST", body: JSON.stringify({ id: r.id, ...body }) }).then(x => x.json()).catch(() => ({ error: "连不上本地服务" }));
    if (res.error) { jobMsg(res.error); return; }
    if (done) done();
    loadJob();
  }
  // 消息：和主界面「消息」页同一个接口——@ 队员就是派活，回复自动发回群里
  let chatData = null, chatRev = null, chatDraft = "";
  const loadChat = () => fetch("/api/chat?channel=" + encodeURIComponent(chatData ? chatData.channel : "大厅") + "&n=8").then(r => r.json()).then(d => {
    chatData = d;
    if (!(open && tab === "chat")) return;
    const had = document.activeElement && document.activeElement.id === "hc-text";
    if (had) document.activeElement.blur();
    panel.dataset.html = ""; render();
    const i = document.getElementById("hc-text");
    if (had && i) { i.focus(); i.setSelectionRange(i.value.length, i.value.length); }
    const l = panel.querySelector(".hc-list"); if (l) l.scrollTop = 1e9;
  }).catch(() => {});
  function chatTab() {
    if (!chatData) { loadChat(); return `<div class="list"><div class="idle">读取中…</div></div>`; }
    const nm = id => id === "user" ? "我" : A(id).name;
    const rows = chatData.messages.slice(-6).map(m => m.system ? `<div class="cmsg sys">${esc(m.text)} ${AB.issueAction(m.issue, true)}</div>`
      : `<div class="cmsg ${m.frm === "user" ? "me" : ""}">${m.frm === "user" ? "" : avatar(A(m.frm), 20)}<div><b>${esc(nm(m.frm))}</b><span data-notr>${esc(m.text)}</span></div></div>`).join("");
    const ats = S.agents.filter(a => a.place === "office" && a.installed && a.billing !== "manual" && !a.sealed).map(a => `<button data-hat="${esc(a.name)}">${avatar(a, 18)}</button>`).join("");
    return `<div class="hchat"><div class="hc-h"># ${esc(chatData.channel)}<button class="ib" data-go="chat" title="打开消息页">${icon("open")}</button></div>
      <div class="hc-list">${rows || `<div class="idle">还没有消息，@ 队员就能派活</div>`}</div>
      <div class="hc-ats">${ats}</div>
      <div class="hc-in"><input id="hc-text" placeholder="@GPT 帮我…（回车发送）" value="${esc(chatDraft)}"><button data-hsend>${icon("chev")}</button></div>${noteHTML2()}</div>`;
  }
  async function hudSend() {
    const inp = document.getElementById("hc-text"); const text = (inp && inp.value || "").trim(); if (!text) return;
    const res = await fetch("/api/chat/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ channel: chatData ? chatData.channel : "大厅", text }) }).then(x => x.json()).catch(() => null);
    if (!res || !res.ok) return jobMsg(res && res.error || "连不上聊天室。请先确认消息是否已发出。");
    if (!(res.issues && res.issues.length && !(res.jobs && res.jobs.length)) && chatDraft.trim() === text) {
      chatDraft = "";
      inp.value = "";
    }
    loadChat();
  }
  panel.addEventListener("click", e => {
    const action = e.target.closest("[data-dispatch-action]");
    if (action) {
      const agent = action.dataset.dispatchAgent;
      const desktop = action.dataset.dispatchAction === "desktop";
      if (desktop && !window.confirm(`要改用 ${A(agent).name} 桌面端吗？打开后请在桌面端继续交代任务。`)) return;
      fetch(desktop ? "/api/open" : "/api/login", { method: "POST", body: JSON.stringify({ agent, how: "app" }) })
        .then(r => r.json()).then(r => jobMsg(r.error || r.note || (desktop ? "已打开桌面端" : "已打开命令行登录")))
        .catch(() => jobMsg("暂时无法打开，请在队员页重试。"));
      return;
    }
    const at = e.target.closest("[data-hat]");
    if (at) { const inp = document.getElementById("hc-text"); inp.value = (inp.value ? inp.value.replace(/\s*$/, " ") : "") + "@" + at.dataset.hat + " "; chatDraft = inp.value; inp.focus(); }
    if (e.target.closest("[data-hsend]")) hudSend();
  });
  panel.addEventListener("input", e => { if (e.target.id === "hc-text") chatDraft = e.target.value; if (e.target.id === "job-text") jobDraft = e.target.value; });
  panel.addEventListener("keydown", e => {
    if (e.target.id === "hc-text" && e.key === "Enter" && !e.isComposing) { e.preventDefault(); hudSend(); }
    if (e.target.id === "job-text" && e.key === "Enter" && !e.isComposing) { e.preventDefault(); jobSend(); }
  });
  async function jobSend() {
    const t = jobDraft.trim(); if (!t || jobSubmitting) return;
    const a = A(role), r = runOf(role);
    if (r) return jobAct("/api/job/note", { text: t, interrupt: jobCut }, () => { jobDraft = ""; jobMsg(jobCut ? "已打断，按你说的接着做" : "发过去了，这一轮做完就照着改"); jobCut = false; });
    // 闲着：走聊天室的 @ 派活（和消息页同一条路），回复会发回大厅
    jobSubmitting = true;
    const res = await fetch("/api/chat/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ channel: "大厅", agent: a.id, text: `@${a.name} ${t}` }) }).then(x => x.json()).catch(() => ({ error: "连不上聊天室。请确认大厅是否已收到这条消息。" }));
    jobSubmitting = false;
    if (role !== a.id) return;
    if (res.error) return jobMsg(res.error);
    if (!res.jobs || !res.jobs.length) {
      const issue = (res.issues || [])[0];
      return jobMsg(issue ? issue.text : "这单没派出去。请到聊天室大厅查看原因。", issue);
    }
    if (jobDraft.trim() === t) { jobDraft = ""; const i = document.getElementById("job-text"); if (i) i.value = ""; }
    jobMsg(`已提交给 ${a.name}，等待启动。回复会发到聊天室大厅。`);
  }
  let jobSubmitting = false;
  panel.addEventListener("change", e => { if (e.target.id === "job-cut") jobCut = e.target.checked; });
  panel.addEventListener("click", e => {
    if (e.target.closest("[data-jsend]")) return jobSend();
    const st = e.target.closest("[data-jstop], [data-jtake]");
    if (st) { // 点两下才生效，免得误触
      const k = st.matches("[data-jstop]") ? "stop" : "take";
      if (!sure(k)) { jobSure = { k, t: Date.now() }; panel.dataset.html = ""; render(); setTimeout(() => { if (jobSure && Date.now() - jobSure.t >= 3000) { jobSure = null; panel.dataset.html = ""; render(); } }, 3100); return; }
      jobSure = null;
      jobAct(st.matches("[data-jstop]") ? "/api/job/stop" : "/api/job/takeover", {}, () => jobMsg(st.matches("[data-jstop]") ? "已停掉，主位会收到通知" : "终端打开了，后台这单已停")); return;
    }
  });
  function officeTab(o) {
    const gs = o.groups.filter(g => g.active).concat(o.groups.filter(g => !g.active).slice(0, 3)); // 悬浮窗里只放在干活的 + 最近 3 个
    const ago = t => { const m = Math.round((Date.now() / 1000 - t) / 60); return m < 60 ? `${Math.max(1, m)} 分钟前` : m < 1440 ? `${Math.round(m / 60)} 小时前` : `${Math.round(m / 1440)} 天前`; };
    const row = (g, id) => {
      const a = A(id), r = S.running.find(x => x.agent === id && x.room === g.id);
      const act = r ? r.task : a.state === "working" ? ((a.acts && a.acts[0] && a.acts[0].plain) || "在干活") : id === o.agent ? "等队员交活" : STATE[a.state].t;
      return `<div class="om-r" data-role="${esc(id)}"><span class="om-av">${avatar(a, 22)}<i style="background:${(STATE[a.state] || STATE.off).c}"></i></span><b>${id === o.agent && g.members.length > 1 ? "👑 " : ""}${esc(a.name)}</b><span>${esc(act)}</span>${r ? `<span class="clock" data-since="${r.start}"></span>` : ""}</div>`;
    };
    return `<div class="office"><div class="of-h"><button class="back" data-back title="返回">${icon("chev")}</button>${avatar(A(o.agent), 30)}
        <div><b>${esc(o.name)}</b><span>${gs.filter(g => g.active).length} 个窗口在干活 · 今天派活 ${o.today} 单</span></div>
        <button class="fb" data-go="office/${encodeURIComponent(o.id)}">主界面看 ${icon("chev")}</button></div>
      ${gs.map(g => `<div class="og-c ${g.active ? "" : "idle"}"><div class="og-h"><i></i>${esc(g.direct ? "直接在用" : g.title)}${g.active ? "" : `<em>${ago(g.last)}</em>`}</div>${g.active ? g.members.map(id => row(g, id)).join("") : ""}</div>`).join("")}</div>`;
  }
  function panelHTML() {
    const ra = role && S.agents.find(a => a.id === role);
    const of = !ra && officeSel && (S.offices || []).find(o => o.id === officeSel);
    const body = ra ? roleTab(ra) : of ? officeTab(of) : tab === "chat" ? chatTab() : tab === "quests" ? questsTab() : tab === "report" ? reportTab() : crewTab();
    const tabs = [["crew", "队员", "team"], ["quests", "任务", "quests"], ["report", "战报", "report"]]
      .map(([k, t, ic]) => `<button class="${tab === k && !ra ? "on" : ""}" data-tab="${k}">${icon(ic)}<span>${t}</span></button>`).join("");
    return `<div class="ph">${LOGO}<div class="tt">agentbus<span>${S.lead ? esc(A(S.lead).name) + " 办公室" : ""}</span></div>
        ${dock === "top" ? `<div class="tabs">${tabs}</div>` : ""}
        ${tab === "crew" && !ra ? `<div class="vt" title="收起时的样子"><button class="${view === "avatar" ? "on" : ""}" data-view="avatar" title="收起时：胶囊">${icon("pill")}</button><button class="${view === "scene" ? "on" : ""}" data-view="scene" title="收起时：小人">${icon("team")}</button></div>` : ""}
        ${S.lead && !A(S.lead).sealed ? `<button class="ib lead-btn" data-openlead title="打开主位 ${esc(A(S.lead).name)}">${avatar(A(S.lead), 20)}</button>` : ""}
        <button class="ib" data-go="base" title="打开 agentbus 主界面">${icon("base")}</button><button class="ib" data-close title="收起">${icon("collapse")}</button></div>
      <div class="body tab-${ra ? "role" : of ? "office" : tab} ${view === "scene" ? "v-scene" : ""}">${body}</div>
      ${dock !== "top" ? `<div class="foot tabs">${tabs}</div>` : ""}`;
  }
  // 新开的对话窗口：问主人要不要接入 bus（钩子报上来的，见 agentbus hook session）
  function noteHTML() {
    const n = joinNote, d = n.delivery || {}, nm = esc(n.name || d.name || "这个窗口");
    const x = `<button class="x" data-jn-close title="关掉">✕</button>`;
    if (d.via === "hook") return `<div class="ask note ok"><span class="t"><b>✅ ${nm} 已接入</b><i>在那个窗口发下一句话，它就会收到 agentbus 的规矩</i></span>${x}</div>`;
    if (n.installed) return `<div class="ask note ok"><span class="t"><b>✅ 钩子装好了</b><i>${esc(n.installed)}</i></span>${x}</div>`;
    return `<div class="ask note"><span class="t"><b>已记下：${nm} 接入</b><i>${n.err ? esc(n.err) : "它还收不到握手，选一个办法 ↓"}</i></span>
      <span class="ask-b">${d.can_hook ? `<button class="y" data-jn-install>装钩子</button>` : ""}<button class="n" data-jn-manual>手动配置</button>
      <button class="n" data-jn-copy title="复制后粘贴到那个对话框发送">复制口令</button></span>${x}</div>`;
  }
  function askHTML() {
    if (joinNote && !open) return noteHTML();
    const q = (S.join_ask || [])[0];
    if (!q || open) return "";
    const a0 = A(q.agent || "claude"), a = q.name && a0.name === a0.id ? { ...a0, name: q.name } : a0; // 名册里没有的（比如反重力）用检测时带来的名字
    const dir = String(q.cwd || "").split("/").filter(Boolean).pop() || "", sub = q.first ? clip(q.first, 22) : dir; // 先给第一句，认得出是哪个窗口
    // 短：头像、谁、第一句的前几个字、接入 / 不了（“以后都自动接入”在设置里）；侧边停靠时竖着排
    return `<div class="ask" data-conv="${esc(q.conv)}">${avatar(a, 22)}<span class="t"><b>${esc(a.name)} 新对话</b>${sub ? `<i data-notr>${esc(clip(sub, 16))}</i>` : ""}</span>
      <span class="ask-b"><button class="y" data-join="1">接入</button><button class="n" data-join="0">不了</button></span></div>`;
  }
  function setNote(n) { // 换卡 / 关卡都走这里：先清掉旧卡的定时器，旧定时器不会关掉新卡
    if (joinNote) clearTimeout(joinNote.timer);
    joinNote = n; render();
  }
  async function copyText(t) { // Tauri 的网页里剪贴板接口偶尔不给用：退回老办法
    try { await navigator.clipboard.writeText(t); return true; } catch (e) {
      const ta = document.createElement("textarea"); ta.value = t; ta.style.position = "fixed"; ta.style.opacity = "0"; document.body.appendChild(ta); ta.select();
      const ok = document.execCommand("copy"); ta.remove(); return ok; }
  }
  askEl.addEventListener("click", async e => {
    if (!joinNote) return;
    const d = joinNote.delivery || {};
    if (e.target.closest("[data-jn-close]")) { setNote(null); return; }
    if (e.target.closest("[data-jn-copy]")) {
      const btn = e.target.closest("[data-jn-copy]");
      if (await copyText(d.phrase || "")) { btn.textContent = "已复制，去对话框粘贴发送"; btn.classList.add("done"); }
      return;
    }
    if (e.target.closest("[data-jn-manual]")) { invoke("open_main", { page: "settings/connect" }); return; }
    if (e.target.closest("[data-jn-install]")) {
      const me = joinNote; // 装钩子要一会儿：回来时卡片已经关了 / 换成别的窗口的，就不动它
      const r = await fetch("/api/connectors/install", { method: "POST", body: JSON.stringify({ id: d.connector }) }).then(x => x.json()).catch(() => ({ error: "装失败了" }));
      if (joinNote !== me) return;
      if (r.error) { me.delivery = { ...d, can_hook: false }; me.err = r.error; }
      else me.installed = r.note || "装好了";
      render();
    }
  });
  askEl.addEventListener("click", e => {
    const b = e.target.closest("[data-join]"); if (!b) return;
    const box = askEl.querySelector(".ask"), always = null; // 「以后都自动接入」挪到设置里了
    const accept = b.dataset.join === "1";
    fetch("/api/join", { method: "POST", body: JSON.stringify({ conv: box.dataset.conv, accept, always: always && always.checked ? true : undefined }) })
      .then(r => r.json()).then(r => { // 接入后一定给反馈：装了钩子说清楚什么时候生效；没装给兜底选项
        if (!accept || !r.delivery) return;
        setNote({ delivery: r.delivery, name: r.name || A(r.delivery.connector).name, t: Date.now() });
        if (r.delivery.via === "hook") { const me = joinNote; me.timer = setTimeout(() => { if (joinNote === me) setNote(null); }, 8000); }
      }).catch(() => {});
    box.classList.add("bye");
  });
  const MOON = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20.2 14.6A8.2 8.2 0 0 1 9.4 3.8a8.6 8.6 0 1 0 10.8 10.8z" fill="currentColor"/></svg>`;
  // 苹果控制中心那种：月亮圆标 + 「专注模式」，开着时圆标是靛蓝底；比胶囊矮一点，中线对齐
  const focusBtn = () => `<button class="fz ${focus() ? "on" : ""}" data-focus title="${focus() ? "专注模式已开：只保持收起，不自动弹出（点一下关）" : "专注模式：只保持收起，不自动弹出"}" aria-pressed="${focus()}"><span class="fz-ic">${MOON}</span><span class="fz-t">专注模式</span></button>`;
  // 贴着胶囊（或小人、接入卡片）实际露出来的那一截放，竖直方向和胶囊中线对齐；
  // 用 offset 尺寸，不受弹出动画的缩放影响
  function placePeek() {
    const rs = [...row.children, island].map(e => e.getBoundingClientRect()).filter(b => b.width && b.height); // 胶囊和窗体取最外边，按钮不会压到任何一个
    const r = rs.length ? rs.reduce((a, b) => ({ left: Math.min(a.left, b.left), right: Math.max(a.right, b.right), top: Math.min(a.top, b.top), bottom: Math.max(a.bottom, b.bottom) }))
      : island.getBoundingClientRect();
    const pw = peekEl.offsetWidth || 84, ph = peekEl.offsetHeight || 28;
    const right = edge === "right";
    peekEl.style.left = Math.round(right ? r.left - pw - 8 : r.right + 8) + "px";
    const h = r.bottom - r.top, cy = h <= 64 ? (r.top + r.bottom) / 2 : r.top + 24; // 侧边长条：对齐最上面的头像
    peekEl.style.top = Math.round(Math.max(0, cy - ph / 2)) + "px";
    peekEl.dataset.side = right ? "l" : "r";
  }
  function showPeek() {
    clearTimeout(peekT);
    if (open || dragging) return;
    if (!peekOn) { peekOn = true; set(peekEl, focusBtn()); peekEl.hidden = false; placePeek(); }
    requestAnimationFrame(() => { if (peekOn && !open && !dragging) peekEl.classList.add("in"); }); // 这一帧之前面板打开了就不再显示
  }
  function hidePeek(now) {
    clearTimeout(peekT);
    const go = () => { peekEl.classList.remove("in"); peekOn = false; };
    if (now) { go(); peekEl.hidden = true; } else peekT = setTimeout(go, 220);
  }
  island.addEventListener("mouseenter", () => { if (!open) showPeek(); });
  island.addEventListener("mouseleave", () => hidePeek());
  peekEl.addEventListener("mouseenter", () => { clearTimeout(peekT); if (peekOn) peekEl.classList.add("in"); });
  peekEl.addEventListener("mouseleave", () => hidePeek());
  // 应用本体每 60 毫秒报一次光标（窗口不是当前窗口时网页自己收不到鼠标移入）：
  // 落在胶囊上就弹按钮；按钮和胶囊之间的缝也算在内，免得移过去时按钮先收了
  const within = (r, x, y, m = 0) => r.width && x >= r.left - m && x <= r.right + m && y >= r.top - m && y <= r.bottom + m;
  let hoverSeen = false;
  window.__hudHover = (x, y) => {
    if (!hoverSeen && x != null) { hoverSeen = true; report("hover 通道正常"); }
    if (x == null) { if (!open) hidePeek(); setThrough(false); return; }
    const onIsland = within(island.getBoundingClientRect(), x, y, 2);
    const onPeek = peekOn && within(peekEl.getBoundingClientRect(), x, y, 10);
    if (!open && !dragging) { if (onIsland || onPeek) showPeek(); else if (peekOn) hidePeek(); }
    // 透明留白处让点击穿过去；展开、拖动时整个窗口都接鼠标
    setThrough(!open && !dragging && !onIsland && !onPeek);
  };
  window.addEventListener("resize", placePeek);
  // 胶囊文字一变（轮播谁在干什么）宽度就变：按钮跟着挪，永远贴在胶囊外侧，不会压上去
  if (window.ResizeObserver) { const ro = new ResizeObserver(() => { if (peekOn) placePeek(); }); ro.observe(row); ro.observe(pill); ro.observe(island); }
  peekEl.addEventListener("click", e => {
    if (!e.target.closest("[data-focus]")) return;
    const on = !focus();
    S.settings.hud.focus = on;
    fetch("/api/settings", { method: "POST", body: JSON.stringify({ hud: { focus: on } }) });
    set(peekEl, focusBtn());
    document.body.classList.toggle("focus", on);
  });
  function badge() {
    const b = pill.querySelector(".badge");
    if (!b) return;
    const n = new Set([...S.running.map(r => r.agent), ...S.agents.filter(a => a.state === "working").map(a => a.id)]).size;
    const v = n ? String(n) : "";
    if (b.textContent !== v) b.textContent = v;
  }
  function set(el, html) { if (el.dataset.html !== html) { el.dataset.html = html; el.innerHTML = html; return true; } return false; }
  function render() {
    if (peekOn) requestAnimationFrame(placePeek); // 内容重画完再贴一次位置
    if (!S) return;
    set(pill, pillHTML());
    set(askEl, askHTML());
    badge();
    document.body.classList.toggle("multi", !open && multi());
    document.body.classList.toggle("asking", !open && asking());
    document.body.classList.toggle("focus", focus());
    if (open) {
      if (dock === "top") panel.style.setProperty("--pw", Math.max(640, crew().length * 96 + 262) + "px"); // 宽度只跟上阵人数走，切标签不变
      // 侧边场景：竖着最多放 6 位，每位的高度按屏幕高度算，保证 6 位一屏放得下；超过 6 位才滑动
      else panel.style.setProperty("--ah", Math.max(70, Math.min(104, Math.floor(((screen.availHeight || 860) - 300) / 6))) + "px");
      const typing = document.activeElement && document.activeElement.id === "hc-text"; // 工作过程的输入框由 loadJob 自己恢复焦点
      if (!typing) set(panel, panelHTML()); // 正在消息框里打字：别重画面板，不然焦点和输入法会被打断
    }
    document.body.classList.toggle("troupe", troupe());
    if (troupe() && !multi()) {
      if (!ABStage.el.isConnected) troupeEl.appendChild(ABStage.el);
      if (!open) ABStage.sync(S, { dock, edge: document.body.dataset.edge, idleLead: S.lead });
    } else if (ABStage.el.isConnected) ABStage.el.remove();
    tick();
    layout();
  }

  // ---------- 交互 ----------
  // 按下时记住点的是谁：拖动用了指针捕获，松手后的 click 目标会变成整排容器，不能再看 e.target
  let downOn = null;
  row.addEventListener("pointerdown", e => { downOn = e.target; }, true);
  row.addEventListener("click", e => {
    if (row.dataset.justDragged) return;
    const t = (downOn && row.contains(downOn) ? downOn : e.target);
    downOn = null;
    if (!t.closest || !t.closest(".pill, .fg, [data-office]")) return;
    const o = t.closest("[data-office]");
    officeSel = o ? o.dataset.office : null; role = null;
    setOpen(true, { pin: true });
  });
  panel.addEventListener("click", e => {
    if (e.target.closest("[data-openlead]")) { const a = A(S.lead); if (!a.sealed) fetch("/api/open", { method: "POST", body: JSON.stringify({ agent: a.id, how: a.open && a.open.app ? "app" : "cli" }) }); return; }
    const go = e.target.closest("[data-go]");
    if (go) { invoke("open_main", { page: go.dataset.go }); if (!T) location.href = "/#" + go.dataset.go; return; }
    if (e.target.closest("[data-close]") || (e.target.closest(".ph") && !e.target.closest("button"))) { doneCard = null; role = null; officeSel = null; setOpen(false, { pin: false }); return; }
    const tb = e.target.closest("[data-tab]");
    if (tb) { tab = tb.dataset.tab; role = null; openMsg = null; if (tab !== "crew") refreshData(true); render(); pinned = true; clearTimeout(closeTimer); return; }
    const rl = e.target.closest("[data-role]");
    if (rl) { role = rl.dataset.role; jobView = null; jobLast = null; jobDraft = ""; refreshData(); render(); loadJob(); pinned = true; clearTimeout(closeTimer); return; }
    if (e.target.closest("[data-back]")) { role = null; officeSel = null; render(); return; }
    const or = e.target.closest("[data-openrole]");
    if (or) { fetch("/api/open", { method: "POST", body: JSON.stringify({ agent: role, how: or.dataset.openrole }) }); return; }
    const mr = e.target.closest("[data-moverole]");
    if (mr) { fetch("/api/move", { method: "POST", body: JSON.stringify({ agent: role, place: mr.dataset.moverole }) }); return; }
    const m = e.target.closest("[data-msg]");
    if (m) { openMsg = openMsg === m.dataset.msg ? null : m.dataset.msg; render(); }
    const v = e.target.closest("[data-view]");
    if (v) { view = v.dataset.view; S.settings.hud.view = view; render(); fetch("/api/settings", { method: "POST", body: JSON.stringify({ hud: { view } }) }); }
    pinned = true; clearTimeout(closeTimer); // 用户在看，就不自动收起
  });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && open) setOpen(false, { pin: false }); });
  // 只有用户点开（窗口真的拿到过焦点）以后再点别处，才收起；刚展开那一下的焦点抖动不算
  let openedAt = 0, hadFocus = false;
  window.addEventListener("focus", () => { hadFocus = true; });
  window.__hudBlur = () => {
    if (!open || dragOff !== null || !hadFocus || Date.now() - openedAt < 600) return;
    hadFocus = false; doneCard = null; openMsg = null; role = null; setOpen(false, { pin: false });
  };
  window.addEventListener("blur", () => window.__hudBlur());
  // 悬浮窗窗口比看得见的面板大一圈（透明边）：点在透明处也算“点了别的地方”，收起来（侧边时尤其明显）
  document.addEventListener("pointerdown", e => {
    if (open && !e.target.closest(".island") && Date.now() - openedAt > 300) { doneCard = null; openMsg = null; role = null; setOpen(false, { pin: false }); }
  });

  // 展开时点了别的应用 / 桌面：悬浮窗不是当前窗口，网页收不到这次点击，由应用壳按系统鼠标计数报过来（x、y 是窗口里的位置，窗口外是 null）
  window.__hudPress = (x, y) => {
    if (!open || Date.now() - openedAt <= 300) return;
    if (x != null) { const el = document.elementFromPoint(x, y); if (el && el.closest(".island, #peek, #ask")) return; } // 点在面板上：交给网页自己处理
    doneCard = null; openMsg = null; role = null; setOpen(false, { pin: false });
  };
  // 拖动胶囊：可以拖到屏幕任意位置悬浮；松手时离边不到 48 像素就吸附到那条边（上 / 下 / 左 / 右）
  let drag = null, dragging = false;
  row.addEventListener("pointerdown", async e => {
    if (open || !e.target.closest(".pill, .fg, [data-office]")) return;
    drag = { x: e.screenX, y: e.screenY, moved: false, win: null }; row.setPointerCapture(e.pointerId);
    const p = await invoke("hud_pos"); if (drag && Array.isArray(p)) drag.win = p;
  });
  // 拖动中看指针离哪条屏幕边近：近了就当场贴过去、换成那条边的形状（左右变竖条，上下变横条），拉远了再变回悬浮
  const SNAP = 56;
  const nearEdge = (x, y) => {
    const L = screen.availLeft || 0, T = screen.availTop || 0, W = screen.availWidth, H = screen.availHeight;
    const d = [["left", x - L], ["right", L + W - x], ["top", y - T], ["bottom", T + H - y]].sort((p, q) => p[1] - q[1])[0];
    if (d[1] < 0 || d[1] >= SNAP) return null; // 指针已经出了这块屏幕（往另一块屏幕拖）：先悬浮跟过去，到了那块屏幕再按它的边吸附
    return { edge: d[0], off: Math.round(d[0] === "top" || d[0] === "bottom" ? x - (L + W / 2) : y - (T + H / 2)) };
  };
  function previewEdge(snap) { // 换形状 + 贴边（贴边位置跟着指针沿边滑）
    const key = snap ? snap.edge : "float";
    if (drag.shape !== key) {
      drag.shape = key;
      applyDock({ dock: key, float_y: drag.ny });
      document.body.classList.add("morph"); clearTimeout(drag.mt); drag.mt = setTimeout(() => document.body.classList.remove("morph"), 260);
      render();
    }
    // 拖动中也按新形状改窗口大小（以前悬浮时只挪位置不改大小：从侧边的窄高窗口拖出来，横着的胶囊只露出一小块）
    const t = measure(), side = key === "left" || key === "right", sz = winSize(t.w, t.h);
    if (snap) invoke("hud_resize", { w: Math.ceil(sz.w), h: Math.ceil(sz.h), dock: snap.edge, offset: snap.off, anchor: side ? sz.h : 0 });
    else invoke("hud_resize", { w: Math.ceil(sz.w), h: Math.ceil(sz.h), dock: "float", offset: 0, anchor: 34 + PAD + 4, cx: drag.px, cy: drag.py });
  }
  row.addEventListener("pointermove", e => {
    if (!drag || !drag.win) return;
    const dx = e.screenX - drag.x, dy = e.screenY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 5) return;
    if (!drag.moved) { hidePeek(true); setThrough(false); } // 一开始拖就把专注按钮收起来，不然它跟着窗口变形压到胶囊上
    drag.moved = true; dragging = true; row.classList.add("dragging");
    drag.px = e.screenX; drag.py = e.screenY;
    drag.nx = drag.win[0] + dx; drag.ny = drag.win[1] + dy;
    drag.snap = nearEdge(e.screenX, e.screenY);
    if (!drag.raf) drag.raf = requestAnimationFrame(() => { if (drag) { drag.raf = 0; previewEdge(drag.snap); } });
  });
  row.addEventListener("pointerup", async () => {
    if (!drag) return;
    const d = drag; drag = null; row.classList.remove("dragging");
    if (!d.moved || !d.win) { dragging = false; return; }
    row.dataset.justDragged = "1"; setTimeout(() => delete row.dataset.justDragged, 50);
    dragging = false;
    const hud = S.settings.hud, patch = d.snap ? { dock: d.snap.edge } : { dock: "float", float_x: Math.round(d.px), float_y: Math.round(d.py) };
    if (d.snap) patch[d.snap.edge === "top" || d.snap.edge === "bottom" ? "offset_top" : "offset_side"] = d.snap.off;
    Object.assign(hud, patch); lastBusy = Date.now();
    applyDock(hud); lastWin = null; render();
    fetch("/api/settings", { method: "POST", body: JSON.stringify({ hud: patch }) });
  });
  function applyDock(h) {
    // float 用宽条排版；在屏幕下半边时往上展开（像贴底部那样）
    const up = h.dock === "float" && Number(h.float_y) > (screen.availHeight || 900) / 2;
    edge = h.dock; dock = edge === "left" || edge === "right" ? edge : "top";
    document.body.dataset.dock = dock; document.body.dataset.edge = edge === "float" ? (up ? "bottom" : "top") : edge;
    pill.dataset.html = panel.dataset.html = "";
  }

  // 空闲计时：没人干活、也没人碰它。「干活」包括派出去的活，也包括你直接在用的 agent（从它的日志看出来在动）
  const busy = () => S && (S.running.length || S.agents.some(a => a.state === "working") || ABStage.busy());
  window.__hudWake = force => { lastBusy = Date.now(); if (force) { lastWin = null; winBox = { w: 0, h: 0 }; layout(); } if (ball || idleHidden) { ball = false; idleHidden = false; document.body.classList.remove("ball"); lastWin = null; render(); } };
  island.addEventListener("pointerdown", () => window.__hudWake());
  setInterval(() => {
    if (!S) return;
    const h = S.settings.hud, idle = (Date.now() - lastBusy) / 60000;
    if (busy() || open) { lastBusy = Date.now(); return; }
    const hideMin = Number(h.hide_min ?? 20), ballMin = Number(h.ball_min ?? 3);
    if (hideMin > 0 && idle >= hideMin && !idleHidden) { idleHidden = true; invoke("hud_visible", { on: false }); }
    else if (ballMin > 0 && idle >= ballMin && !ball) { ball = true; document.body.classList.add("ball"); lastWin = null; render(); }
  }, 15000);
  // 有好几个在干活时，胶囊每 4 秒换一个
  setInterval(() => { if (S && !open && (S.running.length > 1 || S.agents.filter(a => a.state === "working").length > 1)) { pill.dataset.html = ""; render(); } }, 4000);

  const QUIET = 5 * 60 * 1000;
  const lastWork = {};
  let helloSeen = 0;
  // 有 agent 接入：它要是还没在哪个单间里，就临时请上台打个招呼
  let hello = null;
  function greet(agent) {
    hello = { agent, until: Date.now() + 9000 };
    S = withHello(S);
    const r = ((S.rooms || []).find(x => x.members.includes(agent)) || {}).id || "hello:" + agent;
    if (troupe()) ABStage.effect("hello", { room: r, id: agent });
  }
  function withHello(s) {
    if (!hello || hello.until < Date.now() || (s.rooms || []).some(r => r.members.includes(hello.agent))) return s;
    const name = ((s.agents.find(a => a.id === hello.agent)) || {}).name || hello.agent;
    return { ...s, rooms: [...(s.rooms || []), { id: "hello:" + hello.agent, lead: hello.agent, members: [hello.agent], jobs: [], label: name + " · 刚接入" }] };
  }
  function onState(s) {
    if (s && s.settings) I18N.sync(s.settings.lang);
    s = withHello(s);
    const prev = S;
    S = s;
    if (s.chat_rev !== chatRev) { chatRev = s.chat_rev; if (open && tab === "chat") loadChat(); }
    const h = s.settings.hud;
    if (!prev || h.dock !== edge || (edge === "float" && (h.float_x !== (prev && prev.settings.hud.float_x) || h.float_y !== (prev && prev.settings.hud.float_y)))) { applyDock(h); lastWin = null; }
    view = h.view || "avatar";
    if ((busy() || (s.join_ask || []).length) && (ball || idleHidden)) window.__hudWake(); // 有人干活 / 有新对话要问：小球和藏起来的都醒过来
    if ((s.join_ask || []).length > ((prev && prev.join_ask) || []).length) invoke("hud_front");
    invoke("hud_visible", { on: h.enabled !== false && !idleHidden });
    const ids = new Set(s.running.map(r => r.id));
    const fx = (t, d) => { if (troupe() && !multi() && d.room) ABStage.effect(t, d); };
    const roomWith = id => ((s.rooms || []).find(r => r.lead === id && r.live) || (s.rooms || []).find(r => r.members.includes(id)) || {}).id;
    // 直接在用的 agent（不是派活）：安静了 5 分钟以上又开始干活，就弹出来
    const directNew = s.agents.filter(a => a.state === "working" && !s.running.some(r => r.agent === a.id) && Date.now() - (lastWork[a.id] || 0) > QUIET);
    s.agents.forEach(a => { if (a.state === "working") lastWork[a.id] = Date.now(); });
    // 有 agent 刚接入 agentbus（MCP 握手）
    const hi = s.hello && s.hello.t && s.hello.t > helloSeen && Date.now() / 1000 - s.hello.t < 30 ? s.hello : null;
    if (s.hello && s.hello.t) helloSeen = Math.max(helloSeen, s.hello.t);
    if (known) {
      const started = s.running.filter(r => !known.has(r.id));
      const finished = [...known].filter(id => !ids.has(id));
      started.forEach(r => fx("dispatch", { room: r.room, from: r.frm || s.lead, to: r.agent, text: r.task }));
      // 按这一单（run）找它自己的回复：同一位队员同时做好几单时不会拿别单的结果充数；回复还没到就先不出完成卡
      const replyOf = r => !r ? null : r.run ? s.recent.find(x => x.run === r.run) : s.recent.find(x => x.agent === r.agent); // 老记录没有 run：按队员
      finished.forEach(id => {
        const r = prev.running.find(x => x.id === id) || {}, rep = replyOf(r) || {};
        if (r.agent) fx("done", { room: r.room, from: r.agent, to: r.frm || s.lead, ok: rep.ok !== false });
      });
      if (finished.length && h.notify_done) {
        doneCard = finished.map(id => replyOf(prev.running.find(r => r.id === id))).find(Boolean) || null;
      }
      directNew.forEach(a => fx("start", { room: roomWith(a.id), id: a.id }));
      if (hi && hi.agent) greet(hi.agent);
      known = ids;
      const pop = started.length || (finished.length && h.notify_done) || (directNew.length && h.pop_direct !== false) || hi;
      if (pop && troupe()) window.__hudWake(); // 小人样式不弹面板：小人自己上场就是提醒
      else if (pop && h.autopop && !h.focus) { setOpen(true, { auto: true, pin: false }); return; } // 专注模式：有活也不自动展开
    } else if (hi) { known = ids; greet(hi.agent); if (troupe()) window.__hudWake(); else if (h.autopop && !h.focus) { setOpen(true, { auto: true, pin: false }); return; } }
    known = ids;
    render();
  }

  function connect() {
    const es = new EventSource("/api/events");
    es.onmessage = e => onState(JSON.parse(e.data));
    es.onerror = () => { if (!S) { pill.innerHTML = `<span class="txt">agentbus <span>正在连接本地服务…</span></span>`; layout(); } };
  }
  const tick = () => document.querySelectorAll("[data-since]").forEach(e => (e.textContent = clock(Date.now() / 1000 - Number(e.dataset.since))));
  window.__stageTick = () => { if (troupe() && !open) layout(); }; // 小人上场 / 退场后窗口跟着变大变小
  setInterval(tick, 1000);
  connect();
})();
