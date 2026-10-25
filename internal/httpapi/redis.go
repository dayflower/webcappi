package httpapi

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"runtime"
	"strings"
	"time"

	"github.com/dayflower/webcappi/internal/capture"
	"github.com/dayflower/webcappi/internal/config"
	"github.com/redis/go-redis/v9"
)

//go:embed redis.lua
var redisMetadataScript string

type redisMetadata struct {
	client     *redis.Client
	script     *redis.Script
	keys       []string
	maxEntries int
	maxBytes   int64
	timeout    time.Duration
	retention  time.Duration
	fence      time.Duration
}

// The wire format contains only allowlisted validation metadata, never PNG bytes.
type wireMetadata struct {
	Version int                     `json:"version"`
	ETag    string                  `json:"etag"`
	Source  *capture.SourceResponse `json:"source,omitempty"`
	MaxAge  int                     `json:"max_age"`
}

func redisScope(c config.Config) string {
	type profile struct {
		Directory  string
		Extensions []string
		Selectors  []string
	}
	profiles := make(map[string]profile)
	for name, p := range c.Profiles {
		profiles[name] = profile{p.ProfileDirectory, append([]string{}, p.Extensions...), append([]string{}, p.ReadySelectors...)}
	}
	// Automatic templates have process-specific paths; their logical identity is stable.
	if _, ok := profiles[config.DefaultProfileName]; !ok {
		profiles[config.DefaultProfileName] = profile{Directory: "Default", Extensions: []string{}, Selectors: []string{}}
	}
	b, _ := json.Marshal(struct {
		Schema         string
		Architecture   string
		Browser        string
		Namespace      string
		Generation     string
		Profiles       map[string]profile
		Resize         capture.ResizeAlgorithm
		MaxAge         int
		Entries        int
		Bytes          int64
		Retention      int
		CaptureTimeout int
	}{
		Schema: "metadata-v1", Architecture: runtime.GOARCH, Browser: capture.BrowserVersion,
		Namespace: c.Cache.Redis.Namespace, Generation: c.Cache.Redis.Generation,
		Profiles: profiles, Resize: c.ResizeAlgorithm, MaxAge: c.MaxAgeSeconds,
		Entries: c.MetadataMaxEntries, Bytes: c.MetadataMaxBytes,
		Retention: c.Cache.Redis.Retention(), CaptureTimeout: c.CaptureTimeoutSeconds,
	})
	h := sha256.Sum256(b)
	return "webcappi:{" + hex.EncodeToString(h[:]) + "}:"
}

func openRedisMetadata(ctx context.Context, c config.Config) (*redisMetadata, error) {
	r := c.Cache.Redis
	timeout := time.Duration(r.OperationTimeoutMS()) * time.Millisecond
	options := &redis.Options{
		Addr: r.Address, Username: r.Username, MaxRetries: -1,
		DialTimeout: timeout, ReadTimeout: timeout, WriteTimeout: timeout,
		ContextTimeoutEnabled: true, PoolSize: min(c.MaxRequests, 16), MaxActiveConns: min(c.MaxRequests, 16), PoolTimeout: timeout,
		DisableIdentity: true,
	}
	if r.PasswordFile != "" {
		f, err := os.Open(r.PasswordFile)
		if err != nil {
			return nil, errors.New("cannot open Redis password file")
		}
		defer f.Close()
		b, err := io.ReadAll(io.LimitReader(f, 4097))
		if err != nil || len(b) == 0 || len(b) > 4096 {
			return nil, errors.New("redis password file must contain 1-4096 bytes")
		}
		options.Password = strings.TrimRight(string(b), "\r\n")
		if options.Password == "" {
			return nil, errors.New("redis password must not be empty")
		}
	}
	if r.TLS {
		options.TLSConfig = &tls.Config{MinVersion: tls.VersionTLS12}
		if r.CAFile != "" {
			b, err := os.ReadFile(r.CAFile)
			if err != nil {
				return nil, errors.New("cannot read Redis CA file")
			}
			pool, err := x509.SystemCertPool()
			if err != nil {
				pool = x509.NewCertPool()
			}
			if !pool.AppendCertsFromPEM(b) {
				return nil, errors.New("redis CA file contains no certificates")
			}
			options.TLSConfig.RootCAs = pool
		}
	}
	prefix := redisScope(c)
	s := &redisMetadata{
		client: redis.NewClient(options), script: redis.NewScript(redisMetadataScript),
		keys: []string{prefix + "records", prefix + "lru", prefix + "expiry"}, maxEntries: c.MetadataMaxEntries, maxBytes: c.MetadataMaxBytes,
		timeout: timeout, retention: time.Duration(r.Retention()) * time.Second,
		fence: c.Timeout() + 2*timeout + 10*time.Second,
	}
	if err := s.check(ctx); err != nil {
		_ = s.close()
		return nil, err
	}
	return s, nil
}

func (s *redisMetadata) run(ctx context.Context, op, key, token string, extra ...any) ([]any, error) {
	ctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	args := []any{op, key, token, s.maxEntries, s.maxBytes, s.retention.Milliseconds(), s.fence.Milliseconds()}
	args = append(args, extra...)
	result, err := s.script.Run(ctx, s.client, s.keys, args...).Slice()
	if err != nil {
		return nil, metadataFailure(op, err)
	}
	return result, nil
}

func (s *redisMetadata) begin(ctx context.Context, key string, _ time.Time) (metadataSnapshot, error) {
	started := time.Now()
	token := make([]byte, 16)
	if _, err := rand.Read(token); err != nil {
		return metadataSnapshot{}, metadataFailure("token")
	}
	id := hex.EncodeToString(token)
	values, err := s.run(ctx, "begin", key, id)
	if err != nil {
		return metadataSnapshot{}, err
	}
	if len(values) != 5 {
		return metadataSnapshot{}, metadataFailure("decode")
	}
	payload, ok := values[0].(string)
	validated, okV := values[1].(int64)
	now, okN := values[2].(int64)
	epoch, okE := values[3].(string)
	sequence, okS := values[4].(int64)
	if !ok || !okV || !okN || !okE || !okS || sequence <= 0 {
		return metadataSnapshot{}, metadataFailure("decode")
	}
	snapshot := metadataSnapshot{key: key, token: epoch, order: uint64(sequence), now: time.UnixMilli(now), received: time.Now(), clockMargin: time.Since(started)}
	if payload != "" {
		if int64(len(payload)) > s.maxBytes {
			return metadataSnapshot{}, metadataFailure("decode")
		}
		var wire wireMetadata
		if err := json.Unmarshal([]byte(payload), &wire); err != nil || wire.Version != 1 || wire.ETag == "" || wire.MaxAge < 0 || wire.MaxAge > 86400 || validated > now || validated <= 0 {
			return metadataSnapshot{}, metadataFailure("decode")
		}
		snapshot.previous = cloneMetadata(&metadata{key: key, etag: wire.ETag, source: wire.Source, validated: time.UnixMilli(validated), policy: responsePolicy{maxAge: wire.MaxAge}})
	}
	return snapshot, nil
}

func (s *redisMetadata) commit(ctx context.Context, snapshot metadataSnapshot, value *metadata) (time.Time, error) {
	// Anchor to Redis's clock sampled before receiving begin's response. Network
	// delay makes this conservative even when Pod wall clocks disagree.
	validated := snapshot.now.Add(time.Since(snapshot.received))
	value.validated = validated
	if snapshot.token == "" {
		return validated, nil
	}
	copy := cloneMetadata(value)
	payload := ""
	if !copy.policy.noStore {
		b, err := json.Marshal(wireMetadata{Version: 1, ETag: copy.etag, Source: copy.source, MaxAge: copy.policy.maxAge})
		if err != nil {
			return time.Time{}, fmt.Errorf("encode metadata: %w", err)
		}
		// Oversized responses invalidate old data without sending an unbounded value.
		if int64(len(b))+512 <= s.maxBytes {
			payload = string(b)
		}
	}
	noStore := 0
	if payload == "" {
		noStore = 1
	}
	values, err := s.run(ctx, "commit", snapshot.key, snapshot.token, payload, validated.UnixMilli(), noStore, snapshot.order, snapshot.now.Add(s.fence).UnixMilli())
	if err != nil {
		return time.Time{}, err
	}
	if len(values) != 3 {
		return time.Time{}, metadataFailure("decode")
	}
	stamp, ok := values[0].(int64)
	now, okN := values[1].(int64)
	saved, okS := values[2].(int64)
	if !ok || !okN || !okS {
		return time.Time{}, metadataFailure("decode")
	}
	if saved == 0 {
		slog.Debug("metadata update skipped", "reason", "superseded-or-evicted")
	}
	value.validated = time.UnixMilli(stamp)
	return time.UnixMilli(now), nil
}

func (s *redisMetadata) finish(metadataSnapshot) {}
func (s *redisMetadata) close() error            { return s.client.Close() }
func (s *redisMetadata) check(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, s.timeout)
	defer cancel()
	if err := s.client.Ping(ctx).Err(); err != nil {
		return metadataFailure("check", err)
	}
	return nil
}
