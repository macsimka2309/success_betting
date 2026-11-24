"""Parse sports-widget HTML only; never accepts an account page or infers goal minutes."""
from html.parser import HTMLParser
import re


class Node:
    def __init__(self,tag='',attrs=()):
        self.tag=tag;self.attrs=dict(attrs);self.children=[];self.parts=[]
    @property
    def text(self):return ''.join(p.text if isinstance(p,Node) else p for p in self.parts)
    def find(self,prefix):
        return [n for n in self.walk() if any(c.startswith(prefix+'--') for c in n.attrs.get('class','').split())]
    def walk(self):
        for n in self.children:
            yield n;yield from n.walk()


class SportsHTML(HTMLParser):
    def __init__(self,html):
        super().__init__();self.root=Node();self.stack=[self.root];self.feed(html)
    def handle_starttag(self,tag,attrs):
        n=Node(tag,attrs);self.stack[-1].children.append(n);self.stack[-1].parts.append(n)
        if tag not in ('img','br','hr','input','path','meta','link'):self.stack.append(n)
    def handle_endtag(self,tag):
        for i in range(len(self.stack)-1,0,-1):
            if self.stack[i].tag==tag:self.stack=self.stack[:i];break
    def handle_data(self,data):self.stack[-1].parts.append(data)


def parse_timeline(html):
    if not isinstance(html,str) or len(html)>500000:return {'reason':'timeline_unavailable','goals':[]}
    root=SportsHTML(html).root
    timelines=root.find('timeline')
    if len(timelines)!=1:return {'reason':'timeline_unavailable','goals':[]}
    root=timelines[0];goals=[];period='unknown';started=False;second=False
    for row in reversed(list(root.walk())):
        cls=row.attrs.get('class','')
        if any(c.startswith('row-period--') for c in cls.split()):
            label=row.text.casefold()
            if 'доп' in label:period='extra_time'
            elif 'пенальти' in label:period='shootout'
            elif re.search(r'1[-\s]*й\s*тайм',label):period='regulation_1h';started=True
            elif re.search(r'2[-\s]*й\s*тайм',label):period='regulation_2h';second=True
        if not any(c.startswith('item--') for c in cls.split()):continue
        center=row.find('center');teams=row.find('team')
        if len(center)!=1 or len(teams)!=2:continue
        clock=re.fullmatch(r"\s*(\d{1,3})(?:\s*\+\s*(\d+))?['’′]?\s*",center[0].text)
        for side,team in zip(('home','away'),teams):
            areas=team.find('text-area')
            text=''.join(n.text for n in areas).strip()
            score=re.fullmatch(r'(Гол|Пенальти забит|Автогол)\s*\((\d+):(\d+)\)',text)
            if not score:continue
            goals.append(dict(credited_side=side,minute=int(clock[1]) if clock else None,
              added_minute=int(clock[2]) if clock and clock[2] else None,period='unknown' if clock and (int(clock[1])>90 and period=='regulation_2h' or int(clock[1])>45 and period=='regulation_1h') else period,
              score_after_home=int(score[2]),score_after_away=int(score[3]),detail=score[1]))
    headers=root.find('header_team')
    score=[n.find('score') for n in headers]
    final=[int(n[0].text) if len(n)==1 and n[0].text.isdigit() else None for n in score]
    reason=None if started and second and len(final)==2 else 'timeline_incomplete'
    h=a=0
    for goal in goals:
        if goal['period'] not in ('regulation_1h','regulation_2h'):continue
        h+=goal['credited_side']=='home';a+=goal['credited_side']=='away'
        if (h,a)!=(goal['score_after_home'],goal['score_after_away']) or goal['minute'] is None:reason='timeline_incomplete'
    if any(g['period']=='unknown' for g in goals):reason='timeline_incomplete'
    regulation_reason=reason
    if final!=[h,a]:reason='timeline_incomplete'
    teams=[n.find('team-name') for n in headers]
    sides=[n[0].text.strip() if len(n)==1 else None for n in teams]
    return dict(goals=goals,score=final,reason=reason,regulation_score=[h,a],
                regulation_reason=regulation_reason,teams=sides)


def completion_evidence(html,event_id):
    """Only the verified selected Completed filter and an identity-matched row."""
    if not isinstance(html,str) or len(html)>500000:return None
    root=SportsHTML(html).root
    selected=[n for n in root.find('filter-component-sport-mode__item')
              if n.text.strip()=='Завершенные' and any(c.startswith('_selected--') for c in n.attrs.get('class','').split())]
    matches=[]
    for row in root.find('results-event'):
        links=[n for n in row.walk() if n.tag=='a' and re.fullmatch(r'/results/football/\d+/'+re.escape(str(event_id))+r'/?',n.attrs.get('href',''))]
        if len(links)==1:matches.append(row)
    if len(selected)!=1 or len(matches)!=1:return None
    status_nodes=matches[0].find('results-event-time')+matches[0].find('results-scoreBoard')
    text=' '.join(part for n in status_nodes for child in [n,*n.walk()] for part in child.parts if isinstance(part,str)).casefold()
    status='CANC' if re.search(r'\bотмен[её]н\b',text) else 'ABD' if re.search(r'\bпрерван\b',text) else 'REG_END'
    row=matches[0]
    evidence={'status':status,'source_event_id':str(event_id),'source':'fonbet_completed_results','label':'Завершенные'}
    columns=root.find('score-caption')
    labels=[column.text.strip() or column.attrs.get('title','').strip() for column in columns]
    blocks=row.find('results-scoreBlock')
    # The clock block may not have a column caption.
    if len(blocks)==len(labels)+1:
        first=[n.text.strip() for n in blocks[0].find('results-scoreBlock__score')]
        if len(first)==2 and first[0]=='' and re.fullmatch(r"\d{1,3}(?:\+\d+)?['’′]",first[1]):
            blocks=blocks[1:]
    teams=[n.text.strip() for n in row.find('results-event-team__caption')]
    if (status!='REG_END' or len(blocks)!=len(labels) or labels.count('1')!=1
            or labels.count('2')!=1 or labels.index('1')>=labels.index('2')
            or len(teams)!=2 or not all(teams)):
        return evidence
    halves=[[n.text.strip() for n in blocks[labels.index(label)].find('results-scoreBlock__score')]
            for label in ('1','2')]
    if all(len(half)==2 and all(re.fullmatch(r'[0-9]{1,2}',v) for v in half) for half in halves):
        evidence.update(regulation_score=[sum(map(int,side)) for side in zip(*halves)],teams=teams)
    return evidence
