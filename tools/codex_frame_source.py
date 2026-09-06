#!/usr/bin/env python3
"""Isolated read-only render subprocess. stdin metadata -> stdout native PNG."""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.codex_app_server import CodexAppServerClient
from tools.codex_eink_sync import render_real_snapshot


def main():
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('binary', type=Path)
        parser.add_argument('--validated', action='store_true')
        parser.add_argument('--language', choices=('zh-CN','en'), default='zh-CN')
        args = parser.parse_args()
        state = json.load(sys.stdin)
        with CodexAppServerClient(binary=args.binary) as client:
            snapshot = client.fetch()
        now = datetime.now().astimezone()
        if args.validated:
            from tools.companion_data import validated_snapshot
            from tools.eink_text_renderer import render_dashboard, FINAL_PROFILE
            image = render_dashboard(profile=FINAL_PROFILE, data=validated_snapshot(snapshot, state, now, language=args.language), language=args.language)
        else:
            _, image = render_real_snapshot(snapshot, state, now, language=args.language)
        output = io.BytesIO()
        image.save(output, format='PNG')
        sys.stdout.buffer.write(output.getvalue())
        return 0
    except Exception:
        print('{"error_code":"source_unavailable"}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
