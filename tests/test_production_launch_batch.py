from tools.production_launch_batch import select_rows
from tools import production_launch_batch as batch


def row(number, quantity, *, workshop=7):
    return {'item_article': 'CP-A', 'workshop_id': workshop,
            'available_actions': ['materialize'], 'launchable_qty': quantity,
            'source_run_id': number, 'source_plan_period_from': '2026-10-01',
            'source_mrp_requirement_id': number, 'current_identity': str(number)}


def select(rows):
    return select_rows({'snapshot': {}, 'rows': rows}, ['CP-A'], 7)


def test_operator_minimum_keeps_whole_orders_and_stops_at_two():
    result = select([row(3, 200), row(2, 210), row(1, 36)])
    assert [r['launchable_qty'] for r in result['rows']] == [36, 210]


def test_one_sufficient_order_does_not_add_another_or_other_workshop():
    result = select([row(0, 100, workshop=8), row(2, 40), row(1, 58)])
    assert [r['current_identity'] for r in result['rows']] == ['1']


def test_two_insufficient_orders_do_not_pull_a_third():
    result = select([row(1, 1), row(2, 20), row(3, 90)])
    assert result['rows'] == []
    assert result['excluded'][0]['reason'] == 'minimum not met within order limit'


def test_issue_export_preserves_run_boundary_and_resumes_successful_runs(tmp_path, monkeypatch):
    state = {'snapshot_reader': 'resolved-reader.py', 'created': [{'product_id': 1}, {'product_id': 2}],
             'completed_runs': {'620': {'created': [{'product_id': 1}]},
                                '621': {'created': [{'product_id': 2}]}},
             'stages': {'materials': {'created': [{'product_id': 1, 'issue_id': 11},
                                                 {'product_id': 2, 'issue_id': 22}]}}}
    snapshot = {'snapshot': {'source_revision': 'current'},
                'rows': [{'product_id': p, 'current_identity': str(p)} for p in (1, 2)]}
    monkeypatch.setattr(batch, 'read_current', lambda *args: snapshot)
    calls = []

    def post(base, endpoint, payload):
        assert payload['issue_ids'] in ([11], [22]), 'mixed-run export must never be submitted'
        calls.append(payload)
        return {'status': 'ok', 'parent_orders_export': {'orders_error': 0}}

    monkeypatch.setattr(batch, 'post', post)
    path = tmp_path / 'batch.json'
    batch.export_issues('http://resolved-api', path, state, False)
    batch.export_issues('http://resolved-api', path, state, False)
    assert len(calls) == 2
    assert calls[0]['current_identities'] == ['1']
    assert calls[1]['current_identities'] == ['2']
