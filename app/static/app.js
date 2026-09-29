/* RAÍZ Transcribe — interfaz. Clave de acceso en window.RAIZ.K (la página solo se sirve autorizada). */
(function () {
const K = RAIZ.K, IS_LOCAL = RAIZ.LOCAL;
const IOS = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const $ = s => document.querySelector(s);
let cur = null, timer = null, tick = null, curPdf = null, curAudio = null;
const fmtDur = s => { if (s == null) return "duración desconocida"; s = Math.round(s);
  return [Math.floor(s / 3600), Math.floor(s % 3600 / 60), s % 60].map(n => String(n).padStart(2, "0")).join(":"); };
const fmtMB = b => (b / 1048576).toFixed(1) + " MB";
const esc = t => String(t ?? "").replace(/[&<>"]/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const store = { get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } },
                set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} } };

async function api(path, opts = {}) {
  opts.headers = Object.assign({"x-raiz-key": K}, opts.headers || {});
  try { const r = await fetch(path, opts); net(true); return r; }
  catch (e) { net(false); throw e; }
}
function net(ok) { $("#net").hidden = ok; }
const fileUrl = (id, kind, inline) => `/api/jobs/${encodeURIComponent(id)}/file/${kind}?k=${K}` + ((inline ?? IOS) ? "&inline=1" : "");

// ---------- compartir: Share Sheet nativo con archivos ya preparados (iOS exige llamarlo dentro del toque) ----------
function shareFiles(files, fallbackUrl, hintEl) {
  if (files && files.length && navigator.canShare && navigator.canShare({files})) {
    navigator.share({files, title: files[0].name}).catch(e => { if (e.name !== "AbortError") fallback(); });
    return;
  }
  fallback();
  function fallback() {
    if (fallbackUrl) { window.open(fallbackUrl, "_blank"); if (hintEl) { hintEl.hidden = false;
      hintEl.textContent = "Se abrió el archivo: usá el botón Compartir del navegador (en iPhone: cuadrado con flecha)."; } return; }
    for (const f of files || []) { const a = document.createElement("a"); a.href = URL.createObjectURL(f); a.download = f.name;
      document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 60000); }
  }
}

// ================= GRABADOR =================
const Rec = window.RaizRec;
Rec.setKey(K);
let done = null, doneFiles = null;
const STATES = {grabando: "GRABANDO", pausado: "⏸ PAUSADO", interrumpido: "⚠️ INTERRUMPIDO", recuperando: "RECUPERANDO…",
                guardando: "GUARDANDO…", guardado: "✓ GUARDADO", error: "ERROR"};

function showRecPanel(which) { for (const id of ["recIdle", "recLive", "recDone"]) $("#" + id).hidden = id !== which; }

function renderRecState(s, rec) {
  if (!["grabando", "pausado", "interrumpido", "recuperando", "guardando", "error"].includes(s)) return;
  showRecPanel("recLive");
  const st = $("#recStatus"); st.className = s;
  st.innerHTML = s === "grabando" ? '<span class="dot"></span>GRABANDO' : esc(STATES[s] || s);
  $("#recPause").hidden = s !== "grabando";
  $("#recResume").hidden = !(s === "pausado" || s === "interrumpido");
  $("#recStop").disabled = s === "guardando" || s === "recuperando";
  const it = rec && rec.interrupcion;
  $("#recMsg").innerHTML = s === "interrumpido" && it
    ? `<span class="warn"><b>LA GRABACIÓN FUE INTERRUMPIDA A LAS ${fmtDur(it.at / 1000)}</b> (${esc(it.reason)}).<br>Audio preservado hasta ese momento. Podés CONTINUAR (se agrega como parte nueva) o DETENER.</span>`
    : s === "pausado" ? "Pausado: no se está grabando." : s === "grabando" ? `Guardado en el dispositivo cada 5 s${IOS ? " · no bloquees la pantalla ni cambies de app: iOS corta el micrófono" : ""}.` : "";
}
Rec.on("state", renderRecState);
Rec.on("tick", (ms, s) => {
  $("#recTime").textContent = fmtDur(ms / 1000);
  $("#lvl i").style.width = s === "grabando" ? Math.min(100, Rec.level() * 160) + "%" : "0";
});
Rec.on("error", msg => { $("#recMsg").innerHTML = `<span class="err">${esc(msg)}</span>`; });
Rec.on("synced", () => { if (done) refreshDone(done.id); loadLocal(); });
Rec.on("chunk", () => {});

$("#recStart").onclick = async () => {
  try { await Rec.start(); $("#recTime").textContent = "00:00:00"; }
  catch (e) { $("#recHint").innerHTML = `<span class="err">No se pudo usar el micrófono: ${esc(e.message || e.name)}. En iPhone: Configuración → Safari → Micrófono → Permitir.</span>`; }
};
$("#recPause").onclick = () => Rec.pause();
$("#recResume").onclick = async () => { try { await Rec.resume(); } catch (e) { $("#recMsg").innerHTML = `<span class="err">No se pudo reanudar: ${esc(e.message || e.name)}</span>`; } };
$("#recStop").onclick = async () => { const r = await Rec.stop(); if (r) openDone(r.id); loadLocal(); };
$("#recNew").onclick = () => { done = null; showRecPanel("recIdle"); };

let playList = [], playIdx = 0;
function playSegments(audioEl, blobs) {
  playList.forEach(u => URL.revokeObjectURL(u)); playList = blobs.map(b => URL.createObjectURL(b)); playIdx = 0;
  audioEl.hidden = false; audioEl.src = playList[0] || "";
  audioEl.onended = () => { if (++playIdx < playList.length) { audioEl.src = playList[playIdx]; audioEl.play(); } };
}

async function openDone(id) {
  const r = await Rec.get(id); if (!r) return;
  done = r; showRecPanel("recDone");
  $("#recName").value = r.name;
  playSegments($("#recAudio"), await Rec.segmentBlobs(id));
  doneFiles = await Rec.files(id);                       // preparados ya: compartir es instantáneo
  refreshDone(id);
}
async function refreshDone(id) {
  const r = await Rec.get(id); if (!r || !done || done.id !== id) return; done = r;
  const nseg = Object.keys(r.segs || {}).length;
  $("#recDoneInfo").innerHTML = `${fmtDur((r.durMs || 0) / 1000)} · ${fmtMB(r.bytes || 0)}${nseg > 1 ? ` · ${nseg} partes (hubo interrupciones)` : ""}<br>` +
    (r.job ? '<span class="ok">✓ Guardada en este dispositivo y respaldada en RAÍZ.</span>'
           : '<span class="warn">Guardada en este dispositivo. Respaldo en RAÍZ pendiente (se envía solo cuando hay conexión).</span>');
  if (r.job && nseg > 1) {        // audio único ya unido en el servidor → preferirlo para compartir
    try { const b = await (await api(`/api/jobs/${r.job}/file/original`)).blob();
      doneFiles = [new File([b], r.jobName || (r.name + ".m4a"), {type: b.type || "audio/mp4"})]; } catch (e) {}
  }
}
$("#recRename").onclick = async () => {
  const name = $("#recName").value.trim(); if (!name || !done) return;
  await Rec.rename(done.id, name);
  if (done.job) { try { const st = await (await api(`/api/jobs/${done.job}/rename`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({name})})).json();
    await Rec.update(done.id, {jobName: st.archivo_original}); } catch (e) {} }
  doneFiles = await Rec.files(done.id); refreshDone(done.id); loadLocal();
  $("#recRename").textContent = "✓"; setTimeout(() => $("#recRename").textContent = "✏️ RENOMBRAR", 1200);
};
$("#recShare").onclick = () => shareFiles(doneFiles, null, null);
$("#recSave").onclick = () => {  // en iPhone la hoja de Compartir incluye «Guardar en Archivos»
  if (IOS) $("#recDoneInfo").insertAdjacentHTML("beforeend", '<br><span class="muted">En la hoja que se abre elegí <b>Guardar en Archivos</b>.</span>');
  shareFiles(doneFiles, null, null);
};
$("#recTx").onclick = async () => transcribeRec(done && done.id, $("#recTx"));

async function transcribeRec(id, btn) {
  if (!id) return;
  if (btn) { btn.disabled = true; btn.textContent = "Enviando a RAÍZ…"; }
  try {
    const r = await Rec.get(id);
    let jobId = r.job || await Rec.sync(id);
    const st = await (await api(`/api/jobs/${jobId}/transcribe`, {method: "POST"})).json();
    show(st); scrollTo(0, $("#job").offsetTop - 10);
  } catch (e) {
    alert("No se pudo enviar ahora (" + (e.message || e) + "). La grabación sigue guardada en este dispositivo; reintentá con conexión.");
  } finally { if (btn) { btn.disabled = false; btn.textContent = "📝 TRANSCRIBIR"; } }
}

// ---------- recuperación al abrir ----------
let pending = null;
async function checkRecover() {
  const list = await Rec.unfinished(); pending = list[0] || null;
  $("#recover").hidden = !pending; if (!pending) return;
  const ms = await Rec.recoveredDuration(pending.id);
  $("#recoverInfo").textContent = `«${pending.name}» · duración recuperada: ${fmtDur(ms / 1000)}` + (list.length > 1 ? ` · (${list.length} en total)` : "");
}
$("#recoverBtn").onclick = async () => { const r = await Rec.recover(pending.id); $("#recover").hidden = true; await openDone(r.id); loadLocal(); checkRecover(); };
$("#recoverPlay").onclick = async () => playSegments($("#recoverAudio"), await Rec.segmentBlobs(pending.id));
$("#recoverSave").onclick = async () => { const r = await Rec.recover(pending.id); $("#recover").hidden = true; await openDone(r.id); loadLocal(); checkRecover(); };
$("#recoverTx").onclick = async () => { const r = await Rec.recover(pending.id); $("#recover").hidden = true; await openDone(r.id); loadLocal(); checkRecover(); transcribeRec(r.id, null); };

// ---------- grabaciones en el dispositivo ----------
async function loadLocal() {
  const rs = (await Rec.list()).filter(r => r.final);
  $("#localRecs").innerHTML = rs.map(r => `<li data-id="${esc(r.id)}"><b>${esc(r.name)}</b> · ${fmtDur((r.durMs || 0) / 1000)} · ` +
    (r.job ? '<span class="ok">respaldada en RAÍZ</span>' : '<span class="warn">solo en este dispositivo</span>') +
    `<br><button class="sec" data-a="open">Abrir</button>` + (r.job ? `<button class="sec" data-a="del">Borrar del dispositivo</button>` : "") + "</li>").join("")
    || "<li>Ninguna todavía</li>";
  document.querySelectorAll("#localRecs li[data-id]").forEach(li => li.onclick = async e => {
    const a = e.target.dataset.a, id = li.dataset.id; if (!a) return;
    if (a === "open") { await openDone(id); scrollTo(0, 0); }
    if (a === "del" && confirm("¿Borrar esta grabación de ESTE dispositivo? Queda la copia respaldada en RAÍZ.")) { await Rec.remove(id); loadLocal(); }
  });
}

// ================= SUBIR ARCHIVOS (uno o varios) =================
function setSteps(n) { document.querySelectorAll("#steps span").forEach((s, i) => s.className = i < n ? "done" : i === n ? "now" : ""); }

async function readFile(picked) {  // iOS: leer entero antes de enviar (evita el campo vacío → 422)
  const buf = await picked.arrayBuffer();
  if (!buf.byteLength) throw new Error("archivo vacío");
  return new File([buf], picked.name, {type: picked.type || "audio/mp4"});
}

function xhrUpload(file, query, onProgress) {
  return new Promise((res, rej) => {
    const fd = new FormData(); fd.append("file", file, file.name);
    const x = new XMLHttpRequest(); x.open("POST", "/api/upload" + (query || "")); x.setRequestHeader("x-raiz-key", K);
    x.upload.onprogress = e => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total * 100); };
    x.onload = () => { net(true); if (x.status === 200) res(JSON.parse(x.responseText));
      else { let why = ""; try { const d = JSON.parse(x.responseText).detail; why = typeof d === "string" ? d : JSON.stringify(d); } catch (e) { why = x.responseText.slice(0, 300); }
        rej(new Error(`(${x.status}) ${why}`)); } };
    x.onerror = () => { net(false); rej(new Error("el servidor no responde")); };
    x.send(fd);
  });
}

async function upload(picked) {
  $("#result").hidden = true; $("#job").hidden = false; $("#go").hidden = false;
  $("#jname").textContent = picked.name; $("#jmeta").textContent = fmtMB(picked.size);
  setSteps(0); $("#jstate").textContent = "Leyendo el archivo…"; $("#go").disabled = true;
  let file;
  try { file = await readFile(picked); }
  catch (e) { $("#jstate").innerHTML = `<span class="err">No se pudo leer el archivo (${esc(e.message || e)}). Si está en iCloud, abrilo una vez en Archivos para que se descargue y volvé a elegirlo.</span>`; return; }
  const p = $("#jprog"); p.hidden = false; p.value = 0;
  try { const st = await xhrUpload(file, "", v => { p.value = v; $("#jstate").textContent = `Subiendo… ${Math.round(v)} %`; });
    p.hidden = true; show(st); loadHist(); }
  catch (e) { p.hidden = true; $("#jstate").innerHTML = `<span class="err">Error al subir ${esc(e.message)}</span>`; }
}

// ---------- lote ----------
let batch = null;
async function uploadBatch(files) {
  $("#job").hidden = true; $("#result").hidden = true;
  const lote = Date.now().toString(36);
  batch = {lote, items: files.map(f => ({name: f.name, size: f.size, state: "esperando", id: null, error: null}))};
  $("#batch").hidden = false; renderBatch();
  for (let i = 0; i < files.length; i++) {       // en orden de selección; cada uno es un trabajo independiente
    const it = batch.items[i]; it.state = "subiendo"; renderBatch();
    try { const f = await readFile(files[i]);
      const st = await xhrUpload(f, `?auto=1&lote=${lote}`, v => { it.pct = Math.round(v); renderBatch(); });
      it.id = st.id; it.state = st.estado; it.error = st.error; }
    catch (e) { it.state = "error"; it.error = e.message; }
    renderBatch();
  }
  pollBatch(); loadHist();
}
const LABEL = {esperando: "EN ESPERA", subiendo: "SUBIENDO", cargado: "CARGADO", en_cola: "EN COLA", procesando: "TRANSCRIBIENDO…", listo: "✓ LISTO", error: "ERROR"};
function renderBatch() {
  const n = batch.items.length, ok = batch.items.filter(i => i.state === "listo").length;
  $("#batchTitle").textContent = `${n} ARCHIVO${n > 1 ? "S" : ""}`;
  $("#batchCount").textContent = `${ok} / ${n} COMPLETADOS`;
  $("#batchList").innerHTML = batch.items.map(i => `<li>${i.id ? `<a href="#" data-id="${esc(i.id)}">${esc(i.name)}</a>` : esc(i.name)}<br>` +
    `<span class="${i.state === "listo" ? "ok" : i.state === "error" ? "err" : "muted"}">${LABEL[i.state] || esc(i.state)}${i.state === "subiendo" && i.pct != null ? ` ${i.pct} %` : ""}</span>` +
    (i.error ? ` <span class="err">${esc(i.error)}</span>` : "") + "</li>").join("");
  document.querySelectorAll("#batchList a").forEach(a => a.onclick = e => { e.preventDefault(); poll(a.dataset.id); scrollTo(0, $("#job").offsetTop); });
  const ids = batch.items.filter(i => i.state === "listo").map(i => i.id);
  $("#batchZip").hidden = !ids.length; $("#batchZip").href = `/api/zip?ids=${ids.join(",")}&k=${K}`;
}
async function pollBatch() {
  if (!batch) return;
  const ids = batch.items.filter(i => i.id).map(i => i.id);
  try { const L = await (await api(`/api/jobs?ids=${ids.join(",")}`)).json();
    for (const s of L) { const it = batch.items.find(i => i.id === s.id); if (it) { it.state = s.estado; it.error = s.error; } }
    renderBatch(); } catch (e) {}
  if (batch.items.some(i => ["en_cola", "procesando", "subiendo", "esperando"].includes(i.state))) setTimeout(pollBatch, 4000); else loadHist();
}

// ================= TRABAJO / RESULTADO =================
function elapsed(since) { const s = Math.max(0, (Date.now() - new Date(since).getTime()) / 1000);
  return `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`; }

function show(st) {
  cur = st; $("#job").hidden = false;
  $("#jname").textContent = st.archivo_original;
  $("#jmeta").textContent = `${fmtMB(st.tamano_bytes)} · ${fmtDur(st.duracion)}${st.origen === "grabacion" ? " · grabación" : ""}`;
  const busy = st.estado === "en_cola" || st.estado === "procesando";
  const paso = st.estado === "listo" ? 4 : st.estado === "procesando" ? ({1: 1, 2: 1, 3: 2, 4: 3}[st.paso] || 1) : 1;
  setSteps(st.estado === "cargado" || st.estado === "error" ? 1 : paso);
  if (st.estado === "cargado") document.querySelectorAll("#steps span")[1].className = "";
  clearInterval(tick);
  const renderState = () => {
    const est = st.duracion ? ` · suele tardar ~${Math.max(1, Math.round(st.duracion * 0.15 / 60))} min` : "";
    $("#jstate").innerHTML = st.estado === "error"
      ? `<span class="ok">AUDIO CONSERVADO</span> · <span class="err">TRANSCRIPCIÓN: ERROR — ${esc(st.error || "")}</span>`
      : busy ? `⏳ ${esc(st.etapa)} — ${st.inicio ? elapsed(st.inicio) : "0:00"} transcurrido${est}.<br><span class="muted">Podés cerrar o bloquear el iPhone: el servidor sigue y el resultado queda acá.</span>`
      : st.estado === "listo" ? "✅ Listo" : "Audio cargado. Tocá TRANSCRIBIR.";
  };
  renderState(); if (busy) tick = setInterval(renderState, 1000);
  $("#jprog").hidden = !busy; if (busy) $("#jprog").removeAttribute("value");
  $("#go").disabled = !(st.estado === "cargado" || st.estado === "error") || !st.duracion;
  $("#go").textContent = st.estado === "error" ? "REINTENTAR" : "TRANSCRIBIR";
  $("#go").hidden = st.estado === "listo";
  clearTimeout(timer);
  if (busy) timer = setTimeout(() => poll(st.id), 3000);
  const last = store.get("lastJob") || {};
  if (st.estado === "listo") { if (!$("#result").hidden && cur && cur._rendered === st.id) {} else render(st); store.set("lastJob", {id: st.id, visto: true}); }
  else { $("#result").hidden = true; if (last.id !== st.id || !last.visto) store.set("lastJob", {id: st.id, visto: false}); }
  history.replaceState(null, "", `?job=${encodeURIComponent(st.id)}&k=${K}`);
}

async function poll(id) {
  try { const r = await api("/api/jobs/" + encodeURIComponent(id));
    if (r.ok) { const st = await r.json(); show(st); if (st.estado !== "procesando" && st.estado !== "en_cola") loadHist(); return; }
    if (r.status === 404) { store.set("lastJob", null); return; }
  } catch (e) {}
  timer = setTimeout(() => poll(id), 5000);
}

function render(st) {
  const d = st.doc; cur._rendered = st.id;
  $("#result").hidden = false; $("#shareHint").hidden = true;
  $("#diar").textContent = `${d.nota_diarizacion} · ${d.proveedor} / ${d.modelo} · idioma: ${d.idioma}. "palabra[?]" = baja confianza.`;
  $("#text").innerHTML = d.items.map(i => i.tipo === "gap"
    ? `<p class="gap">[${i.ts}] sin habla transcripta (silencio o inaudible)</p>`
    : `<div class="seg"><b><span class="ts">[${i.ts}]</span> ${esc(i.hablante || "")}</b>${esc(i.texto)}</div>`).join("");
  for (const k of ["txt", "md", "pdf"]) { const a = $("#d" + k); a.href = fileUrl(st.id, k);
    if (IOS) { a.target = "_blank"; a.removeAttribute("download"); } else a.setAttribute("download", ""); }
  $("#spkbox").hidden = !d.speakers.length;
  $("#spks").innerHTML = d.speakers.map(s =>
    `<div class="spk"><span>${esc(s.label)} =</span><input data-id="${esc(s.id)}" value="${esc(s.nombre || "")}" placeholder="nombre (opcional)"></div>`).join("");
  const m = st.metricas || {};
  $("#metrics").textContent = `Procesado en ${Math.round(m.tiempo_procesamiento_s)} s (${m.ratio_procesamiento_audio}× la duración) · ${m.hablantes} hablante(s) · ${m.palabras} palabras · costo estimado USD ${m.costo_estimado_usd} (crédito gratuito)`;
  // precargar PDF (y audio si es chico) para que COMPARTIR abra la hoja nativa en el mismo toque
  curPdf = null; curAudio = null; $("#share").textContent = "📤 COMPARTIR PDF"; $("#shareAudio").textContent = "📤 COMPARTIR AUDIO";
  const id = st.id;
  api(`/api/jobs/${encodeURIComponent(id)}/file/pdf`).then(r => r.blob()).then(b => { if (cur && cur.id === id) curPdf = new File([b], st.archivos.pdf, {type: "application/pdf"}); }).catch(() => {});
  if (st.tamano_bytes < 30 * 1048576) prepAudio(st);
}
function prepAudio(st) {
  const id = st.id; $("#shareAudio").textContent = "Preparando audio…";
  return api(`/api/jobs/${encodeURIComponent(id)}/file/original`).then(r => r.blob()).then(b => {
    if (cur && cur.id === id) { curAudio = new File([b], st.archivo_original, {type: b.type || "audio/mp4"}); $("#shareAudio").textContent = "📤 COMPARTIR AUDIO"; } })
    .catch(() => { $("#shareAudio").textContent = "📤 COMPARTIR AUDIO"; });
}

$("#go").onclick = async () => { $("#go").disabled = true;
  try { const r = await api(`/api/jobs/${encodeURIComponent(cur.id)}/transcribe`, {method: "POST"}); show(await r.json()); }
  catch (e) { $("#go").disabled = false; } };

async function copyText(t, btn, label) {
  try { await navigator.clipboard.writeText(t); }
  catch (e) { const a = document.createElement("textarea"); a.value = t; a.setAttribute("readonly", "");
    a.style.position = "fixed"; a.style.opacity = "0"; document.body.appendChild(a);
    a.focus(); a.select(); a.setSelectionRange(0, t.length); document.execCommand("copy"); a.remove(); }
  btn.textContent = "¡COPIADO!"; setTimeout(() => btn.textContent = label, 1500);
}
$("#copy").onclick = () => copyText(cur.txt, $("#copy"), "COPIAR");
$("#share").onclick = () => {
  if (!curPdf && window.isSecureContext) { $("#share").textContent = "Preparando…"; return; }
  shareFiles(curPdf ? [curPdf] : null, fileUrl(cur.id, "pdf", true), $("#shareHint"));
};
$("#shareAudio").onclick = () => {
  if (!curAudio) { prepAudio(cur); return; }   // audio grande: 1er toque prepara, 2º comparte
  shareFiles([curAudio], fileUrl(cur.id, "original", true), $("#shareHint"));
};
$("#saveSpk").onclick = async () => {
  const names = {}; document.querySelectorAll("#spks input").forEach(i => names[i.dataset.id] = i.value);
  $("#saveSpk").disabled = true;
  try { await api(`/api/jobs/${encodeURIComponent(cur.id)}/speakers`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(names)}); }
  finally { $("#saveSpk").disabled = false; }
  cur._rendered = null; poll(cur.id);
};
$("#copyAtajo").onclick = () => copyText(`${location.origin}/api/atajo?k=${K}`, $("#copyAtajo"), "COPIAR DIRECCIÓN DEL ATAJO");

async function loadHist() {
  let r; try { r = await api("/api/jobs"); } catch (e) { setTimeout(loadHist, 5000); return; }
  if (!r.ok) return;
  const L = await r.json();
  $("#hist").innerHTML = L.map(s => `<li><a href="?job=${encodeURIComponent(s.id)}&k=${K}" data-id="${esc(s.id)}">${esc(s.archivo_original)}</a> — ${esc(s.estado)} · ${s.creado ? s.creado.replace("T", " ").slice(0, 16) : ""}</li>`).join("") || "<li>Ninguna todavía</li>";
  document.querySelectorAll("#hist a").forEach(a => a.onclick = e => { e.preventDefault(); poll(a.dataset.id); scrollTo(0, $("#job").offsetTop - 10); });
}

async function setupAccess() {
  if (IS_LOCAL) {
    $("#pcbox").hidden = false;
    try { const a = await (await api("/api/acceso")).json();
      $("#qr").src = "/qr.png?t=" + Date.now(); $("#iurl").textContent = a.iphone_url;
      if (a.nube) $("#pcbox summary").textContent = "📱 Instalar en iPhone (versión nube, funciona sin la PC)";
      if (a.mdns) $("#ipalt").textContent = "Si el iPhone no abre esa dirección, usá: " + a.ip_url; } catch (e) {}
  } else { $("#phonebox").hidden = false; }
}

$("#file").onchange = e => { const fs = [...e.target.files]; e.target.value = "";
  if (fs.length > 1) uploadBatch(fs); else if (fs[0]) upload(fs[0]); };
const dz = $("#drop");
["dragenter", "dragover"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove("over"); }));
dz.addEventListener("drop", e => { const fs = [...e.dataTransfer.files]; if (fs.length > 1) uploadBatch(fs); else if (fs[0]) upload(fs[0]); });
document.addEventListener("visibilitychange", () => { if (!document.hidden && cur && ["en_cola", "procesando"].includes(cur.estado)) poll(cur.id); });

if (location.protocol === "https:" && "serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
if (!Rec.supported()) { $("#recStart").disabled = true; $("#recHint").textContent = "Este navegador no permite grabar (necesita HTTPS o localhost y permiso de micrófono)."; }
const q = new URLSearchParams(location.search).get("job");
const last = store.get("lastJob");
if (q) poll(q); else if (last && last.id && !last.visto) poll(last.id);
loadHist(); setupAccess(); loadLocal(); checkRecover();
window.__raizApp = {openDone, loadLocal, checkRecover, uploadBatch};
})();
