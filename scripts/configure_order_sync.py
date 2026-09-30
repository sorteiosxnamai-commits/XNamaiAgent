"""Install/update the XNamai order sync schedule; secrets stay in Vault.

Requires DATABASE_URL and the existing REMARKETING_CRON_SECRET (or CRON_SECRET).
Run after deploying the authenticated order cron endpoint and its migration.
"""
from __future__ import annotations

import argparse
import os
from urllib.parse import urlparse

import psycopg

JOB_NAME = "xnamai-mercos-order-sync"
SECRET_NAME = "xnamai_mercos_order_sync_cron"


def configure(conn, *, base_url: str, secret: str):
    parsed = urlparse(base_url)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("https_base_url_required")
    if not secret:
        raise ValueError("cron_secret_required")
    endpoint = base_url.rstrip("/") + "/api/cron/commerce/sync/orders"
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM vault.secrets WHERE name=%s", (SECRET_NAME,))
        existing = cur.fetchone()
        if existing:
            cur.execute("SELECT vault.update_secret(%s,%s)", (existing[0], secret))
        else:
            cur.execute("SELECT vault.create_secret(%s,%s)", (secret, SECRET_NAME))
        # Quote only the public URL/name into the cron command. Never interpolate
        # a credential: pg_cron's stored command resolves it from Vault at runtime.
        from psycopg import sql
        command = sql.SQL("""SELECT net.http_post(
            url := {}, headers := jsonb_build_object('Content-Type','application/json',
                'Authorization','Bearer ' || (SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name={})),
            body := '{{}}'::jsonb, timeout_milliseconds := 250000);""").format(
                sql.Literal(endpoint), sql.Literal(SECRET_NAME)).as_string(conn)
        cur.execute("SELECT cron.schedule(%s,%s,%s)", (JOB_NAME, "*/2 * * * *", command))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    database = os.environ.get("DATABASE_URL", "")
    secret = os.environ.get("REMARKETING_CRON_SECRET") or os.environ.get("CRON_SECRET", "")
    if not database or not secret:
        raise SystemExit("DATABASE_URL and cron secret required")
    try:
        with psycopg.connect(database, prepare_threshold=None, connect_timeout=10) as conn:
            configure(conn, base_url=args.base_url, secret=secret)
    except Exception as exc:
        # Driver errors may contain SQL parameters; output the type only.
        raise SystemExit("order_sync_setup_failed:" + type(exc).__name__) from None
    print("order_sync_schedule_configured: every 2 minutes")


if __name__ == "__main__":
    main()
