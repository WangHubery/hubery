#!/bin/sh
# Install the Sunday 20:00 Asia/Shanghai agent-update check.
# The host clock is UTC. Cron on this image ignores CRON_TZ, so the
# job is 12:00 UTC Sunday, and the script checks Shanghai time itself.
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
install -m 755 "$root/scripts/check_agent_updates.py" /usr/local/bin/check-agent-updates.py
install -m 644 -o root -g root "$root/deploy/agent-update-check.cron" /etc/cron.d/agent-update-check

if grep -q '^session[[:space:]]\+required[[:space:]]\+pam_loginuid.so' /etc/pam.d/cron; then
  sed -i 's/^session[[:space:]]\+required[[:space:]]\+pam_loginuid.so/session optional pam_loginuid.so/' /etc/pam.d/cron
fi

mkdir -p /home/ubuntu/.local/state /home/ubuntu/.config/agent-update-check
if [ ! -f /home/ubuntu/.config/agent-update-check/smtp.env ]; then
  cat > /home/ubuntu/.config/agent-update-check/smtp.env << 'EOF'
SMTP_HOST=smtp.qq.com
SMTP_PORT=587
SMTP_USER=28854583@qq.com
SMTP_FROM=28854583@qq.com
SMTP_PASSWORD=
EOF
  chown ubuntu:ubuntu /home/ubuntu/.config/agent-update-check/smtp.env
  chmod 600 /home/ubuntu/.config/agent-update-check/smtp.env
fi

cat > /etc/profile.d/start-agent-cron.sh << 'EOF'
if ! pgrep -x cron >/dev/null 2>&1; then
  /usr/sbin/cron >/dev/null 2>&1 || true
fi
EOF
chmod 644 /etc/profile.d/start-agent-cron.sh

if ! pgrep -x cron >/dev/null 2>&1; then
  /usr/sbin/cron
fi
