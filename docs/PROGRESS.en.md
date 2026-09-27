# Progress record

[한국어](PROGRESS.md) | **English**

## Final goal

Deploy and operate the following on a single-node K3s cluster with one GPU (Graphics Processing Unit), driven only by Git pushes:

- Local LLM (Large Language Model) serving with a web UI (User Interface)
- A RAG (Retrieval-Augmented Generation) pipeline that turns documents, video and audio stored in Nextcloud into searchable vectors
- IoT (Internet of Things) sensor ingestion and alerting, with alerts analyzed by an LLM (AIOps, AI for IT Operations)
- GPU sharing in which user LLM requests take priority over batch work

## Current implementation status

Status was decided by reading the code and manifests in this repository, not from commit messages. The cluster cannot run in this environment, so manifests were only checked statically (yamllint, kubeconform, `kustomize build`); rag-worker was actually run locally.

| Feature | Status | Code location | Notes |
|---|---|---|---|
| Flux GitOps sync | Implemented | `clusters/myubuntu/flux-system/gotk-sync.yaml`, `clusters/myubuntu/infrastructure.yaml` | Pulls `main` every minute, applies `infrastructure/` every 5 minutes, `prune: false` |
| Flux components | Implemented | `clusters/myubuntu/flux-system/gotk-components.yaml` | Flux v2.8.2 |
| SOPS (Secrets OPerationS) decryption | Implemented | `.sops.yaml`, `infrastructure/**/*.enc.yaml`, `decryption` in `infrastructure.yaml` | age key; the `sops-age` secret must be created outside the repository |
| GPU time-slicing | Partial | `infrastructure/kube-system/gpu-time-slicing.yaml`, `device-plugin-patch.yaml` | Flux manages only the ConfigMap; the device plugin patch is excluded from the Kustomization and applied by hand |
| Ollama in the cluster | Partial | `infrastructure/llm/ollama.yaml` | Only a Service + Endpoints. Ollama and the priority proxy (11435) run on the host; their code is not in this repository |
| Open WebUI | Implemented | `infrastructure/llm/open-webui.yaml` | `WEBUI_AUTH=false`, hostPath storage |
| RAG: PDF (Portable Document Format) extraction, embedding, storage | Implemented | `images/rag-worker/main.py` `process_file`, `get_embeddings` | Verified locally: 5 PDFs processed and stored in Qdrant |
| RAG: video/audio transcription | Partial | `main.py` `extract_audio`, `transcribe`, `infrastructure/rag/faster-whisper.yaml` | Code exists but was not verified (no Whisper server available here) |
| RAG: NFS (Network File System) auto-scan | Implemented | `main.py` `_auto_scan`, `infrastructure/rag/nfs-pv.yaml` | Verified locally: scan starts 30 s after boot and processes files one by one |
| RAG: per-user search | Implemented | `main.py` `search` | Filters on the `owner` field. Response verified locally |
| RAG: progress dashboard | Partial | `main.py` `pipeline_status`, `DASHBOARD_HTML` | Failed jobs are shown as "pending" and the failed count is always 0, because `active_jobs` excludes `FAILED` (observed locally with 2 WAV files) |
| RAG: Nextcloud WebDAV (Web Distributed Authoring and Versioning) batch | Partial | `main.py` `list_nextcloud_files`, `download_nc_file`, `batch_process` | Code exists, not verified. TLS (Transport Layer Security) verification is disabled (`verify=False`) |
| RAG: user-first GPU yielding | Partial | `main.py` `_yield_to_user` | Only the worker-side wait exists. The proxy serving `/proxy/stats` is not in this repository |
| Prometheus scraping | Implemented | `infrastructure/monitoring/prometheus.yaml` | Scrapes prometheus, mqtt-exporter, node-exporter |
| Alert rules | Not started | `prometheus.yaml` | No `rule_files`, so no alerts ever reach Alertmanager |
| Alertmanager → AIOps bridge → Ollama | Partial | `infrastructure/monitoring/alertmanager.yaml`, `images/aiops-bridge/bridge.py` | Wired up, but inactive without alert rules. Not run here |
| Grafana annotations | Partial | `bridge.py` `post_grafana_annotation` | POSTs to `/api/annotations` without an auth header and swallows errors. Only anonymous access is enabled, so it is likely rejected (not verified) |
| Grafana + Infinity datasource | Implemented | `infrastructure/monitoring/grafana.yaml` | Grafana 11.6.0, admin password from a SOPS secret |
| LLMOps dashboard | Partial | `infrastructure/monitoring/dashboards/llmops.json` | Reads `/llm/*` and `/batch/*` on host `:9500`; the code serving those endpoints is not in this repository |
| Topology dashboard + topology-exporter | Partial | `dashboards/topology.json`, `images/topology-exporter/topology-exporter.py` | Exporter code exists but has no cluster manifest (meant to run on the host) |
| MQTT (Message Queuing Telemetry Transport) broker and exporter | Implemented | `infrastructure/monitoring/mosquitto.yaml`, `mqtt-exporter.yaml` | Anonymous access allowed |
| IoT simulator | Implemented | `images/iot-simulator/simulator.py`, `infrastructure/simulation/iot-simulator.yaml` | 5 devices, 5% anomaly chance |
| Image build/load script | Implemented | `images/build-and-load.sh` | `docker save` → `k3s ctr images import` |
| CI (Continuous Integration) manifest checks | Implemented | `.github/workflows/validate.yml` | The same checks were run locally during this work and all passed |
| Tests | Not started | none | The custom images have no unit tests |

## Work history

All commit timestamps in `git log` are stored with a +09:00 offset, so they are already KST (Korea Standard Time, Asia/Seoul).

| Date (KST) | Commit | Change |
|---|---|---|
| 2026-03-14 02:33 | a3c5363 | Initial K3s GitOps manifests (Kustomize + SOPS): llm, rag, monitoring, simulation, kube-system namespaces and custom image sources |
| 2026-03-14 02:33 | cf2cde9 | Add Flux v2.8.2 component manifests |
| 2026-03-14 02:33 | 841a4f2 | Add Flux sync manifests (GitRepository, Kustomization) |
| 2026-03-14 02:38 | fd30354 | Exclude the device plugin patch from the Kustomization (managed by an external installer) |
| 2026-03-14 02:39 | 170bda1 | Exclude patch files from kubeconform in CI, fix an f-string syntax error |
| 2026-03-14 03:20 | 56bac04 | Upsert to Qdrant in batches of 256 to stay under the 32 MB request limit |
| 2026-03-14 03:37 | e15a2be | Add the Infinity datasource plugin and provisioning to Grafana |
| 2026-09-27 | (this work) | Remove personal info (IPs, domain, account), write README in Korean/English, add architecture diagram and screenshots, add progress record |
