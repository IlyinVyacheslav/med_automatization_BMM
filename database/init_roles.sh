#!/bin/bash
set -e

: "${DB_PASSWORD:?нужен DB_PASSWORD (пароль ai_bot_registrar)}"
: "${REGISTRAR_PASSWORD:?нужен REGISTRAR_PASSWORD}"
: "${CLINIC_ADMIN_PASSWORD:?нужен CLINIC_ADMIN_PASSWORD}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$DB_NAME" \
     -v bot_pw="$DB_PASSWORD" -v reg_pw="$REGISTRAR_PASSWORD" -v adm_pw="$CLINIC_ADMIN_PASSWORD" <<'SQL'
ALTER ROLE ai_bot_registrar LOGIN PASSWORD :'bot_pw';
ALTER ROLE registrar        LOGIN PASSWORD :'reg_pw';
ALTER ROLE admin            LOGIN PASSWORD :'adm_pw';
SQL

echo "Роли ai_bot_registrar / registrar / admin переведены в LOGIN с паролями из .env."