#!/bin/bash
# =============================================================================
# Create databases for SSO services (Keycloak)
# =============================================================================
# Runs inside the postgres container via docker-entrypoint-initdb.d/ on first
# start. For existing deployments, use: python init.py (which calls
# orchestrator.init.ensure_sso_databases()).
# Nextcloud uses SQLite (self-contained in its own PVC).
# =============================================================================
set -e

echo "Creating SSO databases..."

# Keycloak database
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "postgres" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'keycloak') THEN
            CREATE ROLE keycloak WITH LOGIN PASSWORD 'keycloak';
        END IF;
    END\$\$;

    SELECT 'CREATE DATABASE keycloak OWNER keycloak'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'keycloak')
    \gexec
EOSQL

echo "  keycloak: ok"
echo "SSO databases ready."
