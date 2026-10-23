#!/bin/sh
# Fill a THROWAWAY database with realistic demo tasks for screenshots and the demo GIF.
# Usage:  docs/media/seed-demo.sh /tmp/ftask-demo.db  &&  FTASK_DB=/tmp/ftask-demo.db ftask serve --port 4750
# Never touches your real board: it refuses to run without an explicit path, and refuses an existing file.
set -eu
db=${1:?usage: seed-demo.sh PATH_TO_NEW_DEMO_DB}
[ ! -e "$db" ] || { echo "refusing to overwrite $db" >&2; exit 2; }
export FTASK_DB="$db"
ftask=${FTASK:-ftask}

$ftask add --project Website --title "Renew the domain" --minutes 10 --priority 10 --key website/renew-domain --by website/agent \
  --why "The registrar needs a card and a 3-D Secure confirmation; renewal lapses on Friday." \
  --step "Open the registrar" --link https://registrar.example.com/domains \
  --step "Enter this coupon at checkout" --value "RENEW-2026" \
  --step "Pay and confirm the 3-D Secure prompt" >/dev/null

$ftask add --project Shop --title "Create the Stripe webhook" --minutes 4 --priority 20 --key shop/stripe-webhook --by shop/agent \
  --why "Orders are not marked paid until Stripe can reach the endpoint." \
  --step "Open the webhooks page" --link https://dashboard.stripe.example.com/webhooks \
  --step "Paste the endpoint URL" --value "https://api.example.com/hooks/stripe" \
  --step "Tick the event: checkout.session.completed" >/dev/null

$ftask add --project Mobile --title "Record the checkout flow" --minutes 5 --priority 40 --key mobile/checkout-recording --by mobile/agent \
  --why "The store reviewer asked for a screen recording of purchase." \
  --step "Record the checkout flow on a real device" \
  --step "Attach the recording here" --upload --accept "video/*" --label "Screen recording" >/dev/null

$ftask add --project Mobile --title "Wait for app review" --key mobile/review --by mobile/agent --waiting-on "App store review (24-72h)" >/dev/null
echo "seeded $db"
