#!/bin/sh
# Generates /tmp/turnserver.conf from environment variables and starts Coturn.
#
# Credentials are read from the container environment (supplied via env_file
# in docker-compose.yml).  They do not appear in command-line arguments, which
# are fully visible in `docker inspect .Args` and in host process listings.
# They are only present in the container's .Config.Env — the same exposure
# level as every other service in this project that uses env_file.
set -eu

: "${TURN_AUTH_SECRET:?TURN_AUTH_SECRET must be set in .env — generate with: openssl rand -hex 32}"
: "${TURN_REALM:?TURN_REALM must be set in .env (e.g. chatapp.local for dev, your domain for prod)}"
: "${TURN_EXTERNAL_IP:?TURN_EXTERNAL_IP must be set in .env (127.0.0.1 for local dev; public IP for production)}"

TURN_MIN_PORT="${TURN_MIN_PORT:-49160}"
TURN_MAX_PORT="${TURN_MAX_PORT:-49200}"

# Validate relay port configuration before writing the config file.
# Uses POSIX sh case-pattern matching (no bashisms) to check for non-digit
# characters, then test's integer comparison for the value range.
validate_port() {
    _val="$1"
    _name="$2"
    case "$_val" in
        ''|*[!0-9]*)
            printf 'ERROR: %s must contain only digits, got: "%s"\n' "$_name" "$_val" >&2
            exit 1
            ;;
    esac
    if [ "$_val" -lt 1 ] || [ "$_val" -gt 65535 ]; then
        printf 'ERROR: %s must be between 1 and 65535, got: %s\n' "$_name" "$_val" >&2
        exit 1
    fi
}

validate_port "$TURN_MIN_PORT" "TURN_MIN_PORT"
validate_port "$TURN_MAX_PORT" "TURN_MAX_PORT"
if [ "$TURN_MIN_PORT" -gt "$TURN_MAX_PORT" ]; then
    printf 'ERROR: TURN_MIN_PORT (%s) must be <= TURN_MAX_PORT (%s)\n' \
        "$TURN_MIN_PORT" "$TURN_MAX_PORT" >&2
    exit 1
fi

# Write the config with restrictive permissions so the auth secret is not
# world-readable inside the container.  umask 077 forces mode 600 on any
# file created by this process from this point forward.
umask 077
cat > /tmp/turnserver.conf <<EOF
# Generated at container startup — do not edit manually.
# Edit backend/coturn/entrypoint.sh to change server options.

# REST API short-term credential mode.
# The Django backend (apps/users/turn_credentials.py) generates time-limited
# HMAC tokens signed with TURN_AUTH_SECRET.  The frontend (CallContext.jsx)
# fetches a fresh token per call instead of using a permanent password.
use-auth-secret
static-auth-secret=${TURN_AUTH_SECRET}

realm=${TURN_REALM}
external-ip=${TURN_EXTERNAL_IP}

min-port=${TURN_MIN_PORT}
max-port=${TURN_MAX_PORT}

no-cli
log-file=stdout
fingerprint
EOF
# Belt-and-suspenders: explicitly set 600 so a restart that finds an existing
# file (from a prior container run on the same writeable layer) with wrong
# permissions is corrected before turnserver reads it.
chmod 600 /tmp/turnserver.conf

exec turnserver -c /tmp/turnserver.conf
