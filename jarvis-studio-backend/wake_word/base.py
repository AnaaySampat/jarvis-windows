from abc import ABC, abstractmethod
from typing import Callable


class WakeWordDetector(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def on_detected(self, callback: Callable[[], None]) -> None: ...
