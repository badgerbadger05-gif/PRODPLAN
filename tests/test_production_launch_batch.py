from tools.production_launch_batch import select_rows


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
