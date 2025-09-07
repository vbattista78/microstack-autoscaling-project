# Architettura e Flusso Operativo

1. Provisioning rete: creazione network, router, security groups.
2. Deploy `vm-test` con cloud-init.
3. Monitoraggio CPU (telemetria o script).
4. Soglie:
   - >60% → crea `vm-test-clone`.
   - <20% → elimina `vm-test-clone`.
5. Logging e rollback.
