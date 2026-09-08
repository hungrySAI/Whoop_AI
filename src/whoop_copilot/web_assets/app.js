"use strict";

// All values come from the local application service. This adapter only formats them.
const $ = (id) => document.getElementById(id);
const state = { days: 7, metric: "hrv", section: "overview", cycles: null, cycleRevision: null, cyclePage: 1, cycleRequest: 0, weekly: null, week: null, weeklyRequest: 0, weeklyMetric: "recovery", weeklyPage: 1, weeklyRecords: null, weeklyRecordsRequest: 0, journal: null, journalPage: 1, journalRequest: 0, view: null, status: null, page: 0, chart: null, request: 0, detail: 0, timer: null, freshnessTimer: null, statusReceived: 0, notices: {} };
const number = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
const date = new Intl.DateTimeFormat("zh-CN", { timeZone: "UTC", month: "2-digit", day: "2-digit" });
const dateTime = new Intl.DateTimeFormat("zh-CN", { timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
const fmt = (value) => value == null ? "—" : number.format(value);
const difference = (value, label) => value !== 0 && Math.abs(value) < 0.1 ? `${value > 0 ? "高" : "低"}不足 0.1 ${label}` : `${value > 0 ? "+" : ""}${fmt(value)} ${label}`;
const time = (value, full = true) => value ? (full ? dateTime : date).format(new Date(value)) : "—";
const unit = (value) => value === "score" ? "/ 21" : value || "";
const csrf = document.querySelector('meta[name="whoop-csrf"]').content;
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function notice(message = "", kind = "view") {
  if (message) state.notices[kind] = message; else delete state.notices[kind];
  $("notice").textContent = Object.values(state.notices).join(" ");
  $("notice").hidden = !$("notice").textContent;
}
async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.error || "本地服务没有完成请求，请重试。");
  return body;
}
function sourceButton(key, revision) {
  const button = node("button", "来源 ↗", "source-button");
  button.setAttribute("aria-label", "查看这条观测的来源");
  button.addEventListener("click", () => showEvidence(key, revision));
  return button;
}
function renderCards(cards, briefing) {
  $("daily-headline").textContent = briefing?.headline || "最近已采集的官方记录";
  $("daily-as-of").textContent = briefing ? `截至 ${time(briefing.as_of)} UTC` : "";
  const midpoint = briefing?.items[0]?.comparison.midpoint;
  $("brief-window").textContent = briefing ? `近 ${briefing.days} 天：${time(briefing.start)} 至 ${time(briefing.end)} UTC，按时间等分；分界为 ${time(midpoint)} UTC。` : "";
  $("cards").replaceChildren(...cards.map((card) => {
    const brief = briefing?.items.find((item) => item.key === card.key);
    const article = node("article", undefined, "metric-card");
    const top = node("div", undefined, "card-top");
    top.append(node("span", card.label), node("span", "WHOOP", "official-label"));
    const value = node("strong", fmt(card.latest?.value));
    value.append(node("small", card.unit_label));
    const meta = node("div", undefined, "card-meta");
    const stamp = node("time", card.latest ? time(card.latest.measured_at) + " UTC" : "当前窗口");
    if (card.latest) stamp.dateTime = card.latest.measured_at;
    meta.append(stamp);
    if (card.latest) meta.append(sourceButton(card.key, card.latest.revision_id));
    article.append(top, value, node("p", card.latest?.status_label || "无已采集记录", "status-label"), meta);
    if (brief) {
      article.append(node("p", brief.recency === "today" ? "今日记录 · UTC" : brief.recency === "earlier" ? "此前记录 · 不代表今天" : "没有记录时不补零", "brief-recency"));
      if (brief.context) article.append(node("p", brief.context, "brief-context"));
      const comparison = brief.comparison, change = node("div", undefined, "brief-change");
      change.dataset.state = comparison.status;
      change.append(node("span", `近 ${briefing.days} 天 · 均值变化`, "muted"), node("p", comparison.statement, "brief-statement"), node("small", `前段 ${comparison.first_half.count} 条 / 后段 ${comparison.second_half.count} 条有效观测`));
      const link = node("button", "查看趋势与依据 ↗", "text-button brief-link");
      link.setAttribute("aria-label", `查看${card.label}趋势与依据`);
      link.addEventListener("click", async () => {
        state.metric = card.key;
        await loadView();
        if (state.section === "overview" && state.view?.trend.key === card.key) $("trend-title").focus();
      });
      change.append(link); article.append(change);
    }
    return article;
  }));
  $("cards").setAttribute("aria-busy", "false");
}
function renderPicker(metrics) {
  $("metric-picker").replaceChildren(...metrics.map((metric) => {
    const button = node("button", metric.label);
    button.setAttribute("aria-pressed", String(metric.key === state.metric));
    button.addEventListener("click", () => { state.metric = metric.key; loadView(); });
    return button;
  }));
}
function renderChart(trend, view) {
  if (state.chart) { state.chart.destroy(); state.chart = null; }
  const coverage = trend.coverage;
  const empty = coverage.state !== "has_valid_observations";
  $("chart-empty").hidden = !empty;
  $("trend-chart").hidden = empty;
  $("chart-legend").hidden = empty;
  $("chart-empty-title").textContent = coverage.state === "no_records" ? "当前窗口没有已采集记录" : "已有记录，暂无有效观测";
  $("chart-empty-note").textContent = coverage.state === "no_records"
    ? trend.resource === "workout" ? "无训练记录不代表同步失败，也不计作零负荷。" : "所选范围可能早于已有历史；同步结果请查看「数据连接」。"
    : "具体状态见「观测与来源」；这些记录不参与均值和变化计算。";
  $("trend-chart").setAttribute("aria-label", `${trend.label}，所选 ${view.days} 天窗口内，${coverage.record_days} 个 UTC 日期有记录，${coverage.valid_observation_count} 条有效观测。下方表格提供来源。`);
  if (empty) return;
  const data = trend.points.map((point) => ({ x: Date.parse(point.measured_at), y: point.value, source: point }));
  state.chart = new Chart($("trend-chart"), {
    type: "line",
    data: { datasets: [{ data, borderColor: "#a4efcd", backgroundColor: "#a4efcd", borderWidth: 2, pointRadius: 3.5, pointHoverRadius: 6, pointHitRadius: 12, tension: 0, spanGaps: false }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? false : { duration: 180 },
      locale: "zh-CN", interaction: { intersect: true },
      onClick: (_event, elements) => {
        if (elements.length) {
          const point = data[elements[0].index].source;
          if (point.revision_id) showEvidence(trend.key, point.revision_id);
        }
      },
      plugins: {
        legend: { display: false },
        tooltip: { displayColors: false, padding: 12, backgroundColor: "#253440", titleColor: "#edf4f5", bodyColor: "#edf4f5", callbacks: {
          title: (items) => time(items[0].raw.source.measured_at) + " UTC",
          label: (item) => `${trend.label}  ${fmt(item.raw.y)} ${trend.unit_label}`,
          afterLabel: () => "点击查看来源",
        } },
      },
      scales: {
        x: { type: "linear", min: Date.parse(view.start), max: Date.parse(view.end), grid: { display: false }, border: { display: false }, ticks: { color: "#97a9b7", maxTicksLimit: view.days === 7 ? 7 : 6, font: { size: 12 }, callback: (value) => date.format(new Date(value)) } },
        y: { grace: "15%", grid: { color: "#27343f" }, border: { display: false }, ticks: { color: "#97a9b7", maxTicksLimit: 5, padding: 10, font: { size: 12 } } },
      },
    },
  });
}
function renderRows() {
  const trend = state.view.trend;
  const rows = [...trend.records].reverse();
  const pages = Math.max(1, Math.ceil(rows.length / 30));
  state.page = Math.min(Math.max(0, state.page), pages - 1);
  $("records").replaceChildren(...rows.slice(state.page * 30, (state.page + 1) * 30).map((record) => {
    const tr = node("tr");
    const source = node("td"); source.append(sourceButton(trend.key, record.revision_id));
    tr.append(node("td", time(record.measured_at)), node("td", `${fmt(record.value)} ${trend.unit_label}`), node("td", record.status_label), source);
    return tr;
  }));
  if (!rows.length) {
    const row = node("tr"), cell = node("td", "当前窗口没有已采集记录。");
    cell.colSpan = 4; row.append(cell); $("records").append(row);
  }
  $("record-count").textContent = `${rows.length} 条`;
  $("page-label").textContent = `${state.page + 1} / ${pages}`;
  $("previous-page").disabled = state.page === 0;
  $("next-page").disabled = state.page === pages - 1;
}
async function loadView() {
  const request = ++state.request;
  const days = state.days, metric = state.metric;
  $("cards").setAttribute("aria-busy", "true");
  try {
    const view = await api(`/api/dashboard?days=${days}&metric=${encodeURIComponent(metric)}`);
    if (request !== state.request || days !== state.days || state.section !== "overview") return;
    state.view = view; state.page = 0;
    renderCards(view.cards, view.briefing); renderPicker(view.metrics);
    document.querySelectorAll("[data-days]").forEach((button) => button.setAttribute("aria-pressed", String(Number(button.dataset.days) === days)));
    $("environment").textContent = view.environment === "real" ? "本机加密数据" : "合成演示数据";
    if (state.section === "overview") $("window-label").textContent = `查看范围 ${time(view.start, false)} — ${time(view.end, false)} · ${days} 个 UTC 日期，含今天`;
    const trend = view.trend, summary = trend.summary;
    $("mean-label").textContent = `${trend.label} · 有效观测均值`;
    $("mean-value").textContent = fmt(summary.mean);
    $("mean-unit").textContent = trend.unit_label;
    $("change-value").textContent = summary.status !== "descriptive" || summary.change == null ? "数据不足" : difference(summary.change, trend.unit_label === "%" ? "百分点" : trend.unit_label === "/ 21" ? "分" : trend.unit_label);
    $("change-note").textContent = `前段 ${summary.first_half.count} 条 / 后段 ${summary.second_half.count} 条${summary.status === "insufficient_data" ? " · 样本不足" : ""}`;
    const coverage = trend.coverage;
    $("coverage-range").textContent = coverage.record_count
      ? `窗口内记录日期 ${time(coverage.first_record_at, false)} — ${time(coverage.last_record_at, false)} · UTC`
      : "窗口内记录日期 —";
    $("coverage").textContent = `${coverage.record_days} 个日期有记录 · ${coverage.record_count} 条记录 · ${coverage.valid_observation_count} 条有效观测`;
    $("coverage-states").textContent = coverage.states.filter((item) => item.status !== "valid").map((item) => `${item.label} ${item.count} 条`).join(" · ");
    $("coverage-states").hidden = !$("coverage-states").textContent;
    $("view-time").textContent = `视图更新 ${time(view.generated_at)} UTC`;
    renderRows(); renderChart(trend, view); notice();
  } catch (error) {
    if (request === state.request && state.section === "overview") {
      if (state.view) {
        state.days = state.view.days; state.metric = state.view.trend.key;
        document.querySelectorAll("[data-days]").forEach((button) => button.setAttribute("aria-pressed", String(Number(button.dataset.days) === state.view.days)));
        $("window-label").textContent = `查看范围 ${time(state.view.start, false)} — ${time(state.view.end, false)} · ${state.view.days} 个 UTC 日期，含今天`;
      }
      if (state.status) renderStatus(state.status);
      notice(error.message); $("cards").setAttribute("aria-busy", "false");
    }
  }
}
async function showEvidence(key, revision) {
  $("evidence-value").hidden = false;
  const request = ++state.detail;
  const dialog = $("evidence-dialog");
  if (!dialog.open) dialog.showModal();
  $("evidence-title").textContent = "观测来源";
  $("evidence-state").textContent = "正在读取…";
  $("evidence-value").textContent = ""; $("evidence-fields").replaceChildren(); $("evidence-note").textContent = "";
  try {
    const record = await api(`/api/evidence?metric=${encodeURIComponent(key)}&revision=${revision}`);
    if (request !== state.detail) return;
    $("evidence-title").textContent = record.label;
    $("evidence-state").textContent = `${record.status_label} · ${record.is_current ? "当前来源版本" : "历史来源版本"}`;
    $("evidence-value").textContent = `${fmt(record.value)} ${record.unit_label}`;
    const fields = [
      ["数据来源", `${record.source} · ${record.resource}`],
      ["记录开始 / 测量归属 · UTC", time(record.measured_at)],
      ["记录结束 · UTC", record.measured_end ? time(record.measured_end) : "尚未结束 / 未提供"],
      ["来源时区偏移", record.source_timezone || "未提供"],
      ["WHOOP 指标记录更新时间 · UTC", time(record.metric_updated_at)],
      ...(record.resource === "recovery" ? [
        ["关联周期更新时间 · UTC", time(record.interval_updated_at)],
        ["组合来源版本时间 · UTC", time(record.source_updated_at)],
      ] : []),
      ["本机采集时间 · UTC", time(record.captured_at)],
      ["本机获知版本时间 · UTC", time(record.known_at)],
      ["保留到 · UTC", record.expires_at ? time(record.expires_at) : "合成数据，无真实数据保留期限"],
      ["官方原值", record.original_value == null ? "未提供" : `${fmt(record.original_value)} ${unit(record.original_unit)}${record.status !== "valid" ? "（不参与统计）" : ""}`],
    ];
    $("evidence-fields").replaceChildren(...fields.map(([label, value]) => { const row = node("div"); row.append(node("dt", label), node("dd", value)); return row; }));
    $("evidence-note").textContent = record.interpretation;
  } catch (error) { if (request === state.detail) $("evidence-state").textContent = error.message; }
}
function renderJournal(view) {
  $("window-label").textContent = `查看日期 ${view.start_date} — ${view.end_date} · 按日志所标日期筛选`;
  document.querySelectorAll("[data-days]").forEach((button) => button.setAttribute("aria-pressed", String(Number(button.dataset.days) === state.days)));
  $("environment").textContent = view.environment === "real" ? "本机加密数据" : "合成演示数据";
  $("journal-import-state").textContent = `当前窗口 ${view.total} 条日志 · WHOOP 官方导出`;
  $("journal-import-time").textContent = view.latest_export_at ? `现存日志最近导出：${time(view.latest_export_at)} UTC · 最近导入：${time(view.latest_import_at)} UTC` : "尚无日志导入记录";
  $("journal-unmapped").hidden = !view.unmapped_total;
  $("journal-unmapped").textContent = `另有 ${view.unmapped_total} 条日志尚未配置日期与问答映射，需在本机重新核对导入。`;
  $("journal-empty").hidden = view.total > 0;
  const empty = {
    no_import: ["还没有导入 Journal", "已有的 WHOOP Journal 可通过官方导出接入。"],
    mapping_required: ["日志已保存，等待核对映射", "完成日期、时区与问答列映射后，即可在这里查看。"],
    outside_window: ["所选日期没有日志", "可以切换 7/30 天，或导入较新的官方导出。"],
  }[view.state];
  $("journal-empty-title").textContent = empty?.[0] || "";
  $("journal-empty-note").textContent = empty?.[1] || "";
  const groups = new Map();
  view.entries.forEach((entry) => {
    const key = `${entry.date}|${entry.date_basis}|${entry.timezone}|${entry.day_start}|${entry.day_end}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(entry);
  });
  $("journal-entries").replaceChildren(...Array.from(groups.values()).map((entries) => {
    const first = entries[0], group = node("article", undefined, "journal-day");
    const heading = node("div", undefined, "journal-day-heading");
    heading.append(node("h3", first.date.replaceAll("-", "/")), node("span", `${first.date_label} · ${first.timezone}`, "muted"));
    const metrics = node("div", undefined, "journal-metrics");
    first.metrics.forEach((metric) => {
      const card = node("div", undefined, "journal-metric");
      card.append(node("span", metric.label, "muted"), node("strong", `${fmt(metric.latest?.value)} ${metric.unit_label}`), node("small", metric.latest ? `${metric.latest.status_label} · ${metric.record_count} 条中最近一条` : "无 API 记录", "muted"));
      if (metric.latest) card.append(sourceButton(metric.key, metric.latest.revision_id));
      metrics.append(card);
    });
    group.append(heading, metrics);
    entries.forEach((entry) => {
      const row = node("div", undefined, "journal-row"), answers = node("dl", undefined, "journal-answers");
      entry.answers.forEach((item) => {
        const pair = node("div");
        pair.append(node("dt", item.question), node("dd", item.answer == null ? "未填写" : item.answer, item.answer == null ? "muted" : undefined));
        answers.append(pair);
      });
      const source = node("button", "日志来源 ↗", "source-button");
      source.addEventListener("click", () => showJournalEvidence(entry.revision_id));
      row.append(answers, source); group.append(row);
    });
    return group;
  }));
  $("journal-pagination").hidden = view.pages < 2;
  $("journal-page").textContent = `${view.page} / ${view.pages} · 共 ${view.total} 条`;
  $("journal-previous").disabled = view.page <= 1; $("journal-next").disabled = view.page >= view.pages;
  $("view-time").textContent = `视图更新 ${time(view.generated_at)} UTC`;
}
async function loadJournal() {
  const request = ++state.journalRequest, days = state.days;
  $("journal-panel").setAttribute("aria-busy", "true"); $("refresh-journal").disabled = true;
  $("journal-previous").disabled = true; $("journal-next").disabled = true;
  try {
    const view = await api(`/api/journal?days=${days}&page=${state.journalPage}`);
    if (request !== state.journalRequest || days !== state.days || state.section !== "journal") return;
    state.journal = view; state.journalPage = view.page; renderJournal(view); notice("", "journal");
  } catch (error) {
    if (request === state.journalRequest && state.section === "journal") {
      if (state.journal) { state.days = state.journal.days; state.journalPage = state.journal.page; renderJournal(state.journal); }
      if (state.status) renderStatus(state.status);
      notice(error.message, "journal");
    }
  } finally {
    if (request === state.journalRequest) {
      $("journal-panel").setAttribute("aria-busy", "false"); $("refresh-journal").disabled = false;
      $("journal-previous").disabled = !state.journal || state.journal.page <= 1;
      $("journal-next").disabled = !state.journal || state.journal.page >= state.journal.pages;
    }
  }
}
async function showJournalEvidence(revision) {
  const request = ++state.detail;
  $("evidence-dialog").showModal(); $("evidence-title").textContent = "Journal 来源";
  $("evidence-state").textContent = "正在读取…"; $("evidence-value").hidden = true;
  $("evidence-fields").replaceChildren(); $("evidence-note").textContent = "";
  try {
    const entry = await api(`/api/journal/evidence?revision=${revision}`);
    if (request !== state.detail) return;
    $("evidence-state").textContent = entry.source;
    const fields = [
      [entry.date_label, entry.date], ["来源时区", entry.timezone],
      ["时间精度", entry.time_precision === "date" ? "仅日期；未提供具体时刻" : "来源时间点"],
      ...(entry.source_at ? [["来源时间点 · UTC", time(entry.source_at)]] : []),
      ["导出时间 · UTC", time(entry.exported_at)], ["本机导入时间 · UTC", time(entry.imported_at)],
      ["保留到 · UTC", entry.expires_at ? time(entry.expires_at) : "合成环境"],
      ...entry.answers.map((item) => [item.question, item.answer == null ? "未填写" : item.answer]),
    ];
    $("evidence-fields").replaceChildren(...fields.map(([label, value]) => { const pair = node("div"); pair.append(node("dt", label), node("dd", value)); return pair; }));
    $("evidence-note").textContent = `${entry.association} 导出时间用于区分文件版本，不是 WHOOP 的记录修改时间。`;
  } catch (error) { if (request === state.detail) $("evidence-state").textContent = error.message; }
}
function weeklyControls(busy = false) {
  const options = state.weekly?.choices || [], index = options.findIndex(item => item.week === state.week);
  $("weekly-choice").disabled = busy || !options.length;
  $("weekly-refresh").disabled = busy;
  $("weekly-older").disabled = busy || index < 0 || index >= options.length - 1;
  $("weekly-newer").disabled = busy || index <= 0;
  document.querySelectorAll('#weekly-metrics button').forEach(button => { button.disabled = busy; });
}
function periodCell(period, label, unitLabel) {
  const cell = node("div", undefined, "weekly-period"), coverage = period.coverage;
  cell.append(node("span", label, "weekly-mobile-label"), node("strong", `${fmt(period.mean)} ${unitLabel}`), node("small", `${coverage.valid_observation_count} 条有效 · ${coverage.record_days} 个记录日`));
  const excluded = coverage.states.filter(item => item.status !== "valid").map(item => `${item.label} ${item.count} 条`).join(" · ");
  if (excluded) cell.append(node("small", excluded));
  if (period.open_cycles) cell.append(node("small", `含 ${period.open_cycles} 条未结束周期`));
  return cell;
}
function renderWeeklyMetric(metric) {
  const row = node("article", undefined, "weekly-metric"); row.dataset.metric = metric.key;
  const label = node("div", undefined, "weekly-metric-label"), button = node("button", "查看记录与来源 ↗", "text-button");
  button.setAttribute("aria-label", `查看${metric.label}周记录与来源`);
  button.addEventListener("click", () => { state.weeklyMetric = metric.key; state.weeklyPage = 1; state.weeklyRecords = null; loadWeeklyRecords(true); });
  label.append(node("h3", metric.label), button);
  const comparison = node("div", undefined, "weekly-comparison");
  comparison.append(node("span", "所选周 − 前一周", "weekly-mobile-label"));
  comparison.append(node("strong", metric.comparison.state === "week_in_progress" ? "本周进行中" : metric.comparison.change == null ? "样本不足" : difference(metric.comparison.change, metric.comparison.unit_label)));
  comparison.append(node("small", metric.comparison.state === "week_in_progress" ? "暂不与完整前周比较" : "本应用描述统计"));
  row.append(label, periodCell(metric.selected, "所选周", metric.unit_label), periodCell(metric.previous, "前一周", metric.unit_label), comparison);
  return row;
}
function renderWeekly(view) {
  $("weekly-choice").replaceChildren(...view.choices.map((choice) => { const option = node("option", `${choice.partial ? "本周 · " : ""}${choice.week} — ${choice.last_date}`); option.value = choice.week; option.selected = choice.week === view.week; return option; }));
  $("window-label").textContent = `所选周 ${view.week} — ${view.choices.find(item => item.week === view.week).last_date} · UTC`;
  $("weekly-state").textContent = view.partial ? `本周进行中 · 截至 ${time(view.end)} UTC，暂不作周间比较。` : "日历周已结束 · 下列均值仅代表本机已采集的有效观测。";
  $("weekly-metrics").replaceChildren(...view.metrics.map(renderWeeklyMetric));
  $("view-time").textContent = `视图更新 ${time(view.as_of)} UTC`;
  $("environment").textContent = view.environment === "real" ? "本机加密数据" : "合成演示数据";
  weeklyControls();
}
async function loadWeekly() {
  const request = ++state.weeklyRequest, week = state.week;
  state.weeklyRecordsRequest++; $("weekly-detail").hidden = true; state.weeklyRecords = null;
  weeklyControls(true); $("weekly-panel").setAttribute("aria-busy", "true");
  try {
    const view = await api(`/api/weekly${week ? `?week=${encodeURIComponent(week)}` : ""}`);
    if (request !== state.weeklyRequest || week !== state.week || state.section !== "weekly") return;
    state.weekly = view; state.week = view.week; renderWeekly(view); notice("", "weekly"); notice("", "weekly-records");
  } catch (error) {
    if (request === state.weeklyRequest && state.section === "weekly") {
      if (state.weekly) { state.week = state.weekly.week; renderWeekly(state.weekly); }
      else { $("window-label").textContent = "周回顾暂不可用"; $("weekly-state").textContent = "读取未完成，可刷新重试。"; }
      notice(error.message, "weekly");
    }
  } finally { if (request === state.weeklyRequest) { weeklyControls(); $("weekly-panel").setAttribute("aria-busy", "false"); } }
}
function renderWeeklyRecords(view) {
  state.weekly = view; renderWeekly(view);
  $("weekly-detail-title").textContent = `${view.metric.label} · 观测与来源`;
  $("weekly-detail-note").textContent = `所选周从 ${view.week} 开始 · 两周共 ${view.total} 条记录；按当前保留版本读取。`;
  $("weekly-records").replaceChildren(...view.records.map(record => {
    const row = node("tr"), source = node("td"); source.append(sourceButton(view.metric.key, record.revision_id));
    row.append(node("td", record.period === "selected" ? "所选周" : "前一周"), node("td", time(record.measured_at)), node("td", `${fmt(record.value)} ${view.metric.unit_label}`), node("td", record.status_label), source); return row;
  }));
  if (!view.records.length) { const row = node("tr"), cell = node("td", "这两周没有已采集的该指标记录。"); cell.colSpan = 5; row.append(cell); $("weekly-records").append(row); }
  $("weekly-records-page").textContent = `${view.page} / ${view.pages} · 共 ${view.total} 条`;
  $("weekly-records-previous").disabled = view.page <= 1; $("weekly-records-next").disabled = view.page >= view.pages;
}
async function loadWeeklyRecords(focus = false) {
  if ($("weekly-panel").getAttribute("aria-busy") === "true") return;
  const request = ++state.weeklyRecordsRequest, week = state.week, key = state.weeklyMetric;
  $("weekly-detail").hidden = false; $("weekly-detail").setAttribute("aria-busy", "true");
  $("weekly-records-previous").disabled = true; $("weekly-records-next").disabled = true;
  if (!state.weeklyRecords || state.weeklyRecords.week !== week || state.weeklyRecords.metric.key !== key) { $("weekly-records").replaceChildren(); $("weekly-detail-title").textContent = "正在读取观测与来源…"; $("weekly-detail-note").textContent = ""; $("weekly-records-page").textContent = ""; }
  try {
    const view = await api(`/api/weekly/records?week=${encodeURIComponent(week)}&metric=${key}&page=${state.weeklyPage}`);
    if (request !== state.weeklyRecordsRequest || state.section !== "weekly" || week !== state.week || key !== state.weeklyMetric) return;
    state.weeklyRecords = view; state.weeklyPage = view.page; renderWeeklyRecords(view); notice("", "weekly-records");
    if (focus) $("weekly-detail-title").focus();
  } catch (error) {
    if (request === state.weeklyRecordsRequest && state.section === "weekly") {
      if (state.weeklyRecords?.week === week && state.weeklyRecords.metric.key === key) { state.weeklyPage = state.weeklyRecords.page; renderWeeklyRecords(state.weeklyRecords); }
      else $("weekly-detail-title").textContent = "记录暂不可用，可点击对应指标重试";
      notice(error.message, "weekly-records");
    }
  } finally { if (request === state.weeklyRecordsRequest) $("weekly-detail").setAttribute("aria-busy", "false"); }
}
function cycleControls(busy = false) {
  $("cycles-refresh").disabled = busy;
  $("cycles-previous").disabled = busy || !state.cycles || state.cycles.page <= 1;
  $("cycles-next").disabled = busy || !state.cycles || state.cycles.page >= state.cycles.pages;
  document.querySelectorAll('#cycles-list button, #cycle-groups button').forEach(button => { button.disabled = busy; });
}
function renderCycleDetail(entry, focus = false) {
  $("cycle-detail").hidden = !entry;
  if (!entry) { $("cycle-groups").replaceChildren(); return; }
  state.cycleRevision = entry.revision_id;
  document.querySelectorAll('#cycles-list button').forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.revision) === entry.revision_id)));
  $("cycle-detail-title").textContent = `${time(entry.start)} UTC · 周期回看`;
  $("cycle-interval").textContent = `周期区间：${time(entry.start)} — ${entry.end ? time(entry.end) : "尚未结束"} UTC · 来源时区 ${entry.source_timezone || "未提供"}${entry.open ? " · 周期仍在进行，当前负荷可能更新" : ""}`;
  $("cycle-groups").replaceChildren(...[["sleep", "恢复关联睡眠"], ["recovery", "本周期恢复"], ["cycle", "本周期负荷"]].map(([key, label]) => {
    const group = entry[key], card = node("article", undefined, "cycle-group");
    card.append(node("h4", label));
    if (key === "sleep" && group.state === "linked") {
      card.append(node("p", `${group.nap ? "WHOOP 标记为小睡" : "WHOOP 标记为主睡眠"} · ${time(group.start)} — ${time(group.end)} UTC · 来源时区 ${group.source_timezone || "未提供"}`, "cycle-group-note"));
    } else if (group.state !== "linked") card.append(node("p", group.state_label, "cycle-group-note"));
    const metrics = node("div", undefined, "cycle-values");
    group.metrics.forEach(metric => {
      const item = node("div", undefined, "cycle-value"), title = node("div", undefined, "cycle-value-title");
      title.append(node("span", metric.label), sourceButton(metric.key, metric.revision_id));
      item.append(title, node("strong", `${fmt(metric.value)} ${metric.unit_label}`), node("small", metric.status_label));
      metrics.append(item);
    });
    card.append(metrics); return card;
  }));
  if (focus) $("cycle-detail-title").focus();
}
function renderCycles(view) {
  $("window-label").textContent = `周期起点 ${time(view.start, false)} — ${time(view.end, false)} · ${view.days} 个 UTC 日期，含今天`;
  document.querySelectorAll('[data-days]').forEach(button => button.setAttribute('aria-pressed', String(Number(button.dataset.days) === view.days)));
  $("environment").textContent = view.environment === "real" ? "本机加密数据" : "合成演示数据";
  $("cycles-coverage").textContent = view.total ? `当前范围内有 ${view.total} 个已采集周期 · 每页最多 ${view.page_size} 个` : "所选范围没有已采集的周期。可切换窗口查看已有记录。";
  $("cycles-list").replaceChildren(...view.entries.map(entry => {
    const button = node("button", undefined, "cycle-row"); button.dataset.revision = entry.revision_id;
    button.setAttribute('aria-label', `查看 ${time(entry.start)} UTC 的恢复记录`);
    const stamp = node("span", undefined, "cycle-row-date"); stamp.append(node("strong", time(entry.start)), node("small", entry.open ? "UTC · 周期进行中" : "UTC · 周期起点")); button.append(stamp);
    [["recovery", "恢复"], ["sleep", "关联睡眠"], ["cycle", "负荷"]].forEach(([key, label]) => {
      const metric = entry[key].metrics[0], cell = node("span", undefined, "cycle-row-metric");
      cell.append(node("small", label), node("strong", metric ? `${fmt(metric.value)} ${metric.unit_label}` : "—"), node("small", metric?.status_label || "关联暂不可用")); button.append(cell);
    });
    button.addEventListener('click', () => renderCycleDetail(entry, true)); return button;
  }));
  $("cycles-pagination").hidden = view.pages < 2;
  $("cycles-page").textContent = `${view.page} / ${view.pages}`;
  renderCycleDetail(view.entries.find(entry => entry.revision_id === state.cycleRevision) || view.entries[0]);
  $("view-time").textContent = `视图更新 ${time(view.generated_at)} UTC`;
  cycleControls();
}
async function loadCycles() {
  const request = ++state.cycleRequest, days = state.days;
  $("cycles-panel").setAttribute('aria-busy', 'true'); cycleControls(true);
  try {
    const view = await api(`/api/cycles?days=${days}&page=${state.cyclePage}`);
    if (request !== state.cycleRequest || days !== state.days || state.section !== 'cycles') return;
    state.cycles = view; state.cyclePage = view.page; renderCycles(view); notice('', 'cycles');
  } catch (error) {
    if (request === state.cycleRequest && state.section === 'cycles') {
      if (state.cycles) { state.days = state.cycles.days; state.cyclePage = state.cycles.page; renderCycles(state.cycles); }
      else { $("cycles-coverage").textContent = "读取未完成，可刷新重试。"; $("window-label").textContent = "恢复回看暂不可用"; }
      if (state.status) renderStatus(state.status);
      notice(error.message, 'cycles');
    }
  } finally { if (request === state.cycleRequest) { cycleControls(); $("cycles-panel").setAttribute('aria-busy', 'false'); } }
}
function refreshActiveView() { return state.section === "cycles" ? loadCycles() : state.section === "weekly" ? loadWeekly() : state.section === "journal" ? loadJournal() : loadView(); }
function switchSection(section) {
  state.section = section;
  const journal = section === "journal", weekly = section === "weekly", overview = section === "overview";
  state.weeklyRequest++; state.weeklyRecordsRequest++; state.cycleRequest++;
  $("page-title").textContent = section === "cycles" ? "恢复回看" : weekly ? "周回顾" : journal ? "日志时间线" : "身体状态";
  $("daily-summary").hidden = !overview; $("cards").hidden = !overview; document.querySelector(".trend-panel").hidden = !overview;
  $("journal-panel").hidden = !journal;
  $("cycles-panel").hidden = section !== "cycles"; $("view-cycles").setAttribute("aria-pressed", String(section === "cycles"));
  $("weekly-panel").hidden = !weekly; document.querySelector('.segmented').hidden = weekly;
  $("view-weekly").setAttribute("aria-pressed", String(weekly)); $("view-overview").setAttribute("aria-pressed", String(overview)); $("view-journal").setAttribute("aria-pressed", String(journal));
  $("window-label").textContent = "正在读取所选日期…";
  if (state.status) renderStatus(state.status);
  if (overview) requestAnimationFrame(() => state.chart?.resize());
  refreshActiveView();
}
function renderFreshness(status) {
  clearTimeout(state.freshnessTimer);
  const syncDays = state.section === "weekly" ? 30 : state.days;
  const freshness = status.freshness[String(syncDays)];
  const now = Date.parse(status.checked_at) + performance.now() - state.statusReceived;
  const remaining = Date.parse(freshness.fresh_until) - now;
  const fresh = freshness.state === "fresh" && remaining > 0;
  const paused = status.last_run && status.last_run.status !== "completed";
  let title = fresh ? "本地副本近期已同步" : "本地副本待更新";
  let note = fresh ? `${state.section === "weekly" ? "最近" : "所选"} ${syncDays} 天请求范围在 30 分钟内已成功同步。` : freshness.state === "never" ? "尚无成功同步记录。" : freshness.state === "uncovered" ? `尚无适用的 ${syncDays} 天成功请求。` : "上次适用请求已超过 30 分钟。";
  if (!status.sync_enabled) { title = "合成演示"; note = "刷新只更新本地视图，不访问 WHOOP。"; }
  else if (!status.can_sync) { title = "本地数据仍可查看"; note = status.sync_blocked === "policy" ? "当前本地存储授权未包含 WHOOP API。" : "需在本机完成 WHOOP 连接授权，再刷新此页。"; }
  else if (status.running) { title = "正在更新本地副本"; note = "已有数据仍可查看；补取范围见数据连接，整批完成后更新视图。"; }
  else if (paused || status.error) { title = "同步尚未完成"; note = "已有数据仍可查看。可继续原断点，或重新计算范围并同步。"; }
  else if (!fresh && status.sync_plan[String(syncDays)].reason === "history_gap") { note = "先前成功查询的区间之间仍有待补取部分。"; }
  $("freshness-title").textContent = state.section === "journal" ? `API 指标 · ${title}` : title;
  $("freshness-note").textContent = note + (state.section === "weekly" ? " 周回顾使用本机已保留历史；补取范围可早于所选视图。" : " 同步成功不代表每天都有记录或已评分。");
  $("freshness-check").textContent = `状态检查：${time(status.checked_at)} UTC`;
  $("freshness").classList.toggle("needs-update", !fresh && status.sync_enabled && !status.running);
  // Only age the label locally. This timer never requests data or starts a sync.
  if (fresh) state.freshnessTimer = setTimeout(() => renderFreshness(state.status), remaining + 20);
}
function renderStatus(status) {
  renderFreshness(status);
  $("connection-state").textContent = status.environment === "synthetic" ? "合成演示 · 不访问账户" : status.connected ? "已授权 · 只读连接" : "需要在本机重新授权";
  $("connection-dot").classList.toggle("disconnected", status.environment === "real" && !status.connected);
  $("last-sync").textContent = status.last_success_at ? time(status.last_success_at) + " UTC" : "尚无成功同步";
  const window = status.last_success_window;
  $("sync-window").textContent = window ? `${time(window.start)} — ${time(window.end)} UTC` : "—";
  $("retention").textContent = status.retention_days ? `${status.retention_days} 天` : "合成环境";
  const resumable = status.last_run && status.last_run.status !== "completed";
  const plan = resumable ? status.last_run.catch_up : status.sync_plan[String(state.section === "weekly" ? 30 : state.days)];
  const request = resumable ? status.last_run.request : plan?.request;
  $("planned-window-label").textContent = resumable ? "当前请求范围" : "同步检查范围";
  $("planned-window").textContent = request && status.sync_enabled ? `${time(request.start)} — ${time(request.end)} UTC` : "—";
  let catchupNote = plan?.lookback_limited ? `可用同步起点缺失或过早，本次最多回查 ${plan.lookback_days} 天；更早历史未核查。` : plan?.expanded ? "已扩展查询范围，以补取此前未成功查询的时段，并重查起点附近的近期记录。" : "同步会重查近期记录，获取已发布的评分与修正。";
  if (resumable && !plan) catchupNote = "此断点沿用原请求范围；完成后可再同步至现在。";
  $("catchup-note").textContent = status.sync_enabled ? catchupNote : "合成演示不访问 WHOOP。";
  $("sync-button").textContent = status.running ? "同步中…" : status.sync_enabled ? resumable || status.error ? "↻ 重新同步并补取" : "↻ 同步并补取" : "↻ 刷新视图";
  $("sync-button").disabled = status.running || (status.sync_enabled && !status.can_sync);
  $("sync-progress").hidden = !status.running;
  $("progress").value = status.last_run?.resources_completed || 0;
  $("progress-label").textContent = `同步中 · ${status.last_run?.resources_completed || 0} / 6 类资源`;
  const error = status.running ? "" : status.error || (resumable ? status.last_run.last_error ? "上次同步未完成，已有数据保持可用。网络恢复后可继续；若仍失败，可重新同步并补取。" : "上次同步已暂停，可从保存的断点继续。" : "");
  $("sync-error").textContent = error; $("sync-error").hidden = !error;
  $("resume-button").hidden = !status.can_sync || status.running || !resumable;
  $("resume-window").hidden = !status.can_sync || status.running || !resumable;
  $("resume-window").textContent = resumable ? `续传沿用原请求：${time(status.last_run.request.start)} — ${time(status.last_run.request.end)} UTC；不包含此后新增的数据。` : "";
  $("sync-policy").textContent = "打开时检查一次，按成功请求记录补取，最多回查 366 天；30 分钟内避免重复同步。切换视图后可手动更新，停留期间不定时采集。";
}
async function loadStatus() {
  clearTimeout(state.timer);
  try {
    const previous = state.status;
    const status = await api("/api/status");
    state.status = status; state.statusReceived = performance.now(); renderStatus(status); notice("", "status");
    if ((previous?.running && !status.running) || (previous && previous.last_success_at !== status.last_success_at)) await refreshActiveView();
    if (status.running) state.timer = setTimeout(loadStatus, 2000);
  } catch (error) { notice(error.message, "status"); state.timer = setTimeout(loadStatus, 5000); }
}
async function sync(resume = false, ifStale = false) {
  if (!state.status) return;
  if (!state.status.sync_enabled) { if (!ifStale) await refreshActiveView(); return; }
  if (!state.status.can_sync || state.status.running) return;
  $("sync-button").disabled = true; $("resume-button").disabled = true;
  try {
    const body = { days: state.section === "weekly" ? 30 : state.days };
    if (resume) body.resume = state.status.last_run.run_id;
    if (ifStale) body.if_stale = true;
    const result = await api("/api/sync", { method: "POST", headers: { "Content-Type": "application/json", "X-Whoop-CSRF": csrf }, body: JSON.stringify(body) });
    notice(result.reason === "checkpoint_unavailable" ? "原断点已完成或过期。请重新同步并补取。" : result.reason === "stopping" ? "看板服务正在停止；需要时重新打开即可。" : "", "sync");
    if (result.accepted) state.status = { ...state.status, running: true };
    await loadStatus();
  } catch (error) { notice(error.message, "sync"); await loadStatus(); }
  finally { $("resume-button").disabled = false; }
}
document.querySelectorAll("[data-days]").forEach((button) => button.addEventListener("click", () => { state.days = Number(button.dataset.days); state.journalPage = 1; state.cyclePage = 1; if (state.status) renderStatus(state.status); refreshActiveView(); }));
$("view-overview").addEventListener("click", () => switchSection("overview"));
$("view-journal").addEventListener("click", () => switchSection("journal"));
$("view-cycles").addEventListener("click", () => switchSection("cycles"));
$("cycles-refresh").addEventListener("click", () => loadCycles());
$("cycles-previous").addEventListener("click", () => { state.cyclePage--; loadCycles(); });
$("cycles-next").addEventListener("click", () => { state.cyclePage++; loadCycles(); });
$("view-weekly").addEventListener("click", () => switchSection("weekly"));
$("weekly-choice").addEventListener("change", () => { state.week = $("weekly-choice").value; loadWeekly(); });
$("weekly-refresh").addEventListener("click", () => loadWeekly());
$("weekly-older").addEventListener("click", () => { state.week = state.weekly.choices[state.weekly.choices.findIndex(item => item.week === state.week) + 1].week; loadWeekly(); });
$("weekly-newer").addEventListener("click", () => { state.week = state.weekly.choices[state.weekly.choices.findIndex(item => item.week === state.week) - 1].week; loadWeekly(); });
$("weekly-detail-close").addEventListener("click", () => { state.weeklyRecordsRequest++; $("weekly-detail").hidden = true; });
$("weekly-records-previous").addEventListener("click", () => { state.weeklyPage--; loadWeeklyRecords(); });
$("weekly-records-next").addEventListener("click", () => { state.weeklyPage++; loadWeeklyRecords(); });
$("refresh-journal").addEventListener("click", () => loadJournal());
$("journal-previous").addEventListener("click", () => { state.journalPage--; loadJournal(); });
$("journal-next").addEventListener("click", () => { state.journalPage++; loadJournal(); });
$("previous-page").addEventListener("click", () => { state.page--; renderRows(); });
$("next-page").addEventListener("click", () => { state.page++; renderRows(); });
$("close-evidence").addEventListener("click", () => $("evidence-dialog").close());
$("evidence-dialog").addEventListener("close", () => { state.detail++; });
$("sync-button").addEventListener("click", () => sync());
$("resume-button").addEventListener("click", () => sync(true));
async function initialize() {
  await Promise.all([loadView(), loadStatus()]);
  // One conditional POST per page opening. View changes and polling never call this again.
  await sync(false, true);
}
initialize();
