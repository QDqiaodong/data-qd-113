from .io import load
from .drainage import drain

def run(path,stream_threshold=None):
    return drain(load(path),stream_threshold)
