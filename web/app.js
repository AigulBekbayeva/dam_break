/* Визуализация волны прорыва плотины.
   Кадры лежат в PNG-атласах (0 = сухо, 1..255 = глубина). Приложение
   распаковывает их в типизированные массивы, интерполирует между
   соседними кадрами и рисует результат в canvas, который MapLibre
   проецирует на карту как canvas-источник. */

(() => {
  "use strict";

  const BASEMAP = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";
  const FRAMES_PER_SEC = 6; // скорость воспроизведения в кадрах данных

  /* Шкала глубины. Позиция нелинейна: на мелководье шаг мельче, потому что
     именно там разница между 0.3 и 1.5 м решает, проходима ли улица. */
  const STOPS = [
    [0.1,   0, [182, 242, 228]],
    [0.5,  12, [111, 220, 216]],
    [1.5,  30, [ 52, 174, 220]],
    [3.0,  50, [ 36, 119, 201]],
    [6.0,  71, [ 42,  70, 168]],
    [10.0, 88, [ 59,  35, 128]],
    [14.0, 100, [78,  18,  87]],
  ];
  const TICKS = [0.1, 1, 3, 6, 10, 14];

  const $ = (id) => document.getElementById(id);
  const el = {
    status: $("status"),
    list: $("scenario-list"),
    play: $("play"),
    playIcon: $("play-icon"),
    hydro: $("hydro"),
    rTime: $("r-time"),
    rArea: $("r-area"),
    rDepth: $("r-depth"),
    caret: $("gauge-caret"),
    gaugeValue: $("gauge-value"),
    ticks: $("gauge-ticks"),
    canvas: $("flood-canvas"),
  };

  const state = {
    manifest: null,
    sc: null,          // текущий сценарий (объект манифеста)
    frames: [],        // Uint8Array на кадр
    maxFrame: null,    // Uint8Array огибающей
    lut: null,         // Uint32Array 256 → RGBA
    t: 0,              // дробный индекс кадра
    playing: false,
    mode: "wave",
    ctx: null,
    imageData: null,
    px: null,          // Uint32Array поверх imageData
    merc: null,        // границы кадра в нормализованных координатах Меркатора
    hoverLngLat: null,
    hoverDepth: null,
    hydroDpr: 1,
    lastTick: 0,
    started: false,
  };

  let map = null;
  let pauseTimer = 0;

  /* ────────────────────────────────────────────────────── цвет и шкала */

  const lerp = (a, b, f) => a + (b - a) * f;

  function pctForDepth(d) {
    if (d <= STOPS[0][0]) return 0;
    for (let i = 1; i < STOPS.length; i++) {
      if (d <= STOPS[i][0]) {
        const f = (d - STOPS[i - 1][0]) / (STOPS[i][0] - STOPS[i - 1][0]);
        return lerp(STOPS[i - 1][1], STOPS[i][1], f);
      }
    }
    return 100;
  }

  function colorAtPct(p) {
    for (let i = 1; i < STOPS.length; i++) {
      if (p <= STOPS[i][1]) {
        const f = (p - STOPS[i - 1][1]) / (STOPS[i][1] - STOPS[i - 1][1]);
        const a = STOPS[i - 1][2], b = STOPS[i][2];
        return [lerp(a[0], b[0], f), lerp(a[1], b[1], f), lerp(a[2], b[2], f)];
      }
    }
    return STOPS[STOPS.length - 1][2];
  }

  /* Порядок байтов в ImageData — платформенный, поэтому определяем его,
     а не полагаемся на little-endian. */
  const LITTLE_ENDIAN = (() => {
    const buf = new ArrayBuffer(4);
    new Uint32Array(buf)[0] = 0x0a0b0c0d;
    return new Uint8Array(buf)[0] === 0x0d;
  })();

  function buildLUT(depthScale) {
    const lut = new Uint32Array(256);
    for (let v = 1; v < 256; v++) {
      const d = v * depthScale;
      const [r, g, b] = colorAtPct(pctForDepth(d));
      const a = Math.round(255 * (0.58 + 0.34 * Math.min(1, d / 2)) * Math.min(1, d / 0.3));
      lut[v] = LITTLE_ENDIAN
        ? (a << 24) | (b << 16) | (g << 8) | r
        : (r << 24) | (g << 16) | (b << 8) | a;
    }
    lut[0] = 0;
    return lut;
  }

  function paintTicks() {
    el.ticks.innerHTML = "";
    for (const d of TICKS) {
      const li = document.createElement("li");
      li.textContent = d < 1 ? d.toFixed(1) : String(d);
      li.style.bottom = pctForDepth(d) + "%";
      el.ticks.appendChild(li);
    }
  }

  /* ───────────────────────────────────────────────────── загрузка данных */

  const loadImage = (src) =>
    new Promise((res, rej) => {
      const img = new Image();
      img.onload = () => res(img);
      img.onerror = () => rej(new Error("Не удалось загрузить " + src));
      img.src = src;
    });

  /* Вырезает из атласа канал яркости в отдельный массив на кадр. */
  function unpackSheet(img, sc, startIndex, out) {
    const c = document.createElement("canvas");
    c.width = img.width;
    c.height = img.height;
    const ctx = c.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    const data = ctx.getImageData(0, 0, img.width, img.height).data;

    const { cols, perSheet } = sc.atlas;
    const { width: W, height: H } = sc;
    const count = Math.min(perSheet, sc.frames - startIndex);

    for (let j = 0; j < count; j++) {
      const cellRow = Math.floor(j / cols);
      const cellCol = j % cols;
      const buf = new Uint8Array(W * H);
      for (let y = 0; y < H; y++) {
        let s = ((cellRow * H + y) * img.width + cellCol * W) * 4;
        const d = y * W;
        for (let x = 0; x < W; x++, s += 4) buf[d + x] = data[s];
      }
      out[startIndex + j] = buf;
    }
  }

  async function loadScenario(sc) {
    state.sc = sc;
    state.playing = false;
    state.t = 0;
    state.frames = new Array(sc.frames);
    state.lut = buildLUT(sc.depthScale);
    setPlayIcon();

    const total = sc.atlas.sheets.length + 1;
    let done = 0;
    const step = () => setStatus(`Загрузка кадров… ${Math.round((++done / total) * 100)} %`);

    let index = 0;
    for (const name of sc.atlas.sheets) {
      const img = await loadImage("data/" + name);
      unpackSheet(img, sc, index, state.frames);
      index += sc.atlas.perSheet;
      step();
    }

    const maxImg = await loadImage("data/" + sc.maxdepth);
    const holder = new Array(1);
    unpackSheet(maxImg, { ...sc, frames: 1, atlas: { cols: 1, perSheet: 1 } }, 0, holder);
    state.maxFrame = holder[0];
    step();

    el.hydro.setAttribute("aria-valuemax", String(sc.frames - 1));
    prepareCanvas(sc);
    attachLayer(sc, state.started);
    state.started = true;
    resizeHydro();
    renderCurrent();
    setStatus(null);
  }

  /* ──────────────────────────────────────────────────────────── рендер */

  function prepareCanvas(sc) {
    el.canvas.width = sc.width;
    el.canvas.height = sc.height;
    state.ctx = el.canvas.getContext("2d");
    state.imageData = state.ctx.createImageData(sc.width, sc.height);
    state.px = new Uint32Array(state.imageData.data.buffer);

    const R = 20037508.342789244;
    const [minx, miny, maxx, maxy] = sc.bounds3857;
    const norm = (v) => (v + R) / (2 * R);
    state.merc = {
      x0: norm(minx), x1: norm(maxx),
      y0: 1 - norm(maxy), y1: 1 - norm(miny), // y растёт вниз
    };
  }

  function drawFrame(src) {
    const px = state.px, lut = state.lut;
    for (let k = 0; k < px.length; k++) px[k] = lut[src[k]];
    state.ctx.putImageData(state.imageData, 0, 0);
  }

  function drawInterpolated(t) {
    const n = state.frames.length;
    const i0 = Math.max(0, Math.min(n - 1, Math.floor(t)));
    const i1 = Math.min(n - 1, i0 + 1);
    const f = t - i0;
    const a = state.frames[i0], b = state.frames[i1];
    if (!a || !b) return;

    if (i0 === i1 || f === 0) return drawFrame(a);

    const px = state.px, lut = state.lut;
    for (let k = 0; k < px.length; k++) {
      const va = a[k];
      px[k] = lut[(va + (b[k] - va) * f + 0.5) | 0];
    }
    state.ctx.putImageData(state.imageData, 0, 0);
  }

  function renderCurrent() {
    if (!state.sc) return;
    if (state.mode === "max") drawFrame(state.maxFrame);
    else drawInterpolated(state.t);
    markDirty();
    drawHydro();
    updateReadout();
    updateHover();
  }

  /* MapLibre перезаливает текстуру только пока источник «играет». Держать
     его включённым постоянно — лишний трафик к GPU, поэтому после ручной
     перерисовки даём ему короткое окно и снова останавливаем. */
  function markDirty() {
    const src = map && map.getSource("flood");
    if (!src) return;
    src.play();
    clearTimeout(pauseTimer);
    if (!state.playing) pauseTimer = setTimeout(() => src.pause(), 150);
  }

  /* ────────────────────────────────────────────────────────────── карта */

  function attachLayer(sc, animate = true) {
    if (map.getLayer("flood")) map.removeLayer("flood");
    if (map.getSource("flood")) map.removeSource("flood");

    map.addSource("flood", {
      type: "canvas",
      canvas: "flood-canvas",
      coordinates: sc.corners,
      animate: false,
    });
    map.addLayer({
      id: "flood",
      type: "raster",
      source: "flood",
      paint: { "raster-opacity": 1, "raster-fade-duration": 0 },
    });

    const [tl, , br] = sc.corners;
    map.fitBounds([[tl[0], br[1]], [br[0], tl[1]]], {
      padding: fitPadding(),
      duration: animate ? 600 : 0,
    });
  }

  function fitPadding() {
    const narrow = window.innerWidth <= 720;
    return narrow
      ? { top: 90, bottom: 40, left: 20, right: 100 }
      : { top: 60, bottom: 60, left: 310, right: 170 };
  }

  function depthAt(lngLat) {
    const sc = state.sc;
    if (!sc || !state.merc) return null;
    const mc = maplibregl.MercatorCoordinate.fromLngLat(lngLat);
    const u = (mc.x - state.merc.x0) / (state.merc.x1 - state.merc.x0);
    const v = (mc.y - state.merc.y0) / (state.merc.y1 - state.merc.y0);
    if (u < 0 || u >= 1 || v < 0 || v >= 1) return null;

    const k = ((v * sc.height) | 0) * sc.width + ((u * sc.width) | 0);
    let q;
    if (state.mode === "max") {
      q = state.maxFrame[k];
    } else {
      const n = state.frames.length;
      const i0 = Math.max(0, Math.min(n - 1, Math.floor(state.t)));
      const i1 = Math.min(n - 1, i0 + 1);
      const f = state.t - i0;
      const va = state.frames[i0][k];
      q = va + (state.frames[i1][k] - va) * f;
    }
    return q > 0 ? q * sc.depthScale : 0;
  }

  function updateHover() {
    // Пересчитываем на каждом кадре: под неподвижным курсором вода прибывает.
    state.hoverDepth = state.hoverLngLat ? depthAt(state.hoverLngLat) : null;
    const d = state.hoverDepth;
    if (d === null || d === undefined) {
      el.caret.hidden = true;
      el.gaugeValue.textContent = "—";
      el.gaugeValue.classList.add("is-dry");
      return;
    }
    if (d <= 0) {
      el.caret.hidden = true;
      el.gaugeValue.textContent = "сухо";
      el.gaugeValue.classList.add("is-dry");
      return;
    }
    el.caret.hidden = false;
    el.caret.style.bottom = pctForDepth(d) + "%";
    el.gaugeValue.textContent = d.toFixed(2);
    el.gaugeValue.classList.remove("is-dry");
  }

  /* ────────────────────────────────────────────────────────── гидрограф */

  function resizeHydro() {
    const c = el.hydro;
    const r = c.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    c.width = Math.max(1, Math.round(r.width * dpr));
    c.height = Math.max(1, Math.round(r.height * dpr));
    state.hydroDpr = dpr;
    drawHydro();
  }

  function fmtTime(hours) {
    const h = Math.floor(hours);
    const m = Math.round((hours - h) * 60);
    return `T+${String(h).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  }

  function drawHydro() {
    const sc = state.sc;
    const c = el.hydro;
    const g = c.getContext("2d");
    const W = c.width, H = c.height;
    g.clearRect(0, 0, W, H);
    if (!sc) return;

    const dpr = state.hydroDpr;
    const area = sc.series.areaKm2;
    const n = area.length;
    const peak = Math.max(...area) || 1;
    const padB = 14 * dpr;
    const plotH = H - padB - 2 * dpr;

    const xAt = (i) => (n < 2 ? 0 : (i / (n - 1)) * (W - 1));
    const yAt = (v) => 2 * dpr + plotH * (1 - v / (peak * 1.06));

    // Ось и отметки времени
    g.strokeStyle = "#1c4654";
    g.lineWidth = 1 * dpr;
    g.beginPath();
    g.moveTo(0, H - padB + 0.5);
    g.lineTo(W, H - padB + 0.5);
    g.stroke();

    g.fillStyle = "#6e93a0";
    g.font = `${10 * dpr}px "IBM Plex Mono", monospace`;
    g.textBaseline = "top";
    const totalH = sc.elapsedHours[n - 1] || 1;
    const stepH = totalH > 6 ? 2 : totalH > 2 ? 1 : 0.5;
    for (let t = 0; t <= totalH + 1e-6; t += stepH) {
      const x = (t / totalH) * (W - 1);
      g.textAlign = t === 0 ? "left" : x > W - 30 * dpr ? "right" : "center";
      g.fillText(fmtTime(t), Math.min(W - 1, Math.max(0, x)), H - padB + 3 * dpr);
      g.beginPath();
      g.moveTo(x, H - padB);
      g.lineTo(x, H - padB - 3 * dpr);
      g.stroke();
    }

    const curve = (from, to) => {
      g.beginPath();
      g.moveTo(xAt(from), yAt(area[from]));
      for (let i = from + 1; i <= to; i++) g.lineTo(xAt(i), yAt(area[i]));
    };

    // Весь гидрограф — тонким контуром
    curve(0, n - 1);
    g.strokeStyle = "#2b5b6c";
    g.lineWidth = 1.25 * dpr;
    g.stroke();

    // Прошедшая часть — заливкой
    const tNow = state.mode === "max" ? n - 1 : state.t;
    const iNow = Math.max(0, Math.min(n - 1, Math.floor(tNow)));
    const fNow = tNow - iNow;
    const xNow = xAt(iNow) + (iNow < n - 1 ? fNow * (xAt(iNow + 1) - xAt(iNow)) : 0);
    const aNow = area[iNow] + (iNow < n - 1 ? (area[iNow + 1] - area[iNow]) * fNow : 0);

    const grad = g.createLinearGradient(0, 2 * dpr, 0, H - padB);
    grad.addColorStop(0, "rgba(52,174,220,0.42)");
    grad.addColorStop(1, "rgba(36,119,201,0.06)");
    curve(0, iNow);
    g.lineTo(xNow, yAt(aNow));
    g.lineTo(xNow, H - padB);
    g.lineTo(0, H - padB);
    g.closePath();
    g.fillStyle = grad;
    g.fill();

    curve(0, iNow);
    g.lineTo(xNow, yAt(aNow));
    g.strokeStyle = "#5fd0e0";
    g.lineWidth = 1.5 * dpr;
    g.stroke();

    if (state.mode === "max") return;

    // Отсчёт времени — единственное пурпурное на всей полосе
    g.strokeStyle = "#ff2e88";
    g.lineWidth = 1 * dpr;
    g.beginPath();
    g.moveTo(xNow + 0.5, 0);
    g.lineTo(xNow + 0.5, H - padB);
    g.stroke();

    g.fillStyle = "#ff2e88";
    g.beginPath();
    g.arc(xNow, yAt(aNow), 3 * dpr, 0, Math.PI * 2);
    g.fill();
  }

  function updateReadout() {
    const sc = state.sc;
    if (!sc) return;
    const n = sc.frames;

    if (state.mode === "max") {
      el.rTime.textContent = "весь период";
      el.rArea.textContent = Math.max(...sc.series.areaKm2).toFixed(2);
      el.rDepth.textContent = sc.maxDepth.toFixed(1);
      el.hydro.setAttribute("aria-valuenow", String(n - 1));
      return;
    }

    const i0 = Math.max(0, Math.min(n - 1, Math.floor(state.t)));
    const i1 = Math.min(n - 1, i0 + 1);
    const f = state.t - i0;
    const at = (arr) => arr[i0] + (arr[i1] - arr[i0]) * f;

    el.rTime.textContent = fmtTime(at(sc.elapsedHours));
    el.rArea.textContent = at(sc.series.areaKm2).toFixed(2);
    el.rDepth.textContent = at(sc.series.peakDepth).toFixed(1);
    el.hydro.setAttribute("aria-valuenow", state.t.toFixed(1));
    el.hydro.setAttribute("aria-valuetext",
      `${fmtTime(at(sc.elapsedHours))}, ${at(sc.series.areaKm2).toFixed(2)} км²`);
  }

  /* ────────────────────────────────────────────────────── воспроизведение */

  function setPlayIcon() {
    el.play.classList.toggle("is-playing", state.playing);
    el.play.setAttribute("aria-label", state.playing ? "Пауза" : "Запустить воспроизведение");
    el.playIcon.setAttribute("d", state.playing ? "M4 2.5h3.2v11H4zM8.8 2.5H12v11H8.8z" : "M4 2.5 13 8l-9 5.5z");
  }

  function togglePlay(force) {
    if (state.mode === "max") return;
    state.playing = force === undefined ? !state.playing : force;
    setPlayIcon();
    if (state.playing) {
      if (state.t >= state.frames.length - 1) state.t = 0;
      state.lastTick = performance.now();
      const src = map.getSource("flood");
      if (src) src.play();
      requestAnimationFrame(tick);
    }
  }

  function tick(now) {
    if (!state.playing) return;
    const dt = Math.min(0.25, (now - state.lastTick) / 1000);
    state.lastTick = now;
    state.t += dt * FRAMES_PER_SEC;
    if (state.t >= state.frames.length - 1) state.t = 0; // зациклить
    renderCurrent();
    requestAnimationFrame(tick);
  }

  function scrubTo(clientX) {
    const r = el.hydro.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    state.t = f * (state.frames.length - 1);
    renderCurrent();
  }

  /* ───────────────────────────────────────────────────────── интерфейс */

  function buildScenarioList() {
    el.list.innerHTML = "";
    state.manifest.scenarios.forEach((sc, i) => {
      const b = document.createElement("button");
      b.className = "scenario" + (i === 0 ? " is-on" : "");
      b.type = "button";
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(i === 0));
      b.innerHTML =
        `<b></b><span>макс. ${sc.maxDepth.toFixed(1)} м · ` +
        `${Math.max(...sc.series.areaKm2).toFixed(2)} км²</span>`;
      b.querySelector("b").textContent = sc.label;
      b.addEventListener("click", () => {
        if (state.sc === sc) return;
        [...el.list.children].forEach((n) => {
          n.classList.remove("is-on");
          n.setAttribute("aria-checked", "false");
        });
        b.classList.add("is-on");
        b.setAttribute("aria-checked", "true");
        setStatus("Загрузка кадров…");
        loadScenario(sc).catch(fail);
      });
      el.list.appendChild(b);
    });
  }

  function setMode(mode) {
    state.mode = mode;
    if (mode === "max") togglePlay(false);
    document.querySelectorAll(".mode").forEach((b) => {
      const on = b.dataset.mode === mode;
      b.classList.toggle("is-on", on);
      b.setAttribute("aria-checked", String(on));
    });
    el.play.disabled = mode === "max";
    el.play.style.opacity = mode === "max" ? "0.35" : "";
    renderCurrent();
  }

  function setStatus(msg) {
    el.status.hidden = !msg;
    if (msg) el.status.textContent = msg;
  }

  function fail(err) {
    console.error(err);
    setStatus(err.message || "Не удалось загрузить данные");
  }

  function bindEvents() {
    el.play.addEventListener("click", () => togglePlay());

    document.querySelectorAll(".mode").forEach((b) =>
      b.addEventListener("click", () => setMode(b.dataset.mode))
    );

    let dragging = false;
    el.hydro.addEventListener("pointerdown", (e) => {
      if (state.mode === "max") return;
      dragging = true;
      el.hydro.setPointerCapture(e.pointerId);
      togglePlay(false);
      scrubTo(e.clientX);
    });
    el.hydro.addEventListener("pointermove", (e) => dragging && scrubTo(e.clientX));
    el.hydro.addEventListener("pointerup", (e) => {
      dragging = false;
      el.hydro.releasePointerCapture(e.pointerId);
    });

    el.hydro.addEventListener("keydown", (e) => {
      if (state.mode === "max") return;
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      e.preventDefault();
      togglePlay(false);
      state.t = Math.max(0, Math.min(state.frames.length - 1, Math.round(state.t) + d));
      renderCurrent();
    });

    document.addEventListener("keydown", (e) => {
      if (e.code === "Space" && e.target.tagName !== "BUTTON") {
        e.preventDefault();
        togglePlay();
      }
    });

    let resizeTimer = 0;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(resizeHydro, 120);
    });
  }

  /* ─────────────────────────────────────────────────────────────── старт */

  async function init() {
    paintTicks();
    bindEvents();

    let manifest;
    try {
      const res = await fetch("data/manifest.json");
      if (!res.ok) throw new Error("manifest.json не найден — запустите scripts/prepare.py");
      manifest = await res.json();
    } catch (err) {
      return fail(err);
    }
    state.manifest = manifest;
    if (!manifest.scenarios || !manifest.scenarios.length) {
      return fail(new Error("В манифесте нет сценариев"));
    }

    const first = manifest.scenarios[0];
    const [tl, , br] = first.corners;

    map = new maplibregl.Map({
      container: "map",
      style: BASEMAP,
      bounds: [[tl[0], br[1]], [br[0], tl[1]]],
      fitBoundsOptions: { padding: fitPadding() },
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: "metric" }), "bottom-right");

    map.on("mousemove", (e) => {
      state.hoverLngLat = e.lngLat;
      updateHover();
    });
    map.on("mouseout", () => {
      state.hoverLngLat = null;
      updateHover();
    });

    buildScenarioList();

    map.on("load", () => {
      loadScenario(first)
        .then(() => {
          if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) togglePlay(true);
        })
        .catch(fail);
    });
    map.on("error", (e) => console.warn("map:", e && e.error));
  }

  init();
})();
