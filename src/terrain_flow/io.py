import json, os, sys, tempfile
from pathlib import Path
from .streaming import Sink, TextSink, write_document

def pairs(items):
    result={}
    for k,v in items:
        if k in result: raise ValueError('duplicate JSON key: '+k)
        result[k]=v
    return result

def parse(raw):
    if len(raw)>8_000_000: raise ValueError('input exceeds 8 MB')
    def invalid(value): raise ValueError('non-finite JSON number: '+value)
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid)

def load(path):
    return parse(Path(path).read_bytes())

def serialize(result):
    return json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2,allow_nan=False)+'\n'

def fields(obj,names):
    if not isinstance(obj,dict) or set(obj)!=set(names.split()):
        raise ValueError('expected fields: '+names)

def text(v):
    if not isinstance(v,str) or not v.strip() or len(v)>256 or any(ord(c)<32 for c in v):
        raise ValueError('expected nonempty text, at most 256 characters')
    return v

def integer(v,low=0,high=10**9):
    if type(v)!=int or not low<=v<=high: raise ValueError('integer outside allowed bounds')
    return v

def array(v):
    if not isinstance(v,list) or len(v)>100000: raise ValueError('expected array of at most 100000 entries')
    return v

def publish(path,result):
    """Atomically publish a complete, already-materialised object."""
    path=Path(path)
    if path.is_symlink(): raise ValueError('output symlink is not allowed')
    path.parent.mkdir(parents=True,exist_ok=True)
    data=serialize(result)
    name=None
    try:
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=path.parent,delete=False) as f:
            name=f.name;f.write(data)
        os.replace(name,path)
    finally:
        if name and os.path.exists(name): os.unlink(name)

def _open_atomic(path):
    path=Path(path)
    if path.is_symlink(): raise ValueError('output symlink is not allowed')
    path.parent.mkdir(parents=True,exist_ok=True)
    descriptor,name=tempfile.mkstemp(prefix='.tmp-',dir=path.parent)
    return path,os.fdopen(descriptor,'wb'),name

def _discard(name):
    if name and os.path.exists(name): os.unlink(name)

def publish_document(path, document):
    """Stream one report document to ``path`` with a same-directory temp file.

    The pretty report format (``indent=2``, sorted keys, UTF-8, trailing
    newline) is byte-identical to :func:`serialize`.  The document may hold
    :class:`terrain_flow.streaming.Records` whose arrays are generated one
    record at a time, so neither the record list nor the report string is
    materialised.  The visible file is replaced only after the whole document
    has been written; on any failure the old file survives.
    """
    target,handle,name=_open_atomic(path)
    try:
        sink=Sink(handle)
        write_document(sink,document,indent=2,newline=True)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(name,target)
    except BaseException:
        handle.close()
        _discard(name)
        raise
    return sink.bytes

def stdout_sink():
    """Byte-buffered sink for stdout, falling back to the text layer."""
    stream=getattr(sys.stdout,'buffer',None)
    if stream is None:
        return TextSink(sys.stdout),None
    return Sink(stream),stream

def write_stdout(document):
    """Print the compact stdout form of a document (one trailing newline)."""
    sink,stream=stdout_sink()
    write_document(sink, document, indent=None, newline=True)
    if stream is not None: stream.flush()
    else: sys.stdout.flush()
