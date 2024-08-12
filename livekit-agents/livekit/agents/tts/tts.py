from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator, Union

from livekit import rtc

from ..utils import aio, misc


@dataclass
class SynthesizedAudio:
    request_id: str
    """Request ID (one segment could be made up of multiple requests)"""
    segment_id: str
    """Segment ID, each segment is separated by a flush"""
    frame: rtc.AudioFrame
    """Synthesized audio frame"""
    delta_text: str = ""
    """Current segment of the synthesized audio"""


@dataclass
class TTSCapabilities:
    streaming: bool


class TTS(ABC):
    def __init__(
        self,
        *,
        capabilities: TTSCapabilities,
        sample_rate: int,
        num_channels: int,
        connect_timeout: float = 0,
        keepalive_timeout: float = 0,
    ) -> None:
        self._capabilities = capabilities
        self._sample_rate = sample_rate
        self._num_channels = num_channels
        self._connect_timeout = connect_timeout
        self._keepalive_timeout = keepalive_timeout

    @property
    def capabilities(self) -> TTSCapabilities:
        return self._capabilities

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def num_channels(self) -> int:
        return self._num_channels

    @abstractmethod
    def synthesize(self, text: str) -> ChunkedStream: ...

    def stream(self) -> SynthesizeStream:
        raise NotImplementedError(
            "streaming is not supported by this TTS, please use a different TTS or use a StreamAdapter"
        )

    async def aclose(self) -> None: ...


class ChunkedStream(ABC):
    """Used by the non-streamed synthesize API, some providers support chunked http responses"""

    def __init__(self, *, connect_timeout: float = 0, keepalive_timeout: float = 0):
        self._event_ch = aio.Chan[SynthesizedAudio]()
        self._task = asyncio.create_task(self._main_task())
        self._task.add_done_callback(lambda _: self._event_ch.close())
        self._connect_timeout = connect_timeout
        self._keepalive_timeout = keepalive_timeout
        self._timeout = self._connect_timeout

    async def collect(self) -> rtc.AudioFrame:
        """Utility method to collect every frame in a single call"""
        frames = []
        async for ev in self:
            frames.append(ev.frame)
        return misc.merge_frames(frames)

    @abstractmethod
    async def _main_task(self) -> None: ...

    async def aclose(self) -> None:
        """Close is automatically called if the stream is completely collected"""
        await aio.gracefully_cancel(self._task)
        self._event_ch.close()

    async def __anext__(self) -> SynthesizedAudio:
        if self._timeout > 0:
            try:
                event = await asyncio.wait_for(
                    self._event_ch.__anext__(), self._timeout
                )
            except (TimeoutError, asyncio.TimeoutError) as e:
                raise e.__class__("synthesis timed out")
        else:
            event = await self._event_ch.__anext__()
        self._timeout = self._keepalive_timeout
        return event

    def __aiter__(self) -> AsyncIterator[SynthesizedAudio]:
        return self


class SynthesizeStream(ABC):
    class _FlushSentinel:
        pass

    def __init__(self, *, connect_timeout: float = 0, keepalive_timeout: float = 0):
        self._input_ch = aio.Chan[Union[str, SynthesizeStream._FlushSentinel]]()
        self._event_ch = aio.Chan[SynthesizedAudio]()
        self._task = asyncio.create_task(self._main_task(), name="TTS._main_task")
        self._task.add_done_callback(lambda _: self._event_ch.close())
        self._connect_timeout = connect_timeout
        self._keepalive_timeout = keepalive_timeout
        self._timeout = self._connect_timeout
        self._standby = True

    @abstractmethod
    async def _main_task(self) -> None: ...

    @property
    def standby(self) -> bool:
        return self._standby

    def push_text(self, token: str) -> None:
        """Push some text to be synthesized"""
        self._check_input_not_ended()
        self._check_not_closed()
        self._input_ch.send_nowait(token)

    def flush(self) -> None:
        """Mark the end of the current segment"""
        self._check_input_not_ended()
        self._check_not_closed()
        self._input_ch.send_nowait(self._FlushSentinel())

    def end_input(self) -> None:
        """Mark the end of input, no more text will be pushed"""
        self.flush()
        self._input_ch.close()

    async def aclose(self) -> None:
        """Close ths stream immediately"""
        self._input_ch.close()
        await aio.gracefully_cancel(self._task)
        self._event_ch.close()

    def _check_not_closed(self) -> None:
        if self._event_ch.closed:
            cls = type(self)
            raise RuntimeError(f"{cls.__module__}.{cls.__name__} is closed")

    def _check_input_not_ended(self) -> None:
        if self._input_ch.closed:
            cls = type(self)
            raise RuntimeError(f"{cls.__module__}.{cls.__name__} input ended")

    async def __anext__(self) -> SynthesizedAudio:
        if self._timeout > 0:
            try:
                event = await asyncio.wait_for(
                    self._event_ch.__anext__(), self._timeout
                )
            except (TimeoutError, asyncio.TimeoutError) as e:
                raise e.__class__("synthesis timed out")
        else:
            event = await self._event_ch.__anext__()
        self._timeout = self._keepalive_timeout
        return event

    def __aiter__(self) -> AsyncIterator[SynthesizedAudio]:
        return self
