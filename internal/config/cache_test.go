package config

import "testing"

func TestCacheConfiguration(t *testing.T) {
	validRedis := func() Cache {
		return Cache{Backend: "redis", Redis: &Redis{Address: "redis:6379", Namespace: "production", Generation: "v1"}}
	}
	for _, tc := range []struct {
		name   string
		change func(*Cache)
		valid  bool
	}{
		{"defaults", func(c *Cache) { *c = Cache{} }, true},
		{"memory", func(c *Cache) { *c = Cache{Backend: "memory"} }, true},
		{"redis", func(*Cache) {}, true},
		{"unknown", func(c *Cache) { c.Backend = "unknown" }, false},
		{"missing redis", func(c *Cache) { c.Redis = nil }, false},
		{"unused redis", func(c *Cache) { c.Backend = "memory" }, false},
		{"missing namespace", func(c *Cache) { c.Redis.Namespace = "" }, false},
		{"unsafe namespace", func(c *Cache) { c.Redis.Namespace = "{production}" }, false},
		{"missing generation", func(c *Cache) { c.Redis.Generation = "" }, false},
		{"invalid address", func(c *Cache) { c.Redis.Address = "redis" }, false},
		{"invalid port", func(c *Cache) { c.Redis.Address = "redis:invalid" }, false},
		{"invalid port range", func(c *Cache) { c.Redis.Address = "redis:65536" }, false},
		{"credentials in address", func(c *Cache) { c.Redis.Address = "secret@redis:6379" }, false},
		{"negative timeout", func(c *Cache) { c.Redis.TimeoutMS = -1 }, false},
		{"excessive timeout", func(c *Cache) { c.Redis.TimeoutMS = 5001 }, false},
		{"short retention", func(c *Cache) { c.Redis.RetentionSeconds = 59 }, false},
		{"excessive retention", func(c *Cache) { c.Redis.RetentionSeconds = 604801 }, false},
		{"relative password", func(c *Cache) { c.Redis.PasswordFile = "password" }, false},
		{"relative CA", func(c *Cache) { c.Redis.TLS = true; c.Redis.CAFile = "ca.pem" }, false},
		{"CA without TLS", func(c *Cache) { c.Redis.CAFile = "/run/ca.pem" }, false},
		{"TLS", func(c *Cache) { c.Redis.TLS = true; c.Redis.CAFile = "/run/ca.pem" }, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := validRedis()
			tc.change(&c)
			if err := c.Validate(60); (err == nil) != tc.valid {
				t.Fatalf("valid=%v: %v", tc.valid, err)
			}
		})
	}
}
