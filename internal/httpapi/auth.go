package httpapi

import (
	"crypto/sha256"
	"crypto/subtle"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"regexp"
	"strings"

	"github.com/dayflower/webcappi/internal/config"
)

const maxBearerTokenBytes = 4096

var bearerTokenPattern = regexp.MustCompile(`^[A-Za-z0-9._~+/-]+=*$`)

// BearerAuth keeps only a digest of the token loaded at startup.
type BearerAuth struct {
	enabled bool
	digest  [sha256.Size]byte
}

func NewBearerAuth(c *config.Auth) (*BearerAuth, error) {
	a := &BearerAuth{}
	if c == nil {
		return a, nil
	}
	f, err := os.Open(c.BearerTokenFile)
	if err != nil {
		return nil, fmt.Errorf("open auth.bearer_token_file: %w", err)
	}
	defer f.Close()
	// Allow one trailing CRLF while bounding both allocation and token length.
	b, err := io.ReadAll(io.LimitReader(f, maxBearerTokenBytes+3))
	if err != nil {
		return nil, fmt.Errorf("read auth.bearer_token_file: %w", err)
	}
	token := strings.TrimSuffix(string(b), "\n")
	if len(token) != len(b) {
		token = strings.TrimSuffix(token, "\r")
	}
	if len(token) == 0 || len(token) > maxBearerTokenBytes || !bearerTokenPattern.MatchString(token) {
		return nil, errors.New("auth.bearer_token_file must contain one valid Bearer token of 1..4096 bytes, with an optional trailing LF or CRLF")
	}
	a.enabled = true
	a.digest = sha256.Sum256([]byte(token))
	return a, nil
}

// Handler exempts the guide and probes and authenticates other requests before routing.
func (a *BearerAuth) Handler(next http.Handler) http.Handler {
	if !a.enabled {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/" || r.URL.Path == "/healthz" || r.URL.Path == "/readyz" {
			next.ServeHTTP(w, r)
			return
		}
		values := r.Header.Values("Authorization")
		if len(values) == 1 {
			scheme, token, ok := strings.Cut(values[0], " ")
			token = strings.TrimLeft(token, " ")
			if ok && strings.EqualFold(scheme, "Bearer") && len(token) > 0 && len(token) <= maxBearerTokenBytes {
				digest := sha256.Sum256([]byte(token))
				if subtle.ConstantTimeCompare(digest[:], a.digest[:]) == 1 {
					next.ServeHTTP(w, r)
					return
				}
			}
		}
		w.Header().Set("WWW-Authenticate", "Bearer")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		fail(w, http.StatusUnauthorized, "unauthorized")
	})
}
