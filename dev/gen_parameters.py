"""
gen_parameters.py — regenerate source/Parameters.h/.cpp of a plugin project from its parameters.json.

    python dev/gen_parameters.py <project dir>            # reads <project>/parameters.json
    python dev/gen_parameters.py <spec.json> <out dir>

Same generator as the PluginBuilder COMP's "Generate Parameters" pulse (source/ParamGen.py). Files whose
content would not change are left untouched, so an unchanged spec does not trigger a rebuild.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'source'))

import ParamGen  # noqa: E402


def main(argv):
    if len(argv) == 2:
        spec, out = os.path.join(argv[1], 'parameters.json'), os.path.join(argv[1], 'source')
    elif len(argv) == 3:
        spec, out = argv[1], argv[2]
    else:
        print(__doc__)
        return 2
    try:
        h, c = ParamGen.generate_files(spec, out)
    except ParamGen.SpecError as e:
        print(f'parameters.json: {e}')
        return 1
    print(f'wrote {h}\n      {c}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
