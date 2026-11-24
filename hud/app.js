// J.A.R.V.I.S HUD — основной экран: сфера, система, время и погода, вкладки с плитками, диалог,
// настройки, спящий режим. Связь с ядром по WebSocket.
"use strict";

const $ = (id) => document.getElementById(id);
const Q = new URLSearchParams(location.search);
// В Wallpaper Engine интерфейс — локальный файл: ядро пускает его только с ключом (token в адресе),
// а картинки плиток берутся с сервера ядра.
const TOKEN = Q.get("token") || "";
const WS_URL = `ws://${location.hostname || "127.0.0.1"}:8766` + (TOKEN ? `/?token=${encodeURIComponent(TOKEN)}` : "");
const ASSET_BASE = location.protocol === "file:" ? "http://127.0.0.1:8765/" : "";
// ?wallpaper — интерфейс на обоях рабочего стола. Он рабочий (клики и клавиши пересылает wallpaper.py),
// но без камеры: окно обоев не может показать запрос разрешения, а его «камера выключена» при
// подключении выключало бы камеру, включённую в обычном окне.
const WALLPAPER = Q.has("wallpaper");
if (WALLPAPER) {
  document.documentElement.classList.add("wallpaper");
  for (const k of ["left", "top", "right", "bottom"])  // отступы под панель задач (от wallpaper/index.html)
    document.documentElement.style.setProperty(`--wp-${k}`, `${parseInt(Q.get(k) || "0", 10)}px`);
}

// состояния сферы: цвет, подпись, скорость колец (с), скорость вращения сферы
const MODES = {
  idle:      { c: "#2EE6C8", label: "ОЖИДАНИЕ · скажите «Джарвис»", dur: 16, spin: 0.3 },
  listening: { c: "#3FA9FF", label: "СЛУШАЮ…", dur: 9, spin: 0.6 },
  thinking:  { c: "#FFB347", label: "ДУМАЮ…", dur: 3, spin: 1.8 },
  agent:     { c: "#FFB347", label: "АГЕНТ РАБОТАЕТ", dur: 3, spin: 1.8 },
  speaking:  { c: "#45F0A0", label: "ГОВОРЮ", dur: 7, spin: 0.8 },
  alert:     { c: "#FF5A5F", label: "НУЖНО ПОДТВЕРЖДЕНИЕ", dur: 5, spin: 1.1 },
  muted:     { c: "#6B7A89", label: "МИКРОФОН ВЫКЛЮЧЕН", dur: 40, spin: 0.08 },
  sleep:     { c: "#3E5266", label: "СПЯЩИЙ РЕЖИМ", dur: 80, spin: 0.04 },
  offline:   { c: "#6B7A89", label: "НЕТ СВЯЗИ С ЯДРОМ", dur: 40, spin: 0.08 },
};
const AUTONOMY_HINT = {
  chat: "Только разговор и чтение: никаких действий на компьютере.",
  confirm: "Любое действие (кроме чтения) — только после вашего «да».",
  auto: "Обычные действия — сразу. Опасные (удаление, закрытие окон, команды) — всё равно после вашего «да».",
};
const TAB_TITLE = { apps: "Программы", games: "Игры", files: "Файлы" };

let ws = null, online = false;
let status = { state: "idle", mic: true };
let settings = { log: true, hints: false, city: "", memory: true, tiles: { apps: [], games: [], files: [] } };
let level = 0, levelTarget = 0;
let liveMsg = null, holding = false, userSpeaking = false, agentOn = false, pttUntil = 0;
let tab = "home", confirmId = null, msgCount = 0;

// ════════════ связь ════════════
function connect() {
  ws = new WebSocket(WS_URL);
  ws.onopen = () => {
    online = true;
    if (!WALLPAPER) ws.send(JSON.stringify({ type: "camera", on: !!camStream }));
    applyMode();
  };
  ws.onclose = () => { online = false; applyMode(); setTimeout(connect, 1500); };
  ws.onmessage = (e) => {
    try { handle(JSON.parse(e.data)); } catch (err) { console.error("HUD:", err); }
  };
}
function send(msg) {
  if (WALLPAPER && (msg.type === "camera" || msg.type === "camera_frame")) return;
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(msg));
  else toast("Нет связи с ядром Джарвиса", "alert");
}

function handle(m) {
  switch (m.type) {
    case "status": applyStatus(m); break;
    case "settings": applySettings(m); break;
    case "sys": applySys(m); break;
    case "weather": applyWeather(m); break;
    case "google": applyGoogle(m); break;
    case "wheel": wheelScroll(m); break;
    case "level": levelTarget = m.value; break;
    case "vad": userSpeaking = !!m.speaking; applyMode(); if (m.speaking) setSub("heard", "…"); break;
    case "heard": setSub("heard", `«${m.text}»`, 6000); break;
    case "partial": setSub("heard", `${m.text}…`); break;
    case "ignored": setSub("heard", `«${m.text}» — начните со слова «Джарвис»`, 5000); break;
    case "speaking_text":
      setSub("saying", m.text, 9000);
      if (holding) { holding = false; applyMode(); }
      break;
    case "hold": holding = !!m.on; applyMode(); break;
    case "msg": addMsg(m.role, m.text); break;
    case "msg_start": startLive(); break;
    case "msg_delta":
      if (!liveMsg) startLive();
      liveMsg.querySelector(".t").textContent += m.text;
      scrollLog();
      break;
    case "msg_end": endLive(); break;
    case "interrupted": addSystem("перебит"); break;
    case "toast": toast(m.text); break;
    case "alert": toast(m.text, "alert"); addSystem(m.text, true); break;
    case "device":
      if (!m.mic) $("micName").textContent = "недоступен";
      if (m.name) $("micName").textContent = m.name;
      break;
    case "action": addAction(m); break;
    case "confirm": showConfirm(m); break;
    case "confirm_done": hideConfirm(); break;
    case "capture": flashCapture(m.kind === "camera" ? "◉ СНИМОК С КАМЕРЫ" : "◉ СНИМОК ЭКРАНА"); break;
    case "camera_snap": snapCamera(m.id); break;
    case "camera_request":
      $("btnCam").classList.add("ask");
      toast(`Джарвис просит включить камеру: ${m.reason || "посмотреть"}. Нажмите кнопку камеры внизу.`);
      setTimeout(() => $("btnCam").classList.remove("ask"), 20000);
      break;
    case "agent":
      agentOn = !!m.on;
      $("agentLine").classList.toggle("hidden", !agentOn);
      $("agentNote").textContent = agentOn ? (m.task || "") : "";
      if (agentOn) addSystem("задача передана агенту Opus");
      applyMode();
      break;
    case "agent_note": $("agentNote").textContent = m.text; break;
  }
}

// ════════════ состояние → цвет и подпись ════════════
function currentMode() {
  const s = status;
  if (!online && !Q.get("demo")) return "offline";
  if (s.sleeping) return "sleep";
  if (confirmId !== null) return "alert";
  if (agentOn) return "agent";
  if (s.state === "thinking") return "thinking";
  if (s.state === "speaking" && !holding) return "speaking";
  if (!s.mic) return "muted";
  if (holding || userSpeaking || Date.now() < pttUntil) return "listening";
  if (s.state === "listening" && (!s.wake || s.awake)) return "listening";
  return "idle";
}
let mode = "idle";
let dialPeriods = [64, -48, 32];
const dialAngles = [0, 0, 0];
const dialEls = ["dial1", "dial2", "dial3"].map((id) => document.getElementById(id));
function applyMode() {
  mode = currentMode();
  const p = MODES[mode];
  document.documentElement.style.setProperty("--accent", p.c);
  $("status").textContent = p.label;
  $("orb").setAttribute("aria-label", p.label);
  dialPeriods = [p.dur * 4, -p.dur * 3, p.dur * 2];  // секунд на оборот (минус — в обратную сторону)
  document.querySelectorAll(".dyn").forEach((el) => el.setAttribute("stroke", p.c));
  document.querySelectorAll(".dynfill").forEach((el) => el.setAttribute("fill", p.c));
}

function applyStatus(s) {
  status = s;
  $("btnMic").classList.toggle("on", !!s.mic);
  $("btnMic").setAttribute("aria-pressed", s.mic ? "true" : "false");
  $("model").textContent = s.model || "—";
  $("ttsInfo").textContent = s.voice === false ? "выключен" : s.tts ? `${s.tts}${s.synth_ms ? " · " + s.synth_ms + " мс" : ""}` : "—";
  document.querySelectorAll("#autonomy button").forEach((b) => {
    const on = b.dataset.level === s.autonomy;
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", on ? "true" : "false");
  });
  $("autonomyHint").textContent = AUTONOMY_HINT[s.autonomy] || "";
  $("sleepOv").classList.toggle("hidden", !s.sleeping);
  if (s.sleeping && camStream) setCamera(false);
  renderSwitches();
  applyMode();
}

// ════════════ система ════════════
const HIST = 40;
const hist = { cpu: [], gpu: [], ram: [], disk: [] };
const METERS = [
  { k: "cpu", label: "CPU", unit: "%", max: 100 },
  { k: "gpu", label: "GPU", unit: "%", max: 100 },
  { k: "ram", label: "ОЗУ", unit: "%", max: 100 },
  { k: "disk", label: "ДИСК", unit: " МБ/с", max: 150 },
];
(function buildMeters() {
  $("meters").innerHTML = METERS.map((m) => `
    <div class="card meter" style="margin-top:10px">
      <div class="meter-l"><span class="lbl">${m.label}</span><span class="meter-v" id="mv-${m.k}">—</span></div>
      <svg viewBox="0 0 160 40" preserveAspectRatio="none" aria-hidden="true">
        <path d="M0 10H160M0 20H160M0 30H160" stroke="rgba(143,163,181,0.1)" stroke-width="1"/>
        <path class="fade dynfill" id="ma-${m.k}" d="" fill-opacity="0.14"/>
        <polyline class="fade dyn" id="ml-${m.k}" points="" fill="none" stroke-width="1.6" stroke-linejoin="round"/>
      </svg>
    </div>`).join("");
})();
const tColor = (v) => v < 60 ? "#5BE49B" : v < 80 ? "#FFB347" : "#FF5A5F";

function applySys(m) {
  for (const def of METERS) {
    const v = m[def.k];
    const arr = hist[def.k];
    arr.push(v == null ? 0 : v);
    if (arr.length > HIST) arr.shift();
    const max = def.k === "disk" ? Math.max(def.max, ...arr) : def.max;
    $(`mv-${def.k}`).innerHTML = v == null ? "—" : `${Math.round(v)}<small>${def.unit}</small>`;
    const step = 160 / (HIST - 1), off = HIST - arr.length;
    const pts = arr.map((x, i) => `${((i + off) * step).toFixed(1)},${(38 - Math.min(1, x / max) * 34).toFixed(1)}`);
    $(`ml-${def.k}`).setAttribute("points", pts.join(" "));
    $(`ma-${def.k}`).setAttribute("d", pts.length ? `M${(off * step).toFixed(1)},40 L${pts.join(" L")} L160,40 Z` : "");
  }
  for (const [id, v, src] of [["tCpu", m.cpuT, "LibreHardwareMonitor не запущен (tools\\lhm_autostart.ps1)"], ["tGpu", m.gpuT, "нет данных от видеокарты"]]) {
    const el = $(id), val = el.querySelector(".temp-v"), bar = el.querySelector(".bar b");
    if (v == null) {
      val.innerHTML = "—"; val.style.color = ""; bar.style.width = "0"; el.title = "Нет датчика: " + src;
    } else {
      val.innerHTML = `${Math.round(v)}°<small>C</small>`; val.style.color = tColor(v);
      bar.style.width = Math.min(100, v) + "%"; bar.style.background = tColor(v); el.title = "";
    }
  }
}

// ════════════ погода ════════════
const W_ICON = {
  sun: '<circle cx="12" cy="12" r="4.5"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  "cloud-sun": '<path d="M8 3.5V5M3.5 8H5M4.8 4.8l1 1M12.5 6.3A4 4 0 0 0 5.6 10"/><path d="M17.5 19H8a4 4 0 1 1 .9-7.9A5.5 5.5 0 0 1 19 12a3.5 3.5 0 0 1-1.5 7z"/>',
  cloud: '<path d="M17.5 18H7a4 4 0 1 1 .9-7.9A5.5 5.5 0 0 1 18.5 11a3.5 3.5 0 0 1-1 7z"/>',
  rain: '<path d="M17.5 15H7a4 4 0 1 1 .9-7.9A5.5 5.5 0 0 1 18.5 8a3.5 3.5 0 0 1-1 7z"/><path d="M8 18l-1 3M12 18l-1 3M16 18l-1 3"/>',
  snow: '<path d="M17.5 15H7a4 4 0 1 1 .9-7.9A5.5 5.5 0 0 1 18.5 8a3.5 3.5 0 0 1-1 7z"/><path d="M8 19.5h.01M12 20.5h.01M16 19.5h.01"/>',
  storm: '<path d="M17.5 15H7a4 4 0 1 1 .9-7.9A5.5 5.5 0 0 1 18.5 8a3.5 3.5 0 0 1-1 7z"/><path d="M12 15l-2 4h4l-2 4"/>',
  fog: '<path d="M4 10h16M3 14h18M5 18h14M7 6h10"/>',
};
const deg = (v) => (v < 0 ? "−" + Math.abs(v) : v) + "°";
function applyWeather(w) {
  if (w.error) {
    $("wCity").textContent = "ПОГОДА";
    $("wBody").innerHTML = `<span class="w-msg"></span> <button class="link" id="wSetCity">Указать город</button>`;
    $("wBody").querySelector(".w-msg").textContent = w.error;
    $("wSetCity").onclick = () => openSettings(true);
    return;
  }
  $("wCity").textContent = `ПОГОДА · ${w.city.toUpperCase()}`;
  $("wBody").innerHTML = `
    <div class="w-now">
      <svg viewBox="0 0 24 24" class="fade" aria-hidden="true">${W_ICON[w.icon] || W_ICON.cloud}</svg>
      <div style="display:flex;flex-direction:column"><span class="w-temp">${deg(w.temp)}</span><span class="w-desc"></span></div>
    </div>
    <div class="w-grid" style="margin-top:14px">
      <div><span class="lbl">ОЩУЩАЕТСЯ</span>${deg(w.feels)}</div>
      <div><span class="lbl">ВЕТЕР</span>${w.wind} м/с</div>
      <div><span class="lbl">ВЛАЖН.</span>${w.humidity}%</div>
    </div>
    <div class="w-grid w-days" style="margin-top:14px">${(w.days || []).map((d) =>
      `<div><span class="lbl" style="font-size:13px;letter-spacing:0">${d.day}</span><span style="color:var(--text)">${deg(d.max)} / ${deg(d.min)}</span></div>`).join("")}</div>`;
  $("wBody").querySelector(".w-desc").textContent = w.desc;
}

// ════════════ Google: подключение и сводка ════════════
const G_TEXT = {
  no_client: "Нужен ключ приложения Google (google_client_secret.json) — см. README, раздел «Google».",
  not_connected: "Не подключён.", unknown: "Проверяю…",
};
let gSummary = null;
function applyGoogleStatus(g) {
  g = g || { state: "unknown" };
  const on = g.state === "connected";
  $("gStatus").textContent = on ? `Подключён${g.email ? ": " + g.email : ""}.` : g.state === "error" ? "Ошибка: " + (g.error || "") : G_TEXT[g.state] || "—";
  $("btnGoogleConnect").classList.toggle("hidden", on);
  $("btnGoogleConnect").disabled = g.state === "no_client";
  $("btnGoogleDisconnect").classList.toggle("hidden", !on);
  if (!on) {
    $("gBody").innerHTML = `<span class="w-msg"></span> <button class="link" id="gSetup">Подключить</button>`;
    $("gBody").querySelector(".w-msg").textContent = g.state === "no_client" ? "Google ещё не настроен." : "Google не подключён.";
    $("gSetup").onclick = () => openSettings(false);
  } else if (gSummary) applyGoogle(gSummary);
}
function applyGoogle(m) {
  gSummary = m;
  if (!settings.google || settings.google.state !== "connected") return;
  const mail = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/></svg>';
  const cal = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="17" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>';
  let next = "Сегодня и завтра событий нет";
  if (m.next) {
    const d = new Date(m.next.start);
    const today = new Date().toDateString() === d.toDateString();
    next = `<b></b> · ${today ? "сегодня" : "завтра"} в ${d.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}`;
  }
  $("gBody").innerHTML = `
    <div class="g-row">${mail}<span>${m.unread == null ? "Почта: —" : `Непрочитанных писем: <b>${m.unread}</b>`}</span></div>
    <div class="g-row" style="margin-top:8px">${cal}<span id="gNext">${next}</span></div>`;
  if (m.next) $("gNext").querySelector("b").textContent = m.next.title;
}
$("btnGoogleConnect").onclick = () => send({ type: "google_connect" });
$("btnGoogleDisconnect").onclick = () => send({ type: "google_disconnect" });

// ════════════ вкладки и плитки ════════════
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
function setTab(t) {
  tab = t;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  $("home").classList.toggle("hidden", t !== "home");
  $("folder").classList.toggle("hidden", t === "home");
  if (t !== "home") renderTiles();
}
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function renderTiles() {
  if (tab === "home") return;
  $("folderTitle").textContent = TAB_TITLE[tab];
  const items = (settings.tiles && settings.tiles[tab]) || [];
  // в «Играх» плитки шире — под обложки Steam
  $("tiles").classList.toggle("covers", items.some((it) => it.kind === "cover"));
  const letter = (it) => esc((it.name.replace(/[^\p{L}\p{N}]/gu, "") || "?").charAt(0).toUpperCase());
  const text = (it) => `<span class="tile-body" style="display:flex;flex-direction:column;gap:2px">
          <span class="tile-name">${esc(it.name)}</span><span class="tile-hint">${esc(it.hint || "")}</span></span>`;
  const tiles = items.map((it, i) => `
    <div class="tile-wrap">
      <button class="tile${it.kind === "cover" ? " cover" : ""}" data-i="${i}" title="${esc(it.target)}">
        ${it.kind === "cover" ? `<img src="${ASSET_BASE}${esc(it.img)}" alt="" loading="lazy">`
          : it.img ? `<img class="tile-img" src="${ASSET_BASE}${esc(it.img)}" alt="" loading="lazy">`
          : `<span class="tile-ic">${letter(it)}</span>`}
        ${text(it)}
      </button>
      <button class="tile-x" data-x="${i}" aria-label="Убрать плитку «${esc(it.name)}»">
        <svg class="ic" viewBox="0 0 24 24" aria-hidden="true" style="width:16px;height:16px"><path d="M18 6 6 18M6 6l12 12"/></svg>
      </button>
    </div>`).join("");
  // «Добавить» открывает окно выбора Проводника (его показывает ядро) — путь вручную вводить не нужно
  const plus = '<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>';
  // во «Файлах» одна плитка «Добавить» с выбором внутри: Windows открывает разные окна для папки и файла
  const add = tab === "files"
    ? `<div class="tile add add-split">
         <span class="add-title">${plus}Добавить</span>
         <span class="add-choices">
           <button class="btn-sm" data-pick="folder">Папку</button>
           <button class="btn-sm" data-pick="file">Файл</button>
         </span>
       </div>`
    : `<button class="tile add" data-pick="file">${plus}Добавить ${tab === "games" ? "игру" : "программу"}</button>`;
  $("tiles").innerHTML = tiles + add;
  $("tiles").querySelectorAll(".tile[data-i]").forEach((b) => b.addEventListener("click", () => {
    send({ type: "launch", tab, idx: +b.dataset.i });
    toast(`Открываю «${items[+b.dataset.i].name}»`);
  }));
  $("tiles").querySelectorAll(".tile-x").forEach((b) => b.addEventListener("click", () =>
    send({ type: "tile_remove", tab, idx: +b.dataset.x })));
  $("tiles").querySelectorAll("[data-pick]").forEach((b) => b.addEventListener("click", () => {
    send({ type: "tile_pick", tab, kind: b.dataset.pick });
    toast("Открываю Проводник — выберите " + (b.dataset.pick === "folder" ? "папку" : "файл"));
  }));
}

// ════════════ настройки ════════════
const SWITCHES = [
  { k: "wake", label: "Слово «Джарвис»", hint: "Без него — слушать 20 с после ответа", get: () => status.wake, set: (on) => send({ type: "wake", on }) },
  { k: "barge", label: "Перебивание голосом", hint: "Можно говорить поверх ответа", get: () => status.barge, set: (on) => send({ type: "barge", on }) },
  { k: "voice", label: "Озвучивать ответы", hint: "Иначе — только текст на экране", get: () => status.voice !== false, set: (on) => send({ type: "voice", on }) },
  { k: "memory", label: "Память", hint: "Запоминать факты и предпочтения", get: () => settings.memory, set: (on) => send({ type: "pref", key: "memory", on }) },
  { k: "screen", label: "Доступ к экрану", hint: "Снимки окон только по запросу", get: () => status.screen !== false, set: (on) => send({ type: "screen", on }) },
  { k: "log", label: "Журнал действий", hint: "Показывать в диалоге, что сделал Джарвис", get: () => settings.log, set: (on) => send({ type: "pref", key: "log", on }) },
  { k: "hints", label: "Подсказки на экране", hint: "Показывать горячие клавиши", get: () => settings.hints, set: (on) => send({ type: "pref", key: "hints", on }) },
];
function renderSwitches() {
  $("switches").innerHTML = SWITCHES.map((s) => `
    <button class="sw" role="switch" data-k="${s.k}" aria-checked="${s.get() ? "true" : "false"}">
      <span><span class="t1">${s.label}</span><span class="t2">${s.hint}</span></span>
      <span class="track"><i></i></span>
    </button>`).join("");
  $("switches").querySelectorAll(".sw").forEach((b) => b.addEventListener("click", () => {
    const s = SWITCHES.find((x) => x.k === b.dataset.k);
    s.set(!s.get());
  }));
}
let tilesKey = "";
function applySettings(m) {
  settings = m;
  // настройки приходят и при каждой проверке Google (раз в 5 мин) — плитки перерисовываем, только
  // если они изменились, иначе вкладка прыгала бы наверх посреди прокрутки
  const key = JSON.stringify(m.tiles || {});
  const tilesChanged = key !== tilesKey;
  tilesKey = key;
  document.body.classList.toggle("no-log", !m.log);
  $("hints").classList.toggle("hidden", !m.hints);
  if (document.activeElement !== $("cityInput")) $("cityInput").value = m.city || "";
  applyGoogleStatus(m.google);
  renderSwitches();
  if (tilesChanged) renderTiles();
}
function openSettings(focusCity) {
  $("drawer").classList.remove("hidden");
  (focusCity ? $("cityInput") : $("btnCloseSettings")).focus();
}
const closeSettings = () => $("drawer").classList.add("hidden");
$("btnSettings").onclick = () => openSettings(false);
$("btnCloseSettings").onclick = closeSettings;
$("drawerBg").onclick = closeSettings;
$("cityForm").onsubmit = (e) => {
  e.preventDefault();
  send({ type: "city", city: $("cityInput").value });
  toast("Город сохранён, обновляю погоду…");
};
document.querySelectorAll("#autonomy button").forEach((b) =>
  b.addEventListener("click", () => send({ type: "autonomy", level: b.dataset.level })));

// ════════════ спящий режим ════════════
$("btnSleep").onclick = () => { closeSettings(); setCamera(false); send({ type: "sleep", on: true }); };
$("btnWake").onclick = () => send({ type: "sleep", on: false });

// ════════════ субтитры, диалог, журнал ════════════
const subTimers = {};
function setSub(id, text, clearAfter = 0) {
  $(id).textContent = text;
  clearTimeout(subTimers[id]);
  if (clearAfter) subTimers[id] = setTimeout(() => { $(id).textContent = ""; }, clearAfter);
}
function timeNow() { return new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }); }
function hideEmpty() { const e = $("logEmpty"); if (e) e.remove(); }
function plural(n) {
  const a = n % 10, b = n % 100;
  return a === 1 && b !== 11 ? "сообщение" : a >= 2 && a <= 4 && (b < 12 || b > 14) ? "сообщения" : "сообщений";
}
function bumpCount(d) { msgCount += d; $("dialogLabel").textContent = `Диалог · ${msgCount} ${plural(msgCount)}`; }
function addMsg(role, text) {
  hideEmpty();
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `${role === "user" ? "ВЫ" : "J.A.R.V.I.S."} · ${timeNow()}`;
  const t = document.createElement("p");
  t.className = "t";
  t.textContent = text;
  el.append(meta, t);
  $("log").appendChild(el);
  bumpCount(1);
  scrollLog();
  return el;
}
function addSystem(text, alert = false) {
  hideEmpty();
  const el = document.createElement("div");
  el.className = "msg system" + (alert ? " alert" : "");
  el.textContent = `— ${text} —`;
  $("log").appendChild(el);
  scrollLog();
}
function startLive() { endLive(); liveMsg = addMsg("assistant", ""); liveMsg.classList.add("live"); }
function endLive() {
  if (liveMsg) {
    liveMsg.classList.remove("live");
    if (!liveMsg.querySelector(".t").textContent.trim()) { liveMsg.remove(); bumpCount(-1); }
  }
  liveMsg = null;
}
function scrollLog() { const l = $("log"); l.scrollTop = l.scrollHeight; }

const actionRows = new Map();
const ACTION_ICON = { "выполняю": "⟳", "готово": "✓", "отклонено": "⛔", "запрещено": "⛔", "остановлено": "■" };
function addAction(m) {
  hideEmpty();
  let el = actionRows.get(m.text);
  if (!el || (m.status === "выполняю" && el.dataset.final === "1")) {
    el = document.createElement("div");
    el.innerHTML = `<span class="ic"></span><span class="d"></span><span class="st"></span>`;
    el.querySelector(".d").textContent = m.text;
    if (liveMsg && liveMsg.parentNode) $("log").insertBefore(el, liveMsg); else $("log").appendChild(el);
    actionRows.set(m.text, el);
  }
  const running = m.status === "выполняю", ok = m.status === "готово";
  el.className = `action ${m.risk}${running ? " run" : ok ? " ok" : " fail"}`;
  el.querySelector(".ic").textContent = ACTION_ICON[m.status] || "✕";
  el.querySelector(".st").textContent = m.status;
  el.dataset.final = running ? "0" : "1";
  scrollLog();
}
$("btnClear").onclick = () => {
  $("log").innerHTML = "";
  actionRows.clear();
  liveMsg = null;
  msgCount = 0; bumpCount(0);
};
$("btnDialog").onclick = () => {
  const open = $("dialog").classList.toggle("hidden") === false;
  $("btnDialog").setAttribute("aria-expanded", open ? "true" : "false");
  $("dialogArrow").textContent = open ? "▴" : "▾";
  if (open) scrollLog();
};

function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = text;
  $("toasts").appendChild(el);
  while ($("toasts").children.length > 4) $("toasts").firstChild.remove();
  setTimeout(() => el.remove(), kind === "alert" ? 3500 : 5000);
}

// ════════════ подтверждения ════════════
let confirmAnim = null;
function showConfirm(m) {
  confirmId = m.id;
  $("confirmText").textContent = m.text;
  const dangerous = m.risk === "dangerous";
  $("confirmRisk").textContent = dangerous ? "ОПАСНОЕ ДЕЙСТВИЕ" : "ПОДТВЕРЖДЕНИЕ";
  document.querySelector(".confirm-box").classList.toggle("normal", !dangerous);
  $("confirm").classList.remove("hidden");
  if (confirmAnim) confirmAnim.cancel();
  confirmAnim = $("confirmBar").animate([{ transform: "scaleX(1)" }, { transform: "scaleX(0)" }],
    { duration: (m.timeout || 40) * 1000, fill: "forwards", easing: "linear" });
  if (!WALLPAPER) $("confirmYes").focus();
  applyMode();
}
function hideConfirm() {
  confirmId = null;
  $("confirm").classList.add("hidden");
  if (confirmAnim) { confirmAnim.cancel(); confirmAnim = null; }
  applyMode();
}
function answerConfirm(ok) {
  if (confirmId === null) return;
  send({ type: "confirm_answer", id: confirmId, ok });
  hideConfirm();
}
$("confirmYes").onclick = () => answerConfirm(true);
$("confirmNo").onclick = () => answerConfirm(false);

// ════════════ управление ════════════
$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (text) send({ type: "text", text });
  $("input").value = "";
});
$("btnStop").onclick = () => send({ type: "stop" });
$("btnMic").onclick = () => send({ type: "mic", on: !status.mic });
$("btnKbd").onclick = () => {
  const on = $("form").classList.toggle("hidden") === false;
  $("btnKbd").classList.toggle("on", on);
  $("btnKbd").setAttribute("aria-pressed", on ? "true" : "false");
  if (on) $("input").focus();
};
const typing = (el) => el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA");
let lastPtt = 0;
document.addEventListener("keydown", (e) => {
  if (confirmId !== null) {
    if (e.key === "Enter") { e.preventDefault(); answerConfirm(true); }
    if (e.key === "Escape") { e.preventDefault(); answerConfirm(false); }
    return;
  }
  if (e.key === "Escape") {
    if (!$("drawer").classList.contains("hidden")) { closeSettings(); return; }
    send({ type: "stop" });
    return;
  }
  if (e.key === "F11") {
    e.preventDefault();
    if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen().catch(() => {});
    return;
  }
  // пробел удерживается — Джарвис слушает без слова «Джарвис»
  if (e.code === "Space" && !typing(document.activeElement) && !(document.activeElement && document.activeElement.tagName === "BUTTON")) {
    e.preventDefault();
    pttUntil = Date.now() + 1500;
    if (Date.now() - lastPtt > 1000) { lastPtt = Date.now(); send({ type: "ptt" }); }
    applyMode();
  }
});
document.addEventListener("keyup", (e) => { if (e.code === "Space") setTimeout(applyMode, 1600); });

// ════════════ камера (включается только кнопкой пользователя) ════════════
let camStream = null;
async function setCamera(on) {
  if (!on) {
    if (camStream) camStream.getTracks().forEach((t) => t.stop());
    camStream = null;
  } else if (!camStream) {
    try {
      camStream = await navigator.mediaDevices.getUserMedia({ video: { width: 1280, height: 720 }, audio: false });
      $("camPreview").srcObject = camStream;
      await $("camPreview").play();
      $("btnCam").classList.remove("ask");
    } catch (err) {
      toast("Нет доступа к камере: " + err.message, "alert");
      camStream = null;
    }
  }
  const live = !!camStream;
  $("btnCam").classList.toggle("on", live);
  $("btnCam").setAttribute("aria-pressed", live ? "true" : "false");
  $("camPreview").classList.toggle("live", live);
  $("camBadge").classList.toggle("hidden", !live);
  send({ type: "camera", on: live });
}
$("btnCam").onclick = () => setCamera(!camStream);

let captureTimer = null;
function flashCapture(label) {
  const b = $("captureBadge");
  b.textContent = label;
  b.classList.remove("hidden");
  document.body.classList.remove("flash");
  void document.body.offsetWidth;
  document.body.classList.add("flash");
  clearTimeout(captureTimer);
  captureTimer = setTimeout(() => { b.classList.add("hidden"); document.body.classList.remove("flash"); }, 1800);
}
function snapCamera(id) {
  const v = $("camPreview");
  if (!camStream || !v.videoWidth) return;  // камера включена не в этом окне — молчим
  const c = document.createElement("canvas");
  const scale = Math.min(1, 960 / v.videoWidth);
  c.width = Math.round(v.videoWidth * scale);
  c.height = Math.round(v.videoHeight * scale);
  c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
  flashCapture("◉ СНИМОК С КАМЕРЫ");
  send({ type: "camera_frame", id, data: c.toDataURL("image/jpeg", 0.75).split(",")[1] });
}

// ════════════ часы ════════════
function tickClock() {
  const d = new Date(), pad = (x) => String(x).padStart(2, "0");
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  let date = d.toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  date = date.charAt(0).toUpperCase() + date.slice(1);
  $("clock").textContent = hm; $("seconds").textContent = ":" + pad(d.getSeconds()); $("date").textContent = date;
  $("sleepClock").textContent = hm; $("sleepDate").textContent = date;
}
setInterval(tickClock, 1000);
tickClock();

// ════════════ сфера из частиц, орбиты, уровень голоса ════════════
const cv = $("sphere");
const ctx = cv.getContext("2d");
const N = 900, PTS = [], GA = Math.PI * (3 - Math.sqrt(5));
for (let i = 0; i < N; i++) {
  const y = 1 - (i / (N - 1)) * 2, r = Math.sqrt(1 - y * y), th = GA * i;
  PTS.push([Math.cos(th) * r, y, Math.sin(th) * r]);
}
const REDUCE = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const levelBars = Array.from({ length: 13 }, () => {
  const s = document.createElement("span"); $("levels").appendChild(s); return s;
});
let rot = 0, last = performance.now(), col = null;
const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(h.substr(i, 2), 16));

function modeLevel(t) {
  switch (mode) {
    case "listening": return Math.max(Math.min(1, levelTarget * 1.6), userSpeaking ? 0.35 : 0.12);
    case "speaking": return 0.25 + 0.75 * Math.abs(Math.sin(t * 9) * Math.sin(t * 2.3));
    case "thinking": case "agent": return 0.4 + 0.12 * Math.sin(t * 5);
    case "alert": return 0.45 + 0.55 * Math.abs(Math.sin(t * 4));
    case "muted": case "offline": return 0.04;
    case "sleep": return 0.03 + 0.03 * Math.sin(t * 0.6);
    default: return 0.14 + 0.06 * Math.sin(t * 1.4);
  }
}

// Спрятанная сфера (интерфейс на обоях, пока стол закрыт окнами) не держит цикл кадров — проверка
// раз в полсекунду. На обоях — 30 кадров/с, в окне — сколько даёт экран.
const FRAME_GAP = WALLPAPER ? 1000 / 30 - 2 : 0;
const BUCKETS = 16;
let lastRing = -1;
const barH = [];
let lastDraw = 0;
function draw(nowMs) {
  if (!cv.clientWidth) { last = performance.now(); setTimeout(() => requestAnimationFrame(draw), 500); return; }
  requestAnimationFrame(draw);
  if (nowMs - lastDraw < FRAME_GAP) return;
  lastDraw = nowMs;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const W = Math.round(cv.clientWidth * dpr);
  if (cv.width !== W) { cv.width = W; cv.height = W; }
  const dt = Math.min(0.05, (nowMs - last) / 1000);
  last = nowMs;
  const now = nowMs / 1000;
  const p = MODES[mode] || MODES.idle;
  const target = hexRgb(p.c);
  col = col || target.slice();
  for (let i = 0; i < 3; i++) col[i] += (target[i] - col[i]) * Math.min(1, dt * 3);
  levelTarget *= 0.9;
  level += (modeLevel(now) - level) * Math.min(1, dt * 12);
  const lvl = level;
  const c = col.map(Math.round).join(",");
  const speed = REDUCE ? 0.05 : p.spin;
  rot += dt * speed;

  // кольцо уровня и полоски под сферой
  // вращение колец — в этом же кадре (слои композитора: поворот без перерисовки)
  if (!REDUCE) dialEls.forEach((el, i) => {
    dialAngles[i] = (dialAngles[i] + dt * 360 / dialPeriods[i]) % 360;
    el.style.transform = `rotate(${dialAngles[i].toFixed(2)}deg)`;
  });
  // трогаем DOM только при изменении — иначе браузер перерисовывал бы их в каждом кадре
  // кольцо уровня — только когда есть звук: слушает речь, говорит, думает. Микрофон выключен, сон,
  // нет связи или просто ожидание «Джарвис» — кольцо пустое (раньше оставался «хвост» ~10–20%)
  const ringActive = mode === "listening" || mode === "speaking" || mode === "thinking" || mode === "agent" || mode === "alert";
  // в «слушаю» у уровня есть нижний порог 0.12 (чтобы сфера шевелилась) — для кольца его вычитаем
  const ringLvl = mode === "listening" ? Math.max(0, lvl - 0.12) / 0.88 : lvl;
  const ringV = ringActive ? Math.round(ringLvl * 100) : 0;
  if (ringV !== lastRing) {
    lastRing = ringV;
    const ring = $("levelRing");
    ring.setAttribute("stroke-dasharray", `${ringV} 100`);
    ring.style.opacity = ringV < 2 ? "0" : "1";  // у скруглённого конца даже 0 рисует точку
  }
  levelBars.forEach((b, i) => {
    const h = Math.round(4 + lvl * 34 * (0.4 + 0.6 * Math.abs(Math.sin(now * 6 + i * 0.85))));
    if (barH[i] !== h) { barH[i] = h; b.style.height = h + "px"; }
  });

  const a = rot, bb = 0.38 + Math.sin(now * 0.3) * 0.08;
  const ca = Math.cos(a), sa = Math.sin(a), cb = Math.cos(bb), sb = Math.sin(bb);
  const cx = W / 2, cy = W / 2, R = W * 0.19, F = W * 1.6;
  const view = (x, y, z) => {
    const x1 = x * ca + z * sa, z1 = -x * sa + z * ca;
    const y1 = y * cb - z1 * sb, z2 = y * sb + z1 * cb;
    const s = F / (F + z2);
    return [cx + x1 * s, cy + y1 * s, z2, s];
  };
  ctx.clearRect(0, 0, W, W);
  ctx.globalCompositeOperation = "lighter";

  const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, R * (1.5 + lvl * 0.6));
  g.addColorStop(0, `rgba(${c},${0.35 + lvl * 0.4})`);
  g.addColorStop(0.45, `rgba(${c},${0.12 + lvl * 0.15})`);
  g.addColorStop(1, `rgba(${c},0)`);
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, W, W);

  // Точки группируются по яркости (BUCKETS уровней): одна заливка на группу вместо одной на точку —
  // в разы меньше работы для браузера при том же виде.
  const rings = [[1.55, 1.15, 0.35, 1.0, c], [1.75, 0.5, -0.9, -0.7, "242,181,68"], [1.38, -0.7, 1.2, 1.5, c]];
  const drawRing = (rg, front) => {
    const [rr, tx, tz, sp, rc] = rg;
    const ctx2 = Math.cos(tx), stx = Math.sin(tx), ctz = Math.cos(tz), stz = Math.sin(tz);
    const off = now * sp * (0.4 + speed * 0.6);
    const groups = Array.from({ length: BUCKETS }, () => []);
    for (let k = 0; k < 140; k++) {
      const u = (k / 140) * Math.PI * 2;
      const x = Math.cos(u) * rr * R, z = Math.sin(u) * rr * R;
      const y2 = -z * stx, z2 = z * ctx2;
      const x3 = x * ctz - y2 * stz, y3 = x * stz + y2 * ctz;
      const q = view(x3, y3, z2);
      if ((q[2] < 0) !== front) continue;
      const depth = 0.5 - q[2] / (rr * R * 2);
      const head = ((u - off) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2);
      const trail = Math.max(0, 1 - head / 2.2);
      const al = Math.min(1, (0.12 + depth * 0.35) + trail * 0.7);
      const sz = (1 + trail * 2.2) * q[3] * dpr;
      groups[Math.min(BUCKETS - 1, (al * BUCKETS) | 0)].push(q[0] - sz / 2, q[1] - sz / 2, sz);
    }
    groups.forEach((gr, b) => {
      if (!gr.length) return;
      ctx.fillStyle = `rgba(${rc},${((b + 0.5) / BUCKETS).toFixed(3)})`;
      ctx.beginPath();
      for (let j = 0; j < gr.length; j += 3) ctx.rect(gr[j], gr[j + 1], gr[j + 2], gr[j + 2]);
      ctx.fill();
    });
  };
  rings.forEach((rg) => drawRing(rg, false));

  const groups = Array.from({ length: BUCKETS }, () => []);
  for (let i = 0; i < N; i++) {
    const [px, py, pz] = PTS[i];
    const wob = Math.sin(px * 4.2 + now * 3.1) * Math.cos(py * 5.1 - now * 2.4) + Math.sin(pz * 6 + now * 4) * 0.5;
    const d = R * (1 + lvl * 0.22 * wob + lvl * 0.08);
    const q = view(px * d, py * d, pz * d);
    const depth = 0.5 - q[2] / (2 * d);
    const al = 0.08 + depth * depth * 0.9;
    groups[Math.min(BUCKETS - 1, (al * BUCKETS) | 0)].push(q[0], q[1], (0.8 + depth * 2.2) * q[3] * dpr);
  }
  groups.forEach((gr, b) => {
    if (!gr.length) return;
    ctx.fillStyle = `rgba(${c},${((b + 0.5) / BUCKETS).toFixed(3)})`;
    ctx.beginPath();
    for (let j = 0; j < gr.length; j += 3) {
      ctx.moveTo(gr[j] + gr[j + 2], gr[j + 1]);
      ctx.arc(gr[j], gr[j + 1], gr[j + 2], 0, 6.2832);
    }
    ctx.fill();
  });

  const cr = R * (0.32 + lvl * 0.18);
  const g2 = ctx.createRadialGradient(cx - cr * 0.3, cy - cr * 0.35, 0, cx, cy, cr);
  g2.addColorStop(0, "rgba(255,255,255,0.95)");
  g2.addColorStop(0.35, `rgba(${c},0.85)`);
  g2.addColorStop(1, `rgba(${c},0)`);
  ctx.fillStyle = g2;
  ctx.beginPath();
  ctx.arc(cx, cy, cr, 0, 6.2832);
  ctx.fill();

  rings.forEach((rg) => drawRing(rg, true));
  ctx.globalCompositeOperation = "source-over";
}
requestAnimationFrame(draw);

// ════════════ демо для проверки дизайна без ядра: ?demo=chat | sleep | confirm | apps ════════════
function runDemo(kind) {
  online = true;
  handle({ type: "status", state: kind === "chat" ? "speaking" : "listening", mic: true, wake: true, barge: true,
           screen: true, awake: true, autonomy: "auto", model: "claude-haiku-5-5", tts: "edge", voice: true,
           sleeping: kind === "sleep" });
  handle({ type: "settings", log: true, hints: true, city: "Москва", memory: true,
           tiles: { apps: [{ name: "VS Code", hint: "редактор кода", target: "code" }, { name: "Chrome", hint: "браузер", target: "chrome" }],
                    games: [{ name: "Steam", hint: "библиотека игр", target: "steam" }], files: [] } });
  for (let i = 0; i < 40; i++) handle({ type: "sys", cpu: 15 + Math.random() * 20, ram: 60, gpu: 5, disk: Math.random() * 20, cpuT: null, gpuT: 48 });
  handle({ type: "weather", city: "Москва", temp: 0, feels: -3, wind: 2, humidity: 93, desc: "Небольшой снег", icon: "snow",
           days: [{ day: "Пн", max: 1, min: 0 }, { day: "Вт", max: 0, min: -1 }, { day: "Ср", max: -1, min: -4 }] });
  handle({ type: "msg", role: "user", text: "Джарвис, который час?" });
  handle({ type: "msg", role: "assistant", text: "Без четверти восемь, сэр." });
  handle({ type: "action", text: "узнать время", risk: "safe", status: "готово" });
  if (kind === "chat") handle({ type: "speaking_text", text: "Без четверти восемь, сэр." });
  if (kind === "confirm") handle({ type: "confirm", id: 1, risk: "dangerous", timeout: 40, text: "закрыть окно «Блокнот»" });
  if (kind === "apps") setTab("apps");
}
// ════════════ обои: прокрутка колесом ════════════
// Колесо над рабочим столом приходит событием: от ядра (обои в Wallpaper Engine) или от страницы
// обоев wallpaper.py. x, y — экранные пиксели; интерфейс на обоях занимает весь экран с (0, 0).
function wheelScroll(m) {
  if (!WALLPAPER) return;
  const dpr = window.devicePixelRatio || 1;
  let el = document.elementFromPoint(m.x / dpr, m.y / dpr);
  while (el && el !== document.body) {
    const oy = getComputedStyle(el).overflowY;
    if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 1) break;
    el = el.parentElement;
  }
  if (el && el !== document.body) el.scrollBy({ top: -m.delta / 120 * 110, behavior: "smooth" });
}
if (WALLPAPER) {
  window.addEventListener("message", (e) => {
    if (e.origin === "http://127.0.0.1:8768" && e.data && e.data.type === "wheel") wheelScroll(e.data);
  });
}

const DEMO = Q.get("demo");
if (DEMO) setTimeout(() => runDemo(DEMO), 50); else connect();
renderSwitches();
applyMode();
