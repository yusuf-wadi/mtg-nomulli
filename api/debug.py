import json, sys, os
from http.server import BaseHTTPRequestHandler
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mtg_playable_hands import (
    ensure_bulk_data, infer_produced_from_type,
    CACHE_VERSION, VERSION_PATH, INDEX_PATH
)


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            idx = ensure_bulk_data()
            island = idx.get('island', {})
            plains = idx.get('plains', {})
            body = json.dumps({
                'cache_version_expected': CACHE_VERSION,
                'cache_version_on_disk': VERSION_PATH.read_text().strip() if VERSION_PATH.exists() else 'missing',
                'index_exists': INDEX_PATH.exists(),
                'island': {
                    'type_line': island.get('type_line'),
                    'produced_mana': island.get('produced_mana'),
                    'inferred': infer_produced_from_type(island.get('type_line', '')),
                },
                'plains': {
                    'type_line': plains.get('type_line'),
                    'produced_mana': plains.get('produced_mana'),
                    'inferred': infer_produced_from_type(plains.get('type_line', '')),
                },
            }, indent=2).encode()
        except Exception as e:
            body = json.dumps({'error': str(e)}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass
