#!/bin/bash
# feed.sh - Trigger a feed cycle via the controller's HTTP API.
# Intended for use with cron on a Raspberry Pi.
#
# Cron entry (run `crontab -e` and add):
#   0 18 * * * /home/user/pet_feeder/feed.sh >> /home/user/pet_feeder/scheduled_feeds.log 2>&1
#
# Make sure the system timezone is set to Europe/Helsinki:
#   sudo timedatectl set-timezone Europe/Helsinki

CONTROLLER_URL="http://localhost:8000/trigger_feed"
MAX_RETRIES=3
RETRY_DELAY=5

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Triggering scheduled feed..."

for attempt in $(seq 1 "$MAX_RETRIES"); do
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$CONTROLLER_URL")

    if [ "$HTTP_CODE" = "200" ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Feed triggered successfully."
        exit 0
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Attempt $attempt/$MAX_RETRIES failed (HTTP $HTTP_CODE). Retrying in ${RETRY_DELAY}s ..."
    sleep "$RETRY_DELAY"
done

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: Feed trigger failed after $MAX_RETRIES attempts."
exit 1
