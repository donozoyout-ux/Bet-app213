"""Versioned, bounded archive framing. No network or database writes."""
import gzip
import hashlib
import json
import math
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from src.models import League, Team, Referee, Match, MatchStatistics, Odds1X2, AsianHandicap, AsianTotals

BOOKS = {3:'Crown',8:'Bet365',31:'Sbobet'}
MARKETS = {'1x2':Odds1X2,'ah':AsianHandicap,'ou':AsianTotals}
MAX_ROWS=5000
MAX_LINE=2*1024*1024
MAX_PAYLOAD=128*1024*1024
MAX_ASSET=64*1024*1024  # Application limit, deliberately below platform limits.


def columns(model, exclude=()):
    return tuple(c.name for c in model.__table__.columns if c.name not in {'id',*exclude})


LEAGUE_FIELDS=columns(League)
TEAM_FIELDS=columns(Team)
REFEREE_FIELDS=columns(Referee)
MATCH_FIELDS=columns(Match,('league_id','season_id','home_team_id','away_team_id','referee_id'))
STAT_FIELDS=columns(MatchStatistics,('match_id',))
QUOTE_FIELDS={k:columns(m,('match_id','bookmaker_id')) for k,m in MARKETS.items()}
ROOT={'league','season','teams','referee','match','stats','bookmakers','odds'}
MANIFEST_KEYS={'schema_version','file','sha256','payload_sha256','match_count','odds_count',
               'uncompressed_bytes','league','season','format','contains_personal_account_data'}


def normalized(value):
    return value.astimezone(timezone.utc).isoformat() if isinstance(value,datetime) else value


def encoded(row):
    return json.dumps(row,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=normalized,allow_nan=False)


def partition(league,season):
    if type(league) is not int or league not in (6,7) or not isinstance(season,str) or not re.fullmatch(r'\d{4}-\d{4}',season):
        raise ValueError('Invalid archive partition')
    start,end=map(int,season.split('-'))
    if start<2023 or end!=start+1:
        raise ValueError('Invalid archive season')


def exact(value,keys):
    if not isinstance(value,dict) or set(value)!=set(keys):
        raise ValueError('Archive schema or linked record mismatch')


def positive(value):
    if type(value) is not int or value<=0:
        raise ValueError('Invalid archive identity')


def validate(row,league,season):
    exact(row,ROOT)
    exact(row['league'],LEAGUE_FIELDS)
    exact(row['season'],{'season_name'})
    exact(row['match'],set(MATCH_FIELDS)|{'home_external_id','away_external_id'})
    if row['league']['external_id']!=league or row['season']['season_name']!=season:
        raise ValueError('Archive partition mismatch')
    positive(row['league']['external_id'])
    match=row['match'];positive(match['external_match_id'])
    positive(match['home_external_id']);positive(match['away_external_id'])
    if match['status']!='finished':
        raise ValueError('Only finished fixtures can be archived')
    try:
        kickoff=datetime.fromisoformat(match['kickoff_at'])
        if kickoff.tzinfo is None or kickoff<datetime(2024,1,1,tzinfo=timezone.utc):
            raise ValueError()
    except (TypeError,ValueError):
        raise ValueError('Invalid archive kickoff') from None
    for side in ('ft_home','ft_away','ht_home','ht_away'):
        if match[side] is not None and (type(match[side]) is not int or match[side]<0):
            raise ValueError('Invalid score')
    if not isinstance(row['teams'],list) or len(row['teams'])!=2:
        raise ValueError('Missing linked teams')
    for team in row['teams']:
        exact(team,TEAM_FIELDS);positive(team['external_id'])
        if not isinstance(team['name'],str) or not team['name'].strip():raise ValueError('Invalid team identity')
    if {t['external_id'] for t in row['teams']}!={match['home_external_id'],match['away_external_id']} or match['home_external_id']==match['away_external_id']:
        raise ValueError('Invalid fixture team identity')
    if row['referee'] is not None:
        exact(row['referee'],REFEREE_FIELDS)
    if row['stats'] is not None:
        exact(row['stats'],STAT_FIELDS)
    if not isinstance(row['bookmakers'],list) or sorted(row['bookmakers'],key=lambda b:b.get('external_id',0))!=[
            {'external_id':i,'name':name} for i,name in sorted(BOOKS.items())]:
        raise ValueError('Missing or invalid linked bookmakers')
    if not isinstance(row['odds'],list) or len(row['odds'])>9:
        raise ValueError('Invalid quote count')
    seen=set()
    for quote in row['odds']:
        market=quote.get('market');book=quote.get('bookmaker_external_id')
        if market not in MARKETS or book not in BOOKS:
            raise ValueError('Unknown quote bookmaker/market')
        exact(quote,set(QUOTE_FIELDS[market])|{'market','bookmaker_external_id'})
        if (market,book) in seen:
            raise ValueError('Duplicate quote identity')
        seen.add((market,book))
        for key,value in quote.items():
            if key.startswith(('opening_','closing_','latest_')) and value is not None:
                if type(value) not in (int,float) or not math.isfinite(value) or (not key.endswith('_line') and value<=1):
                    raise ValueError('Invalid quote number')


def sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(512*1024),b''):
            digest.update(block)
    return digest.hexdigest()


class Writer:
    def __init__(self,output):
        self.output=Path(output)
        self.manifest_path=self.output.with_suffix(self.output.suffix+'.manifest.json')
        self.count=self.quotes=self.size=self.last=0
        self.digest=hashlib.sha256()
        self.league=self.season=None

    def __enter__(self):
        self.output.parent.mkdir(parents=True,exist_ok=True)
        if self.output.exists() or self.manifest_path.exists():
            raise ValueError('Refusing to overwrite archive evidence')
        handle=tempfile.NamedTemporaryFile(dir=self.output.parent,prefix='.archive-',delete=False)
        self.partial=Path(handle.name);self.handle=handle
        self.zipped=gzip.GzipFile(filename='',mode='wb',compresslevel=9,mtime=0,fileobj=handle)
        return self

    def add(self,row):
        if self.count==0:
            self.league=row['league']['external_id'];self.season=row['season']['season_name']
            partition(self.league,self.season)
        validate(row,self.league,self.season)
        mid=row['match']['external_match_id']
        if mid<=self.last:
            raise ValueError('Duplicate or unordered source match IDs')
        line=(encoded(row)+'\n').encode('utf-8')
        self.size+=len(line)
        if len(line)>MAX_LINE or self.size>MAX_PAYLOAD or self.count>=MAX_ROWS:
            raise ValueError('Archive streaming limit exceeded')
        self.zipped.write(line);self.digest.update(line)
        self.last=mid;self.count+=1;self.quotes+=len(row['odds'])

    def finish(self):
        self.zipped.close();self.handle.close()
        if not self.count or self.partial.stat().st_size>MAX_ASSET:
            raise ValueError('Empty or oversized archive')
        manifest=dict(schema_version=2,file=self.output.name,sha256=sha256(self.partial),
            payload_sha256=self.digest.hexdigest(),match_count=self.count,odds_count=self.quotes,
            uncompressed_bytes=self.size,league=self.league,season=self.season,
            format='jsonl.gz',contains_personal_account_data=False)
        # Publish the manifest last. Failed exports never produce a valid pair.
        # Atomic no-clobber publication, even if another writer won the name.
        self.output.hardlink_to(self.partial)
        owned=False
        try:
            with self.manifest_path.open('x',encoding='utf-8') as target:
                owned=True
                target.write(encoded(manifest)+'\n')
        except BaseException:
            self.output.unlink(missing_ok=True)
            if owned:self.manifest_path.unlink(missing_ok=True)
            raise
        self.finished=True
        return manifest

    def __exit__(self,*exc):
        self.zipped.close();self.handle.close()
        self.partial.unlink(missing_ok=True)


def write_archive(rows,output):
    with Writer(output) as writer:
        for row in rows:
            writer.add(row)
        return writer.finish()


def read_archive(archive,manifest):
    archive=Path(archive)
    exact(manifest,MANIFEST_KEYS)
    partition(manifest['league'],manifest['season'])
    if manifest['schema_version']!=2 or manifest['format']!='jsonl.gz' or manifest['contains_personal_account_data'] is not False:
        raise ValueError('Unsupported archive manifest')
    if manifest['file']!=archive.name or any(not isinstance(manifest[k],str) or not re.fullmatch('[a-f0-9]{64}',manifest[k]) for k in ('sha256','payload_sha256')):
        raise ValueError('Malformed archive manifest')
    for key,maximum in [('match_count',MAX_ROWS),('odds_count',MAX_ROWS*9),('uncompressed_bytes',MAX_PAYLOAD)]:
        if type(manifest[key]) is not int or not 0<=manifest[key]<=maximum:
            raise ValueError('Invalid manifest count')
    if not manifest['match_count'] or archive.stat().st_size>MAX_ASSET or sha256(archive)!=manifest['sha256']:
        raise ValueError('Archive checksum mismatch')
    count=quotes=size=last=0;digest=hashlib.sha256()
    with gzip.open(archive,'rb') as source:
        while True:
            line=source.readline(MAX_LINE+1)
            if not line:break
            size+=len(line)
            if len(line)>MAX_LINE or size>MAX_PAYLOAD or count>=MAX_ROWS:
                raise ValueError('Archive streaming limit exceeded')
            row=json.loads(line)
            validate(row,manifest['league'],manifest['season'])
            mid=row['match']['external_match_id']
            if mid<=last:raise ValueError('Duplicate or unordered archive match IDs')
            if line!=(encoded(row)+'\n').encode('utf-8'):
                raise ValueError('Noncanonical archive record')
            digest.update(line);count+=1;quotes+=len(row['odds']);last=mid
            yield row
    if (count,quotes,size)!=(manifest['match_count'],manifest['odds_count'],manifest['uncompressed_bytes']):
        raise ValueError('Archive count mismatch')
    if digest.hexdigest()!=manifest['payload_sha256']:
        raise ValueError('Archive payload checksum mismatch')
