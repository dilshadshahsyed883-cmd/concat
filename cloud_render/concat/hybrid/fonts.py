"""Resolve shipped font faces explicitly; unsupported faces never silently substitute."""
from pathlib import Path

FACES = {
    "Hanken Grotesk": {400: "HankenGrotesk-Regular.ttf", 500: "HankenGrotesk-Medium.ttf",
                       600: "HankenGrotesk-SemiBold.ttf", 700: "HankenGrotesk-Bold.ttf"},
    "Inter": {400: "Inter-Regular.ttf", 600: "Inter-SemiBold.ttf", 700: "Inter-Bold.ttf", 900: "Inter-Black.ttf"},
    "Montserrat": {400: "Montserrat-Regular.ttf", 700: "Montserrat-Bold.ttf", 800: "Montserrat-ExtraBold.ttf", 900: "Montserrat-Black.ttf"},
    "Archivo Black": {400: "ArchivoBlack-Regular.ttf"},
    "Anton": {400: "Anton-Regular.ttf"},
}

def resolve_font(family, weight):
    if not isinstance(family, str) or type(weight) not in (int, float):
        raise ValueError("Unsupported caption styling fontFamily or fontWeight")
    family = family.strip('"')
    # Older desktop caption presets serialized Archivo Black as 900 even
    # though the only shipped face (and desktop-selected face) is regular.
    if family == "Archivo Black" and weight == 900:
        weight = 400
    filename = FACES.get(family, {}).get(weight)
    if filename is None:
        raise ValueError("Unsupported caption styling fontFamily or fontWeight; choose a shipped font face")
    packaged = Path(__file__).with_name("fonts") / filename
    source = Path(__file__).resolve().parents[3] / "upstream/src/crates/concat-text/fonts" / filename
    for path in (packaged, source):
        if path.is_file():
            return path
    # Retain compatibility with older images containing the single original face.
    old = Path(__file__).with_name(filename)
    if old.is_file():
        return old
    raise ValueError(f"Caption font is missing from this worker: {filename}; update the worker when authorized")
