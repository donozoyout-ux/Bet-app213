"""Scoped match-page statistics and events. External JavaScript is never executed."""
from html.parser import HTMLParser
from dataclasses import dataclass, field
import hashlib
import json
import re
from .client import SourceError

FIELDS = {'Corner Kicks':'corners','Corner Kicks(HT)':'corners_ht','Yellow Cards':'yellow_cards',
          'Red Cards':'red_cards','Shots':'shots','Shots On Goal':'shots_on_target','Fouls':'fouls',
          'Offsides':'offsides','Possession':'possession'}
STAT_FIELDS = [side+'_'+name for name in FIELDS.values() for side in ('home','away')]
EVENT_TYPES = {'1':'goal','2':'red_card','3':'yellow_card','7':'penalty','8':'own_goal',
               '9':'second_yellow_red','11':'substitution','13':'penalty','14':'var','30':'penalty'}


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    def walk(self,tag=None):
        for child in self.children:
            if isinstance(child,Node):
                if tag is None or child.tag==tag:yield child
                yield from child.walk(tag)
    def text(self):
        return ' '.join(''.join(child.text() if isinstance(child,Node) else child for child in self.children).split())


class Page(HTMLParser):
    def __init__(self,source):
        super().__init__(convert_charrefs=True);self.root=Node('root');self.stack=[self.root];self.feed(source)
    def handle_starttag(self,tag,attrs):
        node=Node(tag,dict(attrs));self.stack[-1].children.append(node)
        if tag not in {'img','br','meta','link','input','hr','source','wbr'}:self.stack.append(node)
    def handle_startendtag(self,tag,attrs):self.handle_starttag(tag,attrs);self.handle_endtag(tag)
    def handle_endtag(self,tag):
        for index in range(len(self.stack)-1,0,-1):
            if self.stack[index].tag==tag:self.stack=self.stack[:index];break
    def handle_data(self,data):self.stack[-1].children.append(data)
    def by_id(self,id):return next((node for node in self.root.walk() if node.attrs.get('id')==id),None)


def value(text,possession=False):
    text=text.strip().removesuffix('%').strip()
    if not re.fullmatch(r'\d+(?:\.\d+)?',text):return None
    number=float(text)
    if possession:return number if 0<=number<=100 else None
    return int(number) if number.is_integer() and number<=2147483647 else None


def parse_statistics(source,match_id,league_id=None):
    identities={key:re.search(r'\b'+key+r"\s*:\s*parseInt\(['\"](-?\d+)['\"]\)",source) for key in ('sId','sclassId','state')}
    if not identities['sId'] or int(identities['sId'][1])!=match_id:
        raise SourceError('Statistics page match identity missing or mismatched')
    if league_id is not None and (not identities['sclassId'] or int(identities['sclassId'][1])!=league_id):
        raise SourceError('Statistics competition identity mismatch')
    page=Page(source);stats={key:None for key in STAT_FIELDS};raw_rows=[]
    ft=page.by_id('ftstat')
    if ft:
        for row in ft.walk('li'):
            title=next((node.text() for node in row.walk('span') if 'stat-title' in node.attrs.get('class','').split()),None)
            cells=[node.text() for node in row.walk('span') if 'stat-c' in node.attrs.get('class','').split()]
            if title and len(cells)==2:
                raw_rows.append({'label':title,'home':cells[0],'away':cells[1]})
                if title in FIELDS:
                    for side,text in zip(('home','away'),cells):stats[side+'_'+FIELDS[title]]=value(text,title=='Possession')
    ht=page.by_id('hf1stat')
    if ht:
        for row in ht.walk('li'):
            title=next((node.text() for node in row.walk('span') if node.attrs.get('class')=='stat-title'),None)
            cells=[node.text() for node in row.walk('span') if node.attrs.get('class')=='stat-c']
            if title=='Corner Kicks' and len(cells)==2:
                for side,text in zip(('home','away'),cells):
                    if stats[side+'_corners_ht'] is None:stats[side+'_corners_ht']=value(text)
    table=page.by_id('eventsTable');events=[];occurrences={};phase=None
    if table:
        for row in table.walk('tr'):
            cells=[child for child in row.children if isinstance(child,Node) and child.tag=='td']
            if 'stage-tit' in row.attrs.get('class',''):
                phase=row.text();continue
            if len(cells)!=5:continue
            minute_text=cells[2].text();minute=re.fullmatch(r"(\d+)(?:\+(\d+))?\s*['′]?",minute_text)
            for side,icon_cell,player_cell in [('home',cells[1],cells[0]),('away',cells[3],cells[4])]:
                icon=next((re.search(r'/bf_img/(\d+)\.png',node.attrs.get('src','')) for node in icon_cell.walk('img') if '/bf_img/' in node.attrs.get('src','')),None)
                if not icon:continue
                code=icon[1];label=next((node.attrs.get('alt') for node in icon_cell.walk('img')),None)
                # Corner/other event codes are accepted only when their actual label says so.
                event_type=EVENT_TYPES.get(code,'corner' if label in {'Corner','Corner Kick'} else 'unknown')
                players=[node.text() for node in player_cell.walk('a') if '/team/player/' in node.attrs.get('href','')]
                raw={'code':code,'label':label,'minute':minute_text,'phase':phase,'players':players,'text':player_cell.text()}
                canonical=json.dumps(raw,sort_keys=True,ensure_ascii=False)+'|'+side
                occurrence=occurrences.get(canonical,0);occurrences[canonical]=occurrence+1
                key=hashlib.sha256((canonical+'|'+str(occurrence)).encode()).hexdigest()
                events.append({'source_key':key,'minute':int(minute[1]) if minute else None,'stoppage_minute':int(minute[2]) if minute and minute[2] else None,
                               'team_side':side,'event_type':event_type,'player_name':players[0] if players else None,
                               'secondary_player_name':players[1] if len(players)>1 else None,'raw':raw})
    # Neither the six live/analysis sources nor their metadata expose a verified referee.
    # Do not infer one from unrelated names, commentary, ads or a match's country.
    return {'statistics':stats,'events':events,'events_available':table is not None,'referee':None,
            'is_final':bool(identities['state'] and int(identities['state'][1])==-1),
            'collection_status':'available' if any(v is not None for v in stats.values()) else 'unavailable',
            'raw':{'match_id':match_id,'competition_id':int(identities['sclassId'][1]) if identities['sclassId'] else None,'rows':raw_rows,'endpoint':f'https://www.goaloo.com/match/live-{match_id}'}}


async def fetch_statistics(client,match_id,league_id):
    source=await client.get_text(f'https://www.goaloo.com/match/live-{match_id}')
    return parse_statistics(source,match_id,league_id)
