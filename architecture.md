# Architecture OMEGA Fusion Core

## Vue d'ensemble

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLUSTER RE-ZERO                                                    │
│                                                                     │
│  ┌──────────────┐   ┌──────────────┐   ┌──────────────┐           │
│  │    emilia    │   │     ram      │   │     rem      │           │
│  │ 192.168.123  │   │ 192.168.123  │   │ 192.168.123  │           │
│  │    .100      │   │    .101      │   │    .102      │           │
│  │              │   │              │   │              │           │
│  │ RTX 2080 Ti  │   │ RTX 3090 Ti  │   │ RTX 3090 Ti  │           │
│  │ (driver KO*) │   │ (MPS actif)  │   │ (driver KO*) │           │
│  └──────────────┘   └──────┬───────┘   └──────────────┘           │
│                             │                                       │
│  * kernel 6.17 incompatible │                                       │
│    → reboot kernel 6.8.x   │                                       │
│                             │                                       │
│                    ┌────────▼────────────────┐                     │
│                    │     Nœud ram (détail)   │                     │
│                    │                         │                     │
│                    │  ┌───────┐  ┌────────┐  │                     │
│                    │  │VM2380 │  │VM2382  │  │                     │
│                    │  │ZoxBT-1│  │ZoxBT-2 │  │                     │
│                    │  │.111   │  │.112    │  │                     │
│                    │  └───┬───┘  └───┬────┘  │                     │
│                    │      │  HTTP    │        │                     │
│                    │      └────┬─────┘        │                     │
│                    │           ▼              │                     │
│                    │  omega-gpu-proxy:9400    │                     │
│                    │           │              │                     │
│                    │  MPS daemon              │                     │
│                    │  /tmp/nvidia-mps/        │                     │
│                    │           │              │                     │
│                    │  RTX 3090 Ti             │                     │
│                    │  24564 MiB VRAM          │                     │
│                    │           │              │                     │
│                    │  gpu-agent-ZoxBT ───────►│──► GPU_REQUEST     │
│                    │                         │    signal           │
│                    └─────────────────────────┘                     │
│                                                                     │
│  live-migrator daemon lit GPU_REQUEST et migre la VM               │
│  vers le nœud avec le GPU le moins chargé                          │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Flux d'un job GPU

```
1. VM envoie une requête HTTP au proxy
   POST http://192.168.123.101:9400/v1/jobs
   {
     "kind": "matrix_multiply",
     "vm_id": 2380,
     "payload": {"n": 512}
   }

2. omega-gpu-proxy reçoit le job
   → Authentifie via token (X-Omega-GPU-Token)
   → Met en queue (state: "queued")
   → Lance le worker backend

3. omega-gpu-worker-app-cuda exécute le job
   → Utilise PyTorch CUDA (via MPS)
   → Contexte CUDA partagé entre tous les jobs
   → Retourne résultat JSON

4. Proxy retourne le résultat
   {
     "state": "succeeded",
     "duration_ms": 145,
     "result": {
       "device": "cuda",
       "gpu_name": "NVIDIA GeForce RTX 3090 Ti",
       "cuda_available": true,
       "checksum": 33466198.0
     }
   }
```

---

## Flux de détection de saturation

```
1. gpu-agent-ZoxBT surveille toutes les 5s
   nvidia-smi → GPU: X% | VRAM: Y/24564 MiB

2. Si GPU% >= seuil (95% production / 20% test)
   → Interroge omega-daemon pour usage cluster
   → Interroge proxy pour jobs actifs par VM
   → Identifie la VM avec le plus de VRAM

3. Écriture atomique du signal
   /var/lib/live-migrator/signals/
   signal_{timestamp}_gpu_request.tmp
   → renommé en .sig (atomique)

4. live-migrator lit le signal
   → Analyse gpu_nodes_usage
   → Choisit le nœud cible (moins chargé)
   → Migre la VM
   → Écrit la réponse dans signals/responses/
```

---

## Pourquoi MPS et pas les alternatives

### VFIO Passthrough (défaut Proxmox)
- ✓ Performance native 100%
- ✗ **1 seule VM par GPU** — gaspillage total
- ✗ Pas de partage possible

### NVIDIA vGPU (Time-sliced)
- ✓ Isolation logicielle entre VMs
- ✓ Jusqu'à 32 VMs par GPU
- ✗ **Licence payante** — incompatible avec machines récupérées
- ✗ Performance ~85-90%

### NVIDIA MIG (Multi-Instance GPU)
- ✓ Isolation matérielle parfaite
- ✓ Performance ~95%
- ✗ **Seulement sur A100/H100** — pas sur RTX 3090 Ti
- ✗ Nombre d'instances limité (~7)

### CUDA MPS (notre choix)
- ✓ **Gratuit** — drivers NVIDIA standards
- ✓ **Jusqu'à 48 clients simultanés**
- ✓ Performance **~95% natif**
- ✓ Kernels CUDA s'exécutent vraiment en parallèle
- ✓ Compatible RTX 3090 Ti (GA102, Compute Capability 8.6)
- ✗ Pas d'isolation des pannes (si un client crashe, tous crashent)

---

## Différence MPS vs Time-slicing

### Sans MPS (time-slicing)
```
t=0ms  [VM1 kernel ████████]
t=10ms                      [VM2 kernel ████████]
t=20ms                                           [VM1 kernel ████████]
→ Exécution séquentielle, commutation de contexte coûteuse
```

### Avec MPS
```
t=0ms  [VM1 kernel ████████]
       [VM2 kernel ████████]
→ Exécution simultanée, contexte CUDA unique partagé
```

---

## Composants du système

### omega-gpu-proxy (Rust)
- Serveur HTTP léger (tokio)
- Authentification par token Bearer
- Queue de jobs avec priorités
- Lance le worker externe pour chaque job
- Routes: `GET /health`, `POST /v1/jobs`, `GET /v1/jobs/:id`

### omega-gpu-worker-app-cuda (Python/PyTorch)
- Reçoit un job JSON sur stdin
- Exécute via PyTorch CUDA
- Retourne résultat JSON sur stdout
- Backends supportés: matrix_multiply, inference, video_encode, render

### gpu-agent-ZoxBT (Python)
- Surveille nvidia-smi toutes les 5s
- Interroge le proxy pour les jobs par VM
- Interroge omega-daemon pour l'état du cluster
- Écrit les signaux au format live-migrator API
