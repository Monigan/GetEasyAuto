import { formatMileage, formatMoney } from "./ui.js";

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Ошибка центра уведомлений");
  return payload;
}

function commaList(value) {
  return String(value || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function numberOrNull(value) {
  return value === "" ? null : Number(value);
}

function renderMailStatus(payload) {
  const root = document.getElementById("mail-import-status");
  root.className = `mail-status ${payload.configured ? "configured" : ""}`;
  if (!payload.configured) {
    root.innerHTML = "<strong>Импорт Auto.ru не настроен</strong><span>Укажите IMAP-параметры в .env и перезапустите сервис.</span>";
    return;
  }
  const total = payload.recent.reduce((sum, item) => sum + item.listing_count, 0);
  root.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = "Почтовый импорт активен";
  const meta = document.createElement("span");
  meta.textContent = `${payload.username} · ${payload.host}/${payload.mailbox} · последние письма: ${total} объявлений`;
  root.append(title, meta);
}

export function createNotificationCenter() {
  const dialog = document.getElementById("notifications-dialog");
  const badge = document.getElementById("notifications-badge");
  const button = document.getElementById("notifications-open");
  const form = document.getElementById("notification-rule-form");
  let initialized = false;
  let knownUnread = new Set();
  let timer;

  async function markRead(item) {
    if (item.read_at) return;
    await api(`/api/notifications/${item.id}/read`, {
      method: "PATCH",
      body: "{}",
    });
  }

  function renderNotifications(payload, announce = true) {
    badge.textContent = payload.unread_count;
    badge.hidden = payload.unread_count === 0;
    const root = document.getElementById("notifications-list");
    root.replaceChildren();
    if (!payload.items.length) {
      root.textContent = "Подходящих объявлений пока нет.";
    }
    const currentUnread = new Set();
    for (const item of payload.items) {
      if (!item.read_at) currentUnread.add(item.id);
      const row = document.createElement("article");
      row.className = `notification-row ${item.read_at ? "read" : "unread"}`;
      const copy = document.createElement("div");
      const criterion = document.createElement("small");
      criterion.textContent = item.rule_name;
      const title = document.createElement("strong");
      title.textContent = item.title;
      const details = document.createElement("span");
      details.textContent = `${formatMoney(item.price)} · ${formatMileage(item.mileage_km)}`;
      copy.append(criterion, title, details);
      const open = document.createElement("a");
      open.textContent = "Открыть ↗";
      open.href = item.url || "#";
      open.target = "_blank";
      open.rel = "noopener noreferrer";
      open.addEventListener("click", () => markRead(item).catch(console.error));
      row.append(copy, open);
      root.append(row);

      if (
        announce
        && initialized
        && !item.read_at
        && !knownUnread.has(item.id)
        && "Notification" in window
        && Notification.permission === "granted"
      ) {
        const notification = new Notification("Найден подходящий автомобиль", {
          body: `${item.title} — ${formatMoney(item.price)}`,
          tag: `listing-${item.source}-${item.external_id}`,
        });
        notification.onclick = () => window.open(item.url, "_blank", "noopener");
      }
    }
    knownUnread = currentUnread;
    initialized = true;
  }

  function renderRules(payload) {
    const root = document.getElementById("notification-rules-list");
    root.replaceChildren();
    if (!payload.items.length) {
      root.textContent = "Критерии ещё не созданы.";
      return;
    }
    for (const item of payload.items) {
      const row = document.createElement("article");
      row.className = "notification-rule-row";
      const copy = document.createElement("div");
      const title = document.createElement("strong");
      title.textContent = item.name;
      const description = document.createElement("small");
      const ranges = [
        item.max_price != null ? `до ${formatMoney(item.max_price)}` : "",
        item.max_mileage != null ? `до ${formatMileage(item.max_mileage)}` : "",
        item.min_year != null ? `от ${item.min_year} г.` : "",
        item.min_power != null ? `от ${item.min_power} л.с.` : "",
      ].filter(Boolean);
      description.textContent = [item.query, ...item.brands, ...item.models, ...ranges]
        .filter(Boolean).join(" · ") || "Без ограничений";
      copy.append(title, description);
      const actions = document.createElement("div");
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.textContent = item.enabled ? "Приостановить" : "Включить";
      toggle.addEventListener("click", async () => {
        await api(`/api/notification-rules/${item.id}`, {
          method: "PATCH",
          body: JSON.stringify({ enabled: !item.enabled }),
        });
        await loadRules();
      });
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Удалить";
      remove.addEventListener("click", async () => {
        await api(`/api/notification-rules/${item.id}`, { method: "DELETE" });
        await Promise.all([loadRules(), loadNotifications(false)]);
      });
      actions.append(toggle, remove);
      row.append(copy, actions);
      root.append(row);
    }
  }

  async function loadNotifications(announce = true) {
    renderNotifications(await api("/api/notifications"), announce);
  }

  async function loadRules() {
    renderRules(await api("/api/notification-rules"));
  }

  async function refreshDialog() {
    const [status] = await Promise.all([
      api("/api/mail-import/status"),
      loadRules(),
      loadNotifications(false),
    ]);
    renderMailStatus(status);
  }

  button.addEventListener("click", async () => {
    dialog.showModal();
    await refreshDialog();
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const list = (name) => commaList(data.get(name));
    await api("/api/notification-rules", {
      method: "POST",
      body: JSON.stringify({
        name: data.get("name"),
        query: data.get("query"),
        brands: list("brands"),
        models: list("models"),
        colors: list("colors"),
        engines: list("engines"),
        min_price: numberOrNull(data.get("min_price")),
        max_price: numberOrNull(data.get("max_price")),
        max_mileage: numberOrNull(data.get("max_mileage")),
        min_year: numberOrNull(data.get("min_year")),
        max_year: numberOrNull(data.get("max_year")),
        min_power: numberOrNull(data.get("min_power")),
      }),
    });
    form.reset();
    await Promise.all([loadRules(), loadNotifications(false)]);
  });

  document.getElementById("notifications-read-all").addEventListener("click", async () => {
    await api("/api/notifications/read-all", { method: "POST", body: "{}" });
    await loadNotifications(false);
  });

  document.getElementById("notifications-browser-enable").addEventListener("click", async () => {
    if ("Notification" in window) await Notification.requestPermission();
  });

  return {
    async init() {
      await loadNotifications(false);
      clearInterval(timer);
      timer = setInterval(() => loadNotifications(true).catch(console.error), 5000);
    },
    refresh: loadNotifications,
  };
}
