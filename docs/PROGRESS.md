# 진행 기록

**한국어** | [English](PROGRESS.en.md)

## 최종 목표

GPU(Graphics Processing Unit) 1장이 달린 단일 노드 K3s 클러스터에서 다음을 Git 푸시만으로 배포·운영하는 것이 목표입니다.

- 로컬 LLM(Large Language Model) 서빙과 웹 UI(User Interface)
- Nextcloud에 쌓인 문서·영상·음성을 벡터로 바꿔 검색하는 RAG(Retrieval-Augmented Generation) 파이프라인
- IoT(Internet of Things) 센서 데이터 수집과 알림, 그리고 알림을 LLM으로 분석하는 AIOps(AI for IT Operations) 흐름
- 사용자 LLM 요청과 배치 작업이 GPU를 나눠 쓸 때 사용자 요청을 먼저 처리하는 구조

## 현재 구현 상태

아래 상태는 커밋 메시지가 아니라 저장소의 코드와 매니페스트를 직접 확인해 정했습니다. 클러스터는 이 환경에서 띄울 수 없어서 매니페스트는 정적 검사(yamllint, kubeconform, `kustomize build`)까지만 확인했고, rag-worker는 로컬에서 실제로 실행했습니다.

| 기능 | 상태 | 근거가 되는 코드 위치 | 비고 |
|---|---|---|---|
| Flux GitOps 동기화 | 구현됨 | `clusters/myubuntu/flux-system/gotk-sync.yaml`, `clusters/myubuntu/infrastructure.yaml` | `main`을 1분마다 가져오고 `infrastructure/`를 5분마다 적용, `prune: false` |
| Flux 컴포넌트 | 구현됨 | `clusters/myubuntu/flux-system/gotk-components.yaml` | Flux v2.8.2 |
| SOPS(Secrets OPerationS) 시크릿 복호화 | 구현됨 | `.sops.yaml`, `infrastructure/**/*.enc.yaml`, `infrastructure.yaml`의 `decryption` | age 키 사용, `sops-age` 시크릿은 저장소 밖에서 만들어야 함 |
| GPU 시분할 | 부분 구현 | `infrastructure/kube-system/gpu-time-slicing.yaml`, `device-plugin-patch.yaml` | ConfigMap만 Flux가 관리하고, device plugin 패치는 Kustomization에서 빠져 있어 수동 적용 |
| Ollama 클러스터 연결 | 부분 구현 | `infrastructure/llm/ollama.yaml` | Service + Endpoints만 있음. Ollama와 우선순위 프록시(11435)는 호스트에서 돌며 이 저장소에 코드가 없음 |
| Open WebUI | 구현됨 | `infrastructure/llm/open-webui.yaml` | `WEBUI_AUTH=false`, hostPath 저장소 |
| RAG: PDF(Portable Document Format) 처리·임베딩·저장 | 구현됨 | `images/rag-worker/main.py` `process_file`, `get_embeddings` | 로컬 실행으로 PDF 5개 처리와 Qdrant 저장 확인 |
| RAG: 영상/음성 받아쓰기 | 부분 구현 | `main.py` `extract_audio`, `transcribe`, `infrastructure/rag/faster-whisper.yaml` | 코드는 있으나 Whisper 서버를 띄우지 못해 실행 확인 못 함 |
| RAG: NFS(Network File System) 자동 스캔 | 구현됨 | `main.py` `_auto_scan`, `infrastructure/rag/nfs-pv.yaml` | 로컬에서 시작 30초 뒤 스캔이 돌아 순서대로 처리되는 것 확인 |
| RAG: 사용자별 검색 | 구현됨 | `main.py` `search` | `owner` 필드로 필터. 로컬에서 응답 확인 |
| RAG: 처리 현황 대시보드 | 부분 구현 | `main.py` `pipeline_status`, `DASHBOARD_HTML` | 실패한 작업이 "대기"로 표시되고 실패 수가 항상 0. `active_jobs`에서 `FAILED`를 빼기 때문 (로컬 실행에서 WAV 2개로 확인) |
| RAG: Nextcloud WebDAV(Web Distributed Authoring and Versioning) 배치 | 부분 구현 | `main.py` `list_nextcloud_files`, `download_nc_file`, `batch_process` | 코드는 있으나 실행 확인 못 함. TLS(Transport Layer Security) 검증 끔(`verify=False`) |
| RAG: 사용자 요청 우선(GPU 양보) | 부분 구현 | `main.py` `_yield_to_user` | 워커 쪽 대기 코드만 있음. `/proxy/stats`를 내주는 프록시는 저장소에 없음 |
| Prometheus 수집 | 구현됨 | `infrastructure/monitoring/prometheus.yaml` | prometheus, mqtt-exporter, node-exporter 수집 |
| 알림 규칙 | 미구현 | `prometheus.yaml` | `rule_files`가 없어 Alertmanager로 가는 알림이 생기지 않음 |
| Alertmanager → AIOps 브리지 → Ollama | 부분 구현 | `infrastructure/monitoring/alertmanager.yaml`, `images/aiops-bridge/bridge.py` | 경로는 연결되어 있으나 위 알림 규칙이 없어 동작하지 않음. 실행 확인 못 함 |
| Grafana 주석 기록 | 부분 구현 | `bridge.py` `post_grafana_annotation` | 인증 헤더 없이 `/api/annotations`에 POST하고 예외를 무시함. 익명 접근만 켜져 있어 거부될 가능성이 큼(확인 못 함) |
| Grafana + Infinity 데이터소스 | 구현됨 | `infrastructure/monitoring/grafana.yaml` | Grafana 11.6.0, 관리자 비밀번호는 SOPS 시크릿 |
| LLMOps 대시보드 | 부분 구현 | `infrastructure/monitoring/dashboards/llmops.json` | `/llm/*`, `/batch/*` 엔드포인트(호스트 `:9500`)를 읽는데, 이 엔드포인트 코드는 저장소에 없음 |
| 토폴로지 대시보드 + topology-exporter | 부분 구현 | `dashboards/topology.json`, `images/topology-exporter/topology-exporter.py` | exporter 코드는 있으나 클러스터 매니페스트가 없음(호스트에서 실행 전제) |
| MQTT(Message Queuing Telemetry Transport) 브로커와 exporter | 구현됨 | `infrastructure/monitoring/mosquitto.yaml`, `mqtt-exporter.yaml` | 익명 접속 허용 |
| IoT 시뮬레이터 | 구현됨 | `images/iot-simulator/simulator.py`, `infrastructure/simulation/iot-simulator.yaml` | 장치 5대, 이상값 확률 5% |
| 이미지 빌드·적재 스크립트 | 구현됨 | `images/build-and-load.sh` | `docker save` → `k3s ctr images import` |
| CI(Continuous Integration) 매니페스트 검사 | 구현됨 | `.github/workflows/validate.yml` | 이번 작업에서 같은 검사를 로컬로 돌려 모두 통과 |
| 테스트 코드 | 미구현 | 없음 | 자체 이미지에 단위 테스트가 없음 |

## 작업 이력

`git log`의 커밋 시각은 모두 +09:00으로 저장되어 있어 그대로 KST(한국 표준시, Asia/Seoul)입니다.

| 날짜 (KST) | 커밋 | 내용 |
|---|---|---|
| 2026-03-14 02:33 | a3c5363 | K3s GitOps 매니페스트 첫 커밋(Kustomize + SOPS): llm, rag, monitoring, simulation, kube-system 네임스페이스와 자체 이미지 소스 |
| 2026-03-14 02:33 | cf2cde9 | Flux v2.8.2 컴포넌트 매니페스트 추가 |
| 2026-03-14 02:33 | 841a4f2 | Flux 동기화 매니페스트(GitRepository, Kustomization) 추가 |
| 2026-03-14 02:38 | fd30354 | device plugin 패치를 Kustomization에서 제외(외부 설치 도구가 관리) |
| 2026-03-14 02:39 | 170bda1 | CI에서 패치 파일을 kubeconform 대상에서 빼고 f-string 문법 오류 수정 |
| 2026-03-14 03:20 | 56bac04 | Qdrant 업서트를 256개씩 나눠 32MB 요청 크기 제한 회피 |
| 2026-03-14 03:37 | e15a2be | Grafana에 Infinity 데이터소스 플러그인과 프로비저닝 추가 |
| 2026-09-27 | (이번 작업) | 개인 정보 제거(IP, 도메인, 계정), README 한/영 작성, 구성도·스크린샷 추가, 진행 기록 추가 |
