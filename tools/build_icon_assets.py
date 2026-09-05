#!/usr/bin/env python3
"""Build reviewable macOS AppIcon and menu-bar assets without modifying the app bundle."""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / 'assets/icons'
SOURCE = ICON_DIR / 'app-icon-master-source.png'
MASTER = ICON_DIR / 'app-icon-master-1024.png'
ICONSET = ICON_DIR / 'AppIcon.iconset'
MENU_DIR = ICON_DIR / 'menu-bar'

SIZES = {
    'icon_16x16.png': 16,
    'icon_16x16@2x.png': 32,
    'icon_32x32.png': 32,
    'icon_32x32@2x.png': 64,
    'icon_128x128.png': 128,
    'icon_128x128@2x.png': 256,
    'icon_256x256.png': 256,
    'icon_256x256@2x.png': 512,
    'icon_512x512.png': 512,
    'icon_512x512@2x.png': 1024,
}


def clean_transparency(image: Image.Image) -> Image.Image:
    """Keep the connected icon body and remove disconnected extraction flecks."""
    image = image.convert('RGBA')
    alpha = image.getchannel('A')
    mask = alpha.point(lambda value: 255 if value else 0)
    ImageDraw.floodfill(mask, (mask.width // 2, mask.height // 2), 128, thresh=0)
    kept = mask.point(lambda value: 255 if value == 128 else 0)
    clean_alpha = Image.new('L', image.size)
    clean_alpha.paste(alpha, mask=kept)
    image.putalpha(clean_alpha)
    return image


def menu_icon(scale: int) -> Image.Image:
    size = 18 * scale
    image = Image.new('L', (size, size), 0)
    draw = ImageDraw.Draw(image)
    def box(values): return tuple(round(value * scale) for value in values)
    stroke = max(1, round(1.4 * scale))
    draw.rounded_rectangle(box((1.5, 1.5, 16.5, 16.5)), radius=round(3 * scale), outline=255, width=stroke)
    # Six quota cells become a single readable rhythm at menu-bar size.
    for index in range(6):
        x = 4 + index * 1.7
        draw.rounded_rectangle(box((x, 5, x + 1.05, 6.4)), radius=max(1, scale // 2), fill=255)
    draw.rounded_rectangle(box((4, 8.5, 13.5, 10.6)), radius=max(1, scale), fill=255)
    draw.rounded_rectangle(box((11, 12, 14, 14.2)), radius=max(1, scale), fill=255)
    rgba = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    rgba.putalpha(image)
    return rgba


def font(size: int):
    for path in ['/System/Library/Fonts/SFNS.ttf', '/System/Library/Fonts/Helvetica.ttc']:
        try: return ImageFont.truetype(path, size)
        except OSError: pass
    return ImageFont.load_default()


def review_sheet(master: Image.Image, menu_1x: Image.Image, menu_2x: Image.Image):
    sheet = Image.new('RGB', (1600, 1000), '#F4F4F6')
    draw = ImageDraw.Draw(sheet)
    draw.text((72, 54), 'Codex Ink · Icon Review', fill='#17171A', font=font(40))
    draw.text((72, 108), 'AppIcon sizes and monochrome menu-bar template · not integrated', fill='#66666C', font=font(22))

    draw.rounded_rectangle((72, 166, 700, 790), radius=30, fill='white')
    draw.text((108, 198), 'AppIcon master', fill='#17171A', font=font(26))
    sample = master.resize((500, 500), Image.Resampling.LANCZOS)
    sheet.paste(sample, (136, 250), sample)

    draw.rounded_rectangle((740, 166, 1528, 500), radius=30, fill='white')
    draw.text((776, 198), 'Small-size check', fill='#17171A', font=font(26))
    x = 790
    for size in [128, 64, 32, 16]:
        icon = master.resize((size, size), Image.Resampling.LANCZOS)
        sheet.paste(icon, (x, 280 + (128-size)//2), icon)
        draw.text((x, 426), f'{size}px', fill='#66666C', font=font(18))
        x += size + 62

    draw.rounded_rectangle((740, 534, 1528, 790), radius=30, fill='white')
    draw.text((776, 566), 'Menu bar template', fill='#17171A', font=font(26))
    draw.rounded_rectangle((784, 632, 1114, 718), radius=18, fill='#FFFFFF')
    draw.rounded_rectangle((1152, 632, 1482, 718), radius=18, fill='#242427')
    light = menu_2x.resize((72, 72), Image.Resampling.NEAREST)
    dark = Image.new('RGBA', light.size, 'white'); dark.putalpha(light.getchannel('A'))
    sheet.paste(light, (912, 639), light)
    sheet.paste(dark, (1280, 639), dark)
    draw.text((884, 736), 'light menu bar', fill='#66666C', font=font(17))
    draw.text((1242, 736), 'dark menu bar', fill='#66666C', font=font(17))

    draw.text((72, 850), 'Brand rule: white e-ink frame · black information · one red state signal', fill='#333337', font=font(24))
    draw.text((72, 900), 'Menu bar icons are template images: macOS supplies the foreground color.', fill='#66666C', font=font(20))
    sheet.save(ICON_DIR / 'icon-review-sheet.png', optimize=False)


def main():
    source = clean_transparency(Image.open(SOURCE))
    master = source.resize((1024, 1024), Image.Resampling.LANCZOS)
    MASTER.parent.mkdir(parents=True, exist_ok=True)
    master.save(MASTER, optimize=False)
    ICONSET.mkdir(parents=True, exist_ok=True)
    for name, size in SIZES.items():
        master.resize((size, size), Image.Resampling.LANCZOS).save(ICONSET / name, optimize=False)
    MENU_DIR.mkdir(parents=True, exist_ok=True)
    one, two = menu_icon(1), menu_icon(2)
    one.save(MENU_DIR / 'CodexInkMenuBarTemplate.png', optimize=False)
    two.save(MENU_DIR / 'CodexInkMenuBarTemplate@2x.png', optimize=False)
    (MENU_DIR / 'CodexInkMenuBarTemplate.svg').write_text('''<svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 18 18"><g fill="none" stroke="#000" stroke-width="1.4"><rect x="1.5" y="1.5" width="15" height="15" rx="3"/></g><g fill="#000"><rect x="4" y="5" width="1.05" height="1.4" rx=".4"/><rect x="5.7" y="5" width="1.05" height="1.4" rx=".4"/><rect x="7.4" y="5" width="1.05" height="1.4" rx=".4"/><rect x="9.1" y="5" width="1.05" height="1.4" rx=".4"/><rect x="10.8" y="5" width="1.05" height="1.4" rx=".4"/><rect x="12.5" y="5" width="1.05" height="1.4" rx=".4"/><rect x="4" y="8.5" width="9.5" height="2.1" rx="1"/><rect x="11" y="12" width="3" height="2.2" rx=".8"/></g></svg>\n''')
    review_sheet(master, one, two)
    print('BUILT AppIcon.iconset (10 PNGs), app-icon-master-1024.png, CodexInkMenuBarTemplate 1x/2x/SVG, icon-review-sheet.png')

if __name__ == '__main__': main()
