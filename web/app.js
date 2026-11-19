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

// 扩展功能组件（extras.js 注入依赖后提供；在文件末尾 render 前完成 setup）
let EX = null;

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
  tracks: 'M4 6h16M4 6v12a2 2 0 002 2h12a2 2 0 002-2V6M4 6a2 2 0 012-2h12a2 2 0 012 2M9 10h6M9 14h6',
  detail: 'M4 4h6v16H4zM14 4h6v7h-6zM14 15h6v5h-6z',
  report: 'M6 2h9l5 5v15H6zM15 2v5h5M9 12h6M9 16h6',
  export: 'M12 3v10M8 9l4 4 4-4M4 17v2a2 2 0 002 2h12a2 2 0 002-2v-2',
  assets: 'M20 7h-4V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2H4a1 1 0 00-1 1v4a1 1 0 001 1h1v6a2 2 0 002 2h10a2 2 0 002-2v-6h1a1 1 0 001-1V8a1 1 0 00-1-1zM10 5h4v2h-4z',
};

// ==================== 应用根 ====================
const TABS = [
  { id: 'compose', label: '启动排程', title: '启动排程' },
  { id: 'progress', label: '运行进度', title: '运行进度' },
  { id: 'history', label: '结果与趋势', title: '历史 · 对比 · 趋势' },
  { id: 'tracks', label: '赛道管理', title: '赛道目录与文件' },
  { id: 'assets', label: '引擎与JDK', title: '安装包与车依赖' },
  { id: 'export', label: '导出与入库', title: '导出与入库' },
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

  // 车表 / 赛道清单刷新：后端重读 cars 文件与 tracks/ 定义（?refresh=1），
  // 前端把已失效的勾选裁掉（自定义字段不动，用户勾选尽量保留）。
  async function reloadBoot(silent) {
    try {
      const { status, j } = await getJSON('/api/bootstrap?refresh=1');
      if (status !== 200) { toast.err(j.error || '刷新失败'); return; }
      CSRF.token = j.csrf_token || CSRF.token;
      setBoot(j);
      setForm((f) => f && Object.assign({}, f, {
        cars: f.cars.filter((c) => (j.cars || []).some((a) => a.name === c)),
        tracks: f.tracks.filter((t) => (j.tracks || []).some((x) => x.name === t)),
      }));
      if (!silent) toast.ok('车与赛道清单已刷新（' + (j.cars || []).length + ' 车 / ' + (j.tracks || []).length + ' 赛道）');
    } catch (e) { toast.err('刷新失败: ' + e); }
  }

  // 赛道文件保存/删除后自动刷新清单（extras.js 发事件；静默，避免与保存 toast 抢位）
  useEffect(() => {
    const onRefresh = () => reloadBoot(true);
    window.addEventListener('raceway-boot-refresh', onRefresh);
    return () => window.removeEventListener('raceway-boot-refresh', onRefresh);
  }, []);

  const goHash = useRef(null);
  function setTab(id, cidOpt) {
    const cid = cidOpt === undefined ? curCid : cidOpt;
    setTabRaw(id);
    if (id === 'progress' || id === 'history' || id === 'export') loadCampaigns(cid || undefined);
    if (goHash.current) goHash.current(id, id === 'progress' ? cid : null);
  }

  // 深链路由：#/progress/<cid> 之类，刷新/分享可直达；hash 变化反向切页
  useEffect(() => {
    if (!EX || !EX.installHashRoute) return;
    goHash.current = EX.installHashRoute(
      () => curCid,
      (tab, cid) => {
        const valid = TABS.some((t) => t.id === tab);
        if (!valid) return;
        setTabRaw(tab);
        if (cid) setCurCid(cid);
        if (tab === 'progress' || tab === 'history' || tab === 'export') loadCampaigns(cid || undefined);
      });
    return () => { goHash.current = null; };
  }, []);

  // 「起跑成功 → 直接带到进度页盯它」：compose 页发事件，这里接住
  useEffect(() => {
    const onView = (e) => { setCurCid(e.detail); setTab('progress', e.detail); };
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
        test_mode: !!form.test_mode,
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
              <${Icon} d=${ICONS[t.id]} /><span>${t.label}</span>
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
              outBox=${outBox} setOutBox=${setOutBox} reloadBoot=${reloadBoot} />`}
          ${tab === 'progress' && html`
            <${ProgressView} boot=${boot} camps=${camps} curCid=${curCid}
              setCurCid=${setCurCid} reload=${loadCampaigns} EX=${EX} />`}
          ${tab === 'history' && html`
            <${HistoryView} boot=${boot} camps=${camps} reload=${loadCampaigns}
              onView=${(cid) => { setCurCid(cid); setTab('progress', cid); }}
              onReorder=${reorderCamp} EX=${EX} curCid=${curCid} />`}
          ${tab === 'tracks' && html`<${TracksView} boot=${boot} EX=${EX} reloadBoot=${reloadBoot} />`}
          ${tab === 'assets' && html`<${AssetsView} reloadBoot=${reloadBoot} />`}
          ${tab === 'export' && html`<${ExportView} boot=${boot} camps=${camps} EX=${EX} />`}
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
    recall: 'recall', est: '', ds_hours: 12, tag: '', incov: false, test_mode: false,
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
    test_mode: !!saved.test_mode,
  };
}

// est 覆盖串 "a=1,b=2" ↔ 对象（编辑表格的数据形态）
function parseEst(s) {
  const m = {};
  (s || '').split(',').forEach((p) => {
    const [k, v] = p.trim().split('=');
    if (k && v && /^\d+$/.test(v.trim())) m[k.trim()] = +v.trim();
  });
  return m;
}
function estToStr(m) {
  return Object.keys(m).map((k) => k + '=' + m[k]).join(',');
}

function ComposeView({ boot, form, setForm, schedBox, setSchedBox, outBox, setOutBox, reloadBoot }) {
  const [hist, setHist] = useState(undefined); // undefined=加载中 null=不可用
  const previewRef = useRef(null); // {key, digest} ← 实时 render 喂给 dry-run/起跑
  const [live, setLive] = useState({ busy: false, err: '', rows: 0, estTotal: 0, tag: '' });
  const [mutex, setMutex] = useState(null); // {free, why} 起跑前探集群占用
  const [carsEditor, setCarsEditor] = useState(false);
  const [cluster, setCluster] = useState(null); // {available, engine, nodes, up}
  useEffect(() => {
    let alive = true;
    getJSON('/api/cluster-status').then(({ j }) => { if (alive && j && j.available) setCluster(j); }).catch(() => {});
    return () => { alive = false; };
  }, []);

  const tracks = boot.tracks || [];
  const baseTracks = useMemo(() => tracks.filter((t) => !t.variant), [tracks]);
  const loadHist = async () => {
    setHist(undefined);
    const { j } = await getJSON('/api/history');
    setHist(j.available ? j : null);
  };
  useEffect(() => { loadHist(); }, []);

  // 清单外的自定义赛道（逗号/空白分隔）
  const customTracks = useMemo(
    () => [...new Set(form.custom.split(/[\s,]+/).filter(Boolean))], [form.custom]);
  const allTracks = useMemo(
    () => [...new Set(form.tracks.concat(customTracks))], [form.tracks, customTracks]);

  function matrix() {
    const m = {
      cars: form.cars, tracks: form.tracks.concat(form.custom.split(',').map((s) => s.trim()).filter(Boolean)),
      rounds: +form.rounds, recall: form.recall, est: form.est,
      ds_hours: +form.ds_hours, include_covered: form.incov, tag: form.tag,
      test_mode: !!form.test_mode,
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
    if (previewRef.current && previewRef.current.key === key) m.digest = previewRef.current.digest;
    const { status, j } = await postJSON(path, m);
    return { status, j };
  }

  // 实时排程预览：/api/render 纯本地计算（不碰集群、不上传），勾选即算 —— 起跑前所见即所得。
  // 与 dry-run/起跑共用同一段矩阵材料，digest 随预览刷新，起跑前服务端仍会复核。
  const matrixKey = JSON.stringify([form.cars, allTracks, form.rounds, form.recall,
    form.est, form.ds_hours, form.incov, form.tag, !!form.test_mode]);
  useEffect(() => {
    if (!form.cars.length || !allTracks.length) {
      previewRef.current = null;
      setLive({ busy: false, err: '', rows: 0, estTotal: 0, tag: '' });
      setSchedBox('（未生成）');
      return;
    }
    let alive = true;
    setLive((s) => Object.assign({}, s, { busy: true }));
    const t = setTimeout(async () => {
      const m = matrix(), key = matrixKey;
      const { status, j } = await postJSON('/api/render', m);
      if (!alive) return;
      if (status !== 200) {
        previewRef.current = null;
        setLive({ busy: false, err: j.error || '未知错误', rows: 0, estTotal: 0, tag: '' });
      } else {
        previewRef.current = { key, digest: j.digest };
        setLive({ busy: false, err: '', rows: j.rows, estTotal: j.est_total_min, tag: j.tag });
        setSchedBox(j.schedule);
      }
    }, 350);
    return () => { alive = false; clearTimeout(t); };
  }, [matrixKey]);

  async function doPreview() {
    if (!previewRef.current) { toast.err('排程还没有预览成功 —— 先解决上方红字（兼容性/矩阵为空）'); return; }
    toast.info('上传 + dry-run 运行中…');
    const { status, j } = await post('/api/preview');
    if (status !== 200) { setOutBox('错误: ' + (j.error || '未知')); toast.err('dry-run 失败'); return; }
    setSchedBox(j.schedule); setOutBox(j.dryrun);
    toast[j.rc == 0 ? 'ok' : 'err'](j.rc == 0 ? ('dry-run 通过，可起跑；' + j.rows + ' 场' + windowHint(j.est_total_min || 0)) : 'dry-run 非零，勿起跑');
  }
  async function doStart() {
    if (!previewRef.current) { toast.err('排程还没有预览成功 —— 先解决红字提示'); return; }
    // 起跑前探一眼集群占用（start 服务端还会再查一次，这里只是提前给出可行动提示）
    try {
      const { j } = await getJSON('/api/mutex');
      if (j && j.free === false) {
        setMutex(j);
        toast.err('集群被占用，未起跑: ' + (j.why || 'runner 在跑') + '（进度页可看是谁）');
        return;
      }
      setMutex(j);
    } catch (e) { /* 预检失败不拦起跑 —— 服务端仍会做最终互斥检查 */ }
    if (!confirm('确认起跑？会占用压测集群。')) return;
    toast.info('校验并起跑…');
    const { status, j } = await post('/api/start');
    if (status !== 200) { setOutBox('错误: ' + (j.error || '未知')); toast.err('未起跑'); return; }
    setOutBox(j.result); toast.ok('已起跑');
    if (j.campaign) window.dispatchEvent(new CustomEvent('raceway-view', { detail: j.campaign }));
  }
  function copySched() {
    navigator.clipboard && navigator.clipboard.writeText(schedBox || '').then(
      () => toast.ok('排程已复制到剪贴板'),
      () => toast.err('复制失败（浏览器拒绝了剪贴板访问）'));
  }

  // 分组全选/反选：点组名一键勾/清该组全部赛道
  function toggleGroup(groupId) {
    const names = tracks.filter((t) => t.group === groupId).map((t) => t.name);
    const all = names.every((n) => form.tracks.includes(n));
    setForm((f) => Object.assign({}, f, { tracks: all
      ? f.tracks.filter((n) => names.indexOf(n) < 0)
      : [...new Set(f.tracks.concat(names))] }));
  }
  function toggleAllCars() {
    const names = (boot.cars || []).map((a) => a.name);
    const all = names.every((n) => form.cars.includes(n));
    setForm((f) => Object.assign({}, f, { cars: all ? [] : names }));
  }

  // 兼容性检查：ZSTD 压缩与 index.source_reuse 是 Easysearch 专有，ES 上直接拒
  const bad = [];
  form.cars.forEach((name) => {
    const car = (boot.cars || []).find((x) => x.name === name) || {};
    allTracks.forEach((t) => {
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
    <div class="compose-grid">
      <div class="compose-left">
    ${EX && EX.PresetBar ? html`<div class="card">
      <div class="card-title">编排预设</div>
      <${EX.PresetBar} form=${form} setForm=${setForm} />
    </div>` : null}

    <div class="card">
      <div class="card-title">排程矩阵
        <div class="spacer"></div>
        <button class="btn btn-ghost btn-sm" onClick=${() => setCarsEditor(true)}
          title="在 Web 里编辑车定义文件（runner 同口径校验，保存后车表立即刷新）">编辑车定义</button>
        <button class="btn btn-ghost btn-sm" onClick=${() => reloadBoot && reloadBoot()}
          title="重读车定义文件与 tracks/ 定义（车表变了、引擎支持变了都从这里拿最新）">↻ 刷新车与赛道</button>
      </div>
      <p class="card-desc">矩阵勾选 → <b>自动剔除指标库里已跑过的格</b> → dry-run 预览（runner 自己的语义）→ 互斥预检后起跑。
        交错由服务端复刻 <code>order_for_round</code>；本页不做任何绕过 precheck 的事。勾选会记住（localStorage），下次打开不用重勾。</p>

      <div class="card-title" style="margin-top:10px">车
        <div class="spacer"></div>
        <button class="btn btn-ghost btn-sm" onClick=${toggleAllCars}>${(boot.cars || []).every((a) => form.cars.includes(a.name)) ? '清空' : '全选'}</button>
      </div>
      ${(boot.cars || []).some((a) => !a.auto) ? null : html`<p class="muted" style="margin:4px 0">（车定义文件里没有车 —— ssh 模式或文件缺失时只显示 pkg/ 自动识别的车）</p>`}
      <div class="grid-checks">
        ${(boot.cars || []).filter((a) => !a.auto).map((a) => html`
          <label key=${a.name} class="check-item">
            <input type="checkbox" checked=${form.cars.includes(a.name)}
              onChange=${(e) => setForm((f) => Object.assign({}, f, { cars: toggled(f.cars, a.name, e.target.checked) }))} />
            <span>${a.name} <small>(${a.engine}${a.build ? '@' + a.build : ''}${a.jdk_dir ? ' + ' + a.jdk_dir.split('/').pop() : ''})</small></span>
          </label>`)}
      </div>
      ${(boot.cars || []).some((a) => a.auto) && html`
        <div class="card-title" style="margin-top:14px">pkg/ 自动识别
          <small class="muted">（未写入车定义文件 · 包名即车名，可直接勾选起跑）</small></div>
        <div class="grid-checks">
          ${(boot.cars || []).filter((a) => a.auto).map((a) => html`
            <label key=${a.name} class="check-item"
              title=${'由 pkg/ 包自动推导（' + a.engine + (a.build ? ' · build ' + a.build : '') + '）；车名确定性生成，跨 campaign 身份稳定'}>
              <input type="checkbox" checked=${form.cars.includes(a.name)}
                onChange=${(e) => setForm((f) => Object.assign({}, f, { cars: toggled(f.cars, a.name, e.target.checked) }))} />
              <span>${a.name} <small>(${a.engine}${a.build ? '@' + a.build : ''} · 自动)</small></span>
            </label>`)}
        </div>
        <p class="card-desc" style="margin:4px 0 0">新包放进 pkg/ 即出现在这里，每次进页自动重读 —— 不用手写车定义。
          要配 JDK（JDK_DIR/JDK_WANT）或自定义标签时，用「引擎与JDK」页的「＋ 车定义」把它固定进车定义文件。</p>`}

      <div class="card-title" style="margin-top:14px">赛道（全部可起跑的赛道，按族分组 · 点组名整组勾选/清空）</div>
      ${(boot.track_groups || []).map((g) => {
        const bases = baseTracks.filter((t) => t.group === g[0]);
        if (!bases.length) return null;
        const groupNames = tracks.filter((t) => t.group === g[0]).map((t) => t.name);
        const selN = groupNames.filter((n) => form.tracks.includes(n)).length;
        return html`
          <div key=${g[0]}>
            <div class="track-group">
              <button class="track-group-btn" onClick=${() => toggleGroup(g[0])}
                title="整组勾选/清空">${g[1]}</button>
              <span class="muted"> ${selN}/${groupNames.length}</span>
            </div>
            ${bases.map((b) => {
              const vs = tracks.filter((t) => t.base === b.name && t.variant && !t.switches);
              const arms = tracks.filter((t) => t.base === b.name && t.switches);
              return html`<div key=${b.name} class="track-block">
                <label class="check-item" title=${b.name + (b.dataset ? ' · ' + b.dataset + ' 语料' : '') + ' · 基座（不带压缩变体）· ~' + b.est + 'min'}>
                  <input type="checkbox" checked=${form.tracks.includes(b.name)}
                    onChange=${(e) => setForm((f) => Object.assign({}, f, { tracks: toggled(f.tracks, b.name, e.target.checked) }))} />
                  <b>${b.label}</b> <small>(${b.name}, ~${b.est}min)${b.dataset ? ' · ' + b.dataset + ' 语料' : ''}</small>
                </label>
                ${arms.length ? html`
                  <div class="row" style="margin:4px 0 0 16px;flex-wrap:wrap;gap:6px">
                    <span class="muted" style="font-size:12px">功能开关：</span>
                    ${arms.map((t) => html`<label key=${t.name} class="check-item"
                      title=${t.name + ' · challenge: ' + t.challenge + ' · ~' + t.est + 'min'
                        + (t.engines ? ' · 只支持 ' + t.engines : '')}>
                      <input type="checkbox" checked=${form.tracks.includes(t.name)}
                        onChange=${(e) => setForm((f) => Object.assign({}, f, { tracks: toggled(f.tracks, t.name, e.target.checked) }))} />
                      <span>${t.label} <small class="mono">${t.name}</small>
                        <small class="badge badge-run">${t.challenge === 'full' ? 'challenge full' : '⇒ ' + t.challenge}</small>
                        <small>(~${t.est}min)</small></span>
                    </label>`)}
                  </div>` : null}
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
        <label class="muted">DS_HOURS <input type="number" style="width:5em" min="0" value=${form.ds_hours}
          title="0=不启用窗底守卫" onInput=${(e) => setForm((f) => Object.assign({}, f, { ds_hours: e.target.value }))} /></label>
        <label class="muted">tag <input type="text" style="width:20ch" placeholder="留空自动生成" value=${form.tag}
          onInput=${(e) => setForm((f) => Object.assign({}, f, { tag: e.target.value }))} /></label>
        <label class="check-item smoke-toggle" title="esrally --test-mode：迷你语料 + 完整任务结构，单场不到 1 分钟。
        用于改配置后快速验证链路；数字与正式跑完全不可比，判定口径不变（VALID 才算数）">
          <input type="checkbox" checked=${!!form.test_mode}
            onChange=${(e) => setForm((f) => Object.assign({}, f, { test_mode: e.target.checked }))} />
          <span>⚡ 冒烟 test-mode <small>（&lt;1 分钟/场，数字不可比）</small></span>
        </label>
      </div>
    </div>

    <div class="card">
      <div class="card-title">每条赛道的耗时 est（分钟）
        <div class="spacer"></div>
        <${EstSuggestBtn} form=${form} setForm=${setForm} allTracks=${allTracks} />
      </div>
      <p class="card-desc">est 只用于排程预估与 DS_HOURS 窗口判定；留空用赛道清单的实测值。逐条改比手写 <code>ds-g1920=52,nested=40</code> 方便。</p>
      <${EstEditor} boot=${boot} form=${form} setForm=${setForm} allTracks=${allTracks} />
    </div>

    <div class="card">
      <details class="fold">
        <summary>已入库历史（指标库 benchmark-result-race）
          ${hist === undefined ? ' · 查询中…'
            : hist === null ? ' · 指标库不可用'
            : hist ? html` · ${hist.summary.races} 场次 / 本次矩阵将剔除 <b>${skip.length}</b> 格` : null}</summary>
        <div class="row"><button class="btn btn-ghost btn-sm" onClick=${loadHist}>刷新历史</button></div>
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
      </details>
    </div>

    <div class="card">
      <details class="fold" ...${bad.length ? { open: 'open' } : {}}>
        <summary>车 × 变体 兼容性
          ${bad.length ? html`<span class="st-bad">✘ ${bad.length} 个组合不兼容（起跑会被拦下）</span>`
            : html`<span class="st-ok">✔ 当前矩阵全部兼容</span>`}</summary>
      ${bad.length ? html`
        <div style="font-size:12px;margin:4px 0 0 8px">
          ${bad.map((b, i) => html`<div key=${i} class="st-bad">${b.car}（${b.eng}）× ${b.t} —— 只支持 ${b.allow}</div>`)}
        </div>` : null}
      <p class="card-desc">ZSTD 压缩与 <code>index.source_reuse</code> 是 Easysearch 专有
        （ES 的 <code>index.codec</code> 只有 default/best_compression，也不认 source_reuse）——
        编排台与 runner 都会在起跑前拦下不兼容的组合。</p>
      </details>
    </div>
      </div>

      <div class="compose-right">
        <div class="card preview-card">
          <div class="card-title">实时排程预览 <div class="spacer"></div>
            <button class="btn btn-ghost btn-sm" onClick=${copySched}
              title="复制排程文本（粘贴给 run-bench.sh / 存档用）">复制</button>
          </div>
          <div class="live-count">
            <b>${form.cars.length}</b> 车 × <b>${allTracks.length}</b> 赛道 × <b>${form.rounds}</b> 轮
            ${live.rows ? html` = <b>${live.rows}</b> 场 · 预计 <b>${fmtMin(live.estTotal)}</b>` : null}
            ${+form.ds_hours > 0 && live.estTotal > form.ds_hours * 60
              ? html` <span class="live-warn">⚠ 超 DS_HOURS=${form.ds_hours}h 窗口</span>` : null}
          </div>
          <div class="row" style="margin-top:4px">
            ${live.busy && html`<span class="live-chip">⟳ 排程计算中…</span>`}
            ${live.err && html`<span class="live-chip err">✘ ${live.err.split('\n')[0]}</span>`}
            ${!live.busy && !live.err && live.rows ? html`<span class="live-chip ok">✓ 排程已就绪（tag ${live.tag}）</span>` : null}
            ${!form.cars.length || !allTracks.length ? html`<span class="live-chip">至少选一台车和一条赛道</span>` : null}
          </div>
          <div class="row" style="margin-top:8px">
            <button class="btn btn-secondary" onClick=${doPreview} disabled=${!previewRef.current}>上传 + dry-run 预览</button>
            <button class="btn btn-primary" onClick=${doStart} disabled=${!previewRef.current}>互斥预检并起跑</button>
            <button class="btn btn-ghost" onClick=${async () => {
              const { status, j } = await getJSON('/api/mutex');
              if (status !== 200) { toast.err(j.error || '查询失败'); return; }
              setMutex(j);
              toast[j.free ? 'ok' : 'err'](j.free ? '集群空闲，随时可起跑' : '集群被占用: ' + (j.why || ''));
            }}>查集群占用</button>
          </div>
          ${mutex && !mutex.free ? html`<div class="warn-box" style="margin-top:6px">⚠ 集群被占用 —— ${mutex.why || 'runner 在跑'}。进度页可看是哪场；等它结束或停止后再起跑。</div>`
            : mutex && mutex.free ? html`<p class="st-ok" style="margin:4px 0 0">✓ 集群空闲（.runner.lock 未被占用）</p>` : null}
          ${cluster && html`<div class="row cluster-line" style="margin-top:6px">
            <span class="muted">集群</span>
            <span class=${'mono ' + (cluster.up ? 'st-ok' : 'st-warn')}>${cluster.engine.join(' / ') || '?'}</span>
            ${cluster.nodes.map((n) => html`<span key=${n.name} class="node-dot" title=${n.name + ' ' + n.ip + ' · 已装 ' + n.ver}>
              ${n.name.split('-').pop()} ${n.port9200 ? '✓' : '✗'}</span>`)}
            <span class="muted">${cluster.up ? '全部 9200 在听' : '有节点 9200 未听 —— 未起集群时起跑会自动部署'}</span>
            <button class="btn btn-ghost btn-sm" onClick=${(e) => {
              const self = e.target;
              getJSON('/api/cluster-status').then(({ j }) => { if (j && j.available) setCluster(j); });
            }}>↻</button>
          </div>
          <p class="card-desc" style="margin:2px 0 0">只读探活（ip.txt 已装版本 + 9200 端口）；green/卡点/冷启动以 runner 流水线为准。</p>`}
          <p class="card-desc">排程随左侧勾选实时刷新（0.35s 防抖，纯本地计算不碰集群）；起跑前仍会做互斥预检与 runner dry-run。</p>
          <div class="mini-h">排程</div>
          <pre class="out light sched-pre">${schedBox}</pre>
          <div class="mini-h">输出（dry-run / 起跑 / 环境体检）</div>
          <pre class="out sched-pre">${outBox}</pre>
        </div>
      </div>
    </div>
    ${carsEditor && html`<${CarsEditorDrawer} onClose=${() => setCarsEditor(false)}
      onSaved=${() => reloadBoot && reloadBoot(true)} />`}
  `;
}

// est 逐赛道编辑表格：已选赛道一行一格，改完自动回到 form.est 字符串（后端契约不变）
function EstEditor({ boot, form, setForm, allTracks }) {
  const estMap = parseEst(form.est);
  function setEst(track, v) {
    setForm((f) => {
      const m = parseEst(f.est);
      if (v === '' || v == null || !/^\d+$/.test(String(v))) delete m[track];
      else m[track] = +v;
      return Object.assign({}, f, { est: estToStr(m) });
    });
  }
  const rows = allTracks.slice().sort();
  return html`<table class="tbl est-tbl">
    <thead><tr><th>赛道</th><th class="num">清单实测</th><th class="num">覆盖 est</th><th>按历史校准</th></tr></thead>
    <tbody>
      ${rows.map((t) => {
        const info = (boot.tracks || []).find((x) => x.name === t);
        const v = estMap[t];
        return html`<tr key=${t}>
          <td class="mono">${t}${info && info.dataset ? html` <small class="muted">${info.dataset}</small>` : null}</td>
          <td class="num muted">${info ? '~' + info.est + 'm' : '—'}</td>
          <td class="num"><input type="number" style="width:6.5em" min="1" placeholder="（用实测）"
            value=${v == null ? '' : v} onInput=${(e) => setEst(t, e.target.value)} /></td>
          <td><${EstCalBtn} track=${t} onSet=${(m) => setEst(t, m)} /></td>
        </tr>`;
      })}
      ${!rows.length && html`<tr><td colspan="4" class="muted">（还没选赛道 —— 上方勾选后这里逐条列出）</td></tr>`}
    </tbody>
  </table>`;
}

function EstCalBtn({ track, onSet }) {
  const [busy, setBusy] = useState(false);
  async function go() {
    setBusy(true);
    try {
      const { j } = await getJSON('/api/est-suggest?track=' + encodeURIComponent(track));
      if (j.available && j.suggest) { onSet(j.suggest); toast.ok(track + ' → ' + j.suggest + ' 分钟（历史中位）'); }
      else toast.err(track + '：历史里没有可用耗时');
    } catch (e) { toast.err(String(e)); }
    setBusy(false);
  }
  return html`<button class="btn btn-ghost btn-sm" onClick=${go} disabled=${busy}>${busy ? '…' : '校准'}</button>`;
}

// 一键把所有已选赛道按历史中位回填（保留手写过的）
function EstSuggestBtn({ form, setForm, allTracks }) {
  const [busy, setBusy] = useState(false);
  async function calibrate() {
    if (!allTracks.length) { toast.err('先选赛道'); return; }
    setBusy(true);
    const out = {};
    await Promise.all(allTracks.map(async (t) => {
      try {
        const { j } = await getJSON('/api/est-suggest?track=' + encodeURIComponent(t));
        if (j.available && j.suggest) out[t] = j.suggest;
      } catch (e) { /* 单条失败忽略 */ }
    }));
    setBusy(false);
    if (!Object.keys(out).length) { toast.err('历史里没有这些赛道的可用耗时'); return; }
    setForm((f) => {
      const m = Object.assign(parseEst(f.est), out);
      return Object.assign({}, f, { est: estToStr(m) });
    });
    toast.ok('已按历史中位耗时回填 ' + Object.keys(out).length + ' 条 est（可再手改）');
  }
  return html`<button class="btn btn-secondary btn-sm" onClick=${calibrate} disabled=${busy}>
    ${busy ? '查询历史中…' : '全部校准'}</button>`;
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
// 场次表的状态筛选：场次一多（codec 矩阵 18+ 场）想找失败/待跑的行全靠肉眼。
// 分组口径与状态卡一致：DEGRADED 算有效，任意 *-FAIL 算失败，SKIP/DEADLINE-SKIP 算跳过。
const STAT_F = [
  ['ALL', '全部'], ['RUN', '运行中'], ['VALID', '有效'], ['FAIL', '失败'], ['SKIP', '跳过'], ['PEND', '待跑'],
];
function statMatch(code, st) {
  switch (code) {
    case 'RUN': return st === 'RUNNING';
    case 'VALID': return st === 'VALID' || st === 'DEGRADED';
    case 'FAIL': return /FAIL/.test(st);
    case 'SKIP': return st === 'SKIP' || st === 'DEADLINE-SKIP';
    case 'PEND': return st === 'PENDING';
    default: return true;
  }
}

function ProgressView({ boot, camps, curCid, setCurCid, reload, EX }) {
  const [queue, setQueue] = useState(null);
  const [tail, setTail] = useState('（空）');
  const [statFilter, setStatFilter] = useState('ALL');
  // 轮询间隔（秒）：0=手动。压测期间可调大/关掉，减少压测机上的 ssh 轮询噪声；
  // 存 localStorage 记住选择，不用每次压测重调。
  const [pollSec, setPollSec] = useState(() => {
    try { const v = localStorage.getItem('raceway.pollSec'); return v == null ? 5 : Number(v); }
    catch (e) { return 5; }
  });
  const [notify, setNotify] = useState(false);
  const [lastPoll, setLastPoll] = useState(null);
  const [paused, setPaused] = useState(false); // 标签页切后台时自动暂停轮询
  const [pollTick, setPollTick] = useState(0);  // 点「立即刷新」时 +1，重跑 effect 拉一次并重排
  const [mon, setMon] = useState(null);         // 进行中场次的每节点 5s 监控
  const [monOn, setMonOn] = useState(() => localStorage.getItem('raceway.monOn') !== '0');
  const [drawerFid, setDrawerFid] = useState(null);
  const [followLog, setFollowLog] = useState(true);
  const [res, setRes] = useState(null);         // 场内速览（逐场次判定 + 关键指标）
  const [stopClose, setStopClose] = useState(null); // stale 一键停止+收尾的状态与输出
  const prevEnded = useRef(null);
  const prevStaleRef = useRef(null);
  const timer = useRef(null);
  const logBox = useRef(null);
  const visibleRef = useRef(true);

  useEffect(() => {
    if (notify && typeof Notification !== 'undefined' && Notification.permission !== 'granted')
      Notification.requestPermission();
  }, [notify]);

  // 标签页切后台自动暂停轮询（回前台立即补一轮）—— 别让挂着的页面空烧压测机
  useEffect(() => {
    const onVis = () => {
      visibleRef.current = !document.hidden;
      if (!document.hidden) setPollTick((n) => n + 1);
    };
    document.addEventListener('visibilitychange', onVis);
    return () => document.removeEventListener('visibilitychange', onVis);
  }, []);
  useEffect(() => { setPaused(document.hidden); }, [pollTick]);

  useEffect(() => {
    let alive = true;
    async function poll() {
      clearTimeout(timer.current);
      if (!curCid || !alive) return;
      if (document.hidden) { setPaused(true); scheduleNext(); return; }
      setPaused(false);
      const enc = encodeURIComponent(curCid);
      try {
        const [rq, rt] = await Promise.all([
          getJSON('/api/queue?cid=' + enc),
          getJSON('/api/logtail?cid=' + enc + '&n=40'),
        ]);
        if (rq.status === 200) {
          setQueue(rq.j);
          // 进行中场次的实时监控：跟轮询同频，关掉或没在跑就清掉
          const run2 = (rq.j.fields || []).find((f) => f.status === 'RUNNING');
          if (run2 && monOn) {
            const rm = await getJSON('/api/monitor?cid=' + enc + '&fid=' + encodeURIComponent(run2.fid));
            if (alive) setMon(rm.status === 200 ? rm.j : null);
          } else setMon(null);
        }
        if (rt.status === 200) setTail(rt.j.lines || '（空）');
        // 场内速览随轮询刷新（纯本地文件聚合，便宜）—— 盯盘时 RUNNING 行的吞吐才会动
        try {
          const rr = await getJSON('/api/campaign-result?cid=' + enc);
          if (alive && rr.status === 200) setRes(rr.j);
        } catch (e2) { /* 下一轮再试 */ }
        setLastPoll(new Date());
      } catch (e) { /* 网络抖动下一轮再试 */ }
      scheduleNext();
    }
    function scheduleNext() {
      if (alive && pollSec > 0) timer.current = setTimeout(poll, pollSec * 1000);
    }
    poll();  // 切到本页/换间隔/换 campaign 时立刻拉一次，之后按间隔排程
    return () => { alive = false; clearTimeout(timer.current); };
  }, [curCid, pollSec, pollTick, monOn]);

  // 日志自动跟随：新内容到了就滚到底（关掉跟随则不动）
  useEffect(() => {
    if (followLog && logBox.current) logBox.current.scrollTop = logBox.current.scrollHeight;
  }, [tail, followLog]);

  // 场内速览：campaign 切换 / 立即刷新时拉一次（纯本地文件聚合，便宜）
  useEffect(() => {
    if (!curCid) { setRes(null); return; }
    let alive = true;
    getJSON('/api/campaign-result?cid=' + encodeURIComponent(curCid))
      .then(({ j }) => { if (alive) setRes(j); })
      .catch(() => { /* 下轮再试 */ });
    return () => { alive = false; };
  }, [curCid, pollTick]);

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
    if (curCamp && curCamp.ended) { toast.info('该 campaign 已停止/结束，无需停止'); return; }
    if (!confirm('确认停止 ' + curCid + '？\n将杀掉远端 runner 与 esrally 进程；campaign 目录保留，可断点续跑。')) return;
    toast.info('停止中…');
    const { status, j } = await postJSON('/api/stop', { cid: curCid });
    if (status !== 200) { setTail('停止失败: ' + (j.error || '未知')); toast.err('停止失败'); return; }
    setTail(j.result || '已停止'); toast.ok('已发送停止命令');
    reload(curCid);
  }

  function endState(c) { return !c.ended ? '运行中' : (/中止/.test(c.last || '') ? '已中止' : '已结束'); }
  // 已停止（已结束/已中止）的 campaign 不再提供停止入口 —— 没有进程可杀，点了也只会白报错
  const curCamp = camps.find((c) => c.cid === curCid);
  const stopDisabled = !curCid
    || (curCamp ? curCamp.ended : !!(queue && queue.campaign === curCid && queue.ended));
  // 没收尾但进度文件长期无更新 → runner 大概率已被杀（页面标出来 + 一键处理）
  const warns = (queue && queue.warnings || []).map((w) => ({ text: w, stale: false }));
  const camp = camps.find((x) => x.cid === (queue && queue.campaign));
  let staleCamp = false;
  if (camp && !camp.ended && camp.mtime) {
    const staleMin = (Date.now() / 1000 - camp.mtime) / 60;
    if (staleMin > 30) {
      staleCamp = true;
      warns.unshift({ text: '进度文件已 ' + Math.round(staleMin) + ' 分钟无更新 —— runner 可能已停止'
        + '（确认没人跑后可在此收尾补结束行，或 CAMP_ID=' + (queue && queue.campaign) + ' 断点续跑）', stale: true });
    }
  }
  // stale 状态变化 → 系统通知（复用完成提醒的授权开关），别让人干等一个卡死的场
  if (staleCamp && notify && prevStaleRef.current !== true
    && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
    try { new Notification('raceway: campaign 疑似卡死', { body: (queue && queue.campaign || '') + ' 进度文件长时间无更新，请到运行进度页处理' }); } catch (e) { /* 忽略 */ }
  }
  if (queue) prevStaleRef.current = staleCamp;
  function gotoClose() {
    const el = document.getElementById('close-fold');
    if (el) { el.open = true; el.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
  }
  // 一键「停止并收尾」：stop（已死则安全跳过）→ close 补结束行（非 dry-run）
  async function stopAndClose() {
    if (!curCid) return;
    if (!confirm('停止并收尾 ' + curCid + '？\n1) 停止远端 runner 与 esrally 进程（已死则安全跳过）\n2) 直接补结束行（非 dry-run，会写入 RACES/PROGRESS）')) return;
    setStopClose({ busy: true, text: '【停止】发送停止命令…' });
    const s = await postJSON('/api/stop', { cid: curCid });
    let text = '【停止】' + (s.status === 200 ? (s.j.result || '已发送停止命令') : ('失败: ' + (s.j.error || '未知')));
    setStopClose({ busy: true, text: text + '\n【收尾】补结束行…' });
    const c = await postJSON('/api/close', { cid: curCid, dry_run: false });
    text += '\n【收尾】' + (c.status === 200 ? (c.j.result || '完成') : ('失败: ' + (c.j.error || '未知')));
    setStopClose({ busy: false, text });
    reload(curCid);
  }

  const done = s.total - s.pending;
  const eta = (s.pending > 0 && s.est_remaining_min > 0)
    ? new Date(Date.now() + s.est_remaining_min * 60000).toLocaleTimeString() : '-';
  const cards = [['总场次', s.total], ['有效', s.valid], ['失败', s.failed], ['跳过', s.skipped],
    ['待跑', s.pending], ['剩余约(分)', s.est_remaining_min != null ? Math.round(s.est_remaining_min) : '-'], ['预计完成', eta]];
  // 快切条：最近 20 场；当前选中的若不在其中（比如老的遗留 campaign），插到最前保证可见
  const chipCamps = camps.slice(0, 20);
  if (curCid && curCamp && !chipCamps.some((c) => c.cid === curCid)) {
    chipCamps.unshift(curCamp);
  }
  // 警告降噪：最多直接展示 2 条（矛盾判定/STATE 冲突一多就把整张表推到视野外），
  // 其余收进折叠
  const warnHead = warns.slice(0, 2);
  const warnTail = warns.slice(2);

  // 场次表状态筛选（切 campaign 不清 —— 看下一场时通常还在找同类行）
  const allFields = (queue && queue.fields) || [];
  const statCounts = {};
  STAT_F.forEach(([code]) => { statCounts[code] = allFields.filter((f) => statMatch(code, f.status)).length; });
  const fields = statFilter === 'ALL' ? allFields : allFields.filter((f) => statMatch(statFilter, f.status));

  // PROGRESS 行着色：一眼分清 OK / 失败 / 警告 / 相位推进
  function lineCls(l) {
    if (/✗|FAIL|CONTAMINATED|ERROR|失败|中止/.test(l)) return 'lg-bad';
    if (/✔|✓|VALID\b/.test(l)) return 'lg-ok';
    if (/WARN|⚠|DEGRAD|SKIP|降级/.test(l)) return 'lg-warn';
    if (/▶|\[[0-9]+\/[0-9]+\]|相位|阶段|precheck|部署|bulk|search/.test(l)) return 'lg-run';
    return '';
  }

  return html`
    <div class="card">
      <div class="row">
        <select style="min-width:320px" value=${curCid || ''}
          onChange=${(e) => setCurCid(e.target.value || null)}>
          ${camps.map((c) => html`
            <option key=${c.cid} value=${c.cid}>${c.cid}（${endState(c)} ${c.valid}/${c.total}）</option>`)}
        </select>
        <button class="btn btn-ghost btn-sm" onClick=${() => reload()}>刷新列表</button>
        <button class="btn btn-ghost btn-sm" disabled=${!curCid} onClick=${() => setPollTick((n) => n + 1)}>立即刷新</button>
        <label class="muted">自动刷新
          <select value=${pollSec} onChange=${(e) => {
            const v = Number(e.target.value); setPollSec(v);
            try { localStorage.setItem('raceway.pollSec', String(v)); } catch (err) { /* 忽略 */ }
          }} title="压测期间可调大间隔或切手动，减少压测机上的 ssh 轮询">
            <option value="5">每 5 秒</option>
            <option value="15">每 15 秒</option>
            <option value="30">每 30 秒</option>
            <option value="60">每 60 秒</option>
            <option value="0">手动（不自动轮询）</option>
          </select>
        </label>
        ${paused && pollSec > 0 && html`<span class="live-chip">⏸ 后台已暂停，回前台继续</span>`}
        ${lastPoll && html`<span class="muted">上次拉取 ${lastPoll.toLocaleTimeString()}</span>`}
        <label class="chk"><input type="checkbox" checked=${notify} onChange=${(e) => setNotify(e.target.checked)} /> 完成提醒</label>
        <div class="spacer"></div>
        <button class="btn btn-danger btn-sm" disabled=${stopDisabled}
          title=${stopDisabled ? '该 campaign 已停止/结束，无需停止' : '杀掉远端 runner 与 esrally 进程（目录保留，可断点续跑）'}
          onClick=${doStop}>■ 停止该 campaign</button>
      </div>

      <div class="camp-chips" title="最近 campaign 快速切换">
        ${chipCamps.map((c) => html`
          <button key=${c.cid} class=${'camp-chip' + (c.cid === curCid ? ' on' : '') + (!c.ended ? ' running' : '')}
            onClick=${() => setCurCid(c.cid)}>
            ${c.cid} <small>${c.valid}/${c.total}</small>
            ${!c.ended ? html`<i class="dot"></i>` : null}
          </button>`)}
        ${camps.length > chipCamps.length && html`<span class="muted">…共 ${camps.length} 场（下拉框可选全部）</span>`}
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
      ${warnHead.map((w, i) => html`<div key=${i} class="warn-box">⚠ ${w.text}
        ${w.stale && html`<span style="margin-left:8px;white-space:nowrap">
          <button class="btn btn-ghost btn-sm" onClick=${gotoClose}
            title="展开下方收尾卡，先预演再补结束行">去收尾</button>
          <button class="btn btn-danger btn-sm" disabled=${stopClose && stopClose.busy}
            onClick=${stopAndClose} title="停止远端 runner（已死则安全跳过）并直接补结束行">停止并收尾…</button>
        </span>`}</div>`)}
      ${stopClose && html`<pre class="out" style="max-height:200px">${stopClose.text}</pre>`}
      ${warnTail.length ? html`<details class="fold warn-more">
        <summary>⚠ 还有 ${warnTail.length} 条警告（矛盾判定 / STATE 冲突 / 窗底等）</summary>
        ${warnTail.map((w, i) => html`<div key=${i} class="warn-box">⚠ ${w.text}</div>`)}
      </details>` : null}
      ${(queue && (queue.fields || []).length) ? html`
        <div class="camp-chips" style="margin-top:10px" title="按状态筛选场次表（口径与上方状态卡一致）">
          <span class="muted" style="font-size:12px;align-self:center">筛选</span>
          ${STAT_F.map(([code, label]) => html`
            <button key=${code} class=${'camp-chip' + (statFilter === code ? ' on' : '')}
              onClick=${() => setStatFilter(code)}>
              ${label} <small>${statCounts[code]}</small>
            </button>`)}
        </div>` : null}
      <table class="tbl">
        <thead><tr><th>seq</th><th>场次</th><th>状态</th><th>用时</th><th>recall</th><th>说明</th><th></th></tr></thead>
        <tbody>
          ${fields.map((f) => {
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
            const openable = f.fid && f.status !== 'PENDING';
            return html`<tr key=${f.seq} class=${f.status === 'RUNNING' ? 'now clickable' : 'clickable'}
              title=${openable ? '点击看该场详情（身份 / 指标 / 监控 / 日志）' : ''}
              onClick=${openable ? () => setDrawerFid(f.fid) : null}>
              <td>${f.seq}</td><td class="mono">${f.fid}</td><td class=${cls}>${f.status}</td>
              <td>${f.duration || '-'}</td><td>${f.recall || '-'}</td><td>${notes.join('  ')}</td>
              <td style="white-space:nowrap" onClick=${(e) => e.stopPropagation()}>
                ${openable ? html`<button class="btn btn-ghost btn-sm"
                  onClick=${() => setDrawerFid(f.fid)}>详情</button>` : null}
              </td></tr>`;
          })}
          ${(!allFields.length) && html`
            <tr><td colspan="7" class="muted">（等待选择 campaign）</td></tr>`}
          ${(allFields.length > 0 && fields.length === 0) && html`
            <tr><td colspan="7" class="muted">（没有「${STAT_F.find((x) => x[0] === statFilter)[1]}」状态的场次）</td></tr>`}
        </tbody>
      </table>
    </div>

    ${res && res.rows && res.rows.some((r) => r.metrics.length) && html`
      <div class="card">
        <div class="card-title">本场结果速览（逐任务指标来自 race.json；点行开详情）
          <div class="spacer"></div>
          <span class="muted">${res.rows.filter((r) => r.metrics.length).length}/${res.rows.length} 场有指标</span>
          <button class="btn btn-ghost btn-sm" onClick=${() => setPollTick((n) => n + 1)}>刷新</button></div>
        <table class="tbl">
          <thead><tr><th>场次</th><th>赛道/轮</th><th>判定</th><th class="num">写入吞吐</th>
            <th>最弱查询任务</th><th class="num">最弱 ops/s</th><th class="num">最弱 p99</th></tr></thead>
          <tbody>
            ${res.rows.map((r) => {
              const write = r.metrics.find((m) => /bulk|index/i.test(m.task) && m.throughput);
              let weak = null;
              for (const m of r.metrics) {
                if (!m.throughput || /bulk|index|refresh|merge|force/i.test(m.task)) continue;
                if (!weak || m.throughput < weak.throughput) weak = m;
              }
              const vCls = r.verdict === 'VALID' ? 'badge-ok'
                : /FAIL/.test(r.verdict) ? 'badge-fail' : /SKIP/.test(r.verdict) ? 'badge-skip' : 'badge-none';
              return html`<tr key=${r.fid} class="clickable" title="点击开详情（身份 / 指标 / 监控 / 日志）"
                onClick=${() => setDrawerFid(r.fid)}>
                <td class="mono">${r.fid}</td>
                <td>${r.track}${r.round ? html` · r${r.round}` : null}</td>
                <td><span class=${'badge ' + vCls}>${r.verdict}</span></td>
                <td class="num">${write ? html`${fmtN(write.throughput)} <small>${write.tput_unit || ''}</small>` : '—'}</td>
                <td class="mono" style="font-size:12px">${weak ? weak.task : '—'}</td>
                <td class="num">${weak ? fmtN(weak.throughput) : '—'}</td>
                <td class="num">${weak && weak.lat_p99 != null
                  ? html`${fmtN(weak.lat_p99)} <small>${weak.lat_unit || ''}</small>` : '—'}</td>
              </tr>`;
            })}
          </tbody>
        </table>
        <p class="card-desc">「最弱查询」= 该场次全部查询任务里吞吐中位最低的一个（整体被最弱任务卡住，跨场次看它最有对比意义）；完整逐任务数字点行进详情。</p>
      </div>`}

    ${run && monOn && html`
      <div class="card">
        <div class="card-title">当前场次实时监控（每节点 ${mon ? mon.interval : 5}s 采样 · 跟轮询同频刷新）
          <div class="spacer"></div>
          ${mon && mon.series && html`<span class="muted">${mon.series.map((x) => x.node).join(' / ')}</span>`}
          <button class="btn btn-ghost btn-sm" onClick=${() => {
            setMonOn(false);
            try { localStorage.setItem('raceway.monOn', '0'); } catch (e) { /* 忽略 */ }
          }}>收起监控</button>
        </div>
        ${mon && mon.series && mon.series.some((x) => (x.rows || []).some((r) => typeof r.cpu_usr === 'number'))
          ? html`<div class="mon-grid">
              ${['cpu_usr', 'cpu_steal', 'load1', 'bench_cpu', 'foreign_cpu'].map((f) =>
                mon.series.some((x) => (x.header || []).includes(f))
                  ? html`<div key=${f} class="mon-cell">
                      <div class="mon-label">${f}${f === 'cpu_steal' ? '（被宿主抢走 = 数据可信度红线）' : ''}</div>
                      ${EX && EX.MonitorChart && html`<${EX.MonitorChart} series=${mon.series} field=${f} />`}
                    </div>` : null)}
            </div>`
          : html`<p class="muted">监控采样还没有数据（部署/冷启动阶段是空的，进压测相位后出现）。</p>`}
      </div>`}
    ${!monOn && html`<div class="row" style="margin-bottom:10px">
      <button class="btn btn-ghost btn-sm" onClick=${() => {
        setMonOn(true);
        try { localStorage.setItem('raceway.monOn', '1'); } catch (e) { /* 忽略 */ }
      }}>展开当前场次实时监控</button>
    </div>`}

    <div class="card">
      <div class="card-title">PROGRESS.txt 尾部（实时日志）
        <div class="spacer"></div>
        <label class="chk"><input type="checkbox" checked=${followLog}
          onChange=${(e) => setFollowLog(e.target.checked)} /> 自动滚到底</label>
      </div>
      <pre class="out" ref=${logBox}>${String(tail).split('\n').map((l, i) =>
        html`<div key=${i} class=${lineCls(l)}>${l}</div>`)}</pre>
    </div>

    ${EX && drawerFid && html`<${EX.RaceDetailDrawer} cid=${curCid} fid=${drawerFid}
      onClose=${() => setDrawerFid(null)} />`}

    <div class="card">
      <details class="fold">
        <summary>日志全文 / 任意 campaign 文件（PROGRESS / RACES / STATE / monitors / 自定义路径 + 关键字过滤）</summary>
        ${EX && html`<${EX.LogViewer} cid=${curCid} />`}
      </details>
    </div>
    ${curCid && html`<div class="card">
      <details class="fold" id="close-fold">
        <summary>campaign 收尾（给没结束行的 campaign 补结束行，默认 dry-run 不改动）</summary>
        ${EX && html`<${EX.CloseCard} cid=${curCid} />`}
      </details>
    </div>`}
  `;
}

// ==================== ③ 外部结果入口（console.conf 默认清单 + 浏览器个人覆盖） ====================
const EXT_LINKS_KEY = 'raceway.extlinks';
function defaultExtLinks(boot) {
  // 优先 console.conf 的 extlink.*（bootstrap 下发）；没有配置文件时用内置两条
  if (boot.ext_links && boot.ext_links.length) return boot.ext_links;
  return [
    { name: 'Kibana 看板', url: boot.kibana_url || '' },
    { name: 'LogPilot', url: '' },
  ];
}
function ExternalLinks({ boot }) {
  const [links, setLinks] = useState(null); // null = 用默认，一旦编辑即固化成个人清单
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({});
  useEffect(() => {
    try {
      const v = JSON.parse(localStorage.getItem(EXT_LINKS_KEY) || 'null');
      if (Array.isArray(v) && v.length) setLinks(v.filter((x) => x && x.name));
    } catch (e) { /* 忽略 */ }
  }, []);
  const list = links || defaultExtLinks(boot);
  function persist(l) {
    setLinks(l);
    try { localStorage.setItem(EXT_LINKS_KEY, JSON.stringify(l)); } catch (e) { /* 忽略 */ }
  }
  function upd(i, k, v) { persist(list.map((x, idx) => idx === i ? Object.assign({}, x, { [k]: v }) : x)); }
  function del(i) { persist(list.filter((_, idx) => idx !== i)); }
  function add() {
    const name = (draft.name || '').trim(), url = (draft.url || '').trim();
    if (!name || !url) { toast.err('名称和 URL 都要填'); return; }
    persist(list.concat([{ name, url }]));
    setDraft({});
    toast.ok('已添加: ' + name);
  }
  function reset() {
    setLinks(null); setEditing(false);
    try { localStorage.removeItem(EXT_LINKS_KEY); } catch (e) { /* 忽略 */ }
    toast.ok('已恢复默认链接' + (boot.console_conf ? '（console.conf 清单）' : ''));
  }
  return html`<div class="card">
    <div class="card-title">外部结果入口
      <div class="spacer"></div>
      ${editing && html`<button class="btn btn-ghost btn-sm" onClick=${reset}>恢复默认</button>`}
      <button class="btn btn-ghost btn-sm" onClick=${() => setEditing(!editing)}>${editing ? '完成' : '配置'}</button>
    </div>
    ${!editing && html`
      <div class="row">
        ${list.map((l, i) => l.url ? html`
          <a key=${i} class="ext-link" href=${l.url} target="_blank" rel="noreferrer"
            title=${l.url}>${l.name} ↗</a>`
          : html`<span key=${i} class="ext-link ext-none" title="还没配 URL —— 点「配置」填写"
              onClick=${() => setEditing(true)}>${l.name}（未配置）</span>`)}
      </div>
      <p class="card-desc" style="margin-top:6px">默认清单来自 raceway 根的 <code>console.conf</code>（extlink.* 行；
        没有配置文件则用内置 Kibana/LogPilot）；此处的改动存浏览器本地、只影响这台机器。
        Kibana 若只听远端 127.0.0.1，先建隧道：ssh -L 5601:127.0.0.1:5601 ${boot.ssh || 'user@host'}</p>`}
    ${editing && html`
      <div>
        ${list.map((l, i) => html`<div class="row" key=${i}>
          <input class="input" style="width:16ch" value=${l.name} placeholder="名称"
            onInput=${(e) => upd(i, 'name', e.target.value)} />
          <input class="input x-grow" value=${l.url} placeholder="URL（http…）"
            onInput=${(e) => upd(i, 'url', e.target.value)} />
          <button class="btn btn-ghost btn-sm" onClick=${() => del(i)} title="删除">✕</button>
        </div>`)}
        <div class="row">
          <input class="input" style="width:16ch" placeholder="新链接名"
            value=${draft.name || ''} onInput=${(e) => setDraft(Object.assign({}, draft, { name: e.target.value }))} />
          <input class="input x-grow" placeholder="URL（http…）"
            value=${draft.url || ''} onInput=${(e) => setDraft(Object.assign({}, draft, { url: e.target.value }))} />
          <button class="btn btn-secondary btn-sm" onClick=${add}>＋ 添加</button>
        </div>
        <p class="card-desc">改完即存；URL 留空的条目在页面上显示为「未配置」。想给所有人换默认清单，
          改 console.conf 后刷新页面（个人清单可「恢复默认」丢弃）。</p>
      </div>`}
  </div>`;
}

// ==================== ③ 历史与结果 ====================
function HistoryView({ boot, camps, reload, onView, onReorder, EX, curCid }) {
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
    <${ExternalLinks} boot=${boot} />

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
      ${cmpOut && cmpOut.data && EX && html`<${AssertCsv} data=${cmpOut.data} EX=${EX} />`}
      ${cmpOut && html`
        <details class="fold"><summary>原始文本输出（复制进报告用）</summary>
          <pre class="out light" style="max-height:320px">${cmpOut.text}</pre></details>`}
    </div>

    ${EX && html`<${EX.ReportView} camps=${camps} curCid=${curCid} />`}

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

    <${TrendView} boot=${boot} />
  `;
}

// 对比结果 → 断言 CSV（B 侧中位做阈值，car-compare --assert / campaign-thresholds.conf 直接可用）
function assertsFromCompare(d) {
  const lines = ['# track,task,metric,cmp,value —— 由对比结果生成（对象 B 的中位）；按需修改阈值/删行'];
  const f4 = (v) => String(Math.round(v * 10000) / 10000);
  (d.tracks || []).forEach((t) => {
    (t.write || []).forEach((r) => {
      if (r.b_med == null) return;
      lines.push([t.track, r.task, r.metric,
        r.direction === 'higher-better' ? '>=' : '<=', f4(r.b_med)].join(','));
    });
    (t.query || []).forEach((r) => {
      if (r.b_tput != null)
        lines.push([t.track, r.task, 'Median Throughput', '>=', f4(r.b_tput)].join(','));
      if (r.b_p99 != null)
        lines.push([t.track, r.task, '99th percentile service time', '<=', f4(r.b_p99)].join(','));
    });
  });
  return lines.join('\n') + '\n';
}

function AssertCsv({ data, EX }) {
  const [text, setText] = useState('');
  function download() {
    try {
      const blob = new Blob([text || ''], { type: 'text/csv;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = 'campaign-thresholds.conf';
      document.body.appendChild(a); a.click();
      setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 100);
      toast.ok('已下载 campaign-thresholds.conf');
    } catch (e) { toast.err('下载失败: ' + e); }
  }
  return html`<details class="fold" onToggle=${(e) => {
    if (e.target.open && !text) setText(assertsFromCompare(data));
  }}>
    <summary>生成断言 CSV —— B 侧中位做阈值，可编辑后下载</summary>
    ${text ? html`<div>
      <textarea class="textarea mono" style="width:100%;height:180px;font-size:12px;margin-top:6px"
        value=${text} onInput=${(e) => setText(e.target.value)} spellcheck=${false}></textarea>
      <div class="row">
        <button class="btn btn-secondary btn-sm" onClick=${download}>下载 CSV</button>
        <button class="btn btn-ghost btn-sm" onClick=${() => {
          navigator.clipboard && navigator.clipboard.writeText(text)
            .then(() => toast.ok('已复制'), () => toast.err('复制失败'));
        }}>复制</button>
        <span class="muted">用法：car-compare --assert 此文件（或 campaign-thresholds.conf）→ 每条规则要求 B 侧 ≥3 轮有效测量，不达标非零退出。</span>
      </div>
    </div>` : html`<p class="muted">展开即按当前对比结果生成。</p>`}
  </details>`;
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
const TREND_KEY = 'raceway.trend';   // 上次查询条件：下次进来原样回填并自动查一次
function TrendView({ boot }) {
  const saved = (() => {
    try { return JSON.parse(localStorage.getItem(TREND_KEY) || 'null'); } catch (e) { return null; }
  })();
  const [car, setCar] = useState(saved && saved.car != null ? saved.car : '');
  const [track, setTrack] = useState(saved && saved.track ? saved.track
    : ((boot.tracks || [])[0] ? boot.tracks[0].name : ''));
  const [metric, setMetric] = useState(saved && saved.metric ? saved.metric : 'throughput');
  const [task, setTask] = useState(saved && saved.task != null ? saved.task : '');
  const [hint, setHint] = useState('');
  const [res, setRes] = useState(null);
  const chartRef = useRef(null);
  const chartInst = useRef(null);

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
      try { localStorage.setItem(TREND_KEY, JSON.stringify(
        { car: car.trim(), track, metric, task: task.trim() })); } catch (e) { /* 忽略 */ }
    } catch (e) { setHint('请求失败: ' + e); }
  }
  // 上次查过就自动来一次 —— 常看趋势的人打开即见图，不用每次重填
  useEffect(() => { if (saved && saved.car) doTrend(); }, []);
  // resize 单监听 + 卸载时释放 echarts 实例（此前每次渲染都挂一个新 listener 会泄漏）
  useEffect(() => {
    const onR = () => { if (chartInst.current) chartInst.current.resize(); };
    window.addEventListener('resize', onR);
    return () => {
      window.removeEventListener('resize', onR);
      if (chartInst.current) { chartInst.current.dispose(); chartInst.current = null; }
    };
  }, []);
  function renderChart(j) {
    loadEcharts().then((ok) => {
      if (!ok || !chartRef.current) return; // 表格兜底
      const chart = echarts.getInstanceByDom(chartRef.current) || echarts.init(chartRef.current);
      chartInst.current = chart;
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
      }, true);
    });
  }
  // 车代号候选：车定义里的 label 与车名（趋势的 series_key 就是 label 这一级）
  const carOptions = [...new Set((boot.cars || [])
    .flatMap((c) => [c.label, c.name]).filter(Boolean))];
  return html`
    <div class="card">
      <div class="card-title">指标趋势（跨 campaign，只读指标库 benchmark-result-metric）</div>
      <div class="row">
        <input type="text" style="width:10ch" placeholder="jdk25" value=${car} list="trend-cars"
          onInput=${(e) => setCar(e.target.value)} />
        <datalist id="trend-cars">
          ${carOptions.map((c) => html`<option key=${c} value=${c} />`)}
        </datalist>
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
      <p class="card-desc">每个点 = 一场 campaign 的一次有效测量；同车多 build 会分线（series_key 带 build）。
        查询条件会记住，下次打开自动重现。</p>
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

// ==================== ④ 赛道管理 ====================
function TracksView({ boot, EX, reloadBoot }) {
  const tracks = boot.tracks || [];
  return html`
    <div class="card">
      <div class="card-title">赛道目录（${tracks.length} 条 · 与 run-bench.sh list 同源）
        <div class="spacer"></div>
        <span class="muted">在「启动排程」页勾选即可编排</span>
        <button class="btn btn-ghost btn-sm" onClick=${() => reloadBoot && reloadBoot()}
          title="重读 tracks/ 定义（引擎支持等）—— 下方赛道文件保存后会自动刷新">↻ 刷新清单</button></div>
      ${(boot.track_groups || []).map((g) => {
        const bases = tracks.filter((t) => t.group === g[0] && !t.variant);
        if (!bases.length) return null;
        return html`<div key=${g[0]}>
          <div class="section-h">${g[1]}</div>
          <table class="tbl">
            <thead><tr><th>赛道</th><th>说明</th><th class="num">~耗时</th><th>语料</th><th>变体</th><th>限定引擎</th></tr></thead>
            <tbody>
              ${bases.map((b) => {
                const vs = tracks.filter((t) => t.base === b.name && t.variant);
                const vEngs = [...new Set(vs.filter((v) => v.engines).map((v) => v.engines))];
                return html`<tr key=${b.name}>
                  <td class="mono">${b.name}</td>
                  <td>${b.label}</td>
                  <td class="num">~${b.est}m</td>
                  <td>${b.dataset || '—'}</td>
                  <td style="max-width:340px;white-space:normal">${vs.length
                    ? vs.map((v) => html`<span key=${v.name} class="mono"
                        style=${'font-size:11px;margin-right:6px;' + (v.engines ? 'color:hsl(221 83% 50%)' : '')}
                        title=${v.engines ? '只支持 ' + v.engines : '两侧通用'}>${v.name}${v.engines ? ' *' : ''}</span>`)
                    : '—'}</td>
                  <td>${b.engines ? html`<span class="badge badge-run">${b.engines}</span>`
                    : vEngs.length ? html`<span class="muted">基座通用 · * 变体限 ${vEngs.join('/')}</span>`
                    : html`<span class="muted">通用</span>`}</td>
                </tr>`;
              })}
            </tbody>
          </table>
        </div>`;
      })}
    </div>
    ${EX && html`<${EX.TrackFilesView} />`}
  `;
}

// ==================== 车定义编辑器（runner 同口径校验 + 原子保存） ====================
// appendLine：外部（如「引擎与JDK」页按包名推导）要求追加的一行模板，进编辑器由人确认后保存
function CarsEditorDrawer({ onClose, onSaved, appendLine }) {
  const [info, setInfo] = useState(null);   // {path, content, sha256, diagnostics}
  const [draft, setDraft] = useState('');
  const [diag, setDiag] = useState([]);
  const [busy, setBusy] = useState(true);
  const [saving, setSaving] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [loadedSha, setLoadedSha] = useState('');

  async function load() {
    setBusy(true); setConflict(false);
    try {
      const { status, j } = await getJSON('/api/cars-file');
      if (status !== 200 || !j.available) {
        setInfo({ path: j ? j.path : '', reason: (j && (j.reason || j.error)) || '读取失败' });
        setDiag(j ? j.diagnostics || [] : []);
        return;
      }
      setInfo({ path: j.path });
      let content = j.content || '';
      if (appendLine && !content.split('\n').some((l) => l.trim() === appendLine)) {
        // 从包名推导的模板行：追加到末尾（草稿层追加，不落盘 —— 保存才生效）；
        // 「重新加载」时已含该行则不重复追加
        content = content.replace(/\s*$/, '') + '\n' + appendLine + '\n';
      }
      setDraft(content);
      setLoadedSha(j.sha256);
      setDiag(j.diagnostics || []);
    } catch (e) { setInfo({ path: '', reason: String(e) }); }
    setBusy(false);
  }
  useEffect(() => { load(); }, []);
  // Esc 关闭编辑器（backdrop 点击之外的第二条出路）
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // 前端即时校验（与服务端同规则的最小子集，仅提示；最终以服务端为准）
  const draftDiag = (() => {
    const out = [];
    const labels = {}, names = {};
    draft.split('\n').forEach((raw, i) => {
      const line = raw.trim();
      if (!line || line.startsWith('#')) return;
      const lineno = i + 1;
      const parts = line.split(/\s+/);
      const name = parts[0];
      const config = parts.slice(1).join(' ');
      names[name] = (names[name] || 0) + 1;
      const kv = {};
      config.split(/\s+/).filter(Boolean).forEach((tok) => {
        const eq = tok.indexOf('=');
        if (eq <= 0) { out.push({ line: lineno, severity: 'error', message: '裸 token（必须 KEY=VALUE）: ' + tok }); return; }
        const k = tok.slice(0, eq);
        kv[k] = tok.slice(eq + 1);
        if (!['ENGINE_NAME', 'ENGINE_VERSION', 'ENGINE_BUILD', 'JDK_DIR', 'CAR_LABEL', 'JDK_WANT', 'ARM_LABEL'].includes(k))
          out.push({ line: lineno, severity: 'error', message: '未知键 ' + k });
      });
      const label = kv.CAR_LABEL || kv.ARM_LABEL || name;
      labels[label] = (labels[label] || 0) + 1;
      const eng = (kv.ENGINE_NAME || '').toLowerCase();
      if (!eng) out.push({ line: lineno, severity: 'error', message: '缺 ENGINE_NAME' });
      if ((eng === 'elasticsearch' || eng === 'opensearch') && (kv.ENGINE_BUILD || kv.JDK_DIR))
        out.push({ line: lineno, severity: 'error', message: eng + ' 不应带 ENGINE_BUILD/JDK_DIR' });
    });
    Object.entries(labels).forEach(([l, n]) => { if (n > 1) out.push({ line: 0, severity: 'error', message: 'CAR_LABEL 重复: ' + l }); });
    Object.entries(names).forEach(([l, n]) => { if (n > 1) out.push({ line: 0, severity: 'error', message: '车名重复: ' + l }); });
    return out;
  })();
  const draftErrors = draftDiag.filter((x) => x.severity === 'error');

  async function save() {
    if (draftErrors.length) { toast.err('先修正 ' + draftErrors.length + ' 处 runner 会拒绝的问题'); return; }
    if (!confirm('保存车定义到 ' + (info ? info.path : '') + '？\n保存后车表立即刷新；正在编排的勾选若引用被删除的车会被裁掉。')) return;
    setSaving(true); setConflict(false);
    const { status, j } = await postJSON('/api/cars-write', { content: draft, expect: loadedSha });
    setSaving(false);
    if (status === 409) { setConflict(true); toast.err('车定义已被他人修改 —— 点「重新加载」拿最新内容'); return; }
    if (status !== 200) { toast.err((j && j.error) || '保存失败'); if (j && j.diagnostics) setDiag(j.diagnostics); return; }
    setLoadedSha(j.sha256);
    toast.ok('车定义已保存，车表已刷新（' + (j.cars || []).length + ' 车）');
    setDiag((j.problems || []).map((p) => ({ line: 0, severity: p[0] === 'error' ? 'error' : 'warning', message: p[1] })));
    onSaved && onSaved(j);
  }

  return html`<div class="drawer-backdrop" onClick=${onClose}>
    <div class="drawer" onClick=${(e) => e.stopPropagation()}>
      <div class="drawer-head">
        <b>车定义</b>
        <span class="muted mono" style="font-size:12px">${info ? info.path : ''}</span>
        <span class="spacer"></span>
        ${loadedSha ? html`<span class="muted mono" style="font-size:11px">sha ${loadedSha.slice(0, 10)}…</span>` : null}
        <button class="btn btn-ghost btn-sm" onClick=${load} disabled=${busy}>重新加载</button>
        <button class="btn btn-ghost btn-sm" onClick=${onClose}>✕ 关闭</button>
      </div>
      <div class="drawer-body">
        ${busy && html`<p class="muted">读取中…</p>`}
        ${!busy && info && info.reason ? html`<div class="warn-box">⚠ ${info.reason}</div>
          <p class="muted">编辑器不可用 —— 请在压测机上直接编辑该文件。</p>` : null}
        ${!busy && info && !info.reason ? html`<div>
          ${appendLine && html`<div class="warn-box">已按包名推导并追加一行车定义模板（<span class="mono">${appendLine}</span>）
            —— 车名/标签可改，确认后保存。车名或 CAR_LABEL 与现有行冲突会被校验拦下。</div>`}
          ${conflict && html`<div class="warn-box">⚠ 内容已被他人修改。点「重新加载」会覆盖你这里的草稿，请先复制重要修改。</div>`}
          ${draftErrors.length ? html`<div class="warn-box">✘ ${draftErrors.length} 处问题（保存会被拒绝）：
            ${draftErrors.slice(0, 6).map((x, i) => html`<div key=${i}>${x.line ? '行 ' + x.line + '：' : ''}${x.message}</div>`)}
            ${draftErrors.length > 6 ? html`<div>…还有 ${draftErrors.length - 6} 条</div>` : null}</div>` : null}
          <textarea class="textarea mono" style="width:100%;height:52vh;font-size:12.5px;line-height:1.55"
            value=${draft} onInput=${(e) => setDraft(e.target.value)} spellcheck=${false}></textarea>
          <div class="row" style="margin-top:8px">
            <button class="btn btn-primary" onClick=${save} disabled=${saving || draftErrors.length}>
              ${saving ? '保存中…' : '保存并刷新车表'}</button>
            <span class="muted">保存走严格校验（runner 同口径）+ 原子写；配置文件仍是唯一数据源。</span>
          </div>
        </div>` : null}
      </div>
    </div>
  </div>`;
}

// ==================== ⑥ 引擎与 JDK（只读资产盘点 + 车依赖） ====================
function fmtSize(n) {
  if (n == null) return '—';
  if (n >= 1 << 30) return (n / (1 << 30)).toFixed(1) + ' GB';
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(0) + ' MB';
  if (n >= 1024) return (n / 1024).toFixed(0) + ' KB';
  return n + ' B';
}

// 包文件名 → 车定义模板行。命名规则与 runner resolve_engine / api_assets 一一对应
// （easysearch-<ver>-<build>-linux-amd64 / elasticsearch-<ver>-linux-x86_64 /
//   opensearch-<ver>-linux-x64 / openjdk-<v>_linux-x64_bin）。认不出的包返回 null。
function pkgToCarLine(name) {
  let m = /^easysearch-(\d+\.\d+\.\d+)-(\d+)-linux-amd64-bundle\.tar\.gz$/.exec(name);
  if (m) {
    const tag = 'ez' + m[1].replace(/\./g, '');
    return tag + ' ENGINE_NAME=easysearch ENGINE_VERSION=' + m[1] + ' ENGINE_BUILD=' + m[2] + ' CAR_LABEL=' + tag;
  }
  m = /^elasticsearch-(\d+\.\d+\.\d+)-linux-x86_64\.tar\.gz$/.exec(name);
  if (m) {
    const tag = 'es' + m[1].replace(/\./g, '');
    return tag + ' ENGINE_NAME=elasticsearch ENGINE_VERSION=' + m[1] + ' CAR_LABEL=' + tag;
  }
  m = /^opensearch-(\d+\.\d+\.\d+)-linux-x64\.tar\.gz$/.exec(name);
  if (m) {
    const tag = 'os' + m[1].replace(/\./g, '');
    return tag + ' ENGINE_NAME=opensearch ENGINE_VERSION=' + m[1] + ' CAR_LABEL=' + tag;
  }
  m = /^openjdk-(\d+)_linux-x64_bin\.tar\.gz$/.exec(name);
  if (m) {
    const tag = 'jdk' + m[1];
    return tag + ' ENGINE_NAME=easysearch ENGINE_VERSION=2.5.0 ENGINE_BUILD=3015'
      + ' JDK_DIR=/opt/jdks/jdk-' + m[1] + ' CAR_LABEL=' + tag + ' JDK_WANT=' + m[1] + '|OpenJDK';
  }
  return null;
}

function AssetsView({ reloadBoot }) {
  const [carsEditor, setCarsEditor] = useState(false);
  const [appendLine, setAppendLine] = useState(null); // 从包名推导的车定义模板行（进编辑器追加）
  const [d, setD] = useState(null);   // {available, pkg_dir, deps, pkg_files, bundles}
  const [err, setErr] = useState('');
  const [busy, setBusy] = useState(false);
  async function load() {
    setBusy(true); setErr('');
    try {
      const { status, j } = await getJSON('/api/assets');
      if (status !== 200) setErr(j.error || '读取失败');
      else setD(j);
    } catch (e) { setErr(String(e)); }
    setBusy(false);
  }
  useEffect(() => { load(); }, []);
  if (err) return html`<div class="card"><p class="st-bad">${err}</p></div>`;
  if (!d) return html`<div class="content"><p class="muted">读取中…</p></div>`;
  if (!d.available) return html`<div class="content"><div class="card">
    <div class="card-title">引擎与 JDK</div>
    <p class="muted">${d.reason || '当前模式读不到安装包目录 —— 资产盘点在压测机本机控制台查看。'}</p>
  </div></div>`;

  const missing = d.deps.filter((r) => r.pkg && !r.pkg_stat.exists);
  const jdkMissing = d.deps.filter((r) => r.jdk_mapped && r.jdk_stat && !r.jdk_stat.exists);
  const byPath = {};
  (d.pkg_files || []).forEach((f) => { byPath[d.pkg_dir + '/' + f.name] = f; });

  return html`
    <div class="card">
      <div class="card-title">起跑依赖（车定义 → 安装包；与 runner resolve_engine 的 die 点同口径）
        <div class="spacer"></div>
        <span class=${missing.length ? 'st-bad' : 'st-ok'}>${missing.length ? '✘ ' + missing.length + ' 个引擎包缺失' : '✓ 引擎包在位'}</span>
        ${jdkMissing.length ? html`<span class="st-warn">⚠ ${jdkMissing.length} 个 JDK 控制机包缺失</span>` : null}
        <button class="btn btn-ghost btn-sm" onClick=${() => setCarsEditor(true)}
          title="在 Web 里编辑车定义文件（runner 同口径校验，保存后车表立即刷新）">编辑车定义</button>
        <button class="btn btn-ghost btn-sm" onClick=${load} disabled=${busy}>↻</button>
      </div>
      <table class="tbl">
        <thead><tr><th>车</th><th>引擎</th><th>引擎包（PKG_DIR）</th><th>JDK 包</th></tr></thead>
        <tbody>
          ${d.deps.map((r) => html`<tr key=${r.car}>
            <td class="mono">${r.car}</td>
            <td class="mono" style="font-size:12px">${r.engine} ${r.ver}${r.build ? '-' + r.build : ''}</td>
            <td>${r.pkg ? html`<span class=${r.pkg_stat.exists ? 'st-ok' : 'st-bad'}>${r.pkg_stat.exists ? '✓' : '✗ 缺失'}</span>
              <span class="mono" style="font-size:11px;word-break:break-all">${r.pkg.replace(d.pkg_dir + '/', '')}</span>` : html`<span class="st-bad">未知引擎</span>`}</td>
            <td>${!r.jdk_dir ? html`<span class="muted">引擎自带 JDK</span>` : html`
              <span class="mono" style="font-size:11px">节点路径 ${r.jdk_dir}</span><br/>
              <span class="muted">节点安装状态：未检查</span>
              ${r.jdk_mapped ? html`<br/><span class=${r.jdk_stat && r.jdk_stat.exists ? 'st-ok' : 'st-warn'}>
                ${r.jdk_stat && r.jdk_stat.exists ? '控制机安装包在位' : '控制机安装包缺失（节点未安装时需要）'}</span>
                <span class="mono" style="font-size:11px"> ${r.jdk_pkg}</span>` : html`<br/><span class="muted">非自动安装路径，请在节点自行确认</span>`}`}</td>
          </tr>`)}
        </tbody>
      </table>
      ${d.config_warning ? html`<div class="warn-box">⚠ ${d.config_warning}</div>` : null}
      <p class="card-desc">引擎包路径按 runner 的命名规则推导；节点 JDK 未检查。JDK 控制机安装包仅在节点缺少对应路径、且支持自动安装时需要。本页只读，实际部署来源以 ibs 配置与运行日志为准。</p>
    </div>
    ${carsEditor && html`<${CarsEditorDrawer} appendLine=${appendLine}
      onClose=${() => { setCarsEditor(false); setAppendLine(null); }}
      onSaved=${() => reloadBoot && reloadBoot(true)} />`}

    <div class="card">
      <div class="card-title">本地包仓库（${d.pkg_dir} · ${(d.pkg_files || []).length} 个文件）</div>
      <p class="card-desc">pkg/ 里的引擎包会按包名自动出现在「启动排程」（确定性车名，可直接勾选起跑，每次进页自动重读）。
        这里点「＋ 车定义」是把包<b>固定</b>进车定义文件 —— 需要配 JDK（JDK_DIR/JDK_WANT）或自定义标签时才用；
        推导口径与 runner resolve_engine 一致，确认车名/标签后保存，保存走原子写 + sha256 并发校验。</p>
      <table class="tbl">
        <thead><tr><th>文件</th><th class="num">大小</th><th>修改时间</th><th>被引用</th><th></th></tr></thead>
        <tbody>
          ${(d.pkg_files || []).map((f) => {
            const path = d.pkg_dir + '/' + f.name;
            const ref = d.deps.some((r) => r.pkg === path || r.jdk_pkg === path) ? '引擎/JDK'
              : (d.bundles || []).some((b) => b.path === path) ? 'BUNDLE_*' : '—';
            const line = ref === '—' ? pkgToCarLine(f.name) : null;
            return html`<tr key=${f.name}>
              <td class="mono" style="font-size:12px;word-break:break-all">${f.name}</td>
              <td class="num">${fmtSize(f.size)}</td>
              <td class="muted">${new Date(f.mtime * 1000).toLocaleDateString()}</td>
              <td>${ref === '—' ? html`<span class="muted">—</span>` : html`<span class="badge badge-run">${ref}</span>`}</td>
              <td style="white-space:nowrap">${line ? html`
                <button class="btn btn-secondary btn-sm" onClick=${() => { setAppendLine(line); setCarsEditor(true); }}
                  title=${'追加车定义行：' + line}>＋ 车定义</button>` : null}</td>
            </tr>`;
          })}
          ${!(d.pkg_files || []).length && html`<tr><td colspan="5" class="muted">（目录为空）</td></tr>`}
        </tbody>
      </table>
    </div>

    <div class="card">
      <div class="card-title">vm-bench.env 的 BUNDLE_* 引用（${(d.bundles || []).length} 条）
        <div class="spacer"></div>
        <span class="muted">runner 与 ibs 当前都没有消费这些键 —— 历史遗留，建议清理或改为注释</span></div>
      <table class="tbl">
        <thead><tr><th>键</th><th>文件</th><th>状态</th></tr></thead>
        <tbody>
          ${(d.bundles || []).map((b) => html`<tr key=${b.key}>
            <td class="mono">${b.key}</td>
            <td class="mono" style="font-size:11px;word-break:break-all">${b.path}</td>
            <td>${b.exists ? html`<span class="st-ok">✓ 存在</span>` : html`<span class="st-bad">✗ 不存在</span>`}</td>
          </tr>`)}
          ${!(d.bundles || []).length && html`<tr><td colspan="3" class="muted">（无 BUNDLE_* 键）</td></tr>`}
        </tbody>
      </table>
      <p class="card-desc">引擎包的消费方是 runner 的 <code>resolve_engine</code>（直接拼 PKG_DIR 路径）与 ibs 的
        <code>LOCAL_PKG_DIR</code>（ibs configs/.env，未启用时部署走官方源下载）—— BUNDLE_* 键不参与。</p>
    </div>
  `;
}

// ==================== ⑤ 导出与入库 ====================
function ExportView({ boot, camps, EX }) {
  const [exports, setExports] = useState([]);
  const [expInfo, setExpInfo] = useState('（先刷新导出清单）');
  const [exp, setExp] = useState('');
  // 默认值来自 console.conf 的 apply.*（bootstrap 下发 apply_defaults；auth 不下发，
  // 服务端在前端口令留空时自动使用配置里的口令）
  const ad = boot.apply_defaults || {};
  const [es, setEs] = useState(ad.es || 'https://127.0.0.1:9237');
  const [auth, setAuth] = useState('');
  const [insec, setInsec] = useState(ad.insecure !== '0');
  const [drop, setDrop] = useState(ad.drop === '1');
  const [apOut, setApOut] = useState('（未运行）');

  async function loadExports() {
    setExpInfo('读取 exports/ 清单中…');
    const { status, j } = await getJSON('/api/exports');
    if (status !== 200) { setExpInfo('读取失败: ' + (j.error || '未知')); return; }
    const list = j.exports || [];
    setExports(list);
    // 保留当前选择（重新生成/刷新后不把用户已选的 tag 跳回第一项）；没有则取第一个
    setExp((prev) => list.some((e) => e.tag === prev) ? prev : (list[0] ? list[0].tag : ''));
    setExpInfo(list.length
      ? '断言未通过或缺失的导出，bench-apply 会在预检拒绝，不会发任何请求。'
      : '（exports/ 下没有可入库的导出）');
  }
  useEffect(() => { loadExports(); }, []);
  // 「一键生成导出」成功后自动带上新 tag（extras.js ExportGenView 发事件）
  useEffect(() => {
    const onR = () => loadExports();
    window.addEventListener('raceway-exports-refresh', onR);
    return () => window.removeEventListener('raceway-exports-refresh', onR);
  }, []);

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
    ${EX && html`<${EX.ExportGenView} camps=${camps} />`}
    <div class="card">
      <div class="card-title">结果入库（exports/ → ES；bench-apply 预检不过不发任何请求，退出码 0=成功 1=有拒收 2=预检失败）
        <div class="spacer"></div>
        <button class="btn btn-ghost btn-sm" onClick=${loadExports}>刷新导出清单</button></div>
      <div class="row">
        <select style="max-width:260px" value=${exp} onChange=${(e) => setExp(e.target.value)}>
          ${exports.map((e) => html`
            <option key=${e.tag} value=${e.tag}>${e.tag}（${e.assertions === 'pass' ? '✔断言通过' : '✘断言' + (e.assertions || '无')}，${e.counts ? e.counts.bench_metric + ' 行指标' : '?'}）</option>`)}
        </select>
        <span class="muted">${expInfo}</span>
      </div>
      <div class="row">
        <label class="muted">ES <input type="text" style="width:32ch" value=${es} onInput=${(e) => setEs(e.target.value)} /></label>
        <label class="muted">口令 <input type="password" style="width:14ch"
          placeholder=${boot.apply_auth_configured ? '（已由 console.conf 提供，留空即用）' : 'user:pass（无鉴权集群留空）'}
          value=${auth} onInput=${(e) => setAuth(e.target.value)} /></label>
        <label class="chk"><input type="checkbox" checked=${insec} onChange=${(e) => setInsec(e.target.checked)} /> 自签证书 --insecure</label>
        <label class="chk"><input type="checkbox" checked=${drop} onChange=${(e) => setDrop(e.target.checked)} />
          --drop（先删三张索引再重建；mapping 变更时必需）</label>
        <button class="btn btn-primary btn-sm" onClick=${doApply}>入库</button>
      </div>
      <p class="card-desc">ES / insecure / drop 默认值来自 raceway 根的 <code>console.conf</code>（apply.* 行，
        没有配置文件用内置默认）；口令若已写进 console.conf，此框留空即用且不在服务端留存。
        入库后在「结果与趋势」页的 Kibana 看板查看。</p>
      <pre class="out" style="max-height:320px">${apOut}</pre>
    </div>
  `;
}

// 注入依赖给扩展组件（extras.js 已先于本脚本加载）
if (window.RacewayExtras && window.RacewayExtras.setup) {
  EX = window.RacewayExtras.setup({
    h, html, useState, useEffect, useRef, useMemo,
    getJSON, postJSON, toast, fmtMin, fmtN, fmtTime, Icon, ICONS, writeHeaders,
  });
}

render(html`<${App} />`, document.getElementById('app'));
