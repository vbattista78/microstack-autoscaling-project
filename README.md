# MicroStack Autoscaling with Load Balancing: CPU Metrics to Scale Actions (Create on High, Delete on Low)

This project demonstrates a practical, reproducible **autoscaling with load balancing** pattern in a single-node **MicroStack (OpenStack)** lab environment.

The project provisions the underlying networking, deploys a baseline VM (`vm-test`), continuously monitors CPU utilization, and—when a configurable high threshold (default **60%**) is crossed—automatically instantiates a second VM (`vm-test-clone`) and **balances the workload between the two** while both are active. When utilization falls below a configurable low threshold (default **20%**), the system performs a **graceful handover** by removing the baseline VM and keeping the clone running as the new primary, ensuring continuity without downtime.

The workflow is implemented with idempotent Bash scripts and a lightweight Python controller, uses cloud-init for first-boot configuration, and is documented with screenshots in `docs/` for straightforward replication and evaluation.

---

## Quick Start (3 steps)

```bash
# 0) One-time: ensure MicroStack is installed and credentials are loaded
source /var/snap/microstack/common/etc/microstack.rc

# 1) Provision baseline (network + baseline VM)
bash scripts/setup_networking.sh
bash scripts/create_vm_test.sh

# 2) Start the autoscaling controller (keeps running)
python3 autoscale_watch.py \
  --clone vm-test-clone \
  --high 60 \
  --low 20 \
  --min-up 4 \
  --min-down 4 \
  --metric max

# 3) In another terminal: generate CPU load to trigger scale-out
./split_after_scale.sh
```

**What you’ll see**
- When CPU rises above **60%** for `--min-up` samples → controller **creates `vm-test-clone`** and **balances load**.
- When CPU drops below **20%** for `--min-down` samples → controller **deletes the baseline** and **keeps the clone** as the new primary.

---

## Project Setup

### 1. MicroStack

#### 1.1 Installation
```bash
sudo snap install microstack --devmode --beta
sudo microstack init --auto --control
```
*Explanation:*  
- `snap install` deploys MicroStack in developer mode; this is convenient in lab scenarios (fewer confinement restrictions).  
- `microstack init --auto --control` configures the core OpenStack services (**Keystone**, **Glance**, **Nova**, **Neutron**) in an all-in-one controller node, applying sensible defaults so you can start issuing OpenStack commands immediately.

#### 1.2 Configure Credentials
```bash
source /var/snap/microstack/common/etc/microstack.rc
```
*Explanation:*  
- Loads OpenStack environment variables (e.g., `OS_AUTH_URL`, `OS_USERNAME`, `OS_PASSWORD`, `OS_PROJECT_NAME`).  
- Without this step, CLI calls like `microstack.openstack server list` will fail with missing auth parameters.

#### 1.3 Verify Installation
```bash
microstack.openstack status
microstack.openstack service list
microstack.openstack image list
microstack.openstack network list
```
*Explanation:*  
- `status` checks the overall health of MicroStack services.  
- `service list` confirms API endpoints/registrations.  
- `image list` ensures base images (e.g., CirrOS) are present.  
- `network list` verifies that the default **external** provider network exists (needed for floating IPs).

---

### 2. Internal Networking and Router
```bash
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
```
*Explanation:*  
- Creates an internal tenant network (`lab-net`) and its subnet.  
- Attaches `lab-router` to the provider **external** network to enable North-South connectivity.  
- Adds the internal subnet to the router so instances on `lab-net` can reach the Internet via **SNAT** performed by Neutron.

---

### 3. Security Group
```bash
microstack.openstack security group create sg-secure

microstack.openstack security group rule create \
  --ingress --ethertype IPv4 --protocol tcp --dst-port 22 \
  --remote-ip <HOST_IP>/32 \
  sg-secure

microstack.openstack security group rule create \
  --ingress --ethertype IPv4 --protocol icmp \
  --remote-ip 192.168.100.0/24 \
  sg-secure
```
*Explanation:*  
- Defines a least-privilege security group for the lab.  
- Allows **SSH** only from your host (`<HOST_IP>/32`) to reduce the attack surface.  
- Enables **ICMP** (ping) within the internal subnet for basic diagnostics (latency, reachability).

---

### 4. Key Pair
```bash
test -f ~/.ssh/lab-key || ssh-keygen -t ed25519 -f ~/.ssh/lab-key -N ""
microstack.openstack keypair show lab-key >/dev/null 2>&1 || \
  microstack.openstack keypair create --public-key ~/.ssh/lab-key.pub lab-key

# (Optional) Also create an RSA key if needed:
test -f ~/.ssh/lab-key-rsa || ssh-keygen -t rsa -b 2048 -f ~/.ssh/lab-key-rsa -N ""
```
*Explanation:*  
- Generates an **ed25519** SSH key (modern, short, secure) if missing and registers the **public** key in Nova (`keypair create`).  
- Instances launched with `--key-name lab-key` will inject this public key, enabling **passwordless** admin access.

---

### 5. Create a CirrOS VM
```bash
microstack.openstack server create test-vm \
  --flavor m1.tiny \
  --image cirros \
  --nic net-id=<LAB_NET_ID> \
  --key-name lab-key \
  --security-group sg-secure
```
*Explanation:*  
- Boots a minimal VM using the CirrOS image to validate networking and access quickly.  
- Uses the internal network (`<LAB_NET_ID>`) and the hardened `sg-secure`.  
- Retrieve `<LAB_NET_ID>` with:  
  `microstack.openstack network show lab-net -f value -c id`.

---

### 6. Floating IP
```bash
microstack.openstack floating ip create external
microstack.openstack server add floating ip test-vm <FIP>
```
*Explanation:*  
- Allocates a **Floating IP** from the provider `external` network and associates it to `test-vm`.  
- This provides **public reachability** (from your host/LAN) without exposing the entire tenant network.

---

### 7. Enable NAT on the Host
```bash
echo 1 | sudo tee /proc/sys/net/ipv4/ip_forward
sudo sed -i 's/^#\?net.ipv4.ip_forward=.*/net.ipv4.ip_forward=1/' /etc/sysctl.conf
sudo sysctl -p

# Adjust interface names as appropriate for your host:
sudo iptables -t nat -A POSTROUTING -s 10.20.20.0/24 -o ens33 -j MASQUERADE
sudo iptables -A FORWARD -i ens33 -o br-ex -m state --state RELATED,ESTABLISHED -j ACCEPT
sudo iptables -A FORWARD -i br-ex -o ens33 -j ACCEPT

sudo apt install -y iptables-persistent
sudo netfilter-persistent save
```
*Explanation:*  
- Enables **kernel IP forwarding** and sets **MASQUERADE** so traffic from internal ranges can egress via the host NIC (`ens33` in the example).  
- The FORWARD rules permit return traffic and allow forwarding between the external NIC and Open vSwitch bridge (`br-ex`).  
- `iptables-persistent` saves rules across reboots.  
> **Note:** Replace `ens33`, `br-ex`, and `10.20.20.0/24` with your actual interface names and subnet.

---

### 8. Test from Inside the CirrOS VM
```bash
ping -c 3 1.1.1.1
ping -c 3 8.8.8.8
ping -c 3 google.com
```
*Explanation:*  
- Validates **basic IP connectivity** (to 1.1.1.1 / 8.8.8.8) and **DNS resolution** (to `google.com`).  
- If first two succeed but DNS fails, check `/etc/resolv.conf` in the guest and DNS settings on the subnet.

---

### 9. Python Automation

#### 9.1 Environment Setup
```bash
python3 -m venv ~/venv-openstack
source ~/venv-openstack/bin/activate
pip install --upgrade pip
pip install openstacksdk
```
*Explanation:*  
- Creates an **isolated Python virtual environment** for OpenStack automation, avoiding system-wide package conflicts.  
- Installs `openstacksdk`, the canonical library for programmatic access to OpenStack APIs.

#### 9.2 `clouds.yaml`
```yaml
# Save as: ~/.config/openstack/clouds.yaml
clouds:
  microstack:
    auth:
      auth_url: https://<MICROSTACK_IP>:5000/v3
      username: admin
      password: <KEYSTONE_PASSWORD>
      project_name: admin
      user_domain_name: default
      project_domain_name: default
    region_name: microstack
    interface: public
    identity_api_version: 3
    verify: false
```
*Explanation:*  
- Centralizes **auth settings** so your Python code (and CLI with `--os-cloud microstack`) can authenticate without exporting environment variables every time.  
- `verify: false` disables TLS verification (handy with MicroStack’s default self-signed cert in lab contexts).

---

### 10. `deploy_secure_vm.py` — Functional Overview & Demo Flow

**What the command does (core features):**
- **Pre-flight checks**: verifies that the **network**, **subnet**, **router**, **security group**, and **key pair** exist; creates them if missing (idempotent behavior).  
- **Baseline VM provisioning**: deploys the primary instance (e.g., `vm-test`) with the selected image/flavor, attaches it to `lab-net`, and injects the SSH key.  
- **Connectivity enablement**: optionally allocates and associates a **Floating IP** for external reachability.  
- **Snapshot lifecycle (optional)**: creates snapshots with **configurable retention**, to preserve a known-good state.  
- **Cleanup mode**: via `--cleanup`, removes instances, orphaned Floating IPs, and obsolete snapshots to restore a clean lab.

**How it fits the autoscaling demo (end-to-end):**
- The script **creates the baseline VM** (step 1 of the demo).  
- Then, when the **autoscaling controller** (see §11) detects **high CPU** and triggers scaling, a **clone instance** (e.g., `vm-test-clone`) is created and **load balancing** is enacted while both are active (step 2).  
- When CPU drops below the **low threshold**, the system performs a **handover** by **deleting the baseline VM** and keeping the clone as the new primary (step 3).  
  > In practice: `deploy_secure_vm.py` provisions the environment and baseline; the **controller** executes the scale actions. Together they realize the full “create VM → create clone → delete base” flow.

*Explanation:*  
- Keeping provisioning and scaling **separate** makes the system easier to reason about and test.  
- The demo mirrors production patterns: infrastructure-as-code for **setup**, a controller/agent for **reactive scaling**.

---

### 11. Autoscaling Controller — `autoscale_watch.py`
Run the controller with the thresholds and parameters for your lab:

```bash
python3 autoscale_watch.py \
  --clone vm-test-clone \
  --high 60 \
  --low 20 \
  --min-up 4 \
  --min-down 4 \
  --metric max
```
*Explanation:*  
- `--clone vm-test-clone`: name of the **clone** to be created when scaling out.  
- `--high 60`: **upper CPU threshold** (%). Crossing it triggers **scale-out** (create the clone).  
- `--low 20`: **lower CPU threshold** (%). Crossing it triggers **handover** (delete the baseline, keep the clone).  
- `--min-up 4`: require **4 consecutive samples** above `--high` before scaling out (debounces spikes).  
- `--min-down 4`: require **4 consecutive samples** below `--low` before deleting the base (prevents flapping).  
- `--metric max`: use the **maximum** CPU value among collected samples per interval (more conservative).  
> Ensure your metric backend is reachable (Ceilometer/Gnocchi/SDK/SSH sampling, depending on your implementation).  
> Use names consistent with your environment (defaults in this README use `vm-test` / `vm-test-clone`).

---

### 12. Load Generation — `split_after_scale.sh`
Trigger CPU pressure to drive the autoscaling event:

```bash
./split_after_scale.sh
```
*Explanation:*  
- Generates **high CPU load (≈100%)** on the baseline VM to reliably cross the **high threshold** and **force scale-out**.  
- Useful for **demos** and **tests**: you can observe the controller logs, the creation of `vm-test-clone`, subsequent **load balancing**, and finally the **deletion of the baseline** once CPU falls and stabilizes below the low threshold.

---

**Placeholders to replace:**  
`<HOST_IP>`, `<LAB_NET_ID>`, `<FIP>`, `<MICROSTACK_IP>`, `<KEYSTONE_PASSWORD>`.
