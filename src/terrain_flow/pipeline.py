from .io import load
from .drainage import drain

def run(path):
    return drain(load(path))
