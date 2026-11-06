#!/usr/bin/env python3
"""Small local server that answers the Resend endpoints used by the module.

It is meant for the end-to-end tests: no Resend account or quota is required.
"""
import base64
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = {
    'emails': [],       # outbound payloads + id
    'domains': [
        {
            'id': 'dom_1', 'name': 'example.com', 'status': 'verified',
            'region': 'us-east-1', 'created_at': '2026-01-01T00:00:00.000Z',
            'records': [
                {'id': 'rec_1', 'record': 'SPF', 'name': 'send', 'type': 'TXT',
                 'value': 'v=spf1 include:amazonses.com ~all', 'ttl': 'Auto', 'status': 'verified'},
                {'id': 'rec_2', 'record': 'DKIM', 'name': 'resend._domainkey', 'type': 'TXT',
                 'value': 'p=MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQ', 'ttl': 'Auto', 'status': 'verified'},
            ],
        },
    ],
    'webhooks': [],
    'received': {},
    'raw_emails': {},
    'fail_next': 0,
}
LOCK = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def _send(self, code, payload=None, raw=None, ctype='application/json'):
        body = raw if raw is not None else json.dumps(payload or {}).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(length) if length else b''

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/health':
            return self._send(200, {'ok': True})
        if path == '/_emails':
            return self._send(200, {'data': STATE['emails']})
        if path == '/_reset':
            with LOCK:
                STATE['emails'].clear()
                STATE['received'].clear()
                STATE['raw_emails'].clear()
            return self._send(200, {'ok': True})
        if path == '/domains':
            return self._send(200, {'data': STATE['domains']})
        if path.startswith('/domains/'):
            for domain in STATE['domains']:
                if domain['id'] == path.split('/')[2]:
                    return self._send(200, domain)
            return self._send(404, {'message': 'not found'})
        if path == '/webhooks':
            return self._send(200, {'data': STATE['webhooks']})
        if path.startswith('/emails/receiving/'):
            email_id = path.split('/')[3]
            if email_id in STATE['received']:
                return self._send(200, STATE['received'][email_id])
            return self._send(404, {'message': 'not found'})
        if path.startswith('/raw/'):
            email_id = path.split('/')[2]
            raw = STATE['raw_emails'].get(email_id)
            if raw is None:
                return self._send(404, {'message': 'no raw'})
            return self._send(200, raw=raw, ctype='message/rfc822')
        return self._send(404, {'message': 'unknown path ' + path})

    def do_POST(self):
        path = self.path.split('?')[0]
        body = self._body()
        if path == '/_receive':  # register a received email for the inbound flow
            data = json.loads(body or b'{}')
            STATE['received'][data['id']] = data['meta']
            STATE['raw_emails'][data['id']] = base64.b64decode(data['raw'])
            return self._send(200, {'ok': True})
        if path == '/emails':
            if STATE['fail_next'] > 0:
                STATE['fail_next'] -= 1
                return self._send(429, {'statusCode': 429, 'name': 'rate_limit_exceeded',
                                        'message': 'Too many requests'})
            payload = json.loads(body or b'{}')
            email_id = str(uuid.uuid4())
            STATE['emails'].append({
                'id': email_id,
                'payload': payload,
                'idempotency_key': self.headers.get('Idempotency-Key'),
                'user_agent': self.headers.get('User-Agent'),
            })
            return self._send(200, {'id': email_id})
        if path == '/emails/batch':
            payloads = json.loads(body or b'[]')
            return self._send(200, {'data': [{'id': str(uuid.uuid4())} for _ in payloads]})
        if path == '/domains':
            data = json.loads(body or b'{}')
            domain = {
                'id': 'dom_' + str(uuid.uuid4())[:8],
                'name': data.get('name'), 'status': 'pending',
                'region': data.get('region') or 'us-east-1',
                'created_at': '2026-01-01T00:00:00.000Z', 'records': [],
            }
            STATE['domains'].append(domain)
            return self._send(200, domain)
        if path.startswith('/domains/') and path.endswith('/verify'):
            for domain in STATE['domains']:
                if domain['id'] == path.split('/')[2]:
                    domain['status'] = 'verified'
                    return self._send(200, domain)
            return self._send(404, {'message': 'not found'})
        if path == '/webhooks':
            data = json.loads(body or b'{}')
            hook = {'id': 'wh_' + str(uuid.uuid4())[:8],
                    'endpoint': data.get('endpoint'),
                    'events': data.get('events'),
                    'signing_secret': 'whsec_' + base64.b64encode(b'test-secret-key-32-bytes-long!!').decode()}
            STATE['webhooks'].append(hook)
            return self._send(200, hook)
        if path.startswith('/webhooks/'):
            return self._send(200, {'id': path.split('/')[2]})
        return self._send(404, {'message': 'unknown path ' + path})

    def do_PATCH(self):
        return self._send(200, {'ok': True})


if __name__ == '__main__':
    server = ThreadingHTTPServer(('0.0.0.0', 8099), Handler)
    print('mock resend api on :8099', flush=True)
    server.serve_forever()