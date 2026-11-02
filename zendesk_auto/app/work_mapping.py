"""Import explicit historical menu assignments for review; never infer missing names."""
from io import BytesIO
from openpyxl import load_workbook


def parse_workbook(data):
    wb = load_workbook(BytesIO(data), read_only=True, data_only=True)
    rows = []
    try:
        for sheet in wb:
            current = ''
            header = False
            for values in sheet.iter_rows(values_only=True, max_col=7):
                v = [str(x).strip() if x is not None else '' for x in values]
                if len(v) < 7:
                    v.extend([''] * (7-len(v)))
                if v[1] == '메뉴' and v[3] == '담당자':
                    header = True
                    continue
                if not header:
                    continue
                if v[1]:
                    current = v[1]
                if current and v[2]:
                    rows.append({'part': current, 'work': v[2], 'developer': v[3],
                                 'developer_backup': v[4], 'pis': v[5], 'pis_backup': v[6]})
        if not rows:
            raise ValueError('파트·세부업무 헤더 또는 데이터가 없습니다.')
        return rows
    finally:
        wb.close()
