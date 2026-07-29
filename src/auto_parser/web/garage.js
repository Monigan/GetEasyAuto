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
      meta.textContent = `${part.category} · ${part.replacement_term}${part.estimated ? " · оценка" : ""}`;
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

    document.getElementById("garage-parts-seed").addEventListener("click", async () => {
      if (!currentId) return;
      await request(`/api/garage/${currentId}/parts/seed`, { method: "POST", body: "" });
      signature = "";
      await refresh();
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
