package config

import (
	"errors"
	"net"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// Cache selects the validation metadata store. The zero value uses memory.
type Cache struct {
	Backend string `json:"backend"`
	Redis   *Redis `json:"redis,omitempty"`
}

type Redis struct {
	Address          string `json:"address"`
	Namespace        string `json:"namespace"`
	Generation       string `json:"generation"`
	Username         string `json:"username,omitempty"`
	PasswordFile     string `json:"password_file,omitempty"`
	TLS              bool   `json:"tls,omitempty"`
	CAFile           string `json:"ca_file,omitempty"`
	TimeoutMS        int    `json:"timeout_ms,omitempty"`
	RetentionSeconds int    `json:"retention_seconds,omitempty"`
}

func (r Redis) OperationTimeoutMS() int {
	if r.TimeoutMS == 0 {
		return 500
	}
	return r.TimeoutMS
}

func (r Redis) Retention() int {
	if r.RetentionSeconds == 0 {
		return 86400
	}
	return r.RetentionSeconds
}

func (c Cache) Validate(maxAge int) error {
	switch c.Backend {
	case "", "memory":
		if c.Redis != nil {
			return errors.New("cache.redis requires the redis backend")
		}
		return nil
	case "redis":
		if c.Redis == nil {
			return errors.New("cache.redis is required")
		}
	default:
		return errors.New("cache.backend must be memory or redis")
	}
	r := c.Redis
	host, port, err := net.SplitHostPort(r.Address)
	portNumber, portErr := strconv.Atoi(port)
	if err != nil || host == "" || strings.ContainsAny(host, "@/ \t\r\n") || portErr != nil || portNumber < 1 || portNumber > 65535 {
		return errors.New("cache.redis.address must be host:port")
	}
	name := regexp.MustCompile(`^[a-zA-Z0-9_-]{1,64}$`)
	if !name.MatchString(r.Namespace) || !name.MatchString(r.Generation) {
		return errors.New("cache.redis requires namespace and generation of 1-64 letters, digits, underscores or hyphens")
	}
	if r.TimeoutMS < 0 || r.OperationTimeoutMS() > 5000 {
		return errors.New("cache.redis.timeout_ms must be 1-5000 when provided")
	}
	if r.RetentionSeconds < 0 || r.Retention() < maxAge || r.Retention() > 604800 {
		return errors.New("cache.redis.retention_seconds must cover max_age_seconds and be at most 604800")
	}
	for _, path := range []string{r.PasswordFile, r.CAFile} {
		if path != "" && !filepath.IsAbs(path) {
			return errors.New("Redis credential and CA files must be absolute paths")
		}
	}
	if r.CAFile != "" && !r.TLS {
		return errors.New("cache.redis.ca_file requires tls")
	}
	return nil
}
