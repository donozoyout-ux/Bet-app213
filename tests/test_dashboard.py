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
    for panel in ['Bookmaker Switcher Tabs', 'Asian Handicap', 'Asian Totals']:
        assert panel in comments
    assert {'predictions-section','market-picks','statistics-detail','dashboard-shell','matches-body', 'match-detail', 'bookmaker-tabs', 'league-select', 'season-select', 'round-select', 'collection-progress'} <= parsed.ids
    assert '<aside' not in source and 'league-nav' not in source and 'pl-64' not in source
    assert '--app-bg:#EFF3F8' in Path('src/api/dashboard-market.css').read_text(encoding='utf-8')
    assert 'dashboard-market.css' in source and 'market-performance' in Path('src/api/dashboard-market.js').read_text(encoding='utf-8')
    assert source.index('id="predictions-section"') < source.index('id="history-filters"')
    assert 'Günün En Güçlü Tahminleri' in text and 'sidebar' not in text
    for unsupported in ['AI İvmesi', 'AI Taktik', 'Topla Oynama', 'Tehlikeli Ataklar', 'Son Olaylar', 'Hakem:', 'Veri Kazıyıcı Kontrol Merkezi', 'Favoriler']:
        assert unsupported not in text
    assert '/dashboard.js' in source
    assert '\ufffd' not in source


def test_dashboard_token_is_not_persisted():
    script = Path('src/api/dashboard.js').read_text(encoding='utf-8')
    assert 'localStorage' not in script and 'sessionStorage' not in script
    assert 'Bearer' not in script
    assert 'SCRAPER_API_TOKEN' not in Path('src/api/dashboard.html').read_text(encoding='utf-8')


def test_dashboard_executes_with_empty_unavailable_and_removed_controls():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js unavailable; supplied in CI')
    subprocess.run([node, 'tests/dashboard_runtime.cjs'], check=True, capture_output=True, text=True)


@pytest.mark.parametrize('scenario',['populated','insufficient','empty','failed','detail-failed','missing','race','filter','best','one','two','three','confidence','pagination'])
def test_predictions_frontend(scenario):
    node=shutil.which('node')
    if not node:pytest.skip('Node.js unavailable; supplied in CI')
    subprocess.run([node,'tests/predictions_runtime.cjs',scenario],check=True,capture_output=True,text=True)
