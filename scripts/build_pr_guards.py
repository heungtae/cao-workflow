"""Embed the shared optional automation gate in standalone CAO deployments."""
import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(check=False):
    guard = (ROOT / 'workflows/_pr/guard.py').read_text().rstrip()
    for name in ('github-pr-review', 'github-pr-apply'):
        path = ROOT / 'workflows' / name / 'workflow.py'
        source = path.read_text()
        if '# BEGIN GENERATED PR GUARD' in source:
            result = re.sub(r'# BEGIN GENERATED PR GUARD.*?# END GENERATED PR GUARD', lambda _: guard, source, flags=re.S)
        else:
            result = source.replace('\ndef main() -> None:', '\n' + guard + '\n\n\ndef main() -> None:')
        if check:
            if result != source:
                raise ValueError('Run python3 scripts/build_pr_guards.py')
        else:
            path.write_text(result)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    build(parser.parse_args().check)
