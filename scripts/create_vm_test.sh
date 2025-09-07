#!/usr/bin/env bash
set -euo pipefail

VM_NAME="vm-test"
IMG=${IMG:-"ubuntu-22.04"}
NET_ID=$(microstack.openstack network show lab-net -f value -c id)
KEY_NAME=${KEY_NAME:-"vito-key"}

# Verifica keypair
if ! microstack.openstack keypair show "$KEY_NAME" >/dev/null 2>&1; then
  echo "❌ Keypair \"$KEY_NAME\" non trovato. Crealo o modifica KEY_NAME."
  exit 1
fi

# Crea VM se non esiste
if microstack.openstack server show "$VM_NAME" >/dev/null 2>&1; then
  echo "ℹ️  La VM $VM_NAME esiste già."
else
  microstack.openstack server create \
    --flavor m1.small \
    --image "$IMG" \
    --nic net-id="$NET_ID" \
    --key-name "$KEY_NAME" \
    --security-group sg-ssh \
    --user-data config/user-data.yaml \
    "$VM_NAME"
  echo "✅ VM $VM_NAME creata."
fi
