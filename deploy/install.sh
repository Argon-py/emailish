#!/usr/bin/env bash
# One-command install of emailish on a fresh Ubuntu/Debian server that is allowed to use
# outbound port 25. This gives you the same kind of verification the commercial services run,
# on your own server, without per-email charges.
#
#   sudo bash install.sh --helo verify.yourdomain.com --mail-from verify-bounces@yourdomain.com \
#                        [--allow yourdomain.com] [--dir /opt/emailish] [--repo URL] [--dry-run]
#
# Before running: point an A record verify.yourdomain.com -> this server's IP, and set the
# server IP's reverse DNS (PTR) to verify.yourdomain.com in your hosting provider's panel.
# --allow limits SMTP to your own domains for a first controlled test (recommended).
set -euo pipefail

HELO="" ; MAIL_FROM="" ; ALLOW="" ; DIR=/opt/emailish ; DRY=0
REPO=https://github.com/Argon-py/emailish.git
ETC=${EMAILISH_ETC:-/etc/emailish} ; DATA=${EMAILISH_DATA:-/var/lib/emailish}
NO_SYSTEMD=${EMAILISH_INSTALL_NO_SYSTEMD:-0}

while [ $# -gt 0 ]; do
  case "$1" in
    --helo) HELO="$2"; shift 2 ;;
    --mail-from) MAIL_FROM="$2"; shift 2 ;;
    --allow) ALLOW="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[ -n "$HELO" ] && [ -n "$MAIL_FROM" ] || { echo "--helo and --mail-from are required (see --help)" >&2; exit 2; }
case "$HELO" in *.*) ;; *) echo "--helo must be a fully-qualified hostname, e.g. verify.yourdomain.com" >&2; exit 2 ;; esac

run() { echo "+ $*"; [ "$DRY" = 1 ] || "$@"; }

if [ "$DRY" = 0 ] && [ "$(id -u)" != 0 ]; then echo "run as root (sudo)" >&2; exit 1; fi

echo "== 1/6 packages"
if command -v apt-get >/dev/null; then run apt-get update -qq; run apt-get install -y -qq python3-venv git; fi

echo "== 2/6 service user"
id emailish >/dev/null 2>&1 || run useradd --system --home "$DIR" --shell /usr/sbin/nologin emailish

echo "== 3/6 code in $DIR"
if [ -d "$DIR/.git" ]; then run git -C "$DIR" pull --ff-only; else run git clone --depth 1 "$REPO" "$DIR"; fi
run python3 -m venv "$DIR/.venv"
run "$DIR/.venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "== 4/6 settings in $ETC/emailish.env"
run mkdir -p "$ETC" "$DATA"
if [ -f "$ETC/emailish.env" ]; then
  echo "   $ETC/emailish.env exists: left unchanged"
elif [ "$DRY" = 0 ]; then
  sed -e "s|^EMAILISH_HELO_HOST=.*|EMAILISH_HELO_HOST=$HELO|" \
      -e "s|^EMAILISH_MAIL_FROM=.*|EMAILISH_MAIL_FROM=$MAIL_FROM|" \
      -e "s|^EMAILISH_SMTP_ALLOWED_DOMAINS=.*|EMAILISH_SMTP_ALLOWED_DOMAINS=$ALLOW|" \
      -e "s|^EMAILISH_DATA_DIR=.*|EMAILISH_DATA_DIR=$DATA|" \
      "$DIR/deploy/emailish.env.example" > "$ETC/emailish.env"
  chmod 640 "$ETC/emailish.env"
  echo "   written (HELO=$HELO, MAIL FROM=$MAIL_FROM, allowlist=${ALLOW:-none})"
fi
[ "$DRY" = 1 ] || { id emailish >/dev/null 2>&1 && chown -R emailish: "$DATA" && chgrp emailish "$ETC/emailish.env"; } || true

echo "== 5/6 readiness check (sends no MAIL/RCPT/DATA)"
READY=0
if [ "$DRY" = 0 ]; then
  if (set -a; . "$ETC/emailish.env"; set +a; cd "$DIR" && .venv/bin/python -m app.cli diagnose ${ALLOW:+--domain "${ALLOW%%,*}"}); then
    READY=1
  fi
else
  echo "+ (set -a; . $ETC/emailish.env; cd $DIR && .venv/bin/python -m app.cli diagnose)"
fi

echo "== 6/6 service"
if [ "$NO_SYSTEMD" = 1 ] || ! command -v systemctl >/dev/null; then
  echo "   systemd not used here. Start manually:"
  echo "   (set -a; . $ETC/emailish.env; set +a; cd $DIR && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000)"
else
  sed -e "s|/opt/emailish|$DIR|g" -e "s|/etc/emailish|$ETC|g" -e "s|/var/lib/emailish|$DATA|g" \
      "$DIR/deploy/emailish.service" > /tmp/emailish.service.$$ 2>/dev/null || true
  run install -m 644 /tmp/emailish.service.$$ /etc/systemd/system/emailish.service
  run systemctl daemon-reload
  run systemctl enable --now emailish
fi

echo
if [ "$READY" = 1 ]; then
  echo "emailish is installed and this server CAN run mailbox checks."
else
  echo "emailish is installed, but the readiness check is NOT READY (see above)."
  echo "Mailbox results will be UNKNOWN until port 25 / reverse DNS are fixed, unless a provider key is set in $ETC/emailish.env."
fi
echo "The web UI listens on 127.0.0.1:8000 only. From your computer:"
echo "   ssh -L 8000:127.0.0.1:8000 <user>@<this-server>     then open http://127.0.0.1:8000"
