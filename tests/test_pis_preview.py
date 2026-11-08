import pytest
from app.pis_preview import preview


def test_weighted_sequence_and_existing_counts():
    rows = [{'name':'A', 'weight':2, 'assigned':0}, {'name':'B', 'weight':1, 'assigned':0}]
    result = preview(rows, 30)
    assert [x['added'] for x in result['summary']] == [20, 10]
    assert rows[0]['assigned'] == 0
    assert preview([{'name':'A', 'weight':1, 'assigned':4},
                    {'name':'B', 'weight':1, 'assigned':0}], 1)['sequence'] == ['B']

def test_30_30_40_without_knowing_future_volume():
    rows = [{'name': n, 'weight': w, 'assigned': 0}
            for n, w in [('A', 30), ('B', 30), ('C', 40)]]
    result = preview(rows, 10)
    assert [x['added'] for x in result['summary']] == [3, 3, 4]
    for _ in range(10):
        name = preview(rows, 1)['sequence'][0]
        next(row for row in rows if row['name'] == name)['assigned'] += 1
    assert [row['assigned'] for row in rows] == [3, 3, 4]


@pytest.mark.parametrize('rows,count', [
    ([], 1), ([{'name':'A','weight':'NaN','assigned':0}], 1),
    ([{'name':'A','weight':0,'assigned':0}], 1),
    ([{'name':'A','weight':1,'assigned':-1}], 1),
    ([{'name':'A','weight':1,'assigned':0}] * 2, 1),
    ([{'name':'A','weight':1,'assigned':0}], 501),
])
def test_invalid_inputs(rows, count):
    with pytest.raises(ValueError):
        preview(rows, count)
