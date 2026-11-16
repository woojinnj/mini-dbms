"use strict";

const get = (id) => document.getElementById(id);
const query = get("query");
const sql = get("sql");
const runButton = get("run");
const auth = get("auth");
const tokenInput = get("token");
const tablePicker = get("table-picker");
const tables = get("tables");
const status = get("status");
const error = get("error");
const output = get("output");
const timing = get("timing");
const pages = get("pages");
const previous = get("previous");
const next = get("next");
const pageLabel = get("page-label");
const number = new Intl.NumberFormat("ko-KR");
const pageSize = 50;

let token = "";
let selectedResult = null;
let page = 0;

async function request(path, options = {}) {
  const headers = new Headers(options.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch {
    throw new Error("MiniDB 서버에 연결할 수 없습니다.");
  }
  if (response.status === 401) {
    const needsFocus = auth.hidden;
    auth.hidden = false;
    token = "";
    tokenInput.value = "";
    if (needsFocus) tokenInput.focus();
    throw new Error("API 토큰을 확인해 주세요.");
  }
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error?.message || `HTTP ${response.status}`);
  return payload;
}

function showError(message) {
  error.textContent = message;
  error.hidden = !message;
}

function preview(table) {
  sql.value = `SELECT *\nFROM ${table}\nLIMIT 100;`;
}

async function connect(showAuthError = false) {
  try {
    const health = await request("/health");
    tables.replaceChildren(...health.tables.map((name) => new Option(name, name)));
    tablePicker.hidden = !tables.length;
    if (!sql.value.trim() && tables.length) preview(tables.value);
    auth.hidden = true;
    tokenInput.value = "";
    showError("");
    return true;
  } catch (failure) {
    // The token form itself signals that the server requires authentication.
    if (auth.hidden || showAuthError) {
      showError(failure.message);
    }
    return false;
  }
}

function renderTable(columns, rows) {
  const viewport = document.createElement("div");
  viewport.className = "table-scroll";
  viewport.tabIndex = 0;
  viewport.setAttribute("role", "region");
  viewport.setAttribute("aria-label", "SQL 결과 표");
  const table = document.createElement("table");
  const headings = table.createTHead().insertRow();
  for (const column of columns) {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = column;
    headings.append(cell);
  }
  const body = table.createTBody();
  for (const row of rows) {
    const tableRow = body.insertRow();
    for (const column of columns) {
      const cell = tableRow.insertCell();
      const value = row[column];
      if (value === null) cell.className = "null";
      cell.textContent = value === null ? "NULL" : String(value);
    }
  }
  viewport.append(table);
  output.replaceChildren(viewport);
}

function renderPage() {
  const { columns, rows } = selectedResult;
  const start = page * pageSize;
  const end = Math.min(start + pageSize, rows.length);
  renderTable(columns, rows.slice(start, end));
  pages.hidden = rows.length <= pageSize;
  previous.disabled = start === 0;
  next.disabled = end === rows.length;
  pageLabel.textContent = `${number.format(start + 1)}–${number.format(end)}`;
}

function renderResult(result) {
  timing.textContent = `${result.elapsed_ms} ms`;
  if (result.command === "SELECT") {
    status.textContent = `${number.format(result.row_count)}행${result.truncated ? " · 결과 제한 도달" : ""}`;
    selectedResult = result;
    page = 0;
    renderPage();
  } else if (result.command === "INSERT") {
    const ids = result.first_id === result.last_id ? result.first_id : `${result.first_id}–${result.last_id}`;
    status.textContent = `${number.format(result.affected_rows)}행 추가 · ID ${ids}`;
  } else if (result.command === "EXPLAIN") {
    renderTable(["테이블", "실행 계획", "인덱스", "테이블 행 수"], [{
      "테이블": result.table,
      "실행 계획": result.plan,
      "인덱스": result.index,
      "테이블 행 수": result.table_rows,
    }]);
  }
}

auth.addEventListener("submit", async (event) => {
  event.preventDefault();
  token = tokenInput.value;
  if (!token) return;
  const connectButton = auth.querySelector("button");
  connectButton.disabled = true;
  try {
    if (await connect(true)) sql.focus();
  } finally {
    connectButton.disabled = false;
  }
});

tables.addEventListener("change", () => {
  preview(tables.value);
  sql.focus();
});

query.addEventListener("submit", async (event) => {
  event.preventDefault();
  const statement = sql.value.trim();
  if (!statement || runButton.disabled) return;

  runButton.disabled = true;
  tables.disabled = true;
  timing.textContent = "";
  output.replaceChildren();
  pages.hidden = true;
  showError("");
  status.textContent = "실행 중…";
  try {
    const response = await request("/query", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/vnd.minidb.ui+json",
      },
      body: JSON.stringify({ sql: statement }),
    });
    status.textContent = "";
    renderResult(response.result);
  } catch (failure) {
    status.textContent = "";
    showError(failure.message);
  } finally {
    runButton.disabled = false;
    tables.disabled = false;
  }
});

sql.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) {
    event.preventDefault();
    query.requestSubmit();
  }
});

previous.addEventListener("click", () => {
  page -= 1;
  renderPage();
});

next.addEventListener("click", () => {
  page += 1;
  renderPage();
});

connect();
