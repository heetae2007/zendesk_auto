"""PIS weighted allocation preview. Does not store or change tickets."""
from decimal import Decimal, InvalidOperation


def preview(rows, count):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError('담당자를 1명 이상 100명 이하로 입력하세요.')
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 500:
        raise ValueError('모의 티켓 건수는 1~500이어야 합니다.')
    people, names = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('담당자 입력 형식이 올바르지 않습니다.')
        name = str(row.get('name', '')).strip()
        try:
            weight = Decimal(str(row.get('weight', '')))
        except InvalidOperation:
            raise ValueError('배정 비율은 0보다 큰 숫자여야 합니다.')
        assigned = row.get('assigned')
        if not name or name in names:
            raise ValueError('담당자 이름은 비어 있거나 중복될 수 없습니다.')
        if not weight.is_finite() or weight <= 0 or weight > 1000000:
            raise ValueError('배정 비율은 0보다 크고 1000000 이하여야 합니다.')
        if isinstance(assigned, bool) or not isinstance(assigned, int) or not 0 <= assigned <= 10000000:
            raise ValueError('기존 건수는 0 이상의 정수여야 합니다.')
        names.add(name)
        people.append({'name': name, 'weight': weight, 'initial': assigned, 'assigned': assigned})
    total_weight = sum(p['weight'] for p in people)
    sequence = []
    for _ in range(count):
        total = sum(p['assigned'] for p in people) + 1
        # Largest target-share deficit, with input order as a deterministic tie-break.
        chosen = max(people, key=lambda p: total * p['weight'] / total_weight - p['assigned'])
        chosen['assigned'] += 1
        sequence.append(chosen['name'])
    return {'sequence': sequence, 'summary': [
        {'name': p['name'], 'weight': str(p['weight']), 'initial': p['initial'],
         'added': p['assigned'] - p['initial'], 'total': p['assigned']} for p in people]}
