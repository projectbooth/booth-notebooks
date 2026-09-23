{{/*
Standard name/label helpers, the same shape every booth-* chart uses. Components: hub, proxy, and
singleuser (the per-user notebook pods, created by the hub, not by Helm).
*/}}

{{- define "booth-notebooks.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "booth-notebooks.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "booth-notebooks.labels" -}}
app.kubernetes.io/name: {{ include "booth-notebooks.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "booth-notebooks.selectorLabels" -}}
app.kubernetes.io/name: {{ include "booth-notebooks.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Notebook pods are labelled by KubeSpawner (component=singleuser-server) plus our part-of label. */}}
{{- define "booth-notebooks.singleuserSelector" -}}
component: singleuser-server
app.kubernetes.io/part-of: booth-notebooks
{{- end -}}

{{- define "booth-notebooks.hubSecretName" -}}
{{- .Values.hub.secret.name | default (printf "%s-hub" (include "booth-notebooks.fullname" .)) -}}
{{- end -}}

{{- define "booth-notebooks.hubUrl" -}}
http://{{ include "booth-notebooks.fullname" . }}-hub:8081
{{- end -}}
