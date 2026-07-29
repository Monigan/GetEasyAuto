import { createGarageController } from "./garage.js";
import { createNotificationCenter } from "./notifications.js";
import { formatMileage, formatMoney, money, setText } from "./ui.js";

const state = {
  page: 1,
  pages: 0,
  listingsSignature: "",
  statsSignature: "",
  marketSignature: "",
  refreshRunning: false,
  refreshPending: false,
  activityRunning: false,
  view: "catalog",
};
const ids = [
  "search", "min-price", "max-price", "min-mileage",
  "max-mileage", "brand", "location", "status", "visibility", "sort"
];
const elements = Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));
const multiFilterElements = [...document.querySelectorAll(".multi-filter")];
const PREFERENCES_KEY = "autoscope.filters.v1";
let refreshActivitySignature = "";
let refreshActivityTimer;

function loadPreferences() {
  try {
    return JSON.parse(localStorage.getItem(PREFERENCES_KEY) || "null");
  } catch {
    return null;
  }
}

const savedPreferences = loadPreferences();
const multiFilterState = new Map(
  multiFilterElements.map((element) => [
    element.dataset.filter,
    new Set(savedPreferences?.multi?.[element.dataset.filter] || []),
  ]),
);
const garageController = createGarageController();
const notificationCenter = createNotificationCenter();

function savePreferences() {
  const controls = Object.fromEntries(
    Object.entries(elements).map(([id, element]) => [id, element.value]),
  );
  const multi = Object.fromEntries(
    [...multiFilterState].map(([name, values]) => [name, [...values]]),
  );
  try {
    localStorage.setItem(
      PREFERENCES_KEY,
      JSON.stringify({ controls, multi, view: state.view }),
    );
  } catch {
    // The app remains functional when browser storage is unavailable.
  }
}

function restoreControlPreferences() {
  for (const [id, value] of Object.entries(savedPreferences?.controls || {})) {
    const element = elements[id];
    if (!element || typeof value !== "string") continue;
    if (
      element.tagName === "SELECT"
      && ![...element.options].some((option) => option.value === value)
    ) {
      continue;
    }
    element.value = value;
  }
}

function params(includePage = true) {
  const query = new URLSearchParams();
  const mapping = {
    search: "q",
    "min-price": "min_price",
    "max-price": "max_price",
    "min-mileage": "min_mileage",
    "max-mileage": "max_mileage",
    brand: "brand",
    location: "location",
    status: "status",
    visibility: "visibility",
    sort: "sort",
  };
  for (const [id, name] of Object.entries(mapping)) {
    const value = elements[id].value.trim();
    if (value) query.set(name, value);
  }
  for (const [name, selected] of multiFilterState) {
    for (const value of selected) query.append(name, value);
  }
  if (includePage) {
    query.set("page", state.page);
    query.set("page_size", "24");
  }
  return query;
}

function formatDate(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" })
    .format(new Date(value));
}

function displayPublished(value) {
  if (!value) return "Не указано";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : new Intl.DateTimeFormat(
    "ru-RU", { dateStyle: "medium", timeStyle: "short" }
  ).format(parsed);
}

function updateMultiFilterSummary(element) {
  const selected = multiFilterState.get(element.dataset.filter);
  const summary = element.querySelector("summary strong");
  const count = selected.size;
  element.classList.toggle("has-selection", count > 0);
  summary.textContent = count ? `Выбрано: ${count}` : "Все";
  const total = [...multiFilterState.values()]
    .reduce((sum, values) => sum + values.size, 0);
  setText(
    "advanced-filter-count",
    total ? `Выбрано значений: ${total}` : "Не выбраны",
  );
}

function setupMultiFilters(attributeOptions = {}) {
  for (const element of multiFilterElements) {
    const name = element.dataset.filter;
    const root = element.querySelector(".multi-options");
    root.replaceChildren();
    const values = attributeOptions[name] || [];
    const selected = multiFilterState.get(name);
    const available = new Set(values);
    for (const value of selected) {
      if (!available.has(value)) selected.delete(value);
    }
    if (!values.length) {
      const empty = document.createElement("div");
      empty.className = "multi-empty";
      empty.textContent = "Нет данных";
      root.append(empty);
      continue;
    }
    const fragment = document.createDocumentFragment();
    for (const value of values) {
      const label = document.createElement("label");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.value = value;
      checkbox.checked = selected.has(value);
      const text = document.createElement("span");
      text.textContent = value;
      checkbox.addEventListener("change", () => {
        const selected = multiFilterState.get(name);
        if (checkbox.checked) selected.add(value);
        else selected.delete(value);
        updateMultiFilterSummary(element);
        scheduleRefresh();
      });
      label.append(checkbox, text);
      fragment.append(label);
    }
    root.append(fragment);
    updateMultiFilterSummary(element);
    element.addEventListener("toggle", () => {
      if (!element.open) return;
      for (const other of multiFilterElements) {
        if (other !== element) other.open = false;
      }
    });
  }
}

function renderCards(payload) {
  const container = document.getElementById("cards");
  const template = document.getElementById("card-template");
  container.replaceChildren();
  document.getElementById("empty").hidden = payload.items.length > 0;
  setText("result-count", `${money.format(payload.total)} шт.`);
  for (const item of payload.items) {
    const card = template.content.cloneNode(true);
    const image = card.querySelector(".car-image");
    const imageWrap = card.querySelector(".car-image-wrap");
    const dataLoader = card.querySelector(".card-data-loader");
    const dataLoaderText = card.querySelector(".card-data-loader-text");
    const pendingData = new Set();
    const activeUpdateStage = item.update_stage || "";
    const activeUpdateState = item.update_state || "";

    function renderCardLoader() {
      dataLoader.hidden = !activeUpdateStage && pendingData.size === 0;
      dataLoader.classList.toggle(
        "is-active-update",
        activeUpdateState === "running",
      );
      dataLoader.classList.toggle("is-success", activeUpdateState === "success");
      dataLoader.classList.toggle("is-error", activeUpdateState === "error");
      dataLoaderText.textContent = activeUpdateStage || (
        pendingData.size
          ? `Ожидаются ${[...pendingData].join(" и ")}`
          : ""
      );
    }

    if (!item.description) pendingData.add("описание");
    if (item.thumbnail_url) {
      pendingData.add("фото");
      image.alt = item.title;
      imageWrap.classList.add("is-loading");
      image.addEventListener("load", () => {
        imageWrap.classList.remove("is-loading");
        imageWrap.classList.add("is-ready");
        pendingData.delete("фото");
        renderCardLoader();
      }, { once: true });
      image.addEventListener("error", () => {
        image.removeAttribute("src");
        imageWrap.classList.remove("is-loading", "is-ready");
        pendingData.delete("фото");
        renderCardLoader();
      }, { once: true });
      image.src = item.thumbnail_url;
    } else if (item.image_pending) {
      pendingData.add("фото");
      imageWrap.classList.add("is-loading");
    }
    renderCardLoader();
    card.querySelector(".source-badge").textContent = item.source;
    card.querySelector(".car-price").textContent = formatMoney(item.price);
    card.querySelector(".car-title").textContent = item.title;
    card.querySelector(".car-mileage").textContent = formatMileage(item.mileage_km);
    card.querySelector(".car-location").textContent = item.location || "Город не указан";
    card.querySelector(".car-published").textContent =
      item.published_at ? `Опубликовано: ${displayPublished(item.published_at)}` : "Дата не указана";
    card.querySelector(".car-description").textContent =
      item.description || "Описание пока не загружено.";
    card.querySelector(".car-seen").textContent = `Обновлено ${formatDate(item.last_seen_at)}`;
    const link = card.querySelector(".car-link");
    link.href = item.url;
    link.addEventListener("click", (event) => event.stopPropagation());
    const garageButton = card.querySelector(".car-garage");
    garageButton.textContent = item.garage_id ? "В гараже" : "+ В гараж";
    garageButton.disabled = Boolean(item.garage_id);
    garageButton.addEventListener("click", async (event) => {
      event.stopPropagation();
      if (item.garage_id) return;
      garageButton.disabled = true;
      garageButton.textContent = "Добавляем…";
      try {
        const response = await fetch("/api/garage", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            listing_source: item.source,
            listing_external_id: item.external_id,
          }),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Не удалось добавить автомобиль в гараж");
        item.garage_id = result.id;
        garageButton.textContent = "В гараже";
        garageButton.disabled = true;
        state.listingsSignature = "";
      } catch (error) {
        garageButton.textContent = "+ В гараж";
        garageButton.disabled = false;
        alert(error.message);
      }
    });
    const hideButton = card.querySelector(".car-hide");
    hideButton.textContent = item.hidden ? "Вернуть" : "Скрыть";
    hideButton.setAttribute(
      "aria-label",
      item.hidden
        ? `Вернуть объявление ${item.title}`
        : `Скрыть объявление ${item.title}`,
    );
    const article = card.querySelector(".car-card");
    const openLoader = card.querySelector(".card-open-loader");
    let opening = false;

    function openFromCard() {
      if (opening) return;
      opening = true;
      article.classList.add("is-opening");
      article.setAttribute("aria-busy", "true");
      openLoader.hidden = false;
      Promise.resolve(openDetails(item)).finally(() => {
        if (article.isConnected) {
          openLoader.hidden = true;
          article.classList.remove("is-opening");
          article.removeAttribute("aria-busy");
        }
        opening = false;
      });
    }

    hideButton.addEventListener("click", async (event) => {
      event.stopPropagation();
      hideButton.disabled = true;
      try {
        const response = await fetch(
          `/api/listings/${encodeURIComponent(item.source)}/${encodeURIComponent(item.external_id)}/visibility`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ hidden: !Boolean(item.hidden) }),
          },
        );
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Не удалось изменить видимость");
        article.classList.add("is-removing");
        state.listingsSignature = "";
        setTimeout(() => refresh().catch(showError), 160);
      } catch (error) {
        hideButton.disabled = false;
        alert(error.message);
      }
    });
    article.addEventListener("click", openFromCard);
    article.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openFromCard();
      }
    });
    container.append(card);
  }
  state.pages = payload.pages;
  renderPagination();
}

function renderRefreshActivity(payload) {
  const panel = document.getElementById("refresh-activity");
  const container = document.getElementById("refresh-activity-items");
  const items = payload.items || [];
  const wasRunning = state.activityRunning;
  state.activityRunning = Boolean(payload.running_count);
  if (!wasRunning && state.activityRunning) {
    state.listingsSignature = "";
    refresh().catch(showError);
    scheduleAutoRefresh(2500);
  }
  const signature = JSON.stringify(
    items.map((item) => [
      item.source,
      item.external_id,
      item.stage,
      item.state,
    ]),
  );
  if (signature === refreshActivitySignature) return;
  refreshActivitySignature = signature;
  clearTimeout(refreshActivityTimer);
  panel.hidden = items.length === 0;
  container.replaceChildren();
  if (!items.length) return;

  setText(
    "refresh-activity-title",
    payload.running_count
      ? "Сейчас обновляется"
      : items.some((item) => item.state === "error")
        ? "Обновление остановлено"
        : "Обновление завершено",
  );
  const stateLabels = {
    running: "Выполняется",
    success: "Обновлено",
    error: "Остановлено",
  };
  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `refresh-activity-item is-${item.state}`;
    const info = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = item.title || item.external_id;
    const stage = document.createElement("small");
    stage.textContent = item.stage;
    info.append(title, stage);
    const status = document.createElement("span");
    status.className = "refresh-activity-status";
    status.textContent = stateLabels[item.state] || item.state;
    button.append(info, status);
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const query = new URLSearchParams({
          source: item.source,
          external_id: item.external_id,
          visibility: "all",
        });
        const response = await fetch(`/api/listings?${query}`);
        const result = await response.json();
        if (!response.ok || !result.items?.length) {
          throw new Error("Карточка не найдена");
        }
        await openDetails(result.items[0]);
      } catch (error) {
        alert(error.message);
      } finally {
        button.disabled = false;
      }
    });
    container.append(button);
  }
  refreshActivityTimer = setTimeout(() => {
    panel.hidden = true;
  }, 3000);
}

function renderPagination() {
  const nav = document.getElementById("pagination");
  nav.replaceChildren();
  if (state.pages <= 1) return;
  const start = Math.max(1, state.page - 2);
  const end = Math.min(state.pages, state.page + 2);
  for (let page = start; page <= end; page += 1) {
    const button = document.createElement("button");
    button.textContent = page;
    button.className = page === state.page ? "active" : "";
    button.addEventListener("click", () => {
      state.page = page;
      refresh();
      window.scrollTo({ top: document.getElementById("catalog-view").offsetTop - 80, behavior: "smooth" });
    });
    nav.append(button);
  }
}

function renderStats(stats) {
  setText("hero-count", money.format(stats.count));
  setText("median-price", stats.median_price == null ? "—" : formatMoney(stats.median_price));
  setText("avg-price", stats.avg_price == null ? "—" : formatMoney(stats.avg_price));
  setText("median-mileage", stats.median_mileage == null ? "—" : formatMileage(stats.median_mileage));
  const correlation = stats.price_mileage_correlation;
  setText("correlation", correlation == null ? "Недостаточно данных" : correlation.toFixed(2));
}

const compactNumber = new Intl.NumberFormat("ru-RU", {
  notation: "compact",
  maximumFractionDigits: 1,
});

function formatCompact(value, suffix = "") {
  return value == null ? "—" : `${compactNumber.format(value)}${suffix}`;
}

function emptyNote(text) {
  const note = document.createElement("p");
  note.className = "empty-note";
  note.textContent = text;
  return note;
}

function renderMarketKpis(summary) {
  const root = document.getElementById("market-kpis");
  const items = [
    ["Объявлений", money.format(summary.count), `${summary.active_count} активных`],
    ["Медианная цена", formatMoney(summary.median_price), `25–75%: ${formatCompact(summary.price_p25, " ₽")} — ${formatCompact(summary.price_p75, " ₽")}`],
    ["Средняя цена", formatMoney(summary.avg_price), `Диапазон ${formatCompact(summary.min_price, " ₽")} — ${formatCompact(summary.max_price, " ₽")}`],
    ["Медианный пробег", formatMileage(summary.median_mileage), `Средний ${formatCompact(summary.avg_mileage, " км")}`],
    ["Новые предложения", money.format(summary.added_7d), `${summary.added_24h} за последние 24 часа`],
    ["Снижения цены", money.format(summary.reductions_count), summary.median_reduction ? `Медиана −${money.format(summary.median_reduction)} ₽ · повышений ${summary.increases_count}` : `Повышений: ${summary.increases_count}`],
    ["Объём предложения", formatCompact(summary.inventory_value, " ₽"), "Суммарная стоимость выборки"],
    ["Полнота данных", `${summary.data_quality}%`, "Фото, описание и характеристики"],
    ["Просмотры", summary.median_views == null ? "—" : money.format(summary.median_views), summary.average_views == null ? "Нет данных платформы" : `В среднем ${money.format(summary.average_views)} на объявление`],
    ["Возраст объявления", summary.median_listing_age_days == null ? "—" : `${money.format(summary.median_listing_age_days)} дн.`, "Медиана по дате публикации"],
    ["Цена ↔ пробег", summary.correlation == null ? "—" : summary.correlation.toFixed(2), "От −1 (обратная связь) до +1"],
    ["Активная выборка", summary.count ? `${Math.round(summary.active_count / summary.count * 100)}%` : "—", `${summary.active_count} объявлений доступны`],
  ];
  root.replaceChildren();
  for (const [label, value, context] of items) {
    const card = document.createElement("article");
    card.className = "market-kpi";
    const name = document.createElement("span");
    name.textContent = label;
    const strong = document.createElement("strong");
    strong.textContent = value;
    const small = document.createElement("small");
    small.textContent = context;
    card.append(name, strong, small);
    root.append(card);
  }
}

function makeInteractive(node, onSelect, item, label) {
  if (!onSelect) return;
  node.classList.add("chart-interactive");
  node.setAttribute("role", "button");
  node.setAttribute("tabindex", "0");
  node.setAttribute("aria-label", label);
  node.addEventListener("click", () => onSelect(item));
  node.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelect(item);
    }
  });
}

function renderColumnChart(id, values, onSelect) {
  const root = document.getElementById(id);
  root.replaceChildren();
  if (!values.length || !values.some((item) => item.value)) {
    root.append(emptyNote("Недостаточно данных для распределения."));
    return;
  }
  const maximum = Math.max(...values.map((item) => item.value), 1);
  for (const item of values) {
    const column = document.createElement("div");
    column.className = "column-item";
    const track = document.createElement("div");
    track.className = "column-track";
    const fill = document.createElement("div");
    fill.className = "column-fill";
    fill.style.height = `${Math.max(2, item.value / maximum * 100)}%`;
    const value = document.createElement("strong");
    value.textContent = money.format(item.value);
    fill.append(value);
    track.append(fill);
    const label = document.createElement("span");
    label.className = "column-label";
    label.textContent = item.label;
    column.append(track, label);
    makeInteractive(
      column,
      onSelect,
      item,
      `Показать автомобили: ${item.label}, ${item.value}`,
    );
    root.append(column);
  }
}

function renderRanking(id, values, contextFormatter, onSelect) {
  const root = document.getElementById(id);
  root.replaceChildren();
  if (!values.length) {
    root.append(emptyNote("Недостаточно данных."));
    return;
  }
  const maximum = Math.max(...values.map((item) => item.count ?? item.value), 1);
  for (const item of values) {
    const count = item.count ?? item.value;
    const row = document.createElement("div");
    row.className = "ranking-row";
    const label = document.createElement("div");
    label.className = "ranking-label";
    const strong = document.createElement("strong");
    strong.textContent = item.label;
    const small = document.createElement("small");
    small.textContent = contextFormatter ? contextFormatter(item) : "";
    label.append(strong, small);
    const track = document.createElement("div");
    track.className = "ranking-track";
    const fill = document.createElement("div");
    fill.className = "ranking-fill";
    fill.style.width = `${count / maximum * 100}%`;
    track.append(fill);
    const value = document.createElement("span");
    value.className = "ranking-value";
    value.textContent = money.format(count);
    row.append(label, track, value);
    makeInteractive(
      row,
      onSelect,
      item,
      `Показать автомобили: ${item.label}, ${count}`,
    );
    root.append(row);
  }
}

function svgNode(name, attributes = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attributes)) {
    node.setAttribute(key, value);
  }
  return node;
}

function renderSeriesChart(id, values, valueKey, labelKey, type = "line", onSelect) {
  const root = document.getElementById(id);
  root.replaceChildren();
  const points = values.filter((item) => Number.isFinite(item[valueKey]));
  if (!points.length) {
    root.append(emptyNote("Пока недостаточно наблюдений."));
    return;
  }
  const width = 720;
  const height = 250;
  const margin = { top: 18, right: 16, bottom: 34, left: 58 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const maximum = Math.max(...points.map((item) => item[valueKey]), 1);
  const svg = svgNode("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": "График рыночных данных",
  });
  for (let index = 0; index <= 4; index += 1) {
    const y = margin.top + plotHeight * index / 4;
    svg.append(svgNode("line", {
      x1: margin.left, x2: width - margin.right, y1: y, y2: y,
      class: "chart-grid",
    }));
    const label = svgNode("text", {
      x: margin.left - 8, y: y + 3, "text-anchor": "end",
      class: "chart-axis",
    });
    label.textContent = compactNumber.format(maximum * (1 - index / 4));
    svg.append(label);
  }
  if (type === "bar") {
    const step = plotWidth / Math.max(points.length, 1);
    points.forEach((item, index) => {
      const barHeight = item[valueKey] / maximum * plotHeight;
      const bar = svgNode("rect", {
        x: margin.left + index * step + step * .15,
        y: margin.top + plotHeight - barHeight,
        width: Math.max(2, step * .7),
        height: Math.max(1, barHeight),
        rx: 2,
        class: "chart-bar",
      });
      const title = svgNode("title");
      title.textContent = `${item[labelKey]}: ${money.format(item[valueKey])}`;
      bar.append(title);
      makeInteractive(
        bar,
        onSelect,
        item,
        `Показать автомобили за ${item[labelKey]}`,
      );
      svg.append(bar);
    });
  } else {
    const coordinates = points.map((item, index) => {
      const x = margin.left + (
        points.length === 1 ? plotWidth / 2 : index / (points.length - 1) * plotWidth
      );
      const y = margin.top + plotHeight - item[valueKey] / maximum * plotHeight;
      return [x, y];
    });
    const path = coordinates.map(
      ([x, y], index) => `${index ? "L" : "M"} ${x} ${y}`
    ).join(" ");
    svg.append(svgNode("path", { d: path, class: "chart-line" }));
    coordinates.forEach(([x, y], index) => {
      const dot = svgNode("circle", { cx: x, cy: y, r: 3, class: "chart-dot" });
      const title = svgNode("title");
      title.textContent = `${points[index][labelKey]}: ${money.format(points[index][valueKey])}`;
      dot.append(title);
      makeInteractive(
        dot,
        onSelect,
        points[index],
        `Показать автомобили: ${points[index][labelKey]}`,
      );
      svg.append(dot);
    });
  }
  const labelIndexes = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])];
  for (const index of labelIndexes) {
    const x = margin.left + (
      points.length === 1 ? plotWidth / 2 : index / (points.length - 1) * plotWidth
    );
    const label = svgNode("text", {
      x, y: height - 10, "text-anchor": index === 0 ? "start" : index === points.length - 1 ? "end" : "middle",
      class: "chart-axis",
    });
    label.textContent = points[index][labelKey];
    svg.append(label);
  }
  root.append(svg);
}

function renderScatter(values) {
  const root = document.getElementById("market-scatter");
  root.replaceChildren();
  if (!values.length) {
    root.append(emptyNote("Нужны объявления одновременно с ценой и пробегом."));
    return;
  }
  const width = 760;
  const height = 300;
  const margin = { top: 18, right: 18, bottom: 38, left: 64 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const maxPrice = Math.max(...values.map((item) => item.price), 1);
  const maxMileage = Math.max(...values.map((item) => item.mileage), 1);
  const svg = svgNode("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": "Диаграмма зависимости цены автомобиля от пробега",
  });
  for (let index = 0; index <= 4; index += 1) {
    const y = margin.top + plotHeight * index / 4;
    const x = margin.left + plotWidth * index / 4;
    svg.append(svgNode("line", { x1: margin.left, x2: width - margin.right, y1: y, y2: y, class: "chart-grid" }));
    svg.append(svgNode("line", { x1: x, x2: x, y1: margin.top, y2: height - margin.bottom, class: "chart-grid" }));
    const yLabel = svgNode("text", { x: margin.left - 8, y: y + 3, "text-anchor": "end", class: "chart-axis" });
    yLabel.textContent = compactNumber.format(maxPrice * (1 - index / 4));
    svg.append(yLabel);
    const xLabel = svgNode("text", { x, y: height - 12, "text-anchor": "middle", class: "chart-axis" });
    xLabel.textContent = compactNumber.format(maxMileage * index / 4);
    svg.append(xLabel);
  }
  for (const item of values) {
    const circle = svgNode("circle", {
      cx: margin.left + item.mileage / maxMileage * plotWidth,
      cy: margin.top + plotHeight - item.price / maxPrice * plotHeight,
      r: 3.5,
      class: "chart-dot",
    });
    const title = svgNode("title");
    title.textContent = `${item.title}: ${formatMoney(item.price)}, ${formatMileage(item.mileage)}`;
    circle.append(title);
    makeInteractive(
      circle,
      openMarketPoint,
      item,
      `Открыть ${item.title}`,
    );
    svg.append(circle);
  }
  root.append(svg);
}

function renderComposition(composition) {
  const root = document.getElementById("market-composition");
  root.replaceChildren();
  const labels = {
    fuel: "Тип двигателя",
    transmission: "Коробка передач",
    drive: "Привод",
    body: "Тип кузова",
    color: "Цвет",
    owners: "Владельцы по ПТС",
  };
  for (const [key, title] of Object.entries(labels)) {
    const panel = document.createElement("article");
    panel.className = "composition-panel";
    const heading = document.createElement("h3");
    heading.textContent = title;
    const list = document.createElement("div");
    list.className = "ranking-list";
    panel.append(heading, list);
    root.append(panel);
    const values = composition[key] || [];
    if (!values.length) {
      list.append(emptyNote("Нет данных"));
      continue;
    }
    const maximum = Math.max(...values.map((item) => item.value), 1);
    for (const item of values.slice(0, 6)) {
      const row = document.createElement("div");
      row.className = "ranking-row";
      const label = document.createElement("div");
      label.className = "ranking-label";
      const strong = document.createElement("strong");
      strong.textContent = item.label;
      label.append(strong);
      const track = document.createElement("div");
      track.className = "ranking-track";
      const fill = document.createElement("div");
      fill.className = "ranking-fill";
      fill.style.width = `${item.value / maximum * 100}%`;
      track.append(fill);
      const value = document.createElement("span");
      value.className = "ranking-value";
      value.textContent = item.value;
      row.append(label, track, value);
      makeInteractive(
        row,
        (selected) => openMarketSample(
          `${title}: ${selected.label}`,
          { [key]: selected.label },
        ),
        item,
        `Показать автомобили: ${item.label}`,
      );
      list.append(row);
    }
  }
}

function renderDeals(values) {
  const root = document.getElementById("market-deals");
  root.replaceChildren();
  if (!values.length) {
    root.append(emptyNote("Нет групп минимум из трёх сопоставимых автомобилей."));
    return;
  }
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const headerRow = document.createElement("tr");
  for (const text of ["Автомобиль", "Цена", "Медиана", "Разница"]) {
    const cell = document.createElement("th");
    cell.textContent = text;
    headerRow.append(cell);
  }
  head.append(headerRow);
  const body = document.createElement("tbody");
  for (const item of values) {
    const row = document.createElement("tr");
    const titleCell = document.createElement("td");
    const link = document.createElement("a");
    link.href = item.url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = item.title;
    titleCell.append(link);
    const price = document.createElement("td");
    price.textContent = formatMoney(item.price);
    const benchmark = document.createElement("td");
    benchmark.textContent = formatMoney(item.benchmark);
    const discount = document.createElement("td");
    discount.className = "deal-discount";
    discount.textContent = `−${item.discount_percent}%`;
    row.append(titleCell, price, benchmark, discount);
    body.append(row);
  }
  table.append(head, body);
  root.append(table);
}

const marketSampleDialog = document.getElementById("market-sample-dialog");

function sampleListingRow(item) {
  const row = document.createElement("article");
  row.className = "market-sample-row";

  const media = document.createElement("div");
  media.className = "sample-media";
  if (item.thumbnail_url) {
    const image = document.createElement("img");
    image.src = item.thumbnail_url;
    image.alt = "";
    image.loading = "lazy";
    media.append(image);
  } else {
    media.textContent = "AUTO";
  }

  const content = document.createElement("div");
  content.className = "sample-content";
  const title = document.createElement("h3");
  title.textContent = item.title;
  const facts = document.createElement("p");
  facts.textContent = [
    formatMileage(item.mileage_km),
    item.location || "Город не указан",
    item.published_at ? `Опубликовано ${displayPublished(item.published_at)}` : "",
  ].filter(Boolean).join(" · ");
  const badges = document.createElement("div");
  badges.className = "sample-badges";
  if (item.hidden) {
    const badge = document.createElement("span");
    badge.textContent = "Скрыто из каталога";
    badges.append(badge);
  }
  content.append(title, facts, badges);

  const aside = document.createElement("div");
  aside.className = "sample-aside";
  const price = document.createElement("strong");
  price.textContent = formatMoney(item.price);
  const details = document.createElement("button");
  details.type = "button";
  details.textContent = "Карточка";
  details.addEventListener("click", () => {
    marketSampleDialog.close();
    openDetails(item);
  });
  const link = document.createElement("a");
  link.href = item.url;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = "Источник ↗";
  aside.append(price, details, link);
  row.append(media, content, aside);
  return row;
}

async function openMarketSample(title, overrides = {}) {
  const list = document.getElementById("market-sample-list");
  const loader = document.getElementById("market-sample-loader");
  setText("market-sample-title", title);
  setText("market-sample-subtitle", "");
  list.replaceChildren();
  loader.hidden = false;
  if (!marketSampleDialog.open) marketSampleDialog.showModal();

  const query = params(false);
  query.set("page", "1");
  query.set("page_size", "100");
  query.set("visibility", "all");
  for (const [key, value] of Object.entries(overrides)) {
    if (value == null || value === "") continue;
    query.delete(key);
    if (Array.isArray(value)) {
      value.forEach((entry) => query.append(key, entry));
    } else {
      query.set(key, value);
    }
  }

  try {
    const response = await fetch(`/api/listings?${query}`);
    if (!response.ok) throw new Error("Не удалось загрузить объявления");
    const payload = await response.json();
    setText(
      "market-sample-subtitle",
      `Показано ${money.format(payload.items.length)} из ${money.format(payload.total)}. Скрытые карточки включены, как и в статистике.`,
    );
    if (!payload.items.length) {
      list.append(emptyNote("В этом срезе нет автомобилей."));
      return;
    }
    list.append(...payload.items.map(sampleListingRow));
  } catch (error) {
    list.append(emptyNote(`Ошибка загрузки: ${error.message}`));
  } finally {
    loader.hidden = true;
  }
}

function openMarketPoint(item) {
  openMarketSample(item.title, {
    source: item.source,
    external_id: item.external_id,
  });
}

function renderMarket(payload) {
  const { summary } = payload;
  setText("hero-count", money.format(summary.count));
  setText("analysis-count", `${money.format(summary.count)} автомобилей`);
  renderMarketKpis(summary);
  const insights = document.getElementById("market-insights");
  insights.replaceChildren();
  for (const text of payload.insights) {
    const item = document.createElement("article");
    item.className = "insight";
    item.textContent = text;
    insights.append(item);
  }
  if (!payload.insights.length) insights.append(emptyNote("Недостаточно данных для выводов."));
  renderColumnChart(
    "market-price-histogram",
    payload.price_histogram,
    (item) => openMarketSample(`Ценовой сегмент: ${item.label}`, {
      min_price: item.min_value,
      max_price: item.max_value,
    }),
  );
  renderColumnChart(
    "market-mileage-histogram",
    payload.mileage_histogram,
    (item) => openMarketSample(`Пробег: ${item.label}`, {
      min_mileage: item.min_value,
      max_mileage: item.max_value,
    }),
  );
  renderScatter(payload.scatter);
  renderSeriesChart(
    "market-additions",
    payload.additions_timeline,
    "value",
    "date",
    "bar",
    (item) => openMarketSample(`Новые объявления за ${item.date}`, {
      first_seen_date: item.date,
    }),
  );
  renderSeriesChart(
    "market-years",
    payload.year_prices,
    "median_price",
    "year",
    "line",
    (item) => openMarketSample(`Автомобили ${item.year} года`, {
      year: item.year,
    }),
  );
  renderSeriesChart("market-price-trend", payload.price_timeline, "median_price", "date");
  renderRanking(
    "market-brands",
    payload.brands,
    (item) => item.median_price ? `Медиана ${formatCompact(item.median_price, " ₽")}` : "Цена не указана",
    (item) => openMarketSample(`Марка: ${item.label}`, { brand: item.label }),
  );
  renderRanking(
    "market-models",
    payload.models,
    (item) => item.median_mileage ? `Пробег ${formatCompact(item.median_mileage, " км")}` : "Пробег не указан",
    (item) => openMarketSample(`Модель: ${item.label}`, {
      brand: item.brand,
      model: item.model,
    }),
  );
  renderRanking(
    "market-locations",
    payload.locations,
    undefined,
    (item) => openMarketSample(`Город: ${item.label}`, { location: item.label }),
  );
  renderComposition(payload.composition);
  renderDeals(payload.deals);
}

document.getElementById("analysis-open-sample").addEventListener(
  "click",
  () => openMarketSample("Текущая выборка"),
);
document.getElementById("analysis-export").addEventListener("click", () => {
  window.location.assign(`/api/export-analysis?${params(false)}`);
});

async function refresh() {
  if (state.refreshRunning) {
    state.refreshPending = true;
    return;
  }
  state.refreshRunning = true;
  state.refreshPending = false;
  try {
    if (state.view === "garage") {
      await garageController.refresh();
      return;
    }
    if (state.view === "analytics") {
      const response = await fetch(`/api/market?${params(false)}`);
      if (!response.ok) throw new Error("Не удалось рассчитать аналитику");
      const market = await response.json();
      const signature = JSON.stringify(market);
      if (signature !== state.marketSignature) {
        state.marketSignature = signature;
        renderMarket(market);
      }
      return;
    }
    const query = params();
    const statsQuery = params(false);
    const [listingsResponse, statsResponse] = await Promise.all([
      fetch(`/api/listings?${query}`),
      fetch(`/api/stats?${statsQuery}`),
    ]);
    if (!listingsResponse.ok || !statsResponse.ok) {
      throw new Error("Не удалось прочитать локальную базу");
    }
    const listings = await listingsResponse.json();
    const stats = await statsResponse.json();
    const listingsSignature = JSON.stringify(listings);
    const statsSignature = JSON.stringify(stats);
    if (listingsSignature !== state.listingsSignature) {
      state.listingsSignature = listingsSignature;
      renderCards(listings);
    }
    if (statsSignature !== state.statsSignature) {
      state.statsSignature = statsSignature;
      renderStats(stats);
    }
  } finally {
    state.refreshRunning = false;
    if (state.refreshPending) {
      state.refreshPending = false;
      queueMicrotask(() => refresh().catch(showError));
    }
  }
}

async function pollActivity() {
  if (document.hidden) return;
  try {
    const response = await fetch("/api/refresh-activity");
    if (!response.ok) throw new Error("Не удалось прочитать фоновые операции");
    renderRefreshActivity(await response.json());
  } catch (error) {
    console.warn(error);
  }
}

let autoRefreshTimer;
function autoRefreshDelay() {
  if (state.activityRunning) return 2500;
  return state.view === "catalog" ? 10000 : 30000;
}

function scheduleAutoRefresh(delay = autoRefreshDelay()) {
  clearTimeout(autoRefreshTimer);
  autoRefreshTimer = setTimeout(async () => {
    if (!document.hidden) {
      await refresh().catch(showError);
    }
    scheduleAutoRefresh();
  }, delay);
}

let timer;
function scheduleRefresh() {
  state.page = 1;
  savePreferences();
  clearTimeout(timer);
  timer = setTimeout(() => refresh().catch(showError), 250);
}

function showError(error) {
  if (state.view === "garage") {
    alert(`Ошибка гаража: ${error.message}`);
    return;
  }
  if (state.view === "analytics") {
    const root = document.getElementById("market-kpis");
    root.replaceChildren(emptyNote(`Ошибка загрузки аналитики: ${error.message}`));
    return;
  }
  const empty = document.getElementById("empty");
  empty.hidden = false;
  empty.querySelector("strong").textContent = "Ошибка загрузки";
  empty.querySelector("p").textContent = error.message;
}

function switchView(view, shouldRefresh = true) {
  if (!["catalog", "analytics", "garage"].includes(view) || state.view === view) return;
  state.view = view;
  state.page = 1;
  document.getElementById("catalog-view").hidden = view !== "catalog";
  document.getElementById("analytics-view").hidden = view !== "analytics";
  document.getElementById("garage-view").hidden = view !== "garage";
  document.getElementById("listing-filters").hidden = view === "garage";
  for (const button of document.querySelectorAll(".nav-button")) {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  }
  const analytics = view === "analytics";
  const garage = view === "garage";
  setText("view-title", garage ? "Гараж" : analytics ? "Аналитика рынка" : "Объявления");
  setText(
    "view-subtitle",
    garage
      ? "Автомобили в собственности, бортжурнал, обслуживание, расходы и будущие запчасти."
      : analytics
      ? "Цены, динамика предложения, структура автопарка и варианты ниже рынка."
      : "Актуальная выборка, история цен и подробные карточки автомобилей.",
  );
  savePreferences();
  if (shouldRefresh) refresh().catch(showError);
  scheduleAutoRefresh();
}

for (const button of document.querySelectorAll(".nav-button")) {
  button.addEventListener("click", () => switchView(button.dataset.view));
}

for (const element of Object.values(elements)) {
  element.addEventListener(element.tagName === "INPUT" ? "input" : "change", scheduleRefresh);
}

document.getElementById("reset").addEventListener("click", () => {
  for (const element of Object.values(elements)) {
    element.value = element.id === "sort"
      ? "recently_updated"
      : element.id === "visibility"
        ? "visible"
        : "";
  }
  for (const element of multiFilterElements) {
    multiFilterState.get(element.dataset.filter).clear();
    for (const checkbox of element.querySelectorAll("input[type=checkbox]")) {
      checkbox.checked = false;
    }
    updateMultiFilterSummary(element);
    element.open = false;
  }
  scheduleRefresh();
});

fetch("/api/meta")
  .then((response) => response.json())
  .then(({ locations, brands, attribute_options: attributeOptions }) => {
    for (const value of locations) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      elements.location.append(option);
    }
    for (const value of brands) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      elements.brand.append(option);
    }
    restoreControlPreferences();
    setupMultiFilters(attributeOptions);
    if (["analytics", "garage"].includes(savedPreferences?.view)) {
      switchView(savedPreferences.view, false);
    }
    savePreferences();
  })
  .then(() => garageController.init())
  .then(() => notificationCenter.init())
  .then(refresh)
  .then(() => {
    pollActivity();
    scheduleAutoRefresh();
  })
  .catch(showError);

setInterval(pollActivity, 2000);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    pollActivity();
    refresh().catch(showError);
    scheduleAutoRefresh();
  }
});

const detailsDialog = document.getElementById("details-dialog");
let carouselImages = [];
let carouselIndex = 0;
let carouselLoading = false;
let carouselTotal = 0;
let detailLoadToken = 0;

function renderCarousel() {
  const image = document.getElementById("detail-image");
  const empty = document.getElementById("detail-image-empty");
  const count = document.getElementById("carousel-count");
  const loader = document.getElementById("carousel-loader");
  const thumbs = document.getElementById("carousel-thumbs");
  loader.hidden = !carouselLoading;
  loader.querySelector("strong").textContent = carouselTotal
    ? `Загружено ${carouselImages.length} из ${carouselTotal}`
    : "Получаем список фотографий…";
  thumbs.replaceChildren();
  if (!carouselImages.length) {
    image.removeAttribute("src");
    image.hidden = true;
    empty.hidden = carouselLoading;
    count.textContent = carouselLoading ? "Загружаем фотографии…" : "0 фото";
    return;
  }
  image.hidden = false;
  empty.hidden = true;
  image.src = carouselImages[carouselIndex];
  count.textContent = carouselLoading
    ? `${carouselIndex + 1} / ${carouselImages.length} · загружаем галерею…`
    : `${carouselIndex + 1} / ${carouselImages.length}`;
  carouselImages.forEach((url, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = index === carouselIndex ? "active" : "";
    const thumb = document.createElement("img");
    thumb.src = url;
    thumb.alt = `Фото ${index + 1}`;
    button.append(thumb);
    button.addEventListener("click", () => {
      carouselIndex = index;
      renderCarousel();
    });
    thumbs.append(button);
  });
}

function renderDetailAttributes(values) {
  const attributes = document.getElementById("detail-attributes");
  attributes.replaceChildren();
  const entries = Object.entries(values || {});
  if (!entries.length) {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = "Данные";
    const value = document.createElement("dd");
    value.textContent = "Характеристики пока не загружены";
    row.append(term, value);
    attributes.append(row);
    return;
  }
  for (const [key, value] of entries) {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = key;
    const description = document.createElement("dd");
    description.textContent = value;
    row.append(term, description);
    attributes.append(row);
  }
}

async function loadGallery(item, token) {
  let prepare = true;
  let warningShown = false;
  try {
    while (token === detailLoadToken && detailsDialog.open) {
      const suffix = prepare ? "?prepare=1" : "";
      setText(
        "detail-load-status",
        prepare
          ? "Приоритетно обновляем описание и характеристики…"
          : `Загружаем фотографии: ${carouselImages.length} из ${carouselTotal || "…"}…`,
      );
      const response = await fetch(
        `/api/listings/${encodeURIComponent(item.source)}/${encodeURIComponent(item.external_id)}/images${suffix}`,
        { method: "POST" },
      );
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || "Не удалось загрузить галерею");
      }
      if (token !== detailLoadToken) return;
      carouselImages = [...new Set(payload.images || carouselImages)];
      carouselTotal = payload.total_count || carouselImages.length;
      carouselIndex = Math.min(
        carouselIndex,
        Math.max(0, carouselImages.length - 1),
      );
      setText(
        "detail-description",
        payload.description || "Описание пока не загружено.",
      );
      setText("detail-mileage", formatMileage(payload.mileage_km));
      setText(
        "detail-views",
        payload.views_count == null
          ? "Не указаны"
          : money.format(payload.views_count),
      );
      setText("detail-published", displayPublished(payload.published_at));
      renderDetailAttributes(payload.attributes);
      renderCarousel();
      if (payload.warning) {
        warningShown = true;
        setText("detail-load-status", payload.warning);
        console.warn(payload.warning);
      } else {
        let status = "Загружаем следующую фотографию…";
        if (payload.complete) {
          status = "Карточка полностью обновлена.";
        } else if (payload.details_refreshed) {
          status = "Описание и характеристики обновлены. Загружаем фотографии…";
        } else if (payload.downloaded_count) {
          status = `Фотография добавлена. Осталось: ${payload.remaining_count}.`;
        }
        setText("detail-load-status", status);
      }
      if (payload.complete || payload.stalled) break;
      prepare = false;
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
  } catch (error) {
    if (token === detailLoadToken) {
      warningShown = true;
      setText("detail-load-status", `Не удалось догрузить данные: ${error.message}`);
      console.error(error);
    }
  } finally {
    if (token === detailLoadToken) {
      carouselLoading = false;
      carouselIndex = Math.min(
        carouselIndex,
        Math.max(0, carouselImages.length - 1),
      );
      renderCarousel();
      if (!warningShown && !carouselImages.length) {
        setText(
          "detail-load-status",
          "Локальные данные показаны, фотографии пока недоступны.",
        );
      }
    }
  }
}

function openDetails(item) {
  const token = ++detailLoadToken;
  setText("detail-brand", [item.brand, item.model].filter(Boolean).join(" · "));
  setText("detail-title", item.title);
  setText("detail-price", formatMoney(item.price));
  setText("detail-mileage", formatMileage(item.mileage_km));
  setText("detail-views", item.views_count == null ? "Не указаны" : money.format(item.views_count));
  setText("detail-published", displayPublished(item.published_at));
  setText("detail-location", item.location || "Не указан");
  setText("detail-description", item.description || "Описание пока не загружено.");
  setText("detail-load-status", "Догружаем данные объявления…");
  document.getElementById("detail-link").href = item.url;
  renderDetailAttributes(item.attributes);
  carouselImages = [...new Set(item.images || [])];
  carouselIndex = 0;
  carouselTotal = 0;
  carouselLoading = true;
  renderCarousel();
  detailsDialog.showModal();
  return loadGallery(item, token);
}

document.getElementById("carousel-prev").addEventListener("click", () => {
  if (!carouselImages.length) return;
  carouselIndex = (carouselIndex - 1 + carouselImages.length) % carouselImages.length;
  renderCarousel();
});
document.getElementById("carousel-next").addEventListener("click", () => {
  if (!carouselImages.length) return;
  carouselIndex = (carouselIndex + 1) % carouselImages.length;
  renderCarousel();
});

for (const dialog of document.querySelectorAll("dialog")) {
  dialog.querySelector(".dialog-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
}

const profilesDialog = document.getElementById("profiles-dialog");
const ALL_CARS_TVER_URL = "https://www.avito.ru/tver/avtomobili/s_probegom-ASgBAgICAUSGFMjmAQ?context=H4sIAAAAAAAA_wEmANn_YToxOntzOjE6InkiO3M6MTY6Ik5zc0RrbFcyUGlMd0hsZDMiO33JlMymJgAAAA&f=ASgBAgICAkSGFMjmAezqFJKZkAM&localPriority=0&radius=200&searchRadius=200";
let profilesRefreshTimer;
document.getElementById("profiles-open").addEventListener("click", () => {
  loadProfiles();
  profilesDialog.showModal();
  clearInterval(profilesRefreshTimer);
  profilesRefreshTimer = setInterval(() => {
    if (profilesDialog.open) loadProfiles().catch(console.error);
  }, 3000);
});
profilesDialog.addEventListener("close", () => {
  clearInterval(profilesRefreshTimer);
});

async function profileRequest(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Ошибка профиля");
  return payload;
}

async function loadProfiles() {
  const payload = await profileRequest("/api/search-profiles");
  const root = document.getElementById("profiles-list");
  root.replaceChildren();
  if (!payload.items.length) {
    root.textContent = "Поисковые профили ещё не добавлены.";
    return;
  }
  for (const item of payload.items) {
    const row = document.createElement("div");
    row.className = "profile-row";
    const title = document.createElement("div");
    const strong = document.createElement("strong");
    const isUrlProfile = item.query.startsWith("https://");
    strong.textContent = isUrlProfile
      ? "Все автомобили с пробегом"
      : item.query;
    const meta = document.createElement("small");
    meta.textContent = isUrlProfile
      ? `По сохранённой ссылке · ${item.source === "auto_ru" ? "Auto.ru" : "Avito"}`
      : `${item.source === "auto_ru" ? "Auto.ru" : "Avito"} · ${item.region} · ${item.radius ?? "любой радиус"} км`;
    title.append(strong, meta);
    const interval = document.createElement("div");
    interval.textContent = `Каждые ${item.interval_minutes} мин.`;
    const state = document.createElement("div");
    const stateLabel = document.createElement("span");
    if (item.last_status === "running") {
      stateLabel.textContent = "Выполняется…";
    } else if (item.waiting_reason) {
      stateLabel.textContent = item.waiting_reason;
    } else if (item.last_status === "ok") {
      stateLabel.textContent = `Готово · ${item.last_result_count} шт.`;
    } else {
      stateLabel.textContent = item.last_status || "Ожидает запуска";
    }
    state.append(stateLabel);
    const effectiveNextRun = item.waiting_until || item.next_run_at;
    if (item.enabled && effectiveNextRun && item.last_status !== "running") {
      const nextRun = document.createElement("small");
      nextRun.textContent = item.waiting_until
        ? `Автоматическое продолжение: ${displayPublished(effectiveNextRun)}`
        : `Следующий запуск: ${displayPublished(effectiveNextRun)}`;
      state.append(nextRun);
    }
    const actions = document.createElement("div");
    actions.className = "profile-actions";
    const run = document.createElement("button");
    run.textContent = "Запустить";
    run.addEventListener("click", async () => {
      await profileRequest(`/api/search-profiles/${item.id}/run`, { method: "POST", body: "{}" });
      await loadProfiles();
    });
    const toggle = document.createElement("button");
    toggle.textContent = item.enabled ? "Пауза" : "Включить";
    toggle.addEventListener("click", async () => {
      await profileRequest(`/api/search-profiles/${item.id}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: !item.enabled }),
      });
      await loadProfiles();
    });
    const remove = document.createElement("button");
    remove.textContent = "Удалить";
    remove.addEventListener("click", async () => {
      await profileRequest(`/api/search-profiles/${item.id}`, { method: "DELETE" });
      await loadProfiles();
    });
    actions.append(run, toggle, remove);
    row.append(title, interval, state, actions);
    root.append(row);
  }
}

document.getElementById("profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  try {
    await profileRequest("/api/search-profiles", {
      method: "POST",
      body: JSON.stringify({
        source: form.get("source"),
        query: form.get("query"),
        region: form.get("region"),
        radius: Number(form.get("radius")),
        interval_minutes: Number(form.get("interval_minutes")),
      }),
    });
    formElement.reset();
    await loadProfiles();
  } catch (error) {
    alert(error.message);
  }
});

document.getElementById("profile-all-cars").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  button.disabled = true;
  try {
    await profileRequest("/api/search-profiles", {
      method: "POST",
      body: JSON.stringify({
        source: "avito",
        query: ALL_CARS_TVER_URL,
        region: "tver",
        radius: 200,
        interval_minutes: 60,
      }),
    });
    await loadProfiles();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
});
