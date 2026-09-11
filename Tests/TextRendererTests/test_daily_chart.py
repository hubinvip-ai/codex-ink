import unittest
from datetime import date, timedelta
from PIL import Image, ImageDraw
from tools.eink_text_renderer import _draw_usage_chart

class DailyChartTests(unittest.TestCase):
    def test_thirty_daily_bars_with_calendar_week_gaps_across_month(self):
        image = Image.new('RGB', (400,300), 'white')
        end = date(2026, 9, 11)
        _draw_usage_chart(ImageDraw.Draw(image), (1,) * 30, end_date=end)
        spans = []
        for x in range(400):
            ink = image.getpixel((x,163)) != (255,255,255)
            if ink and (x == 0 or image.getpixel((x-1,163)) == (255,255,255)):
                spans.append([x,x+1])
            elif ink: spans[-1][1] = x+1
        self.assertEqual(len(spans),30)
        self.assertEqual((spans[0][0],spans[-1][1]),(10,390))
        for i in range(29):
            self.assertEqual(spans[i+1][0]-spans[i][1],6 if (end - timedelta(days=29-i-1)).weekday() == 0 else 2)

    def test_real_zero_day_keeps_its_position_without_demo_values(self):
        image = Image.new('RGB', (400,300), 'white')
        _draw_usage_chart(ImageDraw.Draw(image), (0,) * 30)
        self.assertTrue(all(image.getpixel((x,162)) == (255,255,255) for x in range(10,390)))
