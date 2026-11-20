import io

import pytest
from PIL import Image

from backend.app.ai.images import MAX_VISION_BYTES, MAX_VISION_SIDE, prepare_vision_image
from backend.app.ai.providers import ProviderUnavailable


def encoded(image, fmt="PNG", **kwargs):
    output = io.BytesIO()
    image.save(output, format=fmt, **kwargs)
    return output.getvalue()


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_photo_sized_images_are_bounded_inference_copies(fmt):
    source = encoded(Image.effect_noise((720, 1600), 80).convert("RGB"), fmt)
    original = bytes(source)
    prepared = prepare_vision_image(source)
    assert len(prepared) <= MAX_VISION_BYTES
    assert source == original
    with Image.open(io.BytesIO(prepared)) as image:
        image.load()
        assert image.format == "JPEG" and image.mode == "RGB"
        assert max(image.size) <= MAX_VISION_SIDE
        assert image.width / image.height == pytest.approx(720 / 1600, abs=0.003)
        assert not image.getexif()


def test_small_transparent_images_are_not_upscaled_and_use_white_background():
    image = Image.new("RGBA", (20, 10), (255, 0, 0, 0))
    with Image.open(io.BytesIO(prepare_vision_image(encoded(image)))) as prepared:
        assert prepared.size == (20, 10)
        assert prepared.getpixel((5, 5)) == (255, 255, 255)


def test_exif_orientation_is_applied_and_metadata_removed():
    image = Image.new("RGB", (80, 40), "red")
    exif = Image.Exif()
    exif[274] = 6
    exif[270] = "private image metadata"
    source = encoded(image, "JPEG", exif=exif)
    with Image.open(io.BytesIO(prepare_vision_image(source))) as prepared:
        assert prepared.size == (40, 80)
        assert not prepared.getexif()


def test_invalid_inference_image_is_an_explicit_error():
    with pytest.raises(ProviderUnavailable) as exc:
        prepare_vision_image(b"not a valid image")
    assert exc.value.code == "ai_image_invalid"
