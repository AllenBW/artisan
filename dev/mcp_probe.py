"""Connects to Artisan's MCP server like Claude does, calls every tool and writes the results as JSON.

Used by e2e-test.ps1 during a test roast, and handy by hand to check that the server is up:
    python dev/mcp_probe.py [--url http://127.0.0.1:8765/mcp] [--note "text"] [--tasting "notes"] [--out probe.json]
Exit code 1 if the server cannot be reached or a tool call fails.
"""

import argparse
import asyncio
import json
import sys
from typing import Any

from mcp import Client


def payload(result: Any) -> Any:
    structured = getattr(result, 'structured_content', None) or getattr(result, 'structuredContent', None)
    if structured is not None:
        return structured.get('result', structured) if isinstance(structured, dict) and list(structured) == ['result'] else structured
    texts = [getattr(c, 'text', '') for c in getattr(result, 'content', [])]
    joined = '\n'.join(t for t in texts if t)
    try:
        return json.loads(joined)
    except ValueError:
        return joined


async def probe(url: str, note: str, tasting: str = '') -> dict[str, Any]:
    out: dict[str, Any] = {'url': url, 'errors': []}
    async with Client(url, read_timeout_seconds=15) as client:
        tools = await client.list_tools()
        out['tools'] = sorted(t.name for t in tools.tools)
        calls: list[tuple[str, dict[str, Any]]] = [
            ('get_live_roast', {}),
            ('get_roast_curve', {'step_s': 20}),
            ('list_roasts', {'bean': 'India Natural', 'limit': 3}),
            ('compare_with_history', {}),
        ]
        if note:
            calls.append(('add_roast_note', {'text': note}))
        calls.append(('list_tastings', {'limit': 5}))
        for name, args in calls:
            result = await client.call_tool(name, args)
            if getattr(result, 'is_error', False) or getattr(result, 'isError', False):
                out['errors'].append({name: payload(result)})
            out[name] = payload(result)
        roasts = out.get('list_roasts')
        if tasting and isinstance(roasts, list) and roasts:
            args = {'roast': roasts[0]['name'], 'method': 'pour-over', 'score': 3, 'development': 'under', 'grind': '20',
                'descriptors': ['Sour'], 'notes': tasting}
            result = await client.call_tool('add_tasting', args)
            out['add_tasting'] = payload(result)
            if getattr(result, 'is_error', False) or getattr(result, 'isError', False) or 'error' in (out['add_tasting'] or {}):
                out['errors'].append({'add_tasting': out['add_tasting']})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', default='http://127.0.0.1:8765/mcp')
    ap.add_argument('--note', default='')
    ap.add_argument('--out', default='')
    ap.add_argument('--tasting', default='', help='also log a tasting of the newest India Natural with these notes')
    args = ap.parse_args()
    try:
        result = asyncio.run(probe(args.url, args.note, args.tasting))
    except Exception as e: # pylint: disable=broad-except
        result = {'url': args.url, 'errors': [repr(e)]}
    text = json.dumps(result, indent=1, default=str)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as f:
            f.write(text)
    live = result.get('get_live_roast') or {}
    print(f"tools: {result.get('tools')}\nstate: {live.get('state') if isinstance(live, dict) else live}, BT {live.get('BT') if isinstance(live, dict) else ''}"
        f"\nerrors: {result['errors']}")
    sys.exit(1 if result['errors'] else 0)


if __name__ == '__main__':
    main()
