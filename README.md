# OMEGA Fusion Core — Partage GPU Multi-VM sur Proxmox

> Projet GRID ONE — Équipe OMEGA — ENSPY 2025-2026  
> Étudiant : Balbino Tchoutzine  
> Superviseurs : Ing. Alain Mbo, Ing. Abel Zogning, Ing. Nassair Foupouagnigni

---

## Problème résolu

Par défaut, Proxmox assigne un GPU **exclusivement** à une seule VM via passthrough VFIO.  
Sur un cluster à 3 nœuds avec 3 RTX 3090 Ti, si chaque VM utilise 10% de son GPU,  
**90% des ressources sont gaspillées**.

Ce projet élimine ce gaspillage en permettant à **plusieurs VMs de partager le même GPU simultanément**, sans licence payante.

---

## Architecture

```
Cluster re-zero (3 nœuds)
┌──────────────────────────────────────────────────────────────┐
│  nœud ram (192.168.123.101)                                  │
│                                                              │
│  ┌─────────────────┐    ┌─────────────────────────────────┐ │
│  │ gpu-omega-ZoxBT-1│    │ gpu-omega-ZoxBT-2               │ │
│  │ VM 2380          │    │ VM 2382                         │ │
│  │ 10.50.0.111      │    │ 10.50.0.112                     │ │
│  └────────┬─────────┘    └──────────────┬──────────────────┘ │
│           │  HTTP POST /v1/jobs          │                    │
│           └──────────────┬──────────────┘                    │
│                          ▼                                    │
│              omega-gpu-proxy :9400                           │
│                          │                                    │
│              MPS Daemon (/tmp/nvidia-mps/)                   │
│                          │                                    │
│              RTX 3090 Ti (24564 MiB VRAM)                    │
│                          │                                    │
│         gpu-agent-ZoxBT.py (surveillance)                    │
│                          │                                    │
│         /var/lib/live-migrator/signals/  ←── GPU_REQUEST     │
└──────────────────────────────────────────────────────────────┘
```

---

## Stack technique

| Composant | Rôle |
|---|---|
| Proxmox VE 9.1 | Hyperviseur |
| NVIDIA RTX 3090 Ti | GPU physique (24 Go VRAM) |
| CUDA MPS | Partage GPU multi-processus |
| omega-gpu-proxy | Proxy HTTP entre VMs et GPU |
| gpu-agent-ZoxBT | Agent de surveillance et migration |
| live-migrator | Daemon de migration (équipe DELTA) |

---

## Comparaison avec les alternatives

| Critère | **MPS (notre choix)** | VFIO Passthrough | NVIDIA vGPU |
|---|---|---|---|
| Coût | **Gratuit** | Gratuit | Licence payante |
| VMs par GPU | **Jusqu'à 48** | 1 seule | 16-32 |
| Performance | **~95% natif** | 100% natif | ~90% |
| Migration auto | **Oui** | Non | Partielle |
| Config | **Simple** | Simple | Complexe |

---

## Prérequis

- Proxmox VE 9.x sur chaque nœud
- GPU NVIDIA (Kepler ou plus récent, Compute Capability ≥ 3.5)
- Driver NVIDIA ≥ 470 installé sur l'hôte
- IOMMU activé (`intel_iommu=on iommu=pt` dans le kernel)
- Python 3.11+
- omega-remote-paging installé dans `/opt/omega-remote-paging/`

---

## Installation rapide

### 1. Vérifier les prérequis sur le nœud GPU

```bash
# Vérifier IOMMU
dmesg | grep -i "IOMMU enabled"

# Vérifier le GPU
lspci | grep -i nvidia

# Vérifier les drivers
nvidia-smi
```

### 2. Installer CUDA MPS

```bash
apt-get install -y nvidia-cuda-mps
```

### 3. Activer MPS

```bash
# Mode exclusif GPU
nvidia-smi -i 0 -c EXCLUSIVE_PROCESS

# Démarrer le daemon MPS
export CUDA_MPS_PIPE_DIRECTORY=/tmp/nvidia-mps
export CUDA_MPS_LOG_DIRECTORY=/tmp/nvidia-log
mkdir -p $CUDA_MPS_PIPE_DIRECTORY $CUDA_MPS_LOG_DIRECTORY
nvidia-cuda-mps-control -d

# Vérifier
ps aux | grep mps
ls /tmp/nvidia-mps/
```

### 4. Démarrer le proxy GPU

```bash
# Utiliser la config omega existante
systemctl start omega-gpu-proxy

# Ou manuellement avec paramètres étendus
/opt/omega-remote-paging/bin/omega-gpu-proxy \
  --listen 0.0.0.0:9400 \
  --node-id ram \
  --max-concurrent-jobs 8 \
  --max-matrix-n 4096 \
  --backend-command "/opt/omega-remote-paging/workers/omega-gpu-worker-app-cuda" \
  --api-token-file /etc/omega/gpu-proxy.token \
  --log-level info &
```

### 5. Installer l'agent GPU

```bash
cp agents/gpu-agent-ZoxBT.py /opt/omega-remote-paging/bin/
chmod +x /opt/omega-remote-paging/bin/gpu-agent-ZoxBT.py
python3 /opt/omega-remote-paging/bin/gpu-agent-ZoxBT.py
```

---

## Création des VMs de test

### Cloner une VM omega existante

```bash
# Clone VM 1 sur ram
qm clone 2371 2380 --name gpu-omega-ZoxBT-1 --full 1 --storage ceph-vm

# Clone VM 2 sur ram
qm clone 2371 2382 --name gpu-omega-ZoxBT-2-ram --full 1 --storage ceph-vm

# Démarrer les VMs
qm start 2380
qm start 2382
```

### Configurer le réseau dans chaque VM

```bash
# Dans chaque VM via qm terminal
ip link set enp6s18 up
ip addr add 10.50.0.111/24 dev enp6s18   # VM 2380
ip route add default via 10.50.0.1

# Rendre persistant
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
```

---

## Test de partage GPU simultané

### Soumettre des jobs depuis les 2 VMs simultanément

```bash
TOKEN=$(cat /etc/omega/gpu-proxy.token)

JOB1=$(ssh root@10.50.0.111 "curl -s -X POST http://192.168.123.101:9400/v1/jobs \
  -H 'X-Omega-GPU-Token: $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{\"kind\":\"matrix_multiply\",\"vm_id\":2380,\"payload\":{\"n\":512}}'")

JOB2=$(ssh root@10.50.0.112 "curl -s -X POST http://192.168.123.101:9400/v1/jobs \
  -H 'X-Omega-GPU-Token: $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{\"kind\":\"matrix_multiply\",\"vm_id\":2382,\"payload\":{\"n\":512}}'")

echo "VM1: $JOB1"
echo "VM2: $JOB2"
```

### Résultat attendu

```json
VM1: {"job_id":"...", "vm_id":2380, "state":"queued", "submitted_at":"2026-05-20T14:45:29.627Z"}
VM2: {"job_id":"...", "vm_id":2382, "state":"queued", "submitted_at":"2026-05-20T14:45:29.851Z"}
```

Les deux jobs démarrent quasi simultanément et terminent en ~1.5s chacun sur le **même GPU physique**.

### Vérifier les résultats

```bash
ID1=$(echo $JOB1 | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
ID2=$(echo $JOB2 | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")

sleep 5

curl -s http://127.0.0.1:9400/v1/jobs/$ID1 -H "X-Omega-GPU-Token: $TOKEN" | python3 -m json.tool
curl -s http://127.0.0.1:9400/v1/jobs/$ID2 -H "X-Omega-GPU-Token: $TOKEN" | python3 -m json.tool
```

### Preuve de parallélisme

| | VM 2380 (gpu-omega-ZoxBT-1) | VM 2382 (gpu-omega-ZoxBT-2) |
|---|---|---|
| Soumis à | `14:45:29.627` | `14:45:29.851` |
| GPU | RTX 3090 Ti | RTX 3090 Ti |
| CUDA | true | true |
| Durée | **1593 ms** | **1586 ms** |
| Checksum | 33466198.0 | 33466198.0 |

> Les deux VMs ont utilisé le **même GPU physique** en parallèle via MPS.  
> Sans MPS : ~3s en séquence. Avec MPS : ~1.5s en parallèle.

---

## Agent GPU — gpu-agent-ZoxBT

L'agent surveille le GPU toutes les 5 secondes et envoie un signal `GPU_REQUEST` au live-migrator quand l'utilisation dépasse le seuil configuré.

### Lancer l'agent

```bash
python3 /opt/omega-remote-paging/bin/gpu-agent-ZoxBT.py
```

### Sortie type

```
[2026-05-20T15:45:13Z] [gpu-agent-ZoxBT] GPU:11% VRAM:563/24564MiB aucune VM
[2026-05-20T15:45:18Z] [gpu-agent-ZoxBT] GPU:22% VRAM:1574/24564MiB aucune VM
[2026-05-20T15:45:18Z] [gpu-agent-ZoxBT] SEUIL ATTEINT 22%>=20%
[2026-05-20T15:45:18Z] [gpu-agent-ZoxBT] Signal envoye vmid=2380 -> /var/lib/live-migrator/signals/signal_1779291918_gpu_request.sig
```

### Signal généré

```
type=GPU_REQUEST
vmid=2380
source_agent=gpu-agent-ZoxBT
reason=gpu_saturation
urgency=high
gpu_nodes_usage=ram:22,emilia:none,rem:none
timestamp=2026-05-20T15:45:18
```

---

## Structure du projet

```
omega-fusion-core/
├── README.md                    ← ce fichier
├── agents/
│   └── gpu-agent-ZoxBT.py      ← agent de surveillance GPU
├── configs/
│   ├── cluster.env.example     ← variables d'environnement
│   └── omega-net.service       ← service réseau VMs
├── scripts/
│   ├── setup-mps.sh            ← installation MPS
│   ├── test-gpu-sharing.sh     ← test partage GPU
│   └── create-vms.sh           ← création VMs de test
└── docs/
    ├── architecture.md         ← architecture détaillée
    └── troubleshooting.md      ← dépannage
```

---

## Dépannage rapide

| Problème | Solution |
|---|---|
| `nvidia-smi` échoue | `modprobe nvidia` ou vérifier blacklist |
| MPS ne démarre pas | `nvidia-smi -c EXCLUSIVE_PROCESS` d'abord |
| VM perd son IP | Vérifier service `omega-net` dans la VM |
| Job refusé `n > 512` | Relancer proxy avec `--max-matrix-n 4096` |
| Lock VM bloqué | `fuser /var/lock/qemu-server/lock-XXXX.conf` puis `kill -9` |
| DKMS échoue kernel 7.0 | Normal — kernel actif 6.8.x fonctionne |

---

## Prochaines étapes

- [ ] Configurer MPS sur `emilia` et `rem` (nécessite reboot sur kernel 6.8.x)
- [ ] Implémenter le time-slicing CPU avec ratio 3:1
- [ ] Constituer la bibliothèque de 10 images OS métier
- [ ] Benchmark libération ressources CPU/GPU à l'arrêt d'une VM
