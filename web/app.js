/* raceway 控制台 —— Preact + HTM 轻量实现（视觉风格对齐 logpilot web）
 *
 * 数据面完全复用 bench-compose.py serve 的既有 JSON API：
 *   GET  /api/bootstrap | /api/history | /api/campaigns | /api/queue | /api/logtail
 *   GET  /api/exports | /api/trend
 *   POST /api/render | /api/preview | /api/start | /api/stop | /api/doctor
 *        /api/compare | /api/tracks | /api/apply    （写操作带 X-Raceway-CSRF）
 * 前端不再有任何业务逻辑：矩阵推导、dry-run、互斥预检全在服务端。
 */
'use strict';
const { h, render } = preact;
const { useState, useEffect, useRef, useMemo } = preactHooks;
const html = htm.bind(h);

// ==================== API 封装 ====================
const CSRF = { token: '' };
function writeHeaders() {
  return { 'Content-Type': 'application/json', 'X-Raceway-CSRF': CSRF.token };
}
async function getJSON(path, opts) {
  const r = await fetch(path, Object.assign({ cache: 'no-store' }, opts));
  let j = null;
  try { j = await r.json(); } catch (e) { j = { error: '响应不是 JSON' }; }
  return { status: r.status, j };
}
async function postJSON(path, body) {
  const r = await fetch(path, { method: 'POST', headers: writeHeaders(), body: JSON.stringify(body || {}) });
  let j = null;
  try { j = await r.json(); } catch (e) { j = { error: '响应不是 JSON' }; }
  // 403 有话直说（多半是服务重启后 CSRF 变了）：别让按钮看起来像"操作失败"
  if (r.status === 403) toast.err(j.error || '请求被拒绝（控制台可能已重启，刷新页面重试）');
  return { status: r.status, j };
}

// ==================== 通用工具 ====================
const VT = { VALID: '✓已跑', DEGRADED: '~降级', HALF: '半', SKIP: '跳过', FAIL: '✗失败', UNKNOWN: '?' };
const VC = { VALID: 'v-ok', DEGRADED: 'v-deg', HALF: 'v-half', SKIP: 'v-skip', FAIL: 'v-fail', UNKNOWN: 'v-none' };
const BT = { VALID: 'badge-ok', DEGRADED: 'badge-deg', HALF: 'badge-half', SKIP: 'badge-skip', FAIL: 'badge-fail', UNKNOWN: 'badge-none' };

function fmtMin(m) {
  m = Math.round(m || 0);
  if (!m) return '0 分钟';
  return (m >= 60 ? Math.floor(m / 60) + ' 小时 ' : '') + (m % 60) + ' 分钟';
}
function fmtN(v) { return (v == null) ? '—' : v.toLocaleString('en-US', { maximumFractionDigits: 1 }); }
function fmtTime(sec) { return sec ? new Date(sec * 1000).toLocaleString() : '-'; }

// toast：右下角提示条（替代旧版顶部一行小字）
const toast = (() => {
  let fn = null;
  const show = (cls, text) => fn && fn(cls, text);
  return {
    bind: (f) => { fn = f; },
    info: (t) => show('', t),
    ok: (t) => show('ok', t),
    err: (t) => show('err', t),
  };
})();

// ==================== 图标（内联 SVG，描边风格） ====================
function Icon({ d, size = 15 }) {
  return html`<svg width=${size} height=${size} viewBox="0 0 24 24" fill="none"
    stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d=${d}/></svg>`;
}
const ICONS = {
  compose: 'M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z',
  progress: 'M22 12h-4l-3 9L9 3l-3 9H2',
  history: 'M3 3v18h18M7 14l4-4 3 3 5-6',
  trend: 'M12 8v4l3 3M21 12A9 9 0 1 1 12 3a9 9 0 0 1 9 9z',
  ops: 'M4 7h16M4 12h16M4 17h10M18 15l3 3-3 3',
};

// ==================== 应用根 ====================
const TABS = [
  { id: 'compose', num: '①', label: '启动排程', title: '启动排程' },
  { id: 'progress', num: '②', label: '运行进度', title: '运行进度' },
  { id: 'history', num: '③', label: '历史与结果', title: '历史与结果' },
  { id: 'trend', num: '④', label: '趋势', title: '指标趋势' },
  { id: 'ops', num: '⑤', label: '赛道与入库', title: '赛道与入库' },
];

function App() {
  const [boot, setBoot] = useState(null);
  const [bootErr, setBootErr] = useState('');
  const [tab, setTabRaw] = useState('compose');
  const [toast_, setToast_] = useState(null); // {cls, text, at}
  const toastTimer = useRef(null);

  // compose 表单状态提升到 App：「按此重排」要跨页预填
  const [form, setForm] = useState(null); // 见 ComposeView 的默认值
  const [outBox, setOutBox] = useState('（空）'); // compose 页的输出框（dry-run/起跑/体检共用）
  const [schedBox, setSchedBox] = useState('（未生成）');

  // campaigns：进度页与历史页共用
  const [camps, setCamps] = useState([]);
  const [curCid, setCurCid] = useState(null);

  toast.bind((cls, text) => {
    setToast_({ cls, text, at: Date.now() });
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setToast_(null), cls ? 5000 : 3500);
  });

  useEffect(() => {
    getJSON('/api/bootstrap').then(({ status, j }) => {
      if (status !== 200) { setBootErr(j.error || ('HTTP ' + status)); return; }
      CSRF.token = j.csrf_token || '';
      setBoot(j);
      setForm(defaultForm(j));
      loadCampaigns();
    }).catch((e) => setBootErr(String(e)));
  }, []);

  async function loadCampaigns(selectCid) {
    try {
      const { j } = await getJSON('/api/campaigns');
      const list = j.campaigns || [];
      setCamps(list);
      setCurCid((prev) => {
        let cid = selectCid || prev;
        if (!cid) cid = j.active || (list[0] ? list[0].cid : null);
        return list.some((c) => c.cid === cid) ? cid : (list[0] ? list[0].cid : null);
      });
    } catch (e) { /* 网络抖动下轮再试 */ }
  }

  function setTab(id) {
    setTabRaw(id);
    if (id === 'progress' || id === 'history') loadCampaigns();
  }

  // 「起跑成功 → 直接带到进度页盯它」：compose 页发事件，这里接住
  useEffect(() => {
    const onView = (e) => { setCurCid(e.detail); setTabRaw('progress'); };
    window.addEventListener('raceway-view', onView);
    return () => window.removeEventListener('raceway-view', onView);
  }, []);

  // 勾选偏好即改即存（与旧版 savePrefs-on-change 行为一致）
  useEffect(() => {
    if (!form) return;
    try {
      localStorage.setItem('raceway.compose', JSON.stringify({
        cars: form.cars, tracks: form.tracks, custom: form.custom,
        rounds: form.rounds, recall: form.recall, est: form.est,
        ds_hours: +form.ds_hours, include_covered: form.incov, tag: form.tag,
      }));
    } catch (e) { /* 隐私模式忽略 */ }
  }, [form]);

  // 「按此重排」：拿该场 campaign 的 车×赛道×轮次 预填启动排程
  async function reorderCamp(cid) {
    const { j } = await getJSON('/api/queue?cid=' + encodeURIComponent(cid));
    if (j.error) { toast.err(j.error); return; }
    // 排程模式下起跑头 configs=[]，车名从逐场明细里取
    const cars = [...new Set((j.fields || []).map((f) => f.car).filter(Boolean))];
    const tracks = (j.tracks && j.tracks.length)
      ? j.tracks
      : [...new Set((j.fields || []).map((f) => f.track).filter(Boolean))];
    setForm((f) => Object.assign({}, f, {
      cars, tracks, custom: '', rounds: j.rounds || 1,
      tag: ((cid || '').match(/^([A-Za-z][A-Za-z0-9]*)-\d{8}-\d{6}/) || ['', ''])[1],
    }));
    setTab('compose');
    toast.ok('已按 ' + cid + ' 预填（车 ' + (cars.join('/') || '?') + ' × ' + tracks.length + ' 赛道 × ' + (j.rounds || '?') + ' 轮）—— 检查后点「生成排程」');
  }

  if (bootErr) {
    return html`<div class="content"><div class="card">
      <div class="card-title">控制台连接失败</div>
      <p class="muted">${bootErr}</p>
      <button class="btn btn-secondary" onClick=${() => location.reload()}>重试</button>
    </div></div>`;
  }
  if (!boot || !form) return html`<div class="content"><p class="muted">连接中…</p></div>`;

  const active = TABS.find((t) => t.id === tab);
  const connText = (boot.ssh ? '远端 ' + boot.ssh : '本地') + ' · ' + boot.root;
  return html`
    <div class="shell">
      <aside class="sidebar">
        <div class="brand">
          <div class="brand-mark">rw</div>
          <div>
            <div class="brand-name">raceway</div>
            <div class="brand-sub">压测编排控制台</div>
          </div>
        </div>
        <nav class="nav">
          ${TABS.map((t) => html`
            <button key=${t.id} class="nav-item ${tab === t.id ? 'on' : ''}" onClick=${() => setTab(t.id)}>
              <span class="nav-num">${t.num}</span><${Icon} d=${ICONS[t.id]} /><span>${t.label}</span>
            </button>`)}
        </nav>
        <div class="side-foot">
          ${boot.ssh ? '远端 ' + boot.ssh : '本地模式'}<br/>
          raceway 根 ${boot.root}<br/>
          车源 ${boot.cars_file || '-'}
        </div>
      </aside>
      <div class="main">
        <header class="topbar">
          <h1>${active.title}</h1>
          <div class="spacer"></div>
          <span class="conn-chip" title="${'raceway 根 ' + boot.root + ' · 车源 ' + boot.cars_file}">${connText}</span>
          <${DoctorBtn} boot=${boot} onOut=${(t) => { setOutBox(t); }} goCompose=${() => setTab('compose')} />
        </header>
        <div class="content">
          ${boot.problems && boot.problems.length ? html`
            <div class="warn-box">车定义文件存在问题（${boot.problems.length} 条）：
              ${boot.problems.map((p, i) => html`<div key=${i}>${typeof p === 'string' ? p : (p.join ? p.join('：') : JSON.stringify(p))}</div>`)}
            </div>` : null}
          ${tab === 'compose' && html`
            <${ComposeView} boot=${boot} form=${form} setForm=${setForm}
              schedBox=${schedBox} setSchedBox=${setSchedBox}
              outBox=${outBox} setOutBox=${setOutBox} />`}
          ${tab === 'progress' && html`
            <${ProgressView} boot=${boot} camps=${camps} curCid=${curCid}
              setCurCid=${setCurCid} reload=${loadCampaigns} />`}
          ${tab === 'history' && html`
            <${HistoryView} boot=${boot} camps=${camps} reload=${loadCampaigns}
              onView=${(cid) => { setCurCid(cid); setTab('progress'); }}
              onReorder=${reorderCamp} />`}
          ${tab === 'trend' && html`<${TrendView} boot=${boot} />`}
          ${tab === 'ops' && html`<${OpsView} setToast=${toast} />`}
        </div>
      </div>
    </div>
    ${toast_ && html`<div class="toast ${toast_.cls}">${toast_.text}</div>`}
  `;
}

// ==================== 顶栏：环境体检 ====================
function DoctorBtn({ boot, onOut, goCompose }) {
  const [busy, setBusy] = useState(false);
  async function run() {
    setBusy(true);
    toast.info('环境体检中…（远端 doctor.sh，约 30-60 秒）');
    goCompose();
    onOut('体检中…');
    try {
      const { status, j } = await postJSON('/api/doctor');
      if (status !== 200) { onOut('体检失败: ' + (j.error || '未知')); toast.err('体检失败'); return; }
      onOut(j.result);
      toast[j.rc == 0 ? 'ok' : 'err'](j.rc == 0 ? '环境体检全部通过 ✔' : '体检有失败项（见输出里的 ✘）');
    } catch (e) { onOut('体检请求失败: ' + e); toast.err('体检请求失败'); }
    finally { setBusy(false); }
  }
  return html`<button class="btn btn-ghost btn-sm" disabled=${busy} onClick=${run}
    title="远端跑 doctor.sh：基础工具/env 配置/拓扑/虚机连通/语料/指标库，纯只读">🩺 环境体检</button>`;
}

// ==================== ① 启动排程 ====================
function defaultForm(boot) {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem('raceway.compose') || 'null'); } catch (e) { /* 忽略 */ }
  const allCars = (boot.cars || []).map((a) => a.name);
  const baseTracks = (boot.tracks || []).filter((t) => !t.variant).map((t) => t.name);
  if (!saved) return {
    cars: allCars, tracks: baseTracks, custom: '', rounds: 1,
    recall: 'recall', est: '', ds_hours: 12, tag: '', incov: false,
  };
  return {
    cars: saved.cars || allCars,
    tracks: saved.tracks || baseTracks,
    custom: saved.custom != null ? saved.custom : '',
    rounds: saved.rounds || 1,
    recall: saved.recall || 'recall',
    est: saved.est != null ? saved.est : '',
    ds_hours: saved.ds_hours != null ? saved.ds_hours : 12,
    tag: saved.tag != null ? saved.tag : '',
    incov: !!saved.include_covered,
  };
}

function ComposeView({ boot, form, setForm, schedBox, setSchedBox, outBox, setOutBox }) {
  const [hist, setHist] = useState(undefined); // undefined=加载中 null=不可用
  const previewRef = useRef(null); // {key, digest}

  const tracks = boot.tracks || [];
  const baseTracks = useMemo(() => tracks.filter((t) => !t.variant), [tracks]);
  const loadHist = async () => {
    setHist(undefined);
    const { j } = await getJSON('/api/history');
    setHist(j.available ? j : null);
  };
  useEffect(() => { loadHist(); }, []);

  function matrix() {
    const m = {
      cars: form.cars, tracks: form.tracks.concat(form.custom.split(',').map((s) => s.trim()).filter(Boolean)),
      rounds: +form.rounds, recall: form.recall, est: form.est,
      ds_hours: +form.ds_hours, include_covered: form.incov, tag: form.tag,
    };
    try { localStorage.setItem('raceway.compose', JSON.stringify(m)); } catch (e) { /* 隐私模式忽略 */ }
    return m;
  }
  function windowHint(estTotal) {
    const ds = +form.ds_hours || 0;
    if (ds > 0 && estTotal > ds * 60)
      return '；⚠ 预计 ' + fmtMin(estTotal) + ' 超出 DS_HOURS=' + ds + 'h 窗口，放不下的场次会被 DEADLINE-SKIP';
    return '；预计总耗时约 ' + fmtMin(estTotal) + (ds > 0 ? '（DS_HOURS=' + ds + 'h 窗口内）' : '');
  }
  async function post(path) {
    const m = matrix(), key = JSON.stringify(m);
    if (path === '/api/start' && previewRef.current && previewRef.current.key === key) m.digest = previewRef.current.digest;
    const { status, j } = await postJSON(path, m);
    if (path === '/api/preview') previewRef.current = (status === 200 && j.rc === 0) ? { key, digest: j.digest } : null;
    return { status, j };
  }

  async function doRender() {
    const { status, j } = await post('/api/render');
    if (status !== 200) { setSchedBox('错误: ' + (j.error || '未知')); toast.err('生成失败'); return; }
    setSchedBox(j.schedule);
    toast.ok('生成 ' + j.rows + ' 场' + windowHint(j.est_total_min));
  }
  async function doPreview() {
    toast.info('dry-run 运行中…');
    const { status, j } = await post('/api/preview');
    if (status !== 200) { setOutBox('错误: ' + (j.error || '未知')); toast.err('dry-run 失败'); return; }
    setSchedBox(j.schedule); setOutBox(j.dryrun);
    toast[j.rc == 0 ? 'ok' : 'err'](j.rc == 0 ? ('dry-run 通过，可起跑；' + j.rows + ' 场' + windowHint(j.est_total_min || 0)) : 'dry-run 非零，勿起跑');
  }
  async function doStart() {
    if (!confirm('确认起跑？会占用压测集群。')) return;
    toast.info('校验并起跑…');
    const { status, j } = await post('/api/start');
    if (status !== 200) { setOutBox('错误: ' + (j.error || '未知')); toast.err('未起跑'); return; }
    setOutBox(j.result); toast.ok('已起跑');
    if (j.campaign) dispatchView(j.campaign); // 起跑成功 → 直接带到进度页盯它
  }
  function dispatchView(cid) {
    window.dispatchEvent(new CustomEvent('raceway-view', { detail: cid }));
  }

  // 兼容性检查：ZSTD 压缩与 index.source_reuse 是 Easysearch 专有，ES 上直接拒
  const bad = [];
  form.cars.forEach((name) => {
    const car = (boot.cars || []).find((x) => x.name === name) || {};
    form.tracks.concat(form.custom.split(',').map((s) => s.trim()).filter(Boolean)).forEach((t) => {
      const info = tracks.find((x) => x.name === t);
      const allow = info ? (info.engines || '') : '';
      if (allow && (car.engine || '').toLowerCase() !== allow)
        bad.push({ car: name, eng: car.engine || '?', allow, t });
    });
  });

  // 已跑过的格（将被剔除）
  const skip = [];
  if (hist && hist.available !== false && hist.cells) {
    const want = (name) => {
      const car = (boot.cars || []).find((x) => x.name === name);
      return car && car.build ? (name + '@' + car.build) : name;
    };
    Object.keys(hist.cells || {}).forEach((k) => {
      const [car, track, r] = k.split('|');
      if (!form.cars.includes(car) || !form.tracks.includes(track) || +r > form.rounds) return;
      const c = hist.cells[k], pb = (c.per_build || {})[want(car)];
      if (pb && (pb.verdict === 'VALID' || pb.verdict === 'DEGRADED')) skip.push(car + ' ' + track + ' r' + r);
    });
  }

  return html`
    <div class="card">
      <div class="card-title">排程矩阵</div>
      <p class="card-desc">矩阵勾选 → <b>自动剔除指标库里已跑过的格</b> → 生成排程 → dry-run 预览（runner 自己的语义）→ 互斥预检后起跑。
        交错由服务端复刻 <code>order_for_round</code>；本页不做任何绕过 precheck 的事。勾选会记住（localStorage），下次打开不用重勾。</p>

      <div class="card-title" style="margin-top:10px">车（来自车定义文件）</div>
      <div class="grid-checks">
        ${(boot.cars || []).map((a) => html`
          <label key=${a.name} class="check-item">
            <input type="checkbox" checked=${form.cars.includes(a.name)}
              onChange=${(e) => setForm((f) => Object.assign({}, f, { cars: toggled(f.cars, a.name, e.target.checked) }))} />
            <span>${a.name} <small>(${a.engine}${a.build ? '@' + a.build : ''}${a.jdk_dir ? ' + ' + a.jdk_dir.split('/').pop() : ''})</small></span>
          </label>`)}
      </div>

      <div class="card-title" style="margin-top:14px">赛道（全部可起跑的赛道，按族分组）</div>
      ${(boot.track_groups || []).map((g) => {
        const bases = baseTracks.filter((t) => t.group === g[0]);
        if (!bases.length) return null;
        return html`
          <div key=${g[0]}>
            <div class="track-group">${g[1]}</div>
            ${bases.map((b) => {
              const vs = tracks.filter((t) => t.base === b.name && t.variant);
              return html`<div key=${b.name} class="track-block">
                <label class="check-item" title=${b.name + (b.dataset ? ' · ' + b.dataset + ' 语料' : '') + ' · 基座（不带压缩变体）· ~' + b.est + 'min'}>
                  <input type="checkbox" checked=${form.tracks.includes(b.name)}
                    onChange=${(e) => setForm((f) => Object.assign({}, f, { tracks: toggled(f.tracks, b.name, e.target.checked) }))} />
                  <b>${b.label}</b> <small>(${b.name}, ~${b.est}min)${b.dataset ? ' · ' + b.dataset + ' 语料' : ''}</small>
                </label>
                ${vs.length ? html`
                  <details class="fold" style="margin-left:16px">
                    <summary>变体网格（压缩 × source_reuse）· ${vs.length} 个</summary>
                    <table class="vgrid">
                      <tr><th></th><th>source_reuse 关</th><th>source_reuse 开</th></tr>
                      ${(boot.codec_tokens || []).map((tok) => html`
                        <tr key=${tok}>
                          <th style="text-align:left">${boot.token_label[tok]}</th>
                          ${[false, true].map((sr) => {
                            const t = vs.find((x) => x.compression === boot.token_codec[tok] && x.source_reuse === sr);
                            if (!t) return html`<td key=${sr + '-x'} style="color:hsl(215 16% 70%)">—</td>`;
                            return html`<td key=${sr}>
                              <label class="check-item" title=${t.name + ' · ' + (t.engines ? '只支持 ' + t.engines : '两侧通用') + ' · ~' + t.est + 'min'}>
                                <input type="checkbox" checked=${form.tracks.includes(t.name)}
                                  onChange=${(e) => setForm((f) => Object.assign({}, f, { tracks: toggled(f.tracks, t.name, e.target.checked) }))} />
                                ${t.name} <small>(~${t.est}min)</small>
                              </label></td>`;
                          })}
                        </tr>`)}
                    </table>
                  </details>` : null}
              </div>`;
            })}
          </div>`;
      })}
      <div class="row">
        <span class="muted">自定义（逗号分隔，清单外的赛道）</span>
        <input type="text" style="width:26ch" placeholder="ds-hv1024,..."
          value=${form.custom} onInput=${(e) => setForm((f) => Object.assign({}, f, { custom: e.target.value }))} />
      </div>

      <div class="row" style="margin-top:8px">
        <label class="muted">轮次 <input type="number" style="width:4.5em" min="1" max="9" value=${form.rounds}
          onInput=${(e) => setForm((f) => Object.assign({}, f, { rounds: e.target.value }))} /></label>
        <label class="muted">探针
          <select value=${form.recall} onChange=${(e) => setForm((f) => Object.assign({}, f, { recall: e.target.value }))}>
            <option value="recall">recall（带召回探针）</option>
            <option value="norecall">norecall</option>
          </select></label>
        <label class="muted">est 覆盖 <input type="text" style="width:24ch" placeholder="ds-g1920=52,nested=40"
          title="赛道=分钟；留空用赛道清单的实测值" value=${form.est}
          onInput=${(e) => setForm((f) => Object.assign({}, f, { est: e.target.value }))} /></label>
        <label class="muted">DS_HOURS <input type="number" style="width:5em" min="0" value=${form.ds_hours}
          title="0=不启用窗底守卫" onInput=${(e) => setForm((f) => Object.assign({}, f, { ds_hours: e.target.value }))} /></label>
        <label class="muted">tag <input type="text" style="width:20ch" placeholder="留空自动生成" value=${form.tag}
          onInput=${(e) => setForm((f) => Object.assign({}, f, { tag: e.target.value }))} /></label>
      </div>
    </div>

    <div class="card">
      <div class="card-title">已入库历史（指标库 benchmark-result-race）<div class="spacer"></div>
        <button class="btn btn-ghost btn-sm" onClick=${loadHist}>刷新历史</button></div>
      ${hist === undefined && html`<p class="muted">查询指标库…</p>`}
      ${hist === null && html`<p class="st-bad">指标库不可用 —— 编排照常可用，但无法自动剔除已跑过的格（压测窗口内指标库停着是常态）。</p>`}
      ${hist && hist !== null && html`
        <p class="muted">指标库已连接：<b>${hist.summary.races}</b> 场次 / <b>${hist.summary.rows}</b> 行 /
          <b>${hist.summary.campaigns}</b> 个 campaign / <b>${hist.summary.tracks}</b> 条赛道 / <b>${hist.summary.cars}</b> 个车</p>
        <${HistMatrix} hist=${hist} boot=${boot} form=${form} setForm=${setForm} />
        <div class="legend">
          <span class="v-ok">✓已跑</span><span class="v-deg">~降级</span><span class="v-half">半(缺相位)</span>
          <span class="v-skip">跳过</span><span class="v-fail">✗失败</span><span class="v-none">未跑</span>
        </div>
        <div class="row">
          <label class="chk"><input type="checkbox" checked=${form.incov}
            onChange=${(e) => setForm((f) => Object.assign({}, f, { incov: e.target.checked }))} />
            编排时<b>含</b>已跑过的格（默认剔除，避免重跑已做过的测试）</label>
        </div>
        <div class="skipbox">${form.incov
          ? html`<b>已勾选"含已跑过的格"</b>：本次会重跑全部已跑过的格。`
          : html`本次矩阵将<b>剔除</b> ${skip.length} 格（指标库里当前 build 已跑过）：<br/>${skip.length ? skip.join('、') : '（无）'}`}
        </div>`}
    </div>

    <div class="card">
      <div class="card-title">车 × 变体 兼容性</div>
      ${!bad.length
        ? html`<p class="st-ok">✔ 当前矩阵的车×变体组合全部兼容</p>`
        : html`<p class="st-bad">✘ ${bad.length} 个组合不兼容（起跑会被拦下）</p>`}
      ${bad.length ? html`
        <details class="fold" open>
          <summary>展开明细（${bad.length} 条）</summary>
          <div style="font-size:12px;margin:4px 0 0 8px">
            ${bad.map((b, i) => html`<div key=${i} class="st-bad">${b.car}（${b.eng}）× ${b.t} —— 只支持 ${b.allow}</div>`)}
          </div>
        </details>` : null}
      <p class="card-desc">ZSTD 压缩与 <code>index.source_reuse</code> 是 Easysearch 专有
        （ES 的 <code>index.codec</code> 只有 default/best_compression，也不认 source_reuse）——
        编排台与 runner 都会在起跑前拦下不兼容的组合。</p>
    </div>

    <div class="row" style="margin:14px 0">
      <button class="btn btn-secondary" onClick=${doRender}>生成排程</button>
      <button class="btn btn-secondary" onClick=${doPreview}>上传 + dry-run 预览</button>
      <button class="btn btn-primary" onClick=${doStart}>互斥预检并起跑</button>
    </div>

    <div class="section-h">排程</div>
    <pre class="out light">${schedBox}</pre>
    <div class="section-h">输出（dry-run / 起跑 / 环境体检）</div>
    <pre class="out">${outBox}</pre>
  `;
}

function toggled(arr, item, on) {
  const set = new Set(arr);
  if (on) set.add(item); else set.delete(item);
  return [...set];
}

// 历史×矩阵：车行 × 赛道列，格内按轮次展开 per_build 判定
function HistMatrix({ hist, boot, form, setForm }) {
  const cars = hist.cars || [], tracks = hist.tracks || [];
  const selCars = new Set(form.cars), selTracks = new Set(form.tracks);
  function cellOf(car, track, round) { return hist.cells[car + '|' + track + '|' + round] || null; }
  function roundsOf(car, track) {
    const pre = car + '|' + track + '|', out = [];
    for (const k in hist.cells) if (k.indexOf(pre) === 0) out.push(parseInt(k.slice(pre.length), 10));
    return out.sort((x, y) => x - y);
  }
  return html`<div style="overflow:auto">
    <table class="hmatrix"><tbody>
      <tr><th>车 \\ 赛道</th>${tracks.map((t) => {
        const info = (boot.tracks || []).find((x) => x.name === t);
        const runnable = (boot.tracks || []).some((x) => x.name === t);
        // ds-* 族有两套数据集（jina 系 / GIST 系）—— 跨数据集的绝对值不能直接比
        const dsTxt = info && info.dataset ? html`<br/><small style="font-weight:400;color:hsl(172 66% 28%)">${info.dataset} 语料</small>` : null;
        return html`<th key=${t} class=${selTracks.has(t) ? 'cur' : 'dim'} title=${t}>${t}${dsTxt}${runnable ? null : html`<br/><small style="font-weight:400">仅历史</small>`}</th>`;
      })}</tr>
      ${cars.map((a) => {
        const carDef = (boot.cars || []).find((x) => x.name === a) || {};
        const want = carDef.build ? (a + '@' + carDef.build) : a;
        return html`<tr key=${a}>
          <th class=${selCars.has(a) ? 'cur' : 'dim'}>${a}<br/><small style="font-weight:400">${want}</small></th>
          ${tracks.map((t) => {
            const cells = roundsOf(a, t).map((r) => {
              const c = cellOf(a, t, r);
              let v = 'UNKNOWN', title = '未跑过';
              if (c) {
                const pb = (c.per_build || {})[want];
                if (pb) { v = pb.verdict; title = (pb.campaigns || []).join(',') + ' ' + v; }
                else { v = 'UNKNOWN'; title = '别的 build: ' + (c.builds || []).join(','); }
              }
              const inMatrix = selCars.has(a) && selTracks.has(t) && r <= form.rounds;
              return html`<div key=${r} class=${VC[v] + (inMatrix ? '' : ' past')}
                title=${a + ' ' + t + ' r' + r + ' · ' + title + (inMatrix ? '（在本次矩阵内）' : '（本次矩阵外）')}
                style="border:1px solid hsl(var(--border));padding:1px 5px;margin:1px 0;border-radius:3px">r${r} ${VT[v]}</div>`;
            });
            return html`<td key=${t}>${cells.length ? cells : html`<span class="v-none">—</span>`}</td>`;
          })}
        </tr>`;
      })}
    </tbody></table></div>`;
}

// ==================== ② 运行进度 ====================
function ProgressView({ boot, camps, curCid, setCurCid, reload }) {
  const [queue, setQueue] = useState(null);
  const [tail, setTail] = useState('（空）');
  const [auto, setAuto] = useState(true);
  const [notify, setNotify] = useState(false);
  const prevEnded = useRef(null);
  const timer = useRef(null);

  useEffect(() => {
    if (notify && typeof Notification !== 'undefined' && Notification.permission !== 'granted')
      Notification.requestPermission();
  }, [notify]);

  useEffect(() => {
    let alive = true;
    async function poll() {
      clearTimeout(timer.current);
      if (!curCid || !alive) return;
      const enc = encodeURIComponent(curCid);
      try {
        const [rq, rt] = await Promise.all([
          getJSON('/api/queue?cid=' + enc),
          getJSON('/api/logtail?cid=' + enc + '&n=40'),
        ]);
        if (rq.status === 200) setQueue(rq.j);
        if (rt.status === 200) setTail(rt.j.lines || '（空）');
      } catch (e) { /* 网络抖动下一轮再试 */ }
      if (alive && auto) timer.current = setTimeout(poll, 5000);
    }
    poll();
    return () => { alive = false; clearTimeout(timer.current); };
  }, [curCid, auto]);

  const s = (queue && queue.stats) || {};
  const run = queue && (queue.fields || []).find((f) => f.status === 'RUNNING');
  // 标签页标题当状态栏用：切到别的页也能瞄一眼进度
  if (run) document.title = '▶ ' + run.fid + (run.phase ? ' · ' + run.phase : '');
  else if (queue && queue.ended) document.title = '✔ ' + queue.campaign + '（' + s.valid + '/' + s.total + '）';
  else document.title = 'raceway 控制台';
  // 完成提醒：上一轮还没结束、这一轮结束了 → 系统通知（需授权）
  if (prevEnded.current === false && queue && queue.ended && notify
    && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
    try {
      new Notification('raceway: ' + queue.campaign + ' 已结束',
        { body: s.valid + '/' + s.total + ' 场有效' + (s.failed ? ('，失败 ' + s.failed + ' 场') : '') });
    } catch (e) { /* 忽略 */ }
  }
  if (queue) prevEnded.current = !!queue.ended;

  async function doStop() {
    if (!curCid) { toast.err('未选择 campaign'); return; }
    if (!confirm('确认停止 ' + curCid + '？\n将杀掉远端 runner 与 esrally 进程；campaign 目录保留，可断点续跑。')) return;
    toast.info('停止中…');
    const { status, j } = await postJSON('/api/stop', { cid: curCid });
    if (status !== 200) { setTail('停止失败: ' + (j.error || '未知')); toast.err('停止失败'); return; }
    setTail(j.result || '已停止'); toast.ok('已发送停止命令');
    reload(curCid);
  }

  function endState(c) { return !c.ended ? '运行中' : (/中止/.test(c.last || '') ? '已中止' : '已结束'); }
  // 没收尾但进度文件长期无更新 → runner 大概率已被杀（页面标出来，别让人干等）
  const warns = (queue && queue.warnings || []).slice();
  const camp = camps.find((x) => x.cid === (queue && queue.campaign));
  if (camp && !camp.ended && camp.mtime) {
    const staleMin = (Date.now() / 1000 - camp.mtime) / 60;
    if (staleMin > 30)
      warns.unshift('进度文件已 ' + Math.round(staleMin) + ' 分钟无更新 —— runner 可能已停止'
        + '（确认没人跑后：./close-campaign.sh ' + (queue && queue.campaign) + ' 补结束行收尾，或 CAMP_ID=' + (queue && queue.campaign) + ' 断点续跑）');
  }

  const done = s.total - s.pending;
  const eta = (s.pending > 0 && s.est_remaining_min > 0)
    ? new Date(Date.now() + s.est_remaining_min * 60000).toLocaleTimeString() : '-';
  const cards = [['总场次', s.total], ['有效', s.valid], ['失败', s.failed], ['跳过', s.skipped],
    ['待跑', s.pending], ['剩余约(分)', s.est_remaining_min != null ? Math.round(s.est_remaining_min) : '-'], ['预计完成', eta]];

  return html`
    <div class="card">
      <div class="row">
        <select style="min-width:320px" value=${curCid || ''}
          onChange=${(e) => setCurCid(e.target.value || null)}>
          ${camps.map((c) => html`
            <option key=${c.cid} value=${c.cid}>${c.cid}（${endState(c)} ${c.valid}/${c.total}）</option>`)}
        </select>
        <button class="btn btn-ghost btn-sm" onClick=${() => reload()}>刷新列表</button>
        <label class="chk"><input type="checkbox" checked=${auto} onChange=${(e) => setAuto(e.target.checked)} /> 自动刷新（5s）</label>
        <label class="chk"><input type="checkbox" checked=${notify} onChange=${(e) => setNotify(e.target.checked)} /> 完成提醒</label>
        <div class="spacer"></div>
        <button class="btn btn-danger btn-sm" onClick=${doStop}>■ 停止该 campaign</button>
      </div>

      <div class="cur-box">
        ${run && html`▶ [${run.seq}] <b>${run.fid}</b>${run.phase ? html` · 相位 <b>${run.phase}</b>` : ''}${run.elapsed_min != null ? html` · 已进行 <b>${run.elapsed_min.toFixed(1)}</b> 分钟（起 ${run.started}）` : ''}`}
        ${!run && queue && queue.ended && html`✔ 已结束：${s.valid}/${s.total} 场有效（起跑 ${queue.started_at || '-'}）`}
        ${!run && queue && !queue.ended && queue.started_at && html`… runner 在跑，但当前没有进行中的场次（可能在部署 / 卡点 / 收尾阶段）`}
        ${!queue && html`（等待选择 campaign）`}
      </div>
      <div class="pbar"><i style=${'width:' + (s.total ? 100 * done / s.total : 0) + '%'}></i></div>
      <div class="stat-cards">
        ${cards.map((c) => html`<div key=${c[0]} class="stat-card"><b>${c[1]}</b><span>${c[0]}</span></div>`)}
      </div>
      ${warns.map((w, i) => html`<div key=${i} class="warn-box">⚠ ${w}</div>`)}
      <table class="tbl">
        <thead><tr><th>seq</th><th>场次</th><th>状态</th><th>用时</th><th>recall</th><th>说明</th></tr></thead>
        <tbody>
          ${(queue && queue.fields || []).map((f) => {
            const cls = f.status === 'VALID' ? 'st-ok' : f.status === 'RUNNING' ? 'st-run'
              : (f.status === 'SKIP' || f.status === 'DEADLINE-SKIP') ? 'st-skip'
              : f.status === 'PENDING' ? 'st-pend' : 'st-bad';
            let notes = [];
            if (f.conflicts && f.conflicts.length) notes.push('⚠ 矛盾判定: ' + f.conflicts.join('/'));
            if (f.status === 'RUNNING' && f.phase)
              notes.push('相位 ' + f.phase + (f.elapsed_min ? (' · 已 ' + f.elapsed_min.toFixed(1) + ' 分钟') : ''));
            // 数据可信度证据链：runner 分相位采样的 ENV 判定（最坏相位×节点）
            if (f.env_verdict) {
              const tag = f.env_verdict === 'CONTAMINATED' ? 'ENV⛔' + (f.steal_max != null ? 'steal峰值' + f.steal_max + '%' : '')
                : f.env_verdict === 'WARN' ? 'ENV⚠' : f.env_verdict === 'OK' ? 'ENV✓' : '';
              if (tag) notes.push(tag);
            }
            if (f.detail) notes.push(f.detail);
            return html`<tr key=${f.seq} class=${f.status === 'RUNNING' ? 'now' : ''}>
              <td>${f.seq}</td><td>${f.fid}</td><td class=${cls}>${f.status}</td>
              <td>${f.duration || '-'}</td><td>${f.recall || '-'}</td><td>${notes.join('  ')}</td></tr>`;
          })}
          ${(!queue || !(queue.fields || []).length) && html`
            <tr><td colspan="6" class="muted">（等待选择 campaign）</td></tr>`}
        </tbody>
      </table>
    </div>
    <div class="card">
      <div class="card-title">PROGRESS.txt 尾部（实时日志）</div>
      <pre class="out">${tail}</pre>
    </div>
  `;
}

// ==================== ③ 历史与结果 ====================
function HistoryView({ boot, camps, reload, onView, onReorder }) {
  const [filter, setFilter] = useState('');
  // 两场对比
  const [cmpA, setCmpA] = useState(''), [cmpB, setCmpB] = useState('');
  const [carA, setCarA] = useState(''), [carB, setCarB] = useState('');
  const [cmpOut, setCmpOut] = useState(null); // {data, rc, text}
  const [cmpBusy, setCmpBusy] = useState(false);

  useEffect(() => {
    if (!cmpA && camps.length) setCmpA(camps[0].cid);
    if (!cmpB && camps.length > 1) setCmpB(camps[1].cid);
  }, [camps]);

  async function doCompare() {
    if (!cmpA || !cmpB) { toast.err('请选择两个 campaign'); return; }
    setCmpBusy(true);
    setCmpOut({ text: '对比中…（远端 car-compare，可能要几十秒）' });
    try {
      const { status, j } = await postJSON('/api/compare', { a: cmpA, b: cmpB, car_a: carA, car_b: carB, json: true });
      if (status !== 200) { setCmpOut({ text: '对比失败: ' + (j.error || '未知') }); toast.err('对比失败'); return; }
      setCmpOut({ data: j.data, rc: j.rc, text: (j.rc != 0 ? ('（退出码 ' + j.rc + '）\n') : '') + j.result });
      toast.ok('对比完成：' + cmpA + ' 的 ' + j.car.split(' vs ')[0] + ' vs ' + cmpB + ' 的 ' + j.car.split(' vs ')[1]);
    } catch (e) { setCmpOut({ text: '对比请求失败: ' + e }); toast.err('对比请求失败'); }
    finally { setCmpBusy(false); }
  }

  const f = filter.trim().toLowerCase();
  const list = f ? camps.filter((c) => c.cid.toLowerCase().indexOf(f) >= 0) : camps;
  function endState(c) { return !c.ended ? '运行中' : (/中止/.test(c.last || '') ? '已中止' : '已结束'); }
  function chipCls(s) { return s === '运行中' ? 'badge-run' : s === '已中止' ? 'badge-abort' : 'badge-end'; }

  return html`
    <div class="card">
      <div class="card-title">外部结果入口</div>
      <a href=${boot.kibana_url + '/app/dashboards'} target="_blank" rel="noreferrer">
        Kibana 看板（${boot.kibana_url}）→</a>
      <p class="card-desc" style="margin-top:4px">Kibana 若只听远端 127.0.0.1，先建隧道：
        ssh -L 5601:127.0.0.1:5601 ${boot.ssh || 'user@host'}</p>
    </div>

    <div class="card">
      <div class="card-title">两场对比（car-compare：逐轮实测 + 中位差值 Δ + 轮间极差）</div>
      <div class="row">
        <select value=${cmpA} onChange=${(e) => setCmpA(e.target.value)} style="max-width:240px">
          ${camps.map((c) => html`<option key=${c.cid} value=${c.cid}>${c.cid}</option>`)}
        </select>
        <input type="text" style="width:8ch" placeholder="车(自动)" value=${carA}
          onInput=${(e) => setCarA(e.target.value)} />
        <span class="muted">vs</span>
        <select value=${cmpB} onChange=${(e) => setCmpB(e.target.value)} style="max-width:240px">
          ${camps.map((c) => html`<option key=${c.cid} value=${c.cid}>${c.cid}</option>`)}
        </select>
        <input type="text" style="width:8ch" placeholder="车(自动)" value=${carB}
          onInput=${(e) => setCarB(e.target.value)} />
        <button class="btn btn-primary btn-sm" disabled=${cmpBusy} onClick=${doCompare}>对比</button>
      </div>
      <p class="card-desc">两个 campaign 作为两侧的取数池；车代号留空自动取（B 侧尽量取与 A 不同的车）。
        典型用法：两场各跑了 jdk25/kona25，对比 jdk25 vs kona25。
        Δ 着色：绿=B 更好，红=B 更差，|Δ|&lt;2% 视为持平。</p>
      ${cmpOut && html`<${CompareResult} out=${cmpOut} />`}
      ${cmpOut && html`
        <details class="fold"><summary>原始文本输出（复制进报告用）</summary>
          <pre class="out light" style="max-height:320px">${cmpOut.text}</pre></details>`}
    </div>

    <div class="card">
      <div class="card-title">全部 campaign<div class="spacer"></div>
        <input type="text" placeholder="输入 campaign 名关键字过滤" value=${filter}
          onInput=${(e) => setFilter(e.target.value)} /></div>
      <p class="card-desc">「看进度」跳到运行进度页看该场逐场明细；「按此重排」把该场的 车×赛道×轮次 预填进启动排程（可调整后重新生成）。列表按最近活动排序。</p>
      <table class="tbl">
        <thead><tr><th>campaign</th><th>状态</th><th>VALID / 数据行</th><th>最近活动</th><th>最后一条进度</th><th>操作</th></tr></thead>
        <tbody>
          ${list.map((c) => html`
            <tr key=${c.cid}>
              <td class="mono">${c.cid}</td>
              <td><span class="badge ${chipCls(endState(c))}">${endState(c)}</span></td>
              <td>${c.valid} / ${c.total}</td>
              <td>${fmtTime(c.mtime)}</td>
              <td style="white-space:normal;max-width:340px">${c.last || ''}</td>
              <td style="white-space:nowrap">
                <button class="btn btn-ghost btn-sm" onClick=${() => onView(c.cid)}>看进度</button>
                <button class="btn btn-ghost btn-sm" onClick=${() => onReorder(c.cid)}>按此重排</button>
              </td>
            </tr>`)}
          ${!list.length && html`<tr><td colspan="6" class="muted">（没有匹配的 campaign）</td></tr>`}
        </tbody>
      </table>
    </div>
  `;
}

// 对比结果结构化渲染：Δ 着色（方向感知：吞吐高好/时延低好），|Δ|<2% 持平
function DCell({ v, dir, dec }) {
  if (v == null) return html`<td class="num muted">n/a</td>`;
  let cls = 'num';
  if (Math.abs(v) >= 2) cls = ((dir === 'higher-better' ? v > 0 : v < 0) ? 'num d-up' : 'num d-down');
  return html`<td class=${cls}>${v > 0 ? '+' : ''}${v.toFixed(dec == null ? 1 : dec)}%</td>`;
}
function CompareResult({ out }) {
  const d = out.data;
  if (!d) return html`<p class="muted">（无结构化数据，看下方原始文本）</p>`;
  return html`
    <p class="muted">A=${d.a} B=${d.b} · campaign: ${d.campaigns.join(', ')}
      ${out.rc != 0 && html` · <b class="st-bad">断言未全部通过（退出码 ${out.rc}）</b>`}</p>
    ${(d.tracks || []).map((t) => html`
      <div key=${t.track}>
        <div class="section-h">${t.track}
          <span class="muted">A=${t.series_a} · B=${t.series_b} · 有效轮 A=${t.pool.A.rounds} B=${t.pool.B.rounds}</span></div>
        ${(t.pool.A.camps.length || t.pool.B.camps.length) ? html`
          <p class="muted">取数池 A: ${t.pool.A.camps.join(', ')} · B: ${t.pool.B.camps.join(', ')}</p>` : null}
        ${(t.write || []).length ? html`
          <table class="tbl">
            <thead><tr><th>任务 / 指标</th><th class="num">A 中位</th><th class="num">B 中位</th><th class="num">Δ(B/A)</th><th class="num">有效轮 A / B</th><th>方向</th></tr></thead>
            <tbody>${t.write.map((r, i) => html`
              <tr key=${i}><td>${r.task} / ${r.metric}</td>
                <td class="num">${fmtN(r.a_med)}</td><td class="num">${fmtN(r.b_med)}</td>
                <${DCell} v=${r.delta_pct} dir=${r.direction} />
                <td class="num">${r.a_n} / ${r.b_n}</td>
                <td class="muted">${r.direction === 'higher-better' ? '高好' : '低好'}</td></tr>`)}</tbody>
          </table>` : null}
        ${(t.query || []).length ? html`
          <table class="tbl">
            <thead><tr><th>查询操作</th><th class="num">A ops/s</th><th class="num">B ops/s</th><th class="num">Δ吞吐</th>
              <th class="num">A p50</th><th class="num">B p50</th><th class="num">Δp50</th>
              <th class="num">A p99</th><th class="num">B p99</th><th class="num">Δp99</th></tr></thead>
            <tbody>${t.query.map((r, i) => html`
              <tr key=${i}><td>${r.task}</td>
                <td class="num">${fmtN(r.a_tput)}</td><td class="num">${fmtN(r.b_tput)}</td><${DCell} v=${r.delta_tput} dir="higher-better" />
                <td class="num">${fmtN(r.a_p50)}</td><td class="num">${fmtN(r.b_p50)}</td><${DCell} v=${r.delta_p50} dir="lower-better" />
                <td class="num">${fmtN(r.a_p99)}</td><td class="num">${fmtN(r.b_p99)}</td><${DCell} v=${r.delta_p99} dir="lower-better" /></tr>`)}</tbody>
          </table>` : null}
      </div>`)}
    ${d.asserts && html`<p class=${d.asserts.fails ? 'st-bad' : 'st-ok'}>断言规则 ${d.asserts.rules.length} 条，FAIL ${d.asserts.fails} 条</p>`}
  `;
}

// ==================== ④ 趋势 ====================
function TrendView({ boot }) {
  const [car, setCar] = useState('');
  const [track, setTrack] = useState((boot.tracks || [])[0] ? boot.tracks[0].name : '');
  const [metric, setMetric] = useState('throughput');
  const [task, setTask] = useState('');
  const [hint, setHint] = useState('');
  const [res, setRes] = useState(null);
  const chartRef = useRef(null);

  async function doTrend() {
    if (!car.trim()) { toast.err('请填车代号（如 jdk25 / ez3000）'); return; }
    const [family, stat] = metric.split('|');
    setHint('查询指标库中…');
    try {
      const p = new URLSearchParams({ car: car.trim(), track, family });
      if (stat) p.set('stat', stat);
      if (task.trim()) p.set('task', task.trim());
      const { status, j } = await getJSON('/api/trend?' + p.toString());
      if (status !== 200) { setHint('查询失败: ' + (j.error || '未知')); return; }
      if (!j.available) { setHint('指标库不可用：' + (j.error || '未知')); setRes(j); return; }
      setHint('共 ' + j.n + ' 行 · 单位 ' + (j.unit || '?') + ' · ' +
        (j.direction === 'lower-better' ? '越低越好' : '越高越好'));
      setRes(j);
      renderChart(j);
    } catch (e) { setHint('请求失败: ' + e); }
  }
  function renderChart(j) {
    loadEcharts().then((ok) => {
      if (!ok) return; // 表格兜底
      const chart = echarts.init(chartRef.current);
      const legend = [], series = [], xs = new Set();
      (j.series || []).forEach((s) => {
        legend.push(s.key);
        (s.points || []).forEach((p) => { if (p.t) xs.add(p.t); });
      });
      const xarr = Array.from(xs).sort();
      (j.series || []).forEach((s) => {
        const m = {};
        (s.points || []).forEach((p) => { if (p.t) m[p.t] = p.v; });
        series.push({ name: s.key, type: 'line', connectNulls: true,
          data: xarr.map((t) => m.hasOwnProperty(t) ? m[t] : null) });
      });
      chart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { data: legend, top: 0, textStyle: { fontSize: 11 } },
        grid: { left: 60, right: 20, top: 36, bottom: 60 },
        xAxis: { type: 'category', data: xarr, axisLabel: { rotate: 30, fontSize: 10 } },
        yAxis: { type: 'value', name: j.unit || '', scale: true },
        series,
      });
      window.addEventListener('resize', () => chart.resize());
    });
  }
  return html`
    <div class="card">
      <div class="card-title">指标趋势（跨 campaign，只读指标库 benchmark-result-metric）</div>
      <div class="row">
        <input type="text" style="width:10ch" placeholder="jdk25" value=${car}
          onInput=${(e) => setCar(e.target.value)} />
        <select value=${track} onChange=${(e) => setTrack(e.target.value)}>
          ${(boot.tracks || []).map((t) => html`<option key=${t.name} value=${t.name}>${t.name}</option>`)}
        </select>
        <select value=${metric} onChange=${(e) => setMetric(e.target.value)}>
          <option value="throughput">吞吐（median）</option>
          <option value="latency|p50">时延 p50</option>
          <option value="latency|p99">时延 p99</option>
          <option value="error_rate">错误率</option>
        </select>
        <input type="text" style="width:14ch" placeholder="操作(留空=全部)" value=${task}
          onInput=${(e) => setTask(e.target.value)} />
        <button class="btn btn-primary btn-sm" onClick=${doTrend}>查趋势</button>
      </div>
      <p class="card-desc">每个点 = 一场 campaign 的一次有效测量；同车多 build 会分线（series_key 带 build）。</p>
      <p class="muted">${hint}</p>
      <div id="trchart" ref=${chartRef}></div>
      <${TrendTable} res=${res} />
    </div>
  `;
}
// echarts 按需加载（与旧版同一路径 /echarts.min.js，未随仓库分发则表格兜底）
let ECHARTS_OK;
function loadEcharts() {
  if (ECHARTS_OK !== undefined) return Promise.resolve(ECHARTS_OK);
  return new Promise((res) => {
    const s = document.createElement('script');
    s.src = '/echarts.min.js';
    s.onload = () => { ECHARTS_OK = typeof echarts !== 'undefined'; res(ECHARTS_OK); };
    s.onerror = () => { ECHARTS_OK = false; res(false); };
    document.head.appendChild(s);
  });
}
function TrendTable({ res }) {
  if (!res) return null;
  const series = res.series || [];
  if (!series.length)
    return html`<p class="muted">没有匹配的测量 —— 检查车代号/赛道/指标拼写，或先入库。</p>`;
  return html`
    <table class="tbl">
      <thead><tr><th>序列（series | 操作 | stat）</th><th>campaign</th><th class="num">时间</th><th class="num">值</th><th>变体</th></tr></thead>
      <tbody>
        ${series.map((s) => (s.points || []).map((p, i) => html`
          <tr key=${s.key + '-' + i}>
            <td>${i === 0 ? s.key : ''}</td><td>${p.campaign || ''}</td>
            <td class="num">${p.t || ''}</td><td class="num">${fmtN(p.v)}</td><td>${p.variant || ''}</td>
          </tr>`))}
      </tbody>
    </table>`;
}

// ==================== ⑤ 赛道与入库 ====================
function OpsView() {
  const [tracksOut, setTracksOut] = useState('（未运行）');
  const [exports, setExports] = useState([]);
  const [expInfo, setExpInfo] = useState('（先刷新导出清单）');
  const [exp, setExp] = useState('');
  const [es, setEs] = useState('https://127.0.0.1:9237');
  const [auth, setAuth] = useState('');
  const [insec, setInsec] = useState(true);
  const [drop, setDrop] = useState(false);
  const [apOut, setApOut] = useState('（未运行）');

  async function doTracks() {
    setTracksOut('读取中…');
    const { status, j } = await postJSON('/api/tracks');
    if (status !== 200) { setTracksOut('读取失败: ' + (j.error || '未知')); return; }
    // rc!=0 时 result 是 runner 的报错文本，不能当赛道清单渲染
    if (j.rc !== 0 && j.rc !== undefined) { setTracksOut('读取失败（rc=' + j.rc + '）：' + (j.result || '')); return; }
    setTracksOut(j.result);
  }
  async function loadExports() {
    setExpInfo('读取 exports/ 清单中…');
    const { status, j } = await getJSON('/api/exports');
    if (status !== 200) { setExpInfo('读取失败: ' + (j.error || '未知')); return; }
    const list = j.exports || [];
    setExports(list);
    if (list.length) setExp(list[0].tag);
    setExpInfo(list.length
      ? '断言未通过或缺失的导出，bench-apply 会在预检拒绝，不会发任何请求。'
      : '（exports/ 下没有可入库的导出）');
  }
  useEffect(() => { loadExports(); }, []);

  async function doApply() {
    if (!exp) { toast.err('请先刷新并选择导出'); return; }
    if (!es.trim()) { toast.err('请填 ES 地址'); return; }
    if (drop && !confirm('确认带 --drop 入库？\n会先删除目标集群的三张 benchmark-result-* 索引再重建。')) return;
    setApOut('入库中…（远端 bench-apply，全量预检通过才发送）');
    const { status, j } = await postJSON('/api/apply', { export: exp, es: es.trim(), auth, insecure: insec, drop });
    if (status !== 200) { setApOut('入库请求参数错误: ' + (j.error || '未知')); toast.err('参数错误'); return; }
    setApOut((j.rc != 0 ? ('（退出码 ' + j.rc + '）\n') : '') + j.result);
    toast[j.rc == 0 ? 'ok' : 'err'](j.rc == 0 ? '入库完成 ✔' : (j.rc == 2 ? '预检失败，未发送任何请求' : '有文档被拒收，见输出'));
  }

  return html`
    <div class="card">
      <div class="card-title">赛道目录（远端 run-bench.sh list：定义在不在位 / 近期 campaign）
        <div class="spacer"></div>
        <button class="btn btn-secondary btn-sm" onClick=${doTracks}>刷新赛道清单</button></div>
      <pre class="out light" style="max-height:420px">${tracksOut}</pre>
    </div>
    <div class="card">
      <div class="card-title">结果入库（exports/ → ES；bench-apply 预检不过不发任何请求，退出码 0=成功 1=有拒收 2=预检失败）</div>
      <div class="row">
        <select style="max-width:260px" value=${exp} onChange=${(e) => setExp(e.target.value)}>
          ${exports.map((e) => html`
            <option key=${e.tag} value=${e.tag}>${e.tag}（${e.assertions === 'pass' ? '✔断言通过' : '✘断言' + (e.assertions || '无')}，${e.counts ? e.counts.bench_metric + ' 行指标' : '?'}）</option>`)}
        </select>
        <button class="btn btn-ghost btn-sm" onClick=${loadExports}>刷新导出清单</button>
      </div>
      <p class="card-desc">${expInfo}</p>
      <div class="row">
        <label class="muted">ES <input type="text" style="width:32ch" value=${es} onInput=${(e) => setEs(e.target.value)} /></label>
        <label class="muted">口令 <input type="password" style="width:14ch" placeholder="user:pass（无鉴权集群留空）"
          value=${auth} onInput=${(e) => setAuth(e.target.value)} /></label>
        <label class="chk"><input type="checkbox" checked=${insec} onChange=${(e) => setInsec(e.target.checked)} /> 自签证书 --insecure</label>
        <label class="chk"><input type="checkbox" checked=${drop} onChange=${(e) => setDrop(e.target.checked)} />
          --drop（先删三张索引再重建；mapping 变更时必需）</label>
        <button class="btn btn-primary btn-sm" onClick=${doApply}>入库</button>
      </div>
      <p class="card-desc">口令只用于本次远端命令，不在服务端留存。</p>
      <pre class="out" style="max-height:320px">${apOut}</pre>
    </div>
  `;
}

render(html`<${App} />`, document.getElementById('app'));
