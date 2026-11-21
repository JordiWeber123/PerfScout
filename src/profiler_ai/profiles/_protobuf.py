"""Bounded wire decoding for the field types used by google/pprof's profile.proto."""

from collections import defaultdict
from collections.abc import Iterator

from ._common import ProfileError

type _WireValue = int | memoryview

_MAX_FIELDS = 250_000
_MAX_STRING_BYTES = 64 * 1024


def _varint(data: memoryview, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            raise ProfileError("truncated varint in pprof protobuf")
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            break
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ProfileError("invalid 64-bit varint in pprof protobuf")


def signed_int64(value: int) -> int:
    return value - (1 << 64) if value >= (1 << 63) else value


class Message:
    """Decode scalar, packed repeated, and nested fields without copying wire payloads."""

    def __init__(self, content: memoryview, *, max_fields: int = 4096):
        self._fields: dict[int, list[_WireValue]] = defaultdict(list)
        end = len(content)
        offset = fields = 0
        while offset < end:
            fields += 1
            if fields > max_fields:
                raise ProfileError("message exceeds the pprof protobuf field limit")
            key, offset = _varint(content, offset)
            number, wire = divmod(key, 8)
            if not 0 < number < 2**29:
                raise ProfileError("invalid field number in pprof protobuf")
            if wire == 0:
                value, offset = _varint(content, offset)
                self._fields[number].append(value)
            elif wire == 2:
                width, offset = _varint(content, offset)
                if width > end - offset:
                    raise ProfileError("truncated field in pprof protobuf")
                self._fields[number].append(content[offset : offset + width])
                offset += width
            elif wire in (1, 5):
                # profile.proto declares no fixed-width fields.
                offset += 8 if wire == 1 else 4
                if offset > end:
                    raise ProfileError("truncated field in pprof protobuf")
            else:
                raise ProfileError("unsupported wire type in pprof protobuf")

    @classmethod
    def profile(cls, content: bytes) -> "Message":
        return cls(memoryview(content), max_fields=_MAX_FIELDS)

    def integer(self, number: int, default: int = 0) -> int:
        field = self._fields.get(number)
        if not field:
            return default
        value = field[-1]
        if not isinstance(value, int):
            raise ProfileError("invalid integer field in pprof protobuf")
        return value

    def integers(self, number: int, maximum: int) -> tuple[int, ...]:
        result = []
        for value in self._fields.get(number, ()):
            if isinstance(value, int):
                result.append(value)
            else:
                offset = 0
                while offset < len(value):
                    item, offset = _varint(value, offset)
                    result.append(item)
                    if len(result) > maximum:
                        raise ProfileError("repeated integer field exceeds the pprof limit")
            if len(result) > maximum:
                raise ProfileError("repeated integer field exceeds the pprof limit")
        return tuple(result)

    def _bytes(self, number: int) -> Iterator[memoryview]:
        for value in self._fields.get(number, ()):
            if not isinstance(value, memoryview):
                raise ProfileError("invalid message or string field in pprof protobuf")
            yield value

    def messages(self, number: int) -> Iterator["Message"]:
        return (Message(blob) for blob in self._bytes(number))

    def strings(self, number: int) -> tuple[str, ...]:
        strings = []
        for value in self._bytes(number):
            if len(value) > _MAX_STRING_BYTES:
                raise ProfileError("pprof string exceeds the 64 KiB limit")
            strings.append(str(value, "utf-8"))
        return tuple(strings)


def encode_integer(number: int, value: int) -> bytes:
    return _encode_varint(number * 8) + _encode_varint(value & ((1 << 64) - 1))


def encode_bytes(number: int, value: bytes) -> bytes:
    return _encode_varint(number * 8 + 2) + _encode_varint(len(value)) + value


def _encode_varint(value: int) -> bytes:
    result = bytearray()
    while value >= 128:
        result.append(128 | (value & 127))
        value >>= 7
    result.append(value)
    return bytes(result)
