# Dépannage — OMEGA Fusion Core

## Problèmes fréquents et solutions

---

### nvidia-smi échoue sur le nœud

**Erreur:**
```
NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver.
```

**Cause:** Le driver est blacklisté (configuration Proxmox par défaut pour VFIO).

**Vérification:**
```bash
cat /etc/modprobe.d/*.conf | grep -i "blacklist nvidia"
```

**Solution si blacklist actif (non commenté):**
```bash
# Vérifier que le blacklist est commenté
# blacklist nvidia  ← doit avoir le # devant
# Si pas commenté, commenter et recharger:
update-initramfs -u
reboot
```

**Solution si blacklist commenté mais driver non chargé:**
```bash
modprobe nvidia
nvidia-smi
```

**Solution si kernel incompatible (ex: 6.17 sur emilia):**
```bash
# Vérifier les kernels disponibles
proxmox-boot-tool kernel list

# Choisir un kernel 6.8.x
proxmox-boot-tool kernel pin 6.8.12-24-pve
reboot
```

---

### MPS ne démarre pas

**Erreur:**
```
nvidia-cuda-mps-control: command not found
```

**Solution:**
```bash
apt-get install -y nvidia-cuda-mps
```

**Erreur:**
```
MPS daemon failed to start
```

**Cause probable:** GPU pas en mode EXCLUSIVE_PROCESS.

**Solution:**
```bash
nvidia-smi -i 0 -c EXCLUSIVE_PROCESS
nvidia-cuda-mps-control -d
```

---

### VM bloquée avec lock file

**Erreur:**
```
can't lock file '/var/lock/qemu-server/lock-XXXX.conf' - got timeout
```

**Solution:**
```bash
# Identifier le processus qui tient le lock
lsof /var/lock/qemu-server/lock-XXXX.conf
fuser /var/lock/qemu-server/lock-XXXX.conf

# Tuer le processus
kill -9 $(fuser /var/lock/qemu-server/lock-XXXX.conf 2>/dev/null)

# Supprimer le lock
rm -f /var/lock/qemu-server/lock-XXXX.conf

# Redémarrer la VM
qm start XXXX
```

---

### VM perd son IP au redémarrage

**Cause:** Le DHCP de vmbr1 (10.50.0.x) ne répond pas toujours.

**Solution permanente dans la VM:**
```bash
cat > /etc/systemd/system/omega-net.service << 'EOF'
[Unit]
Description=Omega Network Setup
After=network.target
[Service]
Type=oneshot
ExecStart=/bin/bash -c "ip link set enp6s18 up; ip addr add 10.50.0.111/24 dev enp6s18 || true; ip route add default via 10.50.0.1 || true"
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
systemctl enable omega-net
systemctl start omega-net
```

> Adapter `10.50.0.111` selon la VM (111 pour ZoxBT-1, 112 pour ZoxBT-2).

---

### Proxy GPU rejette les jobs avec n > 512

**Erreur:**
```json
{"error": "matrix_multiply invalide: n=512 doit être entre 1 et 512"}
```

**Cause:** Le service systemd `omega-gpu-proxy` utilise la config par défaut `--max-matrix-n 512`.

**Solution:** Relancer manuellement avec une limite plus haute:
```bash
systemctl stop omega-gpu-proxy

/opt/omega-remote-paging/bin/omega-gpu-proxy \
  --listen 0.0.0.0:9400 \
  --node-id ram \
  --max-concurrent-jobs 8 \
  --max-matrix-n 4096 \
  --backend-command "/opt/omega-remote-paging/workers/omega-gpu-worker-app-cuda" \
  --api-token-file /etc/omega/gpu-proxy.token \
  --log-level info &
```

---

### DKMS échoue sur kernel 7.0.0

**Erreur:**
```
Error! Bad return status for module build on kernel: 7.0.0-3-pve
```

**Cause:** Le driver NVIDIA 535/550 n'est pas encore compatible avec le kernel 7.0.

**Solution:** Ce kernel n'est pas utilisé en production (kernel actif: 6.8.12-9-pve). Ignorer l'erreur ou exclure le kernel 7.0 de DKMS:
```bash
dkms remove nvidia-current/550.163.01 -k 7.0.0-3-pve --no-depmod 2>/dev/null || true
dpkg --configure nvidia-kernel-dkms nvidia-driver
```

---

### Agent GPU ne détecte pas les VMs

**Symptôme:** L'agent affiche "aucune VM active" alors que des jobs sont en cours.

**Cause:** Les jobs finissent trop vite — le proxy les garde dans l'historique mais ils passent à `succeeded` avant la prochaine vérification de l'agent.

**Workaround:** Soumettre beaucoup de jobs en boucle rapide, ou baisser le seuil pour les tests:
```bash
# Modifier le seuil dans l'agent (ligne GPU_THRESHOLD_PCT)
sed -i 's/GPU_THRESHOLD_PCT   = 95/GPU_THRESHOLD_PCT   = 5/' \
  /opt/omega-remote-paging/bin/gpu-agent-ZoxBT.py
```

---

### Clonage Ceph lent ou bloqué

**Symptôme:** Le clonage reste bloqué à 77% pendant plusieurs minutes.

**Cause:** Un OSD Ceph est en état `BLUESTORE_SLOW_OP_ALERT`.

**Vérification:**
```bash
ceph health detail
ceph osd tree | grep osd
```

**Solution:** Attendre que l'OSD se stabilise (généralement <10 min). Ne jamais interrompre un clonage Ceph en cours — cela corrompt le volume.

---

## Checklist de démarrage rapide

```bash
# Sur le nœud ram, dans l'ordre:

# 1. Vérifier MPS
ps aux | grep mps
ls /tmp/nvidia-mps/

# 2. Si MPS absent, le démarrer
nvidia-smi -i 0 -c EXCLUSIVE_PROCESS
export CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps
export CUDA_MPS_LOG_DIRECTORY=/tmp/nvidia-log
mkdir -p $CUDA_MPS_PIPE_DIRECTORY $CUDA_MPS_LOG_DIRECTORY
nvidia-cuda-mps-control -d

# 3. Vérifier le proxy GPU
curl -s http://127.0.0.1:9400/health

# 4. Vérifier les VMs
qm list | grep gpu-omega

# 5. Vérifier les IPs des VMs
ping -c 1 10.50.0.111
ping -c 1 10.50.0.112

# 6. Lancer l'agent
python3 /opt/omega-remote-paging/bin/gpu-agent-ZoxBT.py &

# 7. Lancer le test
bash scripts/test-gpu-sharing.sh
```
