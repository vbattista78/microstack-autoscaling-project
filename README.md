# MicroStack Autoscaling with Load Balancing: CPU Metrics to Scale Actions (Create on High, Delete on Low)
This project demonstrates a practical, reproducible **autoscaling with load balancing** pattern in a single-node **MicroStack (OpenStack)** lab environment.

The project provisions the underlying networking, deploys a baseline VM (vm-test), continuously monitors CPU utilization, and when a configurable high threshold (default **60%**) is crossed automatically instantiates a second VM (vm-test-clone) and **balances the workload between the two** while both are active. When utilization falls below a configurable low threshold (default **20%**), the system performs a **graceful handover** by removing the baseline VM and keeping the clone running as the new primary, ensuring continuity without downtime.

The workflow is implemented with idempotent Bash scripts and a lightweight Python controller, uses cloud-init for first-boot configuration, and is documented with screenshots in `docs/` for straightforward replication and evaluation.

## Project setup:
### 1. MicroStack
#### 1.1 Installation
``` bash
$sudo snap install microstack --devmode --beta
$sudo microstack init --auto --control

Reason:
snap install: Installs Microstack in developer mode.
init --control: Configures all OpenStack services (Keystone, Glance, Nova, Neutron) in all-in-one mode.

```
#### 1.2 Configuring Credentials
``` bash
source /var/snap/microstack/common/etc/microstack.rc

Reason:
Loads OpenStack variables (OS_USERNAME, OS_PASSWORD, etc.) into the environment to use microstack.openstack commands.
```
#### 1.3 Checking the Installation
``` bash
microstack.openstack status
microstack.openstack service list
microstack.openstack image list
microstack.openstack network list

Reason:
Check that services are running and that the Cirros image and external network are available.
```

### 2. Internal Networking
``` bash
microstack.openstack network create lab-net
microstack.openstack subnet create \
  --network lab-net \
  --subnet-range 192.168.100.0/24 \
  --gateway 192.168.100.1 \
  --dns-nameserver 8.8.8.8 \
  --dns-nameserver 1.1.1.1 \
  lab-net-subnet
microstack.openstack router create lab-router
microstack.openstack router set lab-router --external-gateway external
microstack.openstack router add subnet lab-router lab-net-subnet

Reason:
Create an internal lab-net network and connect it to the lab-router router, which allows access to the Internet via the external network.
```

### 3. Security Group
``` bash
microstack.openstack security group create sg-secure
microstack.openstack security group rule create \
  --ingress --ethertype IPv4 --protocol tcp --dst-port 22 \
  --remote-ip <IP_HOST>/32 sg-secure
microstack.openstack security group rule create \
  --ingress --ethertype IPv4 --protocol icmp \
  --remote-ip 192.168.100.0/24 sg-secure

Reason:
Allow SSH access only from the host IP.
Enable ICMP (ping) within the network for diagnostics.
```

### 4. Keypair
``` bash
ssh-keygen -t ed25519 -f ~/.ssh/lab-key -N ""
microstack.openstack keypair create --public-key ~/.ssh/lab-key.pub lab-key

Reason:
Create a key pair for secure password-free access.
```

### 5. Creating a VM
``` bash
microstack.openstack server create test-vm \
  --flavor m1.tiny \
  --image cirros \
  --nic net-id=<ID rete lab-net> \
  --key-name lab-key \
  --security-group sg-secure

Reason:
Start a minimal VM with Cirros image and defined security rules.
```

### 6. Floating IP
``` bash
microstack.openstack floating ip create external
microstack.openstack server add floating ip test-vm <FIP>

Reason:
Assign a public IP (on external network) to reach the VM from the host.
```

### 7. Enabling NAT on the host
``` bash
echo 1 | sudo tee /proc/sys/net/ipv4/ip_forward
sudo sed -i 's/^#\?net.ipv4.ip_forward=.*/net.ipv4.ip_forward=1/' /etc/sysctl.conf
sudo sysctl -p

sudo iptables -t nat -A POSTROUTING -s 10.20.20.0/24 -o ens33 -j MASQUERADE
sudo iptables -A FORWARD -i ens33 -o br-ex -m state --state RELATED,ESTABLISHED -j ACCEPT
sudo iptables -A FORWARD -i br-ex -o ens33 -j ACCEPT
sudo apt install -y iptables-persistent
sudo netfilter-persistent save

Reason:
Allows VMs on the internal network to browse the Internet via NAT.
```

### 8. Test with Cirros, Commands (from within the Cirros VM):
``` bash
ping -c 3 1.1.1.1
ping -c 3 8.8.8.8
ping -c 3 google.com

Reason:
Check connectivity and DNS resolution.
```

### 9. Automation with Python
#### 9.1 Environment setup
``` bash
python3 -m venv ~/venv-openstack
source ~/venv-openstack/bin/activate
pip install --upgrade pip
pip install openstacksdk
```

#### 9.2 File clouds.yaml
``` yaml
clouds:
  microstack:
    auth:
      auth_url: https://<IP>:5000/v3
      username: admin
      password: <keystone-password>
      project_name: admin
      user_domain_name: default
      project_domain_name: default
    region_name: microstack
    interface: public
    identity_api_version: 3
    verify: false
```

### 10. Script deploy_secure_vm.py
``` bash
The developed script automatically handles:
network, router, keypair, and security group creation/verification,
VM deployment, floating IP assignment,
snapshot creation with configurable retention,
--no-snapshot option to avoid snapshots,
--cleanup option to delete VMs, orphaned floating IPs, and obsolete snapshots.

Reason:
Check connectivity and DNS resolution.
```









