import { createGarageController } from "./garage.js";
import { createNotificationCenter } from "./notifications.js";
import { formatMileage, formatMoney, money, setText } from "./ui.js";

const state = {
  page: 1,
  pages: 0,
  listingsSignature: "",
  statsSignature: "",
  marketSignature: "",
  knowledgeSignature: "",
  sparePartsSignature: "",
  sparePartsPage: 1,
  knowledgePage: 1,
  soldPage: 1,
  sparePartsCategory: "",
  knowledgePayload: null,
  refreshRunning: false,
  refreshPending: false,
  activityRunning: false,
  view: "catalog",
  activeProfileId: null,
  activeProfileLabel: "",
};
const ids = [
  "search", "min-price", "max-price", "min-mileage",
  "max-mileage", "source", "brand", "model", "year", "location",
  "visibility", "favorite", "sort"
];
const elements = Object.fromEntries(ids.map((id) => [id, document.getElementById(id)]));
const multiFilterElements = [...document.querySelectorAll(".multi-filter")];
const PREFERENCES_KEY = "autoscope.filters.v1";
const COMPARISON_KEY = "autoscope.comparison.v1";
let refreshActivitySignature = "";
let refreshActivityTimer;
let latestRefreshActivity = { items: [], running_count: 0 };
let refreshActivityOpen = false;
let displayedListingsSignature = "";
let displayedListingsReportedAt = 0;
let modelsByBrand = {};
let allModelOptions = [];

function displayedListingsClientId() {
  const key = "autoscope.displayed-listings-client.v1";
  try {
    let clientId = sessionStorage.getItem(key);
    if (!clientId) {
      clientId = globalThis.crypto?.randomUUID?.()
        || `browser-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      sessionStorage.setItem(key, clientId);
    }
    return clientId;
  } catch {
    return `browser-${Math.random().toString(36).slice(2)}`;
  }
}

const displayedClientId = displayedListingsClientId();

function configureDromBookmarklet() {
  const link = document.getElementById("spare-parts-bookmarklet");
  const target = `${globalThis.location.origin}/capture.html`;
  const origin = globalThis.location.origin;
  const pageCount = Number(document.getElementById("spare-parts-import-pages")?.value || 3);
  const code = `(async function(){const target=${JSON.stringify(target)},origin=${JSON.stringify(origin)},pageCount=${pageCount};if(location.hostname!=="baza.drom.ru"&&location.hostname!=="www.baza.drom.ru"){alert("Откройте страницу списка запчастей Drom и нажмите закладку там.");return}const popup=open(target,"autoscope_capture","width=560,height=420");if(!popup){alert("Разрешите всплывающее окно для импорта в AutoScope.");return}const pages=[{url:location.href,html:document.documentElement.outerHTML}];const base=new URL(location.href);base.pathname=base.pathname.replace(/\\/page\\d+\\/?$/,"/");for(let page=2;page<=pageCount;page++){const next=new URL(base.href);next.pathname=next.pathname.replace(/\\/$/,"")+"/page"+page+"/";try{const response=await fetch(next.href,{credentials:"include"});const html=await response.text();if(!response.ok||/Вы не робот|подозрительный трафик|\\/verify\\?/i.test(html))break;pages.push({url:next.href,html});await new Promise(resolve=>setTimeout(resolve,1200))}catch(error){break}}let sent=false;const receive=function(event){if(event.origin!==origin)return;if(event.data&&event.data.type==="autoscope-ready"&&!sent){sent=true;popup.postMessage({type:"autoscope-capture",url:location.href,html:pages[0].html,pages},origin)}if(event.data&&(event.data.type==="autoscope-import-complete"||event.data.type==="autoscope-import-error")){alert(event.data.message);removeEventListener("message",receive)}};addEventListener("message",receive)})();`;
  link.href = `javascript:${code}`;
  link.title = "Перетащите эту кнопку в панель закладок браузера";
}

configureDromBookmarklet();
document.getElementById("spare-parts-import-pages")?.addEventListener("change", configureDromBookmarklet);

function reportDisplayedListings(items, force = false) {
  const displayed = (items || []).slice(0, 100).map((item) => ({
    source: item.source,
    external_id: item.external_id,
  }));
  const signature = JSON.stringify(displayed);
  const now = Date.now();
  if (
    !force
    && signature === displayedListingsSignature
    && now - displayedListingsReportedAt < 15000
  ) {
    return;
  }
  displayedListingsSignature = signature;
  displayedListingsReportedAt = now;
  fetch("/api/displayed-listings", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      client_id: displayedClientId,
      items: displayed,
    }),
    keepalive: true,
  }).catch((error) => console.warn("Не удалось передать приоритет карточек", error));
}

function setRefreshActivityOpen(open) {
  const panel = document.getElementById("refresh-activity");
  const trigger = document.getElementById("refresh-activity-open");
  refreshActivityOpen = Boolean(open && latestRefreshActivity.items?.length);
  panel.hidden = !refreshActivityOpen;
  trigger.setAttribute("aria-expanded", String(refreshActivityOpen));
}

function loadPreferences() {
  try {
    return JSON.parse(localStorage.getItem(PREFERENCES_KEY) || "null");
  } catch {
    return null;
  }
}

const savedPreferences = loadPreferences();
const comparisonSelection = new Map();
try {
  const savedComparison = JSON.parse(localStorage.getItem(COMPARISON_KEY) || "[]");
  for (const item of savedComparison.slice(0, 2)) {
    if (item?.source && item?.external_id) {
      comparisonSelection.set(`${item.source}:${item.external_id}`, item);
    }
  }
} catch {
  // Сравнение продолжит работать без сохранения между перезагрузками.
}
const multiFilterState = new Map(
  multiFilterElements.map((element) => [
    element.dataset.filter,
    new Set(savedPreferences?.multi?.[element.dataset.filter] || []),
  ]),
);
const garageController = createGarageController();
const notificationCenter = createNotificationCenter();

function listingKey(item) {
  return `${item.source}:${item.external_id}`;
}

function renderComparisonSelection() {
  const bar = document.getElementById("comparison-bar");
  const items = [...comparisonSelection.values()];
  bar.hidden = items.length === 0;
  setText(
    "comparison-selection",
    items.length
      ? items.map((item) => item.title || item.external_id).join(" ↔ ")
      : "Выберите два объявления",
  );
  document.getElementById("comparison-open").disabled = items.length !== 2;
  for (const button of document.querySelectorAll(".car-compare")) {
    const selected = comparisonSelection.has(button.dataset.listingKey);
    button.classList.toggle("is-selected", selected);
    button.textContent = selected ? "В сравнении" : "Сравнить";
  }
  if (currentDetailItem) {
    const selected = comparisonSelection.has(listingKey(currentDetailItem));
    const button = document.getElementById("detail-compare");
    button.classList.toggle("is-selected", selected);
    button.textContent = selected ? "Убрать из сравнения" : "Добавить к сравнению";
  }
  try {
    localStorage.setItem(COMPARISON_KEY, JSON.stringify(items));
  } catch {
    // Состояние остаётся доступно до перезагрузки страницы.
  }
}

function toggleComparison(item) {
  const key = listingKey(item);
  if (comparisonSelection.has(key)) {
    comparisonSelection.delete(key);
  } else {
    if (comparisonSelection.size >= 2) {
      alert("В сравнении уже два автомобиля. Уберите один из них.");
      return;
    }
    comparisonSelection.set(key, {
      source: item.source,
      external_id: item.external_id,
      title: item.title,
    });
  }
  renderComparisonSelection();
}

async function saveListingUserData(item, changes) {
  const current = item.user_data || { favorite: false, note: "" };
  const response = await fetch(
    `/api/listings/${encodeURIComponent(item.source)}/${encodeURIComponent(item.external_id)}/user-data`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...current, ...changes }),
    },
  );
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Не удалось сохранить данные");
  item.user_data = payload;
  state.listingsSignature = "";
  return payload;
}

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
  if (state.activeProfileId != null) {
    query.set("profile_id", String(state.activeProfileId));
  }
  const mapping = {
    search: "q",
    "min-price": "min_price",
    "max-price": "max_price",
    "min-mileage": "min_mileage",
    "max-mileage": "max_mileage",
    source: "source",
    brand: "brand",
    model: "model",
    year: "year",
    location: "location",
    visibility: "visibility",
    favorite: "favorite",
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

function fillDatalist(id, values) {
  const root = document.getElementById(id);
  root.replaceChildren();
  const fragment = document.createDocumentFragment();
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    fragment.append(option);
  }
  root.append(fragment);
}

function updateModelDatalist() {
  const brandQuery = elements.brand.value.trim().toLocaleLowerCase("ru-RU");
  if (!brandQuery) {
    fillDatalist("model-options", allModelOptions);
    return;
  }
  const matches = Object.entries(modelsByBrand)
    .filter(([brand]) => brand.toLocaleLowerCase("ru-RU").includes(brandQuery))
    .flatMap(([, models]) => models);
  fillDatalist(
    "model-options",
    [...new Set(matches)].sort((left, right) => left.localeCompare(right, "ru")),
  );
}

function marketPositionLabel(analytics, { compact = false } = {}) {
  const difference = analytics?.price_difference_percent;
  if (difference == null) {
    return compact ? "Мало данных" : "Нет ценового ориентира";
  }
  if (Math.abs(difference) < 2) {
    return compact ? "На уровне рынка" : "Цена на уровне средней";
  }
  return compact
    ? `${difference < 0 ? "Ниже" : "Выше"} рынка на ${Math.abs(difference)}%`
    : `${Math.abs(difference)}% ${difference < 0 ? "ниже" : "выше"} средней`;
}

function marketSampleLabel(analytics) {
  if (!analytics) return "Статистика ещё не накоплена";
  const years = analytics.generation_limited
    ? ` · ${analytics.year_from}–${analytics.year_to} г.`
    : " · все годы";
  return `${money.format(analytics.listings_count)} объявлений${years}`;
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
    card.querySelector(".car-year").textContent = item.year
      ? `${item.year} год`
      : "Год не указан";
    card.querySelector(".car-mileage").textContent = formatMileage(item.mileage_km);
    card.querySelector(".car-location").textContent = item.location || "Город не указан";
    card.querySelector(".car-published").textContent =
      item.published_at
        ? `${item.published_at_inferred ? "Добавлено в базу" : "Опубликовано"}: ${displayPublished(item.published_at)}`
        : "Дата не указана";
    card.querySelector(".car-description").textContent =
      item.description || "Описание пока не загружено.";
    const marketBadge = card.querySelector(".car-market-badge");
    const marketDifference = item.market_analytics?.price_difference_percent;
    marketBadge.textContent = marketPositionLabel(
      item.market_analytics,
      { compact: true },
    );
    marketBadge.classList.toggle(
      "is-good",
      marketDifference != null && marketDifference < -2,
    );
    marketBadge.classList.toggle(
      "is-high",
      marketDifference != null && marketDifference > 2,
    );
    card.querySelector(".car-market-position").textContent =
      item.market_analytics?.average_price == null
        ? "Средняя цена пока неизвестна"
        : `Средняя цена ${formatMoney(item.market_analytics.average_price)}`;
    card.querySelector(".car-market-sample").textContent = marketSampleLabel(
      item.market_analytics,
    );
    card.querySelector(".car-seen").textContent = `Обновлено ${formatDate(item.last_seen_at)}`;
    const link = card.querySelector(".car-link");
    link.href = item.url;
    link.addEventListener("click", (event) => event.stopPropagation());
    item.user_data ||= { favorite: false, note: "" };
    card.querySelector(".car-note-indicator").hidden = !item.user_data.note;
    const favoriteButton = card.querySelector(".car-favorite");
    const renderFavorite = () => {
      favoriteButton.textContent = item.user_data.favorite ? "★ В избранном" : "☆ Избранное";
      favoriteButton.classList.toggle("is-selected", item.user_data.favorite);
    };
    renderFavorite();
    favoriteButton.addEventListener("click", async (event) => {
      event.stopPropagation();
      favoriteButton.disabled = true;
      try {
        await saveListingUserData(item, { favorite: !item.user_data.favorite });
        renderFavorite();
      } catch (error) {
        alert(error.message);
      } finally {
        favoriteButton.disabled = false;
      }
    });
    const compareButton = card.querySelector(".car-compare");
    compareButton.dataset.listingKey = listingKey(item);
    compareButton.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleComparison(item);
    });
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
  renderComparisonSelection();
  state.pages = payload.pages;
  renderPagination();
}

function renderRefreshActivity(payload) {
  const panel = document.getElementById("refresh-activity");
  const container = document.getElementById("refresh-activity-items");
  const items = payload.items || [];
  latestRefreshActivity = payload;
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
  if (signature === refreshActivitySignature) {
    if (!items.length) setRefreshActivityOpen(false);
    return;
  }
  refreshActivitySignature = signature;
  clearTimeout(refreshActivityTimer);
  setRefreshActivityOpen(items.length > 0);
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
    if (!item.url) {
      button.classList.add("is-information");
      button.disabled = true;
    } else {
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
    }
    container.append(button);
  }
  if (!payload.running_count) {
    refreshActivityTimer = setTimeout(
      () => setRefreshActivityOpen(false),
      5000,
    );
  }
}

document.getElementById("refresh-activity-open").addEventListener(
  "click",
  () => {
    clearTimeout(refreshActivityTimer);
    setRefreshActivityOpen(!refreshActivityOpen);
  },
);
document.getElementById("refresh-activity-close").addEventListener(
  "click",
  () => {
    clearTimeout(refreshActivityTimer);
    setRefreshActivityOpen(false);
  },
);

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
    item.published_at
      ? `${item.published_at_inferred ? "Добавлено в базу" : "Опубликовано"} ${displayPublished(item.published_at)}`
      : "",
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
  renderPriceChanges(payload.price_changes || []);
  renderSoldSummary(payload.sold_summary || {});
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

function sourceLabel(source) {
  return source === "auto_ru" ? "Auto.ru" : source === "drom" ? "Drom" : "Avito";
}

function renderPriceChanges(items) {
  const root = document.getElementById("market-price-changes");
  root.replaceChildren();
  if (!items.length) {
    root.append(emptyNote("Изменений цен пока не зафиксировано."));
    return;
  }
  for (const item of items.slice(0, 12)) {
    const row = document.createElement("a");
    row.className = `price-change ${item.delta < 0 ? "is-down" : "is-up"}`;
    row.href = item.url;
    row.target = "_blank";
    row.rel = "noopener noreferrer";
    const info = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = item.title;
    const meta = document.createElement("small");
    meta.textContent = `${sourceLabel(item.source)} · ${formatDate(item.observed_at)}`;
    info.append(title, meta);
    const prices = document.createElement("span");
    prices.className = "price-change-values";
    const delta = `${item.delta > 0 ? "+" : ""}${formatMoney(item.delta)}`;
    prices.innerHTML = `<small>${formatMoney(item.previous)} → ${formatMoney(item.current)}</small><strong>${delta}${item.delta_percent == null ? "" : ` (${item.delta_percent > 0 ? "+" : ""}${item.delta_percent}%)`}</strong>`;
    row.append(info, prices);
    root.append(row);
  }
}

function renderSoldSummary(summary) {
  const root = document.getElementById("market-sold-summary");
  root.replaceChildren();
  const values = [
    ["Завершено", money.format(summary.count || 0)],
    ["Медианная цена", summary.median_price == null ? "—" : formatMoney(summary.median_price)],
    ["Средняя цена", summary.average_price == null ? "—" : formatMoney(summary.average_price)],
    ["Медиана экспозиции", summary.median_days_on_market == null ? "—" : `${summary.median_days_on_market} дн.`],
  ];
  for (const [label, value] of values) {
    const item = document.createElement("div");
    const caption = document.createElement("span");
    caption.textContent = label;
    const strong = document.createElement("strong");
    strong.textContent = value;
    item.append(caption, strong);
    root.append(item);
  }
}

function renderSold(payload) {
  const { summary, items } = payload;
  setText("hero-count", money.format(summary.count));
  setText("sold-count", `${money.format(summary.count)} шт.`);
  const kpis = document.getElementById("sold-kpis");
  kpis.replaceChildren();
  const values = [
    ["Продано / снято", summary.count, "Завершённые объявления"],
    ["Медианная цена", summary.median_price == null ? "—" : formatMoney(summary.median_price), "Последняя или уточнённая цена"],
    ["Средняя цена", summary.average_price == null ? "—" : formatMoney(summary.average_price), "По архиву продаж"],
    ["Срок экспозиции", summary.median_days_on_market == null ? "—" : `${summary.median_days_on_market} дн.`, "Медиана по выборке"],
  ];
  for (const [label, value, note] of values) {
    const card = document.createElement("article");
    card.className = "market-kpi";
    const span = document.createElement("span"); span.textContent = label;
    const strong = document.createElement("strong"); strong.textContent = value;
    const small = document.createElement("small"); small.textContent = note;
    card.append(span, strong, small);
    kpis.append(card);
  }
  const root = document.getElementById("sold-list");
  root.replaceChildren();
  document.getElementById("sold-empty").hidden = items.length > 0;
  for (const item of items) {
    const row = document.createElement("article");
    row.className = "sold-row";
    const info = document.createElement("div");
    const title = document.createElement("a");
    title.href = item.url; title.target = "_blank"; title.rel = "noopener noreferrer";
    title.textContent = item.title;
    const meta = document.createElement("small");
    meta.textContent = [sourceLabel(item.source), item.location, item.days_on_market == null ? null : `${item.days_on_market} дн. в продаже`, item.sold_at ? `завершено ${formatDate(item.sold_at)}` : null].filter(Boolean).join(" · ");
    info.append(title, meta);
    const price = document.createElement("div");
    price.className = "sold-price";
    const caption = document.createElement("small"); caption.textContent = "Цена продажи";
    const strong = document.createElement("strong"); strong.textContent = formatMoney(item.sold_price);
    const edit = document.createElement("button"); edit.type = "button"; edit.className = "ghost-button"; edit.textContent = "Уточнить";
    edit.addEventListener("click", async () => {
      const raw = prompt("Фактическая цена продажи, ₽", item.sold_price || item.price || "");
      if (raw == null) return;
      const soldPrice = Number(raw.replace(/\s/g, ""));
      if (!Number.isInteger(soldPrice) || soldPrice < 0) return alert("Введите корректную цену");
      const response = await fetch(`/api/listings/${encodeURIComponent(item.source)}/${encodeURIComponent(item.external_id)}/sale`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ sold_price: soldPrice }) });
      const result = await response.json();
      if (!response.ok) return alert(result.error || "Не удалось сохранить цену");
      state.marketSignature = "";
      refresh().catch(showError);
    });
    price.append(caption, strong, edit);
    row.append(info, price);
    root.append(row);
  }
  renderSectionPagination("sold-pagination", payload, (page) => {
    state.soldPage = page;
    refresh().catch(showError);
    document.getElementById("sold-view").scrollIntoView({ behavior: "smooth" });
  });
}

document.getElementById("analysis-open-sample").addEventListener(
  "click",
  () => openMarketSample("Текущая выборка"),
);
document.getElementById("analysis-export").addEventListener("click", () => {
  window.location.assign(`/api/export-analysis?${params(false)}`);
});

function renderSpareParts(payload) {
  const { items = [], vehicles = [], sources = [], summary = {}, facets = {} } = payload;
  setText("spare-parts-count", money.format(summary.offers_count || 0));
  setText("spare-parts-priced", money.format(summary.priced_count || 0));
  setText("spare-parts-models", money.format(summary.models_count || 0));
  const sourceList = document.getElementById("spare-parts-sources");
  sourceList.replaceChildren(...sources.map((url) => new Option(url, url)));
  const vehicleSelect = document.getElementById("spare-parts-vehicle");
  const selected = vehicleSelect.value;
  vehicleSelect.replaceChildren(new Option("Все модели", ""));
  for (const vehicle of vehicles) {
    vehicleSelect.append(new Option(
      `${vehicle.brand} ${vehicle.model} · ${money.format(vehicle.offers_count)}`,
      `${vehicle.brand}|${vehicle.model}`,
    ));
  }
  vehicleSelect.value = [...vehicleSelect.options].some((option) => option.value === selected) ? selected : "";

  const generationSelect = document.getElementById("spare-parts-generation");
  const selectedGeneration = generationSelect.value;
  generationSelect.replaceChildren(new Option("Все", ""));
  for (const entry of facets.generations || []) {
    generationSelect.append(new Option(`${entry.value} · ${money.format(entry.count)}`, entry.value));
  }
  generationSelect.value = [...generationSelect.options].some((option) => option.value === selectedGeneration)
    ? selectedGeneration : "";

  const tabs = document.getElementById("spare-parts-tabs");
  tabs.replaceChildren();
  const categoryItems = [
    { value: "", count: vehicles.reduce((total, vehicle) => total + vehicle.offers_count, 0), label: "Все" },
    ...(facets.categories || []).map((entry) => ({ ...entry, label: entry.value })),
  ];
  for (const entry of categoryItems) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `spare-parts-tab${state.sparePartsCategory === entry.value ? " is-active" : ""}`;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-selected", state.sparePartsCategory === entry.value ? "true" : "false");
    button.append(document.createTextNode(entry.label));
    const count = document.createElement("small");
    count.textContent = money.format(entry.count || 0);
    button.append(count);
    button.addEventListener("click", () => {
      state.sparePartsCategory = entry.value;
      state.sparePartsPage = 1;
      state.sparePartsSignature = "";
      refresh().catch(showError);
    });
    tabs.append(button);
  }

  setText(
    "spare-parts-result-caption",
    `Найдено ${money.format(summary.offers_count || 0)} · страница ${payload.page || 1} из ${payload.pages || 1}`,
  );
  const pagination = document.getElementById("spare-parts-pagination");
  pagination.replaceChildren();
  const totalPages = payload.pages || 1;
  const currentPage = payload.page || 1;
  const pageNumbers = [...new Set([
    1,
    Math.max(1, currentPage - 1),
    currentPage,
    Math.min(totalPages, currentPage + 1),
    totalPages,
  ])].sort((a, b) => a - b);
  for (const pageNumber of pageNumbers) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = pageNumber;
    button.className = pageNumber === currentPage ? "is-active" : "";
    button.addEventListener("click", () => {
      state.sparePartsPage = pageNumber;
      state.sparePartsSignature = "";
      refresh().catch(showError);
    });
    pagination.append(button);
  }
  const root = document.getElementById("spare-parts-list");
  root.replaceChildren();
  document.getElementById("spare-parts-empty").hidden = items.length > 0;
  for (const part of items) {
    const article = document.createElement("article");
    article.className = "spare-part-card";
    const media = document.createElement("div");
    media.className = "spare-part-media";
    if (part.image_url) {
      const image = document.createElement("img");
      image.src = part.image_url;
      image.alt = part.name;
      image.loading = "lazy";
      media.append(image);
    } else {
      media.textContent = "Нет фото";
    }
    const content = document.createElement("div");
    content.className = "spare-part-content";
    const meta = document.createElement("small");
    meta.textContent = [
      `${part.brand} ${part.model}`,
      part.category,
      part.subcategory,
      part.generation && `${part.generation} поколение`,
      part.year_from && `${part.year_from}–${part.year_to || "…"}`,
      part.location,
    ].filter(Boolean).join(" · ");
    const title = document.createElement("h3");
    title.textContent = part.name;
    const description = document.createElement("p");
    description.textContent = part.description || "Описание продавца не указано";
    const footer = document.createElement("div");
    footer.className = "spare-part-footer";
    const price = document.createElement("strong");
    price.textContent = formatMoney(part.price);
    const link = document.createElement("a");
    link.href = part.source_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = part.seller ? `${part.seller} ↗` : "Открыть на Drom ↗";
    footer.append(price, link);
    content.append(meta, title, description, footer);
    article.append(media, content);
    root.append(article);
  }
}

function renderSectionPagination(elementId, payload, onChange) {
  const nav = document.getElementById(elementId);
  nav.replaceChildren();
  const pages = payload.pages || 0;
  const current = payload.page || 1;
  if (pages <= 1) return;
  const numbers = [...new Set([
    1,
    Math.max(1, current - 1),
    current,
    Math.min(pages, current + 1),
    pages,
  ])].sort((left, right) => left - right);
  for (const page of numbers) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = page;
    button.className = page === current ? "active" : "";
    button.addEventListener("click", () => onChange(page));
    nav.append(button);
  }
}

function refreshSparePartsFromFirstPage() {
  state.sparePartsPage = 1;
  state.sparePartsSignature = "";
  refresh().catch(showError);
}

for (const id of [
  "spare-parts-vehicle", "spare-parts-generation", "spare-parts-sort",
  "spare-parts-priced-only", "spare-parts-min-price", "spare-parts-max-price",
]) {
  document.getElementById(id).addEventListener("change", refreshSparePartsFromFirstPage);
}
let sparePartsSearchTimer;
document.getElementById("spare-parts-search").addEventListener("input", () => {
  clearTimeout(sparePartsSearchTimer);
  sparePartsSearchTimer = setTimeout(refreshSparePartsFromFirstPage, 250);
});
document.getElementById("spare-parts-reset").addEventListener("click", () => {
  state.sparePartsCategory = "";
  for (const id of [
    "spare-parts-search", "spare-parts-vehicle", "spare-parts-generation",
    "spare-parts-min-price", "spare-parts-max-price",
  ]) document.getElementById(id).value = "";
  document.getElementById("spare-parts-sort").value = "price_asc";
  document.getElementById("spare-parts-priced-only").checked = false;
  refreshSparePartsFromFirstPage();
});

document.getElementById("spare-parts-import").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  const status = document.getElementById("spare-parts-import-state");
  button.disabled = true;
  status.textContent = "Получаем список товаров…";
  try {
    const response = await fetch("/api/spare-parts/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: event.currentTarget.elements.url.value,
        pages: Number(event.currentTarget.elements.pages.value || 1),
        load_descriptions: false,
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      status.replaceChildren(document.createTextNode(payload.error || "Не удалось импортировать запчасти"));
      if (payload.verification_url) {
        const link = document.createElement("a");
        link.href = payload.verification_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = " Пройти проверку вручную ↗";
        status.append(link);
      }
      return;
    }
    status.textContent = [
      `Добавлено или обновлено: ${money.format(payload.imported)} · ${payload.brand} ${payload.model}${payload.pages_imported ? ` · страниц: ${payload.pages_imported}` : ""}`,
      payload.category,
      payload.subcategory,
    ].filter(Boolean).join(" · ");
    state.sparePartsSignature = "";
    await refresh();
  } catch (error) {
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
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
    if (state.view === "knowledge") {
      const query = new URLSearchParams({
        page: String(state.knowledgePage),
        page_size: "20",
      });
      const search = document.getElementById("knowledge-search").value.trim();
      if (search) query.set("q", search);
      const response = await fetch(`/api/vehicle-analyses?${query}`);
      if (!response.ok) throw new Error("Не удалось загрузить базу слабых мест");
      const payload = await response.json();
      const signature = JSON.stringify(payload);
      if (signature !== state.knowledgeSignature) {
        state.knowledgeSignature = signature;
        renderKnowledge(payload);
      }
      return;
    }
    if (state.view === "spare-parts") {
      const selected = document.getElementById("spare-parts-vehicle").value;
      const query = new URLSearchParams();
      if (selected) {
        const [brand, model] = selected.split("|");
        query.set("brand", brand);
        query.set("model", model);
      }
      const filterValues = {
        search: document.getElementById("spare-parts-search").value.trim(),
        generation: document.getElementById("spare-parts-generation").value,
        min_price: document.getElementById("spare-parts-min-price").value,
        max_price: document.getElementById("spare-parts-max-price").value,
        sort: document.getElementById("spare-parts-sort").value,
      };
      for (const [key, value] of Object.entries(filterValues)) {
        if (value) query.set(key, value);
      }
      if (state.sparePartsCategory) query.set("category", state.sparePartsCategory);
      if (document.getElementById("spare-parts-priced-only").checked) query.set("priced", "1");
      query.set("page", state.sparePartsPage);
      query.set("limit", "60");
      const response = await fetch(`/api/spare-parts?${query}`);
      if (!response.ok) throw new Error("Не удалось загрузить базу запчастей");
      const payload = await response.json();
      const signature = JSON.stringify(payload);
      if (signature !== state.sparePartsSignature) {
        state.sparePartsSignature = signature;
        renderSpareParts(payload);
      }
      return;
    }
    if (state.view === "sold") {
      const query = params(false);
      query.set("page", state.soldPage);
      query.set("page_size", "30");
      const response = await fetch(`/api/sold?${query}`);
      if (!response.ok) throw new Error("Не удалось загрузить архив продаж");
      renderSold(await response.json());
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
    reportDisplayedListings(listings.items);
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
  state.soldPage = 1;
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
  if (state.view === "knowledge") {
    const root = document.getElementById("knowledge-list");
    root.replaceChildren(emptyNote(`Ошибка загрузки базы: ${error.message}`));
    return;
  }
  if (state.view === "spare-parts") {
    const root = document.getElementById("spare-parts-list");
    root.replaceChildren(emptyNote(`Ошибка загрузки запчастей: ${error.message}`));
    return;
  }
  if (state.view === "sold") {
    const root = document.getElementById("sold-list");
    root.replaceChildren(emptyNote(`Ошибка загрузки архива: ${error.message}`));
    return;
  }
  const empty = document.getElementById("empty");
  empty.hidden = false;
  empty.querySelector("strong").textContent = "Ошибка загрузки";
  empty.querySelector("p").textContent = error.message;
}

function switchView(view, shouldRefresh = true) {
  if (!["catalog", "analytics", "knowledge", "spare-parts", "sold", "garage"].includes(view) || state.view === view) return;
  state.view = view;
  if (view !== "catalog") reportDisplayedListings([], true);
  state.page = 1;
  if (view === "knowledge") state.knowledgePage = 1;
  if (view === "sold") state.soldPage = 1;
  document.getElementById("catalog-view").hidden = view !== "catalog";
  document.getElementById("analytics-view").hidden = view !== "analytics";
  document.getElementById("knowledge-view").hidden = view !== "knowledge";
  document.getElementById("spare-parts-view").hidden = view !== "spare-parts";
  document.getElementById("sold-view").hidden = view !== "sold";
  document.getElementById("garage-view").hidden = view !== "garage";
  document.getElementById("listing-filters").hidden = ["garage", "knowledge", "spare-parts"].includes(view);
  for (const button of document.querySelectorAll(".nav-button")) {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-current", active ? "page" : "false");
  }
  const analytics = view === "analytics";
  const knowledge = view === "knowledge";
  const spareParts = view === "spare-parts";
  const sold = view === "sold";
  const garage = view === "garage";
  setText("view-title", garage ? "Гараж" : sold ? "Проданные" : spareParts ? "Автозапчасти" : knowledge ? "Слабые места" : analytics ? "Аналитика рынка" : "Объявления");
  setText(
    "view-subtitle",
    garage
      ? "Автомобили в собственности, бортжурнал, обслуживание, расходы и будущие запчасти."
      : sold
      ? "Архив завершённых объявлений, цены продажи и срок нахождения на рынке."
      : knowledge
      ? "Общая база типовых неисправностей, полученных из сохранённых анализов ChatGPT."
      : spareParts
      ? "Реальные предложения запчастей с Drom, связанные с марками, моделями и поколениями."
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

document.getElementById("knowledge-search").addEventListener(
  "input",
  () => {
    state.knowledgePage = 1;
    state.knowledgeSignature = "";
    clearTimeout(timer);
    timer = setTimeout(() => refresh().catch(showError), 250);
  },
);

elements.brand.addEventListener("input", updateModelDatalist);

for (const element of Object.values(elements)) {
  element.addEventListener(element.tagName === "INPUT" ? "input" : "change", scheduleRefresh);
}

document.getElementById("reset").addEventListener("click", () => {
  setActiveSearchProfile(null);
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
  updateModelDatalist();
  scheduleRefresh();
});

fetch("/api/meta")
  .then((response) => response.json())
  .then(({ locations, brands, models, models_by_brand: groupedModels, sources, attribute_options: attributeOptions }) => {
    modelsByBrand = groupedModels || {};
    allModelOptions = models || [];
    fillDatalist("location-options", locations || []);
    fillDatalist("brand-options", brands || []);
    fillDatalist("year-options", attributeOptions?.year || []);
    for (const value of sources || []) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = sourceLabel(value);
      elements.source.append(option);
    }
    restoreControlPreferences();
    updateModelDatalist();
    setupMultiFilters(attributeOptions);
    if (["analytics", "knowledge", "sold", "garage"].includes(savedPreferences?.view)) {
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
  if (document.hidden) {
    reportDisplayedListings([], true);
  } else {
    pollActivity();
    refresh().catch(showError);
    scheduleAutoRefresh();
  }
});

const detailsDialog = document.getElementById("details-dialog");
const imageLightbox = document.getElementById("image-lightbox");
let carouselImages = [];
let carouselIndex = 0;
let carouselLoading = false;
let carouselTotal = 0;
let detailLoadToken = 0;
let currentDetailItem = null;
renderComparisonSelection();

function renderFullscreenCarousel() {
  if (!imageLightbox.open || !carouselImages.length) return;
  const image = document.getElementById("lightbox-image");
  image.src = carouselImages[carouselIndex];
  image.alt = `Фото ${carouselIndex + 1}`;
  setText(
    "lightbox-count",
    `${carouselIndex + 1} / ${carouselImages.length}`,
  );
}

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
    document.getElementById("carousel-fullscreen").hidden = true;
    return;
  }
  document.getElementById("carousel-fullscreen").hidden = false;
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
  renderFullscreenCarousel();
}

function openFullscreenCarousel() {
  if (!carouselImages.length) return;
  renderFullscreenCarousel();
  imageLightbox.showModal();
  renderFullscreenCarousel();
}

function showPreviousImage() {
  if (!carouselImages.length) return;
  carouselIndex = (
    carouselIndex - 1 + carouselImages.length
  ) % carouselImages.length;
  renderCarousel();
}

function showNextImage() {
  if (!carouselImages.length) return;
  carouselIndex = (carouselIndex + 1) % carouselImages.length;
  renderCarousel();
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

function renderDetailTrims(item) {
  const root = document.getElementById("detail-trims");
  root.replaceChildren();
  const trims = item.trim_options || [];
  if (!trims.length) {
    root.append(emptyNote("Подходящие комплектации пока не найдены в базе Drom."));
    return;
  }
  for (const trim of trims) {
    const row = document.createElement("article");
    row.className = `detail-trim${item.trim_exact ? "" : " is-suggested"}`;
    const title = document.createElement("strong");
    title.textContent = trim.name;
    const details = document.createElement("small");
    details.textContent = Object.entries(trim.attributes || {})
      .filter(([name]) => name !== "Комплектация")
      .map(([name, value]) => `${name}: ${value}`)
      .join(" · ") || "Дополнительные характеристики не сохранены";
    row.append(title, details);
    if (trim.source_url) {
      const link = document.createElement("a");
      link.href = trim.source_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Источник на Drom ↗";
      row.append(link);
    }
    root.append(row);
  }
}

function appendCompatibleSpareParts(root, spareParts) {
  const compatible = spareParts?.compatible_offers || [];
  if (!compatible.length) return;
  const section = document.createElement("section");
  section.className = "analysis-compatible-parts";
  const header = document.createElement("header");
  const title = document.createElement("strong");
  title.textContent = "Совместимые запчасти из базы";
  const caption = document.createElement("small");
  caption.textContent = `${money.format(spareParts.offers_count || compatible.length)} предложений`;
  header.append(title, caption);
  const offers = document.createElement("div");
  offers.className = "analysis-part-offers";
  for (const offer of compatible.slice(0, 6)) {
    const link = document.createElement("a");
    link.className = "analysis-part-offer";
    link.href = offer.source_url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    if (offer.image_url) {
      const image = document.createElement("img");
      image.src = offer.image_url;
      image.alt = "";
      image.loading = "lazy";
      link.append(image);
    }
    const copy = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = offer.name;
    const meta = document.createElement("small");
    meta.textContent = [
      formatMoney(offer.price), offer.subcategory || offer.category,
      offer.seller || "Drom",
    ].filter(Boolean).join(" · ");
    copy.append(name, meta);
    link.append(copy);
    offers.append(link);
  }
  section.append(header, offers);
  root.append(section);
}

function renderDetailMarketAnalytics(analytics) {
  const root = document.getElementById("detail-market-metrics");
  root.replaceChildren();
  const position = document.getElementById("detail-market-position");
  const title = document.getElementById("detail-market-title");
  if (!analytics) {
    title.textContent = "Статистика поколения";
    position.textContent = "Недостаточно данных";
    root.append(emptyNote("Для этого поколения статистика пока не накоплена."));
    return;
  }
  const difference = analytics.price_difference_percent;
  title.textContent = analytics.generation_limited
    ? `${analytics.brand} ${analytics.model}, ${analytics.year_from}–${analytics.year_to} г.`
    : `${analytics.brand} ${analytics.model}, все годы`;
  position.textContent = marketPositionLabel(analytics);
  position.className = difference == null
    ? ""
    : difference <= 0
      ? "is-good"
      : "is-high";
  const metrics = [
    ["В поколении", money.format(analytics.listings_count)],
    ["Сейчас активно", money.format(analytics.active_count)],
    ["Продано / снято", money.format(analytics.sold_count)],
    ["Средняя цена", formatMoney(analytics.average_price)],
    ["Медианная цена", formatMoney(analytics.median_price)],
    ["Средняя цена продажи", analytics.average_sold_price == null ? "Нет данных" : formatMoney(analytics.average_sold_price)],
  ];
  for (const [label, value] of metrics) {
    const row = document.createElement("div");
    const caption = document.createElement("span");
    caption.textContent = label;
    const metric = document.createElement("strong");
    metric.textContent = value;
    row.append(caption, metric);
    root.append(row);
  }
}

function renderDetailUserData(item) {
  item.user_data ||= { favorite: false, note: "" };
  const favorite = document.getElementById("detail-favorite");
  favorite.textContent = item.user_data.favorite ? "★ В избранном" : "☆ Добавить в избранное";
  favorite.classList.toggle("is-selected", item.user_data.favorite);
  const note = document.getElementById("detail-note");
  if (document.activeElement !== note) note.value = item.user_data.note || "";
  setText("detail-note-state", item.user_data.updated_at ? "Заметка сохранена" : "");
  renderComparisonSelection();
}

function renderVehicleAnalysis(vehicleAnalysis, listingAssessment = null, spareParts = currentDetailItem?.spare_parts) {
  const root = document.getElementById("vehicle-analysis-result");
  const stateLabel = document.getElementById("vehicle-analysis-state");
  root.replaceChildren();
  const analysis = vehicleAnalysis?.data;
  const assessment = listingAssessment?.data;
  appendCompatibleSpareParts(root, spareParts);
  if (!analysis) {
    stateLabel.textContent = "Нет сохранённого анализа";
    root.append(emptyNote("Скопируйте промт, получите JSON-ответ и сохраните его здесь."));
    return;
  }
  const reused = vehicleAnalysis.match_kind === "generation"
    ? "Анализ того же поколения"
    : vehicleAnalysis.match_kind === "nearby_year"
      ? `Анализ близкого года${vehicleAnalysis.year ? ` (${vehicleAnalysis.year})` : ""}`
      : "";
  stateLabel.textContent = [
    reused,
    vehicleAnalysis.updated_at && `обновлено ${formatDate(vehicleAnalysis.updated_at)}`,
  ].filter(Boolean).join(" · ") || "Сохранено";
  if (analysis.summary) {
    const summary = document.createElement("div");
    summary.className = "analysis-summary";
    summary.textContent = analysis.summary;
    root.append(summary);
  }
  const excludedIds = new Set(assessment?.excluded_weak_point_ids || []);
  const relevantIds = new Set(assessment?.relevant_weak_point_ids || []);
  const weakPoints = (analysis.weak_points || []).filter((point) => {
    if (point.id && excludedIds.has(point.id)) return false;
    return !relevantIds.size || !point.id || relevantIds.has(point.id);
  });
  if (assessment?.confirmed_maintenance?.length) {
    const maintenance = document.createElement("div");
    maintenance.className = "analysis-summary";
    const heading = document.createElement("strong");
    heading.textContent = "Учтено из описания объявления";
    const list = document.createElement("ul");
    assessment.confirmed_maintenance.forEach((entry) => {
      const item = document.createElement("li");
      item.textContent = typeof entry === "string"
        ? entry
        : [entry.item, entry.evidence].filter(Boolean).join(" — ");
      list.append(item);
    });
    maintenance.append(heading, list);
    root.append(maintenance);
  }
  for (const point of weakPoints) {
    const row = document.createElement("div");
    row.className = "analysis-weak-point";
    const title = document.createElement("strong");
    title.textContent = [point.system, point.issue].filter(Boolean).join(" — ") || "Слабое место";
    const details = document.createElement("small");
    const cost = point.parts_cost_min == null
      ? ""
      : `Запчасти: ${formatMoney(point.parts_cost_min)}–${formatMoney(point.parts_cost_max ?? point.parts_cost_min)}`;
    details.textContent = [
      point.symptoms && `Признаки: ${point.symptoms}`,
      point.check && `Проверка: ${point.check}`,
      cost,
    ].filter(Boolean).join(" · ");
    row.append(title, details);
    root.append(row);
    const marketMatch = (spareParts?.matches || []).find(
      (match) => point.id && match.weak_point_id === point.id,
    );
    if (marketMatch?.offers?.length) {
      const offers = document.createElement("div");
      offers.className = "analysis-part-offers";
      for (const offer of marketMatch.offers) {
        const link = document.createElement("a");
        const selectedOfferIds = marketMatch.selected_offer_ids || [marketMatch.selected_offer_id];
        link.className = `analysis-part-offer${selectedOfferIds.includes(offer.id) ? " is-selected" : ""}`;
        link.href = offer.source_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        if (offer.image_url) {
          const image = document.createElement("img");
          image.src = offer.image_url;
          image.alt = "";
          image.loading = "lazy";
          link.append(image);
        }
        const copy = document.createElement("span");
        const name = document.createElement("strong");
        name.textContent = offer.name;
        const seller = document.createElement("small");
        seller.textContent = [
          offer.matched_repair_part && `Для замены: ${offer.matched_repair_part}`,
          formatMoney(offer.price), offer.seller || "Drom",
        ].filter(Boolean).join(" · ");
        copy.append(name, seller);
        link.append(copy);
        offers.append(link);
      }
      root.append(offers);
    }
  }
  if (assessment?.remaining_investments?.length) {
    const investments = document.createElement("div");
    investments.className = "analysis-summary";
    const heading = document.createElement("strong");
    heading.textContent = "Вложения по этому объявлению";
    const list = document.createElement("ul");
    assessment.remaining_investments.forEach((entry) => {
      const item = document.createElement("li");
      const cost = entry.parts_cost_min == null
        ? ""
        : `${formatMoney(entry.parts_cost_min)}–${formatMoney(entry.parts_cost_max ?? entry.parts_cost_min)}`;
      item.textContent = [
        entry.name,
        cost && `запчасти ${cost}`,
        entry.reason,
      ].filter(Boolean).join(" · ");
      list.append(item);
    });
    investments.append(heading, list);
    root.append(investments);
  }
  const budget = assessment?.parts_investment_total;
  if (budget) {
    const row = document.createElement("div");
    row.className = "analysis-budget";
    row.textContent = `Ожидаемые вложения в запчасти для этого автомобиля: ${formatMoney(budget.min)}–${formatMoney(budget.max)}${budget.notes ? ` · ${budget.notes}` : ""}`;
    root.append(row);
  }
  if (spareParts?.costs && (spareParts.matches?.length || spareParts.costs.investment_max)) {
    const costs = spareParts.costs;
    const row = document.createElement("div");
    row.className = "analysis-cost-total";
    const heading = document.createElement("strong");
    heading.textContent = "Полная стоимость обслуживания и вложений";
    const lines = document.createElement("dl");
    for (const [label, value] of [
      ["Запчасти из базы", formatMoney(costs.parts)],
      ["Работы", `${formatMoney(costs.labor_min)}–${formatMoney(costs.labor_max)}`],
      ["Обслуживание", `${formatMoney(costs.service_min)}–${formatMoney(costs.service_max)}`],
      ["Все ожидаемые вложения", `${formatMoney(costs.investment_min)}–${formatMoney(costs.investment_max)}`],
      ["Автомобиль + вложения", `${formatMoney(costs.total_entry_min)}–${formatMoney(costs.total_entry_max)}`],
    ]) {
      const term = document.createElement("div");
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = value;
      term.append(dt, dd);
      lines.append(term);
    }
    row.append(heading, lines);
    root.append(row);
  }
  for (const [title, values] of [
    ["Что проверить при покупке", analysis.purchase_checklist],
    ["Типовые запчасти", (analysis.parts || []).map((part) => [
      part.name,
      part.price_min == null ? "" : `${formatMoney(part.price_min)}–${formatMoney(part.price_max ?? part.price_min)}`,
    ].filter(Boolean).join(": "))],
  ]) {
    if (!values?.length) continue;
    const row = document.createElement("div");
    row.className = "analysis-summary";
    const heading = document.createElement("strong");
    heading.textContent = title;
    const list = document.createElement("ul");
    values.forEach((value) => {
      const item = document.createElement("li");
      item.textContent = value;
      list.append(item);
    });
    row.append(heading, list);
    root.append(row);
  }
}

function renderAnalysisTrimSelector(item) {
  const select = document.getElementById("vehicle-analysis-trim");
  const names = [...new Set(
    (item.trim_options || []).map((trim) => trim.name).filter(Boolean),
  )];
  select.replaceChildren();
  if (!names.length) {
    const option = document.createElement("option");
    option.value = "__model__";
    option.textContent = "Общий анализ модели";
    select.append(option);
    select.disabled = true;
    return;
  }
  for (const name of names) {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    select.append(option);
  }
  select.value = names.includes(item.analysis_trim_name)
    ? item.analysis_trim_name
    : names.length === 1
      ? names[0]
      : "";
  if (!select.value && names.length > 1) {
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Выберите комплектацию";
    placeholder.selected = true;
    select.prepend(placeholder);
  }
  select.disabled = Boolean(item.trim_exact);
  select.onchange = () => {
    const selectedAnalysis = select.value === "__model__" ? "" : select.value;
    if (selectedAnalysis !== (item.analysis_trim_name || "")) {
      renderVehicleAnalysis(null, item.listing_assessment);
    } else {
      renderVehicleAnalysis(item.vehicle_analysis, item.listing_assessment);
    }
  };
}

function renderDetailSparePartsState(spareParts, importPayload = null) {
  const root = document.getElementById("detail-spare-parts-state");
  root.replaceChildren();
  const summary = document.createElement("p");
  summary.textContent = importPayload?.verification_required
    ? importPayload.error || "Drom запросил ручную проверку «не робот»."
    : importPayload
    ? [
        `Собрано ${money.format(importPayload.imported || 0)} предложений${importPayload.generation ? ` · ${importPayload.generation} поколение` : ""}.`,
        importPayload.warning,
        importPayload.failed_categories
          ? `Не ответили тематические выдачи: ${money.format(importPayload.failed_categories)}.`
          : null,
      ].filter(Boolean).join(" ")
    : spareParts?.offers_count
      ? `В базе найдено ${money.format(spareParts.offers_count)} совместимых предложений.`
      : "Совместимые предложения ещё не загружены.";
  root.append(summary);
  const links = importPayload?.verification_url
    ? [{ category: "Пройти проверку вручную", url: importPayload.verification_url }]
    : importPayload?.links || (
    spareParts?.search_url
      ? [{ category: "Открыть общий список Drom", url: spareParts.search_url }]
      : []
  );
  if (links.length) {
    const linkRoot = document.createElement("div");
    linkRoot.className = "detail-spare-parts-links";
    for (const entry of links) {
      const link = document.createElement("a");
      link.href = entry.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = `${entry.category} ↗`;
      linkRoot.append(link);
    }
    root.append(linkRoot);
  }
}

function renderKnowledge(payload = state.knowledgePayload) {
  if (!payload) return;
  state.knowledgePayload = payload;
  const root = document.getElementById("knowledge-list");
  const empty = document.getElementById("knowledge-empty");
  const queryWords = document.getElementById("knowledge-search").value
    .trim().toLocaleLowerCase("ru-RU").split(/\s+/).filter(Boolean);
  const items = (payload.items || []).filter((item) => {
    if (!queryWords.length) return true;
    const points = item.analysis?.weak_points || [];
    const haystack = [
      item.brand,
      item.model,
      item.trim_name,
      item.analysis?.summary,
      ...points.flatMap((point) => [
        point.fault_type,
        point.system,
        point.issue,
        point.symptoms,
        point.check,
      ]),
    ].filter(Boolean).join(" ").toLocaleLowerCase("ru-RU");
    return queryWords.every((word) => haystack.includes(word));
  });
  root.replaceChildren();
  empty.hidden = items.length > 0;
  setText("knowledge-groups", money.format(payload.summary?.vehicle_groups || 0));
  setText("knowledge-points", money.format(payload.summary?.weak_points || 0));
  setText("hero-count", money.format(payload.summary?.vehicle_groups || 0));
  for (const item of items) {
    const card = document.createElement("article");
    card.className = "knowledge-card";
    const header = document.createElement("header");
    const title = document.createElement("h3");
    title.textContent = [item.brand, item.model, item.trim_name].filter(Boolean).join(" · ");
    const meta = document.createElement("small");
    meta.textContent = [
      item.updated_at && `Обновлено ${formatDate(item.updated_at)}`,
    ].filter(Boolean).join(" · ");
    header.append(title, meta);
    card.append(header);
    if (item.analysis.summary) {
      const summary = document.createElement("p");
      summary.className = "knowledge-card-summary";
      summary.textContent = item.analysis.summary;
      card.append(summary);
    }
    const points = document.createElement("div");
    points.className = "knowledge-points";
    for (const point of item.analysis.weak_points || []) {
      const row = document.createElement("article");
      row.className = "knowledge-point";
      const name = document.createElement("strong");
      name.textContent = [point.system, point.issue].filter(Boolean).join(" — ") || "Типовая неисправность";
      const details = document.createElement("p");
      const cost = point.parts_cost_min == null
        ? ""
        : `Запчасти: ${formatMoney(point.parts_cost_min)}–${formatMoney(point.parts_cost_max ?? point.parts_cost_min)}`;
      details.textContent = [
        point.fault_type && `Тип: ${point.fault_type}`,
        point.symptoms && `Признаки: ${point.symptoms}`,
        point.check && `Проверка: ${point.check}`,
        cost,
      ].filter(Boolean).join(" · ");
      row.append(name, details);
      points.append(row);
    }
    if (!points.childElementCount) points.append(emptyNote("Список неисправностей не заполнен."));
    card.append(points);
    root.append(card);
  }
  renderSectionPagination("knowledge-pagination", payload, (page) => {
    state.knowledgePage = page;
    state.knowledgeSignature = "";
    refresh().catch(showError);
    document.getElementById("knowledge-view").scrollIntoView({ behavior: "smooth" });
  });
}

function vehicleAnalysisPrompt(item) {
  const selectedTrim = document.getElementById("vehicle-analysis-trim").value;
  const trimName = selectedTrim === "__model__" ? "" : selectedTrim;
  const vehicle = [
    item.brand,
    item.model,
    trimName,
  ].filter(Boolean).join(" ");
  const description = item.description?.trim() || "Описание продавца отсутствует.";
  const scope = trimName
    ? `модель и комплектацию ${vehicle}`
    : `модель ${vehicle} без уточнения комплектации`;
  const sharedScope = trimName
    ? "этой модели и комплектации"
    : "этой модели в целом; особенности отдельных комплектаций явно помечай в тексте";
  return `Проанализируй ${scope}. Сформируй общее описание без упоминания конкретного года выпуска. Учитывай российский рынок и естественное старение автомобиля.

Описание конкретного объявления:
"""${description}"""

Сформируй два независимых блока:
1. model_analysis — полный справочник всех типовых слабых мест ${sharedScope}. Не удаляй из него неисправности из-за заявлений продавца: этот блок будет переиспользоваться для подходящих автомобилей.
2. listing_assessment — оценка только этого объявления на основе текста продавца. Если в описании прямо сказано, что узел заменён или обслужен, добавь его ID в excluded_weak_point_ids, чтобы проблема не показывалась в этой карточке. Не считай расплывчатые фразы вроде «всё обслужено» подтверждением конкретного ТО.

Указывай стоимость запчастей и работ раздельно в рублях. Для каждой неисправности обязательно заполняй repair_parts — связанный список конкретных компонентов, которые действительно устраняют именно эту неисправность. Не используй в качестве поисковых терминов общие слова «система», «охлаждение», «двигатель», «ремонт» или название симптома.

Для каждой детали укажи:
- name — понятное название детали;
- part_type — короткий стабильный тип латиницей (например water_pump, thermostat, coolant_hose);
- search_terms — точные названия и реальные синонимы только этой детали;
- exclude_terms — названия соседних, но неподходящих деталей, которые нельзя предлагать.

Пример для течи системы охлаждения: водяная помпа должна находиться по «помпа», «водяная помпа», «водяной насос», но не по словам «система охлаждения»; сервопривод заслонок печки, насос омывателя и топливный насос должны быть исключены. В replacement_parts продублируй только значения name для обратной совместимости. Не выдумывай точность: давай реалистичные диапазоны.

Верни только валидный JSON без Markdown по схеме:
{
  "model_analysis": {
    "summary": "общий вывод по модели",
    "weak_points": [
      {
        "id": "короткий_стабильный_id",
        "fault_type": "двигатель|трансмиссия|подвеска|электрика|кузов|охлаждение|тормоза|рулевое|салон|прочее",
        "system": "узел или система",
        "issue": "типовая проблема",
        "symptoms": "как проявляется",
        "check": "как проверить перед покупкой",
        "parts_cost_min": 0,
        "parts_cost_max": 0,
        "labor_cost_min": 0,
        "labor_cost_max": 0,
        "repair_parts": [
          {
            "name": "водяная помпа",
            "part_type": "water_pump",
            "search_terms": ["помпа", "водяная помпа", "водяной насос", "насос охлаждающей жидкости"],
            "exclude_terms": ["сервопривод заслонок", "насос печки", "насос омывателя", "топливный насос"]
          }
        ],
        "replacement_parts": ["водяная помпа"],
        "priority": "high|medium|low"
      }
    ],
    "purchase_checklist": ["пункт проверки"],
    "parts": [
      {
        "name": "запчасть или комплект",
        "price_min": 0,
        "price_max": 0,
        "replacement_interval": "когда обычно требуется"
      }
    ],
    "sources": [{"title": "источник", "url": "https://..."}]
  },
  "listing_assessment": {
    "description_used": true,
    "confirmed_maintenance": [
      {"item": "обслуженный узел", "evidence": "точная фраза или факт из описания"}
    ],
    "excluded_weak_point_ids": ["id проблемы, уже устранённой по описанию"],
    "relevant_weak_point_ids": ["id оставшейся актуальной проблемы"],
    "remaining_investments": [
      {
        "weak_point_id": "id",
        "name": "необходимая запчасть",
        "reason": "почему актуально для этого объявления",
        "parts_cost_min": 0,
        "parts_cost_max": 0
      }
    ],
    "parts_investment_total": {
      "min": 0,
      "max": 0,
      "notes": "только детали, без стоимости работ"
    }
  }
}`;
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
      setText(
        "detail-published-note",
        payload.published_at_inferred ? "Дата добавления в локальную базу" : "",
      );
      renderDetailAttributes(payload.attributes);
      Object.assign(item, {
        attributes: payload.attributes,
        year: payload.year ?? item.year,
        trim_exact: payload.trim_exact,
        trim_options: payload.trim_options,
        analysis_trim_name: payload.analysis_trim_name,
        drive2_url: payload.drive2_url,
        vehicle_analysis: payload.vehicle_analysis,
        market_analytics: payload.market_analytics,
        user_data: payload.user_data,
        published_at: payload.published_at || item.published_at,
        published_at_inferred: payload.published_at_inferred,
      });
      renderDetailTrims(item);
      renderAnalysisTrimSelector(item);
      item.description = payload.description || item.description;
      item.listing_assessment = payload.listing_assessment;
      renderVehicleAnalysis(
        item.vehicle_analysis,
        item.listing_assessment,
      );
      renderDetailMarketAnalytics(item.market_analytics);
      renderDetailUserData(item);
      const drive2 = document.getElementById("detail-drive2");
      drive2.hidden = !item.drive2_url;
      if (item.drive2_url) drive2.href = item.drive2_url;
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
  currentDetailItem = item;
  setText("detail-brand", [item.brand, item.model].filter(Boolean).join(" · "));
  setText("detail-title", item.title);
  setText("detail-price", formatMoney(item.price));
  setText("detail-mileage", formatMileage(item.mileage_km));
  setText("detail-views", item.views_count == null ? "Не указаны" : money.format(item.views_count));
  setText("detail-published", displayPublished(item.published_at));
  setText(
    "detail-published-note",
    item.published_at_inferred ? "Дата добавления в локальную базу" : "",
  );
  setText("detail-location", item.location || "Не указан");
  setText("detail-description", item.description || "Описание пока не загружено.");
  setText("detail-load-status", "Догружаем данные объявления…");
  document.getElementById("detail-link").href = item.url;
  renderDetailAttributes(item.attributes);
  renderDetailTrims(item);
  renderAnalysisTrimSelector(item);
  renderVehicleAnalysis(item.vehicle_analysis, item.listing_assessment);
  renderDetailMarketAnalytics(item.market_analytics);
  renderDetailUserData(item);
  renderDetailSparePartsState(item.spare_parts);
  const drive2 = document.getElementById("detail-drive2");
  drive2.hidden = !item.drive2_url;
  if (item.drive2_url) drive2.href = item.drive2_url;
  carouselImages = [...new Set(item.images || [])];
  carouselIndex = 0;
  carouselTotal = 0;
  carouselLoading = true;
  renderCarousel();
  detailsDialog.showModal();
  return loadGallery(item, token);
}

function announceDromVerification(payload) {
  const message = payload.error
    || "Drom запросил проверку «не робот». Откройте ссылку в карточке и пройдите её вручную.";
  if ("Notification" in window && Notification.permission === "granted") {
    const notification = new Notification("Требуется проверка Drom", {
      body: message,
      tag: "drom-verification-required",
    });
    notification.onclick = () => {
      if (payload.verification_url) {
        window.open(payload.verification_url, "_blank", "noopener");
      }
    };
  }
  alert(`${message}\n\nСсылка для ручной проверки добавлена в карточку автомобиля.`);
}

async function importSparePartsForCurrentCar({ automatic = false } = {}) {
  if (!currentDetailItem) return;
  const item = currentDetailItem;
  const button = document.getElementById("detail-spare-parts-import");
  const root = document.getElementById("detail-spare-parts-state");
  button.disabled = true;
  root.replaceChildren();
  const status = document.createElement("p");
  status.textContent = automatic
    ? "Анализ сохранён. Автоматически ищем запчасти и расходники для слабых мест…"
    : "Определяем поколение и собираем тематические выдачи Drom…";
  root.append(status);
  try {
    const response = await fetch("/api/spare-parts/import-for-car", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source: item.source,
        external_id: item.external_id,
      }),
    });
    const payload = await response.json();
    if (payload.verification_required && automatic) {
      announceDromVerification(payload);
    }
    if (!response.ok && payload.verification_required) {
      if (currentDetailItem === item) {
        renderDetailSparePartsState(item.spare_parts, payload);
      }
      return;
    }
    if (!response.ok) throw new Error(payload.error || "Не удалось собрать запчасти");
    item.spare_parts = payload.spare_parts;
    if (currentDetailItem === item) {
      renderDetailSparePartsState(payload.spare_parts, payload);
      renderVehicleAnalysis(
        item.vehicle_analysis,
        item.listing_assessment,
        payload.spare_parts,
      );
    }
    state.listingsSignature = "";
    state.sparePartsSignature = "";
  } catch (error) {
    if (currentDetailItem === item) {
      root.replaceChildren();
      const message = document.createElement("p");
      message.className = "is-error";
      message.textContent = error.message;
      root.append(message);
    }
  } finally {
    button.disabled = false;
  }
}

document.getElementById("detail-spare-parts-import").addEventListener(
  "click",
  () => importSparePartsForCurrentCar(),
);

document.getElementById("detail-favorite").addEventListener("click", async () => {
  if (!currentDetailItem) return;
  const button = document.getElementById("detail-favorite");
  button.disabled = true;
  try {
    await saveListingUserData(currentDetailItem, {
      favorite: !currentDetailItem.user_data?.favorite,
    });
    renderDetailUserData(currentDetailItem);
    refresh().catch(showError);
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
});

document.getElementById("detail-compare").addEventListener("click", () => {
  if (currentDetailItem) toggleComparison(currentDetailItem);
});

document.getElementById("detail-note-save").addEventListener("click", async () => {
  if (!currentDetailItem) return;
  const button = document.getElementById("detail-note-save");
  button.disabled = true;
  setText("detail-note-state", "Сохраняем…");
  try {
    await saveListingUserData(currentDetailItem, {
      note: document.getElementById("detail-note").value,
    });
    setText("detail-note-state", "Заметка сохранена");
  } catch (error) {
    setText("detail-note-state", error.message);
  } finally {
    button.disabled = false;
  }
});

function comparisonValue(item, field) {
  if (field === "price") return formatMoney(item.price);
  if (field === "mileage_km") return formatMileage(item.mileage_km);
  if (field === "market_average") return formatMoney(item.market_analytics?.average_price);
  if (field === "market_position") {
    const value = item.market_analytics?.price_difference_percent;
    if (value == null) return "Нет данных";
    return value === 0 ? "На уровне средней" : `${Math.abs(value)}% ${value < 0 ? "ниже" : "выше"} средней`;
  }
  if (field === "market_volume") {
    const market = item.market_analytics;
    return market ? `${market.listings_count} всего · ${market.active_count} активных · ${market.sold_count} продано` : "Нет данных";
  }
  return item[field] ?? "Не указано";
}

function renderComparison(items) {
  const root = document.getElementById("comparison-content");
  root.replaceChildren();
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const heading = document.createElement("tr");
  const empty = document.createElement("th");
  heading.append(empty);
  for (const item of items) {
    const cell = document.createElement("th");
    if (item.thumbnail_url) {
      const image = document.createElement("img");
      image.src = item.thumbnail_url;
      image.alt = "";
      cell.append(image);
    }
    const title = document.createElement("a");
    title.href = item.url;
    title.target = "_blank";
    title.rel = "noopener noreferrer";
    title.textContent = item.title;
    cell.append(title);
    heading.append(cell);
  }
  head.append(heading);
  table.append(head);
  const body = document.createElement("tbody");
  const rows = [
    ["Цена", "price"],
    ["Пробег", "mileage_km"],
    ["Год", "year"],
    ["Город", "location"],
    ["Средняя цена поколения", "market_average"],
    ["Позиция цены", "market_position"],
    ["Объявления и продажи", "market_volume"],
  ];
  const attributeNames = [...new Set(items.flatMap((item) => Object.keys(item.attributes || {})))]
    .filter((name) => name !== "Год выпуска" && name !== "Комплектация");
  for (const [label, field] of rows) {
    const row = document.createElement("tr");
    const caption = document.createElement("th");
    caption.textContent = label;
    row.append(caption);
    for (const item of items) {
      const value = document.createElement("td");
      value.textContent = comparisonValue(item, field);
      row.append(value);
    }
    body.append(row);
  }
  for (const name of attributeNames) {
    const row = document.createElement("tr");
    const caption = document.createElement("th");
    caption.textContent = name;
    row.append(caption);
    for (const item of items) {
      const value = document.createElement("td");
      value.textContent = item.attributes?.[name] || "Не указано";
      row.append(value);
    }
    body.append(row);
  }
  table.append(body);
  root.append(table);
}

document.getElementById("comparison-clear").addEventListener("click", () => {
  comparisonSelection.clear();
  renderComparisonSelection();
});

document.getElementById("comparison-open").addEventListener("click", async () => {
  if (comparisonSelection.size !== 2) return;
  const button = document.getElementById("comparison-open");
  button.disabled = true;
  try {
    const query = new URLSearchParams();
    for (const item of comparisonSelection.values()) {
      query.append("source", item.source);
      query.append("external_id", item.external_id);
    }
    const response = await fetch(`/api/comparison?${query}`);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Не удалось сравнить автомобили");
    renderComparison(payload.items);
    document.getElementById("comparison-dialog").showModal();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = comparisonSelection.size !== 2;
  }
});

document.getElementById("vehicle-prompt-copy").addEventListener(
  "click",
  async () => {
    if (!currentDetailItem) return;
    const button = document.getElementById("vehicle-prompt-copy");
    const promptText = vehicleAnalysisPrompt(currentDetailItem);
    try {
      await navigator.clipboard.writeText(promptText);
      button.textContent = "Промт скопирован";
      setTimeout(() => { button.textContent = "Скопировать промт"; }, 1800);
    } catch {
      window.prompt("Скопируйте промт", promptText);
    }
  },
);

document.getElementById("vehicle-analysis-save").addEventListener(
  "click",
  async () => {
    if (!currentDetailItem) return;
    const textarea = document.getElementById("vehicle-analysis-json");
    const button = document.getElementById("vehicle-analysis-save");
    let analysis;
    try {
      analysis = JSON.parse(textarea.value);
    } catch {
      alert("Ответ должен быть валидным JSON без Markdown-обёртки.");
      return;
    }
    if (!analysis || Array.isArray(analysis) || typeof analysis !== "object") {
      alert("Ожидался JSON-объект анализа.");
      return;
    }
    const selectedTrim = document.getElementById("vehicle-analysis-trim").value;
    const trimName = selectedTrim === "__model__" ? "" : selectedTrim;
    button.disabled = true;
    try {
      const response = await fetch("/api/vehicle-analysis", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source: currentDetailItem.source,
          external_id: currentDetailItem.external_id,
          trim_name: trimName,
          analysis,
        }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Не удалось сохранить анализ");
      currentDetailItem.vehicle_analysis = payload.vehicle_analysis;
      currentDetailItem.listing_assessment = payload.listing_assessment;
      currentDetailItem.analysis_trim_name = payload.analysis_trim_name;
      renderAnalysisTrimSelector(currentDetailItem);
      renderVehicleAnalysis(
        payload.vehicle_analysis,
        payload.listing_assessment,
      );
      textarea.value = "";
      state.listingsSignature = "";
      state.knowledgeSignature = "";
      if (payload.auto_spare_parts) {
        await importSparePartsForCurrentCar({ automatic: true });
      }
    } catch (error) {
      alert(error.message);
    } finally {
      button.disabled = false;
    }
  },
);

document.getElementById("carousel-prev").addEventListener("click", () => {
  showPreviousImage();
});
document.getElementById("carousel-next").addEventListener("click", () => {
  showNextImage();
});
document.getElementById("carousel-fullscreen").addEventListener(
  "click",
  openFullscreenCarousel,
);
document.getElementById("detail-image").addEventListener(
  "click",
  openFullscreenCarousel,
);
document.getElementById("lightbox-prev").addEventListener(
  "click",
  showPreviousImage,
);
document.getElementById("lightbox-next").addEventListener(
  "click",
  showNextImage,
);
imageLightbox.addEventListener("keydown", (event) => {
  if (event.key === "ArrowLeft") showPreviousImage();
  if (event.key === "ArrowRight") showNextImage();
});

for (const dialog of document.querySelectorAll("dialog")) {
  dialog.querySelector(".dialog-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
}

const profilesDialog = document.getElementById("profiles-dialog");
const ALL_CARS_QUERY = "__all_cars__";
const profileForm = document.getElementById("profile-form");
let profilesRefreshTimer;

function setActiveSearchProfile(profileId, label = "") {
  const root = document.getElementById("active-profile-filter");
  state.activeProfileId = profileId == null ? null : Number(profileId);
  state.activeProfileLabel = state.activeProfileId == null ? "" : label;
  root.hidden = state.activeProfileId == null;
  root.querySelector("strong").textContent = state.activeProfileLabel;
  state.page = 1;
}

async function showSearchProfile(profileId, label) {
  setActiveSearchProfile(profileId, label);
  if (profilesDialog.open) profilesDialog.close();
  if (state.view !== "catalog") switchView("catalog", false);
  await refresh();
}

document.getElementById("active-profile-clear").addEventListener("click", () => {
  setActiveSearchProfile(null);
  refresh().catch(showError);
});

function updateAllCarsButton() {
  const source = profileForm.elements.source.value;
  const region = profileForm.elements.region;
  const regionName = region.options[region.selectedIndex]?.text || region.value;
  document.getElementById("profile-all-cars").textContent =
    `Все авто · ${sourceLabel(source)} · ${regionName}`;
}

profileForm.elements.source.addEventListener("change", updateAllCarsButton);
profileForm.elements.region.addEventListener("change", updateAllCarsButton);
updateAllCarsButton();
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
    const isAllCarsProfile = item.query === ALL_CARS_QUERY;
    strong.textContent = isAllCarsProfile
      ? "Все автомобили"
      : isUrlProfile
        ? "Поиск по сохранённой ссылке"
        : item.query;
    const meta = document.createElement("small");
    const sourceName = item.source === "auto_ru" ? "Auto.ru" : item.source === "drom" ? "Drom" : "Avito";
    const priceRange = [
      item.min_price == null ? null : `от ${formatMoney(item.min_price)}`,
      item.max_price == null ? null : `до ${formatMoney(item.max_price)}`,
    ].filter(Boolean).join(" ");
    meta.textContent = isUrlProfile
      ? [item.query, sourceName, priceRange].filter(Boolean).join(" · ")
      : [
          sourceName,
          item.region,
          item.radius == null ? "любой радиус" : `${item.radius} км`,
          priceRange,
        ].filter(Boolean).join(" · ");
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
    const show = document.createElement("button");
    show.textContent = state.activeProfileId === item.id ? "Показан" : "Показать";
    show.disabled = state.activeProfileId === item.id;
    show.addEventListener("click", () => {
      showSearchProfile(item.id, strong.textContent).catch(showError);
    });
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
      if (state.activeProfileId === item.id) {
        setActiveSearchProfile(null);
        refresh().catch(showError);
      }
      await loadProfiles();
    });
    actions.append(show, run, toggle, remove);
    row.append(title, interval, state, actions);
    root.append(row);
  }
}

profileForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  const query = String(form.get("query") || "").trim();
  try {
    const created = await profileRequest("/api/search-profiles", {
      method: "POST",
      body: JSON.stringify({
        source: form.get("source"),
        query,
        all_cars: false,
        region: form.get("region"),
        radius: form.get("radius") === "" ? null : Number(form.get("radius")),
        min_price: form.get("min_price") === "" ? null : Number(form.get("min_price")),
        max_price: form.get("max_price") === "" ? null : Number(form.get("max_price")),
        interval_minutes: Number(form.get("interval_minutes")),
      }),
    });
    formElement.reset();
    updateAllCarsButton();
    await showSearchProfile(created.id, query);
  } catch (error) {
    alert(error.message);
  }
});

document.getElementById("profile-all-cars").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  const form = new FormData(profileForm);
  button.disabled = true;
  try {
    await profileRequest("/api/search-profiles", {
      method: "POST",
      body: JSON.stringify({
        source: form.get("source"),
        all_cars: true,
        region: form.get("region"),
        radius: form.get("radius") === "" ? null : Number(form.get("radius")),
        min_price: form.get("min_price") === "" ? null : Number(form.get("min_price")),
        max_price: form.get("max_price") === "" ? null : Number(form.get("max_price")),
        interval_minutes: Number(form.get("interval_minutes")),
      }),
    });
    setActiveSearchProfile(null);
    if (profilesDialog.open) profilesDialog.close();
    if (state.view !== "catalog") switchView("catalog", false);
    await refresh();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
});
