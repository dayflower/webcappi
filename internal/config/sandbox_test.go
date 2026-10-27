package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSandboxConfiguration(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	for _, tc := range []struct {
		input string
		want  bool
	}{
		{`{}`, true},
		{`{"sandbox":null}`, true},
		{`{"sandbox":""}`, true},
		{`{"sandbox":true}`, true},
		{`{"sandbox":false}`, false},
	} {
		if err := os.WriteFile(path, []byte(tc.input), 0600); err != nil {
			t.Fatal(err)
		}
		cfg, err := Load(path)
		if err != nil || cfg.Sandbox != tc.want {
			t.Fatalf("%s: sandbox=%v, error=%v", tc.input, cfg.Sandbox, err)
		}
	}
	for _, input := range []string{
		`{"sandbox":"false"}`, `{"sandbox":"true"}`, `{"sandbox":0}`, `{"sandbox":[]}`, `{"sandbox":{}}`,
		`{"no_sandbox":true}`, `{"sandbox":true,"typo":true}`, `{"sandbox":false,"auth":{"typo":true}}`,
	} {
		if err := os.WriteFile(path, []byte(input), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := Load(path); err == nil {
			t.Fatalf("accepted invalid config: %s", input)
		}
	}
}
