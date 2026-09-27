# homelab-k8s

A GitOps repository that runs a single-node K3s homelab cluster with Flux. It manages LLM (Large Language Model) serving, a RAG (Retrieval-Augmented Generation) pipeline and IoT (Internet of Things) monitoring as Kustomize manifests.

[한국어](README.md) | **English**

## Architecture

![Cluster architecture](docs/images/architecture.png)

The diagram is drawn from the manifests in `clusters/` and `infrastructure/`. Orange boxes are components that must exist outside the cluster (this repository has no manifests for them).

## Screenshots

The part of this repository that can run on its own is `images/rag-worker`. The captures below come from running rag-worker locally without a cluster, fed with 5 synthetic PDF (Portable Document Format) files and 2 WAV (Waveform Audio File Format) files while the auto-scan ran (Qdrant 1.12.4 binary, Ollama with the `all-minilm` embedding model, no Whisper server).

![RAG pipeline monitor in action](docs/images/rag-dashboard.gif)

| Dashboard after processing | Search API (Application Programming Interface) response |
|---|---|
| ![RAG dashboard](docs/images/rag-dashboard.png) | ![Search result](docs/images/rag-search.png) |

The WAV files failed because no Whisper server was running, yet the dashboard shows them as "pending" (대기). This behavior is recorded in [docs/PROGRESS.en.md](docs/PROGRESS.en.md).

## Features

- **Flux GitOps**: pulls the `main` branch every minute and applies `clusters/myubuntu` and then `infrastructure/`. The Flux v2.8.2 components are committed to the repository.
- **SOPS-encrypted secrets**: `*.enc.yaml` files are encrypted with SOPS (Secrets OPerationS) and an age key; Flux decrypts them at apply time.
- **GPU (Graphics Processing Unit) time-slicing**: the NVIDIA device plugin config splits one GPU into 4 slots.
- **LLM (`llm` namespace)**: Ollama runs on the host as a systemd service and is exposed to the cluster through a Service + Endpoints; Open WebUI is deployed next to it.
- **RAG (`rag` namespace)**
  - `rag-worker` scans Nextcloud data (read-only NFS mount) every 10 minutes. It extracts text from PDFs, transcribes video/audio with faster-whisper, embeds the text with Ollama and stores the vectors in Qdrant.
  - It serves a per-user search API (`/search`), a progress dashboard (`/dashboard`) and manual upload (`/process`).
  - Before GPU work it waits while a user LLM request is active (checks `/proxy/stats` on the priority proxy).
- **Monitoring (`monitoring` namespace)**: Prometheus, Alertmanager, Grafana, node-exporter, Mosquitto (MQTT broker, Message Queuing Telemetry Transport) and mqtt-exporter.
- **AIOps (AI for IT Operations) bridge**: written to receive Alertmanager webhooks, ask Ollama (`mistral:7b`) for a root-cause analysis and post it as a Grafana annotation.
- **IoT simulator (`simulation` namespace)**: 5 virtual devices publish temperature, humidity, pressure and power over MQTT every 5 seconds, with a 5% chance of an anomalous value.
- **Grafana dashboards**: LLMOps monitoring (`llmops.json`) and infrastructure topology (`topology.json`). Both read a JSON (JavaScript Object Notation) API on the host at `:9500` through the Infinity datasource.
- **CI (Continuous Integration)**: GitHub Actions runs yamllint, kubeconform, a dashboard JSON structure check and a SOPS encryption check.

## Usage

### 1. Prerequisites

- One K3s node with an NVIDIA GPU and the NVIDIA device plugin installed
- `flux`, `kubectl`, `sops`, `age` and `docker` CLIs (Command Line Interfaces)
- Ollama and its priority proxy (port 11435) running on the host, and an NFS (Network File System) server exporting the Nextcloud data

### 2. Values to change for your environment

The repository contains documentation addresses (`192.0.2.x` from RFC (Request for Comments) 5737) and `example.com` instead of real ones. Change these before deploying.

| File | Field | Current value | Meaning |
|---|---|---|---|
| `infrastructure/llm/ollama.yaml` | `Endpoints.subsets[].addresses[].ip` | `192.0.2.10` | IP (Internet Protocol) of the host running the Ollama priority proxy |
| `infrastructure/rag/rag-worker.yaml` | `PRIORITY_PROXY_URL` | `http://192.0.2.10:11435` | Proxy address on the same host |
| `infrastructure/rag/rag-worker.yaml` | `NC_URL` | `https://cloud.example.com` | Nextcloud address (for WebDAV (Web Distributed Authoring and Versioning) batches) |
| `infrastructure/rag/nc-credentials.enc.yaml` | `NC_USER`, `NC_PASS` | encrypted | Nextcloud account. `NC_USER` is an optional key |
| `infrastructure/rag/nfs-pv.yaml` | `nfs.server`, `nfs.path` | `192.0.2.20`, Docker volume path | NFS export of the Nextcloud data |
| `infrastructure/monitoring/dashboards/*.json` | Infinity URL (Uniform Resource Locator) | `http://192.0.2.10:9500/...` | Host JSON API address |
| `.sops.yaml` | `age` | public key | Replace with your own age public key and re-encrypt the secrets |
| `clusters/myubuntu/flux-system/gotk-sync.yaml` | `url` | this repository | Your fork, if you forked it |

The Nextcloud account goes into the encrypted secret:

```bash
sops infrastructure/rag/nc-credentials.enc.yaml
# under stringData, set NC_USER: <nextcloud-user> and NC_PASS: <password>, then save
```

If you run topology-exporter on the host, set its addresses with the environment variables `HOST_IP`, `HOST_ALT_IP`, `PUBLIC_DOMAIN`, `MYARCH_SSH` and `PORT` (default 9500).

### 3. Build the images

The `rag-worker`, `aiops-bridge`, `iot-simulator` and `topology-exporter` images are imported straight into K3s containerd without a registry (`imagePullPolicy: Never`). Run this on the K3s node:

```bash
./images/build-and-load.sh              # build all
./images/build-and-load.sh rag-worker   # build one
```

### 4. Bootstrap Flux

```bash
# register the age private key used for SOPS decryption
kubectl create namespace flux-system
kubectl -n flux-system create secret generic sops-age --from-file=age.agekey=<age key file>

# install Flux and connect the repository
flux bootstrap github --owner=<github-owner> --repository=homelab-k8s --branch=main --path=clusters/myubuntu
```

From then on, pushing to `main` is enough; Flux applies the change. `device-plugin-patch.yaml` is excluded from the Kustomization, so apply it yourself with `kubectl patch`.

### 5. Ports

| Service | NodePort |
|---|---|
| Open WebUI | 30080 |
| Grafana | 30300 |
| Prometheus | 30090 |
| Alertmanager | 30093 |
| Mosquitto (MQTT) | 31883 |
| Ollama | 31434 |
| rag-worker | auto-assigned (check with `kubectl -n rag get svc rag-worker`) |

### 6. rag-worker API

| Method | Path | Description |
|---|---|---|
| GET | `/dashboard` | Progress web page (refreshes every 30 s) |
| GET | `/status` | Summary of files on disk / processed / in progress |
| POST | `/scan?user=&force=` | Scan NFS and process unprocessed files one by one |
| POST | `/batch?user=&force=` | Find files via Nextcloud WebDAV search, download and process them |
| POST | `/process` | Process an uploaded file (`file`, `source_path`, `nc_file_id`) |
| GET | `/search?q=&user=&limit=` | Per-user vector search (`user` is required) |
| GET | `/jobs`, `/processed`, `/health` | Job status, processed list, health check |

To try it locally without a cluster, start Qdrant and Ollama first and then run (this is how the screenshots were taken):

```bash
cd images/rag-worker
pip install -r requirements.txt
NC_DATA_DIR=/path/to/nc-data QDRANT_URL=http://127.0.0.1:6333 \
OLLAMA_URL=http://127.0.0.1:11434 EMBED_MODEL=all-minilm \
python -m uvicorn main:app --host 127.0.0.1 --port 18080
# open http://127.0.0.1:18080/dashboard
```

`NC_DATA_DIR` must follow the `<user>/files/...` layout.

### 7. Validate the manifests

The CI checks can be run locally:

```bash
yamllint -d '{extends: relaxed, rules: {line-length: {max: 200}}}' infrastructure/ clusters/
find infrastructure/ -name '*.yaml' ! -name 'kustomization.yaml' ! -name '*.enc.yaml' ! -name '*-patch.yaml' | \
  xargs -I{} kubeconform -strict -ignore-missing-schemas -summary {}
```

## Tech stack

| Area | Technology |
|---|---|
| Cluster | K3s (single node), NVIDIA device plugin (time-slicing x4) |
| GitOps | Flux v2.8.2, Kustomize, SOPS 3.9.4 + age |
| LLM | Ollama (host systemd), Open WebUI `main`, `mistral:7b`, `qwen3-embedding:8b` |
| RAG | faster-whisper-server `latest-cuda` (`Systran/faster-whisper-large-v3-turbo`), Qdrant `latest` |
| Monitoring | Prometheus v3.3.0, Alertmanager v0.28.1, Grafana 11.6.0 + Infinity plugin, node-exporter v1.9.0, Eclipse Mosquitto 2, mqtt-exporter `latest` |
| Custom images | Python 3.12-slim |
| rag-worker libraries | FastAPI 0.115.0, Uvicorn 0.30.0, HTTPX 0.27.0, qdrant-client 1.12.0, python-multipart 0.0.9, watchfiles 0.24.0, PyMuPDF 1.25.0, ffmpeg |
| Other images | requests 2.32.3 (aiops-bridge), paho-mqtt 2.1.0 (iot-simulator), topology-exporter uses the standard library only |
| CI | GitHub Actions, yamllint, kubeconform v0.6.7 |

## Repository layout

```
clusters/myubuntu/        Flux entry point (flux-system, infrastructure Kustomization)
infrastructure/
  kube-system/            GPU time-slicing config
  llm/                    Ollama Service/Endpoints, Open WebUI
  rag/                    faster-whisper, Qdrant, rag-worker, NFS PV (PersistentVolume), Nextcloud secret
  monitoring/             Prometheus, Alertmanager, Grafana (+dashboards), Mosquitto, exporters, aiops-bridge
  simulation/             IoT simulator
images/                   Custom image sources and build-and-load.sh
.github/workflows/        Manifest validation CI
```

## Docs

- [Progress record (docs/PROGRESS.en.md)](docs/PROGRESS.en.md): final goal, per-feature implementation status, work history
