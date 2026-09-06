"""Render fictional publication data with the unchanged native UI profile; no I/O to Codex/BLE."""
from pathlib import Path
from datetime import datetime
import os
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.codex_status_core import DashboardSnapshot, DisplayTask, DisplayStatus
from tools.eink_text_renderer import render_dashboard


def main(language="zh-CN", output=None):
    # Local process only: keep the demonstration date reproducible on another Mac.
    os.environ['TZ'] = 'Asia/Shanghai'
    time.tzset()
    stamp = int(datetime.fromisoformat('2026-09-03T10:28:00+08:00').timestamp())
    snapshot = DashboardSnapshot(
        remaining_percent=61, used_percent=39,
        reset_at_epoch=int(datetime.fromisoformat('2026-09-07T15:00:00+08:00').timestamp()),
        usage_buckets=tuple([0] * 16 + [2, 3, 1, 5, 8, 4, 7, 6, 3, 9, 5, 7, 4, 8, 6, 3, 5, 7, 9, 6, 4, 8, 10, 5, 7, 6, 4, 9, 8, 6]),
        lifetime_tokens=0, updated_at_epoch=stamp,
        account_label='demo@example.com', plan_label='Pro',
        tasks=(
            DisplayTask('demo-1', 'Codex Ink', 'Prepare release' if language == 'en' else '整理发布材料', DisplayStatus.WAITING, stamp),
            DisplayTask('demo-2', 'Portfolio' if language == 'en' else '个人网站', 'Update project page' if language == 'en' else '更新作品页面', DisplayStatus.RUNNING, stamp),
            DisplayTask('demo-3', 'Reading list' if language == 'en' else '阅读清单', 'Organize weekly notes' if language == 'en' else '整理本周笔记', DisplayStatus.QUEUED, stamp),
        ),
    )
    image = render_dashboard(data=snapshot, language=language)
    assert image.size == (400, 300)
    assert set(image.getdata()) <= {(0, 0, 0), (255, 255, 255), (198, 40, 40)}
    output = Path(output) if output else Path(__file__).with_name('dashboard-demo.png')
    image.save(output, format='PNG', optimize=False)
    print(f'Rendered fictional demo: {output} (400x300, three colors)')


if __name__ == '__main__':
    main()
