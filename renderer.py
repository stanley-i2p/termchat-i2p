import base64
import os
from PIL import Image
from PIL import ImageOps
from PIL import ImageFilter


# Terminal capability detection. Implement later if needed.

def terminal_supports_native():

    # term_program = os.environ.get("TERM_PROGRAM", "").lower()
    # term = os.environ.get("TERM", "").lower()
    # 
    # if "wezterm" in term_program:
    #     return "iterm2"
    # 
    # if "kitty" in term:
    #     return "kitty"
    # 
    # if "iterm" in term_program:
    #     return "iterm2"

    return None


# Will need if using wezterm/kitty/iterm2

def render_native(path):

    # term = terminal_supports_native()
    # 
    # if not term:
    #     return None
    # 
    # with open(path, "rb") as f:
    #     data = base64.b64encode(f.read()).decode()
    # 
    # # if term == "wezterm":
    # #     return [f"\033]1337;File=inline=1:{data}\a"]
    # 
    # if term == "iterm2":
    #     return [f"\033]1337;File=inline=1:{data}\a"]
    # 
    # if term == "kitty":
    #     return [f"\033_Gf=100,a=T;{data}\033\\"]

    return None



# Detect BW images. Too primitive for now. Not used.

def is_bw_image(img):
    colors = img.convert("L").getcolors(256)
    return colors is not None and len(colors) <= 2



# BW renderer (QR and diagrams)


def render_bw(path, width=70):

    img = Image.open(path).convert("1")

    w, h = img.size
    ratio = h / w
    height = int(width * ratio)

    img = img.resize((width, height))
    px = img.load()

    lines = []

    for y in range(0, height, 2):
        line = ""

        for x in range(width):

            top = px[x, y] == 0
            bottom = False

            if y + 1 < height:
                bottom = px[x, y + 1] == 0

            if top and bottom:
                char = "█"
            elif top:
                char = "▀"
            elif bottom:
                char = "▄"
            else:
                char = " "

            line += char

        lines.append(line)

    return lines



# Braille renderer

def render_braille(path, width=70):

    img = Image.open(path).convert("L")

    # contrast normalization :)
    img = ImageOps.autocontrast(img)

    img = img.filter(ImageFilter.SHARPEN)

    w, h = img.size
    ratio = h / w

    height = int(width * ratio * 0.5)

    img = img.resize((width * 2, height * 4), Image.Resampling.LANCZOS)
    img = img.convert("1", dither=Image.FLOYDSTEINBERG)
    px = img.load()

    lines = []

    for y in range(0, height * 4, 4):

        line = ""

        for x in range(0, width * 2, 2):

            dots = 0

            for dy in range(4):
                for dx in range(2):

                    if px[x + dx, y + dy] != 0:

                        bit = [
                            [0, 3],
                            [1, 4],
                            [2, 5],
                            [6, 7],
                        ][dy][dx]

                        dots |= 1 << bit

            line += chr(0x2800 + dots)

        lines.append(line)

    return lines



def render_braille_color(path, width=70):

    source = Image.open(path).convert("RGB")
    gray = source.convert("L")
    gray = ImageOps.autocontrast(gray)
    gray = gray.filter(ImageFilter.SHARPEN)

    w, h = gray.size
    ratio = h / w

    height = int(width * ratio * 0.5)

    color_img = source.resize((width * 2, height * 4), Image.Resampling.LANCZOS)
    mask = gray.resize((width * 2, height * 4), Image.Resampling.LANCZOS)
    mask = mask.convert("1", dither=Image.FLOYDSTEINBERG)

    color_px = color_img.load()
    mask_px = mask.load()

    lines = []

    for y in range(0, height * 4, 4):

        line = ""

        for x in range(0, width * 2, 2):

            dots = 0
            red = 0
            green = 0
            blue = 0
            samples = 0

            for dy in range(4):
                for dx in range(2):

                    if mask_px[x + dx, y + dy] != 0:

                        bit = [
                            [0, 3],
                            [1, 4],
                            [2, 5],
                            [6, 7],
                        ][dy][dx]

                        dots |= 1 << bit

                        r, g, b = color_px[x + dx, y + dy]
                        red += r
                        green += g
                        blue += b
                        samples += 1

            char = chr(0x2800 + dots)

            if samples:
                red //= samples
                green //= samples
                blue //= samples
                line += f"[#{red:02x}{green:02x}{blue:02x}]{char}[/]"
            else:
                line += char

        lines.append(line)

    return lines



# Main render

def render_image(path):

    native = render_native(path)

    if native:
        return native

    img = Image.open(path)

    if is_bw_image(img):
        return render_bw(path)

    return render_braille(path)
