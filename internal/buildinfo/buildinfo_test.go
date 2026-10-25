package buildinfo

import (
	"strings"
	"testing"
)

func TestInjectedIdentityTakesPrecedence(t *testing.T) {
	oldVersion, oldRevision, oldModified := Version, Revision, Modified
	t.Cleanup(func() { Version, Revision, Modified = oldVersion, oldRevision, oldModified })
	Version, Revision, Modified = "v0.1.0-rc.1", strings.Repeat("a", 40), "false"
	version, revision, modified := Current()
	if version != Version || revision != Revision || modified != Modified {
		t.Fatalf("injected identity was lost: %s %s %s", version, revision, modified)
	}
	if got := String(); got != "webcappi v0.1.0-rc.1 (commit "+Revision+"; modified false)" {
		t.Fatalf("unexpected identity: %s", got)
	}
}
