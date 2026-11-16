package main

// 新功能写操作的 Go 原生 POST handler。调度在 ServeHTTP 的 POST 分支完成
// （CSRF guard 已在那里执行）；这里只做参数校验 + exec bench-ops.py。

import (
	"encoding/json"
	"net/http"
	"strings"
	"time"
)

func bodyMap(raw []byte) (map[string]any, error) {
	var m map[string]any
	if len(raw) == 0 {
		return map[string]any{}, nil
	}
	err := json.Unmarshal(raw, &m)
	return m, err
}

func bodyStr(m map[string]any, key string) string {
	if v, ok := m[key].(string); ok {
		return strings.TrimSpace(v)
	}
	return ""
}

// nativePostFeature 处理新增写端点；返回 false 表示交回 delegate。
// raw 是已通过 Content-Type / JSON 对象校验的请求体。
func (s *server) nativePostFeature(w http.ResponseWriter, path string, raw []byte) bool {
	s.bootstrapCache()
	m, err := bodyMap(raw)
	if err != nil {
		sendErrJSON(w, 400, "bad json")
		return true
	}
	switch path {
	case "/api/track-write":
		rel := bodyStr(m, "path")
		if !relPathRe.MatchString(rel) || strings.Contains(rel, "..") {
			sendErrJSON(w, 400, "非法赛道路径")
			return true
		}
		content, ok := m["content"].(string)
		if !ok {
			sendErrJSON(w, 400, "文件内容必须是文本")
			return true
		}
		if len(content) > 8*1024*1024 {
			sendErrJSON(w, 400, "赛道文件不能超过 8MB")
			return true
		}
		payload := map[string]any{"path": rel, "content": content}
		if ex := bodyStr(m, "expect"); ex != "" {
			payload["expect"] = ex
		}
		reply, e := s.ops("track-write", payload, 120*time.Second)
		if e != nil {
			sendErrJSON(w, 500, e.Error())
			return true
		}
		sendJSON(w, 200, reply)
		return true

	case "/api/track-delete":
		rel := bodyStr(m, "path")
		if !relPathRe.MatchString(rel) || strings.Contains(rel, "..") {
			sendErrJSON(w, 400, "非法赛道路径")
			return true
		}
		payload := map[string]any{"path": rel}
		if ex := bodyStr(m, "expect"); ex != "" {
			payload["expect"] = ex
		}
		reply, e := s.ops("track-delete", payload, 120*time.Second)
		if e != nil {
			sendErrJSON(w, 500, e.Error())
			return true
		}
		sendJSON(w, 200, reply)
		return true

	case "/api/export-gen":
		tag := bodyStr(m, "tag")
		cids, ok := m["campaigns"].([]any)
		if !ok || len(cids) == 0 {
			sendErrJSON(w, 400, "请选择至少一个 campaign")
			return true
		}
		names := []string{}
		for _, c := range cids {
			s, ok := c.(string)
			if !ok || !cidReF.MatchString(strings.TrimSpace(s)) {
				sendErrJSON(w, 400, "非法 campaign 名")
				return true
			}
			names = append(names, strings.TrimSpace(s))
		}
		reply, e := s.ops("export-gen", map[string]any{"tag": tag, "campaigns": names},
			20*time.Minute)
		if e != nil {
			sendErrJSON(w, 500, e.Error())
			return true
		}
		sendJSON(w, 200, reply)
		return true

	case "/api/close":
		cid := bodyStr(m, "cid")
		if !cidReF.MatchString(cid) {
			sendErrJSON(w, 400, "非法 campaign id")
			return true
		}
		dry := true
		if v, ok := m["dry_run"].(bool); ok {
			dry = v
		}
		payload := map[string]any{"cid": cid, "dry_run": dry}
		if reason := bodyStr(m, "reason"); reason != "" {
			payload["reason"] = reason
		}
		reply, e := s.ops("close", payload, 150*time.Second)
		if e != nil {
			sendErrJSON(w, 500, e.Error())
			return true
		}
		sendJSON(w, 200, reply)
		return true
	}
	return false
}
