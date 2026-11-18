/* raceway 控制台扩展功能（Go 原生后端 / Python fallback 双通道）
 *
 * 依赖注入：window.RacewayExtras.setup({h,html,useState,useEffect,useRef,useMemo,
 *   getJSON,postJSON,toast,VT,VC,BT,fmtMin,fmtN,fmtTime,Icon,ICONS,writeHeaders})
 * 返回 {tabs, views, onTab, enhanceCompose, enhanceProgress}：
 *   tabs  —— 追加到主导航的页签
 *   views —— {id: 组件} 供 App 按 tab 渲染
 *   onTab —— 切换页签时的副作用（深链路由）
 * 所有新增 API 均为 Go 原生 handler，走同一 CSRF 令牌。
 */
'use strict';
(function () {
  function setup(D) {
    const { h, html, useState, useEffect, useRef, useMemo, getJSON, postJSON,
            toast, fmtN, fmtTime, Icon, ICONS } = D;
    const esc = (s) => String(s == null ? '' : s);

    // ---------- 通用小组件 ----------
    function OutBox({ text, title }) {
      return html`<div class="card">
        <div class="card-title">${title || '输出'}</div>
        <pre class="mono" style="max-height:360px;overflow:auto;white-space:pre-wrap;font-size:12px">${esc(text)}</pre>
      </div>`;
    }

    function DownloadBtn({ text, filename, mime, label }) {
      function go() {
        try {
          const blob = new Blob([text || ''], { type: mime || 'text/plain;charset=utf-8' });
          const url = URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = url; a.download = filename || 'download';
          document.body.appendChild(a); a.click();
          setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 100);
          toast.ok('已下载 ' + (filename || '文件'));
        } catch (e) { toast.err('下载失败: ' + e); }
      }
      return html`<button class="btn btn-sm" onClick=${go}>${label || '下载'}</button>`;
    }

    // ---------- ⑥ 赛道文件管理 ----------
    function TrackFilesView() {
      const [files, setFiles] = useState([]);
      const [loading, setLoading] = useState(true);
      const [sel, setSel] = useState(null);        // {path, content, sha256}
      const [draft, setDraft] = useState('');
      const [filter, setFilter] = useState('');
      const [busy, setBusy] = useState(false);
      const [confirmDel, setConfirmDel] = useState(false);

      async function refresh() {
        setLoading(true);
        try {
          const { status, j } = await getJSON('/api/track-files');
          if (status !== 200) { toast.err(j.error || '列赛道失败'); setFiles([]); }
          else setFiles(j.files || []);
        } catch (e) { toast.err(String(e)); }
        setLoading(false);
      }
      useEffect(() => { refresh(); }, []);

      async function openFile(p) {
        setBusy(true);
        try {
          const { status, j } = await getJSON('/api/track-file?path=' + encodeURIComponent(p));
          if (status !== 200) { toast.err(j.error || '读文件失败'); }
          else { setSel(j); setDraft(j.content || ''); setConfirmDel(false); }
        } catch (e) { toast.err(String(e)); }
        setBusy(false);
      }

      function newFile() {
        setSel({ path: '', content: '', sha256: null, isNew: true });
        setDraft(''); setConfirmDel(false);
      }

      async function save() {
        if (!sel) return;
        const p = (sel.path || '').trim();
        // 与后端 safe_track_path 同口径：分段校验，允许 / 子目录（如 geonames/index.json）
        const segs = p.split('/');
        if (!p || p.length > 200 || p.startsWith('/') || p.includes('\\')
          || !segs.every((s) => /^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(s))) {
          toast.err('路径每段只能是字母数字与 ._-，可用 / 分目录（如 geonames/index.json）'); return;
        }
        setBusy(true);
        const body = { path: p, content: draft };
        if (sel.sha256) body.expect = sel.sha256;
        const { status, j } = await postJSON('/api/track-write', body);
        setBusy(false);
        if (status !== 200) { toast.err(j.error || '保存失败'); return; }
        toast.ok((sel.isNew ? '新建' : '保存') + '成功: ' + p);
        window.dispatchEvent(new CustomEvent('raceway-boot-refresh')); // 赛道清单联动刷新
        await refresh(); setSel(null);
      }

      async function del() {
        if (!sel || !sel.path || sel.isNew) return;
        setBusy(true);
        const body = { path: sel.path };
        if (sel.sha256) body.expect = sel.sha256;
        const { status, j } = await postJSON('/api/track-delete', body);
        setBusy(false);
        if (status !== 200) { toast.err(j.error || '删除失败'); return; }
        toast.ok('已删除: ' + sel.path);
        window.dispatchEvent(new CustomEvent('raceway-boot-refresh')); // 赛道清单联动刷新
        setSel(null); setConfirmDel(false); await refresh();
      }

      const shown = files.filter((f) => !filter || f.path.toLowerCase().includes(filter.toLowerCase()));
      return html`<div class="card">
        <div class="card-title">赛道文件（tracks/）
          <span class="spacer"></span>
          <button class="btn btn-sm" onClick=${newFile}>＋ 新建文件</button>
          <button class="btn btn-sm btn-ghost" onClick=${refresh} disabled=${loading}>刷新</button>
        </div>
        <p class="card-desc">编辑的是仓库定版赛道定义；保存走原子写 + sha256 并发校验。改动需同步到压测机后才会被 runner 消费。</p>
        <div class="row"><input class="input" placeholder="筛选文件名…" value=${filter}
          onInput=${(e) => setFilter(e.target.value)} style="max-width:280px" />
          <span class="muted">共 ${files.length} 个文件</span></div>
        <div class="x-files-grid">
          <div style="max-height:520px;overflow:auto;border:1px solid hsl(var(--border));border-radius:6px">
            ${loading ? html`<div class="muted" style="padding:12px">加载中…</div>` : null}
            ${!loading && shown.length === 0 ? html`<div class="muted" style="padding:12px">（无文件）</div>` : null}
            ${shown.map((f) => html`<div key=${f.path}
              class=${'nav-item ' + (sel && sel.path === f.path ? 'on' : '')}
              style="border-radius:0;cursor:pointer;justify-content:space-between"
              onClick=${() => openFile(f.path)}>
              <span class="mono" style="font-size:12px;overflow:hidden;text-overflow:ellipsis">${f.path}</span>
              <small>${fmtN(f.size)}B</small>
            </div>`)}
          </div>
          <div>
            ${!sel ? html`<div class="muted">选择左侧文件查看/编辑，或点「新建文件」。</div>` : html`
              <div class="row">
                <label class="muted">路径</label>
                <input class="input mono x-grow" value=${sel.path}
                  disabled=${!sel.isNew} placeholder="如 geonames/index.json"
                  onInput=${(e) => setSel(Object.assign({}, sel, { path: e.target.value }))} />
              </div>
              <textarea class="textarea mono" style="width:100%;height:340px;font-size:12px"
                value=${draft} onInput=${(e) => setDraft(e.target.value)} spellcheck=${false}></textarea>
              <div class="row" style="margin-top:8px">
                <button class="btn btn-primary btn-sm" onClick=${save} disabled=${busy}>${sel.isNew ? '创建' : '保存'}</button>
                ${!sel.isNew ? html`
                  ${!confirmDel ? html`<button class="btn btn-ghost btn-sm" onClick=${() => setConfirmDel(true)}>删除</button>`
                    : html`<button class="btn btn-danger btn-sm" onClick=${del} disabled=${busy}>确认删除？</button>`}` : null}
                <button class="btn btn-secondary btn-sm" onClick=${() => setSel(null)}>关闭</button>
                ${sel.sha256 ? html`<span class="muted mono">sha ${sel.sha256.slice(0, 12)}…</span>` : null}
              </div>`}
          </div>
        </div>
      </div>`;
    }

    // ---------- ⑦ 报告导出 ----------
    function ReportView({ camps, curCid, setCurCid }) {
      const [cid, setCid] = useState(curCid || (camps[0] && camps[0].cid) || '');
      const [fmt, setFmt] = useState('md');
      const [text, setText] = useState('');
      const [busy, setBusy] = useState(false);
      useEffect(() => { if (!cid && camps[0]) setCid(camps[0].cid); }, [camps]);
      // 跟随进度页/快切条当前选中的 campaign（curCid 变化即带上，不用再手选）
      useEffect(() => { if (curCid) setCid(curCid); }, [curCid]);

      async function gen() {
        if (!cid) { toast.err('先选一个 campaign'); return; }
        setBusy(true);
        try {
          const { status, j } = await getJSON('/api/report?cid=' + encodeURIComponent(cid) + '&fmt=' + fmt);
          if (status !== 200) toast.err(j.error || '生成失败');
          else setText(j.text || '');
        } catch (e) { toast.err(String(e)); }
        setBusy(false);
      }
      return html`<div class="card">
        <div class="card-title">报告导出</div>
        <p class="card-desc">按 campaign 生成 Markdown / HTML 报告，口径取自 PLAN / RACES / ENV-SUMMARY。纯离线读文件，不改数据。</p>
        <div class="row">
          <label class="muted">campaign</label>
          <select class="input x-grow" value=${cid} onInput=${(e) => setCid(e.target.value)}>
            ${camps.map((c) => html`<option key=${c.cid} value=${c.cid}>${c.cid}</option>`)}
          </select>
          <label class="muted">格式</label>
          <select class="input" value=${fmt} onInput=${(e) => setFmt(e.target.value)}>
            <option value="md">Markdown</option><option value="html">HTML</option>
          </select>
          <button class="btn btn-primary btn-sm" onClick=${gen} disabled=${busy}>${busy ? '生成中…' : '生成报告'}</button>
          ${text ? html`<${DownloadBtn} text=${text} filename=${'raceway-' + cid + '.' + (fmt === 'html' ? 'html' : 'md')}
            mime=${fmt === 'html' ? 'text/html;charset=utf-8' : 'text/markdown;charset=utf-8'} label="下载" />` : null}
        </div>
        ${text ? html`<pre class="mono" style="max-height:480px;overflow:auto;white-space:pre-wrap;font-size:12px;margin-top:10px">${text}</pre>` : null}
      </div>`;
    }

    // ---------- ⑧ 一键生成导出 ----------
    function ExportGenView({ camps }) {
      const [sel, setSel] = useState([]);
      const [tag, setTag] = useState('');
      const [filter, setFilter] = useState('');
      const [busy, setBusy] = useState(false);
      const [startedAt, setStartedAt] = useState(null);
      const [elapsed, setElapsed] = useState(0);
      const [out, setOut] = useState('');
      const [summary, setSummary] = useState(null);
      function toggle(cid) {
        setSel((s) => s.includes(cid) ? s.filter((x) => x !== cid) : s.concat([cid]));
      }
      // 生成中计时器（远端 build-race-store → bench-export 可能数分钟）
      useEffect(() => {
        if (!busy || !startedAt) return undefined;
        const t = setInterval(() => setElapsed(Math.round((Date.now() - startedAt) / 1000)), 1000);
        return () => clearInterval(t);
      }, [busy, startedAt]);
      const shown = camps.filter((c) => !filter || c.cid.toLowerCase().includes(filter.toLowerCase()));
      async function gen() {
        if (!sel.length) { toast.err('先选 campaign'); return; }
        if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(tag.trim())) {
          toast.err('导出 tag 只能是安全文件名（字母数字开头）'); return;
        }
        const t0 = Date.now(); // 不能读闭包里的 startedAt —— setState 后本闭包里还是旧值（null）
        setBusy(true); setStartedAt(t0); setElapsed(0); setSummary(null);
        setOut('生成中：build-race-store → bench-export…（可能数分钟，取决于场次与指标量）');
        try {
          const { status, j } = await postJSON('/api/export-gen', { tag: tag.trim(), campaigns: sel });
          if (status !== 200) { setOut('失败: ' + (j.error || '未知')); toast.err('生成失败'); }
          else {
            setOut(j.log || '已生成导出 ' + tag);
            setSummary(j.summary || null);
            toast.ok('导出已生成: ' + tag + '（耗时 ' + Math.round((Date.now() - t0) / 1000) + 's）');
            // 让同页的「结果入库」清单自动带上新 tag（监听方：app.js ExportView）
            window.dispatchEvent(new CustomEvent('raceway-exports-refresh'));
          }
        } catch (e) { setOut(String(e)); toast.err(String(e)); }
        setBusy(false); setStartedAt(null);
      }
      return html`<div class="card">
        <div class="card-title">一键生成导出</div>
        <p class="card-desc">选 campaign → 建 race-store → 生成 exports/&lt;tag&gt;，随后在下方「结果入库」选择该 tag 写入指标库。已存在的 tag 不覆盖；staging 中断自动清理。</p>
        <div class="row">
          <label class="muted">导出 tag</label>
          <input class="input mono" value=${tag} onInput=${(e) => setTag(e.target.value)}
            placeholder="如 exp-20261116" style="max-width:220px" />
          <button class="btn btn-primary btn-sm" onClick=${gen} disabled=${busy || !sel.length}>
            ${busy ? html`生成中… <small>已 ${elapsed}s</small>` : '生成导出（' + sel.length + ' 场）'}</button>
          ${busy && html`<span class="muted">长操作占锁（最长 ${Math.round(1300 / 60)} 分钟）；生成完成前别发起入库。</span>`}
        </div>
        <div class="row" style="margin-top:8px">
          <input class="input x-grow" placeholder="按 campaign 名过滤…" value=${filter}
            onInput=${(e) => setFilter(e.target.value)} style="max-width:260px" />
          <button class="btn btn-ghost btn-sm" onClick=${() => setSel([...new Set(sel.concat(shown.map((c) => c.cid)))])}>选中过滤结果</button>
          <button class="btn btn-ghost btn-sm" onClick=${() => setSel([])}>清空</button>
          <span class="muted">${sel.length}/${camps.length} 已选</span>
        </div>
        <div style="max-height:220px;overflow:auto;border:1px solid hsl(var(--border));border-radius:6px;margin-top:8px">
          ${shown.map((c) => html`<label key=${c.cid} class="check-item" style="border:0;border-bottom:1px solid hsl(var(--border));border-radius:0">
            <input type="checkbox" checked=${sel.includes(c.cid)} onChange=${() => toggle(c.cid)} />
            <span class="mono" style="font-size:12px">${c.cid}</span>
            <small class="muted">${c.valid}/${c.total}</small>
          </label>`)}
          ${!shown.length && html`<div class="muted" style="padding:10px">（无匹配）</div>`}
        </div>
        ${summary && html`<div class="row" style="margin-top:10px;gap:14px;flex-wrap:wrap">
          <span class="stat-card" style="min-width:0"><b>${fmtN(summary.bench_metric)}</b><span>指标行</span></span>
          <span class="stat-card" style="min-width:0"><b>${fmtN(summary.bench_run)}</b><span>场次</span></span>
          <span class="stat-card" style="min-width:0"><b>${fmtN(summary.races_scanned)}</b><span>扫描 race</span></span>
          <span class="stat-card" style="min-width:0"><b>${fmtN(summary.bench_env)}</b><span>环境档案</span></span>
          ${summary.monitors_orphan_cids ? html`<span class="stat-card" style="min-width:0"><b>${summary.monitors_orphan_fids}</b><span>孤儿监控场次（${summary.monitors_orphan_cids} campaign，已丢弃）</span></span>` : null}
        </div>`}
        ${out ? html`<details class="fold" style="margin-top:8px"><summary>原始输出（build-race-store / bench-export）</summary>
          <pre class="mono" style="max-height:240px;overflow:auto;white-space:pre-wrap;font-size:12px;margin-top:6px">${out}</pre></details>` : null}
      </div>`;
    }

    // ---------- ⑨ 实测身份卡 + ⑩ 场次详情抽屉 + 监控时序 ----------
    function kvRows(obj) {
      if (!obj || typeof obj !== 'object') return html`<div class="muted">（无数据）</div>`;
      const rows = [];
      for (const k of Object.keys(obj).sort()) {
        const v = obj[k];
        if (v && typeof v === 'object') continue;
        rows.push(html`<div key=${k} class="row" style="margin:2px 0">
          <span class="muted mono x-kv-key">${k}</span>
          <span class="mono x-kv-val">${esc(v)}</span></div>`);
      }
      return rows.length ? rows : html`<div class="muted">（无标量字段）</div>`;
    }

    function SidecarCard({ cid, fid }) {
      const [data, setData] = useState(null);
      const [busy, setBusy] = useState(false);
      async function load() {
        if (!fid) return;
        setBusy(true);
        try {
          const { status, j } = await getJSON('/api/sidecar?cid=' + encodeURIComponent(cid) +
            '&fid=' + encodeURIComponent(fid));
          if (status !== 200) toast.err(j.error || '读身份失败');
          else setData(j);
        } catch (e) { toast.err(String(e)); }
        setBusy(false);
      }
      useEffect(() => { load(); }, [cid, fid]);
      if (!fid) return html`<div class="muted">选一场看实测身份。</div>`;
      if (!data) return html`<div class="muted">${busy ? '读取中…' : '—'}</div>`;
      if (!data.sidecars || !data.sidecars.length)
        return html`<div class="muted">该场次没有 sidecar 落档。</div>`;
      return html`<div>
        ${data.sidecars.map((s) => html`<div key=${s.phase} style="margin-bottom:12px">
          <div class="section-h">相位 ${s.phase} · <span class="mono" style="font-size:11px">${s.file}</span></div>
          ${kvRows(s.data)}
        </div>`)}
      </div>`;
    }

    let ECHARTS_PROMISE = null;
    function loadEchartsX() {
      if (window.echarts) return Promise.resolve(true);
      if (!ECHARTS_PROMISE) {
        ECHARTS_PROMISE = new Promise((res) => {
          const s = document.createElement('script');
          s.src = '/echarts.min.js';
          s.onload = () => res(typeof window.echarts !== 'undefined');
          s.onerror = () => res(false);
          document.head.appendChild(s);
        });
      }
      return ECHARTS_PROMISE;
    }
    // canvas 不解析 CSS 变量 —— 主题色在渲染时解析成具体值（解析失败回退浅灰）。
    // 本项目变量是组件式定义（--border: 214.3 31.8% 91.4%），要包一层 hsl()
    function cssColor(name, fallback) {
      try {
        const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        if (!v) return fallback;
        return /^\d/.test(v) ? 'hsl(' + v + ')' : v;
      } catch (e) { return fallback; }
    }

    function MonitorChart({ series, field }) {
      const ref = useRef(null);
      const inst = useRef(null);
      const [mode, setMode] = useState('waiting'); // 'waiting' → 'echarts' | 'svg'
      useEffect(() => { loadEchartsX().then((ok) => setMode(ok ? 'echarts' : 'svg')); }, []);
      // echarts 主路径：hover 看数值、图例点选隐藏节点、滚轮/拖拽缩放 —— 排查 steal 峰值必备
      useEffect(() => {
        if (mode !== 'echarts' || !ref.current || !series || !series.length) return;
        const colors = ['#2563eb', '#16a34a', '#dc2626', '#9333ea', '#0891b2', '#ea580c'];
        const xs = [];
        (series[0].rows || []).forEach((r) => {
          xs.push(r.ts != null ? String(r.ts) : '');
        });
        const legend = [], lines = [];
        series.forEach((s, idx) => {
          legend.push(s.node);
          lines.push({
            name: s.node, type: 'line', showSymbol: false,
            data: (s.rows || []).map((r) => (typeof r[field] === 'number' ? r[field] : null)),
            itemStyle: { color: colors[idx % colors.length] },
            lineStyle: { width: 1.5, color: colors[idx % colors.length] },
          });
        });
        if (!xs.length) return;
        const border = cssColor('--border', '#e4e4e7');
        const axisText = cssColor('--muted-foreground', '#71717a');
        const chart = window.echarts.getInstanceByDom(ref.current)
          || window.echarts.init(ref.current);
        inst.current = chart;
        chart.setOption({
          animation: false,
          tooltip: { trigger: 'axis', textStyle: { fontSize: 11 } },
          legend: { data: legend, top: 0, right: 0, textStyle: { fontSize: 10, color: axisText },
            itemWidth: 12, itemHeight: 8 },
          grid: { left: 44, right: 8, top: 20, bottom: 18 },
          xAxis: { type: 'category', data: xs, axisLabel: { show: false },
            axisTick: { show: false }, axisLine: { lineStyle: { color: border } } },
          yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: border } },
            axisLabel: { fontSize: 10, color: axisText } },
          dataZoom: [{ type: 'inside' }],
          series: lines,
        }, true);
      }, [series, field, mode]);
      // SVG 兜底：仓库没带 echarts.min.js 时退回原折线（只读，无数值提示）
      useEffect(() => {
        if (mode !== 'svg' || !ref.current || !series || !series.length) return;
        const colors = ['#2563eb', '#16a34a', '#dc2626', '#9333ea', '#0891b2', '#ea580c'];
        const maxPts = 600;
        const W = ref.current.clientWidth || 720, H = 200;
        const all = [];
        series.forEach((s) => (s.rows || []).forEach((r) => {
          const v = r[field]; if (typeof v === 'number') all.push(v);
        }));
        if (!all.length) return;
        const lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
        const span = (hi - lo) || 1;
        const svg = ['<svg width="100%" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" style="background:hsl(var(--muted)/.3);border-radius:6px">'];
        // grid
        for (let i = 0; i <= 4; i++) {
          const y = 12 + (H - 24) * i / 4;
          svg.push('<line x1="0" y1="' + y + '" x2="' + W + '" y2="' + y + '" stroke="hsl(var(--border))" stroke-width="1"/>');
        }
        svg.push('<text x="4" y="12" font-size="10" fill="hsl(var(--muted-foreground))">' + hi.toFixed(2) + '</text>');
        svg.push('<text x="4" y="' + (H - 4) + '" font-size="10" fill="hsl(var(--muted-foreground))">' + lo.toFixed(2) + '</text>');
        series.forEach((s, idx) => {
          const rows = (s.rows || []).slice(-maxPts);
          if (!rows.length) return;
          const pts = rows.map((r, i) => {
            const v = r[field];
            if (typeof v !== 'number') return null;
            const x = W * i / Math.max(1, rows.length - 1);
            const y = 12 + (H - 24) * (1 - (v - lo) / span);
            return x.toFixed(1) + ',' + y.toFixed(1);
          }).filter(Boolean).join(' ');
          svg.push('<polyline fill="none" stroke="' + (colors[idx % colors.length]) +
            '" stroke-width="1.5" points="' + pts + '"/>');
          svg.push('<text x="' + (W - 130 + (idx % 3) * 42) + '" y="' + (14 + Math.floor(idx / 3) * 13) +
            '" font-size="10" fill="' + (colors[idx % colors.length]) + '">' + esc(s.node) + '</text>');
        });
        svg.push('</svg>');
        ref.current.innerHTML = svg.join('');
      }, [series, field, mode]);
      // 卸载时释放 echarts 实例（进度页/抽屉反复挂载，别泄漏 canvas）
      useEffect(() => () => {
        if (inst.current) { inst.current.dispose(); inst.current = null; }
      }, []);
      return html`<div ref=${ref} style=${mode === 'svg' ? '' : 'height:200px'}></div>`;
    }

    function RaceDetailDrawer({ cid, fid, onClose }) {
      const [detail, setDetail] = useState(null);
      const [monitor, setMonitor] = useState(null);
      const [sub, setSub] = useState('身份');
      const [logFile, setLogFile] = useState(null);   // 产物点击 → 日志页签预填该路径
      const [busy, setBusy] = useState(false);
      useEffect(() => {
        if (!cid || !fid) return;
        setBusy(true);
        Promise.all([
          getJSON('/api/race-detail?cid=' + encodeURIComponent(cid) + '&fid=' + encodeURIComponent(fid)),
          getJSON('/api/monitor?cid=' + encodeURIComponent(cid) + '&fid=' + encodeURIComponent(fid)),
        ]).then(([d, m]) => {
          setDetail(d.j); setMonitor(m.j);
          setBusy(false);
        }).catch((e) => { toast.err(String(e)); setBusy(false); });
      }, [cid, fid]);
      // Esc 关闭抽屉（backdrop 点击之外的第二条出路）
      useEffect(() => {
        const onKey = (e) => { if (e.key === 'Escape') onClose(); };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
      }, [onClose]);

      return html`<div class="drawer-backdrop" onClick=${onClose}>
        <div class="drawer" onClick=${(e) => e.stopPropagation()}>
          <div class="drawer-head">
            <b class="mono">${fid}</b>
            <span class="muted">${cid}</span>
            <span class="spacer"></span>
            <button class="btn btn-ghost btn-sm" onClick=${onClose}>✕ 关闭</button>
          </div>
          <div class="drawer-tabs">
            ${['身份', '详情', '监控', '日志'].map((t) => html`<button key=${t}
              class=${'btn btn-sm ' + (sub === t ? 'btn-primary' : 'btn-ghost')}
              onClick=${() => setSub(t)}>${t}</button>`)}
          </div>
          <div class="drawer-body">
            ${busy ? html`<p class="muted">读取中…</p>` : null}
            ${sub === '身份' && detail ? html`<${SidecarCard} cid=${cid} fid=${fid} />` : null}
            ${sub === '详情' && detail ? html`<div>
              <div class="section-h">计划行（PLAN.tsv）</div>
              ${detail.plan ? html`<pre class="mono" style="font-size:12px">${detail.plan.join('\t')}</pre>` : html`<div class="muted">（无）</div>`}
              <div class="section-h">尝试判定（RACES.md）</div>
              ${detail.races && detail.races.length ? detail.races.map((r, i) => html`<div key=${i} class="mono" style="font-size:12px">${r}</div>`) : html`<div class="muted">（无）</div>`}
              <div class="section-h">逐任务指标</div>
              ${detail.metrics && detail.metrics.length ? html`<table class="hmatrix"><thead><tr><th>相位</th><th>任务</th><th class="num">吞吐 median</th><th class="num">p50</th><th class="num">p99</th></tr></thead><tbody>
                ${detail.metrics.map((m, i) => html`<tr key=${i}><td>${m.phase}</td><td class="mono">${esc(m.task)}</td>
                  <td class="num">${fmtN(m.throughput)} <small>${esc(m.tput_unit || '')}</small></td>
                  <td class="num">${m.lat_p50 != null ? fmtN(m.lat_p50) : '—'}</td>
                  <td class="num">${m.lat_p99 != null ? fmtN(m.lat_p99) : '—'} <small>${m.lat_p99 != null ? esc(m.lat_unit || '') : ''}</small></td></tr>`)}
              </tbody></table>` : html`<div class="muted">（无 race.json 指标）</div>`}
              <div class="section-h">产物（点文件名直接在日志页签里看内容）</div>
              ${detail.artifacts && detail.artifacts.length ? detail.artifacts.map((a) => html`
                <div key=${a} class="mono" style="font-size:12px">
                  <a href="#" style="text-decoration:none"
                    title="在日志页签打开 ${a}"
                    onClick=${(e) => { e.preventDefault(); setLogFile(a); setSub('日志'); }}>${a}</a>
                </div>`) : html`<div class="muted">（无）</div>`}
            </div>` : null}
            ${sub === '监控' && monitor ? html`<div>
              ${!monitor.series || !monitor.series.length ? html`<div class="muted">该场次没有监控 CSV。</div>` : html`
                <div class="muted" style="margin-bottom:6px">每节点 ${monitor.interval}s 采样 · ${monitor.series.map((s) => s.node).join(' / ')}</div>
                ${['cpu_usr', 'cpu_steal', 'bench_cpu', 'load1', 'foreign_cpu'].map((f) => {
                  const has = monitor.series.some((s) => (s.header || []).includes(f));
                  return has ? html`<div key=${f} style="margin-bottom:14px">
                    <div class="section-h">${f}</div><${MonitorChart} series=${monitor.series} field=${f} />
                  </div>` : null;
                })}`}
            </div>` : null}
            ${sub === '日志' ? html`<${LogViewer} cid=${cid} initialFile=${logFile}
              initial=${logFile ? '' : (detail && detail.progress || []).join('\n')} />` : null}
          </div>
        </div>
      </div>`;
    }

    // ---------- ⑪ 日志全文查看 ----------
    function LogViewer({ cid, initial, initialFile }) {
      const common = ['PROGRESS.txt', 'RACES.md', 'STATE.txt', 'PLAN.tsv',
        'monitors/ENV-SUMMARY.csv'];
      const [file, setFile] = useState(initialFile || 'PROGRESS.txt');
      const isCustom = !common.includes(file);
      const [n, setN] = useState(300);
      const [q, setQ] = useState('');
      const [text, setText] = useState(initial || '');
      const [total, setTotal] = useState(0);
      const [busy, setBusy] = useState(false);
      async function load() {
        if (!cid) return;
        setBusy(true);
        try {
          const url = '/api/logfile?cid=' + encodeURIComponent(cid) + '&file=' +
            encodeURIComponent(file) + '&n=' + n + '&q=' + encodeURIComponent(q);
          const { status, j } = await getJSON(url);
          if (status !== 200) setText('读取失败: ' + (j.error || '未知'));
          else { setText((j.lines || []).join('\n')); setTotal(j.total || 0); }
        } catch (e) { setText('读取失败: ' + e); }
        setBusy(false);
      }
      useEffect(() => { if (cid) load(); }, [cid, file]);
      // 行数档位改动立即重读（跳过挂载首轮 —— [cid,file] 已经拉过）；
      // 关键字过滤刻意保留回车触发，避免输入中途空转
      const firstN = useRef(true);
      useEffect(() => {
        if (firstN.current) { firstN.current = false; return; }
        if (cid && file) load();
      }, [n]);
      // 抽屉里点产物切过来时，跟随目标文件（initialFile 只在点击时变化，改 file 自动触发 load）
      useEffect(() => { if (initialFile) setFile(initialFile); }, [initialFile]);
      function copyAll() {
        navigator.clipboard && navigator.clipboard.writeText(text || '').then(
          () => toast.ok('日志已复制到剪贴板'),
          () => toast.err('复制失败（浏览器拒绝了剪贴板访问）'));
      }
      return html`<div>
        <div class="row">
          <select class="input" value=${isCustom ? '__custom' : file}
            onInput=${(e) => { const v = e.target.value; setFile(v === '__custom' ? (file && file) || '' : v); }}>
            ${common.map((f) => html`<option key=${f} value=${f}>${f}</option>`)}
            <option value="__custom">自定义路径…</option>
          </select>
          ${isCustom && html`<input class="input x-grow" placeholder="相对路径，如 monitors/ENV-SUMMARY.csv" value=${file}
            onInput=${(e) => setFile(e.target.value)}
            onKeyDown=${(e) => { if (e.key === 'Enter') load(); }} />`}
          <select class="input" value=${n} onInput=${(e) => setN(+e.target.value)}
            title="改动行数立即重读（关键字过滤仍需回车）">
            <option value="100">100 行</option><option value="300">300 行</option>
            <option value="1000">1000 行</option><option value="5000">5000 行</option>
          </select>
          <input class="input x-grow" placeholder="关键字过滤…（回车即读）" value=${q}
            onInput=${(e) => setQ(e.target.value)}
            onKeyDown=${(e) => { if (e.key === 'Enter') load(); }} />
          <button class="btn btn-sm btn-primary" onClick=${load} disabled=${busy}>${busy ? '读取中…' : '读取'}</button>
          <button class="btn btn-sm btn-ghost" onClick=${copyAll}
            title="复制当前显示的全部日志文本">复制</button>
          <span class="muted">匹配 ${total} 行</span>
        </div>
        <pre class="mono" style="max-height:420px;overflow:auto;white-space:pre-wrap;font-size:12px;margin-top:8px">${text}</pre>
      </div>`;
    }

    function CloseCard({ cid }) {
      const [dry, setDry] = useState(true);
      const [out, setOut] = useState('');
      const [busy, setBusy] = useState(false);
      const [confirm, setConfirm] = useState(false);
      async function run() {
        setBusy(true);
        try {
          const { status, j } = await postJSON('/api/close', { cid, dry_run: dry });
          if (status !== 200) { setOut('失败: ' + (j.error || '未知')); toast.err('收尾失败'); }
          else { setOut(j.result || '(无输出)'); toast.ok(dry ? '预演完成（未改动）' : '已补结束行'); }
        } catch (e) { setOut(String(e)); }
        setBusy(false); setConfirm(false);
      }
      return html`<div>
        <p class="muted">给没有结束行的 campaign 补一行合法结束行；活跃 runner 会被自动跳过，不碰集群/判定。先预演确认再执行。</p>
        <div class="row">
          <label class="check-item"><input type="checkbox" checked=${dry} onChange=${(e) => setDry(e.target.checked)} /> 预演（dry-run，不改动）</label>
          ${dry ? html`<button class="btn btn-secondary btn-sm" onClick=${run} disabled=${busy}>${busy ? '运行中…' : '预演'}</button>`
            : !confirm ? html`<button class="btn btn-ghost btn-sm" onClick=${() => setConfirm(true)}>执行收尾…</button>`
            : html`<button class="btn btn-danger btn-sm" onClick=${run} disabled=${busy}>确认执行收尾</button>`}
        </div>
        ${out ? html`<pre class="mono" style="max-height:300px;overflow:auto;white-space:pre-wrap;font-size:12px;margin-top:8px">${out}</pre>` : null}
      </div>`;
    }

    // ---------- ⑬ 编排预设（localStorage 命名预设 + 导入导出） ----------
    const PRESET_KEY = 'raceway.presets';
    function readPresets() {
      try { return JSON.parse(localStorage.getItem(PRESET_KEY) || '{}'); }
      catch (e) { return {}; }
    }
    function writePresets(p) {
      try { localStorage.setItem(PRESET_KEY, JSON.stringify(p)); } catch (e) {}
    }
    function formSnapshot(form) {
      return {
        cars: form.cars, tracks: form.tracks, custom: form.custom,
        rounds: form.rounds, recall: form.recall, est: form.est,
        ds_hours: form.ds_hours, incov: form.incov, tag: form.tag,
        test_mode: !!form.test_mode,
      };
    }
    function PresetBar({ form, setForm }) {
      const [presets, setPresets] = useState(readPresets());
      const [name, setName] = useState('');
      function save() {
        const n = name.trim();
        if (!n) { toast.err('先填预设名'); return; }
        const p = Object.assign({}, presets); p[n] = formSnapshot(form);
        writePresets(p); setPresets(p); setName(''); toast.ok('已保存预设: ' + n);
      }
      function apply(n) {
        setForm((f) => Object.assign({}, f, presets[n]));
        toast.ok('已套用预设: ' + n);
      }
      function del(n) {
        const p = Object.assign({}, presets); delete p[n];
        writePresets(p); setPresets(p); toast.ok('已删除预设: ' + n);
      }
      function exportAll() {
        const text = JSON.stringify(presets, null, 2);
        const blob = new Blob([text], { type: 'application/json' });
        const url = URL.createObjectURL(blob); const a = document.createElement('a');
        a.href = url; a.download = 'raceway-presets.json'; document.body.appendChild(a);
        a.click(); setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 100);
      }
      function importAll(file) {
        if (!file) return;
        const r = new FileReader();
        r.onload = () => {
          try {
            const p = JSON.parse(r.result);
            if (!p || typeof p !== 'object') throw new Error('格式不对');
            const merged = Object.assign({}, presets, p);
            writePresets(merged); setPresets(merged); toast.ok('已导入 ' + Object.keys(p).length + ' 个预设');
          } catch (e) { toast.err('导入失败: ' + e); }
        };
        r.readAsText(file);
      }
      const names = Object.keys(presets).sort();
      return html`<div class="row" style="margin-bottom:10px">
        <span class="muted">编排预设</span>
        <input class="input" placeholder="预设名" value=${name} onInput=${(e) => setName(e.target.value)} style="max-width:140px" />
        <button class="btn btn-sm btn-ghost" onClick=${save}>保存当前</button>
        ${names.map((n) => html`<span key=${n} class="check-item" style="padding:2px 6px">
          <button class="btn btn-sm btn-secondary" onClick=${() => apply(n)}>${n}</button>
          <button class="btn btn-sm btn-ghost" onClick=${() => del(n)} title="删除">✕</button>
        </span>`)}
        <button class="btn btn-sm btn-ghost" onClick=${exportAll}>导出 JSON</button>
        <label class="btn btn-sm btn-ghost" style="cursor:pointer">导入 JSON
          <input type="file" accept="application/json,.json" style="display:none"
            onChange=${(e) => importAll(e.target.files[0])} />
        </label>
      </div>`;
    }

    // ---------- ⑮ 深链路由（hash） ----------
    function parseHash() {
      const m = /^#\/([a-z]+)(?:\/([^?]+))?/.exec(location.hash || '');
      return m ? { tab: m[1], cid: m[2] ? decodeURIComponent(m[2]) : null } : null;
    }
    function installHashRoute(getCid, apply) {
      function onHash() {
        const h = parseHash();
        if (h && h.tab) apply(h.tab, h.cid);
      }
      window.addEventListener('hashchange', onHash);
      onHash();
      return function go(tab, cid) {
        const next = '#/' + tab + (cid ? '/' + encodeURIComponent(cid) : '');
        if (location.hash !== next) {
          history.replaceState(null, '', next);
        }
      };
    }

    window.RacewayExtras = {
      setup,
      TrackFilesView, ReportView, ExportGenView,
      SidecarCard, RaceDetailDrawer, MonitorChart, LogViewer, CloseCard, PresetBar,
      DownloadBtn, OutBox, kvRows,
      readPresets, writePresets, formSnapshot, parseHash, installHashRoute,
    };
    return window.RacewayExtras;
  }
  window.RacewayExtras = { setup };
})();
