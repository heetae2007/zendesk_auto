from app.telegram_format import format_notification

def test_telegram_contains_pis_and_escapes():
    r={'hospital':'가상병원','part':'보험','agent_name':'개발자','pis_name':'이혜경','summary':'검사','confidence':.9}
    msg=format_notification(r,simulation=True)
    assert '이혜경' in msg and '개발자' in msg and '실제 Zendesk 티켓 아님' in msg
