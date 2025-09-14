# MicroStack Autoscaling with Load Balancing: CPU Metrics to Scale Actions (Create on High, Delete on Low)

This project implements a practical **autoscaling with load balancing** pattern on a single-node **MicroStack (OpenStack)** lab. A controller monitors a baseline VM’s CPU/memory; on sustained high load it **creates a clone**, and on sustained low load it **deletes the baseline** and keeps the **clone as the new primary**. The goal is a small, reproducible lab that demonstrates scale-out and graceful handover.

**Scripts in this repo**
- `deploy_secure_vm.py` — Idempotent provisioning: network/router/security group/keypair, baseline **CirrOS** VM, **Floating IP**, optional **snapshot retention**, and **cleanup**. It also sets minimal security-group rules so SSH and ICMP work out of the box.
- `autoscale_watch.py` — Autoscaling controller: polls **CPU and MEM** via SSH, triggers **clone creation** and **baseline removal**; expects to call the deploy tool as `~/deploy_secure_vm.py`.
- `split_after_scale.sh` — Load generator: produces a deterministic **100% CPU spike** on the baseline to force scale-out and later a **~50/50 split** across base+clone; includes a `--stop` to cleanly stop all loads.

---

## Quick Start (3 steps)

```bash
# 0) Load OpenStack CLI credentials (MicroStack)
source /var/snap/microstack/common/etc/microstack.rc

# 1) Create a baseline VM (CirrOS + Floating IP) with minimal SG and (optionally) no snapshot
python3 ./deploy_secure_vm.py --name VM-test --no-snapshot \
  --keypair lab-key --pubkey-file ~/.ssh/lab-key.pub

# Note the actual name (auto-numbered, e.g., VM-test_1):
microstack.openstack server list

# 2) Start the autoscaling controller (monitor CPU/MEM on the baseline)
python3 ./autoscale_watch.py \
  --server VM-test_1 \
  --clone legacy \
  --high 80 --low 20 \
  --min-up 4 --min-down 4 \
  --metric max

# 3) In another terminal, trigger load and watch the scale-out + handover
./split_after_scale.sh
```

**What you’ll see**
- On sustained **HIGH** (≥ `--high` for `--min-up` samples), the controller **creates a clone** named from `<base>_clone` (automatically numbered by the deploy tool).
- On sustained **LOW** (≤ `--low` for `--min-down` samples), the controller **deletes the baseline** and continues monitoring the **clone** (handover).

**Why each command matters**
- `source …microstack.rc` → loads OpenStack auth variables into your shell (auth URL, project, token). Without this, OpenStack CLI calls will fail.
- `deploy_secure_vm.py …` → creates everything needed (network, SG, keypair) and a **CirrOS** VM with a Floating IP. `--no-snapshot` skips the automatic snapshot (faster for tests).
- `server list` → shows the **actual VM name** created (e.g., `VM-test_1`), which you’ll pass to `--server`.
- `autoscale_watch.py …` → starts the **controller** that measures CPU/MEM via SSH and applies **scale-out / handover** logic.
- `split_after_scale.sh` → generates load (first 100% to trigger scale-out, then ~50/50 on base+clone to demonstrate load balancing).

---

## One-time setup (keys, SDK, path)

1) **SSH keys** (keep both; CirrOS often prefers RSA):
```bash
ssh-keygen -t ed25519 -f ~/.ssh/lab-key -N ""
ssh-keygen -t rsa -b 2048 -f ~/.ssh/lab-key-rsa -N ""
```
*Explanation:* ed25519 is modern/compact; RSA ensures compatibility with CirrOS. Keys enable passwordless SSH.

2) **OpenStack SDK** & **clouds.yaml** (the scripts use `cloud='microstack'`):
```bash
python3 -m pip install --user openstacksdk
mkdir -p ~/.config/openstack
cat > ~/.config/openstack/clouds.yaml <<'YAML'
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
YAML
```
*Explanation:* centralizes credentials for SDK/CLI; `verify: false` avoids self-signed certificate issues in the lab. Replace `<MICROSTACK_IP>` and `<KEYSTONE_PASSWORD>`.

3) **Make the deployer reachable from `$HOME`**  
The controller invokes it as `~/deploy_secure_vm.py`:
```bash
cp ./deploy_secure_vm.py ~/deploy_secure_vm.py
chmod +x ~/deploy_secure_vm.py
```
*Security-group note:* by default, the deployer opens **ICMP** to `0.0.0.0/0` and **SSH 22** only from `10.20.20.1/32`. Change that CIDR to your **host IP/32** (or temporarily open to `0.0.0.0/0` while testing) so SSH works immediately.

---

## How it works — detailed command explanations

### 1) Baseline provisioning — `deploy_secure_vm.py`

**What it does & why**
1. **Networking (idempotent):** creates/validates `lab-net`, `lab-net-subnet (192.168.100.0/24)`, and `lab-router` with **external** gateway, then attaches the subnet to the router → instances can reach the Internet.
2. **Security Group `sg-secure`:** enables **ICMP** (diagnostics) and **SSH 22** from your IP → safe admin access without opening to the entire Internet.
3. **Nova keypair:** if missing, imports your public key (`--pubkey-file`) under name `--keypair` → **passwordless SSH**.
4. **CirrOS `m1.tiny` VM:** creates a baseline named **`<BASE>_N`** (e.g., `VM-test_1`) to avoid name collisions across repeated runs.
5. **Floating IP:** allocates and associates a FIP with the VM → reachable from your host/LAN.
6. **Optional snapshots:** unless you pass `--no-snapshot`, creates a snapshot and **retains only the last `--retain`** (production-like hygiene in a lab).
7. **Cleanup:** with `--cleanup <BASE>` removes all VMs starting with `<BASE>`, orphan Floating IPs, and (optionally) related snapshots.

**Examples**
```bash
# Create/update a baseline VM (no snapshot)
python3 ./deploy_secure_vm.py --name VM-test --no-snapshot \
  --keypair lab-key --pubkey-file ~/.ssh/lab-key.pub

# Full cleanup for a base prefix (VMs/FIPs; add snapshots with --wipe-snaps)
python3 ./deploy_secure_vm.py --cleanup VM-test --wipe-snaps --yes
```

> After deploy, the script prints ready-to-use SSH commands (ed25519 and RSA) and reminds the default CirrOS console password (`cubswin:)`).

---

### 2) Autoscaling controller — `autoscale_watch.py`

**What it measures & how it decides**
- **Metrics:** reads **CPU** (`/proc/stat`) and **MEM** (`/proc/meminfo`) via SSH; choose `--metric cpu`, `--metric mem`, or `--metric max` (default, most conservative).
- **SSH user/key:** user **`cirros`**, **RSA** key for compatibility; point to it with `--ssh-key-path` if needed.
- **Scale-out:** when the metric stays ≥ `--high` for `--min-up` consecutive samples → calls the deployer to create a clone using `<base>_clone` as base (the deployer auto-numbers, e.g., `_clone_1`).
- **Handover:** when the metric stays ≤ `--low` for `--min-down` samples **and a clone exists** → deletes the **baseline** (via the deployer) and switches monitoring to the **clone** (new primary).

**Key parameters (and why)**
```bash
python3 ./autoscale_watch.py \
  --server VM-test_1 \        # VM to monitor (if omitted: interactive selection in a TTY)
  --clone legacy \            # kept for compatibility; the deployer decides the actual name
  --high 80 --low 20 \        # thresholds: high to create, low to hand over
  --min-up 4 --min-down 4 \   # consecutive samples required (anti-flap)
  --interval 5 \              # polling interval in seconds
  --metric max \              # cpu | mem | max (use "max" to be conservative)
  --ssh-key-path ~/.ssh/lab-key-rsa \
  --deploy-keypair lab-key \
  --deploy-pubkey-file ~/.ssh/lab-key.pub
```

**What you’ll see in logs**
- metric lines: `[metrics] cpu=… mem=… -> max=…`
- **SCALE UP**: log lines showing scale up and the deployer being invoked
- **SCALE DOWN**: log lines like “deleting baseline … and proceeding with clone …”

> Why `~/deploy_secure_vm.py`? The controller reuses the deployer’s idempotent logic for both **clone creation** and **baseline deletion**; keeping it in `$HOME` makes the call stable regardless of the current working directory.

---

### 3) Load generator — `split_after_scale.sh`

**What it does & why**
- If **no clone exists**, it starts **100% CPU** on the baseline (one `yes > /dev/null` per vCPU) until the controller scales; then it waits for the clone to have a FIP/SSH, **stops 100%**, and starts a **~50/50 duty cycle** on **both** baseline and clone to showcase load balancing.
- If a clone **already exists**, it skips the 100% spike and starts **50/50** directly on base+clone.
- `--stop`: safely stops the load on base and clone (automatically detects active pairs).

**Usage**
```bash
# Start: interactively choose an ACTIVE VM as the baseline
./split_after_scale.sh

# Stop: end the load on all detected base/clone pairs
./split_after_scale.sh --stop
```

---

## What to expect (end-to-end)

1. `deploy_secure_vm.py --name VM-test` → **`VM-test_1`** ACTIVE with a Floating IP and correct SG in place.  
2. `autoscale_watch.py --server VM-test_1` → monitoring starts (CPU/MEM).  
3. `split_after_scale.sh` → sustained load ≥ 80% → controller **creates `<base>_clone_*`**; when load falls ≤ 20% sustained → controller **deletes the baseline** and continues on the **clone**.

---

## Repository layout

- `deploy_secure_vm.py` — provisioning (network/router/SG/keypair/VM), Floating IP, snapshots, cleanup  
- `autoscale_watch.py` — autoscaler (CPU/MEM polling, scale-out via deployer, handover)  
- `split_after_scale.sh` — deterministic load generator (`100%` spike + `~50/50` balancer with `--stop`)

## Limitations

- **Single-node MicroStack lab:** no high availability and limited performance; intended for reproducible demos rather than production.
- **Guest image:** the baseline uses **CirrOS**, which has a minimal userspace and limited cloud-init features (no package manager).
- **Telemetry path:** metrics are sampled over **SSH** from inside the guest (no Ceilometer/Gnocchi/Monasca).
- **Load balancing scope:** there is no L4/L7 load balancer; the “balancing” is demonstrated by splitting CPU load across the two instances during the overlap window.

### Note on the (deferred) Horizon dashboard

Initially, the project aimed to provide a small **Horizon panel** under the “Project” section to orchestrate the same workflow from the GUI. On **MicroStack**, however, Horizon is delivered as a **snap**; the snap mount is **read-only**, and enabling a custom panel requires changing Horizon’s Python/Django modules and settings (e.g., `INSTALLED_APPS`, panel registrations, `local_settings.py`). Because these files live inside the read-only snap at runtime, the panel cannot be dropped in or enabled without **rebuilding the Horizon snap** or running a **separate Horizon deployment** (e.g., a source/DevStack build or a containerized Horizon) where those changes are allowed.

For this reason, the files needed to explore a dashboard implementation were **authored but not executed** on MicroStack. If you plan to pursue a GUI path on a different OpenStack build, refer to the **official Horizon Dashboard Developer Guide** for panel/plugin structure and enablement steps.

### Reference documentation used for implementation and configuration:
- [MicroStack Documentation](https://microstack.run/docs/)
- [MicroStack Start Reference](https://discourse.ubuntu.com/t/get-started-with-microstack/13998)
- [Official Ubuntu Cloud images](https://cloud-images.ubuntu.com/)
- [OpenStack CLI Reference](https://docs.openstack.org/python-openstackclient/latest/)

### Disclaimers
* This README is both a user guide and a summary report of the project for the period of August 2025.
* Names and IPs have been anonymized for the creation of the repository!
* The laboratory was created without the use of certificates to avoid conflicts and problems during development. The use of certificates is recommended in the event of operational use.

