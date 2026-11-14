"""Bounded-memory JSON emission for streamed report documents.

The object APIs (:func:`terrain_flow.drainage.drain` and
:func:`terrain_flow.pipeline.run`) keep returning complete dictionaries.  The
CLI production path instead feeds a *document descriptor* to this module: a
small dictionary whose potentially huge arrays are :class:`Records` objects
backed by generators.  The encoder walks those generators record by record, so
neither the per-cell list nor the final report string is ever materialised.

The bytes are identical to ``json.dumps(..., sort_keys=True,
ensure_ascii=False, allow_nan=False)`` with either ``indent=2`` and a trailing
newline (report files) or the default compact separators (stdout).

Chunks are joined in a fixed-size buffer (``_BUFFER_LIMIT`` characters) and
handed to a sink.  A sink may, as in batch ingestion, update a SHA-256 and a
byte counter while writing, which is how partition metadata is produced
without a second pass over the report.
"""

import json
from json.encoder import encode_basestring

_PADS = ['  ' * level for level in range(16)]
_BUFFER_LIMIT = 1 << 16
_CHUNK_SIZE = 1 << 20


def _pad(level):
    if level < len(_PADS):
        return _PADS[level]
    return '  ' * level


class Records:
    """A JSON array produced one record at a time by a restartable factory.

    ``factory()`` must return a fresh iterator each call.  A record array is
    normally serialised twice (the output file and stdout), so the document is
    intentionally re-iterable and is never backed by an exhausted generator.
    """

    __slots__ = ('_factory',)

    def __init__(self, factory):
        self._factory = factory

    def __iter__(self):
        return iter(self._factory())


class Sink:
    """Encoded UTF-8 chunks buffered into a binary handle.

    ``hasher`` and ``bytes`` are optional accumulation points: the batch
    staging path uses them to derive the artifact digest and byte count while
    the report is serialised, avoiding a full report buffer.
    """

    __slots__ = ('handle', 'hasher', 'bytes', '_pending', '_pending_size')

    def __init__(self, handle, hasher=None):
        self.handle = handle
        self.hasher = hasher
        self.bytes = 0
        self._pending = []
        self._pending_size = 0

    def write(self, text):
        pending = self._pending
        pending.append(text)
        self._pending_size += len(text)
        if self._pending_size >= _BUFFER_LIMIT:
            self.flush()

    def flush(self):
        if self._pending:
            data = ''.join(self._pending).encode('utf-8')
            self._pending.clear()
            self._pending_size = 0
            if self.hasher is not None:
                self.hasher.update(data)
            self.bytes += len(data)
            self.handle.write(data)

    def digest_value(self):
        return None if self.hasher is None else self.hasher.hexdigest()


class TextSink:
    """Fallback sink for an in-process text stdout without a byte buffer."""

    __slots__ = ('handle',)

    def __init__(self, handle):
        self.handle = handle

    def write(self, text):
        self.handle.write(text)

    def flush(self):
        self.handle.flush()


def _scalar(value):
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    kind = type(value)
    if kind is int:
        # Matches json.encoder's int fast path exactly.
        return int.__repr__(value)
    if kind is str:
        # Matches json.dumps(..., ensure_ascii=False).
        return encode_basestring(value)
    if kind is float:
        return json.dumps(value, allow_nan=False)
    raise TypeError('unsupported JSON value: %r' % (value,))


def _emit(sink, value, indent, level):
    if isinstance(value, Records):
        _emit_sequence(sink, value, indent, level, '[', ']')
    elif isinstance(value, dict):
        _emit_mapping(sink, value, indent, level)
    elif isinstance(value, (list, tuple)):
        _emit_sequence(sink, value, indent, level, '[', ']')
    else:
        sink.write(_scalar(value))


def _emit_sequence(sink, items, indent, level, opening, closing):
    iterator = iter(items)
    try:
        first = next(iterator)
    except StopIteration:
        sink.write(opening + closing)
        return
    if indent is None:
        sink.write(opening)
        _emit(sink, first, indent, level)
        for item in iterator:
            sink.write(', ')
            _emit(sink, item, indent, level)
        sink.write(closing)
        return
    pad = '\n' + _pad(level + 1)
    sink.write(opening + pad)
    _emit(sink, first, indent, level + 1)
    for item in iterator:
        sink.write(',' + pad)
        _emit(sink, item, indent, level + 1)
    sink.write('\n' + _pad(level) + closing)


def _emit_mapping(sink, mapping, indent, level):
    # sort_keys=True: ordering is part of the report byte contract.
    items = sorted(mapping.items())
    if not items:
        sink.write('{}')
        return
    if indent is None:
        sink.write('{')
        first = True
        for key, value in items:
            if not first:
                sink.write(', ')
            first = False
            sink.write(encode_basestring(key) + ': ')
            _emit(sink, value, indent, level)
        sink.write('}')
        return
    pad = '\n' + _pad(level + 1)
    sink.write('{' + pad)
    first = True
    for key, value in items:
        if not first:
            sink.write(',' + pad)
        first = False
        sink.write(encode_basestring(key) + ': ')
        _emit(sink, value, indent, level + 1)
    sink.write('\n' + _pad(level) + '}')


def write_document(sink, document, indent=None, newline=False):
    """Write one document to ``sink``; return the number of UTF-8 bytes."""
    _emit(sink, document, indent, 0)
    if newline:
        sink.write('\n')
    sink.flush()
    return sink.bytes


def chunked_digest(path):
    """Return ``(sha256_hexdigest, byte_count)`` reading in bounded chunks."""
    import hashlib
    hasher = hashlib.sha256()
    total = 0
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
            total += len(chunk)
    return hasher.hexdigest(), total
