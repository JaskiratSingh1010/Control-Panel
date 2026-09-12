#!/usr/bin/env bash
# Make the self-signed certificate the panel serves HTTPS with.
#
# Why HTTPS at all: Chrome refuses to save a file downloaded from a plain http://
# page ("blocked because the site isn't using a secure connection"), so every
# Export Excel fails once the panel is shared over the network.
#
#   bash deploy/make_cert.sh                     # localhost + the addresses below
#   bash deploy/make_cert.sh 192.168.1.45 10.0.0.7   # or list your own
#
# Writes certs/control-panel.crt and certs/control-panel.key. The .key never
# leaves the server and is git-ignored; the .crt is what you hand to people to
# trust once (see the end of this file).
#
# Needs openssl. Git for Windows ships one, so a Git Bash prompt already has it.
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p certs

# Every address the panel is reached by has to be listed, or the browser rejects
# the certificate for that address. Defaults cover this machine on the office LAN
# and the live server.
ADDRS=("$@")
if [ ${#ADDRS[@]} -eq 0 ]; then
  ADDRS=(192.168.1.45 138.252.101.118)
fi

CNF=certs/openssl.cnf
{
  echo '[req]'
  echo 'distinguished_name = dn'
  echo 'x509_extensions    = ext'
  echo 'prompt             = no'
  echo '[dn]'
  echo 'C  = IN'
  echo 'O  = Jivo Group'
  echo 'CN = Jivo Control Panel'
  echo '[ext]'
  echo 'basicConstraints     = critical, CA:TRUE'
  echo 'keyUsage             = critical, digitalSignature, keyCertSign, keyEncipherment'
  echo 'extendedKeyUsage     = serverAuth'
  echo 'subjectAltName       = @alt'
  echo 'subjectKeyIdentifier = hash'
  echo '[alt]'
  echo 'DNS.1 = localhost'
  echo 'IP.1  = 127.0.0.1'
  i=2
  for a in "${ADDRS[@]}"; do
    echo "IP.$i  = $a"
    i=$((i + 1))
  done
} > "$CNF"

# 397 days: Chrome rejects a certificate valid for much longer than that, so a
# "10 year" certificate would be refused outright rather than lasting longer.
openssl req -x509 -newkey rsa:2048 -sha256 -days 397 -nodes \
  -keyout certs/control-panel.key -out certs/control-panel.crt -config "$CNF" 2>/dev/null

echo
echo "wrote certs/control-panel.crt and certs/control-panel.key"
openssl x509 -in certs/control-panel.crt -noout -dates -ext subjectAltName
cat <<'NOTE'

Next:
  1. serve it     .venv/Scripts/python.exe serve.py --https
  2. trust it     give each computer certs/control-panel.crt:
                  double-click -> Install Certificate -> Local Machine
                  -> Place all in: Trusted Root Certification Authorities
                  then restart the browser
  3. open         https://<address>:9443

Without step 2 the browser still shows "not private" - the connection is
encrypted either way, but nothing vouches for who the server is. That one-time
trust is what removes the warning.
NOTE
