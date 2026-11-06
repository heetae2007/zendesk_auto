from app.telegram_format import format_notification

def test_telegram_contains_pis_and_escapes():
    r={'hospital':'가상병원','part':'보험','agent_name':'개발자','pis_name':'이혜경','summary':'검사','confidence':.9}
    msg=format_notification(r,simulation=True)
    assert '이혜경' in msg and '개발자' in msg and '실제 Zendesk 티켓 아님' in msg


def test_telegram_names_each_person_once_under_the_heavier_part():
    def heading(entries,**extra):
        r={'hospital':'H','recommended_assignments':entries,'summary':'s','confidence':.9,**extra}
        return format_notification(r,simulation=True).split('\n')[1]
    a=lambda n:{'agent_name':n,'agent_id':n}
    P,C='PRIMARY','COLLABORATOR'
    assert heading([{'part':'보험','role':P,'agents':[a('김종오')]},{'part':'원무','role':C,'agents':[a('김종오')]}])=='[H] 보험 김종오 책임님'
    assert heading([{'part':'보험','role':C,'agents':[a('김종오')]},{'part':'원무','role':P,'agents':[a('김종오')]}])=='[H] 원무 김종오 책임님'
    assert heading([{'part':'보험','agents':[a('김종오')]},{'part':'원무','agents':[a('김종오')]}])=='[H] 보험 김종오 책임님'  # same weight: the first part
    assert heading([{'part':'보험','role':P,'agents':[a('김종오')]},{'part':'원무','role':C,'agents':[a('김종오'),a('이선주')]}],pis_name='이혜경')=='[H] 보험 김종오 책임님 / 원무 이선주 책임님 / PIS 이혜경 책임님'
    assert heading([{'part':'보험','role':P,'agents':[a('김종오'),a('이선주')]},{'part':'원무','role':C,'agents':[a('이선주'),a('김종오')]}])=='[H] 보험 김종오 책임님, 이선주 책임님'
    assert heading([{'part':'보험','agents':[a('김종오'),a('김종오')]}])=='[H] 보험 김종오 책임님'
    assert heading([{'part':'보험','agents':[]},{'part':'원무','agents':[]}])=='[H] 보험 담당자 미정 / 원무 담당자 미정'
