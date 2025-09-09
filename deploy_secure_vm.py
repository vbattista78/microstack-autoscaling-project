#!/usr/bin/env python3
import argparse, os, sys, time, re
from datetime import datetime
from openstack import connection
from openstack.exceptions import ResourceNotFound

def log(msg, kind="*"):
    print(f"[{kind}] {msg}")

def wait_server_active(conn, server_id, timeout=600, poll=3):
    start = time.time()
    while time.time() - start < timeout:
        s = conn.compute.get_server(server_id)
        if s.status == "ACTIVE":
            return s
        if s.status in ("ERROR","DELETED"):
            raise RuntimeError(f"VM status={s.status}")
        time.sleep(poll)
    raise TimeoutError("Timeout in attesa di ACTIVE")

def ensure_keypair(conn, keypair_name, pubkey_file=None):
    try:
        kp = conn.compute.find_keypair(keypair_name)
        if kp:
            log(f"Keypair '{keypair_name}' già presente in OpenStack","i")
            return kp
        if not pubkey_file:
            raise RuntimeError(f"Keypair '{keypair_name}' assente. Specifica --pubkey-file per crearlo.")
        with open(os.path.expanduser(pubkey_file),'r') as f:
            pubkey = f.read().strip()
        log(f"Creo keypair '{keypair_name}' da {pubkey_file}","*")
        kp = conn.compute.create_keypair(name=keypair_name, public_key=pubkey)
        return kp
    except Exception as e:
        raise RuntimeError(f"Keypair error: {e}")

def ensure_network_bits(conn):
    net = conn.network.find_network("lab-net")
    if net:
        log(f"Rete lab-net esistente: {net.id}","i")
    else:
        log("Creo rete lab-net","*")
        net = conn.network.create_network(name="lab-net")

    subnet = conn.network.find_subnet("lab-net-subnet")
    if subnet:
        log(f"Subnet lab-net-subnet esistente: {subnet.id}","i")
    else:
        log("Creo subnet lab-net-subnet","*")
        subnet = conn.network.create_subnet(
            name="lab-net-subnet",
            network_id=net.id,
            ip_version=4,
            cidr="192.168.100.0/24",
            gateway_ip="192.168.100.1",
            dns_nameservers=["1.1.1.1","8.8.8.8"])

    router = conn.network.find_router("lab-router")
    if not router:
        log("Creo router lab-router","*")
        router = conn.network.create_router(name="lab-router")

    ext = conn.network.find_network("external")
    if ext and (not router.external_gateway_info or router.external_gateway_info.get("network_id") != ext.id):
        log("Imposto gateway esterno su lab-router","i")
        conn.network.update_router(router, external_gateway_info={"network_id": ext.id})
    else:
        log("Router lab-router: gateway esterno già configurato","i")

    # Metodo robusto: proviamo ad aggiungere l'interfaccia; se esiste già, ignoriamo l'errore.
    try:
        conn.network.add_interface_to_router(router, subnet_id=subnet.id)
        log("Aggancio interfaccia lab-net-subnet al router","i")
    except Exception:
        log("Router lab-router: interfaccia su lab-net-subnet già presente","i")

    return net, subnet, router

def ensure_secgroup(conn):
    sg = conn.network.find_security_group("sg-secure")
    if not sg:
        log("Creo Security Group sg-secure","*")
        sg = conn.network.create_security_group(name="sg-secure")

    def have(rule):
        return any(
          (r.ether_type=="IPv4" and r.protocol==rule["protocol"] and r.direction=="ingress"
           and r.remote_ip_prefix==rule.get("remote_ip_prefix") and
           (r.port_range_min==rule.get("port_range_min") or rule.get("port_range_min") is None))
          for r in conn.network.security_group_rules(security_group_id=sg.id)
        )

    rules = [
        dict(protocol="icmp", remote_ip_prefix="0.0.0.0/0", port_range_min=None, port_range_max=None),
        dict(protocol="tcp", remote_ip_prefix="10.20.20.1/32", port_range_min=22, port_range_max=22)
    ]
    for rule in rules:
        if not have(rule):
            conn.network.create_security_group_rule(
                security_group_id=sg.id, direction="ingress", ether_type="IPv4",
                protocol=rule["protocol"], port_range_min=rule.get("port_range_min"),
                port_range_max=rule.get("port_range_max"), remote_ip_prefix=rule["remote_ip_prefix"])
    return sg

def pick_image(conn, name="cirros"):
    img = conn.image.find_image(name)
    if not img:
        raise RuntimeError(f"Immagine '{name}' non trovata")
    return img

def pick_flavor(conn, name="m1.tiny"):
    flv = conn.compute.find_flavor(name)
    if not flv:
        raise RuntimeError(f"Flavor '{name}' non trovato")
    return flv



def ensure_fip(conn, server, network_name="external", wait_secs=120):
    """Associa un floating IP alla VM usando Neutron (robusto)."""
    import time
    from openstack.exceptions import ResourceNotFound

    # 1) Attendi che la VM sia ACTIVE
    try:
        server = conn.compute.wait_for_server(server, status="ACTIVE", failures=["ERROR"], wait=wait_secs, interval=3)
    except Exception:
        # fallback: almeno rifetch
        server = conn.compute.get_server(server.id)

    # 2) Se ha già un FIP, restituiscilo
    try:
        for addr_list in (server.addresses or {}).values():
            for ipinfo in addr_list:
                if ipinfo.get("OS-EXT-IPS:type") == "floating":
                    class Obj: pass
                    o = Obj()
                    o.floating_ip_address = ipinfo["addr"]
                    return o
    except Exception:
        pass


    # 3) Trova/crea un FIP libero
    # risolviamo la rete esterna -> net.id
    net = conn.network.find_network(network_name, ignore_missing=False)
    fip = None
    for f in conn.network.ips():
        if f.floating_network_id and not f.port_id:
            fip = f
            break
    if not fip:
        fip = conn.network.create_ip(floating_network_id=net.id)

    # 4) Trova il port della VM e associa il FIP (Neutron)
    deadline = time.time() + wait_secs
    last_err = None
    while time.time() < deadline:
        try:
            # ricarica server e cerca un port collegato
            server = conn.compute.get_server(server.id)
            vm_ports = list(conn.network.ports(device_id=server.id))
            port = vm_ports[0] if vm_ports else None
            if not port:
                time.sleep(3)
                continue

            # se qualcuno lo ha già associato nel frattempo, siamo a posto
            if fip.port_id == port.id:
                return fip

            # associazione FIP -> port (Neutron)
            fip = conn.network.update_ip(fip, port_id=port.id)

            # double-check: se ora ha port_id, ok
            if fip.port_id == port.id:
                return fip
        except Exception as e:
            last_err = e
        time.sleep(3)

    raise last_err or RuntimeError("Impossibile associare il Floating IP entro il timeout.")


def snapshot_and_wait(conn, server, snap_name, timeout=900):
    img = conn.compute.create_server_image(server, name=snap_name)
    start = time.time()
    while time.time()-start < timeout:
        im = conn.image.get_image(img)
        if getattr(im, "status", "").lower()=="active":
            return im
        if getattr(im, "status","").lower() in ("killed","error"):
            raise RuntimeError(f"Snapshot fallito: status={im.status}")
        time.sleep(3)
    raise TimeoutError("Timeout snapshot")

def prune_old_snapshots(conn, base_name, retain):
    if retain is None:
        return []
    prefix = f"{base_name}-snap-"
    snaps = [i for i in conn.image.images() if (i.name or "").startswith(prefix)]
    def key(i):
        return (getattr(i,"created_at",None) or getattr(i,"updated_at",None) or i.name)
    snaps.sort(key=key, reverse=True)
    to_delete = snaps[retain:]
    for im in to_delete:
        conn.image.delete_image(im.id, ignore_missing=True)
    return to_delete

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", default=None, help="Nome VM da creare")
    p.add_argument("--retain", type=int, default=3, help="Quanti snapshot recenti mantenere (default: 3)")
    p.add_argument("--no-snapshot", action="store_true", help="Non creare lo snapshot post-creazione")
    p.add_argument("--cleanup", help="Base name per cleanup (VM/FIP/snaps)")
    p.add_argument("--wipe-snaps", action="store_true", help="(cleanup) Rimuovi anche gli snapshot")
    p.add_argument("--yes", action="store_true", help="Non chiedere conferme (cleanup)")
    p.add_argument("--keypair", default=os.environ.get("DEPLOYVM_KEYPAIR","lab-key"),
                   help="Nome keypair da usare/creare (default: %(default)s)")
    p.add_argument("--pubkey-file", default=os.environ.get("DEPLOYVM_PUBKEY"),
                   help="Pubkey file da caricare se il keypair non esiste")
    args = p.parse_args()

    log("Connessione a OpenStack...")
    conn = connection.Connection(cloud='microstack')

    if args.cleanup:
        base = args.cleanup
        log(f"Cleanup per base name: '{base}'","*")
        servers = [s for s in conn.compute.servers() if s.name==base or s.name.startswith(base+"-")]
        if servers:
            log(f"VM da cancellare: {[s.name for s in servers]}","i")
            if args.yes or input(f"Confermi cancellazione di {len(servers)} VM? [y/N] ").lower()=="y":
                for s in servers:
                    conn.compute.delete_server(s, ignore_missing=True)
                    log(f"Eliminazione VM avviata: {s.name} ({s.id})","+")
        else:
            log(f"Nessuna VM da cancellare per base '{base}'","i")
        orphans = [f for f in conn.network.ips() if f.status=="DOWN"]
        if orphans:
            log(f"Floating IP orfani da rimuovere: {[f.floating_ip_address for f in orphans]}","i")
            if args.yes or input(f"Confermi cancellazione di {len(orphans)} FIP orfani? [y/N] ").lower()=="y":
                for f in orphans:
                    conn.network.delete_ip(f.id, ignore_missing=True)
                    log(f"FIP rimosso: {f.floating_ip_address}","+")
        else:
            log("Nessun Floating IP orfano","i")
        if args.wipe_snaps:
            prefix = f"{base}-snap-"
            snaps = [i for i in conn.image.images() if (i.name or "").startswith(prefix)]
            if snaps:
                log(f"Snapshot da rimuovere: {[i.name for i in snaps]}","i")
                for im in snaps:
                    conn.image.delete_image(im.id, ignore_missing=True)
                    log(f"Snapshot rimosso: {im.name} (ID: {im.id})","+")
            else:
                log(f"Nessuno snapshot con prefisso '{prefix}'","i")
        log(f"Cleanup terminato per base '{base}'","+")
        return

    base_name = (args.name or input("Inserisci il nome base della nuova VM: ").strip()) or "VM-test"
    net, subnet, router = ensure_network_bits(conn)
    sg = ensure_secgroup(conn)
    ensure_keypair(conn, args.keypair, args.pubkey_file)

    image = pick_image(conn, "cirros")
    flavor = pick_flavor(conn, "m1.tiny")

    # Calcola il prossimo nome disponibile del tipo base_N (VM-test_1, VM-test_2, ...)
    pattern = re.compile(rf"^{re.escape(base_name)}_(\d+)$")
    nums = []
    for s in conn.compute.servers():
        m = pattern.match(s.name or "")
        if m:
            try:
               nums.append(int(m.group(1)))
            except ValueError:
                pass
    next_num = (max(nums) + 1) if nums else 1
    vm_name = f"{base_name}_{next_num}"

    log(f"Creazione/verifica VM {vm_name}...","*")
    server = conn.compute.create_server(
        name=vm_name,
        image_id=image.id,
        flavor_id=flavor.id,
        networks=[{"uuid": net.id}],
        key_name=args.keypair,
        security_groups=[{"name": sg.name}],
        config_drive=True,
    )
    server = wait_server_active(conn, server.id, timeout=600)
    fip = ensure_fip(conn, server)

    snap_id = None
    snap_name = None
    if not args.no_snapshot:
        snap_name = f"{vm_name}-snap-{datetime.now().strftime('%Y%m%d-%H%M')}"
        log(f"Creazione snapshot '{snap_name}'...","*")
        img = snapshot_and_wait(conn, server, snap_name)
        log(f"Snapshot creato: {snap_name} (ID: {img.id})","+")
        snap_id = img.id
        deleted = prune_old_snapshots(conn, vm_name, args.retain)
        for im in deleted:
            log(f"Snapshot vecchio rimosso: {im.name} (ID: {im.id})","i")

    print("\nVM pronta!\n")
    print("Comando SSH (ed25519 locale):")
    print(f"ssh -o StrictHostKeyChecking=no -i {os.path.expanduser('~')}/.ssh/lab-key cirros@{getattr(fip, 'floating_ip_address', fip)}\n")
    print("Se Cirros rifiuta ed25519, usa la RSA con algoritmi legacy (se hai generato ~/.ssh/lab-key-rsa):")
    print(f"ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=yes -o PubkeyAcceptedAlgorithms=+ssh-rsa -o HostKeyAlgorithms=+ssh-rsa -i {os.path.expanduser('~')}/.ssh/lab-key-rsa cirros@{getattr(fip, 'floating_ip_address', fip)}\n")
    if snap_id and snap_name:
        print(f"Snapshot creato: {snap_name} (ID: {snap_id})\n")
    print("Password di default Cirros (per test): cubswin:)\n")

if __name__ == "__main__":
    main()
