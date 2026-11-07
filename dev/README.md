# Development environment

Docker stacks used while working on the Resend connector.

Odoo 20 stack (default):

* Odoo 20.0 on http://localhost:8069, database odoo_resend, login admin/admin
* PostgreSQL 17
* Mailpit on http://localhost:8025 for SMTP checks
* The module is mounted at /mnt/extra-addons/rex_resend_connector

Odoo 19 stack (docker-compose.19.yml): same module, port 8070, database
odoo_resend19.

## Start

    docker compose up -d db
    docker compose run --rm odoo odoo -d odoo_resend -i rex_resend_connector --stop-after-init
    docker compose up -d odoo mailpit

Set the administrator password on a new database:

    docker compose exec -T odoo odoo shell -d odoo_resend --no-http <<'EOF'
    env.ref('base.user_admin').write({'password': 'admin'})
    env.cr.commit()
    EOF

Odoo 19:

    docker compose -f docker-compose.19.yml up -d db
    docker compose -f docker-compose.19.yml run --rm odoo odoo -d odoo_resend19 -i rex_resend_connector --stop-after-init
    docker compose -f docker-compose.19.yml up -d odoo

## Run the tests

    docker compose run --rm -T odoo odoo -d odoo_resend -u rex_resend_connector \
        --test-tags /rex_resend_connector --stop-after-init

For Odoo 19 use the same command with -f docker-compose.19.yml and the odoo_resend19
database.

## Test the module without a Resend account

mock_resend.py is a small local server that answers the endpoints used by the
module: send email, domains, webhooks and receiving. The end-to-end script
configures the module to use it, sends messages through the normal Odoo pipeline,
posts signed webhooks and imports an incoming message.

    python3 mock_resend.py &
    docker compose exec -T odoo odoo shell -d odoo_resend --no-http < e2e_mock_test.py

The last line prints TOTAL FAILURES: 0 when everything passed. With the Odoo 19
stack, run the script in a one-off container and point it at the server container:

    docker compose -f docker-compose.19.yml run --rm -T -e RESEND_WEB=http://odoo:8069 \
        odoo odoo shell -d odoo_resend19 --no-http < e2e_mock_test.py

## Test against the Resend API

    RESEND_API_KEY=re_xxx docker compose exec -T -e RESEND_API_KEY odoo \
        odoo shell -d odoo_resend --no-http < real_resend_test.py

The script checks the key, sends one email through Odoo to delivered@resend.dev,
reads the delivery status from the API and shows whether Resend kept the Odoo
Message-Id (it does not; the module tracks the Resend one). It uses Resend quota.

## Test the real webhook delivery

Resend has to reach the Odoo webhook, so expose the port with a tunnel:

    cloudflared tunnel --url http://localhost:8069 --no-autoupdate

Set the public address in Settings > Discuss > Resend > Public Base URL, click
"Create / update webhook in Resend", then send an email and check the events in
Resend > Configuration > Webhook Events.

## Notes

* The mock server keeps its state in memory; restart it to start clean.
* Python files are loaded when the container starts, restart Odoo after changing
  the module.
* The module folder is mounted from the parent directory; keep both stacks stopped
  when switching versions on the same port.
