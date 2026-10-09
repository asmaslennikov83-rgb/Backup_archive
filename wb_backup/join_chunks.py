"""Restore oversized documents after extracting all archive parts."""
from pathlib import Path
import sys


def join(root):
    for first in Path(root).rglob('*.chunk00001'):
        target = first.with_name(first.name[:-11])
        chunks = sorted(first.parent.glob(target.name + '.chunk[0-9][0-9][0-9][0-9][0-9]'))
        expected = [target.name + f'.chunk{i:05d}' for i in range(1, len(chunks) + 1)]
        if [p.name for p in chunks] != expected:
            raise ValueError('Пропущена часть: ' + target.name)
        with target.open('wb') as output:
            for p in chunks:
                with p.open('rb') as source:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        output.write(block)
        print(target)


if __name__ == '__main__':
    join(sys.argv[1] if len(sys.argv) > 1 else '.')
