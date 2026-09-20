#!/bin/sh
# Declare the App Artifact (version 2, readiness-gated). Re-run only when port/perms change.
set -e
cat > /tmp/app.json <<EOF
{"version": 2, "host": "vm", "sandbox_id": "$PROMPTQL_SANDBOX_ID", "kind": "web", "port": 8080, "protocol": "http",
 "readiness": {"path": "/readyz"},
 "required_permissions": {"integrations": ["__typesafe-api"]}}
EOF
curl -s -o /tmp/app_resp.json -w "artifact PUT %{http_code}\n" -X PUT \
  "$PROMPTQL_PLATFORM_API_URL/v1/artifacts/threads/$PROMPTQL_THREAD_ID/off-label-radar" \
  -H "Authorization: Bearer $PROMPTQL_USER_JWT" -H "Content-Type: application/json" \
  -H "X-PromptQL-Artifact-Type: app" -H "X-PromptQL-Artifact-Title: Off-Label Radar" \
  --data-binary @/tmp/app.json
head -c 400 /tmp/app_resp.json; echo