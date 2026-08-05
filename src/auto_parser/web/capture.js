const state = document.getElementById("capture-state");
const closeButton = document.getElementById("capture-close");
let received = false;

function finish(message, isError = false) {
  state.textContent = message;
  state.classList.toggle("is-error", isError);
  closeButton.hidden = false;
}

window.addEventListener("message", async (event) => {
  if (received || !/^https:\/\/(?:www\.)?baza\.drom\.ru$/.test(event.origin)) return;
  if (event.data?.type !== "autoscope-capture") return;
  received = true;
  state.textContent = "Сохраняем предложения в AutoScope…";
  try {
    const response = await fetch("/api/spare-parts/import-html", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: event.data.url,
        html: event.data.html,
        pages: event.data.pages,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Не удалось импортировать страницу");
    const message = `Готово: сохранено ${payload.imported} предложений · ${payload.brand} ${payload.model}${payload.pages_imported ? ` · страниц: ${payload.pages_imported}` : ""}`;
    finish(message);
    event.source?.postMessage({ type: "autoscope-import-complete", message }, event.origin);
  } catch (error) {
    finish(error.message, true);
    event.source?.postMessage({ type: "autoscope-import-error", message: error.message }, event.origin);
  }
});

const readyTimer = setInterval(() => {
  if (received || !window.opener || window.opener.closed) {
    clearInterval(readyTimer);
    if (!received && (!window.opener || window.opener.closed)) {
      finish("Исходная вкладка Drom закрыта. Откройте её и снова нажмите кнопку импорта.", true);
    }
    return;
  }
  window.opener.postMessage({ type: "autoscope-ready" }, "*");
}, 400);

setTimeout(() => {
  if (!received) finish("Вкладка Drom не ответила. Убедитесь, что кнопка запущена именно на странице списка запчастей.", true);
  clearInterval(readyTimer);
}, 12000);

closeButton.addEventListener("click", () => window.close());
