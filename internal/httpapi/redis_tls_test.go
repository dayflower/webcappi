package httpapi

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"io"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

// Terminate TLS locally and forward the Redis protocol to the disposable server.
// This exercises actual certificate verification, authentication and Lua calls.
func TestRedisTLS(t *testing.T) {
	c := redisConfig(t)
	upstream := c.Cache.Redis.Address
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	certificate := &x509.Certificate{
		SerialNumber: big.NewInt(1), NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, certificate, certificate, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	private, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		t.Fatal(err)
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	pair, err := tls.X509KeyPair(certPEM, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: private}))
	if err != nil {
		t.Fatal(err)
	}
	listener, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{pair}, MinVersion: tls.VersionTLS12})
	if err != nil {
		t.Fatal(err)
	}
	var workers sync.WaitGroup
	stopped := make(chan struct{})
	go func() {
		defer close(stopped)
		for {
			conn, err := listener.Accept()
			if err != nil {
				return
			}
			workers.Add(1)
			go func() {
				defer workers.Done()
				defer conn.Close()
				_ = conn.SetDeadline(time.Now().Add(3 * time.Second))
				remote, err := net.DialTimeout("tcp", upstream, time.Second)
				if err != nil {
					return
				}
				defer remote.Close()
				_ = remote.SetDeadline(time.Now().Add(3 * time.Second))
				done := make(chan struct{})
				go func() { _, _ = io.Copy(remote, conn); _ = remote.Close(); close(done) }()
				_, _ = io.Copy(conn, remote)
				_ = conn.Close()
				<-done
			}()
		}
	}()
	t.Cleanup(func() { _ = listener.Close(); <-stopped; workers.Wait() })
	ca := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(ca, certPEM, 0600); err != nil {
		t.Fatal(err)
	}
	c.Cache.Redis.Address = listener.Addr().String()
	c.Cache.Redis.TLS = true
	c.Cache.Redis.CAFile = ca
	cache := testRedis(t, c)
	s := cache.store.(*redisMetadata)
	ticket, err := s.begin(t.Context(), "tls", time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.commit(t.Context(), ticket, &metadata{key: "tls", etag: `"tls"`, policy: responsePolicy{maxAge: 60}}); err != nil {
		t.Fatal(err)
	}
	if next, err := s.begin(t.Context(), "tls", time.Now()); err != nil || next.previous == nil || next.previous.etag != `"tls"` {
		t.Fatal("TLS metadata sharing failed", err)
	}
	c.Cache.Redis.CAFile = ""
	if cache, err := OpenCache(t.Context(), c); err == nil {
		_ = cache.Close()
		t.Fatal("untrusted TLS certificate accepted")
	}
}

func TestRedisCredentialFiles(t *testing.T) {
	c := redisConfig(t)
	path := filepath.Join(t.TempDir(), "password")
	c.Cache.Redis.PasswordFile = path
	for _, value := range []string{"", "\n", string(make([]byte, 4097))} {
		if err := os.WriteFile(path, []byte(value), 0600); err != nil {
			t.Fatal(err)
		}
		if cache, err := OpenCache(t.Context(), c); err == nil {
			_ = cache.Close()
			t.Fatal("invalid password file accepted")
		}
	}
	if os.Getenv("WEBCAPPI_REDIS_TEST_PASSWORD_FILE") != "" {
		if err := os.WriteFile(path, []byte("incorrect-password"), 0600); err != nil {
			t.Fatal(err)
		}
		if cache, err := OpenCache(t.Context(), c); err == nil {
			_ = cache.Close()
			t.Fatal("incorrect Redis password accepted")
		}
	}
	c.Cache.Redis.PasswordFile = ""
	c.Cache.Redis.TLS = true
	c.Cache.Redis.CAFile = filepath.Join(t.TempDir(), "invalid-ca.pem")
	if err := os.WriteFile(c.Cache.Redis.CAFile, []byte("not a certificate"), 0600); err != nil {
		t.Fatal(err)
	}
	if cache, err := OpenCache(t.Context(), c); err == nil {
		_ = cache.Close()
		t.Fatal("invalid CA file accepted")
	}
}
