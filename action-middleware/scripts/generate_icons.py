from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = ROOT / "assets"
PNG_PATH = ASSETS_DIR / "actionflow.png"
ICO_PATH = ASSETS_DIR / "actionflow.ico"


def build_icon(size: int = 256) -> Image.Image:
    image = Image.new("RGBA", (size, size), (11, 17, 20, 255))
    draw = ImageDraw.Draw(image)

    pad = int(size * 0.16)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.22), fill=(11, 17, 20, 255))
    draw.ellipse((pad, pad, size - pad, size - pad), fill=(13, 189, 139, 255))

    bar_w = max(8, size // 12)
    x0 = size // 2 - bar_w // 2
    x1 = x0 + bar_w
    draw.rounded_rectangle((x0, int(size * 0.24), x1, int(size * 0.78)), radius=bar_w // 2, fill=(247, 251, 252, 255))

    notch = [
        (x1, int(size * 0.38)),
        (int(size * 0.70), int(size * 0.31)),
        (int(size * 0.70), int(size * 0.44)),
        (x1, int(size * 0.51)),
    ]
    draw.polygon(notch, fill=(247, 251, 252, 255))

    notch_left = [
        (x0, int(size * 0.55)),
        (int(size * 0.30), int(size * 0.62)),
        (int(size * 0.30), int(size * 0.49)),
        (x0, int(size * 0.42)),
    ]
    draw.polygon(notch_left, fill=(247, 251, 252, 255))
    return image


def main() -> None:
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    image = build_icon()
    image.save(PNG_PATH, format="PNG")
    image.save(ICO_PATH, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (32, 32), (16, 16)])
    print(f"Generated {PNG_PATH}")
    print(f"Generated {ICO_PATH}")


if __name__ == "__main__":
    main()
