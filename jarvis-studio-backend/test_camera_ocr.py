"""Camera requests remain bounded, correlate replies, and preserve partial OCR."""

import asyncio
import base64
import io
import sys
import types
import unittest
import struct
import zlib
from unittest import mock

import camera_ocr

def png_frame(width=1, height=1):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    raw += chunk(b"IDAT", zlib.compress(b"\0\xff\xff\xff")) + chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(raw).decode()


FRAME = png_frame()


class CameraTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.vision = mock.AsyncMock(return_value="Bonjour")
        self.translate = mock.AsyncMock(return_value="Hello")
        self.bridge = types.ModuleType("llm.groq_bridge")
        self.bridge.vision_query = self.vision
        self.bridge.translate_text = self.translate
        self.modules = mock.patch.dict(sys.modules, {"llm.groq_bridge": self.bridge})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    async def test_reads_and_translates_with_matching_request_id(self):
        result = await camera_ocr.process_frame({"image": FRAME, "from": "fr", "to": "en", "requestId": 41})
        self.assertTrue(result["ok"])
        self.assertEqual((result["text"], result["translation"], result["requestId"]), ("Bonjour", "Hello", 41))
        self.translate.assert_awaited_once_with("Bonjour", "fr", "en")

    async def test_invalid_remote_urls_and_bad_frames_never_reach_a_provider(self):
        for image in ("https://example.com/image.jpg", "data:image/png;base64,broken", "data:text/plain;base64,Zm9v"):
            with self.subTest(image=image):
                result = await camera_ocr.process_frame({"image": image})
                self.assertFalse(result["ok"])
                self.assertTrue(result["error"])
        self.vision.assert_not_awaited()

    async def test_oversized_input_is_rejected_before_decode(self):
        with mock.patch.object(camera_ocr, "MAX_IMAGE_BYTES", 4):
            result = await camera_ocr.process_frame({"image": FRAME + "A" * 200})
        self.assertFalse(result["ok"])
        self.assertIn("too large", result["error"])
        self.vision.assert_not_awaited()

    async def test_unsupported_language_does_not_call_provider(self):
        result = await camera_ocr.process_frame({"image": FRAME, "to": "auto"})
        self.assertFalse(result["ok"])
        self.vision.assert_not_awaited()

    async def test_no_text_is_a_clear_empty_result_without_translation(self):
        self.vision.return_value = "__NO_TEXT__"
        result = await camera_ocr.process_frame({"image": FRAME})
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertIn("No readable text", result["message"])
        self.translate.assert_not_awaited()

    async def test_same_language_does_not_spend_a_translation_call(self):
        result = await camera_ocr.process_frame({"image": FRAME, "from": "fr", "to": "fr"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["translation"], "Bonjour")
        self.translate.assert_not_awaited()

    async def test_cold_vision_recovery_can_finish_before_local_fallback(self):
        # The live synthetic recovery took 54.64 seconds. Simulate its deadline
        # without adding a minute to every suite: the former 45-second wrapper
        # cancels a valid response and incorrectly attempts local OCR.
        async def recovering_vision(awaitable, timeout):
            if timeout < 55:
                awaitable.close()
                raise asyncio.TimeoutError()
            return await awaitable

        with mock.patch.object(camera_ocr.asyncio, "wait_for", recovering_vision), \
             mock.patch.object(camera_ocr, "_local_text", side_effect=RuntimeError("unexpected local fallback")) as local:
            result = await camera_ocr.process_frame({"image": FRAME, "from": "fr", "to": "fr"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "Bonjour")
        local.assert_not_called()

    async def test_vision_unavailable_uses_optional_local_ocr(self):
        self.vision.return_value = ""
        with mock.patch.object(camera_ocr, "_local_text", return_value="Salut") as local:
            result = await camera_ocr.process_frame({"image": FRAME, "from": "fr"})
        self.assertTrue(result["ok"])
        local.assert_called_once()
        self.assertEqual(result["text"], "Salut")

    async def test_missing_local_dependency_has_actionable_error(self):
        self.vision.return_value = ""
        with mock.patch.object(camera_ocr, "_local_text", side_effect=RuntimeError("install easyocr")):
            result = await camera_ocr.process_frame({"image": FRAME})
        self.assertFalse(result["ok"])
        self.assertIn("install easyocr", result["error"])

    async def test_translation_failure_keeps_recognized_text(self):
        self.translate.return_value = "(translation failed: HTTP 429)"
        result = await camera_ocr.process_frame({"image": FRAME})
        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "Bonjour")
        self.assertIn("429", result["error"])

    async def test_fake_signatures_and_truncated_png_never_reach_provider(self):
        images = [b"\x89PNG\r\n\x1a\nfixture", b"\xff\xd8\xffbroken",
                  base64.b64decode(FRAME.split(",")[1])[:-8]]
        for blob in images:
            mime = "jpeg" if blob.startswith(b"\xff") else "png"
            image = f"data:image/{mime};base64," + base64.b64encode(blob).decode()
            with self.subTest(mime=mime):
                result = await camera_ocr.process_frame({"image": image})
                self.assertFalse(result["ok"])
        self.vision.assert_not_awaited()

    async def test_compressed_images_with_excessive_dimensions_are_rejected(self):
        for width, height in ((5000, 5000), (8193, 1)):
            result = await camera_ocr.process_frame({"image": png_frame(width, height)})
            self.assertFalse(result["ok"])
            self.assertIn("dimensions", result["error"])
        self.vision.assert_not_awaited()

    async def test_malformed_language_value_is_an_actionable_error(self):
        result = await camera_ocr.process_frame({"image": FRAME, "from": ["en"]})
        self.assertFalse(result["ok"])
        self.assertIn("supported", result["error"])
        self.vision.assert_not_awaited()

    async def test_local_auto_does_not_silently_assume_english(self):
        self.vision.return_value = ""
        result = await camera_ocr.process_frame({"image": FRAME})
        self.assertFalse(result["ok"])
        self.assertIn("Choose the text's language", result["error"])

    async def test_provider_exception_falls_back_without_leaking_details(self):
        self.vision.side_effect = RuntimeError("provider-specific failure")
        with mock.patch.object(camera_ocr, "_local_text", return_value="Salut"):
            result = await camera_ocr.process_frame({"image": FRAME, "from": "fr"})
        self.assertTrue(result["ok"])
        self.assertNotIn("provider-specific", str(result))

    async def test_real_jpeg_is_accepted_but_truncated_pixel_stream_is_rejected(self):
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (16, 16), "white").save(output, "JPEG")
        blob = output.getvalue()
        image = "data:image/jpeg;base64," + base64.b64encode(blob).decode()
        result = await camera_ocr.process_frame({"image": image, "from": "fr"})
        self.assertTrue(result["ok"])
        self.vision.reset_mock()
        truncated = "data:image/jpeg;base64," + base64.b64encode(blob[:-20]).decode()
        result = await camera_ocr.process_frame({"image": truncated, "from": "fr"})
        self.assertFalse(result["ok"])
        self.vision.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
