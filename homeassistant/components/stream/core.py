"""Provides core stream functionality."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Generator, Iterable
import datetime
import itertools
from typing import TYPE_CHECKING

from aiohttp import web
import async_timeout
import attr

from homeassistant.components.http.view import HomeAssistantView
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util.decorator import Registry

from .const import ATTR_STREAMS, DOMAIN

if TYPE_CHECKING:
    from . import Stream

PROVIDERS = Registry()


@attr.s(slots=True)
class StreamSettings:
    """Stream settings."""

    ll_hls: bool = attr.ib()
    min_segment_duration: float = attr.ib()
    target_part_duration: float = attr.ib()
    hls_advance_part_limit: int = attr.ib()
    hls_part_timeout: float = attr.ib()


@attr.s(slots=True)
class Part:
    """Represent a segment part."""

    duration: float = attr.ib()
    has_keyframe: bool = attr.ib()
    # video data (moof+mdat)
    data: bytes = attr.ib()


@attr.s(slots=True)
class Segment:
    """Represent a segment."""

    sequence: int = attr.ib(default=0)
    # the init of the mp4 the segment is based on
    init: bytes = attr.ib(default=None)
    duration: float = attr.ib(default=0)
    # For detecting discontinuities across stream restarts
    stream_id: int = attr.ib(default=0)
    # Parts are stored in a dict indexed by byterange for easy lookup
    # As of Python 3.7, insertion order is preserved, and we insert
    # in sequential order, so the Parts are ordered
    parts_by_byterange: dict[int, Part] = attr.ib(factory=dict)
    start_time: datetime.datetime = attr.ib(factory=datetime.datetime.utcnow)
    _stream_outputs: Iterable[StreamOutput] = attr.ib(factory=list)
    # Store text of this segment's hls playlist for reuse
    hls_playlist_template: str = attr.ib(default=None)
    hls_playlist_parts: str = attr.ib(default=None)
    # Number of playlist parts rendered so far
    hls_num_parts_rendered: int = attr.ib(default=0)
    # Set to true when all the parts are rendered
    hls_playlist_complete: bool = attr.ib(default=False)

    def __attrs_post_init__(self) -> None:
        """Run after init."""
        for output in self._stream_outputs:
            output.put(self)

    @property
    def complete(self) -> bool:
        """Return whether the Segment is complete."""
        return self.duration > 0

    @property
    def data_size_with_init(self) -> int:
        """Return the size of all part data + init in bytes."""
        return len(self.init) + self.data_size

    @property
    def data_size(self) -> int:
        """Return the size of all part data without init in bytes."""
        # We can use the last part to quickly calculate the total data size.
        if not self.parts_by_byterange:
            return 0
        last_http_range_start, last_part = next(
            reversed(self.parts_by_byterange.items())
        )
        return last_http_range_start + len(last_part.data)

    @callback
    def async_add_part(
        self,
        part: Part,
        duration: float,
    ) -> None:
        """Add a part to the Segment.

        Duration is non zero only for the last part.
        """
        self.parts_by_byterange[self.data_size] = part
        self.duration = duration
        for output in self._stream_outputs:
            output.part_put()

    def get_data(self) -> bytes:
        """Return reconstructed data for all parts as bytes, without init."""
        return b"".join([part.data for part in self.parts_by_byterange.values()])

    def get_aggregating_bytes(
        self, start_loc: int, end_loc: int | float
    ) -> Generator[bytes, None, None]:
        """Yield available remaining data until segment is complete or end_loc is reached.

        Begin at start_loc. End at end_loc (exclusive).
        Used to help serve a range request on a segment.
        """
        pos = start_loc
        while (part := self.parts_by_byterange.get(pos)) or not self.complete:
            if not part:
                yield b""
                continue
            pos += len(part.data)
            # Check stopping condition and trim output if necessary
            if pos >= end_loc:
                assert isinstance(end_loc, int)
                # Trimming is probably not necessary, but it doesn't hurt
                yield part.data[: len(part.data) + end_loc - pos]
                return
            yield part.data

    def _render_hls_template(self, last_stream_id: int, render_parts: bool) -> str:
        """Render the HLS playlist section for the Segment.

        The Segment may still be in progress.
        This method stores intermediate data in hls_playlist_parts, hls_num_parts_rendered,
        and hls_playlist_complete to avoid redoing work on subsequent calls.
        """
        if self.hls_playlist_complete:
            return self.hls_playlist_template
        # Promote str to list[str] to allow appends and joins
        playlist_template = (
            [self.hls_playlist_template] if self.hls_playlist_template else []
        )
        playlist_parts = [self.hls_playlist_parts] if self.hls_playlist_parts else []
        if not playlist_template:
            if last_stream_id != self.stream_id:
                playlist_template.append("#EXT-X-DISCONTINUITY")
            # This is a placeholder where the rendered parts will be inserted
            playlist_template.append("{}")
        if render_parts:
            for http_range_start, part in itertools.islice(
                self.parts_by_byterange.items(),
                self.hls_num_parts_rendered,
                None,
            ):
                playlist_parts.append(
                    f"#EXT-X-PART:DURATION={part.duration:.3f},URI="
                    f'"./segment/{self.sequence}.m4s",BYTERANGE="{len(part.data)}'
                    f'@{http_range_start}"{",INDEPENDENT=YES" if part.has_keyframe else ""}'
                )
        if self.complete:
            playlist_template.pop()
            playlist_template.extend(
                [
                    # Squeeze the placeholder on the same line as #EXT-X-PROGRAM-DATE-TIME
                    # just to keep tidy when there are no parts
                    "{}"
                    + "#EXT-X-PROGRAM-DATE-TIME:"
                    + self.start_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
                    + "Z",
                    f"#EXTINF:{self.duration:.3f},",
                    f"./segment/{self.sequence}.m4s",
                ]
            )
            # Append another line to parts because we don't include a newline before #EXTINF
            playlist_parts.append("")

        # Store intermediate playlist data in member variables for reuse
        self.hls_playlist_template = "\n".join(playlist_template)
        self.hls_playlist_parts = "\n".join(playlist_parts)
        self.hls_num_parts_rendered = len(self.parts_by_byterange)
        self.hls_playlist_complete = self.complete

        return self.hls_playlist_template

    def render_hls(
        self, last_stream_id: int, render_parts: bool, add_hint: bool
    ) -> str:
        """Render the HLS playlist section for the Segment including a hint if requested."""
        playlist_template = self._render_hls_template(last_stream_id, render_parts)
        if not add_hint:
            return playlist_template.format(
                self.hls_playlist_parts if render_parts else ""
            )
        # Preload hints help save round trips by informing the client about the next part.
        # The next part will usually be in this segment but will be first part of the next
        # segment if this segment is already complete.
        # pylint: disable=undefined-loop-variable
        if self.complete:  # Next part belongs to next segment
            sequence = self.sequence + 1
            start = 0
        else:  # Next part is in the same segment
            sequence = self.sequence
            start = self.data_size
        playlist_template_list = [
            playlist_template,
            f'#EXT-X-PRELOAD-HINT:TYPE=PART,URI="./segment/{sequence}.m4s",'
            f"BYTERANGE-START={start}",
        ]
        # If add_hint is true, render_parts must be true, so add the parts
        return "\n".join(playlist_template_list).format(self.hls_playlist_parts)


class IdleTimer:
    """Invoke a callback after an inactivity timeout.

    The IdleTimer invokes the callback after some timeout has passed. The awake() method
    resets the internal alarm, extending the inactivity time.
    """

    def __init__(
        self, hass: HomeAssistant, timeout: int, idle_callback: CALLBACK_TYPE
    ) -> None:
        """Initialize IdleTimer."""
        self._hass = hass
        self._timeout = timeout
        self._callback = idle_callback
        self._unsub: CALLBACK_TYPE | None = None
        self.idle = False

    def start(self) -> None:
        """Start the idle timer if not already started."""
        self.idle = False
        if self._unsub is None:
            self._unsub = async_call_later(self._hass, self._timeout, self.fire)

    def awake(self) -> None:
        """Keep the idle time alive by resetting the timeout."""
        self.idle = False
        # Reset idle timeout
        self.clear()
        self._unsub = async_call_later(self._hass, self._timeout, self.fire)

    def clear(self) -> None:
        """Clear and disable the timer if it has not already fired."""
        if self._unsub is not None:
            self._unsub()

    def fire(self, _now: datetime.datetime) -> None:
        """Invoke the idle timeout callback, called when the alarm fires."""
        self.idle = True
        self._unsub = None
        self._callback()


class StreamOutput:
    """Represents a stream output."""

    def __init__(
        self,
        hass: HomeAssistant,
        idle_timer: IdleTimer,
        deque_maxlen: int | None = None,
    ) -> None:
        """Initialize a stream output."""
        self._hass = hass
        self.idle_timer = idle_timer
        self._event = asyncio.Event()
        self._part_event = asyncio.Event()
        self._segments: deque[Segment] = deque(maxlen=deque_maxlen)

    @property
    def name(self) -> str | None:
        """Return provider name."""
        return None

    @property
    def idle(self) -> bool:
        """Return True if the output is idle."""
        return self.idle_timer.idle

    @property
    def last_sequence(self) -> int:
        """Return the last sequence number without iterating."""
        if self._segments:
            return self._segments[-1].sequence
        return -1

    @property
    def sequences(self) -> list[int]:
        """Return current sequence from segments."""
        return [s.sequence for s in self._segments]

    @property
    def last_segment(self) -> Segment | None:
        """Return the last segment without iterating."""
        if self._segments:
            return self._segments[-1]
        return None

    def get_segment(self, sequence: int) -> Segment | None:
        """Retrieve a specific segment."""
        # Most hits will come in the most recent segments, so iterate reversed
        for segment in reversed(self._segments):
            if segment.sequence == sequence:
                return segment
        return None

    def get_segments(self) -> deque[Segment]:
        """Retrieve all segments."""
        return self._segments

    async def part_recv(self, timeout: float | None = None) -> bool:
        """Wait for an event signalling the latest part segment."""
        try:
            async with async_timeout.timeout(timeout):
                await self._part_event.wait()
        except asyncio.TimeoutError:
            return False
        return True

    def part_put(self) -> None:
        """Set event signalling the latest part segment."""
        # Start idle timeout when we start receiving data
        self._part_event.set()
        self._part_event.clear()

    async def recv(self) -> bool:
        """Wait for the latest segment."""
        await self._event.wait()
        return self.last_segment is not None

    def put(self, segment: Segment) -> None:
        """Store output."""
        self._hass.loop.call_soon_threadsafe(self._async_put, segment)

    @callback
    def _async_put(self, segment: Segment) -> None:
        """Store output from event loop."""
        # Start idle timeout when we start receiving data
        self.idle_timer.start()
        self._segments.append(segment)
        self._event.set()
        self._event.clear()

    def cleanup(self) -> None:
        """Handle cleanup."""
        self._event.set()
        self.idle_timer.clear()
        self._segments = deque(maxlen=self._segments.maxlen)


class StreamView(HomeAssistantView):
    """
    Base StreamView.

    For implementation of a new stream format, define `url` and `name`
    attributes, and implement `handle` method in a child class.
    """

    requires_auth = False
    platform = None

    async def get(
        self, request: web.Request, token: str, sequence: str = ""
    ) -> web.StreamResponse:
        """Start a GET request."""
        hass = request.app["hass"]

        stream = next(
            (s for s in hass.data[DOMAIN][ATTR_STREAMS] if s.access_token == token),
            None,
        )

        if not stream:
            raise web.HTTPNotFound()

        # Start worker if not already started
        stream.start()

        return await self.handle(request, stream, sequence)

    async def handle(
        self, request: web.Request, stream: Stream, sequence: str
    ) -> web.StreamResponse:
        """Handle the stream request."""
        raise NotImplementedError()
