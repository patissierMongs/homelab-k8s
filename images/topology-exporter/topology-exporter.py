#!/usr/bin/env python3
"""
Infrastructure Topology Exporter
- Auto-discovers Docker containers, K8s pods/services, host services
- Detects connections between services (network, env vars, config)
- Exposes /topology endpoint for Grafana Node Graph (Infinity datasource)
- Runs on myubuntu, queries both myarch (Docker) and local (K8s + host)
"""
import json, subprocess, time, threading, http.server, os

MYARCH_SSH = os.getenv("MYARCH_SSH", "myarch")
REFRESH_INTERVAL = int(os.getenv("REFRESH_INTERVAL", "30"))
PORT = int(os.getenv("PORT", "9500"))

_topology = {"nodes": [], "edges": []}
_lock = threading.Lock()

# ── Discovery functions ──────────────────────────────────────────

def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except:
        return ""


def discover_docker_containers():
    """Discover Docker containers on myarch."""
    raw = run(f'ssh {MYARCH_SSH} \'docker ps --format "{{{{.Names}}}}|{{{{.Image}}}}|{{{{.Status}}}}|{{{{.Ports}}}}" 2>/dev/null\'')
    containers = []
    for line in raw.splitlines():
        parts = line.split("|")
        if len(parts) >= 3:
            containers.append({
                "name": parts[0],
                "image": parts[1],
                "status": parts[2],
                "ports": parts[3] if len(parts) > 3 else "",
                "server": "myarch",
            })
    return containers


def discover_k8s():
    """Discover K8s pods and services."""
    pods_raw = run('kubectl get pods --all-namespaces -o json 2>/dev/null', timeout=15)
    svcs_raw = run('kubectl get svc --all-namespaces -o json 2>/dev/null', timeout=15)
    pods, svcs = [], []
    try:
        for p in json.loads(pods_raw)["items"]:
            ns = p["metadata"]["namespace"]
            name = p["metadata"]["name"]
            labels = p["metadata"].get("labels", {})
            app = labels.get("app", name.rsplit("-", 2)[0] if "-" in name else name)
            status = p["status"]["phase"]
            containers = p["spec"].get("containers", [])
            env_vars = {}
            for c in containers:
                for e in c.get("env", []):
                    if "value" in e:
                        env_vars[e["name"]] = e["value"]
            pods.append({
                "name": name, "app": app, "namespace": ns,
                "status": status, "env": env_vars, "server": "myubuntu",
            })
    except:
        pass
    try:
        for s in json.loads(svcs_raw)["items"]:
            ns = s["metadata"]["namespace"]
            name = s["metadata"]["name"]
            svc_type = s["spec"].get("type", "ClusterIP")
            ports = s["spec"].get("ports", [])
            port_strs = [f'{p.get("port","")}/{p.get("protocol","TCP")}' for p in ports]
            node_ports = [str(p["nodePort"]) for p in ports if "nodePort" in p]
            svcs.append({
                "name": name, "namespace": ns, "type": svc_type,
                "ports": port_strs, "node_ports": node_ports,
            })
    except:
        pass
    return pods, svcs


def discover_host_services():
    """Discover systemd services on myubuntu."""
    services = []
    for svc in ["ollama", "ollama-proxy"]:
        status = run(f"systemctl is-active {svc} 2>/dev/null")
        if status:
            services.append({
                "name": svc, "status": status, "server": "myubuntu", "type": "systemd",
            })
    return services


def detect_connections(docker_containers, k8s_pods, k8s_svcs, host_services):
    """Auto-detect connections between services based on env vars, ports, known patterns."""
    edges = []
    seen = set()

    def add_edge(src, dst, label=""):
        key = (src, dst)
        if key not in seen:
            seen.add(key)
            edges.append({"id": f"{src}--{dst}", "source": src, "target": dst, "mainStat": label})

    # K8s pod env vars → service connections
    svc_names = {s["name"]: s for s in k8s_svcs}
    for pod in k8s_pods:
        pod_id = f"k8s:{pod['namespace']}/{pod['app']}"
        for env_key, env_val in pod.get("env", {}).items():
            if not env_val:
                continue
            # Check if env val references a K8s service
            for svc_name, svc in svc_names.items():
                if svc_name in env_val and svc["namespace"] != "kube-system":
                    target_id = f"k8s:{svc['namespace']}/{svc_name}"
                    if target_id != pod_id:  # skip self-references
                        label = env_key.lower().replace("_url", "").replace("_", " ")
                        add_edge(pod_id, target_id, label)
            # Check host service references
            if "192.168.50.108:11435" in env_val or "192.168.50.108:11434" in env_val:
                add_edge(pod_id, "host:ollama-proxy", "ollama api")
            if "100.122.37.120" in env_val:
                add_edge(pod_id, "host:ollama-proxy", env_key.lower())

    # Docker container connections (known patterns)
    docker_names = {c["name"] for c in docker_containers}
    for c in docker_containers:
        cid = f"docker:{c['name']}"
        name = c["name"]
        # Nextcloud → postgres, redis
        if name == "nextcloud":
            if "postgres" in docker_names: add_edge(cid, "docker:postgres", "db")
            if "redis" in docker_names: add_edge(cid, "docker:redis", "cache")
        if name == "nextcloud-cron":
            add_edge(cid, "docker:nextcloud", "cron")
        # Traefik → nextcloud, collabora
        if name == "traefik":
            if "nextcloud" in docker_names: add_edge(cid, "docker:nextcloud", "reverse proxy")
            if "collabora" in docker_names: add_edge(cid, "docker:collabora", "reverse proxy")
            if "rag-proxy" in docker_names: add_edge(cid, "docker:rag-proxy", "reverse proxy")
        # rag-proxy → myubuntu
        if name == "rag-proxy":
            add_edge(cid, "k8s:rag/rag-worker", "tcp proxy")
        # Nextcloud → ollama (context_chat)
        if "nc_app_context_chat" in name:
            add_edge(cid, "host:ollama-proxy", "embedding")
            add_edge("docker:nextcloud", cid, "app_api")

    # Host service connections
    add_edge("host:ollama-proxy", "host:ollama", "priority proxy")

    # K8s ollama service → host ollama (Endpoints redirect)
    if "ollama" in svc_names:
        add_edge("k8s:llm/ollama", "host:ollama-proxy", "k8s endpoints")

    # External access
    add_edge("external:internet", "docker:traefik", "HTTPS :443")

    return edges


def build_topology():
    """Full topology discovery."""
    docker_containers = discover_docker_containers()
    k8s_pods, k8s_svcs = discover_k8s()
    host_services = discover_host_services()
    edges = detect_connections(docker_containers, k8s_pods, k8s_svcs, host_services)

    nodes = []
    node_ids = set()

    # Category colors and icons
    cat_meta = {
        "docker": {"color": "#2496ED", "icon": "docker"},
        "k8s": {"color": "#326CE5", "icon": "kubernetes"},
        "host": {"color": "#4CAF50", "icon": "server"},
        "external": {"color": "#FF9800", "icon": "globe"},
    }

    # Docker containers as nodes
    for c in docker_containers:
        nid = f"docker:{c['name']}"
        node_ids.add(nid)
        nodes.append({
            "id": nid,
            "title": c["name"],
            "subtitle": c["image"].split(":")[0].split("/")[-1],
            "detail__server": "myarch",
            "detail__status": c["status"],
            "detail__ports": c["ports"],
            "detail__category": "docker",
            "arc__docker": 1,
        })

    # K8s pods as nodes (deduplicate by app)
    seen_apps = set()
    for pod in k8s_pods:
        if pod["namespace"] == "kube-system":
            continue
        app_key = f"{pod['namespace']}/{pod['app']}"
        if app_key in seen_apps:
            continue
        seen_apps.add(app_key)
        nid = f"k8s:{app_key}"
        node_ids.add(nid)
        # Find matching service
        svc_info = ""
        for s in k8s_svcs:
            if s["name"] == pod["app"] and s["namespace"] == pod["namespace"]:
                if s["node_ports"]:
                    svc_info = f"NodePort:{','.join(s['node_ports'])}"
                break
        nodes.append({
            "id": nid,
            "title": pod["app"],
            "subtitle": f"{pod['namespace']} ns" + (f" | {svc_info}" if svc_info else ""),
            "detail__server": "myubuntu",
            "detail__status": pod["status"],
            "detail__namespace": pod["namespace"],
            "detail__category": "k8s",
            "arc__k8s": 1,
        })

    # Host services as nodes
    for svc in host_services:
        nid = f"host:{svc['name']}"
        node_ids.add(nid)
        nodes.append({
            "id": nid,
            "title": svc["name"],
            "subtitle": f"systemd ({svc['status']})",
            "detail__server": "myubuntu",
            "detail__status": svc["status"],
            "detail__category": "host",
            "arc__host": 1,
        })

    # External node
    nodes.append({
        "id": "external:internet",
        "title": "Internet",
        "subtitle": "cloud.reimu-chan.mooo.com",
        "detail__category": "external",
        "arc__external": 1,
    })
    node_ids.add("external:internet")

    # K8s service nodes that are referenced but not yet added
    for svc in k8s_svcs:
        if svc["namespace"] == "kube-system":
            continue
        nid = f"k8s:{svc['namespace']}/{svc['name']}"
        if nid not in node_ids:
            node_ids.add(nid)
            nodes.append({
                "id": nid,
                "title": svc["name"],
                "subtitle": f"{svc['namespace']} svc",
                "detail__server": "myubuntu",
                "detail__status": "active",
                "detail__namespace": svc["namespace"],
                "detail__category": "k8s",
                "arc__k8s": 1,
            })

    # Filter edges to only include existing nodes
    valid_edges = [e for e in edges if e["source"] in node_ids and e["target"] in node_ids]

    return {"nodes": nodes, "edges": valid_edges}


# ── Background refresh ───────────────────────────────────────────

def refresh_loop():
    global _topology
    while True:
        try:
            topo = build_topology()
            with _lock:
                _topology = topo
        except Exception as e:
            print(f"Refresh error: {e}")
        time.sleep(REFRESH_INTERVAL)


# ── HTTP Server ──────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/topology":
            with _lock:
                data = _topology
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
        elif self.path == "/topology/nodes":
            with _lock:
                data = _topology.get("nodes", [])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
        elif self.path == "/topology/edges":
            with _lock:
                data = _topology.get("edges", [])
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())
        elif self.path == "/topology/summary":
            with _lock:
                nodes = _topology.get("nodes", [])
                edges = _topology.get("edges", [])
            cats = {}
            for n in nodes:
                c = n.get("detail__category", "unknown")
                cats[c] = cats.get(c, 0) + 1
            summary = [{"total_nodes": len(nodes), "total_edges": len(edges),
                        "docker": cats.get("docker", 0), "k8s": cats.get("k8s", 0),
                        "host": cats.get("host", 0), "external": cats.get("external", 0)}]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(summary, ensure_ascii=False).encode())
        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    # Initial discovery
    print(f"Topology exporter starting on port {PORT}")
    _topology = build_topology()
    print(f"Discovered {len(_topology['nodes'])} nodes, {len(_topology['edges'])} edges")

    # Background refresh thread
    t = threading.Thread(target=refresh_loop, daemon=True)
    t.start()

    server = http.server.HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Serving on port {PORT}")
    server.serve_forever()
