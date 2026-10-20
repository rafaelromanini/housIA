{{- define "housia.labels" -}}
app.kubernetes.io/name: housia
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}

{{- define "housia.secretName" -}}
{{- if .Values.secrets.existingSecret -}}{{ .Values.secrets.existingSecret }}{{- else -}}{{ .Release.Name }}-secrets{{- end -}}
{{- end }}

{{- define "housia.domain" -}}
{{- required "defina domain (nome do site)" .Values.domain -}}
{{- end }}

{{- define "housia.fhirUrl" -}}
{{- if .Values.fhir.internal -}}http://{{ .Release.Name }}-fhir:8080/fhir{{- else -}}{{ required "defina fhir.baseUrl quando fhir.internal=false" .Values.fhir.baseUrl }}{{- end -}}
{{- end }}
