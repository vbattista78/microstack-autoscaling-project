#!/usr/bin/env python3
import argparse, os, sys, time, subprocess, re
from openstack import connection

# ------------------------
# Utility di discovery VMs
# ------------------------
def _list_active_with_fip(conn):
    """Return list of tuples (name, fip) for ACTIVE servers; fip may be None."""
    items = []
    for s in conn.compute.servers():
        try:
            if getattr(s, "status", "").upper() != "ACTIVE":
                continue
            det = conn.compute.get_server(s.id)
            fip = None
            for lst in (det.addresses or {}).values():
                for it in lst:
                    if it.get("OS-EXT-IPS:type") == "floating":
                        fip = it.get("addr"); break
            items.append((s.name, fip))
        except Exception:
            pass
    # sort by name for stable UX
    items.sort(key=lambda t: t[0])
    return items

def _choose_server_interactive(conn):
    items = _list_active_with_fip(conn)
    if not items:
        print("[!] Nessuna istanza ACTIVE trovata.", file=sys.stderr)
        sys.exit(1)
    print("\nSeleziona la VM da monitorare (istanze ACTIVE):")
    for idx, (name, fip) in enumerate(items, 1):
        print(f"  {idx}) {name}  " + (f"@ {fip}" if fip else "(no FIP)"))
    while True:
        choice = input("Inserisci il numero della VM: ").strip()
        if not choice.isdigit():
            print("[!] Inserisci un numero valido."); continue
        i = int(choice)
        if 1 <= i <= len(items):
            return items[i-1][0]
        print("[!] Scelta fuori intervallo.")

# ------------------------
# SSH & metriche
# ------------------------
def ssh_run(host, cmd, key_path, timeout=5):
    opts = [
        "ssh","-o","StrictHostKeyChecking=no",
        "-o","IdentitiesOnly=yes",
        "-o","BatchMode=yes",
        "-o","PubkeyAuthentication=yes",
        "-o","PubkeyAcceptedAlgorithms=+ssh-rsa",
        "-o","HostKeyAlgorithms=+ssh-rsa",
        "-i", os.path.expanduser(key_path),
        f"cirros@{host}", cmd
    ]
    cp = subprocess.run(opts, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        timeout=timeout, text=True)
    if cp.returncode != 0:
        raise RuntimeError(f"SSH failed: {cp.stderr.strip()}")
    return cp.stdout.strip()

def get_metrics(host, key_path):
    a = ssh_run(host, "grep '^cpu ' /proc/stat", key_path)
    time.sleep(1)
    b = ssh_run(host, "grep '^cpu ' /proc/stat", key_path)

    def parse(line):
        parts = line.split()
        vals = list(map(int, parts[1:]))
        idle = vals[3] + vals[4]
        total = sum(vals)
        return total, idle

    t1, i1 = parse(a); t2, i2 = parse(b)
    dt = t2 - t1; di = i2 - i1
    cpu_busy = (1 - (di/dt)) * 100 if dt > 0 else 0.0

    mem = ssh_run(host, "cat /proc/meminfo | egrep 'MemTotal|MemAvailable'", key_path)
    lines = {l.split(':')[0]: int(l.split()[1]) for l in mem.splitlines()}
    mem_total = lines["MemTotal"]; mem_avail = lines["MemAvailable"]
    mem_used_pct = (1 - (mem_avail/mem_total)) * 100

    return cpu_busy, mem_used_pct

# ------------------------
# Cloni <base>_clone_#
# ------------------------
def _clone_prefix(base_name: str) -> str:
    return f"{base_name}_clone"

def _is_clone_of(base_name: str, candidate: str) -> bool:
    pref = _clone_prefix(base_name)
    return candidate == pref or candidate.startswith(pref + "_")

def _list_clones(conn, base_name: str):
    """Ritorna la lista di (server_obj, index_int_or_None) per i cloni di base."""
    pref = _clone_prefix(base_name)
    out = []
    pattern = re.compile(rf"^{re.escape(pref)}_(\d+)$")
    for s in conn.compute.servers():
        try:
            nm = s.name or ""
            if not _is_clone_of(base_name, nm):
                continue
            if getattr(s, "status", "").upper() in ("DELETED", "SOFT_DELETED"):
                continue
            m = pattern.match(nm)
            idx = int(m.group(1)) if m else None
            out.append((s, idx))
        except Exception:
            pass
    # ordina per indice (None < 0), poi per nome
    out.sort(key=lambda t: (-1 if t[1] is None else t[1], t[0].name))
    return out

def _pick_primary_clone_name(base_name: str) -> str:
    """Nome base usato dal deploy per creare il nuovo clone (senza numero)."""
    return _clone_prefix(base_name)

def _get_server_fip(conn, server_obj):
    det = conn.compute.get_server(server_obj.id)
    for lst in (det.addresses or {}).values():
        for it in lst:
            if it.get("OS-EXT-IPS:type") == "floating":
                return it.get("addr")
    return None

# ------------------------
# Main
# ------------------------
def main():
    ap = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--server", required=False, help="Nome della VM da monitorare (se assente, verrà richiesto interattivamente)")
    ap.add_argument("--clone", required=True, help="(Compatibilità) Prefisso/nome clone legacy: mantenuto ma NON usato per il naming")
    ap.add_argument("--high", type=float, default=60.0, help="Soglia alta per lo scale-up (%)")
    ap.add_argument("--low",  type=float, default=20.0, help="Soglia bassa per lo scale-down (%)")
    ap.add_argument("--min-up", type=int, default=4, help="Campioni consecutivi sopra HIGH per scalare su")
    ap.add_argument("--min-down", type=int, default=4, help="Campioni consecutivi sotto LOW per scalare giù")
    ap.add_argument("--interval", type=int, default=5, help="Intervallo di campionamento (s)")
    ap.add_argument("--metric", choices=["cpu","mem","max"], default="max", help="Metrica da usare")
    ap.add_argument("--ssh-key-path", default=os.environ.get("AUTOSCALE_SSH_KEY","~/.ssh/lab-key-rsa"),
                    help="Chiave privata per SSH sulla VM monitorata")
    ap.add_argument("--deploy-keypair", default=os.environ.get("DEPLOYVM_KEYPAIR","lab-key"),
                    help="Keypair da usare per creare i cloni")
    ap.add_argument("--deploy-pubkey-file", default=os.environ.get("DEPLOYVM_PUBKEY"),
                    help="Eventuale file di public key da (ri)inserire in OpenStack")
    args = ap.parse_args()

    conn = connection.Connection(cloud='microstack')

    # Se --server non è passato: in ambiente interattivo chiedi all'utente; in non-interattivo, errore.
    if not args.server:
        if sys.stdin.isatty() and sys.stdout.isatty():
            args.server = _choose_server_interactive(conn)
        else:
            print("[!] Parametro --server obbligatorio in esecuzione non-interattiva.", file=sys.stderr); sys.exit(2)

    # Stato iniziale: VM base e FIP
    base = conn.compute.find_server(args.server)
    if not base:
        print(f"[!] VM '{args.server}' non trovata", file=sys.stderr); sys.exit(1)
    fip = _get_server_fip(conn, base)
    if not fip:
        print("[!] Nessun Floating IP associato alla VM monitorata", file=sys.stderr); sys.exit(1)

    print("[*] Autoscaler avviato")
    print(f"[i] Monitor su '{args.server}' @ {fip} — metric={args.metric}, HIGH={args.high}%, LOW={args.low}%")

    hi_hits = lo_hits = 0

    try:
        while True:
            try:
                cpu, mem = get_metrics(fip, args.ssh_key_path)
                val = cpu if args.metric == "cpu" else mem if args.metric == "mem" else max(cpu, mem)
                print(f"[metrics] cpu={cpu:.1f}% mem={mem:.1f}% -> {args.metric}={val:.1f}%")

                if val >= args.high:
                    hi_hits += 1; lo_hits = 0
                elif val <= args.low:
                    lo_hits += 1; hi_hits = 0
                else:
                    hi_hits = lo_hits = 0

                # -------- Cloni della VM base corrente --------
                clones = _list_clones(conn, args.server)
                # ---------------------------------------------

                # SCALE UP: crea <server>_clone_1 solo se NON esiste già alcun clone
                if hi_hits >= args.min_up:
                    if clones:
                        short = [(s.name, getattr(s, "status", "?")) for (s, idx) in clones]
                        print(f"[-] SCALE UP saltato: clone già presente {short}.")
                    else:
                        print(f"[+] SCALE UP: creo clone per '{args.server}'")
                        cmd = ["~/deploy_secure_vm.py","--name", _pick_primary_clone_name(args.server),
                               "--retain","3","--keypair", args.deploy_keypair]
                        if args.deploy_pubkey_file:
                            cmd += ["--pubkey-file", args.deploy_pubkey_file]
                        subprocess.run(" ".join(cmd), shell=True, check=True)
                    hi_hits = 0

                # SCALE DOWN: cancella la BASE SOLO se esiste almeno un clone
                if lo_hits >= args.min_down:
                    if clones:
                        # Scegli il "clone principale" (indice più alto se presenti numeri)
                        chosen_clone, chosen_idx = clones[-1]  # perché la lista è ordinata per indice asc
                        print(f"[+] SCALE DOWN: elimino base '{args.server}' e continuo su clone '{chosen_clone.name}'")

                        # Cancella la base via deploy script (coerente con tua toolchain)
                        cmd = ["~/deploy_secure_vm.py","--cleanup", args.server,"--wipe-snaps","--yes"]
                        subprocess.run(" ".join(cmd), shell=True, check=True)

                        # Aggiorna target di monitoraggio al clone scelto
                        args.server = chosen_clone.name
                        base = conn.compute.find_server(args.server)
                        fip = _get_server_fip(conn, base)
                        if not fip:
                            print(f"[!] Il clone '{args.server}' non ha FIP. Uscita.", file=sys.stderr); sys.exit(1)
                        print(f"[i] Ora monitoro il clone '{args.server}' @ {fip}")
                    else:
                        print("[-] SCALE DOWN: niente clone, niente delete (salto).")
                    lo_hits = 0

            except Exception as e:
                print(f"[!] Errore ciclo: {e}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("[+] Autoscaler interrotto dall’utente, uscita pulita.")

if __name__ == "__main__":
    main()
