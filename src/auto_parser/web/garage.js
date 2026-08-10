import { emptyMessage, formatMileage, formatMoney, setText } from "./ui.js";

export function createGarageController() {
  let currentId = null;
  let signature = "";

  async function request(url, options) {
    const response = await fetch(url, options);
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Ошибка гаража");
    return payload;
  }

  async function loadListingOptions() {
    const payload = await request("/api/parts/cars");
    const select = document.querySelector('#garage-add-form [name="listing"]');
    for (const car of payload.items) {
      select.append(new Option(
        `${car.title} · ${formatMoney(car.price)} · ${formatMileage(car.mileage_km)}`,
        `${car.source}|${car.external_id}`,
      ));
    }
    select.addEventListener("change", async () => {
      const option = payload.items.find(
        (car) => `${car.source}|${car.external_id}` === select.value,
      );
      if (!option) return;
      const form = document.getElementById("garage-add-form");
      form.elements.name.value = option.title || "";
      form.elements.brand.value = option.brand || "";
      form.elements.model.value = option.model || "";
      form.elements.mileage_km.value = option.mileage_km || "";
      form.elements.purchase_price.value = option.price || "";
      const query = new URLSearchParams({
        source: option.source,
        external_id: option.external_id,
        visibility: "all",
      });
      const listingPayload = await request(`/api/listings?${query}`);
      const attributes = listingPayload.items?.[0]?.attributes || {};
      form.elements.year.value = attributes["Год выпуска"] || "";
      form.elements.color.value = attributes["Цвет"] || "";
      form.elements.engine_type.value = attributes["Тип двигателя"] || "";
      form.elements.engine_volume.value = attributes["Объём двигателя"] || "";
      form.elements.power.value = String(attributes["Мощность"] || "").match(/\d+/)?.[0] || "";
      form.elements.transmission.value = attributes["Коробка передач"] || "";
      form.elements.drive.value = attributes["Привод"] || "";
      form.elements.body.value = attributes["Тип кузова"] || "";
    });
  }

  function renderGarageList(items) {
    const root = document.getElementById("garage-cars");
    root.replaceChildren();
    if (!items.length) {
      root.append(emptyMessage("В гараже пока нет автомобилей."));
      document.getElementById("garage-detail").hidden = true;
      currentId = null;
      return;
    }
    if (!currentId || !items.some((item) => item.id === currentId)) {
      currentId = items[0].id;
    }
    for (const car of items) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `garage-car${car.id === currentId ? " active" : ""}`;
      const title = document.createElement("strong");
      title.textContent = car.name;
      const meta = document.createElement("small");
      meta.textContent = `${formatMileage(car.mileage_km)} · потрачено ${formatMoney(car.spent_total)}`;
      button.append(title, meta);
      button.addEventListener("click", async () => {
        currentId = car.id;
        signature = "";
        await refresh();
      });
      root.append(button);
    }
  }

  function renderEntries(entries) {
    const root = document.getElementById("garage-entries");
    root.replaceChildren();
    if (!entries.length) {
      root.append(emptyMessage("Бортжурнал пока пуст."));
      return;
    }
    const labels = { journal: "Запись", service: "Обслуживание", expense: "Расход" };
    for (const entry of entries) {
      const article = document.createElement("article");
      article.className = `garage-entry is-${entry.entry_type}`;
      const heading = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = entry.title;
      const type = document.createElement("span");
      type.textContent = labels[entry.entry_type] || entry.entry_type;
      heading.append(title, type);
      const meta = document.createElement("small");
      meta.textContent = [
        entry.occurred_at?.slice(0, 10),
        entry.mileage_km != null ? formatMileage(entry.mileage_km) : null,
        entry.cost ? formatMoney(entry.cost) : null,
        entry.category,
      ].filter(Boolean).join(" · ");
      const description = document.createElement("p");
      description.textContent = entry.description || "Без комментария";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "part-delete";
      remove.textContent = "Удалить";
      remove.addEventListener("click", async () => {
        await request(`/api/garage/entries/${entry.id}`, { method: "DELETE" });
        signature = "";
        await refresh();
      });
      article.append(heading, meta, description, remove);
      root.append(article);
    }
  }

  function renderParts(parts) {
    const root = document.getElementById("garage-parts");
    root.replaceChildren();
    if (!parts.length) {
      root.append(emptyMessage("Добавьте запчасть или создайте типовой план."));
      return;
    }
    for (const part of parts) {
      const article = document.createElement("article");
      article.className = `part-card${part.selected_for_replacement ? " is-planned" : ""}`;
      const heading = document.createElement("div");
      heading.className = "part-card-heading";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = Boolean(part.selected_for_replacement);
      const name = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = part.name;
      const meta = document.createElement("small");
      meta.textContent = `${part.category} · ${part.replacement_term} · ${part.estimated ? "ориентировочная цена" : "цена из предложения"}`;
      name.append(title, meta);
      const total = document.createElement("b");
      total.textContent = formatMoney(
        (part.price || 0) * Math.max(1, part.quantity) + (part.labor_cost || 0),
      );
      heading.append(checkbox, name, total);
      const description = document.createElement("p");
      description.textContent = part.description || "Описание не добавлено";
      const footer = document.createElement("div");
      footer.className = "garage-part-footer";
      const seller = document.createElement(part.purchase_url ? "a" : "span");
      seller.textContent = part.seller || "Продавец не указан";
      if (part.purchase_url) {
        seller.href = part.purchase_url;
        seller.target = "_blank";
        seller.rel = "noopener noreferrer";
      }
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "part-delete";
      remove.textContent = "Удалить";
      footer.append(seller, remove);
      checkbox.addEventListener("change", async () => {
        await request(`/api/parts/${part.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ selected_for_replacement: checkbox.checked }),
        });
        signature = "";
        await refresh();
      });
      remove.addEventListener("click", async () => {
        await request(`/api/parts/${part.id}`, { method: "DELETE" });
        signature = "";
        await refresh();
      });
      article.append(heading, description, footer);
      root.append(article);
    }
  }

  function renderAnalysis(payload) {
    const root = document.getElementById("garage-analysis");
    const pointsRoot = document.getElementById("garage-analysis-points");
    const offersRoot = document.getElementById("garage-live-offers");
    const status = document.getElementById("garage-analysis-status");
    const summary = document.getElementById("garage-analysis-summary");
    const analysisRecord = payload.vehicle_analysis;
    const analysis = analysisRecord?.data || {};
    const assessment = payload.listing_assessment?.data || {};
    const excluded = new Set(assessment.excluded_weak_point_ids || []);
    const relevant = new Set(assessment.relevant_weak_point_ids || []);
    const points = (analysis.weak_points || []).filter((point) => {
      if (point.id && excluded.has(point.id)) return false;
      return !relevant.size || !point.id || relevant.has(point.id);
    });
    root.classList.toggle("is-empty", !analysisRecord);
    status.textContent = analysisRecord
      ? `Проведён ${String(analysisRecord.updated_at || "").slice(0, 10)}${analysisRecord.trim_name ? ` · ${analysisRecord.trim_name}` : ""}`
      : "Анализ ещё не проводился";
    summary.textContent = analysis.summary || "Проведите анализ ChatGPT в карточке объявления — рекомендации автоматически появятся здесь.";
    pointsRoot.replaceChildren();
    const matches = new Map(
      (payload.spare_parts?.matches || []).map((match) => [match.weak_point_id, match]),
    );
    for (const point of points) {
      const card = document.createElement("article");
      card.className = `garage-analysis-point priority-${point.priority || "medium"}`;
      const header = document.createElement("header");
      const title = document.createElement("strong");
      title.textContent = [point.system, point.issue].filter(Boolean).join(" — ") || "Рекомендация";
      const priority = document.createElement("span");
      priority.textContent = point.priority === "high" ? "Срочно" : point.priority === "low" ? "Наблюдать" : "Запланировать";
      header.append(title, priority);
      const detail = document.createElement("p");
      detail.textContent = [point.symptoms && `Симптомы: ${point.symptoms}`, point.check && `Проверка: ${point.check}`].filter(Boolean).join(" · ");
      const replacements = document.createElement("div");
      replacements.className = "garage-replacements";
      const repairParts = point.repair_parts || [];
      replacements.textContent = repairParts.length
        ? `Заменить: ${repairParts.map((part) => part.name).filter(Boolean).join(", ")}`
        : "Список деталей для замены не указан";
      const costs = document.createElement("small");
      const match = matches.get(point.id);
      const livePrice = (match?.offers || [])
        .filter((offer) => offer.price != null)
        .reduce((total, offer) => total + offer.price, 0);
      costs.textContent = livePrice
        ? `Найденные предложения: ${formatMoney(livePrice)} · работа от ${formatMoney(point.labor_cost_min || 0)}`
        : `Оценка запчастей: ${formatMoney(point.parts_cost_min)}–${formatMoney(point.parts_cost_max ?? point.parts_cost_min)} · работа от ${formatMoney(point.labor_cost_min || 0)}`;
      card.append(header, detail, replacements, costs);
      pointsRoot.append(card);
    }
    offersRoot.replaceChildren();
    const compatible = payload.spare_parts?.compatible_offers || [];
    if (compatible.length) {
      const heading = document.createElement("strong");
      heading.textContent = `Актуальные предложения (${compatible.length})`;
      const list = document.createElement("div");
      for (const offer of compatible.slice(0, 6)) {
        const link = document.createElement("a");
        link.href = offer.source_url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        const name = document.createElement("span");
        name.textContent = offer.name;
        const price = document.createElement("b");
        price.textContent = formatMoney(offer.price);
        link.append(name, price);
        list.append(link);
      }
      offersRoot.append(heading, list);
    }
  }

  function fillEditForm(car) {
    const form = document.getElementById("garage-edit-form");
    const values = {
      name: car.name, brand: car.brand, model: car.model, year: car.year,
      vin: car.vin, plate_number: car.plate_number, purchase_date: car.purchase_date,
      purchase_price: car.purchase_price, notes: car.notes,
      color: car.attributes?.["Цвет"], engine_type: car.attributes?.["Тип двигателя"],
      engine_volume: car.attributes?.["Объём двигателя"], power: String(car.attributes?.["Мощность"] || "").match(/\d+/)?.[0],
      transmission: car.attributes?.["Коробка передач"], drive: car.attributes?.["Привод"],
      body: car.attributes?.["Тип кузова"],
    };
    for (const [name, value] of Object.entries(values)) {
      if (form.elements[name]) form.elements[name].value = value ?? "";
    }
    document.querySelector('#garage-mileage-form [name="mileage_km"]').value = car.mileage_km ?? "";
  }

  function renderDetail(payload) {
    const { car, analytics } = payload;
    document.getElementById("garage-detail").hidden = false;
    setText("garage-title", car.name);
    setText(
      "garage-meta",
      [car.brand, car.model, car.year, formatMileage(car.mileage_km), car.vin && `VIN ${car.vin}`]
        .filter(Boolean).join(" · "),
    );
    setText("garage-purchase", formatMoney(car.purchase_price));
    setText("garage-spent", formatMoney(analytics.spent_total));
    setText("garage-service", formatMoney(analytics.service_total));
    setText("garage-planned", formatMoney(analytics.planned_total));
    setText("garage-ownership", formatMoney(analytics.ownership_total));
    setText("garage-future", formatMoney(analytics.future_total));
    const photo = document.getElementById("garage-photo");
    const photoEmpty = document.getElementById("garage-photo-empty");
    photo.hidden = !car.photo_url;
    photoEmpty.hidden = Boolean(car.photo_url);
    if (car.photo_url) {
      photo.src = car.photo_url;
      photo.alt = car.name;
    } else {
      photo.removeAttribute("src");
    }
    fillEditForm(car);
    const attributes = document.getElementById("garage-attributes");
    attributes.replaceChildren();
    for (const [name, value] of Object.entries(car.attributes || {})) {
      if (value == null || value === "") continue;
      const term = document.createElement("div");
      const label = document.createElement("dt");
      const content = document.createElement("dd");
      label.textContent = name;
      content.textContent = value;
      term.append(label, content);
      attributes.append(term);
    }
    attributes.hidden = attributes.childElementCount === 0;
    renderEntries(payload.entries);
    renderParts(payload.parts);
    renderAnalysis(payload);
  }

  async function refresh() {
    const list = await request("/api/garage");
    renderGarageList(list.items);
    if (!currentId) return;
    const detail = await request(`/api/garage/${currentId}`);
    const nextSignature = JSON.stringify(detail);
    if (nextSignature !== signature) {
      signature = nextSignature;
      renderDetail(detail);
    }
  }

  function bindForms() {
    document.getElementById("garage-add-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const payload = Object.fromEntries(new FormData(form).entries());
      if (payload.listing) {
        [payload.listing_source, payload.listing_external_id] = payload.listing.split("|", 2);
      }
      delete payload.listing;
      const result = await request("/api/garage", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      currentId = result.id;
      signature = "";
      form.reset();
      await refresh();
    });

    document.getElementById("garage-entry-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!currentId) return;
      const form = event.currentTarget;
      await request(`/api/garage/${currentId}/entries`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.fromEntries(new FormData(form).entries())),
      });
      form.reset();
      signature = "";
      await refresh();
    });

    document.getElementById("garage-part-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!currentId) return;
      const form = event.currentTarget;
      const payload = Object.fromEntries(new FormData(form).entries());
      payload.selected_for_replacement = form.elements.selected_for_replacement.checked;
      await request(`/api/garage/${currentId}/parts`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      form.reset();
      signature = "";
      await refresh();
    });

    document.getElementById("garage-mileage-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!currentId) return;
      const value = event.currentTarget.elements.mileage_km.value;
      await request(`/api/garage/${currentId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mileage_km: value }),
      });
      signature = "";
      await refresh();
    });

    const editForm = document.getElementById("garage-edit-form");
    document.getElementById("garage-edit-toggle").addEventListener("click", () => {
      editForm.hidden = !editForm.hidden;
      if (!editForm.hidden) editForm.scrollIntoView({ behavior: "smooth", block: "center" });
    });
    document.getElementById("garage-edit-close").addEventListener("click", () => {
      editForm.hidden = true;
    });
    editForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!currentId) return;
      await request(`/api/garage/${currentId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(Object.fromEntries(new FormData(editForm).entries())),
      });
      editForm.hidden = true;
      signature = "";
      await refresh();
    });

    document.getElementById("garage-photo-input").addEventListener("change", async (event) => {
      const file = event.currentTarget.files?.[0];
      if (!file || !currentId) return;
      if (file.size > 4 * 1024 * 1024) {
        alert("Выберите фотографию размером до 4 МБ");
        event.currentTarget.value = "";
        return;
      }
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("Не удалось прочитать фотографию"));
        reader.readAsDataURL(file);
      });
      await request(`/api/garage/${currentId}/photo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ data_url: dataUrl }),
      });
      event.currentTarget.value = "";
      signature = "";
      await refresh();
    });

    document.getElementById("garage-parts-seed").addEventListener("click", async () => {
      if (!currentId) return;
      const button = document.getElementById("garage-parts-seed");
      const status = document.getElementById("garage-parts-state");
      button.disabled = true;
      status.textContent = "Сопоставляем типовой план, анализ ChatGPT и найденные предложения…";
      try {
        const result = await request(`/api/garage/${currentId}/parts/seed`, { method: "POST", body: "" });
        status.textContent = `План обновлён: добавлено ${result.created}, обновлено ${result.updated}. Рекомендаций из анализа: ${result.analysis_created}.`;
        signature = "";
        await refresh();
      } finally {
        button.disabled = false;
      }
    });
    document.getElementById("garage-delete").addEventListener("click", async () => {
      if (!currentId || !confirm("Удалить автомобиль и всю его историю?")) return;
      await request(`/api/garage/${currentId}`, { method: "DELETE" });
      currentId = null;
      signature = "";
      await refresh();
    });
  }

  async function init() {
    bindForms();
    await loadListingOptions();
  }

  return { init, refresh };
}
