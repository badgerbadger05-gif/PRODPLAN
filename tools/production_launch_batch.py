"""Resumable API batch, using an accepted journal and explicit operator scope.

No stock/MRP/coverage calculations are performed here. Quantities and allowed
actions are copied from the persisted backend journal. The only selection
rule is the operator's minimum batch size and maximum number of orders.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.pending')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def select_rows(snapshot: dict, articles: list[str], workshop_id: int,
                minimum: float = 50, maximum_orders: int = 2) -> dict:
    selected, excluded = [], []
    for article in articles:
        candidates = [r for r in snapshot['rows'] if r.get('item_article') == article
                      and r.get('workshop_id') == workshop_id
                      and 'materialize' in r.get('available_actions', [])
                      and float(r.get('launchable_qty') or 0) > 0]
        candidates.sort(key=lambda r: (r.get('source_plan_period_from') or '',
                                      r.get('source_run_id') or 0,
                                      r.get('source_mrp_requirement_id') or 0,
                                      r['current_identity']))
        picked = []
        for row in candidates[:maximum_orders]:
            picked.append(row)
            if sum(float(r['launchable_qty']) for r in picked) >= minimum:
                break
        if sum(float(r['launchable_qty']) for r in picked) < minimum:
            excluded.append({'article': article, 'reason': 'minimum not met within order limit'})
        else:
            selected.extend(picked)
    return {'schema_version': 1, 'snapshot': snapshot['snapshot'],
            'selection': {'articles': articles, 'workshop_id': workshop_id,
                          'minimum': minimum, 'maximum_orders': maximum_orders},
            'rows': selected, 'excluded': excluded, 'completed_runs': {},
            'created': [], 'stages': {}}


def post(base: str, endpoint: str, payload: dict) -> dict:
    request = urllib.request.Request(base + '/api/v1/production-control' + endpoint,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'HTTP {exc.code}: {body}') from None


def read_current(base: str, output: Path, reader: str) -> dict:
    # Use the canonical revision-consistent reader, including its pagination
    # and accepted-truth checks. Never stitch independent journal pages.
    import subprocess
    subprocess.run([sys.executable, reader,
        '--base-url', base, '--endpoint', '/api/v1/production-control/orders',
        '--output', str(output)], check=True)
    return json.loads(output.read_text(encoding='utf-8'))


def launch(base: str, path: Path, state: dict) -> None:
    runs = sorted({r['source_run_id'] for r in state['rows']})
    for run in runs:
        if str(run) in state['completed_runs']:
            continue
        snapshot = read_current(base, path.with_name('current-journal.json'), state['snapshot_reader'])
        by_identity = {r['current_identity']: r for r in snapshot['rows']}
        original = [r for r in state['rows'] if r['source_run_id'] == run]
        current = [by_identity[r['current_identity']] for r in original]
        for before, now in zip(original, current):
            expected = float(before.get('materialized_order_qty') or 0)
            materialized = float(now.get('materialized_order_qty') or 0)
            quantity = float(before['launchable_qty'])
            if materialized not in (expected, expected + quantity):
                raise RuntimeError('materialization changed outside this batch')
            if materialized == expected and (float(now.get('launchable_qty') or 0) != quantity
                    or 'materialize' not in now.get('available_actions', [])):
                raise RuntimeError('launch quantity/actions changed; replan required')
        payload = {'work_items': [
            {'work_item_id': now['work_item_id'], 'launch_qty': before['launchable_qty'],
             'expected_materialized_qty': before.get('materialized_order_qty') or 0}
            for before, now in zip(original, current)],
            'current_identities': [r['current_identity'] for r in original],
            'expected_source_revision': snapshot['snapshot']['source_revision'],
            'require_workshop_routing': True, 'initiated_by': 'Codex: authorized production batch'}
        state['inflight'] = {'stage': 'launch', 'run': run, 'payload': payload}
        save(path, state)
        # An uncertain result retains the pre-command materialized quantities.
        # On resume, the server's expected-materialized guard recognizes the
        # exact replay; this never advances the expected quantity on a retry.
        result = post(base, '/orders/from-work-items', payload)
        state.pop('inflight', None)
        created = result.get('created', []) + result.get('reused', [])
        known = {r['product_id'] for r in state['created']}
        state['created'].extend(r for r in created if r['product_id'] not in known)
        state.setdefault('responses', []).append(result)
        succeeded = not (result.get('errors') or result.get('skipped'))
        if succeeded:
            state['completed_runs'][str(run)] = result
        save(path, state)
        if not succeeded:
            raise RuntimeError('batch partially applied; inspect saved response')


def export(base: str, path: Path, state: dict, dry_run: bool) -> None:
    if not state['created']:
        raise RuntimeError('no materialized products in this batch')
    snapshot = read_current(base, path.with_name('current-journal.json'), state['snapshot_reader'])
    ids = {r['product_id'] for r in state['created']}
    rows = [r for r in snapshot['rows'] if r.get('product_id') in ids]
    if {r['product_id'] for r in rows} != ids:
        raise RuntimeError('current journal omits batch products')
    payload = {'order_ids': sorted({r['order_id'] for r in rows}), 'dry_run': dry_run,
               'current_identities': [r['current_identity'] for r in rows],
               'expected_source_revision': snapshot['snapshot']['source_revision']}
    state['inflight'] = {'stage': 'export', 'payload': payload}
    save(path, state)
    result = post(base, '/orders/export-to-1c', payload)
    state['stages']['export_preview' if dry_run else 'export'] = result
    state.pop('inflight', None)
    save(path, state)
    print(json.dumps(result, ensure_ascii=True))


def product_context(base: str, path: Path, state: dict, product_ids: set | None = None) -> dict:
    snapshot = read_current(base, path.with_name('current-journal.json'), state['snapshot_reader'])
    ids = product_ids if product_ids is not None else {r['product_id'] for r in state['created']}
    rows = [r for r in snapshot['rows'] if r.get('product_id') in ids]
    if not ids or {r['product_id'] for r in rows} != ids:
        raise RuntimeError('current batch products unavailable')
    return {'product_ids': sorted(ids), 'current_identities': [r['current_identity'] for r in rows],
            'expected_source_revision': snapshot['snapshot']['source_revision']}


def prepare_materials(base: str, path: Path, state: dict) -> None:
    context = product_context(base, path, state)
    chains = post(base, '/orders/open-paint-weld-chains', {
        **context, 'initiated_by': 'Codex: authorized production batch'})
    state['stages']['chains'] = chains
    save(path, state)
    if set(chains.get('product_ids', [])) != set(context['product_ids']):
        raise RuntimeError('chain expanded outside the selected batch; inspect before proceeding')
    result = post(base, '/material-issues', {
        'product_ids': chains['product_ids'],
        'current_identities': chains['current_identities'],
        'expected_source_revision': chains['source_revision'],
        'initiated_by': 'Codex: authorized production batch'})
    state['stages']['materials'] = result
    save(path, state)
    print(json.dumps(result, ensure_ascii=True))
    if result.get('errors') or result.get('selection_required'):
        raise RuntimeError('material preparation requires resolution; see saved response')


def export_issues(base: str, path: Path, state: dict, dry_run: bool) -> None:
    materials = state['stages'].get('materials') or {}
    issues = [r for kind in ('created', 'reused') for r in materials.get(kind, [])]
    if not issues or materials.get('errors') or materials.get('selection_required'):
        raise RuntimeError('material issues are not ready')
    stage = 'issue_export_preview' if dry_run else 'issue_export'
    results = state['stages'].setdefault(stage, {})
    # Parent order export deliberately rejects mixed planning runs. Preserve
    # the same run boundary as materialization instead of weakening its guard.
    for run, materialized in state['completed_runs'].items():
        if (run in results and results[run].get('status') == 'ok'
                and results[run].get('issues_error') == 0
                and (results[run].get('parent_orders_export') or {}).get('orders_error') == 0):
            continue
        products = {r['product_id'] for kind in ('created', 'reused')
                    for r in materialized.get(kind, [])}
        ids = sorted({r['issue_id'] for r in issues if r['product_id'] in products})
        if not ids:
            raise RuntimeError('run has no prepared material issues')
        context = product_context(base, path, state, products)
        payload = {k:v for k,v in context.items() if k != 'product_ids'}
        payload.update(issue_ids=ids, dry_run=dry_run)
        state['inflight'] = {'stage': stage, 'run': run, 'payload': payload}
        save(path, state)
        result = post(base, '/material-issues/export-to-1c', payload)
        results[run] = result
        state.pop('inflight', None)
        save(path, state)
        print(json.dumps({'run': run, 'result': result}, ensure_ascii=True))
        if (result.get('status') != 'ok' or result.get('issues_error') != 0
                or (result.get('parent_orders_export') or {}).get('orders_error') != 0):
            raise RuntimeError('issue export partially applied; inspect saved response')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['plan', 'launch', 'prepare-materials',
        'issue-export-preview', 'issue-export', 'export-preview', 'export'])
    parser.add_argument('--state', type=Path, required=True)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--snapshot', type=Path)
    parser.add_argument('--articles', nargs='+')
    parser.add_argument('--workshop-id', type=int)
    parser.add_argument('--snapshot-reader', type=Path,
                        help='Canonical reader resolved by the read-access skill preflight')
    args = parser.parse_args()
    if args.phase == 'plan':
        if args.state.exists():
            raise RuntimeError('batch already exists; use a new state path to replan')
        state = select_rows(json.loads(args.snapshot.read_text(encoding='utf-8')),
                            args.articles, args.workshop_id)
        state['base_url'] = args.base_url.rstrip('/')
        reader = args.snapshot_reader or Path(__file__).with_name('prodplan_read_snapshot.py')
        if not reader.is_file():
            raise RuntimeError('provide --snapshot-reader from the active read-access route')
        state['snapshot_reader'] = str(reader.resolve())
        state['batch_digest'] = hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()
        save(args.state, state)
        print(json.dumps({'selected': len(state['rows']), 'excluded': state['excluded']}))
        return
    # Prevent overlapping local invocations. An abandoned lock requires explicit
    # inspection of the saved inflight command before removing it.
    lock = args.state.with_suffix('.lock')
    descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        state = json.loads(args.state.read_text(encoding='utf-8'))
        if state['base_url'] != args.base_url.rstrip('/'):
            raise RuntimeError('batch belongs to a different runtime')
        if args.phase == 'launch':
            launch(args.base_url, args.state, state)
        elif args.phase == 'prepare-materials':
            prepare_materials(args.base_url, args.state, state)
        elif args.phase in ('issue-export-preview', 'issue-export'):
            export_issues(args.base_url, args.state, state, args.phase == 'issue-export-preview')
        else:
            export(args.base_url, args.state, state, args.phase == 'export-preview')
    finally:
        os.close(descriptor)
        lock.unlink()


if __name__ == '__main__':
    main()
