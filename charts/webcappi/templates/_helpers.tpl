{{- define "webcappi.fullname" -}}
{{- printf "%s-webcappi" .Release.Name | trunc 54 | trimSuffix "-" -}}
{{- end -}}

{{- define "webcappi.selectorLabels" -}}
app.kubernetes.io/name: webcappi
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "webcappi.labels" -}}
{{ include "webcappi.selectorLabels" . }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | quote }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "webcappi.image" -}}
{{- if .digest -}}
{{- printf "%s@%s" .repository .digest -}}
{{- else -}}
{{- printf "%s:%s" .repository .tag -}}
{{- end -}}
{{- end -}}

{{- define "webcappi.config" -}}
{{- $config := dict "listen" ":8080" "browser_path" "/opt/chrome/chrome" "no_sandbox" .Values.browser.noSandbox "work_dir" "/work/captures" "profiles" .Values.profiles.descriptors -}}
{{- $fields := dict "concurrency" "concurrency" "queueSize" "queue_size" "maxRequests" "max_requests" "timeoutSeconds" "capture_timeout_seconds" "metadataMaxBytes" "metadata_max_bytes" "metadataMaxEntries" "metadata_max_entries" "maxAgeSeconds" "max_age_seconds" "maxWidth" "max_width" "maxHeight" "max_height" "maxPixels" "max_pixels" "resizeAlgorithm" "resize_algorithm" "maxProfileBytes" "max_profile_bytes" "maxPOSTBytes" "max_post_bytes" "maxPNGBytes" "max_png_bytes" -}}
{{- range $value, $field := $fields -}}
{{- $_ := set $config $field (index $.Values.capture $value) -}}
{{- end -}}
{{- if .Values.auth.enabled -}}
{{- $_ := set $config "auth" (dict "bearer_token_file" "/run/secrets/webcappi/token") -}}
{{- end -}}
{{- $cache := dict "backend" .Values.cache.backend -}}
{{- if eq .Values.cache.backend "redis" -}}
{{- $r := .Values.cache.redis -}}
{{- $redis := dict "address" $r.address "namespace" $r.namespace "generation" $r.generation "username" $r.username "tls" $r.tls "timeout_ms" $r.timeoutMS "retention_seconds" $r.retentionSeconds -}}
{{- if $r.passwordSecret.name -}}
{{- $_ := set $redis "password_file" "/run/secrets/redis/password" -}}
{{- end -}}
{{- if $r.ca.name -}}
{{- $_ := set $redis "ca_file" "/run/redis-ca/ca.crt" -}}
{{- end -}}
{{- $_ := set $cache "redis" $redis -}}
{{- end -}}
{{- $_ := set $config "cache" $cache -}}
{{- $config | mustToPrettyJson -}}
{{- end -}}
