export const money = new Intl.NumberFormat("ru-RU", {
  maximumFractionDigits: 0,
});

export function formatMoney(value) {
  return value == null ? "Цена не указана" : `${money.format(value)} ₽`;
}

export function formatMileage(value) {
  return value == null ? "Пробег не указан" : `${money.format(value)} км`;
}

export function setText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

export function emptyMessage(text) {
  const node = document.createElement("p");
  node.className = "empty-note";
  node.textContent = text;
  return node;
}
