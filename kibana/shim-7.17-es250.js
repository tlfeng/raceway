// ============================================================================
// shim-7.17-es250.js —— Kibana 7.17 → EasySearch 2.5.0 的协议适配层
//
// 用途：Kibana 7.17 的 ES 客户端对服务端做产品校验（version/tagline/build_flavor
// + X-elastic-product 响应头），EasySearch 2.5.0 默认不满足 → Kibana 直接崩溃。
// 本代理既补齐产品校验，也兜住 EasySearch 与 ES 7.17 的 12 处 API 差异。
//
// 与原 kibana717-es250-shim.js 的差别：
//   1) 上游地址/端口/协议/账号全部走环境变量，不再硬编码某台机器的 9201 与口令
//   2) 新增 GET / 的产品校验改写 —— 服务端 ES 未开 elasticsearch.api_compatibility
//      时（version.number=2.5.0、tagline=You Know, For Easy Search!、无
//      X-elastic-product 头），由代理在响应侧改写，不必重启 ES 去改配置
//
// 用法：
//   TARGET_PORT=9237 TARGET_USER=admin TARGET_PASS=*** \
//   node shim-7.17-es250.js 9202 /tmp/es_shim.log
// ============================================================================
const http = require('http');
const https = require('https');
const fs = require('fs');

const TARGET_HOST = process.env.TARGET_HOST || '127.0.0.1';
const TARGET_PORT = Number(process.env.TARGET_PORT || 9201);
const TARGET_SCHEME = process.env.TARGET_SCHEME || 'https';
const LISTEN_PORT = Number(process.argv[2] || 9202);
const LOG_FILE = process.argv[3] || '/tmp/es_shim.log';
const FIX_NODES_INFO = process.env.FIX_NODES_INFO === '1';
const FIX_MAPPINGS = process.env.FIX_MAPPINGS === '1';
// 伪装成 ES 7.17 对外的版本号（Kibana 客户端只认 >=6.7 且 major==6/7 的 ES）
const FAKE_VERSION = process.env.FAKE_ES_VERSION || '7.17.29';
const AUTH = 'Basic ' + Buffer.from(
  `${process.env.TARGET_USER || 'admin'}:${process.env.TARGET_PASS || ''}`
).toString('base64');

const agent = TARGET_SCHEME === 'https'
  ? https
  : http;

// EasySearch 2.5.0 rejects ES-standard params on some field types.
const UNSUPPORTED = {
  flattened: ['ignore_above', 'depth_limit'],
  text: ['ignore_above'],
  match_only_text: ['ignore_above'],
};

function stripUnsupported(mappings) {
  if (!mappings || typeof mappings !== 'object') return 0;
  let n = 0;
  const visit = (props) => {
    if (!props || typeof props !== 'object') return;
    for (const [name, def] of Object.entries(props)) {
      if (!def || typeof def !== 'object') continue;
      const t = def.type;
      if (t && UNSUPPORTED[t]) {
        for (const p of UNSUPPORTED[t]) {
          if (p in def) { delete def[p]; n++; }
        }
      }
      if (def.properties) visit(def.properties);
      if (def.fields && typeof def.fields === 'object') {
        for (const [fname, fdef] of Object.entries(def.fields)) {
          if (fdef && fdef.type && UNSUPPORTED[fdef.type]) {
            for (const p of UNSUPPORTED[fdef.type]) {
              if (p in fdef) { delete fdef[p]; n++; }
            }
          }
        }
      }
    }
  };
  if (mappings.properties && typeof mappings.properties === 'object') {
    visit(mappings.properties);
  }
  for (const docType of Object.values(mappings)) {
    if (docType && typeof docType === 'object' && docType.properties) visit(docType.properties);
  }
  return n;
}

const log = (obj) => fs.appendFileSync(LOG_FILE, JSON.stringify(obj) + '\n');

function upstreamRequest(options, body) {
  return new Promise((resolve) => {
    const r = agent.request(options, (res) => {
      const cs = [];
      res.on('data', (c) => cs.push(c));
      res.on('end', () => {
        try {
          resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(cs).toString('utf8') });
        } catch (e) { resolve(null); }
      });
    });
    r.on('error', () => resolve(null));
    if (body) r.write(body);
    r.end();
  });
}

async function fetchNodes() {
  const r = await upstreamRequest({
    host: TARGET_HOST, port: TARGET_PORT, path: '/_nodes', method: 'GET',
    rejectUnauthorized: false, headers: { authorization: AUTH },
  });
  if (!r) return null;
  try {
    const j = JSON.parse(r.body);
    const nodes = {};
    for (const [id, n] of Object.entries(j.nodes || {})) {
      // 这个回填是给 GET /_nodes/info 用的 —— 该端点在 ES 7.x 里 version 是字符串
      nodes[id] = {
        name: n.name, transport_address: n.transport_address, host: n.host, ip: n.ip,
        version: FAKE_VERSION, build_flavor: n.build_flavor || 'default', build_type: n.build_type,
        http: n.http, roles: n.roles, attributes: n.attributes, settings: n.settings,
      };
    }
    return { nodes };
  } catch (e) { return null; }
}

async function fetchLicense() {
  const r = await upstreamRequest({
    host: TARGET_HOST, port: TARGET_PORT, path: '/_license', method: 'GET',
    rejectUnauthorized: false, headers: { authorization: AUTH },
  });
  if (!r) return null;
  try {
    const j = JSON.parse(r.body);
    const lic = j.license || {};
    return {
      license: {
        ...lic,
        mode: lic.mode || 'basic',
        expiry_date_in_millis: lic.expiry_date_in_millis || '4102444800000',
        type: lic.type || 'basic',
        status: lic.status || 'active',
      },
      features: {},
    };
  } catch (e) { return null; }
}

// Request-side compatibility fixes for ES 7.17 -> EasySearch 2.5.0
function fixRequestBody(method, url, bodyStr) {
  if (!bodyStr) return null;
  let parsed;
  try { parsed = JSON.parse(bodyStr); } catch (e) { return null; }
  let changes = 0;

  if (method === 'DELETE' && /^\/_pit(\?|$)/.test(url) && parsed.id && !parsed.pit_id) {
    parsed.pit_id = [parsed.id];
    delete parsed.id;
    changes++;
  }

  for (const key of ['docvalue_fields', 'fields']) {
    if (parsed[key] && Array.isArray(parsed[key])) {
      for (const f of parsed[key]) {
        if (f && typeof f === 'object' && 'include_unmapped' in f) {
          delete f.include_unmapped;
          changes++;
        }
      }
    }
  }

  if (parsed.sort) {
    const rewrite = (s) => {
      if (!s) return;
      if (Array.isArray(s)) { s.forEach(rewrite); return; }
      if (typeof s === 'object') {
        if ('_shard_doc' in s) { s._id = s._shard_doc; delete s._shard_doc; changes++; return; }
        for (const v of Object.values(s)) rewrite(v);
      }
    };
    rewrite(parsed.sort);
  }

  if (parsed.runtime_mappings && typeof parsed.runtime_mappings === 'object') {
    delete parsed.runtime_mappings;
    changes++;
  }

  if (/\/(_search|_async_search|_count|_msearch|_field_caps)/.test(url)) {
    const stripNulls = (o) => {
      if (!o || typeof o !== 'object') return 0;
      let n = 0;
      if (Array.isArray(o)) {
        for (const item of o) n += stripNulls(item);
        return n;
      }
      for (const [k, v] of Object.entries(o)) {
        if (v === null) { delete o[k]; n++; }
        else n += stripNulls(v);
      }
      return n;
    };
    changes += stripNulls(parsed);
  }

  if (changes === 0) return null;
  return Buffer.from(JSON.stringify(parsed));
}

// 产品校验改写：让 GET / 的形态像 ES 7.17
function fakeProductRoot(body) {
  let j;
  try { j = JSON.parse(body); } catch (e) { return null; }
  if (!j || typeof j !== 'object') return null;
  j.version = j.version || {};
  j.version.number = FAKE_VERSION;
  j.version.distribution = 'elasticsearch';
  if (!j.tagline || j.tagline.includes('Easy Search')) j.tagline = 'You Know, for Search';
  j.build_flavor = j.build_flavor || 'default';
  return JSON.stringify(j);
}

// 节点版本归一：Kibana 7.17 的兼容检查（ensure_es_version.js）把 node.version
// 直接喂给 semver.major() —— 必须是**字符串**。给对象会抛
// "Invalid version. Must be a string. Got type object"。
// 注意 EasySearch 两个端点给的形态不一致：
//   GET /_nodes?filter_path=...   → 有 nodes（但 version 是字符串 "2.5.0"）
//   GET /_nodes/info?filter_path=..→ 返回 {}（没有 nodes 键）—— 需要回填
// 所以这里统一：所有 /_nodes* 响应里的 version 一律改成字符串 FAKE_VERSION，
// 且节点集为空时用 GET /_nodes 的结果回填。
const FAKE_VERSION_OBJ = () => ({
  number: FAKE_VERSION,
  distribution: 'elasticsearch',
  build_flavor: 'default',
  build_type: 'tar',
});

function fakeNodesVersion(body) {
  let j;
  try { j = JSON.parse(body); } catch (e) { return null; }
  if (!j || typeof j !== 'object' || !j.nodes || typeof j.nodes !== 'object') return null;
  let n = 0;
  for (const node of Object.values(j.nodes)) {
    if (!node || typeof node !== 'object') continue;
    node.version = FAKE_VERSION;
    n++;
  }
  return n > 0 ? JSON.stringify(j) : null;
}

http.createServer((req, res) => {
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => {
    let raw = Buffer.concat(chunks);
    const reqBody = raw.toString('utf8');
    const headers = { ...req.headers, host: `${TARGET_HOST}:${TARGET_PORT}` };
    if (!headers.authorization) headers.authorization = AUTH;

    let fixedNote = null;

    // Kibana 轮询 GET /_async_search/status/{id}（ES 7.17 端点），
    // EasySearch 2.5.0 只实现了 GET /_async_search/{id}，直通用会 400
    // 「Invalid index name [_async_search]」。这里把路径改写过去，
    // 响应再转回 ES 7.17 的 status 形态（见下面的 transform）。
    const isAsyncStatus = req.method === 'GET' && /^\/_async_search\/status\//.test(req.url);
    if (isAsyncStatus) {
      req.url = req.url.replace('/_async_search/status/', '/_async_search/');
      fixedNote = 'async_status_path_rewrite';
    }

    if (req.url.includes('enable_fields_emulation')) {
      const u = new URL(req.url, 'http://x');
      u.searchParams.delete('enable_fields_emulation');
      req.url = u.pathname + u.search;
      fixedNote = 'stripped_enable_fields_emulation';
    }
    if (raw.length) {
      const patched = fixRequestBody(req.method, req.url, raw.toString('utf8'));
      if (patched) {
        raw = patched;
        headers['content-length'] = String(raw.length);
        fixedNote = 'request_body_fixed';
      }
    }
    if (FIX_MAPPINGS && raw.length) {
      try {
        const parsed = JSON.parse(reqBody);
        let removed = 0;
        if (parsed && parsed.mappings) {
          removed += stripUnsupported(parsed.mappings);
        } else if (parsed && parsed.properties) {
          removed += stripUnsupported(parsed);
        }
        if (removed > 0) {
          raw = Buffer.from(JSON.stringify(parsed));
          headers['content-length'] = String(raw.length);
          fixedNote = `stripped_${removed}_unsupported_mapping_params`;
        }
      } catch (e) { /* not JSON */ }
    }

    const proxyReq = agent.request(
      {
        host: TARGET_HOST, port: TARGET_PORT, method: req.method, path: req.url,
        rejectUnauthorized: false, headers,
      },
      (proxyRes) => {
        const out = [];
        proxyRes.on('data', (c) => out.push(c));
        proxyRes.on('end', () => {
          let outBody = Buffer.concat(out);
          const outHeaders = { ...proxyRes.headers };

          const send = (fixed, statusOverride) => {
            log({
              ts: new Date().toISOString(), method: req.method, url: req.url,
              reqBody: reqBody.slice(0, 60000), status: statusOverride || proxyRes.statusCode,
              resBody: outBody.toString('utf8').slice(0, 3000), fixed: fixed || fixedNote || null,
            });
            // 产品校验头：Kibana 7.17 在 **404 响应**上判产品
            // （repository.js: indexNotFound && !isSupportedEsServer(headers)
            //   → 抛「ES 不可用 503」而不是干净的 404）。
            // EasySearch 从不发 x-elastic-product，shim 之前只在根路径伪造过，
            // 于是 saved object 取不到时一律 503（UI 设置保存失败、页面 503 等）。
            // 这里给**所有**响应补上：Kibana 的产品校验只认这个头的取值。
            outHeaders['x-elastic-product'] = 'Elasticsearch';
            res.writeHead(statusOverride || proxyRes.statusCode, outHeaders);
            res.end(outBody);
          };

          // _async_search/status 的响应形态转换（EasySearch → ES 7.17）
          if (isAsyncStatus) {
            try {
              const j = JSON.parse(outBody.toString('utf8'));
              const inner = j.response || {};
              const status = {
                id: j.id,
                is_partial: false,
                is_running: false,
                is_stored: j.state === 'STORE_RESIDENT' || j.state === 'STORED',
                is_kept_alive: true,
                completion_status: {
                  status: 'succeeded',
                  took: inner.took || 0,
                  num_reduce_phases: 0,
                  num_phases: 1,
                  total_reduce_phases: 0,
                  total_phases: 1,
                },
                response: inner,
              };
              outBody = Buffer.from(JSON.stringify(status));
              outHeaders['content-type'] = 'application/json; charset=UTF-8';
              outHeaders['content-length'] = String(outBody.length);
              delete outHeaders['content-encoding'];
            } catch (e) { /* 不是 JSON 就保持原样 */ }
          }

          const isRoot = req.method === 'GET' && /^\/?(\?|$)/.test(req.url);
          const isNodes = req.method === 'GET' && /^\/_nodes(\/|\?|$)/.test(req.url);
          const isNodesInfo = req.method === 'GET' && /^\/_nodes\/info(\?|$)/.test(req.url);
          const isXpack = req.method === 'GET' && /^\/_xpack(\?|$)/.test(req.url);
          const isOpenPit = req.method === 'POST' && /\/_pit(\?|$)/.test(req.url);
          const isClosePit = req.method === 'DELETE' && /\/_pit(\?|$)/.test(req.url);

          // ① 产品校验：GET / 改写成 ES 7.17 的形态 + 补 X-elastic-product 头
          if (isRoot) {
            const patched = fakeProductRoot(outBody.toString('utf8'));
            if (patched) {
              outBody = Buffer.from(patched);
              outHeaders['content-type'] = 'application/json; charset=UTF-8';
              outHeaders['content-length'] = String(outBody.length);
              outHeaders['x-elastic-product'] = 'Elasticsearch';
              delete outHeaders['content-encoding'];
              send('product_root', 200);
              return;
            }
          }

          if (isClosePit) {
            try {
              const parsed = JSON.parse(outBody.toString('utf8'));
              if (parsed && !('succeeded' in parsed)) {
                parsed.succeeded = true;
                parsed.num_freed = Array.isArray(parsed.pits) ? parsed.pits.length : 1;
                outBody = Buffer.from(JSON.stringify(parsed));
                outHeaders['content-type'] = 'application/json; charset=UTF-8';
                outHeaders['content-length'] = String(outBody.length);
                delete outHeaders['content-encoding'];
                send('pit_close_succeeded', 200);
                return;
              }
            } catch (e) { /* fall through */ }
          }

          if (isOpenPit) {
            try {
              const parsed = JSON.parse(outBody.toString('utf8'));
              if (parsed && parsed.pit_id && !parsed.id) {
                parsed.id = parsed.pit_id;
                outBody = Buffer.from(JSON.stringify(parsed));
                outHeaders['content-type'] = 'application/json; charset=UTF-8';
                outHeaders['content-length'] = String(outBody.length);
                delete outHeaders['content-encoding'];
                send('pit_id_to_id', 200);
                return;
              }
            } catch (e) { /* fall through */ }
          }

          if (isXpack) {
            fetchLicense().then((payload) => {
              if (payload) {
                outBody = Buffer.from(JSON.stringify(payload));
                outHeaders['content-type'] = 'application/json; charset=UTF-8';
                outHeaders['content-length'] = String(outBody.length);
                delete outHeaders['content-encoding'];
                send('xpack_license', 200);
              } else {
                send();
              }
            });
            return;
          }

          if (FIX_NODES_INFO && isNodesInfo) {
            let parsed = null;
            try { parsed = JSON.parse(outBody.toString('utf8')); } catch (e) { parsed = null; }
            if (parsed && parsed.nodes && Object.keys(parsed.nodes).length === 0) {
              fetchNodes().then((fb) => {
                if (fb) {
                  parsed.nodes = fb.nodes;
                  parsed._nodes = { total: Object.keys(fb.nodes).length, successful: Object.keys(fb.nodes).length, failed: 0 };
                  outBody = Buffer.from(JSON.stringify(parsed));
                  outHeaders['content-length'] = String(outBody.length);
                }
                send('nodes_info');
              });
              return;
            }
          }

          // 节点信息处理（所有 /_nodes* 端点）：
          //   ① 节点集为空 / 没有 nodes 键 → 用 GET /_nodes 的结果回填
          //      （EasySearch 对 _nodes/info?filter_path=... 直接返回 {}）
          //   ② 每个节点的 version 改成字符串 —— Kibana 的兼容检查要字符串
          if (isNodes) {
            const rawText = outBody.toString('utf8');
            let parsed = null;
            try { parsed = JSON.parse(rawText); } catch (e) { parsed = null; }
            const noNodes = !parsed || !parsed.nodes || Object.keys(parsed.nodes).length === 0;
            if (noNodes) {
              fetchNodes().then((fb) => {
                // 回填也可能是空的（上游此刻恰好返回空 nodes 或非 JSON）：
                // Kibana 的版本探测拿到空 nodes 会判定「取不到版本信息」并无限重试，
                // 整个 Kibana 卡在 "not ready yet" 起不来。
                // 版本探测不能依赖上游的瞬时状态 —— 兜底合成一个节点。
                const fbNodes = (fb && fb.nodes) || {};
                const haveReal = Object.keys(fbNodes).length > 0;
                const nodes = haveReal ? fbNodes : {
                  'shim-node-0': {
                    name: 'shim-node-0',
                    transport_address: `${TARGET_HOST}:${TARGET_PORT}`,
                    host: TARGET_HOST,
                    ip: TARGET_HOST,
                    version: FAKE_VERSION,
                    build_flavor: 'default',
                    http: { publish_address: `${TARGET_HOST}:${TARGET_PORT}` },
                    roles: ['data', 'master'],
                  },
                };
                const body = {
                  _nodes: { total: Object.keys(nodes).length, successful: Object.keys(nodes).length, failed: 0 },
                  cluster_name: (parsed && parsed.cluster_name) || '',
                  nodes,
                };
                outBody = Buffer.from(JSON.stringify(body));
                outHeaders['content-type'] = 'application/json; charset=UTF-8';
                outHeaders['content-length'] = String(outBody.length);
                delete outHeaders['content-encoding'];
                send(haveReal ? 'nodes_backfill' : 'nodes_synthesized', 200);
              });
              return;
            }
            const patched = fakeNodesVersion(rawText);
            if (patched) {
              outBody = Buffer.from(patched);
              outHeaders['content-type'] = 'application/json; charset=UTF-8';
              outHeaders['content-length'] = String(outBody.length);
              delete outHeaders['content-encoding'];
              send('nodes_version', 200);
              return;
            }
          }
          send();
        });
      }
    );
    proxyReq.on('error', (e) => {
      log({ ts: new Date().toISOString(), method: req.method, url: req.url, error: String(e) });
      res.writeHead(502, {
        'content-type': 'application/json',
        'x-elastic-product': 'Elasticsearch',
      });
      res.end(JSON.stringify({ error: String(e) }));
    });
    if (raw.length) proxyReq.write(raw);
    proxyReq.end();
  });
}).listen(LISTEN_PORT, '127.0.0.1', () => {
  log({ ts: new Date().toISOString(), event: 'proxy_started', port: LISTEN_PORT, fixNodesInfo: FIX_NODES_INFO, target: `${TARGET_SCHEME}://${TARGET_HOST}:${TARGET_PORT}`, fakeVersion: FAKE_VERSION });
  console.log(`shim proxy on 127.0.0.1:${LISTEN_PORT} -> ${TARGET_SCHEME}://${TARGET_HOST}:${TARGET_PORT} (fixNodesInfo=${FIX_NODES_INFO}, fake=${FAKE_VERSION})`);
});
