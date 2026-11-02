from io import BytesIO
from openpyxl import Workbook
from app.work_mapping import parse_workbook
from app.telegram_format import format_notification

def test_workbook_preserves_blank_and_multiple_people():
    wb=Workbook();s=wb.active
    s.append(['','메뉴','','담당자','1차백업','담당자','1차백업'])
    s.append(['','보험','심사','채희태','이정기','한다연/조한슬',''])
    s.append(['','','청구','','','이혜경','노주영'])
    buf=BytesIO();wb.save(buf)
    rows=parse_workbook(buf.getvalue())
    assert rows[0]['pis']=='한다연/조한슬'
    assert rows[1]=={'part':'보험','work':'청구','developer':'','developer_backup':'','pis':'이혜경','pis_backup':'노주영'}

def test_telegram_contains_pis_and_escapes():
    r={'hospital':'가상병원','part':'보험','agent_name':'개발자','pis_name':'이혜경','summary':'검사','confidence':.9}
    msg=format_notification(r,simulation=True)
    assert '이혜경' in msg and '개발자' in msg and '실제 Zendesk 티켓 아님' in msg
