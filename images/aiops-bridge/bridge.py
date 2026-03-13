#!/usr/bin/env python3
"""AIOps Bridge: Alertmanager webhook -> Ollama LLM analysis."""

import json, logging, os, time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("aiops-bridge")

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama.llm.svc.cluster.local:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral:7b")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "5000"))
GRAFANA_URL = os.getenv("GRAFANA_URL", "http://grafana.monitoring.svc.cluster.local:3000")

SYSTEM_PROMPT = (
    "You are an AIOps assistant analyzing infrastructure alerts from an IoT monitoring system. "
    "Given an alert from Prometheus/Alertmanager, provide: "
    "1) Root cause analysis (2-3 sentences) "
    "2) Severity assessment (low/medium/high/critical) "
    "3) Recommended actions (bullet points). "
    "Be concise and actionable. Focus on IoT sensor data patterns."
)


def build_alert_context(alert):
    labels = alert.get('labels', {})
    annotations = alert.get('annotations', {})
    lines = [
        '## Alert Details',
        '- Status: ' + alert.get('status', 'unknown'),
        '- Alert Name: ' + labels.get('alertname', 'unknown'),
        '- Severity: ' + labels.get('severity', 'unknown'),
        '- Instance: ' + labels.get('instance', 'N/A'),
        '- Job: ' + labels.get('job', 'N/A'),
        '- Started: ' + alert.get('startsAt', ''),
        '- Summary: ' + annotations.get('summary', 'N/A'),
        '- Description: ' + annotations.get('description', 'N/A'),
    ]
    return '\n'.join(lines)


def query_ollama(prompt):
    payload = {
        'model': OLLAMA_MODEL,
        'prompt': prompt,
        'system': SYSTEM_PROMPT,
        'stream': False,
        'options': {'temperature': 0.3, 'num_predict': 512},
    }
    try:
        t0 = time.monotonic()
        resp = requests.post(OLLAMA_URL + '/api/generate', json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()
        elapsed = time.monotonic() - t0
        log.info('LLM: %d tokens in %.1fs', data.get('eval_count', 0), elapsed)
        return data.get('response', 'No response')
    except Exception as e:
        log.error('Ollama error: %s', e)
        return '[LLM Error] ' + str(e)


def post_grafana_annotation(alert, analysis):
    try:
        payload = {
            'text': 'AIOps Analysis\n\n' + analysis,
            'tags': ['aiops', alert.get('labels', {}).get('alertname', 'alert')],
        }
        requests.post(GRAFANA_URL + '/api/annotations', json=payload, timeout=10)
    except Exception:
        pass


class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != '/alert':
            self.send_response(404)
            self.end_headers()
            return
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return
        alerts = payload.get('alerts', [])
        log.info('Received %d alert(s)', len(alerts))
        results = []
        for a in alerts:
            ctx = build_alert_context(a)
            name = a.get('labels', {}).get('alertname', '?')
            log.info('Analyzing: %s', name)
            analysis = query_ollama('Analyze this alert:\n\n' + ctx)
            log.info('Result:\n%s', analysis)
            post_grafana_annotation(a, analysis)
            results.append({
                'alertname': name,
                'analysis': analysis,
                'ts': datetime.now(timezone.utc).isoformat(),
            })
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'results': results}).encode())

    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


if __name__ == '__main__':
    log.info('AIOps Bridge on :%d, Ollama=%s model=%s', LISTEN_PORT, OLLAMA_URL, OLLAMA_MODEL)
    HTTPServer(('0.0.0.0', LISTEN_PORT), WebhookHandler).serve_forever()
