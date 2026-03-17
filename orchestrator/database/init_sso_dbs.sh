#!/bin/bash
# =============================================================================
# Create databases for SSO services (Keycloak, Nextcloud)
# =============================================================================
# Runs inside the postgres container via docker-entrypoint-initdb.d/ on first
# start. For existing deployments, use: python init.py (which calls
# orchestrator.init.ensure_sso_databases()).
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

# Nextcloud database (pre-created for Phase 3)
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "postgres" <<-EOSQL
    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'nextcloud') THEN
            CREATE ROLE nextcloud WITH LOGIN PASSWORD 'nextcloud';
        END IF;
    END\$\$;

    SELECT 'CREATE DATABASE nextcloud OWNER nextcloud'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'nextcloud')
    \gexec
EOSQL

echo "  nextcloud: ok"
echo "SSO databases ready."
