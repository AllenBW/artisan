"""A stand-in for the Claude Messages API, for the end-to-end test of the in-app Claude panel (no API key, no cost).

POST /v1/messages answers like the API does:
- a turn whose last message is the user's question: two tool calls in one reply (get_live_roast, list_tastings)
- a turn that carries tool results: a text reply quoting BT from the get_live_roast result (proves the tool ran)
Every request (headers and body) is appended to --log as one JSON line for the verifier.

    python dev/fake_claude_api.py --port 5021 --log run/claude-requests.jsonl
"""

import json
import argparse
import itertools
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

counter = itertools.count(1)
lock = threading.Lock()


def reply(body:dict) -> dict:
    n = next(counter)
    last = body['messages'][-1]
    content = last.get('content')
    results = [b for b in content if isinstance(b, dict) and b.get('type') == 'tool_result'] if isinstance(content, list) else []
    base = {'id': f'msg_fake_{n}', 'type': 'message', 'role': 'assistant', 'model': body.get('model', ''),
        'stop_sequence': None, 'usage': {'input_tokens': 100, 'output_tokens': 20}}
    if not results:
        return {**base, 'stop_reason': 'tool_use', 'content': [
            {'type': 'text', 'text': 'Let me look at the roast.'},
            {'type': 'tool_use', 'id': f'toolu_fake_{n}_a', 'name': 'get_live_roast', 'input': {}},
            {'type': 'tool_use', 'id': f'toolu_fake_{n}_b', 'name': 'list_tastings', 'input': {'limit': 5}},
        ]}
    bt = None
    for r in results:
        text = r.get('content')
        if isinstance(text, list):
            text = ''.join(b.get('text', '') for b in text if isinstance(b, dict))
        try:
            data = json.loads(text or '')
            if isinstance(data, dict) and 'BT' in data:
                bt = data['BT']
        except ValueError:
            pass
    return {**base, 'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': f'FAKE-CLAUDE reply {n}: BT is {bt}.'}]}


class Handler(BaseHTTPRequestHandler):
    log_path = ''

    def do_POST(self) -> None: # noqa: N802
        length = int(self.headers.get('content-length', 0))
        body = json.loads(self.rfile.read(length) or b'{}')
        with lock:
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps({'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()}, 'body': body}) + '\n')
        if not self.path.startswith('/v1/messages'):
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(reply(body)).encode()
        self.send_response(200)
        self.send_header('content-type', 'application/json')
        self.send_header('request-id', 'req_fake')
        self.send_header('content-length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args:object) -> None:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=5021)
    ap.add_argument('--log', required=True)
    args = ap.parse_args()
    Handler.log_path = args.log
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()
