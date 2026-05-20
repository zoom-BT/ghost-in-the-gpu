#!/usr/bin/env python3
"""
Agent GPU OMEGA — Fusion Core — ZoxBT
======================================
Surveille l'utilisation GPU sur le noeud ram et envoie un signal
GPU_REQUEST au daemon live-migrator quand le seuil est atteint.

Auteur  : Balbino Tchoutzine
Equipe  : OMEGA Fusion Core — ENSPY 2025-2026
Version : 1.0
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone

# ── Configuration ──────────────────────────────────────────────────────────────
GPU_THRESHOLD_PCT   = 95    # Seuil de saturation en % (20 pour les tests)
SIGNAL_DIR          = "/var/lib/live-migrator/signals"
CHECK_INTERVAL_SECS = 5     # Intervalle de surveillance en secondes
COOLDOWN_SECS       = 60    # Délai minimum entre deux signaux
NODE_ID             = os.environ.get("OMEGA_NODE_ID", "ram")
GPU_PROXY_URL       = os.environ.get("OMEGA_GPU_PROXY_URL", "http://192.168.123.101:9400")
TOKEN_FILE          = "/etc/omega/gpu-proxy.token"

# Noeuds GPU du cluster re-zero
GPU_NODES = {
    "ram":    "192.168.123.101",
    "emilia": "192.168.123.100",
    "rem":    "192.168.123.102",
}

last_signal_time = 0


def log(msg):
    """Log avec timestamp UTC."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] [gpu-agent-ZoxBT] {msg}", flush=True)


def get_gpu_usage():
    """
    Retourne (utilisation_pct, vram_utilisee_mib, vram_totale_mib)
    via nvidia-smi.
    """
    try:
        out = subprocess.check_output([
            "nvidia-smi",
            "--query-gpu=utilization.gpu,memory.used,memory.total",
            "--format=csv,noheader,nounits"
        ], text=True).strip()
        parts = out.split(",")
        return int(parts[0].strip()), int(parts[1].strip()), int(parts[2].strip())
    except Exception as e:
        log(f"Erreur nvidia-smi: {e}")
        return 0, 0, 0


def get_jobs_by_vm():
    """
    Interroge le proxy GPU pour obtenir les jobs récents par VM.
    Retourne un dict {vmid: {jobs, vram_mib, dur}} pour les jobs actifs.
    """
    try:
        token = open(TOKEN_FILE).read().strip()
        out = subprocess.check_output([
            "curl", "-s", "--connect-timeout", "2",
            "-H", f"X-Omega-GPU-Token: {token}",
            f"{GPU_PROXY_URL}/v1/jobs"
        ], text=True)
        jobs = json.loads(out) if out.strip() else []
        vm_stats = {}
        for job in (jobs if isinstance(jobs, list) else []):
            vmid  = job.get("vm_id")
            state = job.get("state", "")
            vram  = job.get("vram_mib", 0)
            dur   = job.get("duration_ms")
            if vmid and state in ("running", "queued", "succeeded"):
                if vmid not in vm_stats:
                    vm_stats[vmid] = {"jobs": 0, "vram_mib": 0, "dur": None}
                vm_stats[vmid]["jobs"]    += 1
                vm_stats[vmid]["vram_mib"] += vram
                if dur:
                    vm_stats[vmid]["dur"] = dur
        return vm_stats
    except Exception:
        return {}


def get_gpu_usage_all_nodes():
    """
    Récupère l'utilisation GPU sur tous les noeuds du cluster
    via l'API omega-daemon (port 9300).
    Retourne un dict {noeud: pourcentage_ou_"none"}.
    """
    usage = {}
    for node, ip in GPU_NODES.items():
        try:
            out = subprocess.check_output([
                "curl", "-s", "--connect-timeout", "2",
                f"http://{ip}:9300/control/status"
            ], text=True)
            data = json.loads(out)
            gpu_util = data.get("gpu_util_pct", None)
            if gpu_util is None and node == NODE_ID:
                gpu_util, _, _ = get_gpu_usage()
            usage[node] = gpu_util if gpu_util is not None else "none"
        except Exception:
            usage[node] = get_gpu_usage()[0] if node == NODE_ID else "none"
    return usage


def send_gpu_request(vmid, gpu_nodes_usage):
    """
    Écrit un fichier signal GPU_REQUEST dans le répertoire live-migrator.
    Écriture atomique : fichier .tmp renommé en .sig.
    Respecte le cooldown pour éviter les rafales de signaux.
    """
    global last_signal_time
    now = time.time()

    if now - last_signal_time < COOLDOWN_SECS:
        remaining = int(COOLDOWN_SECS - (now - last_signal_time))
        log(f"Cooldown actif — prochain signal dans {remaining}s")
        return

    os.makedirs(SIGNAL_DIR, exist_ok=True)

    ts_unix = int(now)
    ts_iso  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    # Format: "ram:22,emilia:none,rem:none"
    gpu_usage_str = ",".join(f"{n}:{p}" for n, p in gpu_nodes_usage.items())

    content = (
        f"type=GPU_REQUEST\n"
        f"vmid={vmid}\n"
        f"source_agent=gpu-agent-ZoxBT\n"
        f"reason=gpu_saturation_{gpu_nodes_usage.get(NODE_ID, 0)}_percent\n"
        f"urgency=high\n"
        f"gpu_nodes_usage={gpu_usage_str}\n"
        f"timestamp={ts_iso}\n"
    )

    tmp_path = f"{SIGNAL_DIR}/signal_{ts_unix}_gpu_request.tmp"
    sig_path = f"{SIGNAL_DIR}/signal_{ts_unix}_gpu_request.sig"

    # Écriture atomique (evite lecture partielle par live-migrator)
    with open(tmp_path, "w") as f:
        f.write(content)
    os.rename(tmp_path, sig_path)

    last_signal_time = now
    log(f"Signal GPU_REQUEST envoye — vmid={vmid}")
    log(f"Fichier: {sig_path}")
    log(f"Contenu:\n{content}")


def main():
    log(f"Agent GPU demarre — seuil={GPU_THRESHOLD_PCT}% noeud={NODE_ID}")
    log(f"Proxy GPU: {GPU_PROXY_URL}")
    log(f"Repertoire signaux: {SIGNAL_DIR}")
    log(f"Intervalle: {CHECK_INTERVAL_SECS}s | Cooldown: {COOLDOWN_SECS}s")

    while True:
        gpu_pct, mem_used, mem_total = get_gpu_usage()
        vm_stats = get_jobs_by_vm()

        # Affichage des stats par VM si disponible
        if vm_stats:
            for vmid, stats in vm_stats.items():
                log(f"GPU:{gpu_pct}% | VM{vmid}: {stats['jobs']} jobs | "
                    f"VRAM:{stats['vram_mib']}MiB | dur:{stats['dur']}ms")
        else:
            log(f"GPU:{gpu_pct}% | VRAM:{mem_used}/{mem_total}MiB | aucune VM active")

        # Déclenchement si seuil atteint
        if gpu_pct >= GPU_THRESHOLD_PCT:
            log(f"SEUIL ATTEINT ({gpu_pct}% >= {GPU_THRESHOLD_PCT}%) — analyse migration...")

            gpu_nodes_usage = get_gpu_usage_all_nodes()
            log(f"Usage cluster: {gpu_nodes_usage}")

            if vm_stats:
                # Migre la VM qui utilise le plus de VRAM
                victim = max(vm_stats, key=lambda v: vm_stats[v]["vram_mib"])
                log(f"VM a migrer: {victim} (VRAM={vm_stats[victim]['vram_mib']}MiB)")
                send_gpu_request(victim, gpu_nodes_usage)
            else:
                log("Aucune VM identifiee — signal sans vmid specifique")
                send_gpu_request(0, gpu_nodes_usage)

        time.sleep(CHECK_INTERVAL_SECS)


if __name__ == "__main__":
    main()
