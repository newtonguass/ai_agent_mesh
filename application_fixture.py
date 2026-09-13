"""Small HTTP/1.1 JSON application and client used only by the live lab."""
import argparse
import concurrent.futures
import hashlib
import http.client
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class API(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def send(self, code, body, content_type='application/json'):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ('/', '/health'):
            self.send(200, 'mesh-access-backend\n', 'text/plain')
        else:
            self.send(404, {'error': 'not found'})

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        try:
            order = json.loads(data)
            total = sum(x['quantity'] * x['unitPriceCents'] for x in order['items'])
            assert self.path == '/api/quote' and isinstance(total, int)
        except (ValueError, KeyError, TypeError, AssertionError):
            return self.send(400, {'error': 'invalid quote'})
        self.send(200, {'requestId': order['requestId'], 'totalCents': total,
                        'payloadSHA256': hashlib.sha256(data).hexdigest(),
                        'pod': os.environ['HOSTNAME'], 'version': os.environ.get('APP_VERSION', 'v1')})


def client(args):
    def worker(n):
        connection = None
        rows = []
        for i in range(args.count):
            # Safe POST fixture has no side effects. There are no automatic retries.
            body = json.dumps({'requestId': f'{n}-{i}', 'items': [
                {'quantity': 3, 'unitPriceCents': 199}, {'quantity': 2, 'unitPriceCents': 250}],
                'note': '跨叢集 café ' + 'x' * args.padding}, ensure_ascii=False).encode()
            try:
                if connection is None:
                    connection = http.client.HTTPConnection(args.host, args.port, timeout=5)
                headers = {'Content-Type': 'application/json', 'X-Request-ID': f'{n}-{i}'}
                if args.authority:
                    headers['Host'] = args.authority
                connection.request('POST', '/api/quote', body, headers)
                response = connection.getresponse()
                data = response.read()
                row = {'status': response.status}
                if response.status == 200:
                    result = json.loads(data)
                    assert result['totalCents'] == 1097
                    assert result['requestId'] == f'{n}-{i}'
                    assert result['payloadSHA256'] == hashlib.sha256(body).hexdigest()
                    row.update(pod=result['pod'], version=result['version'])
                rows.append(row)
            except (OSError, http.client.HTTPException) as e:
                rows.append({'status': 'transport-error', 'error': type(e).__name__})
                if connection:
                    connection.close()
                connection = None
            if args.fresh and connection:
                connection.close()
                connection = None
            if args.delay:
                time.sleep(args.delay)
        if connection:
            connection.close()
        return rows

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = [row for batch in pool.map(worker, range(args.workers)) for row in batch]
    counts = {}
    for row in rows:
        key = str(row['status'])
        counts[key] = counts.get(key, 0) + 1
    print(json.dumps({'requests': len(rows), 'statuses': counts,
                      'pods': sorted({r['pod'] for r in rows if 'pod' in r}),
                      'versions': sorted({r['version'] for r in rows if 'version' in r}),
                      'seconds': round(time.monotonic() - started, 3)}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['server', 'client'])
    parser.add_argument('--host')
    parser.add_argument('--port', type=int, default=443)
    parser.add_argument('--count', type=int, default=10)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--padding', type=int, default=0)
    parser.add_argument('--delay', type=float, default=0)
    parser.add_argument('--authority')
    parser.add_argument('--fresh', action='store_true')
    args = parser.parse_args()
    if args.mode == 'server':
        server = ThreadingHTTPServer(('0.0.0.0', 8080), API)
        signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
        server.serve_forever()
        server.server_close()
    else:
        client(args)
