const tabs = [...document.querySelectorAll(".auth-tab")];
const loginForm = document.getElementById("login-form");
const registerForm = document.getElementById("register-form");
const message = document.getElementById("auth-message");
const copy = {
  login: ["С возвращением", "Войдите в аккаунт", "Продолжите работу со своими подборками и аналитикой."],
  register: ["Новый профиль", "Создайте аккаунт", "Сохраните избранное и настройте персональный автопоиск."],
};

function showMessage(text, success = false) {
  message.textContent = text;
  message.classList.toggle("success", success);
  message.hidden = !text;
}

function setMode(mode) {
  const registration = mode === "register";
  loginForm.hidden = registration;
  registerForm.hidden = !registration;
  tabs.forEach((tab) => {
    const active = tab.dataset.mode === mode;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  ["form-eyebrow", "form-title", "form-subtitle"].forEach((id, index) => {
    document.getElementById(id).textContent = copy[mode][index];
  });
  showMessage("");
  (registration ? registerForm : loginForm).querySelector("input")?.focus();
}

tabs.forEach((tab) => tab.addEventListener("click", () => setMode(tab.dataset.mode)));
document.querySelectorAll(".password-toggle").forEach((button) => {
  button.addEventListener("click", () => {
    const input = button.parentElement.querySelector("input");
    const visible = input.type === "text";
    input.type = visible ? "password" : "text";
    button.textContent = visible ? "Показать" : "Скрыть";
    button.setAttribute("aria-label", visible ? "Показать пароль" : "Скрыть пароль");
  });
});

async function submit(form, endpoint) {
  showMessage("");
  if (!form.reportValidity()) return;
  const button = form.querySelector("button[type=submit]");
  const original = button.querySelector("span").textContent;
  button.disabled = true;
  button.querySelector("span").textContent = "Подождите…";
  try {
    const values = Object.fromEntries(new FormData(form));
    delete values.agreement;
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || "Не удалось выполнить запрос");
    showMessage(endpoint.endsWith("register") ? "Аккаунт создан. Открываем AutoScope…" : "Вход выполнен. Открываем AutoScope…", true);
    setTimeout(() => globalThis.location.assign("/"), 450);
  } catch (error) {
    showMessage(error.message || "Что-то пошло не так. Попробуйте ещё раз.");
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = original;
  }
}

loginForm.addEventListener("submit", (event) => { event.preventDefault(); submit(loginForm, "/api/auth/login"); });
registerForm.addEventListener("submit", (event) => { event.preventDefault(); submit(registerForm, "/api/auth/register"); });

fetch("/api/auth/status").then((response) => response.json()).then((status) => {
  if (status.authenticated) showMessage(`Вы уже вошли как ${status.user.name}. Можно перейти к работе.`, true);
}).catch(() => {});
