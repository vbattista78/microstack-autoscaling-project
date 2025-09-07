set -euo pipefail

NET_NAME="lab-net"
SUBNET_NAME="${NET_NAME}-subnet"
SUBNET_CIDR="192.168.100.0/24"

# Crea network e subnet (non fallire se già esistono)
microstack.openstack network show "$NET_NAME" >/dev/null 2>&1 || microstack.openstack network create "$NET_NAME"
microstack.openstack subnet show "$SUBNET_NAME" >/dev/null 2>&1 || microstack.openstack subnet create --subnet-range "$SUBNET_CIDR" --network "$NET_NAME" "$SUBNET_NAME"

# Security group per SSH
microstack.openstack security group show sg-ssh >/dev/null 2>&1 || microstack.openstack security group create sg-ssh
# Regola SSH idempotente
if ! microstack.openstack security group rule list sg-ssh -f value -c "IP Protocol" -c "Port Range" | grep -qE '^tcp\s+22:22$'; then
  microstack.openstack security group rule create --proto tcp --dst-port 22 sg-ssh
fi

echo "✅ Networking pronto."
