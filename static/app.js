const state = {
  summary: null,
  debts: [],
  unmatched: { debts: [], deposits: [] },
  matches: [],
  reliquidaciones: [],
  extracto: [],
  selectedDebts: [],
  selectedDeposit: null,
};
const $ = (s) => document.querySelector(s),
  $$ = (s) => [...document.querySelectorAll(s)];
const money = (n) =>
  new Intl.NumberFormat("es-BO", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(n || 0));
const dateFmt = (s) =>
  s
    ? new Date(s.length === 10 ? s + "T12:00:00" : s).toLocaleDateString(
        "es-BO",
      )
    : "";
const dateTimeFmt = (s) =>
  s
    ? new Date(s.replace(" ", "T")).toLocaleString("es-BO", {
        dateStyle: "short",
        timeStyle: "short",
      })
    : "";
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>'"]/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[
        c
      ],
  );
function toast(msg, error = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (error ? " error" : "");
  setTimeout(() => (t.className = "toast"), 3500);
}
async function api(url, opt) {
  const r = await fetch(url, opt);
  const d = await r.json();
  if (!r.ok) throw new Error(d.error || "Error");
  return d;
}

$$(".tab").forEach((b) =>
  b.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.remove("active"));
    $$(".panel").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("#" + b.dataset.tab).classList.add("active");
  }),
);

function statusClass(s) {
  s = (s || "").toUpperCase();
  if (s.includes("MANUAL")) return "manual";
  if (s.includes("VENCIDO") || s.includes("MORA")) return "vencido";
  if (s.includes("POR VENCER")) return "por-vencer";
  if (s.includes("PAGADO")) return "pagado";
  if (s.includes("AJUSTE") || s.includes("RELIQ") || s.includes("REVERS"))
    return "ajuste";
  return "";
}
function badge(s) {
  return `<span class="status ${statusClass(s)}">${esc(s)}</span>`;
}

async function refreshAll() {
  try {
    const [
      summary,
      unmatched,
      matches,
      dte,
      reliquidaciones,
      extracto,
      companies,
      settings,
      imports,
    ] = await Promise.all([
      api("/api/summary"),
      api("/api/unmatched"),
      api("/api/matches"),
      api("/api/raw/dte"),
      api("/api/raw/reliquidacion"),
      api("/api/raw/extracto"),
      api("/api/companies"),
      api("/api/settings"),
      api("/api/imports"),
    ]);
    state.summary = summary;
    state.debts = summary.debts;
    state.unmatched = unmatched;
    state.matches = matches;
    state.reliquidaciones = reliquidaciones;
    state.extracto = extracto;
    renderDashboard();
    renderDebtTable();
    renderUnmatched();
    renderMatches();
    renderDTE(dte);
    renderReliquidacion();
    renderExtract(extracto);
    renderCompanies(companies);
    renderSettings(settings);
    renderImports(imports);
    $("#unmatchedBadge").textContent =
      unmatched.debts.length + unmatched.deposits.length;
    $("#reliqBadge").textContent = reliquidaciones.length;
  } catch (e) {
    toast(e.message, true);
  }
}

function renderDashboard() {
  const k = state.summary.kpis;
  $("#tcCurrent").value = k.current_tc;
  const cards = [
    ["Por vencer Bs", k.due_soon, "", ""],
    ["Vencido Bs", k.overdue, "danger", ""],
    ["Pendiente neto Bs", k.pending, "accent", "DTE + Reliquidación"],
    ["Cobrado Bs", k.paid, "success", ""],
    ["Deuda registrada Bs", k.total_debt, "", "saldo firmado"],
    ["USD erosionados", k.erosion_usd, "danger", "al TC actual"],
  ];
  $("#kpis").innerHTML = cards
    .map(
      (c) =>
        `<div class="kpi ${c[2]}"><div class="label">${c[0]}</div><div class="value">${money(c[1])}</div><div class="sub">${c[3] || " "}</div></div>`,
    )
    .join("");
  const debts = state.debts.filter((d) => d.amount > 0);
  const companies = {};
  debts.forEach((d) => {
    companies[d.company] ??= { due: 0, late: 0, count: 0, max: 0 };
    if (d.pending > 0) {
      if (d.status.includes("VENCIDO")) companies[d.company].late += d.pending;
      else if (d.status.includes("POR VENCER"))
        companies[d.company].due += d.pending;
      if (d.status.includes("VENCIDO")) {
        companies[d.company].count++;
        companies[d.company].max = Math.max(
          companies[d.company].max,
          d.days_late,
        );
      }
    }
  });
  const compArr = Object.entries(companies)
    .sort((a, b) => b[1].late + b[1].due - (a[1].late + a[1].due))
    .slice(0, 12);
  drawStackedBars(
    $("#companyChart"),
    compArr.map((x) => shortName(x[0])),
    compArr.map((x) => x[1].due),
    compArr.map((x) => x[1].late),
    ["Por vencer", "Vencido"],
  );
  const buckets = [
    ["1–30", 1, 30],
    ["31–60", 31, 60],
    ["61–90", 61, 90],
    ["91–180", 91, 180],
    [">180", 181, 99999],
  ];
  drawBars(
    $("#agingChart"),
    buckets.map((x) => x[0]),
    buckets.map((x) =>
      debts
        .filter(
          (d) => d.pending > 0 && d.days_late >= x[1] && d.days_late <= x[2],
        )
        .reduce((s, d) => s + d.pending, 0),
    ),
  );
  const status = [
    [
      "Pagado",
      debts.filter((d) => d.pending <= 0.01).reduce((s, d) => s + d.amount, 0),
    ],
    [
      "Por vencer",
      debts
        .filter((d) => d.pending > 0 && d.status.includes("POR VENCER"))
        .reduce((s, d) => s + d.pending, 0),
    ],
    [
      "Vencido",
      debts
        .filter((d) => d.pending > 0 && d.status.includes("VENCIDO"))
        .reduce((s, d) => s + d.pending, 0),
    ],
  ];
  drawDonut($("#statusChart"), status);
  const month = {};
  state.matches.forEach((m) => {
    if (!m.payment_date) return;
    const key = m.payment_date.slice(0, 7);
    month[key] = (month[key] || 0) + Number(m.applied_amount || 0);
  });
  const months = Object.keys(month).sort();
  drawLine(
    $("#collectionsChart"),
    months.map((m) => m.slice(5) + "/" + m.slice(0, 4)),
    months.map((m) => month[m]),
  );
  const morosos = Object.entries(companies)
    .filter((x) => x[1].late > 0)
    .sort((a, b) => b[1].late - a[1].late)
    .slice(0, 12);
  $("#morososBody").innerHTML =
    morosos
      .map(
        ([n, v]) =>
          `<tr><td>${esc(n)}</td><td class="num">${money(v.late)}</td><td class="num">${v.count}</td><td class="num">${v.max}</td><td>${badge(v.max > 90 ? "ALTO" : v.max > 30 ? "MEDIO" : "CONTROLADO")}</td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="5" class="muted">Sin saldos vencidos.</td></tr>';
}
function shortName(s) {
  return s.length > 22 ? s.slice(0, 20) + "…" : s;
}

function renderDebtTable() {
  const q = $("#debtSearch").value.toLowerCase(),
    st = $("#debtStatus").value.toUpperCase(),
    co = $("#debtConcept").value.toUpperCase(),
    amt = parseAmount($("#debtTableAmount").value);
  let rows = state.debts.filter(
    (r) =>
      (!q ||
        (
          r.company +
          " " +
          r.status +
          " " +
          r.concept +
          " " +
          r.amount +
          " " +
          r.pending
        )
          .toLowerCase()
          .includes(q)) &&
      (!st || r.status.toUpperCase().includes(st)) &&
      (!co || r.concept.toUpperCase() === co) &&
      (amt === null ||
        Math.abs(Number(r.amount) - amt) <= 0.01 ||
        Math.abs(Number(r.pending) - amt) <= 0.01),
  );
  $("#debtBody").innerHTML =
    rows
      .map(
        (r) =>
          `<tr><td><b>${esc(r.concept)}</b> - ${esc(r.company)}</td><td>${dateFmt(r.date)}</td><td>${esc(r.reference || "")}</td><td>${esc(r.transaction_code || "")}</td><td class="num">${money(r.amount)}</td><td class="num">${money(r.paid)}</td><td class="num">${money(r.pending)}</td><td class="num">${money(r.pending_usd)}</td><td>${dateFmt(r.due_date)}</td><td>${dateTimeFmt(r.last_payment)}</td><td class="num">${r.days_to_payment ?? "—"}</td><td class="num">${r.days_late}</td><td>${badge(r.status)}</td><td>${r.manual ? badge("MANUAL") : r.match_count ? '<span class="status">EXCEL / AUTO</span>' : "—"}</td><td class="num">${r.tc_dte == null ? "—" : money(r.tc_dte)}</td><td class="num">${r.tc_payment == null ? "—" : money(r.tc_payment)}</td><td class="num">${r.tc_due == null ? "—" : money(r.tc_due)}</td><td class="num">${money(r.tc_current)}</td><td class="num">${r.usd_due == null ? "—" : money(r.usd_due)}</td><td class="num">${r.usd_today == null ? "—" : money(r.usd_today)}</td><td class="num">${r.erosion_usd == null ? "—" : money(r.erosion_usd)}</td><td class="num">${r.erosion_bs == null ? "—" : money(r.erosion_bs)}</td><td class="num">${r.erosion_pct == null ? "—" : money(r.erosion_pct) + "%"}</td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="23" class="muted">Sin resultados.</td></tr>';
}

function parseAmount(v) {
  if (v === null || v === undefined || String(v).trim() === "") return null;
  const n = Number(String(v).replace(",", "."));
  return Number.isFinite(n) ? n : null;
}
function amountWithin(value, target, tol) {
  return target === null || Math.abs(Number(value || 0) - target) <= tol;
}
function renderUnmatched() {
  const dq = $("#depSearch").value.toLowerCase(),
    qq = $("#unDebtSearch").value.toLowerCase(),
    depAmt = parseAmount($("#depAmountSearch").value),
    debtAmt = parseAmount($("#unDebtAmountSearch").value),
    tol = Math.max(0, parseAmount($("#amountTolerance").value) ?? 0.01);
  let deps = state.unmatched.deposits.filter(
    (d) =>
      (
        d.branch +
        " " +
        d.description +
        " " +
        d.reference +
        " " +
        d.transaction_code +
        " " +
        d.remaining +
        " " +
        d.amount
      )
        .toLowerCase()
        .includes(dq) && amountWithin(d.remaining, depAmt, tol),
  );
  let debts = state.unmatched.debts.filter(
    (d) =>
      (
        d.company +
        " " +
        d.concept +
        " " +
        d.status +
        " " +
        d.pending +
        " " +
        d.amount
      )
        .toLowerCase()
        .includes(qq) && amountWithin(d.pending, debtAmt, tol),
  );
  if (depAmt !== null)
    deps.sort(
      (a, b) => Math.abs(a.remaining - depAmt) - Math.abs(b.remaining - depAmt),
    );
  if (debtAmt !== null)
    debts.sort(
      (a, b) => Math.abs(a.pending - debtAmt) - Math.abs(b.pending - debtAmt),
    );
  $("#depCount").textContent =
    deps.length + " de " + state.unmatched.deposits.length + " pendientes";
  $("#debtUnCount").textContent =
    debts.length + " de " + state.unmatched.debts.length + " pendientes";
  $("#depositList").innerHTML =
    deps
      .map(
        (d) =>
          `<div class="pick ${state.selectedDeposit === d.id ? "selected" : ""}" onclick="selectDeposit('${d.id}')"><div class="top"><div class="name">${esc(d.branch || "Depósito")}</div><div class="amount">Bs ${money(d.remaining)}</div></div><div class="meta">${dateTimeFmt(d.posted_at)} · Ref: ${esc(d.reference || "—")} · ${esc(d.transaction_code || "")}</div><div class="meta">${esc(d.description || "")}</div></div>`,
      )
      .join("") ||
    '<div class="pick muted">No hay depósitos con esos filtros.</div>';
  $("#unDebtList").innerHTML =
    debts
      .map((d) => {
        const selected = state.selectedDebts.includes(d.id);
        return `<div class="pick ${selected ? "selected" : ""}" onclick="toggleDebt('${d.id}')"><div class="top"><div class="name"><span class="check">${selected ? "✓" : "+"}</span> ${esc(d.concept)} · ${esc(d.company)}</div><div class="amount">Bs ${money(d.pending)}</div></div><div class="meta">${esc(d.concept)} ${dateFmt(d.date)} · límite ${dateFmt(d.due_date)} · ${esc(d.status)}</div></div>`;
      })
      .join("") ||
    '<div class="pick muted">No hay deudas con esos filtros.</div>';
  renderAllocationEditor();
  renderComboSuggestions();
  updateMatchHint();
}
function useSelectedDepositAmount() {
  const d = state.unmatched.deposits.find(
    (x) => x.id === state.selectedDeposit,
  );
  if (!d) return toast("Seleccione primero un depósito.", true);
  $("#unDebtAmountSearch").value = Number(d.remaining).toFixed(2);
  renderUnmatched();
}
function useSelectedDebtAmount() {
  const list = state.unmatched.debts.filter((x) =>
    state.selectedDebts.includes(x.id),
  );
  if (!list.length) return toast("Seleccione al menos una deuda.", true);
  const total = list.reduce((s, d) => s + Number(d.pending), 0);
  $("#depAmountSearch").value = total.toFixed(2);
  renderUnmatched();
}
function selectDeposit(id) {
  state.selectedDeposit = state.selectedDeposit === id ? null : id;
  autoAllocateSelected();
  renderUnmatched();
}
function toggleDebt(id) {
  const i = state.selectedDebts.indexOf(id);
  if (i >= 0) state.selectedDebts.splice(i, 1);
  else state.selectedDebts.push(id);
  autoAllocateSelected();
  renderUnmatched();
}
function selectDebt(id) {
  toggleDebt(id);
}
function clearSelectedDebts() {
  state.selectedDebts = [];
  renderUnmatched();
}
function autoAllocateSelected() {
  setTimeout(() => {
    const dep = state.unmatched.deposits.find(
      (x) => x.id === state.selectedDeposit,
    );
    let available = dep ? Number(dep.remaining) : Infinity;
    state.selectedDebts.forEach((id) => {
      const d = state.unmatched.debts.find((x) => x.id === id),
        el = document.querySelector(`[data-alloc-debt="${CSS.escape(id)}"]`);
      if (!d || !el) return;
      const v = Math.max(0, Math.min(Number(d.pending), available));
      el.value = v.toFixed(2);
      available -= v;
    });
    updateMatchHint();
  }, 0);
}
function setSuggestedAmount() {
  autoAllocateSelected();
}
function renderAllocationEditor() {
  const box = $("#allocationEditor");
  if (!box) return;
  const list = state.unmatched.debts.filter((d) =>
    state.selectedDebts.includes(d.id),
  );
  if (!list.length) {
    box.innerHTML =
      '<div class="muted small">Seleccione uno o varios DTE/Reliquidaciones.</div>';
    return;
  }
  box.innerHTML =
    `<div class="alloc-head"><b>${list.length} documento(s) seleccionado(s)</b><button class="link-btn" onclick="clearSelectedDebts()">Limpiar</button></div>` +
    list
      .map(
        (d) =>
          `<div class="allocation-row"><div><b>${esc(d.concept)} · ${esc(d.company)}</b><small>${dateFmt(d.date)} · pendiente Bs ${money(d.pending)}</small></div><input data-alloc-debt="${esc(d.id)}" type="number" min="0" max="${Number(d.pending)}" step="0.01" value="${Number(d.pending).toFixed(2)}" oninput="updateMatchHint()"></div>`,
      )
      .join("");
}
function selectedAllocations() {
  return state.selectedDebts
    .map((id) => {
      const d = state.unmatched.debts.find((x) => x.id === id),
        el = document.querySelector(`[data-alloc-debt="${CSS.escape(id)}"]`);
      return d && el
        ? { debt_id: id, amount: Number(el.value || 0), debt: d }
        : null;
    })
    .filter(Boolean);
}
function updateMatchHint() {
  const dep = state.unmatched.deposits.find(
      (x) => x.id === state.selectedDeposit,
    ),
    allocs = selectedAllocations(),
    total = allocs.reduce((s, a) => s + a.amount, 0),
    totalDebt = allocs.reduce((s, a) => s + Number(a.debt.pending), 0);
  let t = "Seleccione un depósito y uno o varios DTE/Reliquidaciones.";
  if (dep || allocs.length) {
    t = `Depósito disponible: Bs ${money(dep?.remaining || 0)} · Seleccionado en ${allocs.length} documento(s): Bs ${money(total)} · Pendiente conjunto: Bs ${money(totalDebt)}`;
    if (dep && Math.abs(Number(dep.remaining) - total) <= 0.01)
      t += " · ✓ Cuadra con el depósito";
    else if (dep && total > Number(dep.remaining) + 0.01)
      t += " · ⚠ Supera el depósito";
  }
  $("#matchHint").textContent = t;
}
function renderComboSuggestions() {
  const box = $("#comboSuggestions");
  if (!box) return;
  const dep = state.unmatched.deposits.find(
    (x) => x.id === state.selectedDeposit,
  );
  if (!dep) {
    box.innerHTML =
      '<span class="muted">Seleccione un depósito para buscar pares de DTE/Reliquidaciones por suma.</span>';
    return;
  }
  const tol = Math.max(0, parseAmount($("#amountTolerance").value) ?? 0.01),
    target = Number(dep.remaining),
    rows = state.unmatched.debts.filter((d) => Number(d.pending) > 0.01);
  const combos = [];
  for (let i = 0; i < rows.length; i++) {
    for (let j = i + 1; j < rows.length; j++) {
      const sum = Number(rows[i].pending) + Number(rows[j].pending),
        diff = Math.abs(sum - target);
      if (diff <= tol) combos.push({ a: rows[i], b: rows[j], sum, diff });
    }
  }
  combos.sort((x, y) => x.diff - y.diff || x.sum - y.sum);
  const show = combos.slice(0, 20);
  box.innerHTML = show.length
    ? show
        .map(
          (c, i) =>
            `<button class="combo-item" onclick="selectCombo('${c.a.id}','${c.b.id}')"><span><b>${esc(c.a.concept)} ${esc(c.a.company)}</b> + <b>${esc(c.b.concept)} ${esc(c.b.company)}</b></span><strong>Bs ${money(c.sum)}</strong></button>`,
        )
        .join("")
    : `<span class="muted">No encontré combinación exacta de 2 documentos dentro de ± Bs ${money(tol)}.</span>`;
}
function selectCombo(a, b) {
  state.selectedDebts = [a, b];
  $("#unDebtAmountSearch").value = "";
  renderUnmatched();
  setTimeout(autoAllocateSelected, 0);
}
async function createManualMatch() {
  const allocs = selectedAllocations().filter((a) => a.amount > 0);
  if (!state.selectedDeposit || !allocs.length)
    return toast(
      "Seleccione un depósito y al menos un DTE/Reliquidación.",
      true,
    );
  try {
    const res = await api("/api/manual-match", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        deposit_id: state.selectedDeposit,
        allocations: allocs.map((a) => ({
          debt_id: a.debt_id,
          amount: a.amount,
        })),
        note: $("#matchNote").value,
      }),
    });
    state.selectedDeposit = null;
    state.selectedDebts = [];
    $("#matchNote").value = "";
    toast(
      `${res.allocations || allocs.length} documento(s) emparejados MANUAL por Bs ${money(res.total || 0)}.`,
    );
    await refreshAll();
  } catch (e) {
    toast(e.message, true);
  }
}
function renderMatches() {
  $("#matchBody").innerHTML =
    state.matches
      .map(
        (m) =>
          `<tr><td>${badge(m.status)}</td><td>${badge(m.source)}</td><td>${dateFmt(m.debt_date)}</td><td>${esc(m.company)}</td><td>${esc(m.concept)}</td><td class="num">${money(m.debt_amount)}</td><td>${dateTimeFmt(m.posted_at || m.payment_date)}</td><td>${esc(m.reference || "")}</td><td>${esc(m.transaction_code || "")}</td><td class="num">${money(m.applied_amount)}</td><td>${esc(m.note || "")}</td><td>${m.source === "MANUAL" ? `<button class="btn danger" onclick="deleteManual(${m.id})">Deshacer</button>` : ""}</td></tr>`,
      )
      .join("") || '<tr><td colspan="12" class="muted">Sin cruces.</td></tr>';
}
async function deleteManual(id) {
  if (
    !confirm(
      "¿Deshacer este emparejamiento MANUAL? Volverá a aparecer como pendiente.",
    )
  )
    return;
  try {
    await api("/api/manual-match/" + id, { method: "DELETE" });
    toast("Emparejamiento manual deshecho.");
    await refreshAll();
  } catch (e) {
    toast(e.message, true);
  }
}

function renderDTE(rows) {
  $("#dteBody").innerHTML =
    rows
      .map(
        (r) =>
          `<tr><td>${dateFmt(r.date)}</td><td>${badge(r.concept)}</td><td>${esc(r.company)}</td><td class="num">${money(r.amount)}</td></tr>`,
      )
      .join("") || '<tr><td colspan="4" class="muted">Sin DTE.</td></tr>';
}
function renderReliquidacion() {
  const q = ($("#reliqSearch")?.value || "").toLowerCase(),
    amt = parseAmount($("#reliqAmount")?.value);
  const rows = state.reliquidaciones.filter(
    (r) =>
      (!q || r.company.toLowerCase().includes(q)) &&
      (amt === null || Math.abs(Number(r.amount) - amt) <= 0.01),
  );
  $("#reliqBody").innerHTML =
    rows
      .map(
        (r) =>
          `<tr><td>${dateFmt(r.date)}</td><td>${badge(r.concept)}</td><td>${esc(r.company)}</td><td class="num">${money(r.amount)}</td><td>${badge(Number(r.amount) < 0 ? "AJUSTE" : "DEUDA")}</td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="5" class="muted">Sin reliquidaciones con esos filtros.</td></tr>';
}
function renderExtract(rows) {
  const q = ($("#extractSearch")?.value || "").toLowerCase(),
    amt = parseAmount($("#extractAmount")?.value);
  const filtered = rows.filter((r) => {
    const hay = (
      r.description +
      " " +
      r.reference +
      " " +
      r.transaction_code +
      " " +
      r.movement_status
    ).toLowerCase();
    return (
      (!q || hay.includes(q)) &&
      (amt === null || Math.abs(Math.abs(Number(r.amount)) - amt) <= 0.01)
    );
  });
  $("#extractBody").innerHTML =
    filtered
      .map(
        (r) =>
          `<tr class="${r.effective ? "" : "movement-inactive"}"><td>${dateTimeFmt(r.posted_at)}</td><td>${esc(r.branch)}</td><td>${esc(r.description)}</td><td>${esc(r.reference)}</td><td>${esc(r.transaction_code)}</td><td class="num">${Number(r.debit) > 0 ? money(r.debit) : ""}</td><td class="num">${Number(r.credit) > 0 ? money(r.credit) : ""}</td><td>${badge(r.movement_status)}</td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="8" class="muted">Sin movimientos con esos filtros.</td></tr>';
}
function renderCompanies(rows) {
  $("#companyBody").innerHTML = rows
    .map(
      (r) => `<tr><td>${esc(r.name)}</td><td><b>${esc(r.abbr)}</b></td></tr>`,
    )
    .join("");
}
function renderSettings(s) {
  $("#paymentDays").value = s.payment_days;
  $("#tcSetting").value = s.current_tc;
  $("#tcCurrent").value = s.current_tc;
}
async function saveSettings() {
  try {
    await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_tc: $("#tcCurrent").value }),
    });
    toast("TC actualizado.");
    await refreshAll();
  } catch (e) {
    toast(e.message, true);
  }
}
async function saveSettingsFull() {
  try {
    await api("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        current_tc: $("#tcSetting").value,
        payment_days: $("#paymentDays").value,
      }),
    });
    toast("Configuración guardada.");
    await refreshAll();
  } catch (e) {
    toast(e.message, true);
  }
}
async function uploadWorkbook() {
  const f = $("#uploadFile").files[0];
  if (!f) return toast("Seleccione un archivo.", true);
  const fd = new FormData();
  fd.append("file", f);
  $("#uploadStatus").textContent = "Importando…";
  try {
    const r = await api("/api/import", { method: "POST", body: fd });
    $("#uploadStatus").textContent =
      `${r.debts} deudas, ${r.deposits} depósitos, ${r.companies} empresas.`;
    toast(r.message);
    await refreshAll();
  } catch (e) {
    $("#uploadStatus").textContent = e.message;
    toast(e.message, true);
  }
}
function renderImports(rows) {
  $("#importHistory").innerHTML =
    rows
      .map(
        (r) =>
          `<div class="history-item"><div><b>${esc(r.filename)}</b><div class="muted">${r.debt_count} deudas · ${r.deposit_count} depósitos</div></div><div class="muted">${dateTimeFmt(r.imported_at)}</div></div>`,
      )
      .join("") || '<div class="muted">Sin importaciones.</div>';
}

// Small dependency-free canvas charts: the app remains usable completely offline.
function prep(c) {
  const dpr = window.devicePixelRatio || 1,
    rect = c.getBoundingClientRect();
  c.width = rect.width * dpr;
  c.height = 300 * dpr;
  const x = c.getContext("2d");
  x.setTransform(dpr, 0, 0, dpr, 0, 0);
  x.clearRect(0, 0, rect.width, 300);
  return [x, rect.width, 300];
}
function chartColors() {
  return ["#2b618e", "#a74858", "#d09a3f", "#4b8a69", "#78649a", "#5c7890"];
}

// Tooltip para los canvas. Como los graficos son dibujados a mano, el navegador
// no sabe que cada barra/punto/segmento representa un valor; por eso se agrega
// deteccion manual del cursor.
function getChartTooltip() {
  let t = document.getElementById("chartTooltip");
  if (t) return t;
  t = document.createElement("div");
  t.id = "chartTooltip";
  Object.assign(t.style, {
    position: "fixed",
    zIndex: "9999",
    pointerEvents: "none",
    display: "none",
    background: "#172033",
    color: "#fff",
    padding: "8px 10px",
    borderRadius: "9px",
    boxShadow: "0 8px 24px rgba(15,23,42,.25)",
    font: "600 12px/1.35 system-ui",
    whiteSpace: "pre-line",
    maxWidth: "260px",
  });
  document.body.appendChild(t);
  return t;
}
function bindChartTooltip(c, hitTest) {
  const tip = getChartTooltip();
  c.onmousemove = (e) => {
    const r = c.getBoundingClientRect();
    const hit = hitTest(e.clientX - r.left, e.clientY - r.top);
    if (!hit) {
      tip.style.display = "none";
      c.style.cursor = "default";
      return;
    }
    tip.textContent = hit;
    tip.style.display = "block";
    c.style.cursor = "pointer";
    let left = e.clientX + 14,
      top = e.clientY + 14;
    const tw = tip.offsetWidth,
      th = tip.offsetHeight;
    if (left + tw + 8 > window.innerWidth) left = e.clientX - tw - 14;
    if (top + th + 8 > window.innerHeight) top = e.clientY - th - 14;
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = Math.max(8, top) + "px";
  };
  c.onmouseleave = () => {
    tip.style.display = "none";
    c.style.cursor = "default";
  };
}

function drawBars(c, labels, vals) {
  const [x, w, h] = prep(c),
    pad = { l: 55, r: 15, t: 15, b: 45 },
    mx = Math.max(...vals, 1),
    regions = [];
  x.strokeStyle = "#e2e7ee";
  x.fillStyle = "#6b7688";
  x.font = "11px system-ui";
  for (let i = 0; i < 5; i++) {
    let y = pad.t + ((h - pad.t - pad.b) * i) / 4;
    x.beginPath();
    x.moveTo(pad.l, y);
    x.lineTo(w - pad.r, y);
    x.stroke();
    x.fillText(money(mx * (1 - i / 4)).replace(",00", ""), 4, y + 4);
  }
  const step = (w - pad.l - pad.r) / Math.max(labels.length, 1),
    bw = step * 0.62;
  vals.forEach((v, i) => {
    let bh = ((h - pad.t - pad.b) * v) / mx,
      xx = pad.l + (i + 0.19) * step,
      yy = h - pad.b - bh;
    x.fillStyle = chartColors()[0];
    x.fillRect(xx, yy, bw, bh);
    regions.push({
      x1: xx,
      x2: xx + bw,
      y1: yy,
      y2: h - pad.b,
      label: labels[i],
      value: v,
    });
    x.fillStyle = "#5d6778";
    x.textAlign = "center";
    x.fillText(labels[i], xx + bw / 2, h - 22);
  });
  x.textAlign = "left";
  bindChartTooltip(c, (px, py) => {
    const r = regions.find(
      (r) => px >= r.x1 && px <= r.x2 && py >= r.y1 && py <= r.y2,
    );
    return r ? `${r.label}\nBs ${money(r.value)}` : null;
  });
}

function drawStackedBars(c, labels, a, b, names) {
  const [x, w, h] = prep(c),
    pad = { l: 55, r: 15, t: 20, b: 65 },
    regions = [];
  const totals = a.map((v, i) => v + b[i]),
    mx = Math.max(...totals, 1);
  x.strokeStyle = "#e2e7ee";
  x.fillStyle = "#6b7688";
  x.font = "10px system-ui";
  for (let i = 0; i < 5; i++) {
    let y = pad.t + ((h - pad.t - pad.b) * i) / 4;
    x.beginPath();
    x.moveTo(pad.l, y);
    x.lineTo(w - pad.r, y);
    x.stroke();
    x.fillText(money(mx * (1 - i / 4)).replace(",00", ""), 4, y + 4);
  }
  const step = (w - pad.l - pad.r) / Math.max(labels.length, 1),
    bw = step * 0.56;
  a.forEach((v, i) => {
    let h1 = ((h - pad.t - pad.b) * v) / mx,
      h2 = ((h - pad.t - pad.b) * b[i]) / mx,
      xx = pad.l + i * step + step * 0.22;
    const bottom = h - pad.b;
    x.fillStyle = chartColors()[0];
    x.fillRect(xx, bottom - h1, bw, h1);
    if (v > 0)
      regions.push({
        x1: xx,
        x2: xx + bw,
        y1: bottom - h1,
        y2: bottom,
        label: labels[i],
        series: names[0],
        value: v,
      });
    x.fillStyle = chartColors()[1];
    x.fillRect(xx, bottom - h1 - h2, bw, h2);
    if (b[i] > 0)
      regions.push({
        x1: xx,
        x2: xx + bw,
        y1: bottom - h1 - h2,
        y2: bottom - h1,
        label: labels[i],
        series: names[1],
        value: b[i],
      });
    x.save();
    x.translate(xx + bw / 2, h - 8);
    x.rotate(-0.55);
    x.fillStyle = "#5d6778";
    x.textAlign = "right";
    x.fillText(labels[i], 0, 0);
    x.restore();
  });
  x.fillStyle = chartColors()[0];
  x.fillRect(w - 180, 5, 10, 10);
  x.fillStyle = "#5d6778";
  x.fillText(names[0], w - 166, 14);
  x.fillStyle = chartColors()[1];
  x.fillRect(w - 95, 5, 10, 10);
  x.fillStyle = "#5d6778";
  x.fillText(names[1], w - 81, 14);
  bindChartTooltip(c, (px, py) => {
    const r = regions.find(
      (r) => px >= r.x1 && px <= r.x2 && py >= r.y1 && py <= r.y2,
    );
    return r ? `${r.label}\n${r.series}: Bs ${money(r.value)}` : null;
  });
}

function drawDonut(c, data) {
  const [x, w, h] = prep(c),
    total = data.reduce((s, d) => s + d[1], 0) || 1,
    cx = w * 0.38,
    cy = h * 0.49,
    r = Math.min(w, h) * 0.31,
    inner = r * 0.58,
    segments = [];
  let a = -Math.PI / 2;
  data.forEach((d, i) => {
    let ang = (Math.PI * 2 * d[1]) / total;
    segments.push({ start: a, end: a + ang, label: d[0], value: d[1] });
    x.beginPath();
    x.moveTo(cx, cy);
    x.arc(cx, cy, r, a, a + ang);
    x.closePath();
    x.fillStyle = chartColors()[i];
    x.fill();
    a += ang;
  });
  x.beginPath();
  x.arc(cx, cy, inner, 0, Math.PI * 2);
  x.fillStyle = "#fff";
  x.fill();
  x.fillStyle = "#273448";
  x.font = "700 15px system-ui";
  x.textAlign = "center";
  x.fillText("Bs", cx, cy - 3);
  x.font = "700 13px system-ui";
  x.fillText(money(total), cx, cy + 17);
  x.textAlign = "left";
  data.forEach((d, i) => {
    const y = 75 + i * 34;
    x.fillStyle = chartColors()[i];
    x.fillRect(w * 0.7, y - 10, 10, 10);
    x.fillStyle = "#4f5d70";
    x.font = "11px system-ui";
    x.fillText(d[0], w * 0.7 + 16, y);
    x.fillStyle = "#172033";
    x.font = "700 11px system-ui";
    x.fillText(money(d[1]), w * 0.7 + 16, y + 15);
  });
  bindChartTooltip(c, (px, py) => {
    const dx = px - cx,
      dy = py - cy,
      dist = Math.hypot(dx, dy);
    if (dist < inner || dist > r) return null;
    let ang = Math.atan2(dy, dx);
    if (ang < -Math.PI / 2) ang += Math.PI * 2;
    const s = segments.find(
      (s) => ang >= s.start && ang <= s.end && s.value > 0,
    );
    if (!s) return null;
    const pct = ((s.value / total) * 100).toLocaleString("es-BO", {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    });
    return `${s.label}\nBs ${money(s.value)}\n${pct}% del total`;
  });
}

function drawLine(c, labels, vals) {
  const [x, w, h] = prep(c),
    pad = { l: 55, r: 20, t: 20, b: 45 },
    mx = Math.max(...vals, 1),
    points = [];
  x.strokeStyle = "#e2e7ee";
  x.fillStyle = "#6b7688";
  x.font = "10px system-ui";
  for (let i = 0; i < 5; i++) {
    let y = pad.t + ((h - pad.t - pad.b) * i) / 4;
    x.beginPath();
    x.moveTo(pad.l, y);
    x.lineTo(w - pad.r, y);
    x.stroke();
    x.fillText(money(mx * (1 - i / 4)).replace(",00", ""), 4, y + 4);
  }
  if (!vals.length) {
    x.fillStyle = "#7a8595";
    x.fillText("Sin cobros registrados", pad.l + 20, h / 2);
    bindChartTooltip(c, () => null);
    return;
  }
  x.strokeStyle = chartColors()[0];
  x.lineWidth = 2;
  x.beginPath();
  vals.forEach((v, i) => {
    let xx = pad.l + (i * (w - pad.l - pad.r)) / Math.max(vals.length - 1, 1),
      yy = h - pad.b - ((h - pad.t - pad.b) * v) / mx;
    points.push({ x: xx, y: yy, label: labels[i], value: v });
    i ? x.lineTo(xx, yy) : x.moveTo(xx, yy);
  });
  x.stroke();
  points.forEach((p, i) => {
    x.beginPath();
    x.arc(p.x, p.y, 3.5, 0, Math.PI * 2);
    x.fillStyle = chartColors()[0];
    x.fill();
    if (i % Math.ceil(vals.length / 8) === 0) {
      x.fillStyle = "#5d6778";
      x.textAlign = "center";
      x.fillText(labels[i], p.x, h - 22);
    }
  });
  x.textAlign = "left";
  bindChartTooltip(c, (px, py) => {
    let best = null,
      bestDist = Infinity;
    points.forEach((p) => {
      const d = Math.hypot(px - p.x, py - p.y);
      if (d < bestDist) {
        bestDist = d;
        best = p;
      }
    });
    return best && bestDist <= 12
      ? `${best.label}\nBs ${money(best.value)}`
      : null;
  });
}
window.addEventListener("resize", () => {
  if (state.summary) renderDashboard();
});
refreshAll();
