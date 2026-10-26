{{- define "webcappi.durationMS" -}}
{{- $number := regexFind "^[0-9]+" . | int -}}
{{- $unit := regexFind "[a-z]+$" . -}}
{{- mul $number (index (dict "ms" 1 "s" 1000 "m" 60000 "h" 3600000) $unit) -}}
{{- end -}}

{{- define "webcappi.validate" -}}
{{- if .Values.ingress.enabled -}}
{{- if not (.Capabilities.APIVersions.Has "networking.k8s.io/v1") -}}
{{- fail "ingress requires networking.k8s.io/v1" -}}
{{- end -}}
{{- if empty .Values.ingress.hosts -}}{{- fail "ingress requires hosts and paths" -}}{{- end -}}
{{- range .Values.ingress.hosts -}}
{{- if or (empty .host) (empty .paths) -}}{{- fail "ingress requires hosts and paths" -}}{{- end -}}
{{- range .paths -}}
{{- if or (not (hasPrefix "/" .path)) (not (has .pathType (list "Prefix" "Exact" "ImplementationSpecific"))) -}}
{{- fail "ingress requires absolute paths and supported pathType" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if .Values.httpRoute.enabled -}}
{{- if not (.Capabilities.APIVersions.Has "gateway.networking.k8s.io/v1/HTTPRoute") -}}
{{- fail "httpRoute requires gateway.networking.k8s.io/v1/HTTPRoute; install Gateway API CRDs first or pass --api-versions for offline rendering" -}}
{{- end -}}
{{- if empty .Values.httpRoute.parentRefs -}}{{- fail "httpRoute requires parentRefs" -}}{{- end -}}
{{- range .Values.httpRoute.parentRefs -}}
{{- if empty .name -}}{{- fail "httpRoute parentRefs requires name" -}}{{- end -}}
{{- end -}}
{{- if empty .Values.httpRoute.paths -}}{{- fail "httpRoute requires paths" -}}{{- end -}}
{{- range .Values.httpRoute.paths -}}
{{- if or (not (hasPrefix "/" .value)) (not (has .type (list "PathPrefix" "Exact"))) -}}
{{- fail "httpRoute requires absolute paths and supported match type" -}}
{{- end -}}
{{- end -}}
{{- $timeouts := .Values.httpRoute.timeouts -}}
{{- range $key, $duration := $timeouts -}}
{{- if not (regexMatch "^[0-9]{1,5}(ms|s|m|h)$" $duration) -}}{{- fail "httpRoute timeouts require an integer duration with ms, s, m or h" -}}{{- end -}}
{{- end -}}
{{- if and (hasKey $timeouts "request") (hasKey $timeouts "backendRequest") -}}
{{- $request := include "webcappi.durationMS" $timeouts.request | int64 -}}
{{- $backend := include "webcappi.durationMS" $timeouts.backendRequest | int64 -}}
{{- if and (gt $request 0) (or (eq $backend 0) (gt $backend $request)) -}}
{{- fail "httpRoute backendRequest must not exceed a nonzero request timeout" -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- range $name, $profile := dict "seccompProfile" .Values.browser.seccompProfile "appArmorProfile" .Values.browser.appArmorProfile -}}
{{- $allowed := list "RuntimeDefault" "Localhost" -}}
{{- if eq $name "appArmorProfile" -}}{{- $allowed = append $allowed "" -}}{{- end -}}
{{- if not (has $profile.type $allowed) -}}
{{- fail (printf "browser.%s.type must select a supported confined profile" $name) -}}
{{- end -}}
{{- if eq $profile.type "Localhost" -}}
{{- $pattern := "^[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*$" -}}
{{- if eq $name "appArmorProfile" -}}{{- $pattern = "^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$" -}}{{- end -}}
{{- if not (regexMatch $pattern $profile.localhostProfile) -}}
{{- fail (printf "browser.%s requires a complete, clean Localhost reference" $name) -}}
{{- end -}}
{{- else if $profile.localhostProfile -}}
{{- fail (printf "browser.%s.localhostProfile is only valid for Localhost" $name) -}}
{{- end -}}
{{- end -}}
{{- range $name, $_ := .Values.podAnnotations -}}
{{- if regexMatch "^(container\\.apparmor\\.security\\.beta\\.kubernetes\\.io/|(?:container\\.)?seccomp\\.security\\.alpha\\.kubernetes\\.io/)" $name -}}
{{- fail "podAnnotations must not override security profiles; use browser profile references" -}}
{{- end -}}
{{- end -}}
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
