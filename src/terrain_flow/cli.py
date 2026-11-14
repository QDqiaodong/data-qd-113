DESCRIPTION='Strict-descent D8 drainage accumulation and catchment delineation'
import argparse, json, sys
from pathlib import Path
from .io import publish
from .pipeline import run

def positive(text):
    try: value=int(text)
    except ValueError: raise argparse.ArgumentTypeError('expected a positive integer')
    if value<1: raise argparse.ArgumentTypeError('expected a positive integer')
    return value

def main():
    p=argparse.ArgumentParser(description=DESCRIPTION)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--stream-threshold',type=positive,metavar='N',
                   help='append a threshold stream network for upstream_cells >= N')
    a=p.parse_args()
    try:
        if a.source.resolve()==a.output.resolve(): raise ValueError('source and output must differ')
        result=run(a.source,a.stream_threshold);publish(a.output,result)
    except (ValueError,OSError,KeyError,TypeError,OverflowError,RecursionError) as e:
        print(str(e),file=sys.stderr);return 2
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,allow_nan=False));return 0

if __name__=='__main__': raise SystemExit(main())
