"""Create a fictional document image for local Surya OCR testing."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def main() -> None:
    output = Path("work/surya-smoke.png")
    output.parent.mkdir(parents=True, exist_ok=True)
    font_path = Path("C:/Windows/Fonts/arial.ttf")
    if not font_path.is_file():
        font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    font = ImageFont.truetype(str(font_path), 38)
    heading = ImageFont.truetype(str(font_path), 52)
    image = Image.new("RGB", (1280, 1600), "white")
    draw = ImageDraw.Draw(image)
    draw.text((110, 100), "SAMPLE PRESCRIPTION", font=heading, fill="black")
    lines = [
        "Patient: Example Person",
        "Date: 25 September 2026",
        "Medicine: Example Tablet 500 mg",
        "Dosage: 0-1-1 for 5 days",
        "Instructions: Take after food",
        "Follow-up: Review after 7 days",
    ]
    for index, line in enumerate(lines):
        draw.text((110, 260 + index * 125), line, font=font, fill="black")
    image.save(output)
    print(output.resolve())


if __name__ == "__main__":
    main()
