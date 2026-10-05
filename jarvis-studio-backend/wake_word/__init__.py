from wake_word.base import WakeWordDetector
from wake_word.openwakeword import OpenWakeWordDetector


def create_detector() -> WakeWordDetector:
    return OpenWakeWordDetector()
