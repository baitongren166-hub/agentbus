// 悬浮窗「小人」：全透明，只有小人和必要的小图标，不放文字（说明放在悬停提示里），没有桌子。
// 小人按窗口分组（一个主位窗口一组：主位 + 它派出去的队员）；动作用办公动作表 acts-<角色>.png（接活 / 交成果 / 喝茶 / 埋头干 / 卡住想…）；
// 派活 / 交活时信封在组里飞，飞的时候才连一条线；干完活不马上走：交报告 → 喝咖啡 → 吃饭 / 打盹 → 挥手退场。
// 舞台是常驻节点，悬浮窗重绘时不打断动画；图用 v2/sm/ 的小图（原图解码太吃内存）。
(() => {
  const { esc, robot, clock } = AB;
  const W = 44;                          // 一格立绘（512×1024）缩到多宽
  const Y0 = 150, Y1 = 768;              // 只露出人：格子里 y 150～768（到桌面线为止），不画桌子；干活时只叠上桌上那台电脑
  const H = Math.round((Y1 - Y0) * W / 512), TOP = -Math.round(Y0 * W / 512);
  const LINGER = 36000;                  // 干完活多待多久再退场
  // 办公动作表 6 格：0 歇着看邻座 / 1 接活 / 2 交成果 / 3 喝茶发呆 / 4 埋头干 / 5 卡住想
  const ACT = { social: 0, ready: 1, report: 2, rest: 3, focus: 4, think: 5 };
  const sm = u => (u ? String(u).replace("/art/v2/", "/art/v2/sm/") : u);

  const el = document.createElement("div");
  el.className = "stage2";
  el.innerHTML = `<svg class="wires" aria-hidden="true"></svg><div class="rooms"></div><div class="fx"></div>`;
  const wires = el.querySelector(".wires"), roomsEl = el.querySelector(".rooms"), fx = el.querySelector(".fx");
  const rooms = new Map();   // 窗口组 id → { node, cast, last }
  const figs = new Map();    // "组|agent" → 小人
  const posed = new Map();   // key → { pose, until }  动画里临时摆的姿势
  const queue = [];
  let S = null, dock = "top", edge = "top", only = null, idleLead = null;

  const A = id => S.agents.find(a => a.id === id);
  const keyOf = (room, aid) => room + "|" + aid;
  const hasPoses = a => !!(a && a.frames && a.frames.poses);

  // ---------- 小人现在该是什么样子 ----------
  const hasActs = a => !!(a && a.frames && a.frames.acts);
  const seed = k => [...k].reduce((n, c) => n + c.charCodeAt(0), 0); // 每个人错开节奏，别一起换动作
  function look(f, a) {
    const now = Date.now(), p = posed.get(f.key);
    if (p && p.until > now) return hasActs(a) ? { kind: "act", i: ACT[p.pose] } : { kind: p.pose === "rest" ? "coffee" : "work" };
    if (f.idle) return { kind: "sleep" };
    if (f.leftAt) { // 干完了，退场前歇一会儿：交成果 → 喝茶发呆 → 看看邻座 → 再歇会儿
      const t = now - f.leftAt;
      if (hasActs(a)) return { kind: "act", i: t < 5000 ? ACT.report : t < 16000 ? ACT.rest : t < 28000 ? ACT.social : ACT.rest };
      return t < 4000 ? { kind: "work" } : t < 18000 ? { kind: "coffee" } : t < 30000 ? { kind: "sleep" } : { kind: "coffee" };
    }
    const beat = Math.floor((now + seed(f.key) * 997) / 6000);
    if (a.state === "queued") return hasActs(a) ? { kind: "act", i: ACT.rest } : { kind: "coffee" };
    if (a.state === "working") { // 干活：大多时候敲键盘，隔一阵埋头苦干、卡住想一想
      if (hasActs(a) && beat % 6 === 4) return { kind: "act", i: ACT.focus };
      if (hasActs(a) && beat % 6 === 5) return { kind: "act", i: ACT.think };
      return { kind: "work" };
    }
    // 主位活都派出去了、在等：喝茶，隔一阵看看邻座
    if (hasActs(a)) return { kind: "act", i: beat % 3 === 2 ? ACT.social : ACT.rest };
    return { kind: "coffee" };
  }
  function paint(f) {
    const a = A(f.aid);
    if (!a) return;
    const m = look(f, a), sig = JSON.stringify(m) + (a.char || a.avatar || "");
    if (f.sig === sig) return;
    f.sig = sig;
    const ch = f.ch.style;
    f.node.dataset.look = m.kind;
    ch.top = TOP + "px";
    // 敲键盘时面前放一台打开的电脑（桌子图里那台，桌面线以下裁掉）；埋头干 / 卡住想这两个动作自己带电脑
    const art = S.art || {}, meta = (a.frames && a.frames.acts_meta) || [];
    const pc = m.kind === "work" ? (art.desk_work || art.desk)
      : m.kind === "act" && (meta[m.i] || {}).desk === "clear" ? (art.desk_nomug || art.desk) : null; // 动作表里的“clear”= 电脑开着、杯子在人手上
    f.pc.style.top = TOP + "px";
    if (pc) { if (f.pcSrc !== pc) { f.pcSrc = pc; f.pc.src = sm(pc); } f.pc.hidden = false; } else f.pc.hidden = true;
    if (!a.char) { // 没有立绘：只放头像
      f.ch.innerHTML = a.avatar ? `<img src="${esc(a.avatar)}" alt="">` : robot(a.color);
      ch.backgroundImage = "";
      return;
    }
    f.ch.innerHTML = "";
    if (m.kind !== "act") { ch.scale = ""; ch.translate = ""; }
    if (m.kind === "work" && a.frames && a.frames.work) { // 有打字动画帧的（Claude）
      ch.backgroundImage = `url(${sm(a.frames.work)})`;
      ch.backgroundSize = `${W * 4}px ${W * 2}px`;
      ch.setProperty("--end", `${-W * 4}px`);
      f.ch.className = "ch anim";
    } else if (m.kind === "act") {
      const fit = a.frames.acts_fit || {};
      ch.scale = fit.scale || "";
      ch.translate = fit.dx ? `${(fit.dx * W / 512).toFixed(1)}px 0` : "";
      ch.backgroundImage = `url(${sm(a.frames.acts)})`;
      ch.backgroundSize = `${W * 6}px ${W * 2}px`;
      ch.backgroundPosition = `${-W * m.i}px 0`;
      f.ch.className = "ch act";
    } else {
      ch.backgroundImage = `url(${sm(a.char)})`;
      ch.backgroundSize = `${W * 3}px ${W * 2}px`;
      ch.backgroundPosition = `${-W * ({ work: 0, coffee: 1, sleep: 2 }[m.kind] ?? 1)}px 0`;
      f.ch.className = "ch";
    }
  }

  // ---------- 窗口组和小人 ----------
  function roomNode(rid) {
    let x = rooms.get(rid);
    if (!x) {
      const node = document.createElement("div");
      node.className = "room";
      node.innerHTML = `<div class="cast"></div>`;
      x = { node, cast: node.firstChild };
      rooms.set(rid, x);
    }
    return x;
  }
  function figure(room, aid) {
    const key = keyOf(room, aid);
    let f = figs.get(key);
    if (!f) {
      const node = document.createElement("div");
      node.className = "fg enter";
      node.dataset.role = aid;
      node.innerHTML = `<div class="bub"></div><div class="seat"><div class="ch"></div><img class="pc" alt=""></div><i class="mk"></i>`;
      f = { key, room, aid, node, seat: node.querySelector(".seat"), ch: node.querySelector(".ch"), pc: node.querySelector(".pc"), bub: node.querySelector(".bub") };
      figs.set(key, f);
      setTimeout(() => node.classList.remove("enter"), 450);
    }
    return f;
  }
  function describe(f, r) { // 文字只放在悬停提示里
    const a = A(f.aid);
    if (!a) return;
    const run = S.running.find(x => x.agent === f.aid && x.room === r.id);
    const lead = f.aid === r.lead && (r.members || []).length > 1;
    f.node.classList.toggle("working", !f.leftAt && (a.state === "working" || a.state === "queued"));
    f.node.classList.toggle("queued", !f.leftAt && a.state === "queued");
    f.node.classList.toggle("lead", lead);
    const act = run ? (run.queued ? "排队：" + run.queued : run.task) : f.leftAt ? "干完了，歇会儿" : a.state === "working" ? ((a.acts && a.acts[0] && a.acts[0].plain) || "在干活") : "等队员交活";
    f.node.title = `${lead ? "👑 " : ""}${a.name}${r.label ? "（" + r.label + "）" : ""}\n${act}${run ? "\n" + clock(Date.now() / 1000 - run.start) : ""}`;
  }

  // ---------- 气泡（只用图标）、信封、临时连线 ----------
  function center(key) {
    const f = figs.get(key);
    if (!f || !f.node.isConnected) return null;
    const r = f.seat.getBoundingClientRect(), b = el.getBoundingClientRect();
    return { x: r.left - b.left + r.width / 2, y: r.top - b.top + r.height * 0.28 };
  }
  const side = () => dock !== "top";
  // 贴着屏幕顶边时上面没地方：信封往下绕（气泡也在小人下面，见 hud.css）
  const arc = (p, q, lift) => side() ? { x: Math.max(p.x, q.x) + lift, y: (p.y + q.y) / 2 }
    : edge === "top" ? { x: (p.x + q.x) / 2, y: Math.max(p.y, q.y) + lift } : { x: (p.x + q.x) / 2, y: Math.min(p.y, q.y) - lift };
  function bubble(key, glyph, cls = "", ms = 1600) {
    const f = figs.get(key);
    if (!f) return;
    f.bub.textContent = glyph;
    f.bub.className = "bub on " + cls;
    clearTimeout(f.bubT);
    f.bubT = setTimeout(() => (f.bub.className = "bub"), ms);
  }
  function hop(key) {
    const f = figs.get(key);
    if (!f) return;
    f.seat.classList.remove("hop"); void f.seat.offsetWidth; f.seat.classList.add("hop");
  }
  function pose(key, which, ms = 2600) {
    posed.set(key, { pose: which, until: Date.now() + ms });
    const f = figs.get(key);
    if (f) paint(f);
    setTimeout(() => { const g = figs.get(key); if (g) paint(g); }, ms + 60);
  }
  function fly(from, to, glyph, cls, done) {
    const p = center(from), q = center(to);
    if (!p || !q) { done && done(); return; }
    const b = el.getBoundingClientRect(), m = arc(p, q, 22);
    wires.setAttribute("viewBox", `0 0 ${Math.max(1, b.width)} ${Math.max(1, b.height)}`);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M${p.x},${p.y} Q${m.x},${m.y} ${q.x},${q.y}`);
    path.setAttribute("class", cls);
    wires.appendChild(path); // 只在传信的这一下连线，落地后淡掉
    const e = document.createElement("span");
    e.className = "env " + cls;
    e.textContent = glyph;
    fx.appendChild(e);
    const mid = arc(p, q, 26), k = (pt, s) => ({ transform: `translate(${pt.x - 7}px, ${pt.y - 7}px) scale(${s})` });
    e.animate([k(p, 0.5), { ...k(mid, 1.15), offset: 0.5 }, k(q, 0.85)], { duration: 950, easing: "cubic-bezier(.45,.05,.4,1)" }).onfinish = () => {
      e.remove();
      path.classList.add("out");
      setTimeout(() => path.remove(), 500);
      done && done();
    };
  }
  // 一来一回，不同时开口
  const play = {
    dispatch({ room, from, to }) {
      const a = keyOf(room, from), b = keyOf(room, to);
      pose(a, "social", 1400);
      fly(a, b, "✉", "mail", () => { hop(b); pose(b, "ready", 2400); bubble(b, "👌", "ok"); });
    },
    done({ room, from, to, ok }) {
      const a = keyOf(room, from), b = keyOf(room, to);
      pose(a, ok ? "report" : "think", 2200);
      bubble(a, ok ? "✅" : "❗", ok ? "ok" : "bad", 1000);
      fly(a, b, ok ? "✓" : "!", ok ? "good" : "bad", () => { hop(b); pose(b, ok ? "social" : "think", 2000); bubble(b, ok ? "👍" : "🤔", "", 1500); });
    },
    hello({ room, id }) { const k = keyOf(room, id); hop(k); pose(k, "ready", 2400); bubble(k, "👋", "ok", 2600); },
    start({ room, id }) { const k = keyOf(room, id); hop(k); pose(k, "ready", 2000); bubble(k, "💪", "", 1400); },
  };
  function effect(type, data) { queue.push([type, data]); }

  // 退场：先把宽度收成 0（旁边的人滑过来补位），再拿掉，免得一下子跳动
  function remove(f) {
    figs.delete(f.key);
    posed.delete(f.key);
    f.node.classList.add("gone");
    setTimeout(() => { f.node.remove(); window.__stageTick && window.__stageTick(); }, 420);
  }

  // ---------- 每次状态更新 ----------
  // opts.office：只看这一间办公室的窗口组
  function sync(state, opts = {}) {
    S = state; dock = opts.dock || "top"; edge = opts.edge || "top"; only = opts.office || null; idleLead = opts.idleLead || null;
    el.dataset.dock = dock; el.dataset.edge = edge;
    let list = (S.rooms || []).filter(r => !only || (r.lead || "") === only);
    // 没人干活、也没人在歇着：主位趴在那儿睡觉（小人样式没有球，总得有个能点的人）
    if (!list.length && idleLead && ![...figs.values()].some(f => !f.idle)) list = [{ id: "idle", lead: idleLead, members: [idleLead], idle: true }];
    const now = Date.now(), want = new Set();
    list.forEach(r => r.members.forEach(aid => want.add(keyOf(r.id, aid))));
    for (const [key, f] of [...figs]) {
      if (want.has(key)) { f.leftAt = 0; f.byeSaid = false; f.node.classList.remove("leave"); continue; }
      if (f.idle) { remove(f); continue; } // 睡觉的主位：有人开工就直接让位，不用歇
      if (!f.leftAt) f.leftAt = now;
      const t = now - f.leftAt;
      if (t > LINGER) { remove(f); continue; }
      if (t > LINGER - 3500 && !f.byeSaid) { f.byeSaid = true; bubble(key, "👋", "", 2400); hop(key); }
      if (t > LINGER - 1300) f.node.classList.add("leave");
    }
    // 窗口组：现在有的 + 还有人在歇着没走的（保持原来的位置）
    const order = list.map(r => r.id);
    for (const f of figs.values()) if (!order.includes(f.room)) order.push(f.room);
    order.forEach((rid, i) => {
      const x = roomNode(rid), r = list.find(y => y.id === rid) || x.last || { id: rid, members: [] };
      x.last = r;
      if (roomsEl.children[i] !== x.node) roomsEl.insertBefore(x.node, roomsEl.children[i] || null);
      const members = [...r.members];
      for (const f of figs.values()) if (f.room === rid && !members.includes(f.aid)) members.push(f.aid);
      members.sort((p, q) => (p === r.lead ? -1 : q === r.lead ? 1 : 0)).forEach((aid, j) => {
        const f = figure(rid, aid);
        f.idle = !!r.idle;
        const kids = [...x.cast.children].filter(n => !n.classList.contains("gone"));
        if (kids[j] !== f.node) x.cast.insertBefore(f.node, kids[j] || null);
        describe(f, r);
        paint(f);
      });
    });
    for (const [id, x] of rooms) if (!order.includes(id) && !x.cast.querySelector(".fg:not(.gone)")) { x.node.remove(); rooms.delete(id); }
    el.classList.toggle("empty", !figs.size);
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!el.isConnected) return;
      while (queue.length) { const [t, d] = queue.shift(); play[t] && play[t](d); }
    }));
  }

  // 歇着的小人要按时间换姿势、到点退场：没有新状态推过来时也要自己走表
  setInterval(() => { if (S && el.isConnected && [...figs.values()].some(f => f.leftAt || !f.node.classList.contains("working"))) sync(S, { dock, edge, office: only, idleLead }); }, 1000);

  window.ABStage = { el, sync, effect, busy: () => figs.size > 0 };
})();
