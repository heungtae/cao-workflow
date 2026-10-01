#!/usr/bin/env python3
"""Build self-contained deployment resources from repository-owned fragments."""
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(check=False):
    common = (ROOT / 'workflows/_incident/common.py').read_text()
    for name, fragment in [('mcp-exception-issue', 'monitor.py'), ('github-issue-fix', 'fix.py')]:
        output = ROOT / 'workflows' / name / 'workflow.py'
        body = common + '\n\n' + (ROOT / 'workflows/_incident' / fragment).read_text()
        if check:
            if not output.exists() or output.read_text() != body:
                raise ValueError(f'Regenerate {output} with scripts/build_incident_workflows.py')
        else:
            output.write_text(body)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    build(parser.parse_args().check)
