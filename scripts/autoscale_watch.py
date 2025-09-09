#!/usr/bin/env python3
"""
Autoscaling controller (lab demo) for MicroStack:
- Watches CPU on the BASE VM.
- When CPU > --high for --min-up samples => create CLONE.
- When CPU < --low  for --min-down samples => delete BASE (handover keeps CLONE).

Requires MicroStack CLI on the host and SSH access to the BASE VM.

Usage example:
  python3 scripts/autoscale_watch.py --clone vm-test-clone --high 60 --low 20 --min-up 4 --min-down 4 --metric max

Optional args:
  --base vm-test         # baseline VM name (default)
  --base-host 10.20.20.X # FIP/host of baseline; if omitted, tries to infer via openstack CLI
  --user ubuntu          # SSH user for the baseline VM (default)
  --ssh-key ~/.ssh/lab-key
  --sample 15            # seconds between samples (default)
  --image ubuntu-22.04   # image for clone
  --net lab-net          # network name for clone
  --key-name lab-key     # Nova keypair
  --secgroup sg-secure   # security group
"""
import argparse, os, re, subprocess, time, shlex
from pathlib import Path

def sh(cmd: str) -> str:
    return subprocess.check_output(cmd, shell=True, text=True).strip()

def server_exists(name: str) -> bool:
    out = sh("microstack.openstack server list -f value -c Name")
    return name in out.splitlines()

def get_addresses(name: str) -> str:
    try:
        return sh(f"microstack.openstack server show {shlex.quote(name)} -f value -c addresses")
    except subprocess.CalledProcessError:
        return ""

def infer_base_host(name: str) -> str:
    addrs = get_addresses(name)
    # Try "external=IP", else take the first IP found
    m = re.search(r"external=([0-9.]+)", addrs)
    if m:
        return m.group(1)
    m = re.search(r"([0-9]{1,3}(?:\.[0-9]{1,3}){3})", addrs)
    return m.group(1) if m else ""

def cpu_usage_over_ssh(host: str, user: str, key: str) -> float:
    key = str(Path(key).expanduser())
    # Parse 'top' idle% -> usage = 100 - idle
    cmd = f'ssh -i {shlex.quote(key)} -o StrictHostKeyChecking=no {shlex.quote(user)}@{shlex.quote(host)} ' \
          f'"LANG=C top -bn1 | grep Cpu | head -n1"'
    out = sh(cmd)
    # Examples: "Cpu(s): 12.3%us,  1.0%sy,  0.0%ni, 86.2%id, ..."
    m = re.search(r'(\d+(?:\.\d+)?)%id', out)
    if not m:
        # Fallback: look for "Cpu(s): x.x us, y.y sy, z.z id" variants
        m = re.search(r'(\d+(?:\.\d+)?)\s*id', out)
    idle = float(m.group(1)) if m else 45.0
    return max(0.0, min(100.0, 100.0 - idle))

def create_clone(args):
    net_id = sh(f"microstack.openstack network show {shlex.quote(args.net)} -f value -c id")
    cmd = (
        "microstack.openstack server create "
        f"--flavor m1.small --image {shlex.quote(args.image)} "
        f"--nic net-id={net_id} "
        f"--key-name {shlex.quote(args.key_name)} "
        f"--security-group {shlex.quote(args.secgroup)} "
        f"{shlex.quote(args.clone)}"
    )
    sh(cmd)

def delete_server(name: str):
    sh(f"microstack.openstack server delete {shlex.quote(name)}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clone", required=True, help="Clone VM name to create during scale-out")
    ap.add_argument("--base", default="vm-test", help="Baseline VM name (default: vm-test)")
    ap.add_argument("--base-host", default="", help="Baseline host/IP (FIP). If empty, infer via OpenStack.")
    ap.add_argument("--user", default="ubuntu", help="SSH user for baseline VM")
    ap.add_argument("--ssh-key", default="~/.ssh/lab-key", help="SSH key for baseline VM")
    ap.add_argument("--high", type=int, default=60)
    ap.add_argument("--low", type=int, default=20)
    ap.add_argument("--min-up", type=int, default=4)
    ap.add_argument("--min-down", type=int, default=4)
    ap.add_argument("--sample", type=int, default=15)
    ap.add_argument("--metric", choices=["max","avg"], default="max")
    ap.add_argument("--image", default="ubuntu-22.04")
    ap.add_argument("--net", default="lab-net")
    ap.add_argument("--key-name", default="lab-key")
    ap.add_argument("--secgroup", default="sg-secure")
    args = ap.parse_args()

    host = args.base_host or infer_base_host(args.base)
    if not host:
        print("ERROR: cannot infer BASE host IP. Provide --base-host or assign a Floating IP.")
        return

    up_count = down_count = 0
    clone_created = server_exists(args.clone)

    print(f"[watch] base={args.base}@{host}, clone={args.clone}, high={args.high}%, low={args.low}%, sample={args.sample}s")
    try:
        while True:
            try:
                cpu = cpu_usage_over_ssh(host, args.user, args.ssh_key)
            except Exception as e:
                print(f"[warn] CPU sample failed: {e}")
                time.sleep(args.sample)
                continue

            print(f"[metric] cpu={cpu:.1f}% (up={up_count}/{args.min_up}, down={down_count}/{args.min_down})")

            if cpu >= args.high:
                up_count += 1
                down_count = 0
            elif cpu <= args.low:
                down_count += 1
                up_count = 0
            else:
                # In the middle, decay counters slowly
                up_count  = max(0, up_count-1)
                down_count= max(0, down_count-1)

            # Scale-out
            if (not clone_created) and up_count >= args.min_up:
                print(f"[action] HIGH sustained ⇒ creating clone {args.clone}")
                create_clone(args)
                clone_created = True
                up_count = 0

            # Handover: delete base
            if clone_created and down_count >= args.min_down and server_exists(args.base):
                print(f"[action] LOW sustained ⇒ deleting baseline {args.base} (handover keeps clone)")
                delete_server(args.base)
                down_count = 0

            time.sleep(args.sample)
    except KeyboardInterrupt:
        print("\n[exit] Stopped by user.")

if __name__ == "__main__":
    main()
