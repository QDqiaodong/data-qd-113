DESCRIPTION='Strict-descent D8 drainage accumulation and catchment delineation'
import argparse, json, sys
from pathlib import Path
from .io import publish
from .pipeline import run
from .lake import (BatchError, ingest, rebuild_catalog, verify_lake)
from .connectivity import run as run_connectivity

COMMANDS=('batch','verify','rebuild-catalog','connectivity')

def positive(text):
    try: value=int(text)
    except ValueError: raise argparse.ArgumentTypeError('expected a positive integer')
    if value<1: raise argparse.ArgumentTypeError('expected a positive integer')
    return value

def single(argv):
    p=argparse.ArgumentParser(description=DESCRIPTION)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--stream-threshold',type=positive,metavar='N',
                   help='append a threshold stream network for upstream_cells >= N')
    a=p.parse_args(argv)
    try:
        if a.source.resolve()==a.output.resolve(): raise ValueError('source and output must differ')
        result=run(a.source,a.stream_threshold);publish(a.output,result)
    except (ValueError,OSError,KeyError,TypeError,OverflowError,RecursionError) as e:
        print(str(e),file=sys.stderr);return 2
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,allow_nan=False));return 0

def batch(argv):
    p=argparse.ArgumentParser(prog='terrain-flow batch',
                              description='Ingest a manifest of terrain sources into a partitioned lake.')
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--lake',type=Path,required=True)
    p.add_argument('--result',type=Path,
                   help='also write the batch summary to this path atomically')
    a=p.parse_args(argv)
    try:
        summary=ingest(a.manifest,a.lake)
        if a.result is not None:publish(a.result,summary)
    except (BatchError,ValueError,OSError,KeyError,TypeError,OverflowError,RecursionError) as e:
        print(str(e),file=sys.stderr);return 2
    print(json.dumps(summary,ensure_ascii=False,sort_keys=True,allow_nan=False))
    return 1 if summary['counts']['failed'] else 0

def verify(argv):
    p=argparse.ArgumentParser(prog='terrain-flow verify',
                              description='Verify lake artifacts and catalog consistency.')
    p.add_argument('--lake',type=Path,required=True)
    a=p.parse_args(argv)
    try: result=verify_lake(a.lake)
    except (BatchError,ValueError,OSError) as e:
        print(str(e),file=sys.stderr);return 2
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,allow_nan=False))
    return 0 if result['ok'] else 1

def rebuild(argv):
    p=argparse.ArgumentParser(prog='terrain-flow rebuild-catalog',
                              description='Rebuild catalog.json from committed partitions.')
    p.add_argument('--lake',type=Path,required=True)
    a=p.parse_args(argv)
    try: result=rebuild_catalog(a.lake)
    except (BatchError,ValueError,OSError) as e:
        print(str(e),file=sys.stderr);return 2
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,allow_nan=False))
    return 0

def connectivity(argv):
    p=argparse.ArgumentParser(prog='terrain-flow connectivity',
                              description='Build the rising-water merge-forest index and answer level queries.')
    p.add_argument('--grid',type=Path,required=True,help='existing terrain JSON (cell_size_m, elevations)')
    p.add_argument('--queries',type=Path,required=True,
                   help='JSON document {"queries":[{"row","col","level"}, ...]}')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args(argv)
    try:
        if a.grid.resolve()==a.output.resolve() or a.queries.resolve()==a.output.resolve():
            raise ValueError('output must differ from the input files')
        result=run_connectivity(a.grid,a.queries);publish(a.output,result)
    except (ValueError,OSError,KeyError,TypeError,OverflowError,RecursionError) as e:
        print(str(e),file=sys.stderr);return 2
    print(json.dumps(result,ensure_ascii=False,sort_keys=True,allow_nan=False))
    return 0

def main(argv=None):
    argv=sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in COMMANDS:
        command=argv.pop(0)
        return {'batch':batch,'verify':verify,'rebuild-catalog':rebuild,
                'connectivity':connectivity}[command](argv)
    return single(argv)

if __name__=='__main__': raise SystemExit(main())
