#!/usr/bin/env python3
"""Isolated read-only render subprocess. stdin metadata -> stdout native PNG."""

from __future__ import annotations

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
        if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] != '--validated'):
            return 2
        state = json.load(sys.stdin)
        with CodexAppServerClient(binary=Path(sys.argv[1])) as client:
            snapshot = client.fetch()
        now = datetime.now().astimezone()
        if len(sys.argv) == 3:
            from tools.companion_data import validated_snapshot
            from tools.eink_text_renderer import render_dashboard, FINAL_PROFILE
            image = render_dashboard(profile=FINAL_PROFILE, data=validated_snapshot(snapshot, state, now))
        else:
            _, image = render_real_snapshot(snapshot, state, now)
        output = io.BytesIO()
        image.save(output, format='PNG')
        sys.stdout.buffer.write(output.getvalue())
        return 0
    except Exception:
        print('{"error_code":"source_unavailable"}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
