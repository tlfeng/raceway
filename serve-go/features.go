package main

// 新功能原生实现：读操作本地 os / ssh exec；写操作 exec bench-ops.py。
// 设计原则：所有路径先白名单校验再拼；ssh 模式下路径仍是远端 POSIX 路径。

import (
	"bytes"
	"encoding/csv"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

var (
	fidRe  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$`)
	// 赛道/日志相对路径：允许 / 作目录分隔，首段以字母数字开头，每段合法；
	// .. 与绝对路径由各 handler 另外显式拒绝。
	relPathRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$`)
	cidReF    = cidRe // 复用 main.go 的 campaign 白名单
)

func q1(q url.Values, key string) string {
	if v, ok := q[key]; ok && len(v) > 0 {
		return v[0]
	}
	return ""
}

func strKV(k, v string) map[string]any { return map[string]any{k: v} }

// sendErrJSON 统一错误体。
func sendErrJSON(w http.ResponseWriter, code int, msg string) {
	sendJSON(w, code, map[string]any{"error": msg})
}

// ---------- 文件读取：本地 os / ssh cat ----------

// runCmd 执行本地或远端命令，stdin 可注入文本，统一带超时。
func (s *server) runCmd(cmdStr, stdin string, timeout time.Duration) (string, error) {
	if s.ssh == "" {
		cmd := exec.Command("/bin/bash", "-c", cmdStr)
		if stdin != "" {
			cmd.Stdin = strings.NewReader(stdin)
		}
		out, err := cmd.CombinedOutput()
		return string(out), err
	}
	args := []string{"ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"}
	args = append(args, strings.Fields(s.ssh)...)
	args = append(args, "bash", "-lc", cmdStr)
	cmd := exec.Command(args[0], args[1:]...)
	if stdin != "" {
		cmd.Stdin = strings.NewReader(stdin)
	}
	out, err := cmd.CombinedOutput()
	return string(out), err
}

// readFile 读取仓库内绝对路径的内容（本地 os.ReadFile / ssh cat）。
// 返回 ("", false) 表示文件不存在或不可读。
func (s *server) readFile(abs string) (string, bool) {
	if s.ssh == "" {
		b, err := os.ReadFile(abs)
		if err != nil {
			return "", false
		}
		return string(b), true
	}
	quoted := "'" + strings.ReplaceAll(abs, "'", `'\''`) + "'"
	cmd := "cat " + quoted + " 2>/dev/null"
	out, err := s.runCmd(cmd, "", 30*time.Second)
	if err != nil {
		return "", false
	}
	return out, true
}

// campPath 拼 campaign 内路径（cid 必须已通过 cidRe 校验）。
func (s *server) campPath(cid, rel string) string {
	return path.Join(s.resultsRoot, cid, rel)
}

// repoPath 拼仓库内路径。
func (s *server) repoPath(rel string) string {
	return path.Join(s.repoRoot, rel)
}

// ---------- 写操作：exec bench-ops.py ----------

// ops 调用 bench-ops.py 的一个 mode，payload 经 stdin 传 JSON。
func (s *server) ops(mode string, payload map[string]any, timeout time.Duration) (map[string]any, error) {
	if payload == nil {
		payload = map[string]any{}
	}
	input, _ := json.Marshal(payload)
	script := s.repoPath("bench-ops.py")
	quoted := "'" + strings.ReplaceAll(script, "'", `'\''`) + "'"
	cmd := "cd " + "'" + strings.ReplaceAll(s.repoRoot, "'", `'\''`) + "'" +
		" && python3 -B " + quoted + " " + mode
	out, err := s.runCmd(cmd, string(input), timeout)
	if err != nil {
		return nil, fmt.Errorf("执行 %s 失败: %v: %s", mode, err, tail(out, 400))
	}
	var reply map[string]any
	if e := json.Unmarshal([]byte(strings.TrimSpace(out)), &reply); e != nil {
		return nil, fmt.Errorf("%s 响应非 JSON: %s", mode, tail(out, 400))
	}
	if msg, ok := reply["error"]; ok {
		return nil, fmt.Errorf("%v", msg)
	}
	return reply, nil
}

func tail(s string, n int) string {
	if len(s) > n {
		return s[len(s)-n:]
	}
	return s
}

// ---------- 赛道文件 ----------

// GET /api/track-files —— 列 tracks/ 文件树。
func (s *server) nativeTrackFiles(w http.ResponseWriter) bool {
	reply, err := s.ops("track-list", nil, 60*time.Second)
	if err != nil {
		sendErrJSON(w, 500, err.Error())
		return true
	}
	sendJSON(w, 200, reply)
	return true
}

// GET /api/track-file?path= —— 读单个赛道文件。
func (s *server) nativeTrackFile(w http.ResponseWriter, q url.Values) bool {
	rel := q1(q, "path")
	if !relPathRe.MatchString(rel) || strings.Contains(rel, "..") {
		sendErrJSON(w, 400, "非法赛道路径")
		return true
	}
	reply, err := s.ops("track-read", map[string]any{"path": rel}, 60*time.Second)
	if err != nil {
		sendErrJSON(w, 500, err.Error())
		return true
	}
	sendJSON(w, 200, reply)
	return true
}

// POST /api/track-write|track-delete —— 由 _post_core 分发的旁路，见 features_post.go。

// ---------- 实测身份卡 ----------

// GET /api/sidecar?cid=&fid=
func (s *server) nativeSidecar(w http.ResponseWriter, q url.Values) bool {
	cid, fid := q1(q, "cid"), q1(q, "fid")
	if !cidReF.MatchString(cid) || !fidRe.MatchString(fid) {
		sendErrJSON(w, 400, "非法 campaign 或场次 id")
		return true
	}
	sidecars := []any{}
	for _, ph := range []string{"wq", "w", "q", "m"} {
		name := fmt.Sprintf("%s-%s-sidecar.json", fid, ph)
		txt, ok := s.readFile(s.campPath(cid, path.Join("results", name)))
		if !ok || strings.TrimSpace(txt) == "" {
			continue
		}
		var doc any
		if e := json.Unmarshal([]byte(txt), &doc); e != nil {
			doc = map[string]any{"_parse_error": tail(txt, 200)}
		}
		sidecars = append(sidecars, map[string]any{"phase": ph, "file": name, "data": doc})
	}
	sendJSON(w, 200, map[string]any{"cid": cid, "fid": fid, "sidecars": sidecars})
	return true
}

// ---------- 场次详情抽屉 ----------

// GET /api/race-detail?cid=&fid=
func (s *server) nativeRaceDetail(w http.ResponseWriter, q url.Values) bool {
	cid, fid := q1(q, "cid"), q1(q, "fid")
	if !cidReF.MatchString(cid) || !fidRe.MatchString(fid) {
		sendErrJSON(w, 400, "非法 campaign 或场次 id")
		return true
	}
	var planRow []string
	if plan, ok := s.readFile(s.campPath(cid, "PLAN.tsv")); ok {
		for _, line := range strings.Split(plan, "\n") {
			cols := strings.Split(line, "\t")
			if len(cols) > 0 && cols[0] == fid {
				planRow = cols
				break
			}
		}
	}
	raceRows := []string{}
	if races, ok := s.readFile(s.campPath(cid, "RACES.md")); ok {
		for _, ln := range strings.Split(races, "\n") {
			if strings.HasPrefix(ln, "| "+fid+" ") {
				raceRows = append(raceRows, ln)
			}
		}
	}
	progLines := []string{}
	if prog, ok := s.readFile(s.campPath(cid, "PROGRESS.txt")); ok {
		for _, ln := range strings.Split(prog, "\n") {
			if strings.Contains(ln, fid) {
				progLines = append(progLines, ln)
			}
		}
		if len(progLines) > 40 {
			progLines = progLines[len(progLines)-40:]
		}
	}
	metrics := []any{}
	for _, ph := range []string{"wq", "w", "q"} {
		name := fmt.Sprintf("%s-%s-race.json", fid, ph)
		txt, ok := s.readFile(s.campPath(cid, path.Join("results", name)))
		if !ok {
			continue
		}
		var race map[string]any
		if e := json.Unmarshal([]byte(txt), &race); e != nil {
			continue
		}
		// 新版 rally 的指标在 results.op_metrics（顶层是空的）；兼容旧布局
		opsList := []any{}
		if res, ok := race["results"].(map[string]any); ok {
			opsList, _ = res["op_metrics"].([]any)
		}
		if len(opsList) == 0 {
			opsList, _ = race["op_metrics"].([]any)
		}
		for _, raw := range opsList {
			op, _ := raw.(map[string]any)
			if op == nil {
				continue
			}
			task := op["task"]
			if task == nil {
				task = op["operation"]
			}
			if task == nil {
				task = op["name"]
			}
			metrics = append(metrics, map[string]any{
				"phase": ph, "task": task,
				"throughput": metricMedian(op["throughput"]),
				"tput_unit":  metricUnit(op["throughput"]),
				"lat_p50":    metricPct(op["latency"], "50_0"),
				"lat_p99":    metricPct(op["latency"], "99_0"),
				"lat_unit":   metricUnit(op["latency"]),
			})
		}
	}
	artifacts := []string{}
	for _, name := range []string{
		fmt.Sprintf("%s-wq.md", fid), fmt.Sprintf("%s-wq-race.json", fid),
		fmt.Sprintf("%s-wq-sidecar.json", fid), fmt.Sprintf("%s-expected-tasks.json", fid),
	} {
		if _, ok := s.readFile(s.campPath(cid, path.Join("results", name))); ok {
			artifacts = append(artifacts, name)
		}
	}
	sendJSON(w, 200, map[string]any{
		"cid": cid, "fid": fid, "plan": planRow,
		"races": raceRows, "progress": progLines,
		"metrics": metrics, "artifacts": artifacts,
	})
	return true
}

func metricMean(v any) any {
	m, ok := v.(map[string]any)
	if !ok {
		return v
	}
	if x, ok := m["mean"]; ok {
		return x
	}
	return nil
}

// metricMedian：rally 吞吐取 median（缺失回 mean）
func metricMedian(v any) any {
	m, ok := v.(map[string]any)
	if !ok {
		return nil
	}
	if x, ok := m["median"]; ok {
		return x
	}
	return m["mean"]
}

// metricPct：rally 百分位键是 50_0 / 99_0 这类格式
func metricPct(v any, key string) any {
	m, ok := v.(map[string]any)
	if !ok {
		return nil
	}
	return m[key]
}

func metricUnit(v any) any {
	m, ok := v.(map[string]any)
	if !ok {
		return nil
	}
	return m["unit"]
}

// ---------- 每节点 5 秒监控时序 ----------

// GET /api/monitor?cid=&fid=
func (s *server) nativeMonitor(w http.ResponseWriter, q url.Values) bool {
	cid, fid := q1(q, "cid"), q1(q, "fid")
	if !cidReF.MatchString(cid) || !fidRe.MatchString(fid) {
		sendErrJSON(w, 400, "非法 campaign 或场次 id")
		return true
	}
	// load-monitor 按相位分 tag 采集：文件名是 <fid>-<相位>-<node>.csv（如 -wq-），
	// (相位, 节点) 清单来自 ENV-SUMMARY.csv。兼容旧布局 <fid>-<node>.csv；
	// 同一节点跨相位的多份 CSV 按行拼接成一条曲线。
	type triple struct{ phase, node string }
	triples := []triple{}
	if env, ok := s.readFile(s.campPath(cid, path.Join("monitors", "ENV-SUMMARY.csv"))); ok {
		if rows, e := csv.NewReader(strings.NewReader(env)).ReadAll(); e == nil && len(rows) > 1 {
			for _, r := range rows[1:] {
				cols := make([]string, len(r))
				for i, v := range r {
					cols[i] = strings.TrimSpace(v)
				}
				if len(cols) >= 3 && cols[0] == fid && cols[2] != "" {
					t := triple{phase: cols[1], node: cols[2]}
					found := false
					for _, x := range triples {
						if x == t {
							found = true
							break
						}
					}
					if !found {
						triples = append(triples, t)
					}
				}
			}
		}
	}
	if len(triples) == 0 {
		for _, n := range []string{"node1", "node2", "esrally"} {
			triples = append(triples, triple{node: n})
		}
	}
	series := []any{}
	byNode := map[string]int{} // node → series 下标
	for _, t := range triples {
		names := []string{}
		if t.phase != "" {
			names = append(names, fmt.Sprintf("%s-%s-%s.csv", fid, t.phase, t.node))
		}
		names = append(names, fmt.Sprintf("%s-%s.csv", fid, t.node)) // 旧布局兜底
		for _, name := range names {
			txt, ok := s.readFile(s.campPath(cid, path.Join("monitors", name)))
			if !ok {
				continue
			}
			rows, e := csv.NewReader(strings.NewReader(txt)).ReadAll()
			if e != nil || len(rows) < 2 {
				continue
			}
			header := rows[0]
			data := []any{}
			for _, r := range rows[1:] {
				if len(r) != len(header) {
					continue
				}
				row := map[string]any{}
				for i, k := range header {
					if f, e := strconv.ParseFloat(strings.TrimSuffix(r[i], "%"), 64); e == nil {
						row[k] = f
					} else {
						row[k] = r[i]
					}
				}
				data = append(data, row)
				if len(data) >= 5000 {
					break
				}
			}
			if idx, seen := byNode[t.node]; seen {
				// 同节点跨相位：拼行
				cur := series[idx].(map[string]any)
				cur["rows"] = append(cur["rows"].([]any), data...)
			} else {
				byNode[t.node] = len(series)
				series = append(series, map[string]any{
					"node": t.node, "file": name, "header": header, "rows": data,
				})
			}
			break // 一个 (相位, 节点) 命中一份文件即可
		}
	}
	sendJSON(w, 200, map[string]any{
		"cid": cid, "fid": fid, "series": series, "interval": 5,
	})
	return true
}

// ---------- 日志全文查看 ----------

// GET /api/logfile?cid=&file=&n=&q=
func (s *server) nativeLogfile(w http.ResponseWriter, q url.Values) bool {
	cid, rel := q1(q, "cid"), q1(q, "file")
	if !cidReF.MatchString(cid) {
		sendErrJSON(w, 400, "非法 campaign id")
		return true
	}
	if !relPathRe.MatchString(rel) || strings.Contains(rel, "..") {
		sendErrJSON(w, 400, "非法文件路径")
		return true
	}
	ext := path.Ext(rel)
	switch ext {
	case ".txt", ".md", ".json", ".csv", ".log", ".tsv", ".raw":
	default:
		sendErrJSON(w, 400, "只允许查看文本/日志/JSON/CSV 文件")
		return true
	}
	n := 200
	if v := q1(q, "n"); v != "" {
		if parsed, e := strconv.Atoi(v); e == nil && parsed >= 1 && parsed <= 5000 {
			n = parsed
		}
	}
	filter := strings.ToLower(q1(q, "q"))
	txt, ok := s.readFile(s.campPath(cid, rel))
	if !ok {
		sendErrJSON(w, 404, "文件不存在或不可读")
		return true
	}
	lines := strings.Split(strings.TrimRight(txt, "\n"), "\n")
	if filter != "" {
		kept := []string{}
		for _, ln := range lines {
			if strings.Contains(strings.ToLower(ln), filter) {
				kept = append(kept, ln)
			}
		}
		lines = kept
	}
	total := len(lines)
	truncated := total > n
	if truncated {
		lines = lines[total-n:]
	}
	sendJSON(w, 200, map[string]any{
		"cid": cid, "file": rel, "total": total, "lines": lines, "truncated": truncated,
	})
	return true
}

// ---------- est 自动校准 ----------

// GET /api/est-suggest?track=
func (s *server) nativeEstSuggest(w http.ResponseWriter, q url.Values) bool {
	track := q1(q, "track")
	if track == "" || !relPathRe.MatchString(track) {
		sendErrJSON(w, 400, "请指定合法赛道")
		return true
	}
	var vals []float64
	// 列出 campaign 目录
	listCmd := "ls -1 " + "'" + strings.ReplaceAll(s.resultsRoot, "'", `'\''`) + "' 2>/dev/null"
	if s.ssh != "" {
		args := []string{"ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"}
		args = append(args, strings.Fields(s.ssh)...)
		args = append(args, "bash", "-lc", listCmd)
		cmd := exec.Command(args[0], args[1:]...)
		out, e := cmd.CombinedOutput()
		if e == nil {
			vals = s.collectEst(string(out), track)
		}
	} else {
		entries, e := os.ReadDir(s.resultsRoot)
		if e == nil {
			names := []string{}
			for _, en := range entries {
				names = append(names, en.Name())
			}
			vals = s.collectEst(strings.Join(names, "\n"), track)
		}
	}
	sort.Float64s(vals)
	if len(vals) == 0 {
		sendJSON(w, 200, map[string]any{"track": track, "available": false, "n": 0, "suggest": nil})
		return true
	}
	mid := vals[len(vals)/2]
	if len(vals)%2 == 0 {
		mid = (vals[len(vals)/2-1] + vals[len(vals)/2]) / 2
	}
	sug := int(mid + 0.5)
	if sug < 1 {
		sug = 1
	}
	sendJSON(w, 200, map[string]any{
		"track": track, "available": true, "n": len(vals), "suggest": sug,
		"min": vals[0], "max": vals[len(vals)-1],
	})
	return true
}

var durRe = regexp.MustCompile(`^(\d+(?:\.\d+)?)\s*(min|s)$`)

func (s *server) collectEst(listing, track string) []float64 {
	vals := []float64{}
	for _, cid := range strings.Split(listing, "\n") {
		cid = strings.TrimSpace(cid)
		if !cidReF.MatchString(cid) {
			continue
		}
		plan, ok := s.readFile(s.campPath(cid, "PLAN.tsv"))
		if !ok {
			continue
		}
		fids := map[string]bool{}
		for _, line := range strings.Split(plan, "\n") {
			cols := strings.Split(line, "\t")
			if len(cols) > 2 && cols[2] == track {
				fids[cols[0]] = true
			}
		}
		if len(fids) == 0 {
			continue
		}
		races, ok := s.readFile(s.campPath(cid, "RACES.md"))
		if !ok {
			continue
		}
		for _, ln := range strings.Split(races, "\n") {
			if !strings.HasPrefix(ln, "| ") {
				continue
			}
			cols := []string{}
			for _, c := range strings.Split(strings.Trim(ln, "|"), "|") {
				cols = append(cols, strings.TrimSpace(c))
			}
			if len(cols) < 4 || !fids[cols[0]] {
				continue
			}
			if !strings.Contains(cols[1], "VALID") && !strings.Contains(cols[1], "DEGRADED") {
				continue
			}
			m := durRe.FindStringSubmatch(cols[3])
			if m == nil {
				continue
			}
			v, _ := strconv.ParseFloat(m[1], 64)
			if m[2] == "s" {
				v /= 60
			}
			if v > 0 && v < 1440 {
				vals = append(vals, v)
			}
		}
	}
	return vals
}

// ---------- 报告导出 ----------

// GET /api/report?cid=&fmt=md|html
func (s *server) nativeReport(w http.ResponseWriter, q url.Values) bool {
	cid, fmtType := q1(q, "cid"), q1(q, "fmt")
	if !cidReF.MatchString(cid) {
		sendErrJSON(w, 400, "非法 campaign id")
		return true
	}
	if fmtType == "" {
		fmtType = "md"
	}
	if fmtType != "md" && fmtType != "html" {
		sendErrJSON(w, 400, "格式只能是 md 或 html")
		return true
	}
	lines := []string{"# raceway 跑分报告: " + cid, ""}
	plan, _ := s.readFile(s.campPath(cid, "PLAN.tsv"))
	n := 0
	for range strings.Split(strings.TrimSpace(plan), "\n") {
		n++
	}
	if n > 0 && n > 1 {
		n--
	}
	lines = append(lines, fmt.Sprintf("- 计划场次: %d", n), "")
	lines = append(lines, "## 逐场判定", "", "| 场次 | 状态 | 卡点 | ENV | 用时 |", "|---|---|---|---|---|")
	envMap := map[string][2]string{}
	if env, ok := s.readFile(s.campPath(cid, path.Join("monitors", "ENV-SUMMARY.csv"))); ok {
		if rows, e := csv.NewReader(strings.NewReader(env)).ReadAll(); e == nil {
			for _, r := range rows[1:] {
				if len(r) >= 11 {
					fid := strings.TrimSpace(r[0])
					prev := envMap[fid]
					verdict := strings.TrimSpace(r[10])
					steal := strings.TrimSpace(r[6])
					if verdictRank(verdict) > verdictRank(prev[0]) {
						prev[0] = verdict
					}
					if f, e := strconv.ParseFloat(steal, 64); e == nil {
						cur, _ := strconv.ParseFloat(prev[1], 64)
						if f > cur {
							prev[1] = steal
						}
					}
					envMap[fid] = prev
				}
			}
		}
	}
	if races, ok := s.readFile(s.campPath(cid, "RACES.md")); ok {
		for _, ln := range strings.Split(races, "\n") {
			if !strings.HasPrefix(ln, "| ") {
				continue
			}
			cols := []string{}
			for _, c := range strings.Split(strings.Trim(ln, "|"), "|") {
				cols = append(cols, strings.TrimSpace(c))
			}
			if len(cols) < 4 || cols[0] == "场次" || strings.HasPrefix(cols[0], "-") {
				continue
			}
			env := envMap[cols[0]]
			lines = append(lines, fmt.Sprintf("| %s | %s | %s | %s | %s |",
				cols[0], cols[1], cols[2], orDash(env[0]), cols[3]))
		}
	}
	lines = append(lines, "", "## 环境采样（最坏 ENV / steal 峰值）", "",
		"| 场次 | ENV | steal_max |", "|---|---|---|")
	fids := []string{}
	for f := range envMap {
		fids = append(fids, f)
	}
	sort.Strings(fids)
	for _, f := range fids {
		lines = append(lines, fmt.Sprintf("| %s | %s | %s |", f, orDash(envMap[f][0]), orDash(envMap[f][1])))
	}
	lines = append(lines, "", "---", "", "由 raceway Go 控制台生成；口径见 docs/METHODOLOGY.md。")
	md := strings.Join(lines, "\n")
	if fmtType == "html" {
		md = "<!doctype html><html><head><meta charset=\"utf-8\"><title>raceway " + cid +
			"</title><style>body{font:14px/1.6 -apple-system,Segoe UI,sans-serif;max-width:960px;" +
			"margin:40px auto;color:#222}pre{white-space:pre-wrap}</style></head><body><pre>" +
			htmlEscape(md) + "</pre></body></html>"
	}
	sendJSON(w, 200, map[string]any{"cid": cid, "format": fmtType, "text": md})
	return true
}

func verdictRank(v string) int {
	switch v {
	case "CONTAMINATED":
		return 3
	case "WARN":
		return 2
	case "OK":
		return 1
	case "NO-DATA":
		return 0
	}
	return -1
}

func orDash(v string) string {
	if v == "" {
		return "-"
	}
	return v
}

func htmlEscape(s string) string {
	r := strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", "\"", "&quot;")
	return r.Replace(s)
}

// ---------- 路由分发 ----------

// nativeGetFeature 处理新增的 GET 功能端点；返回 false 表示不在本 handler 职责内。
func (s *server) nativeGetFeature(w http.ResponseWriter, path string, q url.Values) bool {
	// 初始化 resultsRoot / repoRoot / ssh（惰性从 bootstrap 拉取一次）。
	s.bootstrapCache()
	switch path {
	case "/api/track-files":
		return s.nativeTrackFiles(w)
	case "/api/track-file":
		return s.nativeTrackFile(w, q)
	case "/api/sidecar":
		return s.nativeSidecar(w, q)
	case "/api/race-detail":
		return s.nativeRaceDetail(w, q)
	case "/api/monitor":
		return s.nativeMonitor(w, q)
	case "/api/logfile":
		return s.nativeLogfile(w, q)
	case "/api/est-suggest":
		return s.nativeEstSuggest(w, q)
	case "/api/report":
		return s.nativeReport(w, q)
	}
	return false
}

// ensure imports referenced (bytes/io kept for potential streaming; avoid unused)
var _ = bytes.MinRead
var _ = io.EOF
