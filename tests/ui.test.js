/* Прогон app.js в jsdom на настоящем манифесте.
   Подменяются только вещи, которых в jsdom нет: MapLibre и распаковка
   атласов (она требует WebGL/canvas). Вся логика выбора — настоящая. */
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const ROOT = path.join(__dirname, "..", "web");
const manifest = JSON.parse(fs.readFileSync(path.join(ROOT, "data/manifest.json"), "utf8"));

let src = fs.readFileSync(path.join(ROOT, "app.js"), "utf8");

// Заглушка загрузки кадров: сохраняем аргументы, эмулируем готовность.
src = src.replace(
  /async function loadScenario\(sc, \{ refit = true, at = null \} = \{\}\) \{[\s\S]*?\n  \}\n/,
  `async function loadScenario(sc, { refit = true, at = null } = {}) {
    state.sc = sc;
    state.frames = new Array(sc.frames).fill(null);
    if (at !== null) state.t = at * (state.frames.length - 1);
    window.__calls.push({ id: sc.id, refit, at, t: state.t });
    return Promise.resolve();
  }\n`
);

const html = fs.readFileSync(path.join(ROOT, "index.html"), "utf8")
  .replace(/<script[\s\S]*?<\/script>/g, "");

const dom = new JSDOM(html, { runScripts: "outside-only", pretendToBeVisual: true });
const w = dom.window;

w.__calls = [];
const handlers = {};
w.maplibregl = {
  Map: class {
    constructor() {
      this.on = (e, f) => { handlers[e] = f; };
      this.addControl = () => {};
      this.getSource = () => null;      // слой ещё не подключён
      this.getLayer = () => null;
      this.addSource = () => {}; this.addLayer = () => {};
      this.removeSource = () => {}; this.removeLayer = () => {};
      this.fitBounds = () => {};
    }
  },
  NavigationControl: class {}, ScaleControl: class {},
  MercatorCoordinate: { fromLngLat: () => ({ x: 0, y: 0 }) },
};
w.fetch = async () => ({ ok: true, json: async () => manifest });
w.matchMedia = () => ({ matches: true });   // reduced-motion: без автозапуска
w.requestAnimationFrame = () => 0;
w.HTMLCanvasElement.prototype.getContext = () => ({
  clearRect(){}, beginPath(){}, moveTo(){}, lineTo(){}, stroke(){}, fill(){},
  arc(){}, closePath(){}, fillText(){}, createLinearGradient: () => ({ addColorStop(){} }),
  createImageData: (a, b) => ({ data: new Uint8ClampedArray(a * b * 4) }),
  putImageData(){}, drawImage(){}, getImageData: (x, y, a, b) => ({ data: new Uint8ClampedArray(a * b * 4) }),
});

w.eval(src);

const $ = (s) => w.document.querySelector(s);
const rows = (s) => [...w.document.querySelectorAll(s)];
const label = (n) => n.querySelector("b").textContent;
const note = (n) => n.querySelector("span").textContent;
const chosen = (s) => rows(s).filter((n) => n.classList.contains("is-on")).map(label);

let fails = 0;
const check = (name, cond, extra = "") => {
  console.log(`  ${cond ? "✓" : "✗"} ${name}${extra ? "  — " + extra : ""}`);
  if (!cond) fails++;
};

setTimeout(() => {
  handlers.load();                       // эмулируем событие загрузки карты
  setTimeout(() => {
    console.log("\n── стартовое состояние ──");
    const groups = rows("#group-list .group");
    check("блок водохранилищ показан", !$("#group-block").hasAttribute("hidden"));
    check("две группы", groups.length === 2, groups.map(label).join(" / "));
    check("подписи из group.txt", label(groups[0]) === "Верхне-Тобольское");
    check("сводка по группе", /2 сцен/.test(note(groups[0])), note(groups[0]));
    check("сценариев первой группы: 2", rows("#scenario-list .scenario").length === 2);
    check("выбрана первая группа", chosen("#group-list .group")[0] === "Верхне-Тобольское");
    check("выбран первый сценарий", chosen("#scenario-list .scenario")[0] === "Максимальный паводок");

    console.log("\n── переключение сценария внутри группы ──");
    // Сдвигаем шкалу времени, иначе «удержание позиции» неотличимо от сброса
    for (let i = 0; i < 3; i++) {
      const e = new w.KeyboardEvent("keydown", { key: "ArrowRight", cancelable: true });
      $("#hydro").dispatchEvent(e);
    }
    w.__calls = [];
    rows("#scenario-list .scenario")[1].dispatchEvent(new w.Event("click"));
    setTimeout(() => {
      const c = w.__calls[0];
      check("загружен второй сценарий", c && c.id.includes("vt-proryv"), c && c.id);
      check("камера НЕ пересобирается", c && c.refit === false);
      // 3 кадра из 36 → доля 3/35, перенесённая в 60-кадровый сценарий
      const want = 3 / 35;
      check("позиция на шкале удержана, а не сброшена",
        c && Math.abs(c.at - want) < 1e-9 && c.at > 0,
        c && `at=${c.at.toFixed(4)} (ожидалось ${want.toFixed(4)}), t=${c.t.toFixed(2)} из 59`);
      check("отметка переехала", chosen("#scenario-list .scenario")[0] === "Прорыв плотины");

      console.log("\n── переключение водохранилища ──");
      w.__calls = [];
      rows("#group-list .group")[1].dispatchEvent(new w.Event("click"));
      setTimeout(() => {
        const c = w.__calls[0];
        check("загружен сценарий второй группы", c && c.id.includes("kt-"), c && c.id);
        check("камера пересобирается", c && c.refit === true);
        check("шкала сброшена в начало", c && c.at === null);
        check("список сценариев перестроен",
          rows("#scenario-list .scenario").map(label).join("/") === "Максимальный паводок/Прорыв плотины");
        check("выбрана вторая группа", chosen("#group-list .group")[0] === "Каратомарское");
        check("ровно одна группа отмечена", chosen("#group-list .group").length === 1);

        console.log("\n── шкала глубин ──");
        const ticks = rows("#gauge-ticks li").map((n) => n.textContent);
        check("подписи масштабированы под rampMax=16", ticks.includes("16"), ticks.join(", "));

        console.log(fails ? `\n${fails} проверок не прошло` : "\nвсе проверки пройдены");
        process.exit(fails ? 1 : 0);
      }, 20);
    }, 20);
  }, 20);
}, 20);
