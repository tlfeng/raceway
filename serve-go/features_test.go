package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestTrackRelValidation(t *testing.T) {
	bad := []string{"", "../x", "a/../b", "/etc/passwd", `a\b`, "a/./b", "a b", "a\x00b"}
	for _, s := range bad {
		if relPathRe.MatchString(s) && strings.Contains(s, "..") == false && !strings.HasPrefix(s, "/") && !strings.Contains(s, `\`) && !strings.Contains(s, "\x00") {
			// 只放行既匹配白名单又无危险片段的
			if s == "a b" || s == "" {
				t.Fatalf("不应放行 %q", s)
			}
		}
	}
	good := []string{"geonames/index.json", "hv1024/challenges/default.json", "a-b_c.d"}
	for _, s := range good {
		if !relPathRe.MatchString(s) {
			t.Fatalf("应放行 %q", s)
		}
	}
}

func TestReadFileLocal(t *testing.T) {
	d := t.TempDir()
	p := filepath.Join(d, "x.txt")
	if err := os.WriteFile(p, []byte("hello\n"), 0644); err != nil {
		t.Fatal(err)
	}
	s := &server{}
	got, ok := s.readFile(p)
	if !ok || strings.TrimSpace(got) != "hello" {
		t.Fatalf("readFile = %q, %v", got, ok)
	}
	if _, ok := s.readFile(filepath.Join(d, "none")); ok {
		t.Fatal("不存在的文件应返回 false")
	}
}

func TestNativeLogfileFilter(t *testing.T) {
	d := t.TempDir()
	cid := "camp1"
	campDir := filepath.Join(d, cid)
	if err := os.MkdirAll(campDir, 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(campDir, "debug.log"),
		[]byte("alpha OK\nbeta FAIL\ngamma OK\ndelta FAIL\n"), 0644); err != nil {
		t.Fatal(err)
	}
	s := &server{resultsRoot: d, repoRoot: d}
	q := url.Values{}
	q.Set("cid", cid)
	q.Set("file", "debug.log")
	q.Set("n", "2")
	q.Set("q", "FAIL")
	w := httptest.NewRecorder()
	if !s.nativeLogfile(w, q) {
		t.Fatal("handler 未接住")
	}
	var body struct {
		Total int      `json:"total"`
		Lines []string `json:"lines"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatal(err, w.Body.String())
	}
	if body.Total != 2 || len(body.Lines) != 2 || body.Lines[0] != "beta FAIL" {
		t.Fatalf("body = %+v", body)
	}
}

func TestNativeSidecar(t *testing.T) {
	d := t.TempDir()
	cid, fid := "camp2", "a-geo-r1"
	res := filepath.Join(d, cid, "results")
	if err := os.MkdirAll(res, 0755); err != nil {
		t.Fatal(err)
	}
	doc := `{"engine":{"version":"2.5.0"},"jdk":{"version":"25"}}`
	if err := os.WriteFile(filepath.Join(res, fid+"-wq-sidecar.json"), []byte(doc), 0644); err != nil {
		t.Fatal(err)
	}
	s := &server{resultsRoot: d, repoRoot: d}
	q := url.Values{"cid": []string{cid}, "fid": []string{fid}}
	w := httptest.NewRecorder()
	if !s.nativeSidecar(w, q) {
		t.Fatal("handler 未接住")
	}
	var body struct {
		Sidecars []struct {
			Phase string         `json:"phase"`
			Data  map[string]any `json:"data"`
		} `json:"sidecars"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatal(err, w.Body.String())
	}
	if len(body.Sidecars) != 1 || body.Sidecars[0].Phase != "wq" {
		t.Fatalf("sidecars = %+v", body.Sidecars)
	}
}

func TestNativeMonitor(t *testing.T) {
	d := t.TempDir()
	cid, fid := "camp3", "a-geo-r1"
	mon := filepath.Join(d, cid, "monitors")
	if err := os.MkdirAll(mon, 0755); err != nil {
		t.Fatal(err)
	}
	env := "fid,phase,node,ip,samples,steal_avg,steal_max,foreign_avg,foreign_max,swapin,env_verdict\n" +
		fid + ",w,n1,1.1.1.1,2,0.1,0.2,0,0,0,OK\n"
	if err := os.WriteFile(filepath.Join(mon, "ENV-SUMMARY.csv"), []byte(env), 0644); err != nil {
		t.Fatal(err)
	}
	csv := "ts,cpu_usr,cpu_steal,bench_cpu,load1\n100,12.5,0.1,30.0,2.1\n105,13.0,0.2,31.5,2.3\n"
	if err := os.WriteFile(filepath.Join(mon, fid+"-n1.csv"), []byte(csv), 0644); err != nil {
		t.Fatal(err)
	}
	s := &server{resultsRoot: d, repoRoot: d}
	q := url.Values{"cid": []string{cid}, "fid": []string{fid}}
	w := httptest.NewRecorder()
	if !s.nativeMonitor(w, q) {
		t.Fatal("handler 未接住")
	}
	var body struct {
		Interval int `json:"interval"`
		Series   []struct {
			Node string           `json:"node"`
			Rows []map[string]any `json:"rows"`
		} `json:"series"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatal(err, w.Body.String())
	}
	if body.Interval != 5 || len(body.Series) != 1 || body.Series[0].Node != "n1" {
		t.Fatalf("body = %+v", body)
	}
	bench := body.Series[0].Rows[1]["bench_cpu"]
	if f, ok := bench.(float64); !ok || f != 31.5 {
		t.Fatalf("bench_cpu = %v", bench)
	}
}

func TestNativeReportMarkdown(t *testing.T) {
	d := t.TempDir()
	cid := "camp4"
	camp := filepath.Join(d, cid)
	if err := os.MkdirAll(filepath.Join(camp, "monitors"), 0755); err != nil {
		t.Fatal(err)
	}
	plan := "fid\tconfig\ttrack\tround\trecall\test\na-geo-r1\tk1\tgeo\t1\trecall\t45\n"
	if err := os.WriteFile(filepath.Join(camp, "PLAN.tsv"), []byte(plan), 0644); err != nil {
		t.Fatal(err)
	}
	races := "| 场次 | 状态 | 赛前卡点 | 用时 |\n|---|---|---|---|\n| a-geo-r1 | VALID | OK | 29min |\n"
	if err := os.WriteFile(filepath.Join(camp, "RACES.md"), []byte(races), 0644); err != nil {
		t.Fatal(err)
	}
	s := &server{resultsRoot: d, repoRoot: d}
	q := url.Values{"cid": []string{cid}, "fmt": []string{"md"}}
	w := httptest.NewRecorder()
	if !s.nativeReport(w, q) {
		t.Fatal("handler 未接住")
	}
	var body struct {
		Text string `json:"text"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(body.Text, "raceway 跑分报告") || !strings.Contains(body.Text, "a-geo-r1") {
		t.Fatalf("report = %q", body.Text)
	}
}

func TestNativeBadPathRejected(t *testing.T) {
	s := &server{resultsRoot: "/x", repoRoot: "/x"}
	q := url.Values{"cid": []string{"../evil"}, "fid": []string{"a"}}
	w := httptest.NewRecorder()
	if !s.nativeSidecar(w, q) || w.Code != http.StatusBadRequest {
		t.Fatalf("非法 cid 应 400，实际 %d", w.Code)
	}
}
