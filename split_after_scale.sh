#!/usr/bin/env bash
set -euo pipefail

# === Config ===
USER="cirros"
KEY="${KEY:-~/.ssh/lab-key-rsa}"
SSH_OPTS=(
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o IdentitiesOnly=yes
  -o BatchMode=yes
  -o PubkeyAuthentication=yes
  -o PubkeyAcceptedAlgorithms=+ssh-rsa
  -o HostKeyAlgorithms=+ssh-rsa
  -i "${KEY/#\~/$HOME}"
)

# PID files remoti
PID_100="/tmp/loadgen100.pid"   # più PID (uno per vCPU), uno per riga
PID_50="/tmp/loadgen50.pid"

info(){ echo "[*] $*"; }
warn(){ echo "[-] $*" >&2; }
err(){  echo "[!] $*" >&2; exit 1; }

# Variabili globali
BASE_NAME=""; BASE_FIP=""
CLONE_NAME=""; CLONE_FIP=""

# -----------------------------
# Helpers OpenStack/Microstack
# -----------------------------

pick_best_ip_from_addresses() {
  local addr_str="$1"
  local ips=()
  while read -r ip; do [[ -n "$ip" ]] && ips+=("$ip"); done < <(grep -Eo '([0-9]{1,3}\.){3}[0-9]{1,3}' <<< "$addr_str" || true)
  (( ${#ips[@]} )) || { echo ""; return 0; }
  for ip in "${ips[@]}"; do [[ "$ip" =~ ^10\. ]] && { echo "$ip"; return 0; }; done
  echo "${ips[-1]}"
}

get_fip_by_name() {
  local name="$1" addresses
  addresses="$(microstack.openstack server show "$name" -f value -c addresses || true)"
  pick_best_ip_from_addresses "$addresses"
}

get_created_epoch() {
  local name="$1" created
  created="$(microstack.openstack server show "$name" -f value -c created 2>/dev/null | head -n1 || true)"
  date -d "$created" +%s 2>/dev/null || echo 0
}

list_active_instances() {
  local line name status fip
  while IFS= read -r line; do
    name="$(awk '{print $1}' <<< "$line")"
    status="$(awk '{print $2}' <<< "$line")"
    [[ -n "$name" && "${status^^}" == "ACTIVE" ]] || continue
    fip="$(get_fip_by_name "$name")"
    echo "$name ${fip:-}"
  done < <(microstack.openstack server list -f value -c Name -c Status)
}

choose_instance_prompt() {
  mapfile -t LINES < <(list_active_instances)
  (( ${#LINES[@]} )) || err "Nessuna istanza ACTIVE trovata."

  echo
  echo "Seleziona la VM (ACTIVE) su cui applicare il carico:"
  local i name fip
  for i in "${!LINES[@]}"; do
    name="$(awk '{print $1}' <<< "${LINES[$i]}")"
    fip="$(awk '{print $2}' <<< "${LINES[$i]}")"
    printf "  %2d) %s  %s\n" "$((i+1))" "$name" "${fip:+@ $fip}"
  done

  while true; do
    read -rp "Numero VM: " n < /dev/tty || { warn "Impossibile leggere dal TTY."; exit 1; }
    [[ "$n" =~ ^[0-9]+$ ]] || { warn "Inserisci un numero valido."; continue; }
    (( n>=1 && n<=${#LINES[@]} )) || { warn "Scelta fuori intervallo."; continue; }
    local sel="${LINES[$((n-1))]}"
    BASE_NAME="$(awk '{print $1}' <<< "$sel")"
    BASE_FIP="$(awk '{print $2}' <<< "$sel")"
    [[ -n "$BASE_NAME" ]] || { warn "Nome VM vuoto, riprova."; continue; }
    [[ -n "$BASE_FIP" ]] || BASE_FIP="$(get_fip_by_name "$BASE_NAME")"
    [[ -n "$BASE_FIP" ]] || err "La VM scelta non ha un IP raggiungibile (FIP)."
    break
  done
}

clone_prefix() { echo "${1}_clone"; }

find_existing_clone() {
  local base="$1" pref cname cfip
  pref="$(clone_prefix "$base")"
  while IFS= read -r cname; do
    [[ -n "$cname" ]] || continue
    [[ "$cname" =~ ^${pref}(_[0-9]+)?$ ]] || continue
    cfip="$(get_fip_by_name "$cname")"
    [[ -n "$cfip" ]] || continue
    echo "$cname $cfip"
    break
  done < <(microstack.openstack server list -f value -c Name)
}

wait_clone_ready_setvars() {
  local base="$1" retries=90 sleep_s=2 pref
  pref="$(clone_prefix "$base")"
  info "Attendo la nascita del clone con prefisso '${pref}' (max ~$((retries*sleep_s/60)) min)..."
  CLONE_NAME=""; CLONE_FIP=""
  for _ in $(seq 1 "$retries"); do
    while IFS= read -r cname; do
      [[ -n "$cname" ]] || continue
      [[ "$cname" =~ ^${pref}(_[0-9]+)?$ ]] || continue
      local cfip; cfip="$(get_fip_by_name "$cname")"
      [[ -n "$cfip" ]] || continue
      if ssh -q "${SSH_OPTS[@]}" "${USER}@${cfip}" "true"; then
        CLONE_NAME="$cname"; CLONE_FIP="$cfip"; return 0
      fi
    done < <(microstack.openstack server list -f value -c Name)
    sleep "$sleep_s"
  done
  return 1
}

# -------------------
# Carichi CPU remoti
# -------------------

# 100% su tutte le vCPU (un 'yes' per core)
start_100() {
  local host="$1"
  ssh "${SSH_OPTS[@]}" "${USER}@${host}" "sh -s" <<'EOF'
set -e
PID_FILE="/tmp/loadgen100.pid"
[ -f "$PID_FILE" ] && rm -f "$PID_FILE"
NCPU="$(command -v nproc >/dev/null 2>&1 && nproc || grep -c ^processor /proc/cpuinfo || echo 1)"
[ -z "$NCPU" ] && NCPU=1; [ "$NCPU" -lt 1 ] && NCPU=1
for _ in $(seq 1 "$NCPU"); do
  ( nohup sh -c 'yes > /dev/null' >/dev/null 2>&1 & echo $! >> "$PID_FILE" ) &
done
wait
EOF
}

# Stop 100% robusto (compatibile BusyBox/ash, senza pgrep/pkill)
stop_100() {
  local host="$1"
  ssh "${SSH_OPTS[@]}" "${USER}@${host}" "sh -s" <<'EOF'
set -e
PID_FILE="/tmp/loadgen100.pid"
if [ -f "$PID_FILE" ]; then
  while read -r p; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done < "$PID_FILE"
  sleep 0.3
  while read -r p; do [ -n "$p" ] && kill -9 "$p" 2>/dev/null || true; done < "$PID_FILE"
  rm -f "$PID_FILE"
fi
if command -v killall >/dev/null 2>&1; then
  killall -q yes 2>/dev/null || true
else
  for _ in 1 2; do
    PS="$(ps w 2>/dev/null | tr -s ' ')"
    echo "$PS" | grep -E '(^|[ /])yes( |$)' >/dev/null 2>&1 || break
    for pid in $(echo "$PS" | awk '/(^|[ \/])yes( |$)/{print $1}'); do
      kill -9 "$pid" 2>/dev/null || true
    done
    sleep 0.2
  done
fi
EOF
}

# ~50% CPU con duty-cycle breve
start_50() {
  local host="$1"
  ssh "${SSH_OPTS[@]}" "${USER}@${host}" "nohup sh -c '
    while :; do
      yes > /dev/null & p=\$!
      sleep 0.2
      kill \$p 2>/dev/null || true
      sleep 0.2
    done
  ' >/dev/null 2>&1 & echo \$! > ${PID_50}"
}

stop_50() {
  local host="$1"
  ssh "${SSH_OPTS[@]}" "${USER}@${host}" "sh -s" <<'EOF'
set -e
PID_FILE="/tmp/loadgen50.pid"
if [ -f "$PID_FILE" ]; then
  P="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$P" ]; then kill "$P" 2>/dev/null || true; fi
  rm -f "$PID_FILE"
fi
if command -v killall >/dev/null 2>&1; then
  killall -q yes 2>/dev/null || true
fi
EOF
}

stop_all_on_host() {
  local host="$1"
  ssh "${SSH_OPTS[@]}" "${USER}@${host}" "sh -s" <<'EOF'
set -e
PID100="/tmp/loadgen100.pid"
PID50="/tmp/loadgen50.pid"
if [ -f "$PID100" ]; then
  while read -r p; do [ -n "$p" ] && kill "$p" 2>/dev/null || true; done < "$PID100"
  sleep 0.3
  while read -r p; do [ -n "$p" ] && kill -9 "$p" 2>/dev/null || true; done < "$PID100"
  rm -f "$PID100"
fi
if [ -f "$PID50" ]; then
  P="$(cat "$PID50" 2>/dev/null || true)"
  if [ -n "$P" ]; then kill "$P" 2>/dev/null || true; fi
  rm -f "$PID50"
fi
if command -v killall >/dev/null 2>&1; then
  killall -q yes 2>/dev/null || true
else
  PS="$(ps w 2>/dev/null | tr -s " ")"
  for pid in $(echo "$PS" | awk '/(^|[ \/])yes( |$)/{print $1}'); do
    kill -9 "$pid" 2>/dev/null || true
  done
fi
EOF
}

# -------------------
# STOP AUTOMATICO --stop (nuovo comportamento)
# -------------------

auto_stop_both_in_pairs() {
  # Trova coppie base/clone attive e ferma i carichi su ENTRAMBE le VM di ogni coppia.
  mapfile -t ACTIVE_NAMES < <(microstack.openstack server list -f value -c Name -c Status | awk '$2=="ACTIVE"{print $1}')
  (( ${#ACTIVE_NAMES[@]} )) || return 1

  # Presence map
  declare -A PRESENT=()
  for n in "${ACTIVE_NAMES[@]}"; do PRESENT["$n"]=1; done

  # Evita duplicati per base
  declare -A PAIRS_SEEN=()
  local found=0

  for n in "${ACTIVE_NAMES[@]}"; do
    if [[ "$n" =~ ^(.+)_clone(_[0-9]+)?$ ]]; then
      local base="${BASH_REMATCH[1]}"
      # se esiste la base attiva, abbiamo una coppia
      if [[ -n "${PRESENT[$base]:-}" && -z "${PAIRS_SEEN[$base]:-}" ]]; then
        local base_fip clone_fip
        base_fip="$(get_fip_by_name "$base")"
        clone_fip="$(get_fip_by_name "$n")"
        if [[ -n "$base_fip" && -n "$clone_fip" ]]; then
          info "Stop automatico dei carichi sulla coppia: ${base} @ ${base_fip}  +  ${n} @ ${clone_fip}"
          stop_all_on_host "$base_fip"
          stop_all_on_host "$clone_fip"
          found=1
          PAIRS_SEEN["$base"]=1
        fi
      fi
    fi
  done

  [[ $found -eq 1 ]] || return 1
  info "Fatto."
  return 0
}

# -------------------
# STOP dinamico (fallback)
# -------------------

stop_mode_prompt() {
  mapfile -t LINES < <(list_active_instances)
  (( ${#LINES[@]} )) || err "Nessuna istanza ACTIVE trovata per lo stop."

  echo
  echo "Seleziona la VM su cui fermare i carichi:"
  local i name fip
  for i in "${!LINES[@]}"; do
    name="$(awk '{print $1}' <<< "${LINES[$i]}")"
    fip="$(awk '{print $2}' <<< "${LINES[$i]}")"
    printf "  %2d) %s  %s\n" "$((i+1))" "$name" "${fip:+@ $fip}"
  done

  while true; do
    read -rp "Numero VM: " n < /dev/tty
    [[ "$n" =~ ^[0-9]+$ ]] || { warn "Inserisci un numero valido."; continue; }
    (( n>=1 && n<=${#LINES[@]} )) || { warn "Scelta fuori intervallo."; continue; }
    local sel="${LINES[$((n-1))]}"
    local name2 fip2
    name2="$(awk '{print $1}' <<< "$sel")"
    fip2="$(awk '{print $2}' <<< "$sel")"
    [[ -n "$name2" && -n "$fip2" ]] || { warn "Selezione non valida, riprova."; continue; }
    info "Stop carichi su ${name2} @ ${fip2}"
    stop_all_on_host "$fip2"
    info "Fatto."
    break
  done
}

# -----------
# Main flow
# -----------

usage(){
  cat <<EOF
Uso:
  $(basename "$0")             # scegli base, picco >60%, attendi clone pronto, bilancia 50/50
  $(basename "$0") --stop      # ARRESTA i carichi su entrambe le VM di ogni coppia base/clone; fallback a prompt se nessuna coppia trovata
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then usage; exit 0; fi

if [[ "${1:-}" == "--stop" ]]; then
  if ! auto_stop_both_in_pairs; then
    # Nessuna coppia trovata → fallback a prompt
    stop_mode_prompt
  fi
  exit 0
fi

# 1) Scegli la base tra le ACTIVE
choose_instance_prompt
info "Base scelta: ${BASE_NAME} @ ${BASE_FIP}"

# 2) Clone già presente?
read -r CNAME_EXIST CFIP_EXIST <<< "$(find_existing_clone "$BASE_NAME" || true)"
if [[ -n "${CNAME_EXIST:-}" && -n "${CFIP_EXIST:-}" ]]; then
  info "Clone già presente: ${CNAME_EXIST} @ ${CFIP_EXIST} → salto fase 100% sulla base."
  info "Avvio 50% su base e clone…"
  start_50 "$CFIP_EXIST"     # prima sul clone
  start_50 "$BASE_FIP"       # poi sulla base
  info "Bilanciamento attivo. Usa '--stop' per fermare i carichi."
  ssh "${SSH_OPTS[@]}" "${USER}@${BASE_FIP}"  "printf '[diag base ] yes-procs: '; ps w | awk '/(^|[ \\/])yes( |$)/{c++} END{print (c?c:0)}'"
  ssh "${SSH_OPTS[@]}" "${USER}@${CFIP_EXIST}" "printf '[diag clone] yes-procs: '; ps w | awk '/(^|[ \\/])yes( |$)/{c++} END{print (c?c:0)}'"
  exit 0
fi

# 3) Innesca picco >60% sulla base
info "Nessun clone pronto. Avvio 100% CPU su ${BASE_NAME} per far scattare lo scale-up…"
start_100 "$BASE_FIP"

# 4) Attendi clone pronto (FIP + SSH)
if wait_clone_ready_setvars "$BASE_NAME"; then
  if [[ -z "$CLONE_NAME" || -z "$CLONE_FIP" ]]; then
    warn "Clone trovato ma variabili vuote. Fermo il carico 100% e termino."
    stop_100 "$BASE_FIP"; exit 1
  fi
  info "Clone pronto: ${CLONE_NAME} @ ${CLONE_FIP}"
else
  warn "Timeout in attesa del clone. Fermo il carico 100% sulla base e termino."
  stop_100 "$BASE_FIP"; exit 1
fi

# 5) Bilancia 50/50 — ordine sicuro + pausa + verifica residui
info "Arresto 100% sulla base e avvio bilanciamento 50/50…"
stop_100 "$BASE_FIP"
sleep 1.0
ssh "${SSH_OPTS[@]}" "${USER}@${BASE_FIP}" "if command -v killall >/dev/null 2>&1; then killall -q yes 2>/dev/null || true; else PS=\$(ps w 2>/dev/null | tr -s ' '); for pid in \$(echo \"\$PS\" | awk '/(^|[ \\/])yes( |$)/{print \$1}'); do kill -9 \"\$pid\" 2>/dev/null || true; done; fi"

start_50 "$CLONE_FIP"
start_50 "$BASE_FIP"

info "Bilanciamento attivo tra ${BASE_NAME} @ ${BASE_FIP} e ${CLONE_NAME} @ ${CLONE_FIP}."
ssh "${SSH_OPTS[@]}" "${USER}@${BASE_FIP}"  "printf '[diag base ] yes-procs: '; ps w | awk '/(^|[ \\/])yes( |$)/{c++} END{print (c?c:0)}'"
ssh "${SSH_OPTS[@]}" "${USER}@${CLONE_FIP}" "printf '[diag clone] yes-procs: '; ps w | awk '/(^|[ \\/])yes( |$)/{c++} END{print (c?c:0)}'"

info "Per interrompere i carichi, esegui: $(basename "$0") --stop"

