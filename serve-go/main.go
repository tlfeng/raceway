// raceway 控制台后端（Go 外壳 / strangler 模式）。
//
// Go 只接管 HTTP 层：监听、同源与 CSRF 守卫、安全响应头、静态资源；
// 所有 /api/* 业务逻辑逐请求转发给既有 Python（bench-compose.py serve-rpc），
// 复用与 `serve` 完全相同的路由核心，保证接口契约一致，同时把常驻内存降到 Go 量级。
package main

import (
	"bytes"
	"crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
)

// 静态资源白名单：URL 路径 → 仓库内相对路径 / MIME（与 bench-compose.py 的 _WEB_STATIC 一致）。
var webStatic = map[string]struct{ file, mime string }{
	"/web/style.css":            {"style.css", "text/css; charset=utf-8"},
	"/web/app.js":               {"app.js", "application/javascript; charset=utf-8"},
	"/web/extras.js":            {"extras.js", "application/javascript; charset=utf-8"},
	"/web/vendor/preact.umd.js": {"vendor/preact.umd.js", "application/javascript; charset=utf-8"},
	"/web/vendor/hooks.umd.js":  {"vendor/hooks.umd.js", "application/javascript; charset=utf-8"},
	"/web/vendor/htm.umd.js":    {"vendor/htm.umd.js", "application/javascript; charset=utf-8"},
	"/legacy":                   {"legacy.html", "text/html; charset=utf-8"}, // 旧页面独立入口
}

var csrfRe = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

type server struct {
	repoDir string   // 仓库根：web/、echarts.min.js、bench-compose.py 所在
	pyArgs  []string // 透传给 python serve-rpc 的原始 flag（--root/--ssh/... 全含）
	csrf    string   // 每次启动生成的写操作令牌（前端从 bootstrap 取回）
	allowed []string // Host 白名单：127.0.0.1:port / localhost:port / [::1]:port
	origins []string // Origin 白名单：http://<allowed>
	port    int

	// 只读混合档：启动后从 python bootstrap 惰性拉一次并缓存，用于把本地
	// /api/logtail 交给 Go 原生处理（无 ssh、无 ES、无重推导）；其余仍委派。
	bsOnce      sync.Once
	resultsRoot string
	ssh         string
	repoRoot    string // bootstrap 的 root（远端仓库根）；为空时回落 repoDir
}

// newCSRFToken 生成与 Python secrets.token_urlsafe(32) 同字符集（base64url 无填充）的令牌。
func newCSRFToken() (string, error) {
	b := make([]byte, 32)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// writeSecurity 复刻 Python _send 的固定安全头（nosniff + DENY + Cache-Control）。
func writeSecurity(w http.ResponseWriter, cache string) {
	h := w.Header()
	h.Set("Cache-Control", cache)
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("X-Frame-Options", "DENY")
}

func (s *server) hostAllowed(host string) bool {
	for _, a := range s.allowed {
		if a == host {
			return true
		}
	}
	return false
}

// guard 对齐 _local_request：返回 "" 表示通过，否则返回可行动的中文原因。
func (s *server) guard(r *http.Request, write bool) string {
	host := r.Header.Get("Host")
	if host == "" {
		host = r.Host
	}
	if !s.hostAllowed(host) {
		sorted := append([]string(nil), s.allowed...)
		sort.Strings(sorted)
		return fmt.Sprintf("不认识的 Host: %q。控制台只接受本机回环地址（%s）。 用 ssh -L 转发时请直接打开 http://127.0.0.1:%d。",
			host, strings.Join(sorted, " / "), s.port)
	}
	if write {
		if origin := r.Header.Get("Origin"); origin != "" && !contains(s.origins, origin) {
			return fmt.Sprintf("Origin %q 与本机不同源，写操作被拒绝", origin)
		}
		switch site := r.Header.Get("Sec-Fetch-Site"); site {
		case "", "same-origin", "none":
		default:
			return fmt.Sprintf("Sec-Fetch-Site=%q：跨站写操作被拒绝", site)
		}
		token := r.Header.Get("X-Raceway-CSRF")
		if !csrfRe.MatchString(token) ||
			subtle.ConstantTimeCompare([]byte(token), []byte(s.csrf)) != 1 {
			return "CSRF 令牌缺失或不匹配（控制台重启后令牌会变）—— 刷新页面重试"
		}
	}
	return ""
}

func contains(xs []string, v string) bool {
	for _, x := range xs {
		if x == v {
			return true
		}
	}
	return false
}

func sendJSON(w http.ResponseWriter, code int, payload any) {
	writeSecurity(w, "no-store")
	w.Header().Set("Content-Type", "application/json")
	data, _ := json.Marshal(payload)
	w.WriteHeader(code)
	w.Write(data)
}

func sendForbidden(w http.ResponseWriter, why string) {
	sendJSON(w, http.StatusForbidden, map[string]string{"error": why})
}

// serveStatic 原生处理 '/'、'/legacy'、'/web/*'、'/echarts.min.js'；文件缺失返回 handled=false 交由 Python 兜底。
// '/' 优先 index.html，缺失回退 legacy.html（与 bench-compose.py _route_get_core 对齐）。
func (s *server) serveStatic(w http.ResponseWriter, path string) (handled bool) {
	var abs, mime, cache string
	switch {
	case path == "/":
		mime, cache = "text/html; charset=utf-8", "no-store"
		abs = filepath.Join(s.repoDir, "web", "index.html")
		if _, err := os.Stat(abs); err != nil {
			if alt := filepath.Join(s.repoDir, "web", "legacy.html"); fileExists(alt) {
				abs = alt // index 缺失 → 旧页兜底
			} else {
				return false // 都没有 → 委派 Python（返回 500 文案）
			}
		}
	case path == "/echarts.min.js":
		abs, mime, cache = filepath.Join(s.repoDir, "echarts.min.js"), "application/javascript; charset=utf-8", "public, max-age=86400"
	default:
		st, ok := webStatic[path]
		if !ok {
			return false
		}
		abs, mime = filepath.Join(s.repoDir, "web", st.file), st.mime
		cache = "no-store" // 仅 /legacy 会走到这里，保持一致
		if path != "/legacy" {
			cache = "public, max-age=300"
		}
	}
	data, err := os.ReadFile(abs)
	if err != nil {
		return false // 缺失 → 交给 Python（统一 404/500 文案）
	}
	writeSecurity(w, cache)
	w.Header().Set("Content-Type", mime)
	w.WriteHeader(200)
	w.Write(data)
	return true
}

func fileExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

type rpcResp struct {
	Code  int    `json:"code"`
	Ctype string `json:"ctype"`
	Body  string `json:"body"`
	Cache string `json:"cache"`
}

// runRPC 调用 python serve-rpc：stdin 传请求描述，stdout 收 {code,ctype,body,cache}。
func (s *server) runRPC(method, path string, q url.Values, body json.RawMessage) (rpcResp, error) {
	if body == nil {
		body = json.RawMessage("{}")
	}
	req := map[string]any{"method": method, "path": path, "csrf_token": s.csrf}
	if q != nil {
		req["query"] = q
	} else {
		req["query"] = map[string][]string{}
	}
	req["body"] = json.RawMessage(body)
	argList := append([]string{"serve-rpc"}, s.pyArgs...)
	cmd := exec.Command("python3", append([]string{filepath.Join(s.repoDir, "bench-compose.py")}, argList...)...)
	cmd.Dir = s.repoDir
	cmd.Stderr = os.Stderr
	in, _ := json.Marshal(req)
	cmd.Stdin = bytes.NewReader(in)
	out, err := cmd.Output()
	if err != nil {
		return rpcResp{}, fmt.Errorf("python serve-rpc 失败: %w", err)
	}
	var resp rpcResp
	if e := json.Unmarshal(out, &resp); e != nil {
		return rpcResp{}, fmt.Errorf("serve-rpc 响应无效: %w", e)
	}
	return resp, nil
}

func writeRPC(w http.ResponseWriter, resp rpcResp) {
	code := resp.Code
	if code == 0 {
		code = 500
	}
	writeSecurity(w, resp.Cache)
	w.Header().Set("Content-Type", resp.Ctype)
	w.WriteHeader(code)
	io.WriteString(w, resp.Body)
}

func (s *server) delegate(w http.ResponseWriter, method, path string, q url.Values, body json.RawMessage) {
	resp, err := s.runRPC(method, path, q, body)
	if err != nil {
		sendJSON(w, 500, map[string]string{"error": err.Error()})
		return
	}
	writeRPC(w, resp)
}

// bootstrapCache 惰性拉取一次 /api/bootstrap，缓存 results_root 与 ssh 模式，
// 供本地只读 logtail 判定能否走 Go 原生分支。失败则留空 → logtail 自动回落委派。
func (s *server) bootstrapCache() {
	s.bsOnce.Do(func() {
		resp, err := s.runRPC("GET", "/api/bootstrap", nil, nil)
		if err != nil || resp.Code != 200 {
			return
		}
		var b struct {
			ResultsRoot string `json:"results_root"`
			SSH         string `json:"ssh"`
			Root        string `json:"root"`
		}
		if json.Unmarshal([]byte(resp.Body), &b) == nil {
			s.resultsRoot = b.ResultsRoot
			s.ssh = b.SSH
			s.repoRoot = b.Root
			if s.repoRoot == "" {
				s.repoRoot = s.repoDir
			}
		}
	})
}

var cidRe = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]*$`)

// nativeLogtail 复刻 api_logtail 的本地分支（无 ssh、无 ES、无推导）：
// 读 results_root/<cid>/PROGRESS.txt 的最后 n 行。仅当可安全本地处理时返回 handled=true；
// ssh 模式、非法 cid、非数字 n 一律 handled=false 交由 serve-rpc，保证错误响应与 Python 逐字一致。
func (s *server) nativeLogtail(w http.ResponseWriter, q url.Values) (handled bool) {
	s.bootstrapCache()
	if s.ssh != "" || s.resultsRoot == "" {
		return false // 远端读取或 results_root 未知：委派
	}
	cid := ""
	if v := q.Get("cid"); v != "" {
		cid = v
	} else if v := q["cid"]; len(v) > 0 {
		cid = v[0]
	}
	if !cidRe.MatchString(cid) {
		return false // 非法 cid → 委派，沿用 Python 的 %r 报错文案
	}
	// n 必须可解析为 int（Python: int(n or 50)），否则委派让 Python 抛其原生错误。
	nStr := q.Get("n")
	var n int
	if nStr == "" {
		n = 50
	} else if parsed, err := strconv.Atoi(nStr); err != nil {
		return false
	} else {
		n = parsed
	}
	if n < 1 {
		n = 1
	}
	if n > 400 {
		n = 400
	}
	base := filepath.Clean(s.resultsRoot)
	f := filepath.Clean(filepath.Join(base, cid, "PROGRESS.txt"))
	// 纵深防御：解析后的路径必须仍在 results_root 之内（cid 正则已排除 '/'，双保险）。
	if f != base && !strings.HasPrefix(f, base+string(os.PathSeparator)) {
		return false
	}
	data, err := os.ReadFile(f)
	var tail string
	if err == nil {
		// 对齐 Python 文本模式：通用换行 \r\n / \r → \n
		text := strings.ReplaceAll(string(data), "\r\n", "\n")
		text = strings.ReplaceAll(text, "\r", "\n")
		lines := pyReadlines(text) // 每行保留结尾 '\n'（末行无换行则保留为无换行元素）
		if len(lines) > n {
			lines = lines[len(lines)-n:]
		}
		tail = strings.Join(lines, "")
	}
	body := tailJSON(tail)
	writeSecurity(w, "no-store")
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(200)
	w.Write(body)
	return true
}

// pyReadlines 复刻 Python 文本 readlines()：按 '\n' 切分并保留每行结尾的 '\n'；
// 仅末尾无换行的残段作为不带 '\n' 的最后一个元素（不产生尾随空串元素）。
func pyReadlines(s string) []string {
	if s == "" {
		return nil
	}
	var out []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			out = append(out, s[start:i+1])
			start = i + 1
		}
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}

// tailJSON 生成与 Python json.dumps({'lines': tail}, ensure_ascii=False) 逐字节一致的
// 对象：冒号后单空格、不转义 <>& 与非 ASCII。logtail 响应只有这一个键，故手写拼装。
func tailJSON(tail string) []byte {
	var v bytes.Buffer
	enc := json.NewEncoder(&v)
	enc.SetEscapeHTML(false)
	_ = enc.Encode(tail) // 产出带引号与转义的 JSON 字符串，末尾含换行
	val := bytes.TrimRight(v.Bytes(), "\n")
	buf := make([]byte, 0, len(val)+12)
	buf = append(buf, `{"lines": `...)
	buf = append(buf, val...)
	buf = append(buf, '}')
	return buf
}

func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	u, _ := url.Parse(r.RequestURI)
	path := u.Path
	switch r.Method {
	case http.MethodGet:
		if why := s.guard(r, false); why != "" {
			sendForbidden(w, why)
			return
		}
		if s.serveStatic(w, path) {
			return
		}
		if path == "/api/logtail" && s.nativeLogtail(w, u.Query()) {
			return
		}
		if s.nativeGetFeature(w, path, u.Query()) {
			return
		}
		s.delegate(w, "GET", path, u.Query(), nil)
	case http.MethodPost:
		if why := s.guard(r, true); why != "" {
			sendForbidden(w, why)
			return
		}
		if ct := strings.SplitN(r.Header.Get("Content-Type"), ";", 2)[0]; strings.TrimSpace(ct) != "application/json" {
			sendJSON(w, 415, map[string]string{"error": "application/json required"})
			return
		}
		raw, err := io.ReadAll(io.LimitReader(r.Body, 1<<20+1))
		if err != nil || len(raw) > 1<<20 {
			sendJSON(w, 400, map[string]string{"error": "bad json or length"})
			return
		}
		if len(raw) == 0 {
			raw = []byte("{}")
		}
		var probe any
		if json.Unmarshal(raw, &probe) != nil {
			sendJSON(w, 400, map[string]string{"error": "bad json or length"})
			return
		}
		if _, ok := probe.(map[string]any); !ok {
			sendJSON(w, 400, map[string]string{"error": "JSON object required"})
			return
		}
		if s.nativePostFeature(w, path, raw) {
			return
		}
		s.delegate(w, "POST", path, nil, json.RawMessage(raw))
	default:
		sendJSON(w, 405, map[string]string{"error": "method not allowed"})
	}
}

// parsePort 仅解析 --port，其余 flag 原样透传给 python，避免与 python argparse 抢参数。
func parsePort(args []string) int {
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--port" && i+1 < len(args) {
			if p, err := strconv.Atoi(args[i+1]); err == nil {
				return p
			}
		}
		if v, ok := strings.CutPrefix(a, "--port="); ok {
			if p, err := strconv.Atoi(v); err == nil {
				return p
			}
		}
	}
	return 8802
}

func main() {
	repoDir, err := os.Getwd()
	if err != nil {
		fmt.Fprintln(os.Stderr, "获取工作目录失败:", err)
		os.Exit(1)
	}
	// 前端都在 web/：新版优先，旧页兜底；两个都没有就起不来（与 cmd_serve 一致）。
	if !fileExists(filepath.Join(repoDir, "web", "index.html")) &&
		!fileExists(filepath.Join(repoDir, "web", "legacy.html")) {
		fmt.Fprintln(os.Stderr, "找不到前端 web/index.html 或 web/legacy.html —— 同步仓库时请带上 web/ 目录")
		os.Exit(1)
	}
	if !fileExists(filepath.Join(repoDir, "web", "index.html")) {
		fmt.Fprintln(os.Stderr, "警告：web/index.html 缺失，使用旧页面 web/legacy.html（/ 与 /legacy 均可访问）")
	}
	csrf, err := newCSRFToken()
	if err != nil {
		fmt.Fprintln(os.Stderr, "生成 CSRF 令牌失败:", err)
		os.Exit(1)
	}
	port := parsePort(os.Args[1:])
	s := &server{
		repoDir: repoDir,
		pyArgs:  os.Args[1:],
		csrf:    csrf,
		port:    port,
	}
	s.allowed = []string{
		fmt.Sprintf("127.0.0.1:%d", port),
		fmt.Sprintf("localhost:%d", port),
		fmt.Sprintf("[::1]:%d", port),
	}
	for _, h := range s.allowed {
		s.origins = append(s.origins, "http://"+h)
	}
	addr := fmt.Sprintf("127.0.0.1:%d", port)
	fmt.Printf("raceway 控制台(Go 外壳): http://%s  业务逻辑委派 python serve-rpc\n", addr)
	if err := http.ListenAndServe(addr, s); err != nil {
		fmt.Fprintln(os.Stderr, "启动失败:", err)
		os.Exit(1)
	}
}
