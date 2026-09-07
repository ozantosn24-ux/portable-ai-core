#!/usr/bin/env bash
# End-to-end login + authorization check, driven by curl the way a browser would.
#
#   scripts/idp_check.sh alice
#
# It follows the real redirect chain: /auth/login -> Keycloak -> login form POST ->
# /auth/callback -> /me. Nothing is called directly; if a Location header is wrong the
# check fails here rather than passing on a handler that a browser could never reach.
#
# Prints one PASS/FAIL line per assertion and exits non-zero on the first failure, so a
# green run cannot be produced by a step that silently did nothing.
set -euo pipefail

USER_NAME="${1:-alice}"
APP="http://app.drill.internal:8000"
IDP="http://idp.drill.internal:8080"
JAR="$(mktemp)"; KCJAR="$(mktemp)"
trap 'rm -f "$JAR" "$KCJAR"' EXIT

case "$USER_NAME" in
  alice) PASSWORD="drill-not-a-real-password-alice" ;;
  bob)   PASSWORD="drill-not-a-real-password-bob" ;;
  mia)   PASSWORD="drill-not-a-real-password-mia" ;;
  *) echo "unknown drill user: $USER_NAME" >&2; exit 2 ;;
esac

fails=0
check() { # check <label> <actual> <expected>
  if [ "$2" = "$3" ]; then
    echo "  PASS  $1 -> $2"
  else
    echo "  FAIL  $1 -> got '$2', expected '$3'"
    fails=$((fails + 1))
  fi
}

echo "== login as $USER_NAME =="

# 1) RP starts the flow and hands us the IdP URL (plus a pre-auth session cookie).
AUTH_URL="$(curl -s -c "$JAR" -o /dev/null -w '%{redirect_url}' "$APP/auth/login")"
[ -n "$AUTH_URL" ] || { echo "FAIL: /auth/login returned no Location"; exit 1; }
case "$AUTH_URL" in
  "$IDP"/realms/drill/protocol/openid-connect/auth*) echo "  PASS  authorize URL points at the realm" ;;
  *) echo "  FAIL  unexpected authorize URL: $AUTH_URL"; exit 1 ;;
esac
grep -q 'code_challenge_method=S256' <<<"$AUTH_URL" && echo "  PASS  PKCE S256 requested" || {
  echo "  FAIL  no S256 challenge in the authorize URL"; exit 1; }

# 2) Keycloak renders its login form; pull the action URL out of it.
LOGIN_HTML="$(curl -s -c "$KCJAR" "$AUTH_URL")"
FORM_ACTION="$(grep -o 'action="[^"]*"' <<<"$LOGIN_HTML" | head -1 | sed 's/^action="//; s/"$//' | sed 's/&amp;/\&/g')"
[ -n "$FORM_ACTION" ] || { echo "FAIL: no login form found on the IdP page"; exit 1; }

# 3) Submit the credentials; Keycloak redirects back to the RP with code + state.
CALLBACK_URL="$(curl -s -b "$KCJAR" -c "$KCJAR" -o /dev/null -w '%{redirect_url}' \
  --data-urlencode "username=$USER_NAME" --data-urlencode "password=$PASSWORD" \
  --data-urlencode "credentialId=" "$FORM_ACTION")"
case "$CALLBACK_URL" in
  "$APP"/auth/callback*code=*) echo "  PASS  IdP redirected back with an authorization code" ;;
  *) echo "  FAIL  unexpected callback URL: ${CALLBACK_URL:-<empty>}"; exit 1 ;;
esac

# 4) The RP validates state, nonce, signature and claims, then rotates the session.
CB_CODE="$(curl -s -b "$JAR" -c "$JAR" -o /dev/null -w '%{http_code}' "$CALLBACK_URL")"
check "/auth/callback status" "$CB_CODE" "303"

ME="$(curl -s -b "$JAR" "$APP/me")"
echo "  /me -> $ME"
SUBJECT="$(grep -o '"user_id":"[^"]*"' <<<"$ME" | sed 's/.*:"//; s/"//')"
CSRF="$(grep -o '"csrf_token":"[^"]*"' <<<"$ME" | sed 's/.*:"//; s/"//')"
[ -n "$SUBJECT" ] || { echo "FAIL: /me carries no principal"; exit 1; }
echo "  PASS  session established for sub=$SUBJECT"

status() { curl -s -b "$JAR" -o /dev/null -w '%{http_code}' "$@"; }

echo "== resource authorization =="
case "$USER_NAME" in
  alice)
    check "GET /mailboxes/sales-a/summary" "$(status "$APP/mailboxes/sales-a/summary")" "200"
    check "GET /mailboxes/sales-b/summary" "$(status "$APP/mailboxes/sales-b/summary")" "403"
    check "POST /mailboxes/sales-a/drafts" \
      "$(status -X POST -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
         -d '{"subject":"drill","body":"drill"}' "$APP/mailboxes/sales-a/drafts")" "201"
    check "POST drafts without CSRF" \
      "$(status -X POST -H 'Content-Type: application/json' \
         -d '{"subject":"drill","body":"drill"}' "$APP/mailboxes/sales-a/drafts")" "403"
    ;;
  bob)
    check "GET /mailboxes/sales-b/summary" "$(status "$APP/mailboxes/sales-b/summary")" "200"
    check "GET /mailboxes/sales-a/summary" "$(status "$APP/mailboxes/sales-a/summary")" "403"
    ;;
  mia)
    check "GET /mailboxes/sales-a/summary" "$(status "$APP/mailboxes/sales-a/summary")" "200"
    check "GET /mailboxes/sales-b/summary" "$(status "$APP/mailboxes/sales-b/summary")" "200"
    check "POST /mailboxes/sales-a/drafts" \
      "$(status -X POST -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
         -d '{"subject":"drill","body":"drill"}' "$APP/mailboxes/sales-a/drafts")" "403"
    ;;
esac

echo "== logout =="
check "POST /auth/logout (with CSRF)" "$(status -X POST -H "X-CSRF-Token: $CSRF" "$APP/auth/logout")" "204"
# Sunucu tarafı kayıt gitti: ESKİ çerez artık ölü olmalı.
check "GET /me after logout" "$(status "$APP/me")" "401"

[ "$fails" -eq 0 ] || { echo "FAILED assertions: $fails"; exit 1; }
echo "ALL CHECKS PASSED for $USER_NAME"
