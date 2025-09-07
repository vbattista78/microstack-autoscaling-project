# microstack-autoscaling-project
Autoscaling Demo on MicroStack/OpenStack with VM Cloning
## ▶️ Avvio rapido
```bash
# 1) Reti & sicurezza
bash scripts/setup_networking.sh

# 2) VM di test
bash scripts/create_vm_test.sh

# 3) Autoscaler (soglie 60% / 20%)
python3 scripts/autoscale_monitor.py
