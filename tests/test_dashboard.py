from html.parser import HTMLParser
from pathlib import Path
import subprocess
import shutil
import pytest


class DashboardMarkup(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.text = []
        self.ids = set()
        self.comments = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'template'}:
            self.hidden += 1
        self.ids.update(value for name, value in attrs if name == 'id')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'template'}:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)

    def handle_comment(self, data):
        self.comments.append(data)


def test_stitch_panels_and_safe_empty_states_are_retained():
    source = Path('src/api/dashboard.html').read_text(encoding='utf-8')
    parsed = DashboardMarkup()
    parsed.feed(source)
    text = ' '.join(' '.join(parsed.text).split())
    for sample in ['Arsenal', 'Manchester City', 'Galatasaray', 'Fenerbahçe', '48 Karşılaşma', '342', '99.98', '48,720', 'Tüm Sistemler Aktif']:
        assert sample not in text
    assert 'Veri bekleniyor' in text and 'Henüz veri yok' in text
    comments = ' '.join(parsed.comments)
    for panel in ['Big Scoreboard', 'Bookmaker Switcher Tabs', 'Asian Handicap', 'Asian Totals', 'Service Status List']:
        assert panel in comments
    assert {'league-nav', 'matches-body', 'match-detail', 'bookmaker-tabs', 'league-select', 'season-select', 'round-select', 'collection-progress'} <= parsed.ids
    for unsupported in ['AI İvmesi', 'AI Taktik', 'Topla Oynama', 'Tehlikeli Ataklar', 'Son Olaylar', 'Hakem:', 'Veri Kazıyıcı Kontrol Merkezi', 'Favoriler']:
        assert unsupported not in text
    assert '/dashboard.js' in source
    assert {'production-api', 'production-db', 'production-count', 'production-job', 'matches-scroll'} <= parsed.ids
    for unsupported in ['GitHub Keepalive Cron', 'Düşüş / Değer Artışı', 'Yükseliş / Risk', 'PRO notifications']:
        assert unsupported not in text
    assert '#league-sidebar {display:none;}' in source
    assert '#dashboard-shell {padding-left:0;}' in source
    assert '\ufffd' not in source


def test_dashboard_token_is_not_persisted():
    script = Path('src/api/dashboard.js').read_text(encoding='utf-8')
    assert 'localStorage' not in script and 'sessionStorage' not in script
    assert 'Bearer' not in script
    assert 'SCRAPER_API_TOKEN' not in Path('src/api/dashboard.html').read_text(encoding='utf-8')


@pytest.mark.parametrize('scenario', ['empty','unavailable','missing-controls','populated','upcoming',
    'books-fail','leagues-fail','seasons-fail','odds-fail','detail-fail','count-fail','status-fail','jobs-fail'])
def test_dashboard_runtime(scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable; supplied in CI')
    subprocess.run([node, 'tests/dashboard_runtime.cjs', scenario], check=True, capture_output=True, text=True)
