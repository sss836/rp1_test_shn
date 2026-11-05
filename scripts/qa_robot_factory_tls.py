"""Local synthetic HTTPS acceptance; no production certificate or device is used."""
import http.server
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import time

from rp1_monitor.factory_bridge import BridgeError, JsonHttpClient


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b'{"items":[]}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            if self.path == '/slow':
                for value in body:
                    self.wfile.write(bytes([value]))
                    self.wfile.flush()
                    time.sleep(.04)
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass


def main():
    with tempfile.TemporaryDirectory(prefix='rp1-synthetic-tls-') as directory:
        cert, key = Path(directory) / 'cert.pem', Path(directory) / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-keyout', str(key), '-out', str(cert), '-subj', '/CN=localhost',
                        '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = 'https://127.0.0.1:' + str(server.server_address[1])
        previous_ca = os.environ.get('SSL_CERT_FILE')
        try:
            try:
                JsonHttpClient(url, timeout=1).request('GET', '/ok')
                raise AssertionError('An untrusted TLS certificate was accepted')
            except BridgeError:
                pass
            os.environ['SSL_CERT_FILE'] = str(cert)
            assert JsonHttpClient(url, timeout=1).request('GET', '/ok') == {'items': []}
            client = JsonHttpClient(url, timeout=.12)
            started = time.monotonic()
            try:
                client.request('GET', '/slow')
                raise AssertionError('Slow TLS response escaped its deadline')
            except BridgeError as failure:
                assert failure.code == 'request_deadline_exceeded', failure.code
            assert time.monotonic() - started < .7
            assert not any(t.name == 'rp1-bridge-request-deadline' and t.is_alive() for t in threading.enumerate())
            print('PASS: untrusted TLS refused; trusted CA HTTPS succeeded; slow TLS body interrupted; no deadline timer remained', flush=True)
        finally:
            if previous_ca is None:
                os.environ.pop('SSL_CERT_FILE', None)
            else:
                os.environ['SSL_CERT_FILE'] = previous_ca
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    main()
