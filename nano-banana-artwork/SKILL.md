---
name: nano-banana-artwork
description: >
  Generate consistent character artwork using Google's Nano Banana Pro image generation API
  (also known as gemini-3-pro-image-preview). Use this skill whenever the user wants to create
  AI-generated character illustrations, movie posters, logos, themed artwork sets, or any
  project requiring consistent character appearance across multiple images using the Google
  GenAI / Nano Banana Pro API. Also trigger when the user mentions "Nano Banana", "character
  consistency", "Pixar style posters", "AI movie posters", "logo design", "studio logo",
  or wants to generate a set of themed images featuring the same person/character, or wants
  to iteratively refine AI-generated logos or icons.
---

# Nano Banana Pro Artwork Generation

A proven workflow for creating consistent, high-quality character artwork across multiple
themed images using Google's Nano Banana Pro API. Developed through extensive iteration
on a bat mitzvah movie poster project.

## Core Principles

### 1. Character Consistency Is Everything

The #1 challenge is making the same character look identical across different scenes, outfits,
and themes. The solution has three parts:

- **Reference photos**: Use 3-4 real photos of the person at different angles as identity anchors
- **Style anchor images**: Once you generate one image that nails the character, feed it back
  as a reference for ALL subsequent generations
- **Explicit anti-drift language**: Certain themes (Barbie, cartoon, fairy tale) will pull the
  model toward childish/cartoon rendering. You must explicitly counteract this in prompts

### 2. Semi-Realistic Beats Pixar

Through iteration, we learned that asking for "Pixar style" produces inconsistent, overly
cartoonish results with generic features. The sweet spot is:

> "Semi-realistic 3D animated style — closer to realistic than cartoon. NOT a cute childish
> Pixar look."

Key descriptors that help: "naturally-proportioned eyes (NOT oversized cartoon eyes)",
"angular face with defined cheekbones — not round or baby-faced", "natural skin texture"

### 3. Separate Character From Environment

For themed posters (Barbie, etc.), explicitly state that the theme applies to the
ENVIRONMENT and COLORS only, not the character's rendering style. This prevents the model
from making the character look like a doll/cartoon just because the scene is cartoony.

## API Setup

### Model and Endpoint

```python
# Model name
MODEL = "nano-banana-pro-preview"

# REST API endpoint (use REST directly — the Python SDK may lag behind on features)
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={API_KEY}"
```

### Resolution Control

The Python `google-genai` SDK's `ImageConfig` class may not expose `image_size`. Use the
REST API directly to get 4K output:

```python
import requests, base64, json
from PIL import Image
from io import BytesIO

def generate_image(prompt, reference_images, api_key, aspect_ratio="2:3", resolution="4K"):
    """Generate an image with reference photos for character consistency.

    Args:
        prompt: Full text prompt
        reference_images: List of PIL Image objects (real photos + style anchors)
        api_key: Google AI Studio API key
        aspect_ratio: "2:3" for vertical posters, "3:2" for landscape, etc.
        resolution: "1K", "2K", or "4K" (MUST be uppercase)

    Returns:
        PIL Image object
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models/nano-banana-pro-preview:generateContent?key={api_key}"

    # Build parts: text prompt + reference images as base64
    parts = [{"text": prompt}]
    for img in reference_images:
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        parts.append({"inlineData": {"mimeType": "image/jpeg", "data": b64}})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": {
                "aspectRatio": aspect_ratio,
                "imageSize": resolution   # MUST be uppercase: "4K" not "4k"
            }
        }
    }

    resp = requests.post(url, json=payload, timeout=300)
    data = resp.json()

    for part in data["candidates"][0]["content"]["parts"]:
        if "inlineData" in part:
            return Image.open(BytesIO(base64.b64decode(part["inlineData"]["data"])))
    return None
```

**Critical details:**
- `imageConfig` is the correct field name (NOT `imageGenerationConfig`)
- `imageSize` must be uppercase: `"4K"` not `"4k"` — lowercase silently defaults to 1K
- Resolution output sizes: 1K (~848×1264), 2K (~1696×2528), 4K (~3392×5056) for 2:3

### Resolution Strategy

- Use `"2K"` for drafts and iteration (cheaper, faster)
- Use `"4K"` for final versions only (~$0.15/image vs ~$0.10)
- 4K at 2:3 = ~3392×5056 pixels = ~11×17" at 300 DPI (good for print)

## Preprocessing Reference Photos

Always preprocess before sending to the API:

```python
from PIL import Image, ImageOps

def preprocess_reference(img_path, max_dim=2048):
    img = Image.open(img_path)
    img = ImageOps.exif_transpose(img)  # Fix EXIF rotation (critical for phone photos!)
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img
```

**Why `exif_transpose` matters:** iPhone photos often have EXIF orientation tags that make
them display correctly in photo viewers but appear upside-down as raw pixel data. The API
sees raw pixels, so you MUST fix this before sending.

## The Phased Workflow

### Phase 1: Character Establishment (2K, ~10 calls)

Generate simple portraits with ONLY the character description + reference photos. No themed
costumes, no complex backgrounds. Just: "standing in a park, smiling, casual clothes."

**Goal:** Find the right balance of realism vs. stylization that captures the person's likeness.

**What to iterate on:**
- Eye size (too large = generic cartoon)
- Face shape (round vs. angular)
- Hair texture and bangs
- Eyebrow prominence
- Skin tone accuracy
- Overall rendering style (too Pixar vs. too realistic)

Once you get one image that the user loves, that becomes the **style anchor** for everything else.

### Phase 2: Themed Drafts (2K, ~30-40 calls)

Generate 2-3 variations per theme/poster. Feed the style anchor image alongside the
reference photos for every generation. Include explicit character consistency instructions
in every prompt.

**Prompt structure:**
```
[Character consistency instruction — reference the style anchor]
[Character description block — same for every poster]
[Theme-specific scene, outfit, pose, background]
[Typography and branding instructions]
[Logo placement instructions]
```

Generate multiple variations with subtle differences:
- Different poses (standing vs. walking vs. seated)
- Different compositions (close-up vs. full body)
- Different backgrounds (minimal vs. detailed)
- Different expressions (smile vs. smirk vs. laugh)

### Phase 3: Refinement (2K or 4K, ~10-20 calls)

Take the user's favorite from each theme and generate 3 near-identical variants with
only tiny differences. This is where you dial in the final look.

Common refinements at this stage:
- Logo size and placement
- Necklace/jewelry details
- Text font and positioning
- Expression subtleties
- Background details

### Phase 4: Final Renders (4K, ~3-5 calls per poster)

Re-run the winning prompts at 4K. Generate 2-3 variants for final selection.

## Prompt Architecture

### The Character Consistency Block

Prepend this to EVERY prompt (customize the description for your character):

```
Use the attached reference photos of the real person AND the [style anchor poster name]
poster image as your character and style guide. The animated character MUST look identical
to the person in the [style anchor] poster — same face shape, same eye size and shape,
same nose, same eyebrows, same hair, same semi-realistic rendering style.

The character is [age]-year-old [name] rendered in a semi-realistic 3D animated style —
the SAME style as the attached [style anchor] reference. [Specific physical description].
The rendering should be elegant and semi-realistic — NOT cute, NOT childish, NOT overly
cartoonish, NOT Pixar-style big eyes.
```

### Anti-Drift Language for Cartoon-Prone Themes

For themes like Barbie, Disney, fairy tales — add this:

```
CRITICAL: Render [name] in the EXACT same semi-realistic style as the [style anchor]
posters. The [theme] aesthetic applies to the ENVIRONMENT and COLORS only — NOT to the
character's rendering style. She should NOT look like a doll or cartoon.
```

### Logo/Branding Instructions

Be extremely specific about logo placement. Lessons learned:
- Specify exact corner (e.g., "lower left corner")
- State what it should NOT overlap ("must not block the character's hands")
- Reference a size ("similar to a typical movie production company logo")
- Describe the visual design explicitly ("red rectangle with white MIRI, STUDIOS in white to the right")

### Typography Instructions

- State where text goes ("at the TOP", "at the BOTTOM ONLY")
- Specify "do NOT place at the top" if you only want it at the bottom (the model tends to duplicate)
- Describe font style, color, and relative sizing
- For multi-word titles, specify color per word if needed

## Project Structure

```
project/
  .env                    # GOOGLE_API_KEY=xxx
  config.py               # Paths, model name, reference image list
  prompts.py              # All prompt configs with character block
  preprocess.py           # EXIF fix + resize
  generate_posters.py     # Main CLI script
  miri_images/
    processed/            # Preprocessed reference photos
  output/
    phase1_character/     # Character establishment tests
    phase2_drafts/        # Themed draft iterations
    phase3_final/         # Final 4K renders
  logs/
    generation_log.jsonl  # Every API call logged with full prompt
```

### Logging

Log every API call as a JSON line with: timestamp, poster_id, phase, prompt, resolution,
output filename, success/failure. This lets you trace exactly which prompt produced which
image and replay successful generations.

## Logo / Icon Generation Mode

When creating logos, icons, or branding elements instead of character posters, the workflow
shifts from character consistency to **design precision and iterative refinement**.

### Logo Workflow

1. **Start with a reference** — feed an existing logo or style reference (Columbia Pictures,
   Marvel Studios, etc.) alongside your prompt. The model reproduces compositions well from
   visual references.

2. **Iterate on specific elements** — logos are easier to refine than character art because
   changes are more discrete: "make the text larger", "move the logo to the lower left",
   "add a glow effect to the letters". Be surgical in your change requests.

3. **Use 1:1 aspect ratio** for logos (square), not 2:3.

4. **Use 2K for drafts, 4K for finals** — same as character art.

5. **Feed the best version back** — same style-anchor technique as character art. When a
   version is 90% right, feed it as a reference and ask for the specific change.

### Logo Prompting Tips

- **Describe every element explicitly**: figure, text, colors, background, spatial layout
- **Specify text placement precisely**: "across the top", "below the pedestal", "evenly
  spaced with the torch between the I and R"
- **Reference classic logos by name**: "in the style of the Columbia Pictures logo" works
  well as a composition anchor
- **For text effects**: describe how light interacts with letters — "torch casts warm golden
  glow on nearby letters" — rather than abstract style descriptions
- **"Change nothing except X" works better for logos than characters** — but still generate
  2-3 variants because small drift is unavoidable

### Background Removal for Logos

When logos need transparent backgrounds (for overlays, print materials, etc.):

**Approach 1: Programmatic removal (PIL/Python)**

Instruct the model to use pure black `#000000` background, then remove near-black pixels:

```python
from PIL import Image

img = Image.open('logo.png').convert('RGBA')
pixels = img.load()
w, h = img.size

# threshold: max channel value to treat as "black"
# Start at 5% (~13) and increase if needed. 10% (~26) is usually sufficient.
threshold = 26  # ~10% of 255

for y in range(h):
    for x in range(w):
        r, g, b, a = pixels[x, y]
        if max(r, g, b) <= threshold:
            pixels[x, y] = (r, g, b, 0)

img.save('logo_transparent.png', 'PNG')
```

**Important caveat:** AI-generated "black" backgrounds are never truly uniform `#000000`.
The model introduces subtle dark gradients and noise even when explicitly instructed to use
pure black. Expect ~55-60% of pixels to be true `#000000`, with the rest being near-black.
A 10% threshold typically removes the background cleanly but may nibble at dark gradient
edges (like glow rays). For production quality, **use Photoshop** instead (see below).

**Approach 2: Photoshop (recommended for final production)**

Photoshop's "Color Range" or Magic Wand tool with feathering handles the gradient edges
much better than a hard threshold. This is the recommended approach for final print-quality
assets. Steps:

1. Open the logo in Photoshop
2. Select > Color Range
3. Click on the black background with the eyedropper
4. Adjust Fuzziness slider (start ~30, increase until background is fully selected)
5. Use "Add to Sample" eyedropper (+) to click any remaining dark patches
6. Click OK to create selection
7. Optional: Select > Modify > Feather (1-2px) for smoother edges
8. Press Delete to remove the selected background
9. File > Export > Export As PNG (ensure Transparency is checked)

## Key Lessons Learned

1. **Start with character, not themes.** Get the face right in a neutral setting before
   adding costumes and backgrounds.

2. **Feed successful outputs back as references.** Once you nail one poster, use it as a
   style anchor for all others. This is the single most effective technique for consistency.

3. **The model drifts toward cartoon for "cute" themes.** Barbie, Disney, fairy tales all
   pull toward childish rendering. Fight this with explicit anti-drift language.

4. **Use the REST API, not the Python SDK.** The SDK may lag behind on features like
   `imageSize`. The REST API gives you full control.

5. **EXIF rotation will bite you.** Phone photos look right in Finder but are upside-down
   in raw pixels. Always use `PIL.ImageOps.exif_transpose()`.

6. **Logo placement is hard.** Be extremely specific. The model tends to make logos too
   large or place them where they overlap the character. Iterate on this separately.

7. **Generate 3 near-identical variants for final selection.** Even with the same prompt,
   outputs vary subtly. Give the user options.

8. **Cost is ~$0.07-0.15 per image.** A full project with 50-80 iterations runs ~$5-10.

9. **Uppercase "4K" matters.** Lowercase "4k" silently defaults to 1K resolution.

10. **"Change nothing except X" rarely works perfectly.** The model will always introduce
    small variations. Accept this and generate multiple variants.
