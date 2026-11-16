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
)

// 静态资源白名单：URL 路径 → 仓库内相对路径 / MIME（与 bench-compose.py 的 _WEB_STATIC 一致）。
var webStatic = map[string]struct{ file, mime string }{
	"/web/style.css":            {"style.css", "text/css; charset=utf-8"},
	"/web/app.js":               {"app.js", "application/javascript; charset=utf-8"},
	"/web/vendor/preact.umd.js": {"vendor/preact.umd.js", "application/javascript; charset=utf-8"},
	"/web/vendor/hooks.umd.js":  {"vendor/hooks.umd.js", "application/javascript; charset=utf-8"},
	"/web/vendor/htm.umd.js":    {"vendor/htm.umd.js", "application/javascript; charset=utf-8"},
}

var csrfRe = regexp.MustCompile(`^[A-Za-z0-9_-]{1,128}$`)

type server struct {
	repoDir string   // 仓库根：web/、echarts.min.js、bench-compose.py 所在
	pyArgs  []string // 透传给 python serve-rpc 的原始 flag（--root/--ssh/... 全含）
	csrf    string   // 每次启动生成的写操作令牌（前端从 bootstrap 取回）
	allowed []string // Host 白名单：127.0.0.1:port / localhost:port / [::1]:port
	origins []string // Origin 白名单：http://<allowed>
	port    int
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

// serveStatic 原生处理 '/'、'/web/*'、'/echarts.min.js'；文件缺失返回 handled=false 交由 Python 兜底。
func (s *server) serveStatic(w http.ResponseWriter, path string) (handled bool) {
	var abs, mime, cache string
	switch {
	case path == "/":
		abs, mime, cache = filepath.Join(s.repoDir, "web", "index.html"), "text/html; charset=utf-8", "no-store"
	case path == "/echarts.min.js":
		abs, mime, cache = filepath.Join(s.repoDir, "echarts.min.js"), "application/javascript; charset=utf-8", "public, max-age=86400"
	default:
		if st, ok := webStatic[path]; ok {
			abs, mime, cache = filepath.Join(s.repoDir, "web", st.file), st.mime, "public, max-age=300"
		} else {
			return false
		}
	}
	data, err := os.ReadFile(abs)
	if err != nil {
		return false // 缺失 → 交给 Python（走内联旧页 _PAGE 或统一 404 文案）
	}
	writeSecurity(w, cache)
	w.Header().Set("Content-Type", mime)
	w.WriteHeader(200)
	w.Write(data)
	return true
}

// delegate 调用 python serve-rpc：stdin 传请求描述，stdout 收 {code,ctype,body,cache}。
func (s *server) delegate(w http.ResponseWriter, method, path string, q url.Values, body json.RawMessage) {
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
		sendJSON(w, 500, map[string]string{"error": fmt.Sprintf("python serve-rpc 失败: %v", err)})
		return
	}
	var resp struct {
		Code  int    `json:"code"`
		Ctype string `json:"ctype"`
		Body  string `json:"body"`
		Cache string `json:"cache"`
	}
	if e := json.Unmarshal(out, &resp); e != nil {
		sendJSON(w, 500, map[string]string{"error": fmt.Sprintf("serve-rpc 响应无效: %v", e)})
		return
	}
	code := resp.Code
	if code == 0 {
		code = 500
	}
	writeSecurity(w, resp.Cache)
	w.Header().Set("Content-Type", resp.Ctype)
	w.WriteHeader(code)
	io.WriteString(w, resp.Body)
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
