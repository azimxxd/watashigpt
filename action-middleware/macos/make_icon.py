"""Render the app icon: dark squircle with the ActionFlow logo, then produce ICNS."""
import subprocess
import sys
from pathlib import Path
from AppKit import (NSImage, NSColor, NSBezierPath, NSMakeRect,
                    NSBitmapImageRep, NSPNGFileType,
                    NSCompositingOperationSourceOver)

REPO_ROOT = Path(__file__).resolve().parents[2]
LOGO = REPO_ROOT / "docs" / "media" / "actionflow-logo-dark.png"

CANVAS = 1024
# Apple's macOS icon grid: 824pt squircle centered on a 1024pt canvas.
SQUIRCLE = NSMakeRect(100, 100, 824, 824)
RADIUS = 185.4
LOGO_WIDTH = 640

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
if not LOGO.exists():
    sys.exit(f"logo not found: {LOGO}")

logo = NSImage.alloc().initWithContentsOfFile_(str(LOGO))
if logo is None:
    sys.exit(f"could not read logo: {LOGO}")
logo_height = LOGO_WIDTH * logo.size().height / logo.size().width

image = NSImage.alloc().initWithSize_((CANVAS, CANVAS))
image.lockFocus()
NSColor.colorWithCalibratedRed_green_blue_alpha_(0.10, 0.11, 0.13, 1).set()
NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(SQUIRCLE, RADIUS, RADIUS).fill()
# hairline edge so the dark squircle separates from dark Dock/menu backgrounds
NSColor.colorWithCalibratedWhite_alpha_(1, 1).colorWithAlphaComponent_(0.08).set()
edge = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
    NSMakeRect(101.5, 101.5, 821, 821), RADIUS - 1.5, RADIUS - 1.5)
edge.setLineWidth_(3)
edge.stroke()
logo.drawInRect_fromRect_operation_fraction_(
    NSMakeRect((CANVAS - LOGO_WIDTH) / 2, (CANVAS - logo_height) / 2,
               LOGO_WIDTH, logo_height),
    NSMakeRect(0, 0, 0, 0), NSCompositingOperationSourceOver, 1.0)
image.unlockFocus()
rep = NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
source = out / "icon.png"
rep.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(str(source), True)
iconset = out / "ActionFlow.iconset"
iconset.mkdir(exist_ok=True)
for size in (16, 32, 128, 256, 512):
    for scale in (1, 2):
        suffix = "@2x" if scale == 2 else ""
        subprocess.run(["sips", "-z", str(size*scale), str(size*scale), str(source),
                        "--out", str(iconset / f"icon_{size}x{size}{suffix}.png")], check=True, capture_output=True)
subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out / "ActionFlow.icns")], check=True)
