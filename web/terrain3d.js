'use strict';
/* 3D terrain view: the current map view as a textured elevation mesh (WebGL, no libraries).
   Drag to rotate, scroll or pinch to zoom, shift-drag or two-finger drag to tilt. */
const Terrain3D = (() => {
  let cur = null;

  const VS = `attribute vec3 p; attribute vec2 uv; attribute vec3 n; uniform mat4 m; varying vec2 vUv; varying float vL;
    void main(){ vUv = uv; vec3 l = normalize(vec3(-0.5, 0.6, 0.65)); vL = 0.55 + 0.45 * max(dot(normalize(n), l), 0.0); gl_Position = m * vec4(p, 1.0); }`;
  const FS = `precision mediump float; uniform sampler2D t; varying vec2 vUv; varying float vL;
    void main(){ vec4 c = texture2D(t, vUv); gl_FragColor = vec4(c.rgb * vL, 1.0); }`;
  // buildings: flat-coloured extruded footprints
  const BVS = `attribute vec3 p; attribute vec3 c; uniform mat4 m; varying vec3 vC; void main(){ vC = c; gl_Position = m * vec4(p, 1.0); }`;
  const BFS = `precision mediump float; varying vec3 vC; void main(){ gl_FragColor = vec4(vC, 1.0); }`;
  const MAX_BLDG = 80000, BLDG_MAX_SPAN = 9000;  // metres of view width beyond which buildings are skipped

  function program(gl, vs, fs) {
    const pr = gl.createProgram();
    for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); gl.attachShader(pr, s) }
    gl.linkProgram(pr); return pr;
  }

  // ear clipping for a simple polygon (x[], y[] counter-clockwise) -> triangle vertex indices
  function earcut(x, y) {
    const n = x.length, out = [];
    if (n > 400) { for (let i = 1; i < n - 1; i++) out.push(0, i, i + 1); return out }  // clipping is O(n³): very rare giant outlines get a fan
    const V = [...Array(n).keys()];
    const area2 = (a, b, c) => (x[b] - x[a]) * (y[c] - y[a]) - (y[b] - y[a]) * (x[c] - x[a]);
    let guard = 0;
    while (V.length > 3 && guard++ < n * n) {
      let cut = false;
      for (let i = 0; i < V.length; i++) {
        const a = V[(i + V.length - 1) % V.length], b = V[i], c = V[(i + 1) % V.length];
        if (area2(a, b, c) <= 0) continue;  // reflex corner
        let inside = false;
        for (const p of V) {
          if (p === a || p === b || p === c || (x[p] === x[a] && y[p] === y[a]) || (x[p] === x[b] && y[p] === y[b]) || (x[p] === x[c] && y[p] === y[c])) continue;
          if (area2(a, b, p) >= 0 && area2(b, c, p) >= 0 && area2(c, a, p) >= 0) { inside = true; break }
        }
        if (inside) continue;
        out.push(a, b, c); V.splice(i, 1); cut = true; break;
      }
      if (!cut) break;
    }
    if (V.length === 3) out.push(V[0], V[1], V[2]);
    else for (let i = 1; i < V.length - 1; i++) out.push(V[0], V[i], V[i + 1]);  // degenerate leftovers
    return out;
  }

  function mat(fov, aspect, eye, target) {  // perspective * lookAt, column-major
    const f = 1 / Math.tan(fov / 2), near = 1, far = 1e7;
    const P = [f / aspect, 0, 0, 0, 0, f, 0, 0, 0, 0, (far + near) / (near - far), -1, 0, 0, 2 * far * near / (near - far), 0];
    const z = norm(sub(eye, target)), x = norm(cross([0, 0, 1], z)), y = cross(z, x);
    const V = [x[0], y[0], z[0], 0, x[1], y[1], z[1], 0, x[2], y[2], z[2], 0, -dot(x, eye), -dot(y, eye), -dot(z, eye), 1];
    const o = new Float32Array(16);
    for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) { let s = 0; for (let k = 0; k < 4; k++) s += P[k * 4 + r] * V[c * 4 + k]; o[c * 4 + r] = s }
    return o;
  }
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]], dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const norm = a => { const l = Math.hypot(...a) || 1; return [a[0] / l, a[1] / l, a[2] / l] };

  async function open(wrap, o) {
    close();
    const box = document.createElement('div'); box.className = 't3d';
    box.innerHTML = `<canvas></canvas><div class="bar"><button data-a="exit">✕ Exit 3D</button>
      <label title="Exaggerate heights">Height ×<input type="range" min="1" max="5" step="0.5" value="${o.z < 10 ? 3 : 1.5}"><span></span></label>
      <button data-a="reset">Reset view</button></div><div class="hint">Loading elevation…</div>`;
    wrap.appendChild(box);
    const cv = box.querySelector('canvas'), hint = box.querySelector('.hint'), range = box.querySelector('input');
    const me = cur = { box, alive: true };  // this session: checks after each await use me, not whatever cur is by then
    box.querySelector('[data-a=exit]').onclick = close;
    const onKey = e => { if (e.key === 'Escape') close() }; document.addEventListener('keydown', onKey); me.onKey = onKey;

    // the visible area, in the same web-mercator frame the 2D map uses
    const G = o.geo, Z = o.z, cx = G.lon2x(o.lon, Z), cy = G.lat2y(o.lat, Z), hw = o.w / 512, hh = o.h / 512;
    // 192 cells on the long side keeps the vertex count (193 x 193 at most) inside 16-bit indices
    const N = o.w >= o.h ? 192 : Math.max(32, Math.round(192 * o.w / o.h)), M = o.w >= o.h ? Math.max(32, Math.round(192 * o.h / o.w)) : 192;
    const lonAt = i => G.x2lon(cx - hw + 2 * hw * i / N, Z), latAt = j => G.y2lat(cy - hh + 2 * hh * j / M, Z);
    const span = z => [Math.floor(G.lon2x(lonAt(0), z)), Math.floor(G.lon2x(lonAt(N), z)), Math.floor(G.lat2y(latAt(0), z)), Math.floor(G.lat2y(latAt(M), z))];
    // elevation detail for this extent (17 = 1 m lidar), but never more than ~64 tiles to load
    let tz = Math.max(4, Math.min(o.maxTz || 12, Math.round(Z + Math.log2(Math.max(o.w, o.h) / 512))));
    for (let s = span(tz); tz > 4 && (s[1] - s[0] + 1) * (s[3] - s[2] + 1) > 64; s = span(--tz));
    // fetch every elevation tile that covers the view (and the 30 m ones, which back up any spot lidar misses)
    const need = new Set();
    for (const z of tz > 12 ? [tz, 12] : [tz]) { const [x0, x1, y0, y1] = span(z); for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) need.add(z + '/' + x + '/' + y) }
    for (let tries = 0; tries < 60; tries++) {
      let waiting = 0;
      for (const k of need) { const [z, x, y] = k.split('/').map(Number); if (o.getDem(z, x, y) === undefined) { o.demTile(z, x, y); waiting++ } }
      if (!waiting || !me.alive) break;
      hint.textContent = `Loading elevation… ${need.size - waiting}/${need.size}`;
      await new Promise(r => setTimeout(r, 150));
    }
    if (!me.alive) return;
    const height = (lat, lon) => {
      let fx, fy, x, y, t;
      for (let z = tz; z >= 4 && !t; z = z > 12 ? 12 : z - 1) {  // the chosen detail, else the 30 m tile, else coarser
        fx = G.lon2x(lon, z); fy = G.lat2y(lat, z); x = Math.floor(fx); y = Math.floor(fy); t = o.getDem(z, x, y);
      }
      if (!t) return 0;
      const px = Math.min(254.999, Math.max(0, (fx - x) * 256 - .5)), py = Math.min(254.999, Math.max(0, (fy - y) * 256 - .5)), i = px | 0, j = py | 0, a = px - i, b = py - j, H = t.h;
      return Math.max(-50, H[j * 256 + i] * (1 - a) * (1 - b) + H[j * 256 + i + 1] * a * (1 - b) + H[(j + 1) * 256 + i] * (1 - a) * b + H[(j + 1) * 256 + i + 1] * a * b);
    };
    // mesh in metres around the centre
    const mpp = 40075016 * Math.cos(o.lat * Math.PI / 180) / 2 ** Z / 256, W = o.w * mpp, Hm = o.h * mpp;
    const hts = new Float32Array((N + 1) * (M + 1));
    let hmin = Infinity, hmax = -Infinity;
    for (let j = 0; j <= M; j++) for (let i = 0; i <= N; i++) { const v = height(latAt(j), lonAt(i)); hts[j * (N + 1) + i] = v; if (v < hmin) hmin = v; if (v > hmax) hmax = v }
    const gl = cv.getContext('webgl', { antialias: true, preserveDrawingBuffer: false });
    if (!gl) { hint.textContent = '3D needs WebGL, which this browser has turned off.'; return }
    const prog = program(gl, VS, FS), bprog = program(gl, BVS, BFS);
    gl.useProgram(prog);
    const pos = new Float32Array((N + 1) * (M + 1) * 3), uv = new Float32Array((N + 1) * (M + 1) * 2), nrm = new Float32Array((N + 1) * (M + 1) * 3);
    const idx = new Uint16Array(N * M * 6); let q = 0;
    for (let j = 0; j < M; j++) for (let i = 0; i < N; i++) { const a = j * (N + 1) + i, b = a + 1, c = a + N + 1, d = c + 1; idx.set([a, c, b, b, c, d], q); q += 6 }
    const pb = gl.createBuffer(), ub = gl.createBuffer(), nb = gl.createBuffer(), ib = gl.createBuffer();
    function build(ex) {
      for (let j = 0; j <= M; j++) for (let i = 0; i <= N; i++) {
        const k = j * (N + 1) + i;
        pos[k * 3] = -W / 2 + W * i / N; pos[k * 3 + 1] = Hm / 2 - Hm * j / M; pos[k * 3 + 2] = (hts[k] - hmin) * ex;
        uv[k * 2] = i / N; uv[k * 2 + 1] = j / M;
      }
      for (let j = 0; j <= M; j++) for (let i = 0; i <= N; i++) {
        const k = j * (N + 1) + i, l = hts[j * (N + 1) + Math.max(0, i - 1)], r = hts[j * (N + 1) + Math.min(N, i + 1)], u = hts[Math.max(0, j - 1) * (N + 1) + i], d = hts[Math.min(M, j + 1) * (N + 1) + i];
        const n = norm([-(r - l) * ex / (2 * W / N), (u - d) * ex / (2 * Hm / M), 1]); nrm.set(n, k * 3);
      }
      for (const [buf, data] of [[pb, pos], [ub, uv], [nb, nrm]]) { gl.bindBuffer(gl.ARRAY_BUFFER, buf); gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW) }
      buildBldg(ex);
    }
    // attribute state is shared between programs in WebGL 1: point each program at its own buffers before drawing
    function attrs(pr, list) {
      for (let i = 0; i < 4; i++) gl.disableVertexAttribArray(i);
      for (const [buf, loc, size] of list) {
        const at = gl.getAttribLocation(pr, loc); if (at < 0) continue;
        gl.bindBuffer(gl.ARRAY_BUFFER, buf); gl.enableVertexAttribArray(at); gl.vertexAttribPointer(at, size, gl.FLOAT, false, 0, 0);
      }
    }
    // ground height of the (exaggerated) terrain mesh at mesh coordinates, bilinear like the rendered surface
    const ground = (mx, my) => {
      const fi = Math.max(0, Math.min(N - 1e-6, (mx + W / 2) / W * N)), fj = Math.max(0, Math.min(M - 1e-6, (Hm / 2 - my) / Hm * M));
      const i = fi | 0, j = fj | 0, a = fi - i, b = fj - j, k = j * (N + 1) + i;
      return (hts[k] * (1 - a) * (1 - b) + hts[k + 1] * a * (1 - b) + hts[k + N + 1] * (1 - a) * b + hts[k + N + 2] * a * b) - hmin;
    };
    // buildings: footprints in mesh metres + height, loaded after the terrain is up
    const B = { list: [], pb: gl.createBuffer(), cb: gl.createBuffer(), count: 0 };
    function buildBldg(ex) {
      if (!B.list.length) { B.count = 0; return }
      const P = [], C = [], L = norm([-0.5, 0.6, 0]);
      for (const f of B.list) {
        const { x, y, h } = f, n = x.length;
        let g0 = Infinity, g1 = -Infinity;
        for (let i = 0; i < n; i++) { const g = ground(x[i], y[i]); if (g < g0) g0 = g; if (g > g1) g1 = g }
        const z0 = f.mh ? g0 * ex + f.mh : g0 * ex - 1, z1 = (f.mh || f.part ? g0 : g1) * ex + h;  // parts of one tower share a ground level
        const tone = 0.9 + 0.1 * ((f.id * 2654435761 >>> 0) % 97) / 97;  // slight per-building variation
        for (const t of f.tri) P.push(x[t], y[t], z1), C.push(0.93 * tone, 0.91 * tone, 0.87 * tone);
        for (let i = 0; i < n; i++) {
          const j = (i + 1) % n, dx = x[j] - x[i], dy = y[j] - y[i], l = Math.hypot(dx, dy) || 1;
          const sh = (0.62 + 0.33 * Math.max(0, (dy * L[0] - dx * L[1]) / l)) * tone, lo = sh * 0.82;
          const r = 0.86, g = 0.82, b = 0.76;
          P.push(x[i], y[i], z0, x[j], y[j], z0, x[j], y[j], z1, x[i], y[i], z0, x[j], y[j], z1, x[i], y[i], z1);
          C.push(r * lo, g * lo, b * lo, r * lo, g * lo, b * lo, r * sh, g * sh, b * sh, r * lo, g * lo, b * lo, r * sh, g * sh, b * sh, r * sh, g * sh, b * sh);
        }
      }
      gl.bindBuffer(gl.ARRAY_BUFFER, B.pb); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(P), gl.STATIC_DRAW);
      gl.bindBuffer(gl.ARRAY_BUFFER, B.cb); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(C), gl.STATIC_DRAW);
      B.count = P.length / 3;
    }
    async function loadBldg() {
      if (W > BLDG_MAX_SPAN || Hm > BLDG_MAX_SPAN) return 'Zoom in to a town to see 3D buildings.';
      const sc = 2 ** (30 - Z), X0 = (cx - hw) * sc, X1 = (cx + hw) * sc, Y0 = (cy - hh) * sc, Y1 = (cy + hh) * sc;
      const tiles = [];
      for (let ty = Math.floor(Y0 / 65536); ty <= Math.floor(Y1 / 65536); ty++)
        for (let tx = Math.floor(X0 / 65536); tx <= Math.floor(X1 / 65536); tx++) tiles.push([tx, ty, Math.hypot((tx + .5) * 65536 - cx * sc, (ty + .5) * 65536 - cy * sc)]);
      tiles.sort((a, b) => a[2] - b[2]);  // nearest first, so a cap trims the edges
      const k = 256 * mpp / sc, ms = [], osm = [], seen = new Set();
      // response: [count of Microsoft int32s] + Microsoft records [n, h, X0, Y0, dX..] + OSM records [n, h, minh, part, X0, Y0, dX..]
      const parse = (buf, i, end, extra, out) => {
        while (i < end) {
          const n = buf[i], h = buf[i + 1], mh = extra ? buf[i + 2] : 0, part = extra ? buf[i + 3] : 0; i += extra ? 4 : 2;
          let X = buf[i], Y = buf[i + 1]; i += 2;
          const key = X + ',' + Y;  // a big footprint is filed under every bucket it reaches: keep one copy
          const x = [(X - cx * sc) * k], y = [-(Y - cy * sc) * k];
          for (let v = 1; v < n; v++) {
            X += buf[i]; Y += buf[i + 1]; i += 2;
            const px = (X - cx * sc) * k, py = -(Y - cy * sc) * k;
            if (px !== x[x.length - 1] || py !== y[y.length - 1]) { x.push(px); y.push(py) }  // repeated points stall the triangulation
          }
          if (x.length > 1 && x[0] === x[x.length - 1] && y[0] === y[y.length - 1]) { x.pop(); y.pop() }
          if (x.length < 3 || seen.has(key)) continue;
          seen.add(key);
          let mx = 0, my = 0; for (let v = 0; v < x.length; v++) { mx += x[v]; my += y[v] } mx /= x.length; my /= x.length;
          if (Math.abs(mx) > W / 2 || Math.abs(my) > Hm / 2) continue;
          out.push({ x, y, mx, my, h: h > 0 ? h / 10 : 4, mh: mh / 10, part, est: h > 0, osm: extra });
        }
      };
      for (const [tx, ty] of tiles) {
        let buf;
        try { const r = await fetch(`/api/map/bldg3d/${tx}/${ty}?v=${o.ver || ''}`); if (!r.ok) continue; buf = new Int32Array(await r.arrayBuffer()) } catch { continue }
        if (!me.alive) return;
        if (!buf.length) continue;
        parse(buf, 1, 1 + buf[0], false, ms); parse(buf, 1 + buf[0], buf.length, true, osm);
        if (ms.length + osm.length > MAX_BLDG * 2) break;
      }
      // OSM footprints with measured heights replace Microsoft's estimates; a building drawn as parts drops its outline
      const CELL = 40, grid = new Map(), cellsOf = f => {
        let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
        for (let v = 0; v < f.x.length; v++) { x0 = Math.min(x0, f.x[v]); x1 = Math.max(x1, f.x[v]); y0 = Math.min(y0, f.y[v]); y1 = Math.max(y1, f.y[v]) }
        const out = []; for (let i = Math.floor(x0 / CELL); i <= Math.floor(x1 / CELL); i++) for (let j = Math.floor(y0 / CELL); j <= Math.floor(y1 / CELL); j++) out.push(i + ',' + j);
        return out;
      };
      const inside = (f, px, py) => { let c = false; for (let a = 0, b = f.x.length - 1; a < f.x.length; b = a++) if ((f.y[a] > py) !== (f.y[b] > py) && px < (f.x[b] - f.x[a]) * (py - f.y[a]) / (f.y[b] - f.y[a]) + f.x[a]) c = !c; return c };
      const hit = (px, py, keep) => { for (const f of grid.get(Math.floor(px / CELL) + ',' + Math.floor(py / CELL)) || []) if (keep(f) && inside(f, px, py)) return f; return null };
      for (const f of osm) for (const c of cellsOf(f)) { if (!grid.has(c)) grid.set(c, []); grid.get(c).push(f) }
      for (const p of osm) if (p.part) { const o = hit(p.mx, p.my, f => !f.part && f !== p); if (o) o.hasParts = true }
      const list = osm.filter(f => !f.hasParts);
      let replaced = 0;
      for (const f of ms) { if (hit(f.mx, f.my, () => true)) { replaced++; continue } list.push(f) }
      list.sort((a, b) => Math.hypot(a.mx, a.my) - Math.hypot(b.mx, b.my));  // nearest first, so a cap trims the edges
      const capped = list.length > MAX_BLDG; if (capped) list.length = MAX_BLDG;
      list.forEach((f, i) => {
        let a2 = 0; for (let v = 0; v < f.x.length; v++) { const w = (v + 1) % f.x.length; a2 += f.x[v] * f.y[w] - f.x[w] * f.y[v] }
        if (a2 < 0) { f.x.reverse(); f.y.reverse() }  // counter-clockwise, so walls face outwards
        f.tri = earcut(f.x, f.y); f.id = i;
      });
      B.list = list; buildBldg(ex); render();
      if (!list.length) return '';
      const measured = list.filter(f => f.osm).length, guessed = list.filter(f => !f.est).length;
      return `${list.length.toLocaleString()} buildings${capped ? ' (nearest shown)' : ''} · ${measured ? `${measured.toLocaleString()} with mapped heights (OpenStreetMap), the rest ` : 'heights are '}satellite estimates${guessed ? `; ${guessed.toLocaleString()} without one shown one storey tall` : ''}`;
    }
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, idx, gl.STATIC_DRAW);
    // the 2D map (streets, labels, pins) becomes the ground texture
    const tex = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, tex);
    const snap = document.createElement('canvas'), S = Math.min(2048, gl.getParameter(gl.MAX_TEXTURE_SIZE)), sk = S / Math.max(o.w, o.h);
    snap.width = Math.round(o.w * sk); snap.height = Math.round(o.h * sk);
    snap.getContext('2d').drawImage(o.texture, 0, 0, snap.width, snap.height);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, snap);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.enable(gl.DEPTH_TEST); gl.enable(gl.CULL_FACE); gl.cullFace(gl.BACK); gl.frontFace(gl.CW);
    gl.disable(gl.CULL_FACE);

    const home = { yaw: -0.35, pitch: 0.62, dist: Math.max(W, Hm) * 1.05 };
    const cam = { ...home };
    let ex = +range.value;
    const setEx = () => { ex = +range.value; box.querySelector('.bar span').textContent = ex.toFixed(1); build(ex); render() };
    range.oninput = setEx;
    box.querySelector('[data-a=reset]').onclick = () => { Object.assign(cam, home); render() };
    function render() {
      if (!me.alive) return;
      const w = cv.clientWidth, h = cv.clientHeight, R = Math.min(2, devicePixelRatio || 1);
      if (cv.width !== Math.round(w * R)) { cv.width = Math.round(w * R); cv.height = Math.round(h * R) }
      gl.viewport(0, 0, cv.width, cv.height); gl.clearColor(0.62, 0.76, 0.84, 1); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      const zc = (hmax - hmin) * ex * 0.3, e = [Math.sin(cam.yaw) * Math.cos(cam.pitch) * cam.dist, -Math.cos(cam.yaw) * Math.cos(cam.pitch) * cam.dist, Math.sin(cam.pitch) * cam.dist + zc];
      const mvp = mat(0.8, w / h, e, [0, 0, zc]);
      gl.useProgram(prog); attrs(prog, [[pb, 'p', 3], [ub, 'uv', 2], [nb, 'n', 3]]);
      gl.uniformMatrix4fv(gl.getUniformLocation(prog, 'm'), false, mvp);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, ib);
      gl.drawElements(gl.TRIANGLES, idx.length, gl.UNSIGNED_SHORT, 0);
      if (B.count) {
        gl.useProgram(bprog); attrs(bprog, [[B.pb, 'p', 3], [B.cb, 'c', 3]]);
        gl.uniformMatrix4fv(gl.getUniformLocation(bprog, 'm'), false, mvp);
        gl.drawArrays(gl.TRIANGLES, 0, B.count);
      }
    }
    me.render = render;
    // orbit controls
    const pts = new Map(); let last = null, pinch = 0;
    cv.addEventListener('pointerdown', e => { cv.setPointerCapture(e.pointerId); pts.set(e.pointerId, [e.clientX, e.clientY]); last = [e.clientX, e.clientY, e.shiftKey]; if (pts.size === 2) { const [a, b] = [...pts.values()]; pinch = Math.hypot(a[0] - b[0], a[1] - b[1]) } });
    cv.addEventListener('pointermove', e => {
      if (!pts.has(e.pointerId)) return; pts.set(e.pointerId, [e.clientX, e.clientY]);
      if (pts.size === 2) { const [a, b] = [...pts.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]); if (pinch) cam.dist = Math.max(W * 0.08, Math.min(W * 4, cam.dist * pinch / d)); pinch = d; render(); return }
      const dx = e.clientX - last[0], dy = e.clientY - last[1]; last = [e.clientX, e.clientY, e.shiftKey];
      if (e.shiftKey || e.buttons === 2) cam.pitch = Math.max(0.12, Math.min(1.5, cam.pitch + dy * 0.005));
      else { cam.yaw -= dx * 0.006; cam.pitch = Math.max(0.12, Math.min(1.5, cam.pitch + dy * 0.004)) }
      render();
    });
    const up = e => { pts.delete(e.pointerId); pinch = 0 };
    cv.addEventListener('pointerup', up); cv.addEventListener('pointercancel', up);
    cv.addEventListener('contextmenu', e => e.preventDefault());
    cv.addEventListener('wheel', e => { e.preventDefault(); cam.dist = Math.max(W * 0.08, Math.min(W * 4, cam.dist * Math.exp(e.deltaY * 0.0015))); render() }, { passive: false });
    me.ro = new ResizeObserver(render); me.ro.observe(cv);
    setEx();
    const rel = hmax - hmin;
    const base = rel < 1 ? 'No elevation data here (ocean or outside the US).' : `${matchMedia('(pointer: coarse)').matches ? 'Drag to turn and tilt · pinch to zoom' : 'Drag to rotate · scroll to zoom · shift-drag to tilt'} · ${(window.lbMetric && window.lbMetric()) ? `${Math.round(hmin).toLocaleString()}–${Math.round(hmax).toLocaleString()} m` : `${Math.round(hmin * 3.28084).toLocaleString()}–${Math.round(hmax * 3.28084).toLocaleString()} ft`}`;
    hint.textContent = base + (W <= BLDG_MAX_SPAN && Hm <= BLDG_MAX_SPAN ? ' · loading buildings…' : '');
    const note = await loadBldg();
    if (!me.alive) return;
    hint.textContent = note ? `${base} · ${note}` : base;
    setTimeout(() => { if (me.alive) hint.style.opacity = '.75' }, 5000);
    setTimeout(() => { if (me.alive) hint.hidden = true }, 12000);  // it covers a lot of a phone screen
  }
  function close() {
    if (!cur) return;
    cur.alive = false; cur.ro && cur.ro.disconnect(); document.removeEventListener('keydown', cur.onKey);
    cur.box.remove(); cur = null;
  }
  return { open, close, isOpen: () => !!cur };
})();
