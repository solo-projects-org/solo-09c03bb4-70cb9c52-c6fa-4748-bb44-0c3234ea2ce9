/* 清障页：领批 / 按批号重下 / 上传导入回执（签收并结批） */
"use strict";

const $ = id => document.getElementById(id);
const esc = s => String(s == null ? "" : s).replace(/[<>&"']/g, c =>
  ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;" }[c]));

function fmtTime(iso) {
  return iso ? iso.slice(0, 19).replace("T", " ") : "";
}

async function api(url, opts) {
  const resp = await fetch(url, opts);
  let data;
  try { data = await resp.json(); } catch (_) { data = { ok: false, error: "HTTP " + resp.status }; }
  if (!resp.ok && !data.error) data.error = "HTTP " + resp.status;
  return { status: resp.status, data };
}

/* ---------------- 批次列表 ---------------- */

let batches = [];

async function loadBatches() {
  const { data } = await api("/api/batches");
  batches = (data && data.batches) || [];
  renderBatchTable();
  renderBatchSelect();
  await showCurrentBatch(batches.find(b => b.status === "open"));
}

function renderBatchTable() {
  const tbody = $("batchRows");
  tbody.innerHTML = "";
  $("batchEmpty").hidden = batches.length > 0;
  for (const b of batches) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><strong>${esc(b.batch_no)}</strong></td>` +
      `<td>${b.status === "open" ? "🟡 进行中" : "✅ 已结批"}</td>` +
      `<td>${b.item_count}</td>` +
      `<td>${b.signed_count}</td>` +
      `<td>${b.carried_count}</td>` +
      `<td>${fmtTime(b.created_at)}</td>` +
      `<td>${fmtTime(b.closed_at) || "—"}</td>` +
      `<td><a href="${esc(b.geojson_url)}" download>GeoJSON</a></td>`;
    tbody.appendChild(tr);
  }
}

function renderBatchSelect() {
  const sel = $("batchSelect");
  const keep = sel.value;
  sel.innerHTML = "";
  if (!batches.length) {
    const opt = document.createElement("option");
    opt.textContent = "（暂无批次）";
    opt.disabled = true;
    sel.appendChild(opt);
    syncRedownload();
    return;
  }
  for (const b of batches) {
    const opt = document.createElement("option");
    opt.value = b.batch_no;
    opt.textContent = `${b.batch_no}（${b.status === "open" ? "进行中" : "已结批"}，${b.item_count} 点）`;
    sel.appendChild(opt);
  }
  if (batches.some(b => b.batch_no === keep)) sel.value = keep;
  syncRedownload();
  syncReceiptControls();
}

function selectedBatch() {
  const no = $("batchSelect").value;
  return batches.find(b => b.batch_no === no) || null;
}

function syncRedownload() {
  const sel = $("batchSelect");
  const btn = $("redownloadGeo");
  if (sel.value) {
    btn.href = `/api/batches/${encodeURIComponent(sel.value)}.geojson`;
    btn.setAttribute("download", `batch-${sel.value}.geojson`);
  } else {
    btn.removeAttribute("href");
  }
}

/* 回执只能提交给所选批号；已结批不能再签收 */
function syncReceiptControls() {
  const b = selectedBatch();
  const open = !!(b && b.status === "open");
  $("receiptBtn").disabled = !open;
  $("templateBtn").disabled = !b;
  $("receiptFile").disabled = !open;
  if (b && b.status === "closed") {
    $("receiptStatus").textContent =
      `批次 ${b.batch_no} 已结批，重交相同编号集合会返回原结果。`;
  } else if (b) {
    $("receiptStatus").textContent = "";
  }
}

$("batchSelect").addEventListener("change", () => {
  syncRedownload();
  syncReceiptControls();
});

/* ---------------- 当前批次展示 ---------------- */

async function showCurrentBatch(batch) {
  const box = $("currentBatch");
  if (!batch) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  $("curBatchNo").textContent = batch.batch_no;
  $("curBatchMeta").textContent =
    `${batch.status === "open" ? "进行中" : "已结批"} · 领批 ${fmtTime(batch.created_at)}`;

  const { data } = await api(`/api/batches/${encodeURIComponent(batch.batch_no)}`);
  const items = (data && data.batch && data.batch.items) || [];
  $("curItemCount").textContent = items.length;
  const tbody = $("curItems");
  tbody.innerHTML = "";
  for (const it of items) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td><strong>${it.record_id}</strong></td>` +
      `<td>${it.lat.toFixed(6)}</td>` +
      `<td>${it.lng.toFixed(6)}</td>` +
      `<td>${esc(it.note)}</td>` +
      `<td>${fmtTime(it.uploaded_at)}</td>`;
    if (it.status === "signed") tr.classList.add("signed-row");
    tbody.appendChild(tr);
  }
  const geo = $("downloadGeo");
  geo.href = batch.geojson_url;
  geo.setAttribute("download", `batch-${batch.batch_no}.geojson`);
}

/* ---------------- 领批 ---------------- */

$("claimBtn").addEventListener("click", async () => {
  $("claimStatus").textContent = "领取中…";
  try {
    const { status, data } = await api("/api/batches/claim", { method: "POST" });
    if (status !== 200 || !data.ok) throw new Error(data.error || "领批失败");
    if (!data.batch) {
      $("claimStatus").textContent = "没有待发点位：未创建空批次，巡护员可继续补传。";
    } else if (data.created) {
      $("claimStatus").textContent =
        `已创建批次 ${data.batch.batch_no}，共 ${data.batch.item_count} 个点位。`;
    } else {
      $("claimStatus").textContent =
        `已有未结批次 ${data.batch.batch_no}（${data.batch.item_count} 点），返回原批。`;
    }
    await loadBatches();
  } catch (e) {
    $("claimStatus").textContent = "领批失败：" + e.message;
  }
});

/* ---------------- 回执上传 ---------------- */

$("templateBtn").addEventListener("click", () => {
  const b = selectedBatch();
  const blob = new Blob(["record_id\n"], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `receipt-${(b && b.batch_no) || "batch"}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});

function renderReceiptResult(data, isError) {
  const box = $("receiptResult");
  box.hidden = false;
  if (isError) {
    box.innerHTML = `<p class="receipt-error">✖ ${esc(data.error)}</p>` +
      (data.outside_ids ? `<p class="hint">批外编号：${data.outside_ids.map(esc).join(", ")}</p>` : "") +
      (data.unexpected_ids && data.unexpected_ids.length
        ? `<p class="hint">多出的编号：${data.unexpected_ids.map(esc).join(", ")}</p>` : "");
    return;
  }
  box.innerHTML =
    `<p class="receipt-ok">✔ ${esc(data.message || "回执已接受，批次已结批。")}</p>` +
    `<p class="hint">已签收编号：${(data.signed_ids || []).map(esc).join(", ") || "无"}</p>` +
    `<p class="hint">未签收、留待下一批：${(data.carried_ids || []).map(esc).join(", ") || "无"}</p>`;
}

$("receiptBtn").addEventListener("click", async () => {
  const file = $("receiptFile").files[0];
  if (!file) {
    $("receiptStatus").textContent = "请先选择回执 CSV 文件。";
    return;
  }
  const b = selectedBatch();
  if (!b) return;
  $("receiptStatus").textContent = "上传中…";
  const fd = new FormData();
  fd.append("file", file, file.name);
  try {
    const { status, data } = await api(
      `/api/batches/${encodeURIComponent(b.batch_no)}/receipt`,
      { method: "POST", body: fd }
    );
    if (status !== 200 || !data.ok) {
      $("receiptStatus").textContent = "回执被退回，签收状态未改变。";
      renderReceiptResult(data, true);
    } else {
      $("receiptStatus").textContent = "回执已接受。";
      renderReceiptResult(data, false);
    }
    $("receiptFile").value = "";
    await loadBatches();
  } catch (e) {
    $("receiptStatus").textContent = "上传失败：" + e.message;
  }
});

/* ---------------- 启动 ---------------- */
loadBatches().catch(e => {
  $("claimStatus").textContent = "加载失败：" + e.message;
});
