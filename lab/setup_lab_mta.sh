#!/usr/bin/env bash
# Controlled SMTP lab: a REAL Postfix MTA (+ postgrey) on 127.0.0.1:25 hosting test domains
# that we control. Used to test emailish's SMTP engine against a real mail server without
# touching any third-party server.
#
# WARNING: this rewrites /etc/postfix/main.cf. Run it ONLY in a disposable container or VM
# (e.g. a CI runner or the dev container), never on a machine whose mail you care about.
#
#   sudo apt-get install -y postfix postgrey     # Debian/Ubuntu, "No configuration" is fine
#   sudo bash lab/setup_lab_mta.sh               # (re)configure and (re)start
#   sudo bash lab/setup_lab_mta.sh stop
set -euo pipefail
PF=/etc/postfix

if [ "${1:-}" = "stop" ]; then
  postfix stop 2>/dev/null || true
  [ -f /var/run/postgrey/lab.pid ] && kill "$(cat /var/run/postgrey/lab.pid)" 2>/dev/null || true
  exit 0
fi

# Lab domains (".mock" is not a real TLD, so nothing here can collide with real mail):
#   lab-valid.mock    mailboxes: alice, bob, info; special: full@, disabled@
#   lab-catchall.mock accepts ANY recipient (virtual alias catch-all)
#   lab-grey.mock     mailbox carol, behind postgrey greylisting (5 s delay)
#   lab-temp.mock     every recipient gets a temporary 451 failure
#   lab-blocked.mock  every recipient gets a 554 5.7.1 blocklist-style rejection
cat > $PF/main.cf <<'EOF'
compatibility_level = 3.6
myhostname = mx.lab.mock
mydomain = lab.mock
myorigin = $mydomain
mydestination =
inet_interfaces = 127.0.0.1
inet_protocols = ipv4
# Treat localhost as an ordinary internet client (no relay or trust privileges).
mynetworks = 192.0.2.1/32
smtpd_client_event_limit_exceptions =
smtpd_banner = $myhostname ESMTP lab
biff = no
append_dot_mydomain = no
readme_directory = no
maillog_file = /var/log/postfix-lab.log

virtual_mailbox_domains = lab-valid.mock lab-grey.mock lab-temp.mock lab-blocked.mock
virtual_mailbox_maps = hash:/etc/postfix/lab_vmailbox
virtual_mailbox_base = /var/mail/lab
virtual_alias_domains = lab-catchall.mock
virtual_alias_maps = hash:/etc/postfix/lab_valias
virtual_transport = discard:

smtpd_helo_required = yes
smtpd_helo_restrictions = reject_invalid_helo_hostname, reject_non_fqdn_helo_hostname
smtpd_restriction_classes = greylist
greylist = check_policy_service inet:127.0.0.1:10023
smtpd_recipient_restrictions = check_recipient_access hash:/etc/postfix/lab_access, reject_unauth_destination
smtpd_client_recipient_rate_limit = ${LAB_RCPT_RATE_LIMIT}
anvil_rate_time_unit = 60s
EOF
sed -i "s/\${LAB_RCPT_RATE_LIMIT}/${LAB_RCPT_RATE_LIMIT:-0}/" $PF/main.cf

cat > $PF/lab_vmailbox <<'EOF'
alice@lab-valid.mock     lab-valid.mock/alice/
bob@lab-valid.mock       lab-valid.mock/bob/
info@lab-valid.mock      lab-valid.mock/info/
full@lab-valid.mock      lab-valid.mock/full/
disabled@lab-valid.mock  lab-valid.mock/disabled/
carol@lab-grey.mock      lab-grey.mock/carol/
dave@lab-temp.mock       lab-temp.mock/dave/
erin@lab-blocked.mock    lab-blocked.mock/erin/
EOF
cat > $PF/lab_valias <<'EOF'
@lab-catchall.mock       alice@lab-valid.mock
EOF
cat > $PF/lab_access <<'EOF'
full@lab-valid.mock      552 5.2.2 Mailbox full
disabled@lab-valid.mock  550 5.2.1 Mailbox disabled, not accepting messages
lab-grey.mock            greylist
lab-temp.mock            451 4.3.0 Temporary lookup failure
lab-blocked.mock         554 5.7.1 Service unavailable; Client host [127.0.0.1] blocked using zen.spamhaus.org
EOF
postmap $PF/lab_vmailbox $PF/lab_valias $PF/lab_access
grep -q "^discard " $PF/master.cf || echo "discard   unix  -       -       n       -       -       discard" >> $PF/master.cf
newaliases 2>/dev/null || true

# (re)start postgrey only if it is not already running; wait for the old instance to release the port
if ! (echo > /dev/tcp/127.0.0.1/10023) 2>/dev/null; then
  mkdir -p /var/run/postgrey
  postgrey --inet=127.0.0.1:10023 --delay=5 --auto-whitelist-clients=0 -d --pidfile=/var/run/postgrey/lab.pid
  for i in $(seq 1 20); do (echo > /dev/tcp/127.0.0.1/10023) 2>/dev/null && break; sleep 0.25; done
fi

postfix check
if postfix status >/dev/null 2>&1; then postfix reload; else postfix start; fi
sleep 1
postfix status
echo "lab MTA ready on 127.0.0.1:25 (rate limit: ${LAB_RCPT_RATE_LIMIT:-0} recipients/min, 0 = off)"
