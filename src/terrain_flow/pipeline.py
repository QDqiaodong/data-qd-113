from .io import load
from .drainage import analyze, drain

def run(path,stream_threshold=None):
    """Object API: load a terrain file and return the complete report."""
    return drain(load(path),stream_threshold)

def prepare(path,stream_threshold=None):
    """Bounded API: load a terrain file and return compact :class:`Analysis`.

    The returned state exposes ``document()`` for streamed serialisation and
    ``report()`` for the historical complete-dictionary form.  It is the
    single-file CLI production entry point.
    """
    return analyze(load(path),stream_threshold)
