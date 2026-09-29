/* RAÍZ Transcribe — grabador con anti-pérdida.
 *
 * CAPA 1 (fuente de verdad): cada fragmento de MediaRecorder (cada 5 s) se escribe en IndexedDB del
 *   dispositivo ANTES de hacer cualquier otra cosa. Nada vive solo en RAM más de ~5 s.
 * CAPA 2: si hay red, cada fragmento se sube además al servidor (/api/rec/<id>/chunk). Si falla, se
 *   reintenta; al finalizar el servidor informa qué fragmentos faltan y se reenvían desde IndexedDB.
 * CAPA 3: al DETENER se arma el archivo final en el servidor (trabajo con original preservado y respaldado).
 *   La copia del dispositivo NO se borra sola.
 *
 * Honestidad del indicador: solo se muestra "GRABANDO" mientras llegan fragmentos y la pista del micrófono
 * está viva. Si iOS silencia/corta el micrófono (bloqueo, llamada, otra app), se marca INTERRUMPIDO con la
 * hora, se conserva lo grabado y CONTINUAR abre un segmento nuevo.
 */
(function () {
  const SLICE_MS = 5000;
  const DB_NAME = "raiz-transcribe", DB_VER = 1;
  let db = null;

  function openDB() {
    if (db) return Promise.resolve(db);
    return new Promise((res, rej) => {
      const r = indexedDB.open(DB_NAME, DB_VER);
      r.onupgradeneeded = () => {
        const d = r.result;
        if (!d.objectStoreNames.contains("recs")) d.createObjectStore("recs", {keyPath: "id"});
        if (!d.objectStoreNames.contains("chunks")) d.createObjectStore("chunks", {keyPath: "key"}).createIndex("rec", "recId");
      };
      r.onsuccess = () => { db = r.result; res(db); };
      r.onerror = () => rej(r.error);
    });
  }
  function tx(store, mode, fn) {
    return openDB().then(d => new Promise((res, rej) => {
      const t = d.transaction(store, mode); let out;
      t.oncomplete = () => res(out); t.onerror = () => rej(t.error); t.onabort = () => rej(t.error);
      out = fn(t.objectStore(store));
    }));
  }
  const reqP = r => new Promise((res, rej) => { r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
  const putRec = rec => tx("recs", "readwrite", s => { s.put(rec); });
  const getRec = id => openDB().then(d => reqP(d.transaction("recs").objectStore("recs").get(id)));
  const allRecs = () => openDB().then(d => reqP(d.transaction("recs").objectStore("recs").getAll()));
  const putChunk = c => tx("chunks", "readwrite", s => { s.put(c); });
  const recChunks = id => openDB().then(d => reqP(d.transaction("chunks").objectStore("chunks").index("rec").getAll(id)))
    .then(cs => cs.sort((a, b) => a.seg - b.seg || a.n - b.n));
  async function delRec(id) {
    const cs = await recChunks(id);
    await tx("chunks", "readwrite", s => cs.forEach(c => s.delete(c.key)));
    await tx("recs", "readwrite", s => { s.delete(id); });
  }

  function pickMime() {
    if (!window.MediaRecorder) return null;
    for (const m of ["audio/mp4", "audio/mp4;codecs=mp4a.40.2", "audio/webm;codecs=opus", "audio/webm"])
      if (MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) return m;
    return "";
  }
  const extFor = mime => (mime || "").includes("webm") ? ".webm" : ".m4a";
  const pad = n => String(n).padStart(2, "0");
  function autoName() { const d = new Date();
    return `Grabación ${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}-${pad(d.getMinutes())}`; }
  const newId = () => Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 10);

  // ---------------- estado del grabador activo ----------------
  const R = {rec: null, stream: null, mr: null, seg: 0, n: 0, lastChunkAt: 0, tickAt: 0, elapsed: 0, state: "idle",
             hiddenAt: 0, chunksWhileHidden: 0, wake: null, ctx: null, analyser: null, stopping: null, listeners: {}};
  const emit = (ev, ...a) => (R.listeners[ev] || []).forEach(f => { try { f(...a); } catch (e) { console.error(e); } });

  function setState(s, extra) { R.state = s; if (R.rec) { R.rec.state = s; Object.assign(R.rec, extra || {}); putRec(R.rec).catch(() => {}); } emit("state", s, R.rec); }

  async function wakeLock(on) {
    try {
      if (on && "wakeLock" in navigator && !R.wake) { R.wake = await navigator.wakeLock.request("screen"); R.wake.addEventListener("release", () => { R.wake = null; }); }
      if (!on && R.wake) { await R.wake.release(); R.wake = null; }
    } catch (e) { /* no soportado o denegado: no es crítico */ }
  }

  async function openSegment() {
    const constraints = {audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: true}};
    R.stream = window.__raizTestStream ? window.__raizTestStream() : await navigator.mediaDevices.getUserMedia(constraints);
    const track = R.stream.getAudioTracks()[0];
    track.onmute = () => interrupt("el sistema silenció el micrófono");
    track.onended = () => interrupt("el micrófono se cortó");
    const opts = {audioBitsPerSecond: 64000}; if (R.rec.mime) opts.mimeType = R.rec.mime;
    const mr = new MediaRecorder(R.stream, opts);
    if (!R.rec.mime) { R.rec.mime = mr.mimeType || ""; R.rec.ext = extFor(R.rec.mime); }
    R.mr = mr; R.n = 0;
    mr.ondataavailable = ev => onData(ev, mr);
    mr.onerror = ev => interrupt("error del grabador: " + ((ev.error && ev.error.name) || "desconocido"));
    mr.start(SLICE_MS);
    R.lastChunkAt = performance.now();
    meter();
  }

  function onData(ev, mr) {
    const rec = R.rec;
    if (!ev.data || !ev.data.size || !rec) return;
    const seg = mr === R.mr ? R.seg : (mr._seg ?? R.seg), n = mr === R.mr ? R.n++ : mr._n++;
    if (document.hidden) R.chunksWhileHidden++;
    R.lastChunkAt = performance.now();
    const c = {key: `${rec.id}|${seg}|${n}`, recId: rec.id, seg, n, blob: ev.data, t: Date.now(), up: false};
    // escrituras encadenadas: DETENER espera a que termine la última antes de dar por guardada la grabación
    R.writes = (R.writes || Promise.resolve()).then(async () => {
      try {
        await putChunk(c);                                           // CAPA 1: primero al disco del dispositivo
        rec.segs = rec.segs || {}; rec.segs[seg] = Math.max(rec.segs[seg] || 0, n + 1);
        rec.durMs = Math.max(rec.durMs || 0, Math.round(R.elapsed)); rec.bytes = (rec.bytes || 0) + ev.data.size;
        rec.updated = Date.now();
        await putRec(rec);
        emit("chunk", rec, c);
        Uploader.kick();                                             // CAPA 2: en segundo plano
      } catch (e) {
        emit("error", "No se pudo guardar un fragmento en el dispositivo: " + (e.message || e));
      }
    });
  }

  function closeSegment() {
    // detiene la sesión actual y espera el último fragmento (se guarda antes de resolver)
    const mr = R.mr; R.mr = null;
    if (!mr) return Promise.resolve();
    mr._seg = R.seg; mr._n = R.n;
    return new Promise(res => {
      mr.onstop = () => setTimeout(res, 150);
      try { if (mr.state !== "inactive") mr.stop(); else res(); } catch (e) { res(); }
      setTimeout(res, 4000);
    }).then(() => { if (R.stream) R.stream.getTracks().forEach(t => { t.onmute = t.onended = null; t.stop(); }); R.stream = null; stopMeter(); });
  }

  let tickTimer = null;
  function tick() {
    const now = performance.now();
    if (R.state === "grabando") {
      R.elapsed += now - R.tickAt;
      // vigilante: si no llegan fragmentos, NO seguimos mostrando "GRABANDO"
      if (now - R.lastChunkAt > SLICE_MS * 2.6 && !document.hidden) interrupt("dejaron de llegar datos del micrófono");
    }
    R.tickAt = now;
    emit("tick", R.elapsed, R.state);
  }

  async function interrupt(reason) {
    if (R.state !== "grabando" && R.state !== "pausado") return;
    const at = R.elapsed;
    try { if (R.mr && R.mr.state === "recording") R.mr.requestData(); } catch (e) {}
    setState("interrumpido", {interrupcion: {at, reason, when: Date.now()}});
    await closeSegment();
    await (R.writes || Promise.resolve());
    await wakeLock(false);
    emit("interrupted", R.rec, reason, at);
  }

  document.addEventListener("visibilitychange", async () => {
    if (!R.rec) return;
    if (document.hidden) {
      R.hiddenAt = performance.now(); R.chunksWhileHidden = 0;
      try { if (R.mr && R.mr.state === "recording") R.mr.requestData(); } catch (e) {}   // guardar lo último antes de que iOS suspenda
    } else {
      const gone = performance.now() - R.hiddenAt;
      const t = R.stream && R.stream.getAudioTracks()[0];
      const dead = !t || t.readyState === "ended" || t.muted || !R.mr || R.mr.state === "inactive";
      // si estuvo oculto más de 2 fragmentos sin producir datos → iOS dejó de capturar en ese lapso
      if (R.state === "grabando" && (dead || (gone > SLICE_MS * 2 && R.chunksWhileHidden === 0))) {
        await interrupt(dead ? "iOS cortó el micrófono mientras la app estaba en segundo plano"
                             : "iOS suspendió la app (pantalla bloqueada u otra app)");
      } else if (R.state === "grabando") { wakeLock(true); }
      R.tickAt = performance.now();
    }
  });
  window.addEventListener("pagehide", () => { try { if (R.mr && R.mr.state === "recording") R.mr.requestData(); } catch (e) {} });

  // medidor de nivel (evidencia visual de que entra sonido)
  function meter() {
    try {
      const AC = window.AudioContext || window.webkitAudioContext; if (!AC || !R.stream) return;
      R.ctx = R.ctx || new AC(); if (R.ctx.state === "suspended") R.ctx.resume();
      const src = R.ctx.createMediaStreamSource(R.stream); R.analyser = R.ctx.createAnalyser(); R.analyser.fftSize = 512;
      src.connect(R.analyser);
    } catch (e) { R.analyser = null; }
  }
  function stopMeter() { R.analyser = null; }
  function level() {
    if (!R.analyser) return 0;
    const a = new Uint8Array(R.analyser.fftSize); R.analyser.getByteTimeDomainData(a);
    let m = 0; for (const v of a) m = Math.max(m, Math.abs(v - 128)); return m / 128;
  }

  // ---------------- API pública ----------------
  const Rec = {
    supported: () => !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder && window.indexedDB),
    on(ev, f) { (R.listeners[ev] = R.listeners[ev] || []).push(f); },
    get state() { return R.state; }, get current() { return R.rec; }, get elapsed() { return R.elapsed; }, level,
    async start() {
      if (R.state === "grabando") return;
      try { if (navigator.storage && navigator.storage.persist) navigator.storage.persist(); } catch (e) {}
      R.rec = {id: newId(), name: autoName(), created: Date.now(), mime: pickMime(), ext: null, segs: {}, durMs: 0,
               bytes: 0, state: "grabando", job: null};
      R.rec.ext = extFor(R.rec.mime);
      R.seg = 0; R.elapsed = 0;
      await putRec(R.rec);
      try { await openSegment(); }
      catch (e) { await delRec(R.rec.id); R.rec = null; R.state = "idle"; throw e; }
      R.tickAt = performance.now(); clearInterval(tickTimer); tickTimer = setInterval(tick, 250);
      setState("grabando"); wakeLock(true);
    },
    pause() {
      if (R.state !== "grabando" || !R.mr) return;
      try { R.mr.requestData(); R.mr.pause(); } catch (e) {}
      setState("pausado");
    },
    async resume() {
      if (R.state === "pausado" && R.mr && R.mr.state === "paused") {
        const t = R.stream && R.stream.getAudioTracks()[0];
        if (t && t.readyState === "live" && !t.muted) { R.mr.resume(); R.lastChunkAt = performance.now(); R.tickAt = performance.now(); setState("grabando"); wakeLock(true); return; }
        await closeSegment();
      }
      if (R.state === "interrumpido" || R.state === "pausado") {   // segmento nuevo (tras interrupción de iOS)
        if (R.mr) await closeSegment();
        R.seg = Object.keys(R.rec.segs || {}).length ? Math.max(...Object.keys(R.rec.segs).map(Number)) + 1 : 0;
        setState("recuperando");
        try { await openSegment(); } catch (e) { setState("interrumpido"); throw e; }
        R.tickAt = performance.now(); setState("grabando", {interrupcion: null}); wakeLock(true);
      }
    },
    async stop() {
      if (!R.rec || ["idle", "guardado"].includes(R.state)) return R.rec;
      setState("guardando");
      await closeSegment();
      await (R.writes || Promise.resolve());   // el último fragmento ya está en el dispositivo
      clearInterval(tickTimer);
      R.rec.durMs = Math.round(R.elapsed);
      setState("guardado", {final: true, durMs: Math.round(R.elapsed)});
      await wakeLock(false);
      const rec = R.rec; R.rec = null; R.state = "idle";
      Uploader.kick();
      return rec;
    },
    // ----- grabaciones guardadas en el dispositivo -----
    list: () => allRecs().then(rs => rs.sort((a, b) => b.created - a.created)),
    unfinished: () => allRecs().then(rs => rs.filter(r => !r.final && (!R.rec || r.id !== R.rec.id))),
    get: getRec,
    async recover(id) {                  // grabación que quedó a medias (cierre/refresh): se da por guardada
      const r = await getRec(id); if (!r) return null;
      const cs = await recChunks(id); r.segs = {};
      for (const c of cs) r.segs[c.seg] = Math.max(r.segs[c.seg] || 0, c.n + 1);
      r.final = true; r.state = "guardado"; r.recuperada = true; await putRec(r); Uploader.kick(); return r;
    },
    async segmentBlobs(id) {             // un Blob reproducible por segmento
      const r = await getRec(id), cs = await recChunks(id), by = {};
      for (const c of cs) (by[c.seg] = by[c.seg] || []).push(c.blob);
      const type = (r.mime || "audio/mp4").split(";")[0];
      return Object.keys(by).sort((a, b) => a - b).map(s => new Blob(by[s], {type}));
    },
    async files(id) {                    // para compartir/guardar sin servidor: 1 archivo por segmento
      const r = await getRec(id), bs = await Rec.segmentBlobs(id), ext = r.ext || extFor(r.mime);
      return bs.map((b, i) => new File([b], bs.length > 1 ? `${r.name} (parte ${i + 1})${ext}` : `${r.name}${ext}`, {type: b.type}));
    },
    async rename(id, name) { const r = await getRec(id); if (!r) return; r.name = name; await putRec(r); return r; },
    async update(id, patch) { const r = await getRec(id); if (!r) return; Object.assign(r, patch); await putRec(r); return r; },
    async remove(id) { await delRec(id); },
    async recoveredDuration(id) { const r = await getRec(id); if (r.durMs) return r.durMs;
      const cs = await recChunks(id); return cs.length * SLICE_MS; },
  };

  // ---------------- CAPA 2: subida al servidor ----------------
  const Uploader = {
    busy: false, key: null,
    kick() { if (!this.busy) this.run(); },
    async run() {
      if (!Uploader.key || !navigator.onLine) return;
      this.busy = true;
      try {
        const recs = await allRecs();
        for (const r of recs) {
          if (r.job) continue;
          for (const c of (await recChunks(r.id)).filter(c => !c.up)) {
            const ok = await fetch(`/api/rec/${r.id}/chunk?seg=${c.seg}&n=${c.n}`, {method: "POST", body: c.blob,
              headers: {"x-raiz-key": Uploader.key, "content-type": "application/octet-stream"}}).then(x => x.ok).catch(() => false);
            if (!ok) throw new Error("sin conexión");
            c.up = true; await putChunk(c);
          }
          if (r.final && !r.job) await Rec.sync(r.id);
        }
      } catch (e) { setTimeout(() => Uploader.kick(), 15000); }
      finally { this.busy = false; }
    },
  };
  window.addEventListener("online", () => Uploader.kick());

  // Finalizar en el servidor: crea el trabajo (audio final preservado). Reenvía lo que falte.
  Rec.sync = async function (id) {
    const r = await getRec(id); if (!r || !r.final) return null; if (r.job) return r.job;
    for (let round = 0; round < 3; round++) {
      const res = await fetch(`/api/rec/${id}/finalize`, {method: "POST",
        headers: {"x-raiz-key": Uploader.key, "content-type": "application/json"},
        body: JSON.stringify({name: r.name + (r.ext || ".m4a"), mime: r.mime, segments: r.segs})});
      if (res.status === 409) {
        const {missing} = await res.json(); const cs = await recChunks(id);
        for (const [s, n] of missing) { const c = cs.find(x => x.seg === s && x.n === n); if (!c) continue;
          const ok = await fetch(`/api/rec/${id}/chunk?seg=${s}&n=${n}`, {method: "POST", body: c.blob,
            headers: {"x-raiz-key": Uploader.key, "content-type": "application/octet-stream"}}).then(x => x.ok).catch(() => false);
          if (!ok) throw new Error("sin conexión"); c.up = true; await putChunk(c); }
        continue;
      }
      if (!res.ok) throw new Error((await res.text()).slice(0, 200));
      const job = await res.json(); r.job = job.id; r.jobName = job.archivo_original; await putRec(r);
      emit("synced", r, job); return job.id;
    }
    throw new Error("faltan fragmentos");
  };
  Rec.setKey = k => { Uploader.key = k; Uploader.kick(); };
  Rec._internal = R;
  window.RaizRec = Rec;
})();
