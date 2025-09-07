#!/usr/bin/env python3
import subprocess, time

UP_THRESH = 60
DOWN_THRESH = 20
POLL = 30  # secondi

def cpu_of(name: str) -> int:
    """
    TODO: sostituire con metrica reale (telemetria/ssh/agent).
    Placeholder: restituisce 55.
    """
    return int(subprocess.check_output(["bash","-lc","echo 55"]).decode().strip())

def clone_exists() -> bool:
    out = subprocess.check_output(["bash","-lc","microstack.openstack server list -f value -c Name"]).decode()
    return "vm-test-clone" in out.splitlines()

def create_clone():
    cmd = r"""microstack.openstack server create \
      --flavor m1.small \
      --image ubuntu-22.04 \
      --nic net-id=$(microstack.openstack network show lab-net -f value -c id) \
      --key-name vito-key \
      --security-group sg-ssh \
      vm-test-clone"""
    subprocess.check_call(["bash","-lc",cmd])

def delete_clone():
    subprocess.check_call(["bash","-lc","microstack.openstack server delete vm-test-clone"])

if __name__ == "__main__":
    while True:
        try:
            c = cpu_of("vm-test")
            print(f"CPU vm-test: {c}%")
            if c > UP_THRESH and not clone_exists():
                print("➡️  Soglia superata: creo vm-test-clone")
                create_clone()
            elif c < DOWN_THRESH and clone_exists():
                print("⬅️  Soglia bassa: elimino vm-test-clone")
                delete_clone()
        except Exception as e:
            print("WARN:", e)
        time.sleep(POLL)
