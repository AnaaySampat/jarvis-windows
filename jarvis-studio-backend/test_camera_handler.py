"""Camera results must return to their requesting window, not the HUD bus."""

import unittest
from unittest import mock

import camera_ocr
import main


class CameraHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_camera_result_is_targeted_and_preserves_correlation(self):
        request = {"image": "fixture", "requestId": 17}
        result = {"ok": True, "requestId": 17, "text": "Bonjour", "translation": "Hello"}
        process = mock.AsyncMock(return_value=result)
        send = mock.AsyncMock(return_value=True)
        broadcast = mock.AsyncMock()
        with mock.patch.object(camera_ocr, "process_frame", process), \
             mock.patch.object(main, "send_to_current", send), \
             mock.patch.object(main, "emit", broadcast):
            await main.handle_ocr_translate(request)
        process.assert_awaited_once_with(request)
        send.assert_awaited_once_with("ocr_result", result)
        broadcast.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
