{{- define "webcappi.validate" -}}
{{- if and (gt (int .Values.replicaCount) 1) (ne .Values.cache.backend "redis") -}}
{{- fail "replicaCount > 1 requires cache.backend=redis" -}}
{{- end -}}
{{- if and .Values.auth.enabled (or (empty .Values.auth.existingSecret) (empty .Values.auth.tokenKey)) -}}
{{- fail "auth.enabled requires auth.existingSecret and auth.tokenKey" -}}
{{- end -}}
{{- if not (or .Values.image.digest .Values.image.tag) -}}
{{- fail "image requires a fixed tag or digest" -}}
{{- end -}}
{{- if eq .Values.cache.backend "redis" -}}
{{- $r := .Values.cache.redis -}}
{{- if or (empty $r.address) (empty $r.namespace) (empty $r.generation) -}}
{{- fail "Redis requires address, namespace and explicit generation" -}}
{{- end -}}
{{- $port := regexFind "[0-9]+$" $r.address | int -}}
{{- if or (lt $port 1) (gt $port 65535) -}}
{{- fail "Redis address port must be between 1 and 65535" -}}
{{- end -}}
{{- if ne (empty $r.passwordSecret.name) (empty $r.passwordSecret.key) -}}
{{- fail "Redis passwordSecret requires both name and key" -}}
{{- end -}}
{{- if ne (empty $r.ca.name) (empty $r.ca.key) -}}
{{- fail "Redis CA requires both name and key" -}}
{{- end -}}
{{- if and $r.ca.name (not $r.tls) -}}
{{- fail "Redis CA requires TLS" -}}
{{- end -}}
{{- $retention := int $r.retentionSeconds -}}
{{- if eq $retention 0 -}}{{- $retention = 86400 -}}{{- end -}}
{{- if lt $retention (int .Values.capture.maxAgeSeconds) -}}
{{- fail "Redis retentionSeconds must cover capture.maxAgeSeconds" -}}
{{- end -}}
{{- end -}}
{{- $bundle := .Values.profiles.bundle -}}
{{- if or $bundle.repository $bundle.tag $bundle.digest -}}
{{- if not (and $bundle.repository (or $bundle.tag $bundle.digest)) -}}
{{- fail "profiles.bundle requires repository and an immutable tag or digest" -}}
{{- end -}}
{{- if empty .Values.profiles.descriptors -}}
{{- fail "profiles.bundle requires profile descriptors" -}}
{{- end -}}
{{- else if .Values.profiles.descriptors -}}
{{- fail "profile descriptors require profiles.bundle" -}}
{{- end -}}
{{- $profiles := .Values.profiles.descriptors -}}
{{- if and (eq (len $profiles) 128) (not (hasKey $profiles "default")) -}}
{{- fail "configure at most 128 profiles including the automatic default" -}}
{{- end -}}
{{- range $name, $p := $profiles -}}
{{- if or (ne (clean $p.user_data_dir) $p.user_data_dir) (not (hasPrefix "/bundles/" $p.user_data_dir)) -}}
{{- fail (printf "profile %s: user_data_dir must be a clean path below /bundles/" $name) -}}
{{- end -}}
{{- range $p.extensions -}}
{{- if or (ne (clean .) .) (not (hasPrefix "/bundles/" .)) (contains "," .) (eq . $p.user_data_dir) (hasPrefix (printf "%s/" $p.user_data_dir) .) (hasPrefix (printf "%s/" .) $p.user_data_dir) -}}
{{- fail (printf "profile %s: extension paths must be clean paths below /bundles/, separate from user_data_dir" $name) -}}
{{- end -}}
{{- end -}}
{{- range $p.ready_selectors -}}
{{- if or (empty (trim .)) (gt (len .) 1024) -}}
{{- fail (printf "profile %s: ready_selectors must contain 1-1024 nonblank bytes" $name) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if ge (len (include "webcappi.config" .)) 1048576 -}}
{{- fail "generated config.json must fit within the application's 1 MiB limit" -}}
{{- end -}}
{{- end -}}
