/* Melores Checklist — Telegram Mini App. Dizayn va navigatsiya Trello bot
   Mini App'i bilan BIR XIL (css/app.css, splash, ikonka sprite'i, tab-bar,
   stack + BackButton, MainButton — o'sha yordamchilar nusxasi). Faqat
   ekranlar boshqa: xodim savollari, javoblar, nazoratchilar, jarima sozlamasi.
   Trello botdagi yordamchi o'zgarsa — bu yerda ham yangilang. */

const API_BASE = "/api";
const root = document.getElementById("app");
const tabbarRoot = document.getElementById("tabbar");
const state = { me: null };
const nav = { stack: [], section: null, transition: null };
let mainButtonHandler = null;
const ACCENT = "#4f3ff0";

const ROLE_LABELS = {
  worker: "Ishchi", brigadier: "Brigadir", supervisor: "Rahbar",
  admin: "Admin", observer: "Kuzatuvchi", seller: "Sotuvchi",
};
const ROLE_ICONS = {
  worker: "user", brigadier: "users", supervisor: "chart",
  admin: "settings", observer: "user", seller: "briefcase",
};
const ERRORS = {
  not_registered: "Siz tizimda ro'yxatdan o'tmagansiz.",
  forbidden: "Bu panel faqat admin belgilagan xodimlar uchun.",
  admin_only: "Faqat admin uchun.",
  unauthorized: "Ilovani Telegram ichidan oching.",
  text_length: "Savol matni 1–1000 belgi bo'lishi kerak.",
  penalty_range: "Jarima 0 dan 10 gacha bo'lishi kerak.",
  penalty: "Jarima qiymati noto'g'ri.",
  question_sent: "Savol allaqachon yuborilgan — endi o'zgartirib bo'lmaydi.",
  nothing_to_cancel: "Bekor qiladigan jarima yo'q (yoki allaqachon bekor qilingan).",
  employee_ids: "Kamida bitta xodimni tanlang.",
  reader_ids: "Javobni ko'radiganlar ro'yxati noto'g'ri.",
  points: "Ballni ro'yxatdan tanlang.",
  points_list: "Ball variantlari: 1–6 ta son, har biri 0,05 dan 10 gacha (masalan: 0,2 0,5 0,7 1).",
  schedule_order: "Yuborish vaqti javob muddatidan oldin bo'lishi kerak.",
  remind_every: "Eslatma oralig'i 0–240 daqiqa bo'lishi kerak.",
  time: "Vaqt noto'g'ri (SS:DD).",
  full_name: "Ism 2–150 belgi bo'lishi kerak.",
  phone: "Telefon raqami noto'g'ri (kamida 9 raqam).",
  role: "Rolni tanlang.",
  phone_taken: "Bu telefon raqami bilan faol xodim allaqachon bor.",
  self_demote: "O'zingizni admin'likdan olib bo'lmaydi yoki o'chirib bo'lmaydi.",
  trello_ids: "Kamida bitta xodimni tanlang.",
  window_open: "Hozir savollar ketgan va javob kutilmoqda — jadvalni muddatdan keyin o'zgartiring.",
  day: "Sana noto'g'ri.",
  month: "Oy noto'g'ri.",
};

/* Pastki tab-bar. Admin — hammasi; nazoratchi (o'ziga xodim biriktirilgan
   har qanday xodim) — "Nazoratchilar" va sozlamasiz. */
function tabDefs() {
  const tabs = [
    { key: "home", icon: icon("home"), label: "Bosh sahifa", screen: screenHome },
    { key: "employees", icon: icon("users"), label: "Xodimlar", screen: screenEmployees },
  ];
  if (state.me.is_admin) tabs.push({ key: "viewers", icon: icon("chart"), label: "Rahbarlar", screen: screenViewers });
  tabs.push({ key: "profile", icon: icon("user"), label: "Profil", screen: screenProfile });
  return tabs;
}

function switchTab(tabKey, screenFn) {
  nav.section = tabKey;
  resetTo(screenFn);
}

function renderTabBar() {
  if (!tabbarRoot || !state.me) return;
  const defs = tabDefs();
  tabbarRoot.innerHTML = `
    <nav class="tab-bar">
      <div class="pill-bg" id="tab-pill"></div>
      ${defs.map((d) => `
        <button class="tab-item" data-key="${d.key}" aria-selected="${d.key === nav.section}">
          <span class="tab-ic">${d.icon}</span><span class="tab-lbl">${esc(d.label)}</span>
        </button>
      `).join("")}
    </nav>
  `;
  defs.forEach((d) => {
    tabbarRoot.querySelector(`[data-key="${d.key}"]`).onclick = () => switchTab(d.key, d.screen);
  });
  positionTabPill();
}

function positionTabPill() {
  const pill = tabbarRoot.querySelector("#tab-pill");
  const active = tabbarRoot.querySelector('.tab-item[aria-selected="true"]');
  if (!pill || !active) return;
  pill.style.left = `${active.offsetLeft}px`;
  pill.style.width = `${active.offsetWidth}px`;
}
window.addEventListener("resize", () => positionTabPill());

function icon(name) {
  return `<svg class="ic-svg"><use href="#ic-${name}"></use></svg>`;
}

function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function tg() {
  return window.Telegram && window.Telegram.WebApp;
}

function showError(message) {
  const app = tg();
  if (app && app.showAlert) app.showAlert(message);
  else window.alert(message);
}

function confirmAction(message) {
  const app = tg();
  return new Promise((resolve) => {
    if (app && app.showConfirm) app.showConfirm(message, resolve);
    else resolve(window.confirm(message));
  });
}

/* Ball rangi: Trello botdagi scoreClass/heroTone/scoreSigned bilan bir xil. */
function scoreClass(value) {
  return value > 0 ? "pos" : value < 0 ? "neg" : "zero";
}
function heroTone(value) {
  return value > 0 ? "positive" : value < 0 ? "critical" : "";
}
function scoreSigned(value) {
  return (value > 0 ? "+" : "") + String(Math.round(value * 100) / 100).replace(".", ",");
}

async function api(path, opts = {}) {
  const app = tg();
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (app && app.initData) headers["X-Telegram-Init-Data"] = app.initData;
  const res = await fetch(API_BASE + path, Object.assign({}, opts, { headers }));
  let body = null;
  try {
    body = await res.json();
  } catch (e) {
    /* bo'sh javob */
  }
  if (!res.ok) {
    const code = body && body.error;
    const error = new Error(ERRORS[code] || code || `HTTP ${res.status}`);
    error.status = res.status;
    throw error;
  }
  return body;
}

function setScreen(html) {
  root.innerHTML = html;
}

function hideMainButton() {
  const app = tg();
  if (!app) return;
  if (mainButtonHandler) {
    app.MainButton.offClick(mainButtonHandler);
    mainButtonHandler = null;
  }
  app.MainButton.hide();
}

function setMainButton(text, onClick, color) {
  const app = tg();
  if (!app) return;
  if (mainButtonHandler) app.MainButton.offClick(mainButtonHandler);
  // Ikki marta bosish (yoki Telegram'ning ikki marta yuborgan click'i) ikki
  // xil yozuv yaratmasin — sinovda bir savol ikki marta saqlangan (2026-11-09).
  let busy = false;
  mainButtonHandler = async () => {
    if (busy) return;
    busy = true;
    app.MainButton.showProgress(false);
    try {
      await onClick();
    } finally {
      busy = false;
      app.MainButton.hideProgress();
    }
  };
  app.MainButton.setText(text);
  if (color) app.MainButton.setParams({ color });
  app.MainButton.onClick(mainButtonHandler);
  app.MainButton.show();
}

async function show(renderFn, ...args) {
  nav.stack.push([renderFn, args]);
  nav.transition = "fwd";
  await renderCurrent();
}

async function goBack() {
  if (nav.stack.length > 1) {
    nav.stack.pop();
    nav.transition = "back";
    await renderCurrent();
  }
}

async function resetTo(renderFn, ...args) {
  nav.stack = [[renderFn, args]];
  nav.transition = "fade";
  await renderCurrent();
}

async function replaceTop(renderFn, ...args) {
  nav.stack[nav.stack.length - 1] = [renderFn, args];
  await renderCurrent();
}

async function renderCurrent() {
  const app = tg();
  if (app) {
    if (nav.stack.length > 1) app.BackButton.show();
    else app.BackButton.hide();
  }
  hideMainButton();
  const [fn, args] = nav.stack[nav.stack.length - 1];
  try {
    await fn(...args);
  } catch (e) {
    setScreen(`<p class="error-banner">${esc(e.message || "Xatolik yuz berdi")}</p><button class="btn" id="btn-retry">Qayta urinish</button>`);
    root.querySelector("#btn-retry").onclick = renderCurrent;
  }
  applyStagger();
  animateNumbers();
  if (nav.transition) {
    root.classList.remove("enter-fwd", "enter-back", "enter-fade");
    void root.offsetWidth;
    root.classList.add("enter-" + nav.transition);
    nav.transition = null;
  }
  renderTabBar();
}

const STAGGER_SELECTOR = ".nav-card, .task-card, .hero-tile, .member-card, .stat-row, " +
  ".emp-row, .lead-card, .alert-card, .fin-card, .kpi-list-item, .settings-row, .toggle-row, .radio-row";
function applyStagger() {
  root.querySelectorAll(STAGGER_SELECTOR).forEach((el, i) => {
    el.classList.add("stagger");
    el.style.setProperty("--i", i);
  });
}

function animateNumbers() {
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  root.querySelectorAll(".hero-tile .num, .chart-head .big").forEach((el) => {
    const m = el.textContent.match(/^([+-]?)(\d+)(.*)$/);
    if (!m) return;
    const [, sign, digits, suffix] = m;
    const target = Number(digits);
    if (!target) return;
    const start = performance.now();
    const dur = 650;
    const step = (now) => {
      const p = Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - p, 3);
      el.textContent = `${sign}${Math.round(target * eased)}${suffix}`;
      if (p < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}

const loading = () => setScreen(`<p class="loading">Yuklanmoqda...</p>`);

function empRowHtml(e, i) {
  const sub = e.questions ? `bugun ${e.questions} savol · ${e.answered_today}/${e.questions} javob` : "bugun savol yo'q";
  const warn = !e.has_telegram ? " · Telegram ulanmagan" : !e.started_bot ? " · botni ochmagan" : "";
  return `
    <button class="emp-row" data-i="${i}" data-name="${esc(e.full_name.toLowerCase())}">
      <span class="dot-status ${e.started_bot ? "on" : "off"}"></span>
      <span class="grow"><div class="name">${esc(e.full_name)}</div>
        <div class="role">${esc(ROLE_LABELS[e.role] || e.role)} · ${esc(sub)}${esc(warn)}</div></span>
      ${e.month_penalty ? `<span class="badge warn">${scoreSigned(-e.month_penalty)}</span>` : ""}
      <span class="chev">›</span>
    </button>`;
}

function bindEmpRows(list) {
  root.querySelectorAll(".emp-row").forEach((el) => {
    el.onclick = () => show(screenEmployeeDetail, list[Number(el.dataset.i)].id);
  });
}

function bindSearch() {
  const input = root.querySelector("#f-search");
  input.oninput = () => {
    const term = input.value.trim().toLowerCase();
    root.querySelectorAll("[data-name]").forEach((el) => {
      el.style.display = el.dataset.name.includes(term) ? "" : "none";
    });
  };
}

/* ---------- Bosh sahifa ---------- */

async function screenHome() {
  loading();
  const employees = await api("/employees");
  const participants = employees.filter((e) => e.questions && e.has_telegram);
  const done = participants.filter((e) => e.answered_today >= e.questions);
  const pending = participants.filter((e) => e.answered_today < e.questions);
  const monthPenalty = employees.reduce((s, e) => s + e.month_penalty, 0);
  const notStarted = employees.filter((e) => !e.started_bot).length;
  const first = state.me.full_name.split(" ")[0];
  setScreen(`
    <p class="greet">Salom, ${esc(first)}</p>
    <p class="greet-wave">Kun davomida savol yozing — ${state.me.start} da yuboriladi, jarima ${state.me.deadline} da</p>
    <div class="hero-row">
      <div class="hero-tile ${participants.length && done.length === participants.length ? "positive" : ""}"><span class="num">${done.length}/${participants.length}</span><span class="lbl">Bugun to'liq javob berdi</span></div>
      <div class="hero-tile ${pending.length ? "warn" : ""}"><span class="num">${pending.length}</span><span class="lbl">Javob kutilmoqda</span></div>
    </div>
    <div class="hero-row">
      <div class="hero-tile ${heroTone(-monthPenalty)}"><span class="num">${scoreSigned(-monthPenalty)}</span><span class="lbl">Shu oy checklist jarimasi</span></div>
      <div class="hero-tile"><span class="num">${employees.length}</span><span class="lbl">Xodimlar</span></div>
    </div>
    ${notStarted ? `<button class="alert-card" id="nav-nostart"><span class="ic">${icon("alert")}</span><span class="grow">${notStarted} ta xodim botni hali ochmagan — savollar ularga bormaydi</span><span class="chev">›</span></button>` : ""}
    <button class="nav-card" id="nav-answers"><span class="ic">${icon("inbox")}</span><span class="grow">Kunlik javoblar</span><span class="chev">›</span></button>
    <button class="nav-card" id="nav-stats"><span class="ic">${icon("chart")}</span><span class="grow">Oylik statistika</span><span class="chev">›</span></button>
    ${pending.length ? `<p class="section-lbl">Bugun javob kutilmoqda</p>${pending.map(empRowHtml).join("")}` : ""}
  `);
  bindEmpRows(pending);
  const ns = root.querySelector("#nav-nostart");
  if (ns) ns.onclick = () => show(screenEmployeeList, "Botni ochmaganlar", "nostart");
  root.querySelector("#nav-answers").onclick = () => show(screenDayAnswers, null);
  root.querySelector("#nav-stats").onclick = () => show(screenStats, null);
  setMainButton("➕ Savol yozish", () => show(screenBulkQuestion), ACCENT);
}

/* ---------- Xodimlar ---------- */

async function screenEmployees() {
  loading();
  const employees = await api("/employees");
  const roles = Object.keys(ROLE_LABELS).filter((r) => employees.some((e) => e.role === r));
  setScreen(`
    <p class="page-title">Xodimlar (${employees.length})</p>
    ${state.me.is_admin ? `
      <button class="nav-card" id="nav-add"><span class="ic">${icon("user")}</span><span class="grow">Xodim qo'shish</span><span class="chev">›</span></button>
      <button class="nav-card" id="nav-import"><span class="ic">${icon("users")}</span><span class="grow">Trello botdan import</span><span class="chev">›</span></button>` : ""}
    <button class="nav-card accent" id="nav-all"><span class="ic">${icon("list")}</span><span class="grow">Hammasi · qidirish</span><span class="chev">›</span></button>
    ${roles.map((r) => `
        <button class="nav-card" data-role="${r}">
          <span class="ic">${icon(ROLE_ICONS[r])}</span><span class="grow">${esc(ROLE_LABELS[r])}</span><span class="badge">${employees.filter((e) => e.role === r).length}</span><span class="chev">›</span>
        </button>
      `).join("")}
  `);
  root.querySelector("#nav-all").onclick = () => show(screenEmployeeList, "Barcha xodimlar", "all");
  if (state.me.is_admin) {
    root.querySelector("#nav-add").onclick = () => show(screenEmployeeForm, null);
    root.querySelector("#nav-import").onclick = () => show(screenTrelloImport);
  }
  root.querySelectorAll(".nav-card[data-role]").forEach((el) => {
    el.onclick = () => show(screenEmployeeList, ROLE_LABELS[el.dataset.role], el.dataset.role);
  });
  setMainButton("➕ Savol yozish", () => show(screenBulkQuestion), ACCENT);
}

/* Savol muhimligi: javobsiz qolsa shuncha jarima. Variantlar — Sozlamalarda (state.me.points). */
const fmtPts = (p) => String(p).replace(".", ",");

function pointsHtml(selected) {
  const opts = state.me.points.includes(selected) ? state.me.points : [...state.me.points, selected].sort((a, b) => a - b);
  return `<div class="segmented" id="f-points">${opts.map((p) =>
    `<button type="button" data-p="${p}" aria-selected="${p === selected}">${fmtPts(p)} ball</button>`).join("")}</div>`;
}

function bindPoints() {
  const btns = [...root.querySelectorAll("#f-points button")];
  btns.forEach((b) => { b.onclick = () => btns.forEach((x) => x.setAttribute("aria-selected", String(x === b))); });
  return () => Number(btns.find((b) => b.getAttribute("aria-selected") === "true").dataset.p);
}

/* Xodimlar ro'yxati rol bo'yicha guruhlab (Xodimlar bo'limidagidek), har guruhda "hammasi". */
function pickerHtml(employees, key, checked) {
  const roles = [...new Set([...Object.keys(ROLE_LABELS), ...employees.map((e) => e.role)])]
    .filter((r) => employees.some((e) => e.role === r));
  return roles.map((r) => {
    const group = employees.filter((e) => e.role === r);
    return `
      <label class="check-row"><input type="checkbox" data-group="${key}:${r}" />
        <span class="grow"><b>${esc(ROLE_LABELS[r] || r)}</b> <span class="hint">(${group.length}) · hammasi</span></span></label>
      ${group.map((e) => `
        <label class="check-row sub" data-name="${esc(e.full_name.toLowerCase())}">
          <input type="checkbox" class="f-${key}" data-role="${r}" value="${e.id}" ${checked.includes(e.id) ? "checked" : ""} />
          <span class="grow">${esc(e.full_name)}${e.started_bot ? "" : ` <span class="hint">· botni ochmagan</span>`}</span>
        </label>`).join("")}`;
  }).join("");
}

function bindPicker(key, counterId) {
  const boxes = [...root.querySelectorAll(`.f-${key}`)];
  const count = () => { root.querySelector(counterId).textContent = boxes.filter((b) => b.checked).length; };
  root.querySelectorAll(`[data-group^="${key}:"]`).forEach((g) => {
    const role = g.dataset.group.slice(key.length + 1);
    g.onchange = () => {
      boxes.forEach((b) => { if (b.dataset.role === role && b.closest(".check-row").style.display !== "none") b.checked = g.checked; });
      count();
    };
  });
  boxes.forEach((b) => { b.onchange = count; });
  count();
  return () => boxes.filter((b) => b.checked).map((b) => Number(b.value));
}

/* Bitta savol — bir nechta xodimga; ball (muhimlik) va javobni kimlar ko'rishi shu yerda belgilanadi. */
async function screenBulkQuestion(preselectId) {
  loading();
  const employees = (await api("/employees")).filter((e) => e.has_telegram);
  const pre = preselectId ? [preselectId] : [];
  setScreen(`
    <p class="page-title">Yangi savol</p>
    <div class="field">
      <label>Savol matni</label>
      <textarea id="f-text" maxlength="1000" placeholder="Masalan: Bugun qaysi mijozlarga qo'ng'iroq qildingiz?"></textarea>
    </div>
    <div class="field"><label>Muhimligi — javob berilmasa jarima</label>${pointsHtml(state.me.points[0])}</div>
    <p class="hint">Savol har kuni ${state.me.start} da takrorlanadi, o'chirmaguningizcha. Kun davomida yozilgani bugundan, ${state.me.start} dan keyin yozilgani ertadan boshlanadi.</p>
    <div class="field"><input id="f-search" placeholder="Ism bo'yicha qidirish..." /></div>
    <p class="section-lbl">Kim javob beradi · tanlangan: <b id="cnt-ans">0</b></p>
    ${pickerHtml(employees, "ans", pre)}
    <p class="section-lbl">Javobni kim ko'radi · tanlangan: <b id="cnt-read">0</b></p>
    <p class="hint">Xodim javob yozishi bilan javob shu odamlarga ham boradi (ular botda /start bosgan bo'lishi kerak). Admin va nazoratchi ${state.me.deadline} dagi yig'mani baribir oladi.</p>
    ${pickerHtml(employees, "read", [])}
  `);
  bindSearch();
  const points = bindPoints();
  const answerers = bindPicker("ans", "#cnt-ans");
  const readers = bindPicker("read", "#cnt-read");
  setMainButton("💾 Saqlash", async () => {
    const text = root.querySelector("#f-text").value.trim();
    const ids = answerers();
    if (!text) return showError("Savol matnini kiriting.");
    if (!ids.length) return showError(ERRORS.employee_ids);
    try {
      const r = await api("/questions", { method: "POST", body: JSON.stringify({
        text, employee_ids: ids, reader_ids: readers(), points: points() }) });
      showError(`${r.count} ta xodimga yozildi — ${r.day} ${state.me.start} dan boshlab har kuni yuboriladi.`);
      await goBack();
    } catch (err) {
      showError(err.message);
    }
  }, ACCENT);
}

/* filter: "all" | "nostart" (botni ochmagan) | rol nomi.
   ponytail: to'liq ro'yxatni olib mijozda filtrlaymiz (Trello Mini App bilan
   bir xil naqsh) — minglab xodim bo'lsa serverga filtr qo'shiladi. */
async function screenEmployeeList(title, filter) {
  loading();
  const all = await api("/employees");
  const list = filter === "all" ? all
    : filter === "nostart" ? all.filter((e) => !e.started_bot)
    : all.filter((e) => e.role === filter);
  setScreen(`
    <p class="page-title">${esc(title)} (${list.length})</p>
    ${filter === "nostart" ? `<p class="page-sub">Xodim @${esc(state.me.bot_username || "bot")} da bir marta /start bosmaguncha savollar unga bormaydi.</p>` : ""}
    ${list.length > 8 ? `<div class="field"><input id="f-search" placeholder="Ism bo'yicha qidirish..." /></div>` : ""}
    ${list.length ? list.map(empRowHtml).join("") : `<p class="empty-state">Xodim topilmadi</p>`}
  `);
  if (list.length > 8) bindSearch();
  bindEmpRows(list);
}

async function screenEmployeeDetail(employeeId) {
  loading();
  const e = await api(`/employees/${employeeId}`);
  const weekPenalty = e.days.reduce((s, d) => s + (d.cancelled ? 0 : d.penalty || 0), 0);
  setScreen(`
    <p class="page-title">${esc(e.full_name)}</p>
    ${!e.has_telegram ? `<p class="error-banner">Telegram ulanmagan — savollar bormaydi.</p>`
      : !e.started_bot ? `<p class="error-banner">Botni hali ochmagan — savollar bormaydi, jarima ham yozilmaydi. Unga botda /start bosishni ayting.</p>` : ""}
    <div class="panel">
      <div class="kv-row"><span class="k">${esc(cap(e.target_label))} ${state.me.start} da yuboriladi</span><span class="v">${e.queued.length} savol</span></div>
      <div class="kv-row"><span class="k">Jarima (7 kun)</span><span class="v">${scoreSigned(-weekPenalty)}</span></div>
    </div>
    ${e.trello ? `
      <p class="section-lbl">Umumiy ball · shu oy (Trello botdan)</p>
      <div class="panel">
        <div class="kv-row"><span class="k">Checklist jarimasi</span><span class="v">${scoreSigned(-e.trello.checklist)}</span></div>
        <div class="kv-row"><span class="k">Muddat va boshqa jarimalar</span><span class="v">${scoreSigned(-e.trello.other)}</span></div>
        <div class="kv-row"><span class="k">Bonus</span><span class="v">${scoreSigned(e.trello.plus)}</span></div>
        <div class="kv-row"><span class="k">Jami</span><span class="v ${scoreClass(e.trello.total)}">${scoreSigned(e.trello.total)}</span></div>
        ${e.trello.pay_day ? `<div class="kv-row"><span class="k">Oylik kuni</span><span class="v">${esc(e.trello.pay_day)}</span></div>` : ""}
      </div>` : `<p class="hint">Faqat checklist xodimi — Trello botda yo'q, umumiy ball va oylik kuni yuritilmaydi.</p>`}
    ${state.me.is_admin ? `<button class="nav-card" id="emp-edit"><span class="ic">${icon("settings")}</span><span class="grow">Xodimni tahrirlash</span><span class="chev">›</span></button>` : ""}
    <p class="section-lbl">Har kungi savollar · ${esc(e.target_label)} ${state.me.start} dan</p>
    ${e.queued.length ? e.queued.map((q, i) => `
      <div class="fin-card" data-qid="${q.id}">
        <div class="top"><span class="task">${i + 1}. ${esc(q.text)}</span></div>
        <p class="hint">${q.points ? `javobsiz ${fmtPts(q.points)} ball` : "umumiy stavka"}${q.readers ? ` · javobni ${q.readers} kishi ko'radi` : ""}</p>
        <div class="member-actions">
          ${i ? `<button class="q-up">↑ Yuqoriga</button>` : ""}
          <button class="q-edit">Tahrirlash</button>
          <button class="q-del">O'chirish</button>
        </div>
      </div>
    `).join("") : `<p class="empty-state">Savol yo'q — yozmasangiz, ${esc(e.target_label)} unga hech narsa bormaydi.</p>`}
    <p class="hint">Savollar har kuni ${state.me.start} da takrorlanadi, o'chirmaguningizcha. ${state.me.start} dan keyin yozilgan, tahrirlangan yoki o'chirilgani ertadan kuchga kiradi.</p>
    <p class="section-lbl">Tarix · oxirgi 7 kun</p>
    ${e.days.length ? e.days.map((d) => dayPanelHtml(d)).join("") : `<p class="empty-state">Hali savol yuborilmagan</p>`}
  `);
  root.querySelectorAll(".fin-card[data-qid]").forEach((card) => {
    const q = e.queued.find((x) => x.id === Number(card.dataset.qid));
    card.querySelector(".q-edit").onclick = () => show(screenQuestionForm, employeeId, q, e.target_label);
    const up = card.querySelector(".q-up");
    if (up) up.onclick = async () => {
      try {
        await api(`/questions/${q.id}/up`, { method: "POST" });
        await replaceTop(screenEmployeeDetail, employeeId);
      } catch (err) {
        showError(err.message);
      }
    };
    card.querySelector(".q-del").onclick = async () => {
      if (!(await confirmAction("Savol o'chirilsinmi?"))) return;
      try {
        await api(`/questions/${q.id}`, { method: "DELETE" });
        await replaceTop(screenEmployeeDetail, employeeId);
      } catch (err) {
        showError(err.message);
      }
    };
  });
  bindCancel(() => replaceTop(screenEmployeeDetail, employeeId));
  const edit = root.querySelector("#emp-edit");
  if (edit) edit.onclick = () => show(screenEmployeeForm, e);
  setMainButton("➕ Savol qo'shish", () => show(screenBulkQuestion, employeeId), ACCENT);
}

/* Xodim qo'shish (emp=null) / tahrirlash — faqat admin. Faqat checklist'dagi yozuv o'zgaradi. */
async function screenEmployeeForm(emp) {
  setScreen(`
    <p class="page-title">${emp ? "Xodimni tahrirlash" : "Yangi xodim"}</p>
    <div class="field"><label>Ism familiya</label><input id="f-name" maxlength="150" value="${esc(emp ? emp.full_name : "")}" /></div>
    <div class="field"><label>Telefon</label><input id="f-phone" type="tel" maxlength="20" placeholder="+998 90 123 45 67" value="${esc(emp && emp.phone ? emp.phone : "")}" /></div>
    <div class="field"><label>Rol</label><select id="f-role">${Object.keys(ROLE_LABELS).map((r) =>
      `<option value="${r}" ${(emp ? emp.role : "worker") === r ? "selected" : ""}>${esc(ROLE_LABELS[r])}</option>`).join("")}</select></div>
    ${emp ? `<label class="check-row"><input type="checkbox" id="f-active" checked /><span class="grow">Faol <span class="hint">— belgini olsangiz, ro'yxatdan chiqadi va savol bormaydi</span></span></label>` : ""}
    <p class="hint">${emp && emp.linked ? "Trello botdagi yozuvi o'zgarmaydi — faqat checklist'dagisi." : "Faqat checklist botga qo'shiladi, Trello botga tushmaydi."}
      Xodim @${esc(state.me.bot_username || "bot")} da /start bosib, shu telefon raqamini ulashganda ro'yxatdan o'tadi.</p>
  `);
  setMainButton("💾 Saqlash", async () => {
    const body = {
      full_name: root.querySelector("#f-name").value.trim(),
      phone: root.querySelector("#f-phone").value.trim(),
      role: root.querySelector("#f-role").value,
    };
    if (emp) body.is_active = root.querySelector("#f-active").checked;
    try {
      if (emp) await api(`/employees/${emp.id}`, { method: "PUT", body: JSON.stringify(body) });
      else await api("/employees", { method: "POST", body: JSON.stringify(body) });
      await goBack();
    } catch (err) {
      showError(err.message);
    }
  }, ACCENT);
}

/* Trello botdagi, checklist'da hali yo'q xodimlarni tanlab import qilish — faqat admin. */
async function screenTrelloImport() {
  loading();
  const list = (await api("/trello-import")).map((t) => ({ ...t, started_bot: true }));
  setScreen(`
    <p class="page-title">Trello'dan import</p>
    <p class="page-sub">Trello botda bor, lekin checklist'da yo'q xodimlar. Import qilingani Trello'ga bog'lanadi: jarimasi umumiy ballga qo'shiladi, oylik kuni ko'rinadi. Tanlangan: <b id="cnt-imp">0</b></p>
    ${list.length > 8 ? `<div class="field"><input id="f-search" placeholder="Ism bo'yicha qidirish..." /></div>` : ""}
    ${list.length ? pickerHtml(list, "imp", []) : `<p class="empty-state">Hammasi allaqachon checklist'da</p>`}
  `);
  if (list.length > 8) bindSearch();
  const picked = bindPicker("imp", "#cnt-imp");
  if (!list.length) return;
  setMainButton("⬇️ Import qilish", async () => {
    const ids = picked();
    if (!ids.length) return showError(ERRORS.employee_ids);
    try {
      const r = await api("/trello-import", { method: "POST", body: JSON.stringify({ trello_ids: ids }) });
      showError(`${r.count} ta xodim qo'shildi.`);
      await goBack();
    } catch (err) {
      showError(err.message);
    }
  }, ACCENT);
}

const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

function bindCancel(rerender) {
  root.querySelectorAll("[data-cancel]").forEach((btn) => {
    btn.onclick = async () => {
      if (!(await confirmAction("Jarima bekor qilinsinmi? KPI'dan o'chiriladi, oylik kuni qayta hisoblanadi, xodimga xabar boradi."))) return;
      try {
        await api(`/days/${btn.dataset.cancel}/cancel-penalty`, { method: "POST" });
        await rerender();
      } catch (err) {
        showError(err.message);
      }
    };
  });
}

/* ---------- Kunlik javoblar (21:00 yig'ma tugmasi shu ekranni ochadi) ---------- */

const isoShift = (iso, days) => {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
};

async function screenDayAnswers(dayIso) {
  loading();
  const data = await api(`/answers${dayIso ? `?day=${dayIso}` : ""}`);
  const done = (r) => r.items.every((it) => it.a);
  const rows = [...data.rows].sort((a, b) => done(a) - done(b));  // javobsizlar tepada
  const full = data.rows.filter(done).length;
  setScreen(`
    <p class="page-title">Javoblar</p>
    <div class="segmented">
      <button id="d-prev">‹ Oldingi</button>
      <button aria-selected="true">${esc(data.label)}</button>
      <button id="d-next" ${data.day >= data.today ? "disabled" : ""}>Keyingi ›</button>
    </div>
    ${rows.length ? `
      <div class="hero-row">
        <div class="hero-tile ${full === rows.length ? "positive" : "warn"}"><span class="num">${full}/${rows.length}</span><span class="lbl">To'liq javob berdi</span></div>
      </div>
      ${rows.length > 8 ? `<div class="field"><input id="f-search" placeholder="Ism bo'yicha qidirish..." /></div>` : ""}
      ${rows.map((r) => `<div data-name="${esc(r.full_name.toLowerCase())}">${dayPanelHtml(r, r.full_name)}</div>`).join("")}
    ` : `<p class="empty-state">${data.day >= data.today ? `Bugungi savollar hali yuborilmagan (${state.me.start} da ketadi)` : "Bu kuni savol yuborilmagan"}</p>`}
  `);
  if (rows.length > 8) bindSearch();
  bindCancel(() => replaceTop(screenDayAnswers, data.day));
  root.querySelector("#d-prev").onclick = () => replaceTop(screenDayAnswers, isoShift(data.day, -1));
  const next = root.querySelector("#d-next");
  if (data.day < data.today) next.onclick = () => replaceTop(screenDayAnswers, isoShift(data.day, 1));
}

/* ---------- Oylik statistika ---------- */

const monthShift = (ym, n) => {
  const [y, m] = ym.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + n, 1));
  return d.toISOString().slice(0, 7);
};
const pct = (r) => (r.asked ? Math.round((r.answered / r.asked) * 100) : 100);

async function screenStats(month) {
  loading();
  const data = await api(`/stats${month ? `?month=${month}` : ""}`);
  const rows = [...data.rows].sort((a, b) => pct(a) - pct(b) || b.penalty - a.penalty);  // eng intizomsiz tepada
  const asked = rows.reduce((s, r) => s + r.asked, 0);
  const answered = rows.reduce((s, r) => s + r.answered, 0);
  const penalty = rows.reduce((s, r) => s + r.penalty, 0);
  setScreen(`
    <p class="page-title">Oylik statistika</p>
    <div class="segmented">
      <button id="m-prev">‹</button>
      <button aria-selected="true">${esc(data.label)}</button>
      <button id="m-next" ${data.month >= data.current ? "disabled" : ""}>›</button>
    </div>
    <div class="hero-row">
      <div class="hero-tile ${asked && answered === asked ? "positive" : asked ? "warn" : ""}"><span class="num">${asked ? Math.round((answered / asked) * 100) : 0}%</span><span class="lbl">Javob berildi (${answered}/${asked})</span></div>
      <div class="hero-tile ${heroTone(-penalty)}"><span class="num">${scoreSigned(-penalty)}</span><span class="lbl">Jami jarima</span></div>
    </div>
    <p class="hint">Faqat savol yetib borgan, yopilgan (${state.me.deadline} dan keyin) kunlar. Eng past foiz tepada.</p>
    ${rows.length ? rows.map((r, i) => `
      <button class="emp-row" data-i="${i}">
        <span class="grow"><div class="name">${esc(r.full_name)}</div>
          <div class="role">${r.answered}/${r.asked} javob · ${r.days} kun${r.kpi_total !== null ? ` · umumiy KPI ${scoreSigned(r.kpi_total)}` : ""}</div></span>
        <span class="badge ${pct(r) < 100 ? "warn" : ""}">${pct(r)}%</span>
        ${r.penalty ? `<span class="badge warn">${scoreSigned(-r.penalty)}</span>` : ""}
        <span class="chev">›</span>
      </button>
    `).join("") : `<p class="empty-state">Bu oyda hali ma'lumot yo'q</p>`}
  `);
  root.querySelectorAll(".emp-row").forEach((el) => {
    el.onclick = () => show(screenEmployeeDetail, rows[Number(el.dataset.i)].id);
  });
  root.querySelector("#m-prev").onclick = () => replaceTop(screenStats, monthShift(data.month, -1));
  if (data.month < data.current) root.querySelector("#m-next").onclick = () => replaceTop(screenStats, monthShift(data.month, 1));
}

function dayPanelHtml(d, title) {
  const answered = d.items.filter((it) => it.a).length;
  let pill;
  if (d.cancelled) pill = `<span class="status-pill neutral">Jarima bekor qilingan</span>`;
  else if (!d.closed) pill = `<span class="status-pill neutral">Davom etmoqda</span>`;
  else if (d.blocked) pill = `<span class="status-pill critical">Bloklagan · ${scoreSigned(-d.penalty)}</span>`;
  else if (!d.delivered) pill = `<span class="status-pill neutral">Yetib bormagan</span>`;
  else if (d.penalty) pill = `<span class="status-pill critical">${answered}/${d.items.length} · ${scoreSigned(-d.penalty)}</span>`;
  else pill = `<span class="status-pill positive">${answered}/${d.items.length} ✓</span>`;
  const canCancel = state.me.is_admin && d.closed && d.penalty && !d.cancelled && d.day_id;
  return `
    <div class="panel">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <span style="font-weight:700;font-size:14px">${esc(title || d.day)}</span>${pill}
      </div>
      ${d.items.map((it) => `
        <div class="kv-row" style="flex-direction:column;gap:3px">
          <span class="k">${esc(it.q)}${it.points ? ` · ${fmtPts(it.points)} ball` : ""}</span>
          ${it.a ? `<span style="white-space:pre-wrap;overflow-wrap:anywhere">${esc(it.a)}</span><span class="hint">${esc(it.at)}</span>`
            : `<span class="hint">javob berilmagan</span>`}
        </div>
      `).join("")}
      ${canCancel ? `<div class="member-actions"><button data-cancel="${d.day_id}">Jarimani bekor qilish</button></div>` : ""}
    </div>`;
}

/* Faqat tahrirlash (yangi savol — screenBulkQuestion). */
async function screenQuestionForm(employeeId, question, targetLabel) {
  setScreen(`
    <p class="page-title">Savolni tahrirlash</p>
    <div class="field">
      <label>Savol matni</label>
      <textarea id="f-text" maxlength="1000">${esc(question.text)}</textarea>
    </div>
    <div class="field"><label>Muhimligi — javob berilmasa jarima</label>${pointsHtml(question.points ?? state.me.points[0])}</div>
    <p class="hint">O'zgarish ${esc(targetLabel || "bugun")} ${state.me.start} dan kuchga kiradi, o'tgan kunlar tarixi o'zgarmaydi. Xodim chatda matn bilan javob beradi.</p>
  `);
  const points = bindPoints();
  setMainButton("💾 Saqlash", async () => {
    const text = root.querySelector("#f-text").value.trim();
    if (!text) return showError("Savol matnini kiriting.");
    try {
      await api(`/questions/${question.id}`, { method: "PUT", body: JSON.stringify({ text, points: points() }) });
      await goBack();
    } catch (err) {
      showError(err.message);
    }
  }, ACCENT);
}

/* ---------- Nazoratchilar (faqat admin) ---------- */

async function screenViewers() {
  loading();
  const data = await api("/viewers");
  setScreen(`
    <p class="page-title">Rahbarlar (${data.leaders.length})</p>
    <p class="page-sub">Rahbar hamma xodimni ko'radi, savol yozadi va ${state.me.deadline} da to'liq yig'ma oladi. Sozlama, jarimani bekor qilish, rahbar/nazoratchi tayinlash — faqat adminda.</p>
    ${data.leaders.map((l) => `
      <div class="fin-card">
        <div class="top"><span class="task">${esc(l.full_name)}</span></div>
        <div class="member-actions"><button data-unlead="${l.id}">Rahbarlikdan olish</button></div>
      </div>`).join("")}
    <button class="nav-card" id="add-leader"><span class="ic">${icon("users")}</span><span class="grow">Rahbar qo'shish</span><span class="chev">›</span></button>
    <p class="page-title" style="margin-top:18px">Nazoratchilar (${data.viewers.length})</p>
    <p class="page-sub">Nazoratchi faqat o'ziga biriktirilgan xodimlarning javoblarini ko'radi, savollarini tahrirlaydi va har kuni ${state.me.deadline} da yig'ma oladi. Admin hammasini ko'radi.</p>
    ${data.viewers.length ? data.viewers.map((v, i) => `
      <button class="emp-row" data-i="${i}">
        <span class="dot-status on"></span>
        <span class="grow"><div class="name">${esc(v.full_name)}</div><div class="role">${v.employee_ids.length} ta xodimni ko'radi</div></span>
        <span class="badge">${v.employee_ids.length}</span><span class="chev">›</span>
      </button>
    `).join("") : `<p class="empty-state">Hali nazoratchi yo'q</p>`}
  `);
  root.querySelectorAll(".emp-row").forEach((el) => {
    el.onclick = () => show(screenViewerEdit, data.viewers[Number(el.dataset.i)].id);
  });
  root.querySelector("#add-leader").onclick = () => show(screenViewerPick, true);
  root.querySelectorAll("[data-unlead]").forEach((btn) => {
    btn.onclick = async () => {
      if (!(await confirmAction("Rahbarlikdan olinsinmi? Panel unga endi ko'rinmaydi."))) return;
      try {
        await api(`/leaders/${btn.dataset.unlead}`, { method: "PUT", body: JSON.stringify({ on: false }) });
        await replaceTop(screenViewers);
      } catch (err) {
        showError(err.message);
      }
    };
  });
  setMainButton("➕ Nazoratchi qo'shish", () => show(screenViewerPick, false), ACCENT);
}

/* leader=true — rahbar tanlash (bosilganda darhol qo'shiladi), aks holda nazoratchi. */
async function screenViewerPick(leader) {
  loading();
  const data = await api("/viewers");
  const taken = new Set((leader ? data.leaders : data.viewers).map((v) => v.id));
  const candidates = data.employees.filter((e) => !taken.has(e.id) && e.role !== "admin");
  setScreen(`
    <p class="page-title">${leader ? "Rahbar" : "Nazoratchi"} tanlang</p>
    <div class="field"><input id="f-search" placeholder="Ism bo'yicha qidirish..." /></div>
    ${candidates.map((e, i) => `
      <button class="emp-row" data-i="${i}" data-name="${esc(e.full_name.toLowerCase())}">
        <span class="grow"><div class="name">${esc(e.full_name)}</div><div class="role">${esc(ROLE_LABELS[e.role] || e.role)}</div></span>
        <span class="chev">›</span>
      </button>
    `).join("")}
  `);
  bindSearch();
  root.querySelectorAll(".emp-row").forEach((el) => {
    const id = candidates[Number(el.dataset.i)].id;
    el.onclick = async () => {
      if (!leader) return replaceTop(screenViewerEdit, id);
      try {
        await api(`/leaders/${id}`, { method: "PUT", body: JSON.stringify({ on: true }) });
        await goBack();
      } catch (err) {
        showError(err.message);
      }
    };
  });
}

async function screenViewerEdit(viewerId) {
  loading();
  const data = await api("/viewers");
  const viewer = data.employees.find((e) => e.id === viewerId);
  const current = new Set((data.viewers.find((v) => v.id === viewerId) || { employee_ids: [] }).employee_ids);
  setScreen(`
    <p class="page-title">${esc(viewer ? viewer.full_name : "—")}</p>
    <p class="page-sub">Qaysi xodimlarning javoblarini ko'radi? Tanlangan: <b id="cnt">${current.size}</b></p>
    <div class="field"><input id="f-search" placeholder="Ism bo'yicha qidirish..." /></div>
    ${data.employees.filter((e) => e.id !== viewerId).map((e) => `
      <label class="check-row" data-name="${esc(e.full_name.toLowerCase())}">
        <input type="checkbox" value="${e.id}" ${current.has(e.id) ? "checked" : ""} />
        <span class="grow">${esc(e.full_name)} <span class="hint">${esc(ROLE_LABELS[e.role] || e.role)}</span></span>
      </label>
    `).join("")}
    <p class="hint">Hammasidan belgini olib saqlasangiz — nazoratchi ro'yxatdan chiqariladi.</p>
  `);
  bindSearch();
  root.querySelectorAll(".check-row input").forEach((el) => {
    el.onchange = () => { root.querySelector("#cnt").textContent = root.querySelectorAll(".check-row input:checked").length; };
  });
  setMainButton("💾 Saqlash", async () => {
    const ids = [...root.querySelectorAll(".check-row input:checked")].map((i) => Number(i.value));
    try {
      await api(`/viewers/${viewerId}`, { method: "PUT", body: JSON.stringify({ employee_ids: ids }) });
      await goBack();
    } catch (err) {
      showError(err.message);
    }
  }, ACCENT);
}

/* ---------- Profil va sozlama ---------- */

async function screenProfile() {
  const me = state.me;
  const initials = me.full_name.split(" ").map((w) => w[0]).slice(0, 2).join("");
  setScreen(`
    <p class="page-title">Profil</p>
    <div class="profile-head">
      <div class="avatar-circle">${esc(initials)}</div>
      <p class="greet" style="margin:2px 0 0">${esc(me.full_name)}</p>
      <p class="greet-wave" style="margin:0">${esc(ROLE_LABELS[me.role] || me.role)}${me.is_admin ? "" : me.is_leader ? " · rahbar" : " · nazoratchi"}</p>
    </div>
    <div class="panel">
      <div class="kv-row"><span class="k">Savollar keladi</span><span class="v">${state.me.start}</span></div>
      <div class="kv-row"><span class="k">Eslatmalar</span><span class="v">${state.me.reminders.join(" · ") || "—"}</span></div>
      <div class="kv-row"><span class="k">Jarima va yig'ma</span><span class="v">${state.me.deadline}</span></div>
    </div>
    ${me.is_admin ? `
      <p class="section-lbl">Boshqaruv</p>
      <button class="nav-card" id="nav-settings"><span class="ic">${icon("settings")}</span><span class="grow">Sozlamalar</span><span class="chev">›</span></button>
    ` : ""}
  `);
  const settingsBtn = root.querySelector("#nav-settings");
  if (settingsBtn) settingsBtn.onclick = () => show(screenSettings);
}

const REMIND_OPTIONS = [0, 15, 20, 30, 45, 60, 90, 120];

/* Hamma sozlama bitta formada (faqat admin). */
async function screenSettings() {
  loading();
  const s = await api("/settings");
  if (!REMIND_OPTIONS.includes(s.remind_every_min)) REMIND_OPTIONS.push(s.remind_every_min);
  setScreen(`
    <p class="page-title">Sozlamalar</p>
    <p class="section-lbl">Kun jadvali (Toshkent vaqti)</p>
    <div class="field"><label>Savollar yuboriladi</label><input id="f-start" type="time" value="${esc(s.start_time)}" /></div>
    <div class="field"><label>Javob muddati — shu vaqtda jarima va yig'ma</label><input id="f-deadline" type="time" value="${esc(s.deadline_time)}" /></div>
    <div class="field"><label>Eslatma oralig'i</label>
      <select id="f-every">${REMIND_OPTIONS.sort((a, b) => a - b).map((m) =>
        `<option value="${m}" ${m === s.remind_every_min ? "selected" : ""}>${m ? `har ${m} daqiqada` : "eslatma yo'q"}</option>`).join("")}</select></div>
    <p class="hint">Kechqurun savollar ketib, javob kutilayotgan paytda (yuborish va muddat oralig'ida) jadvalni o'zgartirib bo'lmaydi.</p>
    <p class="section-lbl">Ball tizimi</p>
    <div class="field"><label>Savol muhimligi variantlari (ball, bo'sh joy bilan)</label>
      <input id="f-points-list" inputmode="decimal" value="${esc(s.points.map(fmtPts).join(" "))}" placeholder="0,2 0,5 0,7 1" /></div>
    <p class="hint">Savol yozilganda shulardan biri tanlanadi: javob berilmasa shuncha jarima. 1 dan 6 tagacha qiymat, 0,05–10.</p>
    <div class="field"><label>Balli yo'q eski savollar uchun jarima</label>
      <input id="f-rate" type="number" inputmode="decimal" min="0" max="10" step="0.05" value="${esc(s.penalty_per_question)}" /></div>
    <p class="hint">Jarima umumiy KPI ballga yoziladi va oylik kunini suradi (asosiy tizim sozlamasi bo'yicha).</p>
  `);
  setMainButton("💾 Saqlash", async () => {
    const points = root.querySelector("#f-points-list").value.trim().split(/[\s;]+/).filter(Boolean)
      .map((x) => Number(x.replace(",", ".")));
    if (!points.length || points.some((p) => !Number.isFinite(p))) return showError(ERRORS.points_list);
    const body = {
      start_time: root.querySelector("#f-start").value,
      deadline_time: root.querySelector("#f-deadline").value,
      remind_every_min: Number(root.querySelector("#f-every").value),
      points,
      penalty_per_question: Number(root.querySelector("#f-rate").value.replace(",", ".")),
    };
    try {
      await api("/settings", { method: "PUT", body: JSON.stringify(body) });
      state.me = await api("/me");
      showError("Saqlandi.");
      await goBack();
    } catch (err) {
      showError(err.message);
    }
  }, ACCENT);
}

/* ---------- Bootstrap ---------- */

function applyTheme(scheme) {
  document.documentElement.setAttribute("data-theme", scheme === "dark" ? "dark" : "light");
}

async function bootstrap() {
  const splash = window.MeloresSplash;
  await Promise.all([_bootstrap(), splash ? splash.ready : Promise.resolve()]);
  if (splash) splash.hide();
}

async function _bootstrap() {
  const app = tg();
  if (app) {
    app.ready();
    app.expand();
    applyTheme(app.colorScheme);
    app.onEvent("themeChanged", () => applyTheme(app.colorScheme));
    app.BackButton.onClick(goBack);
  } else {
    applyTheme("light");
  }

  try {
    state.me = await api("/me");
  } catch (e) {
    setScreen(`<p class="error-banner">${esc(e.message)}</p>${e.status === 403 ? "" : `<button class="btn" id="btn-retry">Qayta urinish</button>`}`);
    const retry = root.querySelector("#btn-retry");
    if (retry) retry.onclick = bootstrap;
    return;
  }
  const defs = tabDefs();
  nav.section = defs[0].key;
  await resetTo(defs[0].screen);
  // 21:00 yig'madagi "Javoblarni ko'rish" tugmasi: ?screen=answers&day=YYYY-MM-DD
  const params = new URLSearchParams(location.search);
  if (params.get("screen") === "answers") await show(screenDayAnswers, params.get("day"));
}

bootstrap();
